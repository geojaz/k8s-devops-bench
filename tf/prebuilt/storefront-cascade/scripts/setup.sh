#!/usr/bin/env bash
#
# Setup for the storefront-cascade task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# Unlike stuck-rollout, none of the five defects here is a race against a
# controller: the readiness-probe port, the inflated request, the dropped
# label, the tight PDB, and the missing allow policy are all correct-from-
# creation-time facts, not something that only becomes true after a rollout
# settles. So there is exactly one manifest apply, followed by a bounded
# self-check that waits for the cluster to have actually evaluated the
# defects (probes run, endpoints computed) before asserting on them.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: storefront-cascade is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

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
# Apply the whole seed: three namespaces, five defects, two uninvolved
# workloads (canary, payments/ledger).
# ---------------------------------------------------------------------------
echo "==> Applying the storefront seed..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

# ---------------------------------------------------------------------------
# Wait for the pieces that are meant to come up healthy, so a broken seed
# manifest (not the intended defects) fails loudly here instead of showing up
# as a confusing self-check failure below.
# ---------------------------------------------------------------------------
echo "==> Waiting for the canary and the uninvolved sibling to come up..."
kubectl -n storefront rollout status deploy/checkout-canary --timeout="${WAIT_TIMEOUT}s"
kubectl -n payments rollout status deploy/ledger --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Self-check. Deliberately NOT `kubectl rollout status deploy/checkout`: that
# can only ever time out here by design (the probe never succeeds), which is
# indistinguishable from a cluster that is merely slow. Assert the three
# defect symptoms positively instead, after waiting for the controllers to
# have actually evaluated them at least once.
# ---------------------------------------------------------------------------
echo "==> Waiting for checkout's readiness probe to have run and failed at least once..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read unhealthy_events kubectl -n storefront get events \
    --field-selector reason=Unhealthy -o jsonpath='{.items[*].involvedObject.name}'
  [[ "${unhealthy_events}" == *checkout-* ]] && break
  (( SECONDS >= _deadline )) && fail "no Unhealthy event for a checkout pod appeared within ${WAIT_TIMEOUT}s; the readiness probe defect (port 8081 against a container serving 8080) may not have actually taken hold"
  sleep 3
done

echo "==> Asserting checkout has fewer than 3 ready replicas..."
guarded_read checkout_ready kubectl -n storefront get deploy checkout -o jsonpath='{.status.readyReplicas}'
checkout_ready="${checkout_ready:-0}"
(( checkout_ready < 3 )) || fail "checkout readyReplicas=${checkout_ready}, expected fewer than 3 (readiness probe should be pointed at the wrong port)"

echo "==> Asserting the checkout Service has no endpoints..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read checkout_addrs kubectl -n storefront get endpointslice \
    -l kubernetes.io/service-name=checkout -o jsonpath='{.items[*].endpoints[*].addresses}'
  [[ -z "${checkout_addrs// /}" ]] && break
  (( SECONDS >= _deadline )) && fail "checkout Service has endpoint addresses (${checkout_addrs}), expected none -- the selector (version=stable) should match no pod"
  sleep 3
done

echo "==> Asserting the cross-namespace probe Deployment is NOT ready..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read probe_replicas kubectl -n storefront-client get deploy probe -o jsonpath='{.status.replicas}'
  [ "${probe_replicas:-0}" -ge 1 ] && break
  (( SECONDS >= _deadline )) && fail "probe Deployment never reported any replicas; cannot evaluate its readiness"
  sleep 3
done
guarded_read probe_ready kubectl -n storefront-client get deploy probe -o jsonpath='{.status.readyReplicas}'
[ -z "${probe_ready}" ] || [ "${probe_ready}" = "0" ] || fail "probe deployment readyReplicas='${probe_ready}', expected 0 or unset -- it should not be able to reach checkout yet (probe port, Service selector, and NetworkPolicy are all still broken)"

echo "==> Sanity: canary and payments/ledger are healthy and untouched by the seed's own defects..."
guarded_read canary_ready kubectl -n storefront get deploy checkout-canary -o jsonpath='{.status.readyReplicas}'
[ "${canary_ready}" = "1" ] || fail "checkout-canary readyReplicas='${canary_ready:-<empty>}', expected 1"

guarded_read payments_ready kubectl -n payments get deploy ledger -o jsonpath='{.status.readyReplicas}'
[ "${payments_ready}" = "2" ] || fail "payments/ledger readyReplicas='${payments_ready:-<empty>}', expected 2"

echo "==> Setup complete."
echo "    Seeded: storefront/checkout 3 desired, ${checkout_ready:-0} ready (probe port 8081 vs container 8080)."
echo "            storefront/checkout Service has no endpoints (selector requires version=stable)."
echo "            storefront/checkout-pdb minAvailable=3/3 -> disruptionsAllowed=0."
echo "            storefront/default-deny-ingress present, no allow policy for storefront-client."
echo "            storefront-client/probe not ready (cross-namespace request cannot land)."
echo "            storefront/checkout-canary 1/1, payments/ledger 2/2, both uninvolved."
echo "    Inspect: kubectl -n storefront get deploy checkout -o yaml"
echo "             kubectl -n storefront get events --field-selector reason=Unhealthy"
echo "             kubectl -n storefront get pdb checkout-pdb -o yaml"
