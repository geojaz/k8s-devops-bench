#!/usr/bin/env bash
#
# Does a freshly-seeded task actually hold still, and is it actually unsolved?
#
# THE BUG CLASS THIS EXISTS FOR. A task asserts things about the world that nobody
# checked against a live cluster. Real examples, all found the expensive way:
#
#   * a condition describing a state the API server rejects outright (C-062)
#   * a quota sized below the pod count the same task seeds (stuck-rollout, v1)
#   * an HPA with minReplicas 1 on an idle workload, next to an objective demanding
#     2 ready replicas: the seed defeats its own objective a few minutes in, with no
#     agent involved (working-as-intended)
#
# The seed's own self-check does not catch these. It answers "did the fault take",
# asked once, immediately. It does not ask "is this state STABLE" or "is this task
# already solved".
#
# Two questions, and the second needs TIME:
#
#   1. Is anything already true that should not be? (an objective satisfied at t0
#      means the task is solved before the agent starts)
#   2. Does the cluster drift on its own? Controllers reconcile. HPAs have a five
#      minute scale-down stabilization window. CronJobs fire. A snapshot taken the
#      instant seeding finishes can look perfect and be wrong by the time an agent
#      reads it.
#
#   ./hack/seed-stability.sh analytics                    # default 6m settle
#   ./hack/seed-stability.sh shop storefront --settle 420
#   ./hack/seed-stability.sh analytics --settle 60        # quick smoke
#
# Exit 0 = stable. Exit 1 = the cluster changed on its own; the diff says how.
set -euo pipefail

SETTLE=360
NAMESPACES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --settle) SETTLE="$2"; shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) NAMESPACES+=("$1"); shift ;;
  esac
done
[[ ${#NAMESPACES[@]} -gt 0 ]] || { echo "usage: $0 <namespace>... [--settle SECONDS]" >&2; exit 2; }

# Fields that change on every read and say nothing about drift. Without stripping
# these every diff is 100% noise and the check gets ignored, which is worse than
# not having it.
snapshot() {
  for ns in "${NAMESPACES[@]}"; do
    kubectl -n "$ns" get deploy,statefulset,daemonset,replicaset,hpa,resourcequota,pdb,cronjob,job \
      -o json 2>/dev/null | jq -S '
        [ .items[]? | {
            kind, name: .metadata.name, ns: .metadata.namespace,
            spec: (.spec | del(.template.metadata.creationTimestamp)),
            replicas:      (.status.replicas      // null),
            readyReplicas: (.status.readyReplicas // null),
            desired:       (.status.desiredReplicas // null),
            used:          (.status.used          // null),
            active:        (.status.active        // null | if type=="array" then length else . end)
          } ]'
  done
}

echo "==> Snapshot 1 (immediately after seed): ${NAMESPACES[*]}"
snapshot > /tmp/seed-t0.json
echo "    $(jq 'length' /tmp/seed-t0.json | paste -sd+ - | bc 2>/dev/null || echo '?') objects captured"

echo "==> Settling for ${SETTLE}s (HPA scale-down stabilization is 5m by default)..."
sleep "$SETTLE"

echo "==> Snapshot 2"
snapshot > /tmp/seed-t1.json

echo "==> Comparing"
if diff -q /tmp/seed-t0.json /tmp/seed-t1.json >/dev/null 2>&1; then
  printf '\033[32mSTABLE\033[0m: no drift across %ss. The state an agent reads is the state that was seeded.\n' "$SETTLE"
  exit 0
fi

printf '\033[31mUNSTABLE\033[0m: the cluster changed on its own within %ss.\n\n' "$SETTLE"
diff -u /tmp/seed-t0.json /tmp/seed-t1.json | grep -E '^[+-]' | grep -vE '^[+-]{3}' | head -40
cat <<'EOF'

An agent never sees the seeded state, so any objective or safeguard keyed to it is
unreliable. Usual causes, in the order they actually happen:

  * an HPA whose minReplicas differs from the replica count an objective asserts
  * a workload with no load, where the HPA drives toward minReplicas
  * a CronJob that fired between the two snapshots
  * a controller reconciling something the seed set imperatively
  * probes flapping a workload in and out of Ready

Fix the seed so the state is a fixed point, not a starting point.
EOF
exit 1
