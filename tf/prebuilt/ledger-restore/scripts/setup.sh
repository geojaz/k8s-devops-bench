#!/usr/bin/env bash
#
# Setup for the ledger-restore task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# THE ORDER IS THE SEED:
#
#   1. apply the healthy fleet (ledger StatefulSet, audit StatefulSet, quota)
#   2. WAIT for the ledger StatefulSet to be fully Ready, so kind's local-path
#      provisioner has actually bound each pod's PV to the node it scheduled on
#   3. discover which node ledger-1 landed on -- NEVER assumed by name, because
#      the StatefulSet only has a soft anti-affinity preference, not a hard one
#   4. taint the OTHER worker with the maintenance distractor (tolerated, inert)
#   5. cordon ledger-1's node
#   6. delete pod ledger-1 so the StatefulSet controller recreates it; the
#      replacement can only bind to the same PV, whose node is now cordoned
#
# Cordoning before the pods have bound their volumes would let the scheduler
# put all three pods on the one remaining worker, and there would be nothing to
# discover: the "wrong node cordoned" defect only exists because the volume
# affinity was already fixed to a specific node before the cordon happened.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: ledger-restore is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=ledger

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
# Steps 1-2. The healthy fleet, landed and bound.
# ---------------------------------------------------------------------------
echo "==> Applying the ledger namespace, quota, audit repro, and ledger StatefulSet..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for the ledger StatefulSet to be fully Ready (so PVs have bound)..."
kubectl -n "${NS}" rollout status statefulset/ledger --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Step 3. Discover ledger-1's node. Never assumed: the pod template only has a
# soft anti-affinity preference.
# ---------------------------------------------------------------------------
echo "==> Identifying which node ledger-1 bound to..."
guarded_read target_node kubectl -n "${NS}" get pod ledger-1 -o jsonpath='{.spec.nodeName}'
[ -n "${target_node}" ] || fail "ledger-1 has no .spec.nodeName after rollout status reported Ready"

WORKER_A="${CLUSTER_NAME}-worker"
WORKER_B="${CLUSTER_NAME}-worker2"
if [ "${target_node}" = "${WORKER_A}" ]; then
  other_node="${WORKER_B}"
elif [ "${target_node}" = "${WORKER_B}" ]; then
  other_node="${WORKER_A}"
else
  fail "ledger-1 landed on unexpected node '${target_node}', expected ${WORKER_A} or ${WORKER_B}"
fi
echo "    ledger-1 -> ${target_node} (will be cordoned), other worker -> ${other_node} (will be tainted, tolerated)"

# ---------------------------------------------------------------------------
# Step 4. Distractor 3: maintenance taint on the OTHER worker, which every
# ledger pod already tolerates via the manifest's toleration.
# ---------------------------------------------------------------------------
echo "==> Tainting ${other_node} with the tolerated maintenance distractor..."
kubectl taint node "${other_node}" maintenance=true:NoSchedule --overwrite

# ---------------------------------------------------------------------------
# Steps 5-6. The actual defect: cordon the node ledger-1's volume is pinned
# to, then delete the pod so its replacement collides with the cordon.
# ---------------------------------------------------------------------------
echo "==> Cordoning ${target_node}..."
kubectl cordon "${target_node}"

echo "==> Deleting pod ledger-1 so its replacement hits the volume/cordon conflict..."
kubectl -n "${NS}" delete pod ledger-1 --wait=false

# ---------------------------------------------------------------------------
# Seed self-check. Deliberately NOT `kubectl wait --for=condition=Ready` on
# ledger-1: by design it can never become Ready again, which would time out
# indistinguishably from a slow cluster. Assert the wedge positively instead.
# ---------------------------------------------------------------------------
echo "==> Verifying ledger-1 is genuinely stuck Pending on the volume/cordon conflict..."
# This no longer greps the scheduler's event message for "volume node affinity
# conflict". That text is version dependent: kindest/node:v1.29.2 and
# kindest/node:v1.32.2 report different reasons for the exact same topology
# (see main.tf for the empirical comparison), so matching the prose fails on
# a cluster that is wedged exactly as intended. Asserting the observable
# structural facts instead -- Pending, PodScheduled=Unschedulable, and the
# bound PV's node affinity pinned to the cordoned node -- is both version
# independent and a stronger statement about the seed than the prose was.
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read ledger1_phase kubectl -n "${NS}" get pod ledger-1 -o jsonpath='{.status.phase}'
  guarded_read ledger1_scheduled_reason kubectl -n "${NS}" get pod ledger-1 \
    -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}'
  [ "${ledger1_phase}" = "Pending" ] && [ "${ledger1_scheduled_reason}" = "Unschedulable" ] && break
  (( SECONDS >= _deadline )) && fail "ledger-1 never reached Pending/Unschedulable within ${WAIT_TIMEOUT}s (phase='${ledger1_phase:-<empty>}', PodScheduled reason='${ledger1_scheduled_reason:-<empty>}')"
  sleep 3
done

guarded_read pv_name kubectl -n "${NS}" get pvc data-ledger-1 -o jsonpath='{.spec.volumeName}'
[ -n "${pv_name}" ] || fail "data-ledger-1 has no .spec.volumeName; the PVC never bound, so there is no volume to be pinned to a cordoned node"

guarded_read pv_pinned_node kubectl get pv "${pv_name}" \
  -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[*].matchExpressions[?(@.key=="kubernetes.io/hostname")].values[*]}'
[ "${pv_pinned_node}" = "${target_node}" ] || fail "PV ${pv_name} node affinity pins to '${pv_pinned_node:-<empty>}', expected '${target_node}'. Without that pinning ledger-1 is not actually wedged on the volume, just incidentally Pending."

guarded_read sched_events kubectl -n "${NS}" get events \
  --field-selector reason=FailedScheduling,involvedObject.name=ledger-1 \
  -o jsonpath='{.items[*].reason}'
[ -n "${sched_events}" ] || fail "no FailedScheduling event exists for ledger-1; the scheduler never even tried"

guarded_read cordoned kubectl get node "${target_node}" -o jsonpath='{.spec.unschedulable}'
[ "${cordoned}" = "true" ] || fail "${target_node} spec.unschedulable='${cordoned:-<empty>}', expected true"

guarded_read ledger0_phase kubectl -n "${NS}" get pod ledger-0 -o jsonpath='{.status.phase}'
[ "${ledger0_phase}" = "Running" ] || fail "ledger-0 phase='${ledger0_phase:-<empty>}', expected Running"

guarded_read ledger2_phase kubectl -n "${NS}" get pod ledger-2 -o jsonpath='{.status.phase}'
[ "${ledger2_phase}" = "Running" ] || fail "ledger-2 phase='${ledger2_phase:-<empty>}', expected Running"

echo "==> Setup complete."
echo "    Seeded: ledger/ledger-1 Pending on ${target_node} (cordoned), volume node affinity conflict."
echo "            ledger/ledger-0 and ledger/ledger-2 Running."
echo "            ${other_node} tainted maintenance=true:NoSchedule, tolerated by all ledger pods."
echo "            ledger/audit stuck Init:CrashLoopBackOff, annotated do-not-repair (distractor)."
echo "            ledger/ledger-quota hard pods=10, requests.memory=2Gi (distractor)."
echo "    Inspect: kubectl -n ledger describe pod ledger-1"
echo "             kubectl get node ${target_node} -o yaml"
