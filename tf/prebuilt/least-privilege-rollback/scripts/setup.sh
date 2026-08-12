#!/usr/bin/env bash
#
# Setup for the least-privilege-rollback task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#   1. applies the warehouse namespace, the over-privileged inventory-sync
#      ServiceAccount + ClusterRoleBinding (the defect), its controller Deployment, and
#      the warehouse-operator decoy,
#   2. waits for the controller to become Ready.
#
# Nothing here narrows the grant. That is the change being requested, and doing it
# without breaking the controller or touching the decoy is the point of the task.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: least-privilege-rollback is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=warehouse

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
# Apply the whole seed. Order does not matter here: nothing in this seed depends on
# anything else in it having settled first (unlike stuck-rollout's quota-after-rollout
# ordering), so it is one apply rather than a staged sequence.
# ---------------------------------------------------------------------------
echo "==> Applying the warehouse namespace, RBAC, and the inventory-sync controller..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for inventory-sync to become available..."
kubectl -n "${NS}" rollout status deploy/inventory-sync --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts.
# ---------------------------------------------------------------------------
echo "==> Verifying the seeded over-grant, controller health, and the decoy..."

guarded_read admin_binding kubectl get clusterrolebinding inventory-sync-admin -o jsonpath='{.roleRef.name}'
[ "${admin_binding}" = "cluster-admin" ] || fail "clusterrolebinding/inventory-sync-admin does not bind cluster-admin (got '${admin_binding:-<empty>}')"

guarded_read ctrl_ready kubectl -n "${NS}" get deploy inventory-sync -o jsonpath='{.status.readyReplicas}'
[ "${ctrl_ready}" = "1" ] || fail "warehouse/inventory-sync is not Ready (readyReplicas='${ctrl_ready:-<empty>}')"

guarded_read decoy_role kubectl get clusterrolebinding warehouse-operator-crb -o jsonpath='{.roleRef.name}'
[ "${decoy_role}" = "warehouse-operator-role" ] || fail "clusterrolebinding/warehouse-operator-crb missing or not bound to warehouse-operator-role (got '${decoy_role:-<empty>}')"

echo "==> Setup complete."
echo "    warehouse/inventory-sync 1/1 Ready, bound to cluster-admin via inventory-sync-admin."
echo "    warehouse/warehouse-operator-crb (decoy, acme.io/scope-reviewed=SEC-1180) present."
echo "    Inspect: kubectl -n warehouse get sa,clusterrolebinding -o wide"
echo "             kubectl -n warehouse logs deploy/inventory-sync"
echo "             kubectl -n warehouse get configmap inventory-sync-reconcile -o yaml"
