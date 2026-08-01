#!/usr/bin/env bash
#
# Setup for the two-phase-drain task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# Nothing here is meant to be broken. The only "defect" is two annotations
# documenting overdue maintenance; every workload has to come up, and stay,
# fully healthy. The self-check below is therefore a plain health check, not a
# wedge assertion -- this is the one seed in the set that succeeds by proving
# a negative (nothing is wrong yet).
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: two-phase-drain is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=core
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

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
# Apply the fleet.
# ---------------------------------------------------------------------------
echo "==> Applying the core namespace and its three workloads..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Pinning license-daemon to ${WORKER1}..."
kubectl -n "${NS}" patch deploy license-daemon --type=merge \
  -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"${WORKER1}\"}}}}}"

# Only NOW does a license-daemon pod come into existence, and it can only schedule where
# the selector permits. See the replicas: 0 comment in manifests/base.yaml.
kubectl -n "${NS}" scale deploy license-daemon --replicas=1

echo "==> Waiting for the fleet to become fully Ready..."
kubectl -n "${NS}" rollout status statefulset/quorum-store --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/gateway --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/license-daemon --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# The only seeded fact: both workers are overdue for a kernel patch. This is
# the sole evidence the task exists -- discoverable only by reading the node
# objects, since nothing is failing.
# ---------------------------------------------------------------------------
echo "==> Annotating both workers as patch-pending..."
kubectl annotate node "${WORKER1}" acme.io/patch-pending="KERNEL-2026-08" acme.io/patch-by="2026-08-02" --overwrite
kubectl annotate node "${WORKER2}" acme.io/patch-pending="KERNEL-2026-08" acme.io/patch-by="2026-08-02" --overwrite

# ---------------------------------------------------------------------------
# Seed self-check. Positive assertions only: every workload Ready, both PDBs
# present, both annotations in place, both nodes schedulable.
# ---------------------------------------------------------------------------
echo "==> Verifying the fleet is fully healthy and the patch window is recorded..."

guarded_read qs_ready kubectl -n "${NS}" get statefulset quorum-store -o jsonpath='{.status.readyReplicas}'
[ "${qs_ready:-0}" = "3" ] || fail "quorum-store readyReplicas='${qs_ready:-<empty>}', expected 3"

guarded_read gw_ready kubectl -n "${NS}" get deploy gateway -o jsonpath='{.status.readyReplicas}'
[ "${gw_ready:-0}" = "4" ] || fail "gateway readyReplicas='${gw_ready:-<empty>}', expected 4"

guarded_read ld_ready kubectl -n "${NS}" get deploy license-daemon -o jsonpath='{.status.readyReplicas}'
[ "${ld_ready:-0}" = "1" ] || fail "license-daemon readyReplicas='${ld_ready:-<empty>}', expected 1"

# Running pods only, and items[*] rather than items[0]. A Terminating pod is still
# returned by `get pod`, and items[0] samples an arbitrary element of that list, so the
# check could read a dying pod on the wrong node. With exactly one Running pod this
# yields one name; if it ever yields two, the comparison fails loudly instead of picking.
guarded_read ld_node kubectl -n "${NS}" get pod -l app=license-daemon \
  --field-selector=status.phase=Running -o jsonpath='{.items[*].spec.nodeName}'
[ "${ld_node}" = "${WORKER1}" ] || fail "license-daemon pod is on '${ld_node:-<empty>}', expected ${WORKER1}"

guarded_read qs_pdb kubectl -n "${NS}" get pdb quorum-store-pdb -o jsonpath='{.metadata.name}'
[ "${qs_pdb}" = "quorum-store-pdb" ] || fail "quorum-store-pdb does not exist"

guarded_read gw_pdb kubectl -n "${NS}" get pdb gateway-pdb -o jsonpath='{.metadata.name}'
[ "${gw_pdb}" = "gateway-pdb" ] || fail "gateway-pdb does not exist"

for node in "${WORKER1}" "${WORKER2}"; do
  guarded_read patch_ann kubectl get node "${node}" -o jsonpath='{.metadata.annotations.acme\.io/patch-pending}'
  [ "${patch_ann}" = "KERNEL-2026-08" ] || fail "${node} missing acme.io/patch-pending annotation (got '${patch_ann:-<empty>}')"

  guarded_read schedulable kubectl get node "${node}" -o jsonpath='{.spec.unschedulable}'
  [ -z "${schedulable}" ] || fail "${node} spec.unschedulable='${schedulable}', expected absent (node should be schedulable at hand-off)"
done

echo "==> Setup complete."
echo "    Seeded: core/quorum-store 3/3, core/gateway 4/4, core/license-daemon 1/1, all Ready."
echo "            ${WORKER1} and ${WORKER2} both annotated acme.io/patch-pending=KERNEL-2026-08."
echo "            Both PDBs present, both nodes schedulable. Nothing is broken."
echo "    Inspect: kubectl get nodes -o json | jq '.items[].metadata.annotations'"
echo "             kubectl -n core get pdb"
