#!/usr/bin/env bash
#
# Setup for the nightly-backlog task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts.
#
# THE ORDER IS THE SEED:
#
#   1. apply the namespace, its pod quota (12), and the overlap-prone CronJob
#   2. pre-create 11 standalone "backlog" Job objects that each hold one pod slot for
#      9 minutes (sleep 540) and WAIT for the quota to actually reflect them
#   3. apply reconcile-api (2 replicas desired) LAST, once only 1 slot remains, so
#      exactly one replica is admitted and the other is refused by the quota
#
# WHY STEP 2 IS FAKED RATHER THAN LET THE REAL CRONJOB DO IT: the genuine pile-up
# mechanism (concurrencyPolicy Allow + a 9-minute job body against a 5-minute
# schedule) takes on the order of 15+ minutes of wall-clock time to actually
# accumulate a backlog. Pre-creating standalone Job objects that hold the same pod
# quota gets the cluster to the SAME observable state -- quota nearly exhausted,
# reconcile-api degraded -- at hand-off, instead of stalling `tofu apply` for a
# quarter of an hour. The CronJob itself (applied in step 1, from manifests/base.yaml)
# is left running afterwards with its real defects (concurrencyPolicy: Allow, no
# activeDeadlineSeconds), so the genuine mechanism keeps compounding on top of the
# pre-seeded backlog for as long as the agent takes to fix it.
#
# LIMITATION: the 11 pre-created Job objects are standalone (kubectl create job),
# not owned by the nightly-recon CronJob, so they will not appear in
# `nightly-recon`'s `status.active` list or its own Job history. An agent that reads
# `kubectl get jobs -n reconcile` will see them as separate objects rather than as
# CronJob-owned runs. This is a deliberate trade against the 15-minute real wall-clock
# cost of letting the CronJob actually build the backlog itself, and it is unproven
# whether a downstream check that specifically inspects CronJob ownership on the
# backlog jobs (as opposed to the quota and the degraded Deployment) would pass.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: nightly-backlog is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

NS=reconcile
BACKLOG_JOBS=11

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
# trigger changes, and step 2 cannot fill the quota to the exact count if a previous
# run's backlog jobs are still sitting in it.
# ---------------------------------------------------------------------------
echo "==> Clearing any backlog jobs left by a previous seed run..."
kubectl delete job -n "${NS}" -l seeded-by=nightly-backlog-setup --ignore-not-found --wait=true 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 1. Namespace, quota, and the CronJob.
# ---------------------------------------------------------------------------
echo "==> Applying the namespace, pod quota, and nightly-recon CronJob..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

# ---------------------------------------------------------------------------
# Step 2. Pre-create the backlog: standalone Jobs, one pod each, that each hold a
# quota slot for 9 minutes. Leaves exactly 1 of 12 pod slots free.
# ---------------------------------------------------------------------------
echo "==> Pre-creating ${BACKLOG_JOBS} backlog job pods (sleep 540 each)..."
for i in $(seq 1 "${BACKLOG_JOBS}"); do
  kubectl create job "nightly-recon-backlog-${i}" -n "${NS}" \
    --image=busybox:1.36 -- sh -c "sleep 540"
  kubectl label job "nightly-recon-backlog-${i}" -n "${NS}" seeded-by=nightly-backlog-setup --overwrite
done

echo "==> Waiting for the quota to reflect the backlog (expect used.pods=${BACKLOG_JOBS})..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read used kubectl get resourcequota reconcile-quota -n "${NS}" -o jsonpath='{.status.used.pods}'
  [ "${used}" = "${BACKLOG_JOBS}" ] && break
  (( SECONDS >= _deadline )) && fail "quota reconcile-quota never reached ${BACKLOG_JOBS} used pods; status.used.pods was '${used:-<empty>}'. The backlog jobs did not consume quota as expected."
  sleep 3
done

# ---------------------------------------------------------------------------
# Step 3. reconcile-api, last, with only 1 pod slot left.
# ---------------------------------------------------------------------------
echo "==> Applying reconcile-api (2 replicas desired, only 1 slot free)..."
kubectl apply -f "${MANIFESTS_DIR}/api.yaml"

echo "==> Waiting for reconcile-api's admitted replica to become ready..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read ready kubectl -n "${NS}" get deploy reconcile-api -o jsonpath='{.status.readyReplicas}'
  [ "${ready}" = "1" ] && break
  (( SECONDS >= _deadline )) && fail "reconcile-api never settled at readyReplicas=1; last observed '${ready:-<empty>}'"
  sleep 3
done

# ---------------------------------------------------------------------------
# Seed self-check. Assert the pile-up positively: the quota is at its ceiling, the
# API is genuinely short a replica because of it (not for some unrelated reason),
# and the quota's own event log names the API as the thing it refused.
# ---------------------------------------------------------------------------
echo "==> Verifying the pile-up is genuinely holding the API back..."

guarded_read used kubectl get resourcequota reconcile-quota -n "${NS}" -o jsonpath='{.status.used.pods}'
[ "${used}" -ge 11 ] 2>/dev/null || fail "reconcile-quota status.used.pods is '${used:-<empty>}', expected at/near its ceiling of 12"

guarded_read desired kubectl -n "${NS}" get deploy reconcile-api -o jsonpath='{.spec.replicas}'
guarded_read ready kubectl -n "${NS}" get deploy reconcile-api -o jsonpath='{.status.readyReplicas}'
ready="${ready:-0}"
(( ready < desired )) || fail "reconcile-api is NOT degraded: readyReplicas=${ready}, spec.replicas=${desired}"

# The FailedCreate event naming the quota is the evidence the agent has to find.
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read quota_denials kubectl -n "${NS}" get events \
    --field-selector reason=FailedCreate -o jsonpath='{.items[*].message}'
  [[ "${quota_denials}" == *"exceeded quota"* ]] && break
  (( SECONDS >= _deadline )) && fail "no FailedCreate event mentioning 'exceeded quota' appeared within ${WAIT_TIMEOUT}s. Without it the agent has no evidence the second replica was refused by the quota rather than by something else."
  sleep 3
done

echo "==> Setup complete."
echo "    Seeded: reconcile/reconcile-quota used.pods=${used}/12."
echo "            reconcile/reconcile-api readyReplicas=${ready}/${desired}, degraded by the pile-up."
echo "            reconcile/nightly-recon left running with concurrencyPolicy=Allow, no"
echo "            activeDeadlineSeconds, and 10/10 history limits -- the real mechanism keeps"
echo "            compounding the pre-seeded backlog above."
echo "    Inspect: kubectl -n reconcile get resourcequota reconcile-quota -o yaml"
echo "             kubectl -n reconcile get cronjob nightly-recon -o yaml"
echo "             kubectl -n reconcile get jobs"
