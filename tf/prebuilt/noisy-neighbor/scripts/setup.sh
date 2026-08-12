#!/usr/bin/env bash
#
# Setup for the noisy-neighbor task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#
#   1. applies the unexplained PriorityClass, the analytics namespace, and both
#      workloads (manifests/base.yaml)
#   2. waits for both to become Ready
#   3. asserts the seeded QoS classes actually hold: scoring-api Burstable (DEFECT A,
#      no limits despite a high-value PriorityClass), batch-loader BestEffort
#      (DEFECT B, no requests or limits at all)
#
# LIMITATION: this seed establishes the QoS structure the task's judgment turns on
# and gives batch-loader a real, growing memory footprint, but it does NOT make
# node-level kubelet eviction reliably reproducible on a local kind cluster. Real
# eviction depends on the node's evictionHard thresholds, which are whatever the kind
# node image ships and are unset by this stack; forcing it reliably would need
# explicit evictionHard tuning in the kind node config (kind_config.node.kubeadmConfigPatches
# or an equivalent kubelet config), which this stack does not yet do. The self-check
# below verifies structure (QoS class, resource shape), not that an eviction actually
# fired -- only a live cluster observation could show that, and it would be flaky by
# nature of depending on real memory pressure timing.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: noisy-neighbor is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
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
# Step 1. PriorityClass, namespace, both workloads.
# ---------------------------------------------------------------------------
echo "==> Applying the PriorityClass, analytics namespace, and both workloads..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

# ---------------------------------------------------------------------------
# Step 2. Wait for both to become Ready.
# ---------------------------------------------------------------------------
echo "==> Waiting for scoring-api and batch-loader to become available..."
kubectl -n "${NS}" rollout status deploy/scoring-api  --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/batch-loader --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts. status.qosClass is computed by the API server from the pod's
# own requests/limits, so asserting it is a genuine structural check.
# ---------------------------------------------------------------------------
echo "==> Verifying the seeded QoS classes..."

guarded_read scoring_qos kubectl -n "${NS}" get pods -l app=scoring-api -o jsonpath='{.items[*].status.qosClass}'
[ -n "${scoring_qos}" ] || fail "no scoring-api pods found to check QoS class on"
for q in ${scoring_qos}; do
  [ "${q}" = "Burstable" ] || fail "scoring-api pod has QoS class '${q}', expected Burstable (DEFECT A did not take)"
done

guarded_read batch_qos kubectl -n "${NS}" get pods -l app=batch-loader -o jsonpath='{.items[*].status.qosClass}'
[ -n "${batch_qos}" ] || fail "no batch-loader pods found to check QoS class on"
for q in ${batch_qos}; do
  [ "${q}" = "BestEffort" ] || fail "batch-loader pod has QoS class '${q}', expected BestEffort (DEFECT B did not take)"
done

guarded_read pc_value kubectl get priorityclass critical-scoring -o jsonpath='{.value}'
[ "${pc_value}" = "1000000" ] || fail "critical-scoring PriorityClass has value '${pc_value:-<empty>}', expected 1000000"

guarded_read bound_pc kubectl -n "${NS}" get pods -l app=scoring-api -o jsonpath='{.items[*].spec.priorityClassName}'
for p in ${bound_pc}; do
  [ "${p}" = "critical-scoring" ] || fail "scoring-api pod is not bound to critical-scoring (got '${p}')"
done

echo "==> Setup complete."
echo "    Seeded: analytics/scoring-api 2/2, Burstable, priorityClassName critical-scoring -- DEFECT A"
echo "            analytics/batch-loader 3/3, BestEffort, growing its own memory footprint -- DEFECT B"
echo "            PriorityClass critical-scoring value 1000000, annotated only acme.io/added and acme.io/ref"
echo "    Inspect: kubectl -n analytics get pods -o custom-columns=NAME:.metadata.name,QOS:.status.qosClass"
echo "             kubectl get priorityclass critical-scoring -o yaml"
