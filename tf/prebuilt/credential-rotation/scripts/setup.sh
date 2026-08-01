#!/usr/bin/env bash
#
# Setup for the credential-rotation task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#
#   1. applies the payments namespace, the trust set, the RBAC letting the verifier
#      read the issuer's Deployment spec, and both workloads (manifests/base.yaml)
#   2. computes "6 days from now" and annotates signing-key-v1 with it -- a static
#      manifest cannot template a date relative to apply time, so this has to happen
#      here rather than in base.yaml
#   3. waits for both workloads to become Ready
#   4. asserts the expiry annotation is genuinely ~6 days out and that nothing is
#      already flapping (both restart counts flat at zero) before handing off
#
# Nothing here is a defect: the current key is valid, both workloads are healthy, and
# every check an agent runs against live traffic passes. The only thing wrong is that
# a key expires in six days, which is exactly the kind of finding a survey has to
# produce on its own.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: credential-rotation is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
NS=payments

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

# Portable "N days from/before now" as YYYY-MM-DD: GNU date's `-d`, falling back to
# BSD/macOS date's `-v`. Used both to compute the seeded not-after date and, in the
# self-check, to re-derive the expected bounds without assuming which dialect is on
# PATH.
days_offset(){
  local n="$1"
  date -u -d "${n} days" +%Y-%m-%d 2>/dev/null || date -u -v"${n}"d +%Y-%m-%d
}

# ---------------------------------------------------------------------------
# Step 1. Namespace, trust set, RBAC, both workloads.
# ---------------------------------------------------------------------------
echo "==> Applying the payments namespace, trust set, RBAC, and both workloads..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

# ---------------------------------------------------------------------------
# Step 2. The only evidence anything needs doing: an expiry six days out.
# ---------------------------------------------------------------------------
NOT_AFTER="$(days_offset 6)"
echo "==> Annotating signing-key-v1 with acme.io/not-after=${NOT_AFTER}..."
kubectl -n "${NS}" annotate secret signing-key-v1 "acme.io/not-after=${NOT_AFTER}" --overwrite

# ---------------------------------------------------------------------------
# Step 3. Wait for both workloads.
# ---------------------------------------------------------------------------
echo "==> Waiting for issuer and verifier to become available..."
kubectl -n "${NS}" rollout status deploy/issuer   --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/verifier --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts.
# ---------------------------------------------------------------------------
echo "==> Verifying the expiry annotation and workload health..."

guarded_read seeded_not_after kubectl -n "${NS}" get secret signing-key-v1 -o jsonpath='{.metadata.annotations.acme\.io/not-after}'
[ -n "${seeded_not_after}" ] || fail "signing-key-v1 has no acme.io/not-after annotation"

seeded_epoch="$(date -u -d "${seeded_not_after}" +%s 2>/dev/null || date -u -jf %Y-%m-%d "${seeded_not_after}" +%s)"
now_epoch="$(date -u +%s)"
diff_days=$(( (seeded_epoch - now_epoch) / 86400 ))
(( diff_days >= 5 && diff_days <= 7 )) || fail "signing-key-v1's not-after (${seeded_not_after}) is ${diff_days} days out, expected ~6"

guarded_read issuer_ready kubectl -n "${NS}" get deploy issuer -o jsonpath='{.status.readyReplicas}'
[ "${issuer_ready}" = "2" ] || fail "payments/issuer is not Ready (readyReplicas='${issuer_ready:-<empty>}')"

guarded_read verifier_ready kubectl -n "${NS}" get deploy verifier -o jsonpath='{.status.readyReplicas}'
[ "${verifier_ready}" = "2" ] || fail "payments/verifier is not Ready (readyReplicas='${verifier_ready:-<empty>}')"

guarded_read issuer_restarts kubectl -n "${NS}" get pods -l app=issuer -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
for r in ${issuer_restarts}; do
  [ "${r}" = "0" ] || fail "payments/issuer already has a nonzero restart count ('${issuer_restarts}'); the seed must start from a flat baseline"
done

guarded_read verifier_restarts kubectl -n "${NS}" get pods -l app=verifier -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
for r in ${verifier_restarts}; do
  [ "${r}" = "0" ] || fail "payments/verifier already has a nonzero restart count ('${verifier_restarts}'); the seed must start from a flat baseline"
done

echo "==> Setup complete."
echo "    Seeded: payments/signing-key-v1 expires ${NOT_AFTER} (~6 days out)."
echo "            payments/issuer 2/2, payments/verifier 2/2, both restart counts flat at 0."
echo "    Inspect: kubectl -n payments get secret signing-key-v1 -o yaml"
echo "             kubectl -n payments get deploy,pods"
