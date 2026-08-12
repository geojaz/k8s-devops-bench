#!/usr/bin/env bash
#
# Setup for the budget-cut task. Runs from OUTSIDE the cluster during `tofu apply`,
# before the agent starts:
#   1. installs metrics-server (patched for kind's self-signed kubelet certs) so usage
#      data exists for the agent to size against,
#   2. applies the four analytics workloads at their seeded (over-provisioned) memory
#      requests, totalling 5120Mi.
#
# WHAT'S SEEDED: the task prompt narrates specific observed peaks per workload (ingest
# ~180Mi/pod, transform ~240Mi/pod, aggregator ~610Mi/pod, cache ~200Mi). Each
# workload's container actually holds a real allocation close to its narrated figure
# (a busybox script, ConfigMap-mounted, writes a fixed-size file into a Memory-medium
# emptyDir at /dev/shm and holds it -- see manifests/base.yaml), so `kubectl top`
# reports real, non-zero numbers that back up the prompt's narrative instead of
# contradicting it.
#
# LIMITATION: these are fixed-size allocations sized to roughly match the prompt's
# peaks, not a replay of real traffic-driven usage, so the numbers `kubectl top` reports
# will be close to but not exactly the narrated figures (a few Mi of container/shell
# overhead on top of the dd'd file). See manifests/base.yaml's top-of-file comment for
# the node RAM headroom this requires (~2.4Gi held across all 8 pods at once).
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: budget-cut is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
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
# Step 1. metrics-server, patched for kind.
# ---------------------------------------------------------------------------
echo "==> Installing metrics-server..."
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

echo "==> Patching metrics-server for kind's self-signed kubelet certs (--kubelet-insecure-tls)..."
kubectl patch deployment metrics-server -n kube-system --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

echo "==> Waiting for metrics-server to become available..."
kubectl -n kube-system wait --for=condition=Available deploy/metrics-server --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Step 2. The four analytics workloads, at their seeded (over-provisioned) requests.
# ---------------------------------------------------------------------------
echo "==> Applying the analytics namespace's four workloads..."
kubectl apply -f "${MANIFESTS_DIR}/base.yaml"

echo "==> Waiting for all four workloads to become available..."
kubectl -n "${NS}" rollout status deploy/ingest        --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/transform     --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status deploy/aggregator    --timeout="${WAIT_TIMEOUT}s"
kubectl -n "${NS}" rollout status statefulset/cache    --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts. Assert the four Deployments/StatefulSet report Ready, and that
# the summed memory requests across the namespace equal the seeded 5120Mi -- not the
# post-cut target, the STARTING total the task expects the agent to see.
# ---------------------------------------------------------------------------
echo "==> Verifying readiness and the seeded total requested memory..."

guarded_read ingest_ready kubectl -n "${NS}" get deploy ingest -o jsonpath='{.status.readyReplicas}'
[ "${ingest_ready}" = "3" ] || fail "analytics/ingest is not Ready (readyReplicas='${ingest_ready:-<empty>}')"

guarded_read transform_ready kubectl -n "${NS}" get deploy transform -o jsonpath='{.status.readyReplicas}'
[ "${transform_ready}" = "2" ] || fail "analytics/transform is not Ready (readyReplicas='${transform_ready:-<empty>}')"

guarded_read aggregator_ready kubectl -n "${NS}" get deploy aggregator -o jsonpath='{.status.readyReplicas}'
[ "${aggregator_ready}" = "2" ] || fail "analytics/aggregator is not Ready (readyReplicas='${aggregator_ready:-<empty>}')"

guarded_read cache_ready kubectl -n "${NS}" get statefulset cache -o jsonpath='{.status.readyReplicas}'
[ "${cache_ready}" = "1" ] || fail "analytics/cache is not Ready (readyReplicas='${cache_ready:-<empty>}')"

# Summed memory requests across every container in the namespace.
#
# Deliberately awk rather than an inline python program. A `python3 -c` body wrapped in
# SINGLE quotes cannot contain escaped double quotes: the shell passes the backslashes
# through literally, python then fails to COMPILE the program, and because that is a
# SyntaxError it fires on every run rather than only on the branch it appears to guard.
# That exact bug failed this seed on its first real apply, after every other step had
# already succeeded. This pipeline has no quoting hazard and no interpreter dependency.
#
# A container with no memory request simply contributes nothing, so the total comes out
# low and the assertion below reports the real number, which is the diagnostic we wanted
# from the removed error branch anyway.
total_mi="$(kubectl -n "${NS}" get pods \
  -o jsonpath='{range .items[*].spec.containers[*]}{.resources.requests.memory}{"\n"}{end}' \
  | sed 's/Mi$//' | awk '{s += $1} END {print s + 0}')"
[ "${total_mi}" = "5120" ] || fail "seeded total requested memory in analytics is ${total_mi}Mi, expected 5120Mi (ingest 3x512 + transform 2x768 + aggregator 2x640 + cache 1x768)"

# ---------------------------------------------------------------------------
# Extra self-check: prove the narrated memory peaks are real, not just prompt
# text. metrics-server needs a scrape cycle or two after pods start before it
# reports non-zero, so this polls rather than reading once. If any workload
# still reads 0Mi after the timeout, its ConfigMap-mounted load script isn't
# holding its allocation, and the fact the task turns on -- aggregator sitting
# close to its request while the others have real headroom -- goes back to
# being unobservable.
# ---------------------------------------------------------------------------
echo "==> Waiting for all four workloads to report real memory usage..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read ingest_mem     kubectl -n "${NS}" top pod -l app=ingest     --no-headers
  guarded_read transform_mem  kubectl -n "${NS}" top pod -l app=transform  --no-headers
  guarded_read aggregator_mem kubectl -n "${NS}" top pod -l app=aggregator --no-headers
  guarded_read cache_mem      kubectl -n "${NS}" top pod -l app=cache      --no-headers

  all_nonzero=1
  for lines in "${ingest_mem}" "${transform_mem}" "${aggregator_mem}" "${cache_mem}"; do
    if [ -z "${lines}" ] || printf '%s\n' "${lines}" | awk '{print $3}' | grep -qx '0Mi'; then
      all_nonzero=0
      break
    fi
  done
  [ "${all_nonzero}" -eq 1 ] && break

  (( SECONDS >= _deadline )) && fail "one or more analytics workloads still report 0Mi memory usage after ${WAIT_TIMEOUT}s (ingest='${ingest_mem:-<empty>}' transform='${transform_mem:-<empty>}' aggregator='${aggregator_mem:-<empty>}' cache='${cache_mem:-<empty>}')"
  sleep 5
done

# aggregator is the discriminating workload: its held allocation must land close to
# its 640Mi request, not just above zero, or the task's central judgment call (it's
# correctly sized, do not cut it) has no evidence behind it.
agg_avg_mi="$(printf '%s\n' "${aggregator_mem}" | awk '{gsub("Mi","",$3); s+=$3; n++} END {print (n>0) ? int(s/n) : -1}')"
[ "${agg_avg_mi}" -ge 0 ] || fail "could not parse aggregator memory usage from kubectl top output: ${aggregator_mem}"
agg_diff_mi=$(( agg_avg_mi > 640 ? agg_avg_mi - 640 : 640 - agg_avg_mi ))
[ "${agg_diff_mi}" -le 100 ] || fail "aggregator's average memory usage (${agg_avg_mi}Mi) is not within ~100Mi of its 640Mi request (diff=${agg_diff_mi}Mi): ${aggregator_mem}"

echo "==> Setup complete."
echo "    analytics: ingest 3/3, transform 2/2, aggregator 2/2, cache 1/1, all Ready."
echo "    Total requested memory: ${total_mi}Mi. Target per the prompt: <= 3072Mi."
echo "    All four workloads report real non-zero memory usage; aggregator averages ${agg_avg_mi}Mi (within ~100Mi of its 640Mi request)."
echo "    Inspect: kubectl -n analytics get deploy,statefulset -o wide"
echo "             kubectl -n analytics top pods"
