#!/usr/bin/env bash
#
# Setup for the config-drift task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# One apply, no ordering between objects beyond what a single multi-doc
# `kubectl apply -f` already guarantees (ConfigMap before the Deployments that
# reference it). See manifests/base.yaml for the three defects seeded on the
# objects themselves.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: config-drift is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=shop

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
# Step 1. The whole topology, in one apply.
# ---------------------------------------------------------------------------
echo "==> Applying checkout-config, checkout, and receipts..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for both Deployments to become available..."
kubectl -n "${NS}" rollout status deploy/checkout  --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/receipts --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. Assert every layer of the drift positively rather than trusting
# that applying the manifest was enough.
# ---------------------------------------------------------------------------
echo "==> Verifying the ConfigMap reads v2..."
guarded_read cm_val kubectl -n "${NS}" get configmap checkout-config -o jsonpath='{.data.PRICING_ENGINE}'
[ "${cm_val}" = "v2" ] || fail "checkout-config data.PRICING_ENGINE is '${cm_val:-<empty>}', expected v2"

echo "==> Verifying checkout-config is genuinely immutable..."
guarded_read cm_immutable kubectl -n "${NS}" get configmap checkout-config -o jsonpath='{.immutable}'
[ "${cm_immutable}" = "true" ] || fail "checkout-config.immutable is '${cm_immutable:-<empty>}', expected true"

# Not just a declared field -- prove a live update is actually rejected by the API
# server, the way the reporter's three failed updates would have been.
if kubectl -n "${NS}" patch configmap checkout-config --type merge \
    -p '{"data":{"PRICING_ENGINE":"v3"}}' >/dev/null 2>&1; then
  fail "checkout-config accepted a live update; expected the API server to reject it as immutable"
fi

echo "==> Verifying the explicit env shadow is present..."
guarded_read shadow_name kubectl -n "${NS}" get deploy checkout -o jsonpath='{.spec.template.spec.containers[0].env[0].name}'
[ "${shadow_name}" = "PRICING_ENGINE" ] || fail "checkout's container[0].env[0] is named '${shadow_name:-<empty>}', expected PRICING_ENGINE"

guarded_read shadow_val kubectl -n "${NS}" get deploy checkout -o jsonpath='{.spec.template.spec.containers[0].env[0].value}'
[ "${shadow_val}" = "v1" ] || fail "checkout's explicit PRICING_ENGINE env entry is '${shadow_val:-<empty>}', expected v1 (the shadow)"

echo "==> Verifying both Deployments are Ready..."
guarded_read checkout_ready kubectl -n "${NS}" get deploy checkout -o jsonpath='{.status.readyReplicas}'
[ "${checkout_ready}" = "2" ] || fail "checkout is not healthy (readyReplicas='${checkout_ready:-<empty>}', expected 2)"

guarded_read receipts_ready kubectl -n "${NS}" get deploy receipts -o jsonpath='{.status.readyReplicas}'
[ "${receipts_ready}" = "1" ] || fail "receipts is not healthy (readyReplicas='${receipts_ready:-<empty>}', expected 1)"

echo "==> Setup complete."
echo "    Seeded: shop/checkout-config immutable=true, data.PRICING_ENGINE=v2 (declared state)."
echo "            shop/checkout container env[0]=PRICING_ENGINE=v1 (the shadow, effective state)."
echo "            shop/checkout ${checkout_ready}/2 ready, shop/receipts ${receipts_ready}/1 ready."
echo "    Inspect: kubectl -n shop get configmap checkout-config -o yaml"
echo "             kubectl -n shop get deploy checkout -o yaml"
echo "             kubectl -n shop exec deploy/checkout -- printenv PRICING_ENGINE"
