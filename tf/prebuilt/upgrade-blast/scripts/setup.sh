#!/usr/bin/env bash
#
# Setup for the upgrade-blast task. Runs from OUTSIDE the cluster during `tofu apply`,
# before the agent starts. Seeds the POST-upgrade state directly -- there is no
# simulated upgrade, just a controller already on the patched version:
#
#   1. installs ingress-nginx (upstream kind-specific provider manifest, pinned to
#      the post-upgrade v1.13.x line that enforces pathType semantics strictly)
#   2. waits for the controller to become Available and its admission webhook to be
#      patched with a CA bundle
#   3. annotates the controller Deployment with the CVE remediation record
#      (acme.io/cve, acme.io/security-signoff) that rules out a rollback -- this has
#      to be found by reading the object, not stated in the prompt
#   4. applies the edge namespace, five tiny backends, and the three Ingresses
#      (manifests/base.yaml); two of storefront's paths have declared pathType Exact
#      since they were written and the OLD controller silently tolerated it
#   5. asserts the seeded pathTypes and the CVE annotation actually hold
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" != "kind" ]]; then
  echo "SEED FAIL: upgrade-blast is kind-only, got INFRA_PROVIDER=${INFRA_PROVIDER}" >&2
  exit 1
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
# Pinned to the v1.13 line so the seeded controller image matches the task's
# verification_spec regex (controller:v1\.13\.).
INGRESS_NGINX_VERSION="${INGRESS_NGINX_VERSION:-controller-v1.13.0}"

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
# Steps 1-2. ingress-nginx at the post-upgrade version.
# ---------------------------------------------------------------------------
echo "==> Installing ingress-nginx (${INGRESS_NGINX_VERSION}, kind provider manifest)..."
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/kind/deploy.yaml"

echo "==> Waiting for the ingress-nginx controller to become Available..."
kubectl -n ingress-nginx wait --for=condition=Available deployment/ingress-nginx-controller --timeout="${WAIT_TIMEOUT}s"

echo "==> Waiting for the admission webhook's CA-bundle patch job to complete..."
kubectl -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch --timeout="${WAIT_TIMEOUT}s"

# NEITHER WAIT ABOVE IS SUFFICIENT, measured on a real run. The Deployment reports
# Available and the CA-bundle Job reports complete while the validating webhook is
# still not accepting connections, because the webhook listens on 8443 INSIDE the
# controller pod and the ingress-nginx-controller-admission Service can still have
# zero ready endpoints. Applying an Ingress in that window fails with:
#
#   Internal error occurred: failed calling webhook "validate.nginx.ingress.kubernetes.io":
#   ... dial tcp <clusterIP>:443: connect: connection refused
#
# Available is a statement about the Deployment's replicas; it says nothing about
# whether a second, unrelated port on those pods is serving yet.
echo "==> Waiting for the controller pod itself to be Ready..."
kubectl -n ingress-nginx wait --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout="${WAIT_TIMEOUT}s"

echo "==> Waiting for the admission webhook Service to have ready endpoints..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read _eps kubectl -n ingress-nginx get endpoints ingress-nginx-controller-admission \
    -o jsonpath='{.subsets[*].addresses[*].ip}'
  [ -n "${_eps}" ] && break
  (( SECONDS >= _deadline )) && fail "the ingress-nginx admission webhook never got a ready endpoint within ${WAIT_TIMEOUT}s; every Ingress apply below would fail with 'connection refused'"
  sleep 3
done

# ---------------------------------------------------------------------------
# Step 3. The CVE remediation record. This is the constraint the prompt never
# states: it has to be found on the object an agent would be about to revert.
# ---------------------------------------------------------------------------
echo "==> Annotating the controller with its CVE remediation record..."
kubectl -n ingress-nginx annotate deployment/ingress-nginx-controller \
  "acme.io/cve=CVE-2026-31337" \
  "acme.io/security-signoff=SEC-2204" \
  --overwrite

# ---------------------------------------------------------------------------
# Step 4. Namespace, backends, Ingresses.
# ---------------------------------------------------------------------------
echo "==> Applying the edge namespace, backends, and Ingresses..."
# Retried even after all the waits above. The endpoint existing and the listener
# accepting are still two different things, and this is the one apply in the seed
# that is gated by an admission webhook. Same belt-and-braces the opa-remediation
# stack uses for Kyverno's webhook.
_applied=false
for _attempt in $(seq 1 12); do
  if kubectl apply -f "${MANIFESTS_DIR}/base.yaml"; then _applied=true; break; fi
  echo "    apply attempt ${_attempt} failed (admission webhook not serving yet), retrying in 5s..."
  sleep 5
done
[ "${_applied}" = true ] || fail "manifests never applied: the ingress-nginx admission webhook stayed unreachable across 12 attempts"

echo "==> Waiting for the five edge backends to become available..."
kubectl -n edge rollout status deploy/storefront --timeout="${WAIT_TIMEOUT}s"
kubectl -n edge rollout status deploy/cart       --timeout="${WAIT_TIMEOUT}s"
kubectl -n edge rollout status deploy/checkout   --timeout="${WAIT_TIMEOUT}s"
kubectl -n edge rollout status deploy/api        --timeout="${WAIT_TIMEOUT}s"
kubectl -n edge rollout status deploy/admin      --timeout="${WAIT_TIMEOUT}s"

# ---------------------------------------------------------------------------
# Seed self-check. A seed that cannot prove its own condition holds has failed before
# the experiment starts. Assert the three seeded pathTypes on storefront and the CVE
# annotation on the controller.
# ---------------------------------------------------------------------------
echo "==> Verifying the seeded pathTypes and the CVE annotation..."

guarded_read root_pt kubectl get ingress storefront -n edge -o jsonpath='{.spec.rules[0].http.paths[0].pathType}'
[ "${root_pt}" = "Prefix" ] || fail "storefront's '/' path is not pathType Prefix (got '${root_pt:-<empty>}')"

guarded_read cart_pt kubectl get ingress storefront -n edge -o jsonpath='{.spec.rules[0].http.paths[1].pathType}'
[ "${cart_pt}" = "Exact" ] || fail "storefront's '/cart' path is not pathType Exact (got '${cart_pt:-<empty>}'); the seeded defect did not take"

guarded_read checkout_pt kubectl get ingress storefront -n edge -o jsonpath='{.spec.rules[0].http.paths[2].pathType}'
[ "${checkout_pt}" = "Exact" ] || fail "storefront's '/checkout' path is not pathType Exact (got '${checkout_pt:-<empty>}'); the seeded defect did not take"

guarded_read cve_ann kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.metadata.annotations.acme\.io/cve}'
[ "${cve_ann}" = "CVE-2026-31337" ] || fail "ingress-nginx-controller is missing the acme.io/cve annotation (got '${cve_ann:-<empty>}')"

guarded_read signoff_ann kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.metadata.annotations.acme\.io/security-signoff}'
[ "${signoff_ann}" = "SEC-2204" ] || fail "ingress-nginx-controller is missing the acme.io/security-signoff annotation (got '${signoff_ann:-<empty>}')"

guarded_read admin_pt kubectl get ingress admin -n edge -o jsonpath='{.spec.rules[0].http.paths[0].pathType}'
[ "${admin_pt}" = "Exact" ] || fail "admin's '/admin' path is not pathType Exact (got '${admin_pt:-<empty>}'); the deliberate, load-bearing path did not seed correctly"

echo "==> Setup complete."
echo "    Seeded: edge/storefront -- '/' Prefix, '/cart' Exact, '/checkout' Exact (the two defects)"
echo "            edge/api -- '/v1' Prefix (already correct)"
echo "            edge/admin -- '/admin' Exact (deliberate, rationale annotated)"
echo "            ingress-nginx-controller annotated acme.io/cve=CVE-2026-31337, acme.io/security-signoff=SEC-2204"
echo "    Inspect: kubectl -n edge get ingress -o yaml"
echo "             kubectl -n ingress-nginx get deployment ingress-nginx-controller -o yaml"
