# Cloud Resource Property Verifier Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the resource-type-registry cloud verifier with one where the task spec supplies the read-only gcloud invocation itself, evaluated with the existing shared property semantics.

**Architecture:** One self-contained verifier module (`cloud_resource_property.py`) holding a frozen per-provider `ProviderDescriptor` (binary, read-only verb allowlist, JSON flags, not-found stderr markers, context injection) replaces the `devops_bench/cloud/` provider framework, which is deleted. Evaluation reuses `evaluate_matched_objects()` from `_property_semantics.py` unchanged.

**Tech Stack:** Python 3.14, pydantic v2, jsonpath-ng, pytest >= 8, ruff. Test style: mock `run()` at the verifier module, no real gcloud or network.

**Spec:** `docs/superpowers/specs/2026-08-13-cloud-resource-property-redesign-design.md` (committed on this branch). Read it before starting.

## Global Constraints

- Branch: `cloud-resource-property`. All work commits there.
- Commit messages: single line, conventional-commit style. Never add a `Co-Authored-By` trailer. Never add a `Claude-Session` trailer. No trailers at all.
- No em-dashes in code comments or docstrings.
- Every new Python file starts with the repo's standard Apache 2.0 license header: copy the first 13 comment lines verbatim from `devops_bench/verification/verifiers/resource_property.py`.
- Run tests with `.venv/bin/python -m pytest <paths> -v` from the repo root (a `uv`-managed venv exists at `.venv`). Lint with `.venv/bin/ruff check devops_bench tests` and `.venv/bin/ruff format --check devops_bench tests`; if `ruff` is not in the venv, use `uvx ruff@0.15.17`.
- The verifier registry key and exported class name stay `cloud_resource_property` / `CloudResourcePropertyVerifier`; `devops_bench/verification/verifiers/__init__.py` must not need edits.

---

### Task 1: Rewrite the verifier and its tests

**Files:**
- Rewrite (full replacement): `devops_bench/verification/verifiers/cloud_resource_property.py`
- Rewrite (full replacement): `tests/unit/verification/test_cloud_resource_property.py`

**Interfaces:**
- Consumes: `evaluate_matched_objects(op, expected, across_matches, path, objects, names, subject) -> tuple[Literal["pass","fail"], str, dict]`, `_compile(path)`, `_compile_regex(pattern)`, `_SET_OPS`, `_VALUE_OPS` from `devops_bench.verification.verifiers._property_semantics`; `VERIFIERS`, `BaseVerifier`, `VerificationResult`, `VerificationStatus`, `single_call_timeout` from `devops_bench.verification.base`; `run()` from `devops_bench.core.subprocess`; `get_env()` from `devops_bench.core`.
- Produces: `CloudResourcePropertyVerifier` (pydantic model registered as `"cloud_resource_property"`) with fields `type`, `provider` (Literal["gcp"], default "gcp"), `args: list[str]`, `path`, `op`, `value`, `across_matches`; and `ProviderDescriptor` (frozen dataclass). Task 2 relies on this module having NO import from `devops_bench.cloud`.

- [ ] **Step 1: Write the new test file**

Replace the entire contents of `tests/unit/verification/test_cloud_resource_property.py` with the following (prepend the standard Apache header copied from `tests/unit/verification/test_package_import.py` first):

```python
"""Tests for the task-supplied-argv cloud_resource_property verifier.

The subprocess runner is patched at the verifier module; no real gcloud,
no network. Argv assembly, the read-only verb guard, stderr
classification, and evaluation wiring are each covered separately.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.verification.verifiers.cloud_resource_property import (
    CloudResourcePropertyVerifier,
)

_MODULE = "devops_bench.verification.verifiers.cloud_resource_property"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gcloud"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patched_run(completed: subprocess.CompletedProcess):
    return patch(f"{_MODULE}.run", return_value=completed)


@pytest.fixture(autouse=True)
def _no_ambient_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)


# --- argv assembly -----------------------------------------------------------


def test_appends_binary_and_format_json() -> None:
    with _patched_run(_completed(stdout=json.dumps({"name": "s"}))) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s", "--region", "us-central1"],
            path="name",
            op="eq",
            value="s",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert argv[0] == "gcloud"
    assert argv[1:8] == ["compute", "networks", "subnets", "describe", "s", "--region", "us-central1"]
    assert argv[-1] == "--format=json"
    assert "--project" not in argv


def test_injects_project_from_env_when_not_in_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-123")
    with _patched_run(_completed(stdout="[]")) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r", "--region", "us-central1"],
            op="absent",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert argv[-3:] == ["--project", "proj-123", "--format=json"]


def test_explicit_project_in_args_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-123")
    with _patched_run(_completed(stdout="[]")) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r", "--project=other"],
            op="absent",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert "--project=other" in argv
    assert "proj-123" not in argv


# --- eager validation --------------------------------------------------------


def test_rejects_args_without_read_only_verb() -> None:
    with pytest.raises(ValidationError, match="read-only verb"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "delete", "my-vm", "--zone", "us-central1-a"],
            op="exists",
        )


def test_accepts_each_read_only_verb() -> None:
    for verb in ("list", "describe", "get-iam-policy"):
        CloudResourcePropertyVerifier(args=["something", verb], op="exists")


def test_rejects_author_supplied_format_flag() -> None:
    with pytest.raises(ValidationError, match="output format"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "list", "--format=yaml"],
            op="exists",
        )
    with pytest.raises(ValidationError, match="output format"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "list", "--format", "yaml"],
            op="exists",
        )


def test_rejects_invalid_jsonpath_at_load() -> None:
    with pytest.raises(ValidationError, match="not a valid JSONPath"):
        CloudResourcePropertyVerifier(args=["x", "list"], path="a[", op="eq", value=1)


def test_rejects_invalid_regex_at_load() -> None:
    with pytest.raises(ValidationError, match="invalid pattern"):
        CloudResourcePropertyVerifier(args=["x", "list"], path="a", op="matches", value="[")


def test_rejects_value_op_without_path() -> None:
    with pytest.raises(ValidationError, match="requires 'path'"):
        CloudResourcePropertyVerifier(args=["x", "list"], op="eq", value=1)


# --- result mapping ----------------------------------------------------------


def test_describe_object_payload_eq_pass() -> None:
    payload = {"name": "nat-1", "natIpAllocateOption": "AUTO_ONLY"}
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "describe", "nat-1", "--router", "r"],
            path="natIpAllocateOption",
            op="eq",
            value="AUTO_ONLY",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"


def test_list_payload_across_matches_every() -> None:
    payload = [
        {"name": "a", "privateIpGoogleAccess": True},
        {"name": "b", "privateIpGoogleAccess": True},
    ]
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "list"],
            path="privateIpGoogleAccess",
            op="eq",
            value=True,
            across_matches="every",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_matches_op_end_to_end() -> None:
    payload = {"name": "nat-router-prod", "natIpAllocateOption": "AUTO_ONLY"}
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "describe", "nat-router-prod"],
            path="name",
            op="matches",
            value=r"^nat-router-",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_empty_list_with_absent_passes() -> None:
    with _patched_run(_completed(stdout="[]")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_not_found_stderr_with_absent_passes() -> None:
    with _patched_run(_completed(returncode=1, stderr="ERROR: resource was not found")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "ghost"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_not_found_stderr_with_value_op_fails_not_errors() -> None:
    with _patched_run(_completed(returncode=1, stderr="ERROR: resource was not found")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "ghost"],
            path="name",
            op="eq",
            value="ghost",
        ).verify(timeout_sec=0)

    assert result.status == "fail"
    assert result.success is False


def test_permission_stderr_is_error_never_absence() -> None:
    with _patched_run(
        _completed(returncode=1, stderr="ERROR: PERMISSION_DENIED: caller forbidden")
    ):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "error"
    assert result.success is False


def test_non_json_stdout_is_error() -> None:
    with _patched_run(_completed(stdout="not json")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "instances", "list"],
            op="exists",
        ).verify(timeout_sec=0)

    assert result.status == "error"


def test_runner_exception_is_error() -> None:
    with patch(f"{_MODULE}.run", side_effect=OSError("gcloud not on PATH")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "instances", "list"],
            op="exists",
        ).verify(timeout_sec=0)

    assert result.status == "error"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/verification/test_cloud_resource_property.py -v`
Expected: collection error or widespread failures (the current module still requires `resource_type` and imports `devops_bench.cloud`). If instead they all pass, stop: something is wrong.

- [ ] **Step 3: Write the new verifier module**

Replace the entire contents of `devops_bench/verification/verifiers/cloud_resource_property.py` with the following (prepend the standard Apache header copied from `devops_bench/verification/verifiers/resource_property.py` first):

```python
"""Assert a JSONPath property of a cloud resource (GCP today).

The cloud-side sibling of ``resource_property``: same JSONPath dialect,
operator table, and quantifier semantics (imported from
``_property_semantics``, shared rather than reimplemented), applied to the
parsed JSON output of a read-only cloud CLI invocation that the task spec
itself supplies in ``args``.

The bench does not enumerate which cloud resources exist. ``args`` is the
CLI invocation minus the binary, minus output-format flags, and (usually)
minus the provider context flag. What the bench owns per provider is one
frozen :class:`ProviderDescriptor`: the binary, the read-only verb
allowlist, the JSON output flags, the not-found stderr markers, and how
ambient context is injected (``--project`` from ``GCP_PROJECT_ID``).
Grading never falls back to the CLI's own ambient config.

A cloud CLI's "resource absent" and "caller lacks permission to look" both
surface as a nonzero exit, and they are not the same claim: stderr matching
a not-found marker is treated as an empty fetched-object set (so ``op:
absent`` can pass on the right grounds); any other nonzero exit is
``status="error"``, never ``"fail"``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError as _JsonPathError
from pydantic import model_validator

from devops_bench.core import get_env
from devops_bench.core.subprocess import run
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)
from devops_bench.verification.verifiers._property_semantics import (
    _SET_OPS,
    _VALUE_OPS,
    _compile,
    _compile_regex,
    evaluate_matched_objects,
)

__all__ = ["CloudResourcePropertyVerifier", "ProviderDescriptor"]


@dataclass(frozen=True)
class ProviderDescriptor:
    """Everything the bench knows about one cloud CLI, as constants.

    Attributes:
        binary: The CLI executable.
        read_verbs: ``args`` must contain at least one of these tokens; the
            only allowlist in the design, per provider rather than per
            resource.
        json_args: Output-format flags the verifier appends; a spec passing
            its own is rejected at load time.
        not_found_markers: Case-insensitive stderr substrings meaning "the
            resource genuinely does not exist", checked on nonzero exit.
        context_flag: Flag injected from ``context_env`` when the spec did
            not pass it explicitly.
        context_env: Environment variable supplying ``context_flag``'s value.
    """

    binary: str
    read_verbs: frozenset[str]
    json_args: tuple[str, ...]
    not_found_markers: tuple[str, ...]
    context_flag: str | None = None
    context_env: str | None = None


_GCP = ProviderDescriptor(
    binary="gcloud",
    read_verbs=frozenset({"list", "describe", "get-iam-policy"}),
    json_args=("--format=json",),
    not_found_markers=("was not found", "not_found", "no such", "does not exist"),
    context_flag="--project",
    context_env="GCP_PROJECT_ID",
)

# Adding a provider is one descriptor here plus widening the `provider`
# Literal below; no protocol, no registry, no plugin machinery.
_DESCRIPTORS: dict[str, ProviderDescriptor] = {"gcp": _GCP}


def _object_label(obj: Any, index: int) -> str:
    """Best-effort display label for one fetched object.

    Cloud JSON shapes vary by resource type (there is no uniform
    ``metadata.name``); fall back to a positional label when a dict has no
    ``name`` field, or the object is not a dict at all.
    """
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str):
            return name
    return f"item[{index}]"


@VERIFIERS.register("cloud_resource_property")
class CloudResourcePropertyVerifier(BaseVerifier):
    """Compare a JSONPath property of objects fetched by a spec-supplied CLI call.

    See the module docstring for the absent-vs-permission-denied distinction
    and the shared JSONPath/operator semantics with ``resource_property``.
    """

    type: Literal["cloud_resource_property"] = "cloud_resource_property"
    provider: Literal["gcp"] = "gcp"
    args: list[str]
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    across_matches: Literal["every", "none"] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> CloudResourcePropertyVerifier:
        """Reject specs that could mutate, or cannot mean anything, at load time."""
        desc = _DESCRIPTORS[self.provider]
        if not set(self.args) & desc.read_verbs:
            msg = (
                f"args {self.args!r} contain no read-only verb for provider "
                f"{self.provider!r}; expected one of {sorted(desc.read_verbs)}"
            )
            raise ValueError(msg)
        owned = {flag.split("=", 1)[0] for flag in desc.json_args}
        clashing = [a for a in self.args if a.split("=", 1)[0] in owned]
        if clashing:
            msg = (
                f"args must not set the output format ({clashing!r}); the verifier "
                f"appends {list(desc.json_args)!r} itself"
            )
            raise ValueError(msg)
        if self.op not in _SET_OPS and not self.path:
            raise ValueError(f"op {self.op!r} requires 'path'")
        if self.path is not None:
            try:
                _compile(self.path)
            except _JsonPathError as exc:
                msg = f"path {self.path!r} is not a valid JSONPath: {exc}"
                raise ValueError(msg) from exc
        if self.op == "absent" and self.across_matches:
            msg = "op 'absent' already asserts emptiness and does not take 'across_matches'"
            raise ValueError(msg)
        if self.op == "exists" and not self.path and self.across_matches:
            msg = (
                "op 'exists' without 'path' applies to the fetched object set "
                "and does not take 'across_matches'"
            )
            raise ValueError(msg)
        if self.op in _VALUE_OPS and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        if self.op == "matches":
            try:
                _compile_regex(str(self.value))
            except re.error as exc:
                msg = f"op 'matches' has an invalid pattern {self.value!r}: {exc}"
                raise ValueError(msg) from exc
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the property until it holds or the budget runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _assemble_argv(self) -> list[str]:
        """The full command: binary, spec args, injected context, owned format flags."""
        desc = _DESCRIPTORS[self.provider]
        argv: list[str] = [desc.binary, *self.args]
        if desc.context_flag and desc.context_env:
            explicit = any(
                a == desc.context_flag or a.startswith(desc.context_flag + "=")
                for a in self.args
            )
            ambient = get_env(desc.context_env)
            if not explicit and ambient:
                argv += [desc.context_flag, ambient]
        argv += list(desc.json_args)
        return argv

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: run the CLI, parse JSON, apply the operator."""
        desc = _DESCRIPTORS[self.provider]
        try:
            completed = run(
                self._assemble_argv(), check=False, timeout=single_call_timeout(timeout_sec)
            )
        except Exception as exc:  # noqa: BLE001 - a CLI failure is a check error
            return "error", f"{desc.binary} invocation failed: {exc}", None

        subject = " ".join(self.args)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            lowered = stderr.lower()
            if not any(marker in lowered for marker in desc.not_found_markers):
                return (
                    "error",
                    f"{desc.binary} exited {completed.returncode}: {stderr or 'no stderr'}",
                    None,
                )
            objects: list[Any] = []
        else:
            try:
                payload = json.loads(completed.stdout or "null")
            except json.JSONDecodeError as exc:
                return "error", f"{desc.binary} produced non-JSON output: {exc}", None
            if payload is None:
                objects = []
            else:
                objects = payload if isinstance(payload, list) else [payload]

        if not objects:
            # A not-found exit and a legitimately empty listing both mean the
            # same observation: nothing matched. `absent` asserts exactly
            # that; a property op cannot hold on nothing, so it must FAIL,
            # not error, mirroring resource_property's name-mode semantics.
            raw = {"matched": 0, "names": []}
            if self.op == "absent":
                return "pass", f"{subject}: no objects, as required", raw
            if self.path is not None:
                return "fail", f"{subject}: no objects; property check cannot hold", raw
            return "fail", f"{subject}: no objects", raw

        names = [_object_label(obj, i) for i, obj in enumerate(objects)]
        return evaluate_matched_objects(
            self.op, self.value, self.across_matches, self.path, objects, names, subject
        )
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/verification/test_cloud_resource_property.py -v`
Expected: all tests PASS. If `test_accepts_each_read_only_verb` fails on pydantic defaults, check that `type` has a default value as shown above.

- [ ] **Step 5: Run the wider verification test package**

Run: `.venv/bin/python -m pytest tests/unit/verification -v`
Expected: all PASS, including `test_package_import.py::test_cloud_resource_property_importable_from_package_root` (the class name and export are unchanged).

- [ ] **Step 6: Commit**

```bash
git add devops_bench/verification/verifiers/cloud_resource_property.py tests/unit/verification/test_cloud_resource_property.py
git commit -m "refactor(verification): cloud_resource_property takes task-supplied read-only argv" -- devops_bench/verification/verifiers/cloud_resource_property.py tests/unit/verification/test_cloud_resource_property.py
```

Single-line message exactly as above. No trailers of any kind.

---

### Task 2: Delete the provider framework and verify the tree

**Files:**
- Delete: `devops_bench/cloud/` (entire directory: `__init__.py`, `base.py`, `gcloud.py`)
- Delete: `tests/unit/cloud/` (entire directory)

**Interfaces:**
- Consumes: Task 1's verifier module, which no longer imports `devops_bench.cloud`.
- Produces: a tree with no references to `devops_bench.cloud`; the full unit suite and ruff clean.

- [ ] **Step 1: Confirm nothing still imports the package**

Run: `grep -rn "devops_bench.cloud\|devops_bench\.cloud" devops_bench tests --include="*.py"`
Expected: no output. If anything matches, fix that import first (it should not happen if Task 1 was completed as written; report it rather than improvising).

Also check for the entry-point group: `grep -rn "devops_bench.cloud" pyproject.toml`
Expected: no output. If pyproject.toml declares an entry point into `devops_bench.cloud`, remove that line as part of this task.

- [ ] **Step 2: Delete the directories**

```bash
git rm -r devops_bench/cloud tests/unit/cloud
```

- [ ] **Step 3: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: all PASS, none collected from `tests/unit/cloud`.

- [ ] **Step 4: Lint and format check**

Run: `.venv/bin/ruff check devops_bench tests && .venv/bin/ruff format --check devops_bench tests`
(If ruff is not in the venv, use `uvx ruff@0.15.17` with the same arguments.)
Expected: clean. Fix any findings in the files this plan touched only.

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor(verification): drop devops_bench.cloud provider framework" -- devops_bench/cloud tests/unit/cloud
```

Single-line message exactly as above. No trailers of any kind. If Step 1 removed an entry-point line from pyproject.toml, include pyproject.toml in the same commit.

---

## Verification checklist (whole plan)

1. `pytest tests/unit` fully green.
2. `grep -rn "known_resource_types\|RESOURCE_FETCHERS\|compute_router_nat" devops_bench tests` returns nothing.
3. `git log --oneline` on the branch shows the spec commit plus exactly two implementation commits.
