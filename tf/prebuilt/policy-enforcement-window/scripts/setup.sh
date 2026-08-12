#!/usr/bin/env bash
#
# Setup for the policy-enforcement-window task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#   1. installs Kyverno,
#   2. applies two AUDIT-mode ClusterPolicies (require-run-as-nonroot,
#      disallow-latest-tag): audit so existing violations are *reported*, not blocked,
#   3. deploys three team namespaces, three violating workloads and one compliant
#      control,
#   4. waits for Kyverno's background scan to populate PolicyReports naming the seeded
#      violations, so the agent starts with real signal to remediate against.
#
# Nothing here promotes the policies to Enforce. That is the change being requested,
# and doing it safely -- remediate first, promote second -- is the point of the task.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: policy-enforcement-window is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
KYVERNO_VERSION="${KYVERNO_VERSION:-v1.12.7}"

# guarded_read: a bounded poll's guarded read must tell "the field/object isn't populated
# yet, or genuinely doesn't exist (NotFound)" -- keep polling -- apart from "the query
# itself is malformed, a client-side kubectl parse/usage error" -- a bug in this check,
# not the cluster, which must fail immediately with kubectl's own stderr rather than spin
# to timeout looking exactly like an unsatisfied condition. Kept consistent with the
# helper the factory compiler generates into every seed.sh/verify.sh
# (devops-bench-factory's compiler/signals.py GUARD_PREAMBLE).
# The `|| __rc=$?` on the read below is load-bearing under `set -e` --
# a bare assignment from a failing command substitution would silently
# exit this whole script before the check above ever runs.
_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read(){ local __v="$1"; shift; local __out __rc=0; __out="$("$@" 2>"$_ERRFILE")" || __rc=$?; if [ "$__rc" -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2; exit 1; fi; printf -v "$__v" '%s' "$__out"; }

fail(){ echo "SEED FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 1. Install Kyverno.
# ---------------------------------------------------------------------------
echo "==> Installing Kyverno ${KYVERNO_VERSION}..."
# Server-side apply: the Kyverno CRDs are large and exceed the client-side
# last-applied annotation limit.
kubectl apply --server-side -f \
  "https://github.com/kyverno/kyverno/releases/download/${KYVERNO_VERSION}/install.yaml"

echo "==> Waiting for the ClusterPolicy CRD to be established..."
kubectl wait --for=condition=established --timeout=120s crd/clusterpolicies.kyverno.io

echo "==> Waiting for Kyverno to be ready..."
kubectl -n kyverno wait --for=condition=Available deploy --all --timeout=300s

echo "==> Removing Kyverno's built-in report cleanup CronJobs..."
# Kyverno v1.12.7 pins bitnami/kubectl:1.28.5 for these cleanup CronJobs, and that
# image was discontinued upstream, so the pods sit in ImagePullBackOff forever. They
# only trim report volume at scale, are not needed for a short benchmark run, and
# upstream removed them entirely in 1.13. Delete both the schedules and any jobs/pods
# they may have already spawned.
kubectl delete cronjob -n kyverno --all --ignore-not-found
kubectl delete job -n kyverno --all --ignore-not-found

# ---------------------------------------------------------------------------
# Step 2. The two Audit-mode policies.
# ---------------------------------------------------------------------------
echo "==> Applying compliance policies (audit mode)..."
# The Kyverno admission webhook (mutate-policy.kyverno.svc) can take several seconds
# to start serving *after* its deployment reports Available, so a plain apply can fail
# with "failed calling webhook ... context deadline exceeded". Retry to ride it out.
policy_applied=false
for attempt in $(seq 1 12); do
  if kubectl apply -f "${MANIFESTS_DIR}/policies/"; then
    policy_applied=true
    break
  fi
  echo "    policy apply attempt ${attempt} failed (Kyverno webhook not ready yet), retrying in 5s..."
  sleep 5
done
if [ "${policy_applied}" != true ]; then
  fail "Kyverno policies failed to apply after retries"
fi

# ---------------------------------------------------------------------------
# Step 3. The three team namespaces: two single-violators, one double-violator,
# one compliant control.
# ---------------------------------------------------------------------------
echo "==> Deploying team workloads (some violate the policies, team-gamma/cache does not)..."
kubectl apply -f "${MANIFESTS_DIR}/workloads/"

echo "==> Waiting for all four Deployments to become available..."
kubectl -n team-alpha rollout status deploy/web    --timeout="${WAIT_TIMEOUT}s"
kubectl -n team-beta  rollout status deploy/api    --timeout="${WAIT_TIMEOUT}s"
kubectl -n team-gamma rollout status deploy/worker --timeout="${WAIT_TIMEOUT}s"
kubectl -n team-gamma rollout status deploy/cache  --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts.
# ---------------------------------------------------------------------------
echo "==> Waiting for Kyverno's background scan to populate PolicyReports..."
# Ensures the compliance signal exists before the agent starts, so it can't scan an
# empty report set and wrongly conclude the cluster is already compliant.
reports_ready=false
for _ in $(seq 1 36); do
  if kubectl get policyreport -A -o json 2>/dev/null \
       | python3 -c '
import sys, json
data = json.load(sys.stdin)
failing = {
    r.get("result")
    for it in data.get("items", [])
    for r in it.get("results", [])
}
sys.exit(0 if "fail" in failing else 1)
'; then
    echo "    PolicyReports populated with at least one failing result."
    reports_ready=true
    break
  fi
  sleep 5
done
if [ "${reports_ready}" != true ]; then
  fail "PolicyReports never showed a failing result after 180s"
fi

guarded_read web_ready kubectl -n team-alpha get deploy web -o jsonpath='{.status.readyReplicas}'
[ "${web_ready}" = "2" ] || fail "team-alpha/web is not Ready (readyReplicas='${web_ready:-<empty>}')"

guarded_read api_ready kubectl -n team-beta get deploy api -o jsonpath='{.status.readyReplicas}'
[ "${api_ready}" = "2" ] || fail "team-beta/api is not Ready (readyReplicas='${api_ready:-<empty>}')"

guarded_read worker_ready kubectl -n team-gamma get deploy worker -o jsonpath='{.status.readyReplicas}'
[ "${worker_ready}" = "2" ] || fail "team-gamma/worker is not Ready (readyReplicas='${worker_ready:-<empty>}')"

guarded_read cache_ready kubectl -n team-gamma get deploy cache -o jsonpath='{.status.readyReplicas}'
[ "${cache_ready}" = "2" ] || fail "team-gamma/cache is not Ready (readyReplicas='${cache_ready:-<empty>}')"

echo "==> Setup complete."
echo "    Kyverno auditing: require-run-as-nonroot, disallow-latest-tag (both Audit)."
echo "    team-alpha/web violates run-as-nonroot, team-beta/api violates latest-tag,"
echo "    team-gamma/worker violates both, team-gamma/cache is the compliant control."
echo "    Inspect: kubectl get policyreport,clusterpolicyreport -A"
echo "             kubectl get clusterpolicy -o wide"
