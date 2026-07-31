#!/usr/bin/env bash
#
# Setup for the t024-quota-scaleup task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#   1. applies the seed manifests (Namespace, ResourceQuota, Deployment,
#      Service) as ONE `kubectl apply -f` over a single multi-document file, in
#      that exact document order,
#   2. asserts the seeded fault actually took: the 500m CPU ResourceQuota
#      admits only 2 of storefront's 4 requested replicas.
#
# Document order in manifests/seed.yaml is load-bearing: kubectl applies a
# multi-document file's objects sequentially, in file order, over separate API
# calls, so applying the ResourceQuota ahead of the Deployment gives the quota
# admission plugin a real window to see it before the ReplicaSet controller
# starts creating pods. On a real GKE run where the quota was created alongside
# or after the Deployment, the ReplicaSet controller created all 4 pods before
# the quota was enforcing at all: 4 SuccessfulCreate events, zero FailedCreate,
# the seeded fault silently did not take. A seed that can't prove its condition
# holds has failed before the experiment starts, so this script asserts the
# seeded state below rather than assuming the ordering alone was enough.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" == "gcp" ]]; then
  echo "==> Fetching GKE credentials for cluster ${CLUSTER_NAME:?} in project ${PROJECT_ID:?} (${LOCATION:?})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${LOCATION}" --project "${PROJECT_ID}"
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

# guarded_read: a bounded poll's guarded read must tell "the field/object
# isn't populated yet, or genuinely doesn't exist (NotFound)" -- keep polling,
# that's what a converging ResourceQuota status looks like -- apart from "the
# query itself is malformed, a client-side kubectl parse/usage error" -- a bug
# in this check, not the cluster, that must fail immediately with kubectl's
# own stderr rather than spin to timeout looking exactly like an unsatisfied
# condition. (A bracket-quoted jsonpath map key, e.g.
# status.used["requests.cpu"], is exactly that kind of bug: it's the dialect
# Python's jsonpath_ng.ext accepts, not kubectl's Go JSONPath, which rejects
# it outright with "invalid array index" -- see the jsonpath below, which
# uses kubectl's own escaped-dot dialect instead.) Kept consistent with the
# `guarded_read` the factory compiler now generates into every seed.sh/
# verify.sh (devops-bench-factory's compiler/signals.py GUARD_PREAMBLE).
_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read(){ local __v="$1"; shift; local __out __rc; __out="$("$@" 2>"$_ERRFILE")"; __rc=$?; if [ $__rc -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2; exit 1; fi; printf -v "$__v" '%s' "$__out"; }

echo "==> Applying seed manifests (Namespace, ResourceQuota, Deployment, Service, in that order)..."
kubectl apply -f "${MANIFESTS_DIR}/seed.yaml"

echo "==> Waiting for the ResourceQuota to actually gate the storefront scale-up..."
# Deliberately NOT `kubectl wait --for=condition=Available deploy --all` (the
# pattern the other prebuilt stacks use to start the agent from a healthy
# fleet): that would hang forever here by design. Only 2 of storefront's 4
# requested replicas can ever be admitted under the 500m quota cap, so its
# Available condition can never reach the desired replica count. Assert the
# seeded fault directly instead of waiting on a condition it can never satisfy.
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  # guarded_read (defined above): a nonzero kubectl exit because the quota
  # object/field isn't visible yet must not kill the script under `set -e`,
  # and reads as "not yet satisfied" -- but a malformed query is a bug in
  # this check and fails immediately instead of spinning to timeout.
  guarded_read used kubectl get resourcequota app-quota -n shop -o jsonpath='{.status.used.requests\.cpu}'
  [ "${used}" = "500m" ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: expected resourcequota/app-quota status.used[\"requests.cpu\"] to reach '500m' (2 of 4 storefront replicas admitted, the other 2 rejected) within ${WAIT_TIMEOUT}s; last observed value was '${used:-<empty, path absent>}'" >&2
    exit 1
  fi
  sleep 3
done

echo "==> Setup complete."
echo "    Seeded: shop/app-quota (requests.cpu: 500m) caps shop/storefront to 2 of 4 replicas."
echo "    Inspect: kubectl -n shop get resourcequota app-quota -o yaml"
echo "             kubectl -n shop get rs -o wide"
echo "             kubectl -n shop get events --field-selector reason=FailedCreate"
