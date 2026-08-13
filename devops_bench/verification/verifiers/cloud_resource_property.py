# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Assert a JSONPath property of a cloud resource (GCP today).

The cloud-side sibling of ``resource_property``: same JSONPath dialect,
operator table, and quantifier semantics (imported from
``_property_semantics``, shared rather than reimplemented), applied to a
resource fetched via a provider-dispatched
:class:`~devops_bench.cloud.base.CloudResourceFetcher` instead of ``kubectl``.

``provider`` names a key in
:data:`~devops_bench.cloud.base.RESOURCE_FETCHERS`; ``resource_type`` is a
key into that provider's own resource-type registry (e.g.
``"project_iam_policy"`` for GCP). Both are validated eagerly at
spec-load time, along with ``path`` and any regex ``value``, so a bad spec
fails before any cloud CLI call runs.

A cloud CLI's "resource absent" and "caller lacks permission to look" both
surface as a nonzero exit, and they are not the same claim: a fetcher raises
``ResourceAbsentError`` only for a genuine not-found, which this verifier
treats as an empty fetched-object set (never a `pass` by accident on the
wrong grounds); anything else -- ``CloudProviderError``, malformed JSON, an
unresolved project -- is ``status="error"``, never ``"fail"``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError as _JsonPathError
from pydantic import model_validator

from devops_bench.cloud import gcloud as _gcp
from devops_bench.cloud.base import RESOURCE_FETCHERS, ResourceAbsentError
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

__all__ = ["CloudResourcePropertyVerifier"]

# Provider name -> the module owning that provider's resource-type registry
# (`known_resource_types`/`required_scope_fields`), used only for eager
# spec-load validation. Widen alongside the `provider` Literal below when a
# new provider lands.
_RESOURCE_TYPE_REGISTRIES = {"gcp": _gcp}


def _object_label(obj: Any, index: int) -> str:
    """Best-effort display label for one fetched object.

    Cloud JSON shapes vary by provider and resource type (unlike Kubernetes,
    there is no uniform ``metadata.name``); fall back to a positional label
    when a dict has no ``name`` field, or the object is not a dict at all.
    """
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str):
            return name
    return f"item[{index}]"


@VERIFIERS.register("cloud_resource_property")
class CloudResourcePropertyVerifier(BaseVerifier):
    """Compare a JSONPath property of a fetched cloud resource.

    See the module docstring for the absent-vs-permission-denied distinction
    and the shared JSONPath/operator semantics with ``resource_property``.
    """

    type: Literal["cloud_resource_property"] = "cloud_resource_property"
    provider: Literal["gcp"] = "gcp"
    resource_type: str
    project: str | None = None
    scope: dict[str, str] | None = None
    resource_name: str | None = None
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    across_matches: Literal["every", "none"] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> CloudResourcePropertyVerifier:
        """Reject combinations that cannot mean anything at evaluation time."""
        registry_module = _RESOURCE_TYPE_REGISTRIES[self.provider]
        known = registry_module.known_resource_types()
        if self.resource_type not in known:
            msg = (
                f"resource_type {self.resource_type!r} is not known for provider "
                f"{self.provider!r}; available: {sorted(known)}"
            )
            raise ValueError(msg)
        required = registry_module.required_scope_fields(self.resource_type)
        have = set((self.scope or {}).keys())
        missing = [name for name in required if name not in have]
        if missing:
            msg = (
                f"resource_type {self.resource_type!r} requires scope field(s) "
                f"{missing}; got scope={self.scope!r}"
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
                "op 'exists' without 'path' applies to the fetched resource set "
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

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: fetch, resolve matches, apply the operator."""
        fetcher_cls = RESOURCE_FETCHERS.get(self.provider)
        try:
            payload = fetcher_cls().fetch(
                self.resource_type,
                project=self.project,
                scope=self.scope or {},
                resource_name=self.resource_name,
                timeout=single_call_timeout(timeout_sec),
            )
        except ResourceAbsentError:
            objects: list[Any] = []
        except Exception as exc:  # noqa: BLE001 - a fetch failure is a check error
            return (
                "error",
                f"{self.provider} fetch for {self.resource_type} failed: {exc}",
                None,
            )
        else:
            objects = payload if isinstance(payload, list) else [payload]

        names = [_object_label(obj, i) for i, obj in enumerate(objects)]

        return evaluate_matched_objects(
            self.op, self.value, self.across_matches, self.path, objects, names, self.resource_type
        )
