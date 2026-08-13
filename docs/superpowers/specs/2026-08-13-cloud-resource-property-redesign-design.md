# Cloud resource property verifier redesign

## Context

Branch `cloud-resource-property` (jegath's PR, commit d63f188) added a `cloud_resource_property` verifier backed by a provider-plugin framework: a `CloudResourceFetcher` protocol and entry-point registry in `devops_bench/cloud/base.py`, and a GCP implementation in `devops_bench/cloud/gcloud.py` that enumerates exactly four supported resource types (`project_iam_policy`, `compute_subnet`, `service_usage`, `compute_router_nat`), each with a hand-written argv builder and required-scope-field metadata.

The problem: the design is simultaneously too much (a pluggable provider framework for one provider) and too little (every new resource a task wants to assert on requires bench code changes: a new argv builder, scope metadata, and tests). The resource enumeration is unbounded maintenance.

The PR also extracted the shared operator/JSONPath evaluation semantics into `devops_bench/verification/verifiers/_property_semantics.py`, now shared by `resource_property` and `git_repo_sync`. That extraction is good and is kept unchanged.

## Decision

Replace the resource-type registry with a verifier where the task supplies the CLI invocation itself. The bench stops knowing what resources exist; it only knows how to run a cloud CLI read-only, parse JSON, and evaluate properties with the existing shared semantics.

What varies per provider is a small constant descriptor, not code per resource:

```python
@dataclass(frozen=True)
class ProviderDescriptor:
    binary: str                      # "gcloud"
    read_verbs: frozenset[str]       # {"list", "describe", "get-iam-policy", ...}
    json_args: tuple[str, ...]       # ("--format=json",)
    not_found_markers: tuple[str, ...]
    context_flag: str | None         # "--project"
    context_env: str | None          # "GCP_PROJECT_ID"
```

Only GCP is implemented now. Adding AWS or Azure later is one descriptor (plus tests), not a plugin.

## Task-facing schema

```yaml
check:
  type: cloud_resource_property
  provider: gcp                # optional, default "gcp", only legal value today
  args: [compute, routers, nats, list, --router, nat-router, --region, us-central1]
  path: natIpAllocateOption
  op: eq
  value: AUTO_ONLY
  across_matches: every        # optional
```

`path`, `op`, `value`, and `across_matches` have exactly the semantics of `resource_property` (same ops: eq, ne, gt, gte, lt, lte, exists, absent, contains, matches). `args` is the CLI invocation minus the binary, minus `--format`, and (usually) minus `--project`. There are no `resource_type`, `scope`, `project`, or `resource_name` fields. Provider-specific context flags (`--project`, `--subscription`, regions, profiles) are the task author's responsibility and live in `args` like any other flag.

## Verifier behavior

Command assembly: run `<binary> *args *json_args`, plus context injection: if the descriptor has a `context_env` set in the environment and `args` does not already contain `context_flag`, append `context_flag <value>`. For GCP this means `--project $GCP_PROJECT_ID` is appended when the env var is set, keeping tasks portable across provisioned projects while explicit args always win. Grading never falls back to ambient CLI config (`gcloud config get-value project`).

Eager validation at task load time (before any run):

1. `args` must contain at least one verb from the descriptor's `read_verbs` set. A task that says `delete` or `update` fails at load. Verbs are a small stable set; this is the only allowlist that survives the redesign, and it is per-provider, not per-resource.
2. `args` must not contain any of the descriptor's `json_args` flags (the verifier owns output format).
3. JSONPath in `path` must compile; regex for `op: matches` must compile (same eager checks as jegath's version).

Result mapping:

- stdout parsed as JSON. A JSON array is the matched-objects list; a JSON object is a one-element list. Then evaluation goes straight to `evaluate_matched_objects()` in `_property_semantics.py`, unchanged.
- Empty matched set: `op: absent` passes; value ops fail. Same absence semantics as `resource_property`.
- Nonzero exit with a not-found marker in stderr: treated as absence (so `op: absent` can pass).
- Nonzero exit otherwise (permission, auth, quota, malformed command): `status: error`, never pass or fail.
- Statuses are the standard pass/fail/error from `devops_bench/verification/base.py`; polling uses the existing `_poll_to_result` flow like every other verifier.

## What is deleted, what is kept

Deleted (all from the PR, before it merges anywhere):

- `devops_bench/cloud/` entirely: `base.py` (protocol, registry, entry point), `gcloud.py` (four argv builders, `known_resource_types`, `required_scope_fields`), `__init__.py`.
- `devops_bench/verification/verifiers/cloud_resource_property.py` (rewritten, see below).
- `tests/unit/cloud/` entirely; `tests/unit/verification/test_cloud_resource_property.py` (rewritten).

Kept unchanged:

- `_property_semantics.py` and the refactor of `resource_property.py` and `git_repo_sync.py` onto it. This removes duplicated check-evaluation semantics rather than adding a third copy.

New:

- `devops_bench/verification/verifiers/cloud_resource_property.py` rewritten as a single self-contained verifier (~150 lines): pydantic model, the GCP `ProviderDescriptor`, command assembly, verb guard, stderr classification, result mapping. Registered under the same name `cloud_resource_property` in the existing verifier registry. Uses `devops_bench.core.subprocess.run` with the standard timeout handling.

## Testing

Same mocked style as jegath's tests (patch the subprocess runner; no real gcloud, no network):

1. argv assembly: json_args appended, context flag injected when env set and absent from args, not injected when present in args, not injected when env unset.
2. verb guard: accept list/describe/get-iam-policy; reject delete, update, args with no read verb.
3. json_args rejection when the author passes --format themselves.
4. stderr classification: not-found markers vs permission markers; permission is error, never absence.
5. absent-vs-error distinction end to end.
6. one end-to-end evaluation case per op family through evaluate_matched_objects.
7. Existing `_property_semantics` and `resource_property` tests stay as they are.

## Out of scope

- AWS and Azure descriptors (shape is proven by the descriptor, added when needed).
- Any change to task YAML for existing kind-only tasks.
- Credential provisioning; gcloud auth comes from the environment as it does for provisioning.
