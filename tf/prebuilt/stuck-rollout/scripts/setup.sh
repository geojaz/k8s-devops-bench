#!/usr/bin/env bash
#
# Setup for the stuck-rollout task. Runs from OUTSIDE the cluster during `tofu apply`,
# before the agent starts.
#
# THE ORDER IS THE SEED. Every step below has to happen after the one before it:
#
#   1. apply the healthy fleet (catalog 6 + search 2, permissive rollout strategy)
#   2. WAIT for both to land
#   3. roll catalog to a second revision and WAIT     -> leaves stale ReplicaSets behind,
#                                                        which the task asks the agent to trim
#   4. apply the ResourceQuota at exactly the settled count (8/8)
#   5. patch the strategy to maxUnavailable=0 (maxSurge stays 1)
#   6. push a third revision, which can never land
#
# Applying the quota before step 3 deadlocks the seed on itself: the setup rollouts need
# surge room the quota does not permit. Applying it after step 5 means the wedge never
# happens, because the surge succeeds. There is exactly one workable order.
#
# WHY THIS WEDGES: maxUnavailable=0 is an ordinary zero-downtime setting and is not a
# defect on its own. Against a quota already at its ceiling it deadlocks -- the rollout
# must surge a ninth pod, the quota refuses it, and the strategy forbids taking an old
# pod down to make room instead. Neither half is worth an alert. See condition C-214 in
# devops-bench-factory.
#
# NOT maxUnavailable=0 AND maxSurge=0: the API server rejects that outright ("may not be
# 0 when maxSurge is 0"), so that state cannot be seeded at all. Condition C-062 claimed
# otherwise and has been marked Invalid.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: stuck-rollout is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=shop
IMG_V1=nginx:1.25-alpine
IMG_V2=nginx:1.26-alpine
IMG_V3=nginx:1.27-alpine   # the revision that must never land

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
# Step 0. Make the seed re-runnable. `tofu apply` re-runs this whole script when the
# trigger changes, and step 1's rollouts cannot surge if a previous run's quota is
# still in place. Drop it first.
# ---------------------------------------------------------------------------
echo "==> Clearing any quota left by a previous seed run..."
kubectl delete resourcequota shop-quota -n "${NS}" --ignore-not-found

# ---------------------------------------------------------------------------
# Steps 1-2. The healthy fleet, landed.
# ---------------------------------------------------------------------------
echo "==> Applying the healthy fleet (catalog 6 replicas, search 2)..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for both workloads to become available..."
kubectl -n "${NS}" rollout status deploy/search  --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/catalog --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Step 3. A second revision that DOES land, purely to leave rollout history behind.
# ---------------------------------------------------------------------------
echo "==> Landing a second revision so there is stale rollout history to clean up..."
kubectl -n "${NS}" set image deploy/catalog "catalog=${IMG_V2}"
kubectl -n "${NS}" rollout status deploy/catalog --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Step 4. The quota, at exactly the settled count.
# ---------------------------------------------------------------------------
echo "==> Applying the pod quota at the settled count (expect 8/8)..."
kubectl apply -f "${MANIFESTS_DIR}/quota.yaml"

_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read used kubectl get resourcequota shop-quota -n "${NS}" -o jsonpath='{.status.used.pods}'
  [ "${used}" = "8" ] && break
  (( SECONDS >= _deadline )) && fail "quota shop-quota never reached its ceiling; status.used.pods was '${used:-<empty>}', expected 8. The namespace is not at the settled pod count, so the rollout below will not wedge."
  sleep 3
done

# ---------------------------------------------------------------------------
# Steps 5-6. The wedge.
# ---------------------------------------------------------------------------
echo "==> Wedging the rollout (maxUnavailable=0 against a quota at its ceiling)..."
kubectl -n "${NS}" patch deploy/catalog --type=merge \
  -p '{"spec":{"strategy":{"rollingUpdate":{"maxUnavailable":0,"maxSurge":1}}}}'

echo "==> Pushing the revision that must never land..."
kubectl -n "${NS}" set image deploy/catalog "catalog=${IMG_V3}"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts. Deliberately NOT `kubectl rollout status`: that can only ever
# time out here by design, which is indistinguishable from a cluster that is merely slow.
# Assert the wedge positively instead.
# ---------------------------------------------------------------------------
echo "==> Verifying the rollout is genuinely wedged..."

guarded_read desired_img kubectl -n "${NS}" get deploy catalog -o jsonpath='{.spec.template.spec.containers[0].image}'
[ "${desired_img}" = "${IMG_V3}" ] || fail "catalog does not desire ${IMG_V3} (got '${desired_img}')"

# The FailedCreate event naming the quota is the evidence the agent has to find. If it
# never appears, the quota is not actually refusing the surge and the task has no signal.
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read quota_denials kubectl -n "${NS}" get events \
    --field-selector reason=FailedCreate -o jsonpath='{.items[*].message}'
  [[ "${quota_denials}" == *"exceeded quota"* ]] && break
  (( SECONDS >= _deadline )) && fail "no FailedCreate event mentioning 'exceeded quota' appeared within ${WAIT_TIMEOUT}s. Without it the agent has no evidence to diagnose from and the rollout is stuck for some other reason."
  sleep 3
done

guarded_read updated kubectl -n "${NS}" get deploy catalog -o jsonpath='{.status.updatedReplicas}'
updated="${updated:-0}"
(( updated < 6 )) || fail "rollout is NOT wedged: updatedReplicas=${updated}, expected fewer than 6"

guarded_read search_ready kubectl -n "${NS}" get deploy search -o jsonpath='{.status.readyReplicas}'
[ "${search_ready}" = "2" ] || fail "sibling 'search' is not healthy (readyReplicas='${search_ready:-<empty>}')"

rs_count="$(kubectl -n "${NS}" get rs -l app=catalog --no-headers | wc -l | tr -d ' ')"
(( rs_count >= 3 )) || fail "expected at least 3 catalog ReplicaSets for rollout history, found ${rs_count}"

echo "==> Setup complete."
echo "    Seeded: shop/catalog desires ${IMG_V3}, updatedReplicas=${updated}/6, wedged indefinitely."
echo "            shop/shop-quota pods 8/8, owner-annotated, not the agent's to edit."
echo "            ${rs_count} catalog ReplicaSets retained (revisionHistoryLimit 20)."
echo "            shop/search 2/2, uninvolved."
echo "    Inspect: kubectl -n shop get deploy catalog -o yaml"
echo "             kubectl -n shop get events --field-selector reason=FailedCreate"
echo "             kubectl -n shop get resourcequota shop-quota -o yaml"
