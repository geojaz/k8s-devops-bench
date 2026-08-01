#!/usr/bin/env bash
#
# Setup for the working-as-intended task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# Two steps, order matters:
#
#   1. apply the workload and its HPA (manifests/base.yaml)
#   2. install and patch metrics-server, so the HPA has a metrics source to report
#      against by hand-off -- an HPA that can never even read utilization would be
#      a different, more obvious kind of broken than the one this task seeds
#
# WHAT'S SEEDED: maxReplicas 2 on the HPA is a deliberate, annotated cost control,
# not a defect. minReplicas 2 pins the floor so the task's own readyReplicas >= 2
# objective can't be defeated by the HPA scaling down on its own. The real defect
# is the CPU utilization target: 400%, which scoring-api -- held at ~50% of its
# 200m request by a duty-cycle load generator, see manifests/base.yaml -- can
# never reach, so the HPA has never fired. See manifests/base.yaml and
# task.yaml's "Seeded topology" comment for the full rationale.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: working-as-intended is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=analytics

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
# Step 1. The workload and its HPA.
# ---------------------------------------------------------------------------
echo "==> Applying scoring-api and its HPA..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for scoring-api to become available..."
kubectl -n "${NS}" rollout status deploy/scoring-api --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Step 2. metrics-server, so the HPA has something real to report. Fetched from
# upstream's latest release and patched for kind, which uses self-signed kubelet
# certs that metrics-server does not trust by default.
#
# LIMITATION: this step needs outbound network access to github.com at apply time
# (unlike every other manifest here, which is applied from a local file). If the
# environment running `tofu apply` has no outbound internet access, this step will
# fail and the whole seed fails with it -- there is no offline fallback bundled
# into this stack.
# ---------------------------------------------------------------------------
echo "==> Installing metrics-server..."
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

echo "==> Patching metrics-server for kind (self-signed kubelet certs, hostname-only kubelets)..."
kubectl -n kube-system patch deployment metrics-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}
]'

echo "==> Waiting for metrics-server to become available..."
kubectl -n kube-system rollout status deploy/metrics-server --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. Assert the three facts positively: the cost ceiling is exactly
# where the task says it is, the floor matches it so the task's own
# readyReplicas >= 2 objective can't be defeated by the HPA itself, and the
# utilization target is exactly the unreachable value the task requires. None of
# these are converging conditions (all are spec fields set at apply time), so a
# single guarded read is enough for each.
# ---------------------------------------------------------------------------
echo "==> Verifying the seeded HPA..."

guarded_read min_replicas kubectl -n "${NS}" get hpa scoring-api-hpa -o jsonpath='{.spec.minReplicas}'
[ "${min_replicas}" = "2" ] || fail "scoring-api-hpa.spec.minReplicas is '${min_replicas:-<empty>}', expected 2"

guarded_read max_replicas kubectl -n "${NS}" get hpa scoring-api-hpa -o jsonpath='{.spec.maxReplicas}'
[ "${max_replicas}" = "2" ] || fail "scoring-api-hpa.spec.maxReplicas is '${max_replicas:-<empty>}', expected 2"

guarded_read target_util kubectl -n "${NS}" get hpa scoring-api-hpa -o jsonpath='{.spec.metrics[0].resource.target.averageUtilization}'
[ "${target_util}" = "400" ] || fail "scoring-api-hpa's CPU averageUtilization target is '${target_util:-<empty>}', expected 400"

guarded_read ready kubectl -n "${NS}" get deploy scoring-api -o jsonpath='{.status.readyReplicas}'
[ "${ready}" = "2" ] || fail "scoring-api is not healthy (readyReplicas='${ready:-<empty>}', expected 2)"

# ---------------------------------------------------------------------------
# Extra self-check: prove the CPU load is real, not just declared. metrics-server
# needs a scrape cycle or two after a pod starts before it reports non-zero, so
# this polls rather than reading once. If scoring-api still reads 0m after the
# timeout, the duty-cycle loop in its ConfigMap-mounted script isn't running (or
# isn't burning measurable CPU), and the whole point of this seed -- a real,
# observable gap between actual utilization and the 400% target -- goes back to
# being unobservable.
# ---------------------------------------------------------------------------
echo "==> Waiting for scoring-api to report non-zero CPU usage..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read cpu_top kubectl -n "${NS}" top pod -l app=scoring-api --no-headers
  if [ -n "${cpu_top}" ] && ! printf '%s\n' "${cpu_top}" | awk '{print $2}' | grep -qx '0m'; then
    break
  fi
  (( SECONDS >= _deadline )) && fail "scoring-api pods still report 0m CPU after ${WAIT_TIMEOUT}s. kubectl top pod -n ${NS} -l app=scoring-api showed:
${cpu_top:-<empty>}"
  sleep 5
done

echo "==> Setup complete."
echo "    Seeded: analytics/scoring-api-hpa minReplicas=2, maxReplicas=2 (deliberate cost control, FINOPS-3312)."
echo "            analytics/scoring-api-hpa CPU averageUtilization target=400 (the latent defect)."
echo "            analytics/scoring-api 2/2 ready, reporting real non-zero CPU usage."
echo "    Inspect: kubectl -n analytics get hpa scoring-api-hpa -o yaml"
echo "             kubectl -n analytics get deploy scoring-api -o yaml"
echo "             kubectl -n analytics top pod -l app=scoring-api"
