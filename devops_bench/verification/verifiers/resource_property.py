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

"""Assert a JSONPath property of one or more Kubernetes objects.

This is the general-purpose leaf verifier. Where ``pod_healthy`` and
``scaling_complete`` each answer one fixed question, this one reads any field of
any resource and compares it, which is what most task objectives actually need.

JSONPath rather than a dotted path because filter predicates make checks
order-independent. ``containers[?(@.name="web")].image`` keeps meaning the same
thing when someone injects a sidecar ahead of the app container, where
``containers[0].image`` would silently start checking the wrong one.

``eq``/``ne`` only coerce both sides to numbers when there is a concrete
numeric signal: a non-bool int/float on either side, or a Kubernetes quantity
string (a numeric stem plus a known suffix, e.g. ``"100m"`` or ``"1Gi"``) on
either side. Two plain strings compare with raw ``==`` instead, because a
version string like an image tag is not a quantity: ``"1.20" == "1.2"`` must
stay false even though ``1.20 == 1.2`` as floats. Ordering ops (``gt`` /
``gte`` / ``lt`` / ``lte``) always coerce, since they are meaningless for
anything but numbers.

An unresolved ``path`` fails closed for every op except ``ne``: absence is
not equal to anything, so ``ne`` alone is satisfied by a field that is not
there at all (e.g. a ConfigMap without an ``immutable`` field is mutable).
Every positive op (``eq``, ``gte``, ``exists``, ``contains``, ...) still
treats an unresolved path as an unobservable predicate, not a satisfied one.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError as _JsonPathError
from pydantic import model_validator

from devops_bench.k8s import get_resource
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
    _apply_op,
    _compile,
    _compile_regex,
    evaluate_matched_objects,
    to_number,
)

# `_apply_op` and `to_number` are re-exported rather than merely imported:
# they were part of this module's public surface before the semantics moved
# to `_property_semantics`, and existing importers still reach for them here.
__all__ = ["ResourcePropertyVerifier", "_apply_op", "to_number"]


def _object_name(obj: Any) -> str:
    """Best-effort display name for a matched object."""
    if isinstance(obj, dict):
        metadata = obj.get("metadata")
        if isinstance(metadata, dict):
            name = metadata.get("name")
            if isinstance(name, str):
                return name
    return "<unnamed>"


@VERIFIERS.register("resource_property")
class ResourcePropertyVerifier(BaseVerifier):
    """Compare a JSONPath property of matched Kubernetes objects.

    The field is ``resource_name`` rather than ``name`` because ``BaseVerifier``
    already uses ``name`` for the check's own label, which is what lands on
    :attr:`VerificationResult.name`. In practice, most task specs write the
    object's own name as ``name`` (it reads the same way as ``kind`` and
    ``namespace``) rather than ``resource_name``, which used to be silently
    absorbed as a result label and left the fetch unscoped: see
    :meth:`_target_name`.

    When ``path`` ends in a wildcard-like segment (``[*]``, a slice, or a
    filter ``[?(...)]``) and ``across_matches`` is set, quantification is over
    the ELEMENTS that segment selects, not over the flattened values the path
    happens to resolve to: a container missing the target field is a failing
    observation under ``every``, not an invisible one that silently drops out
    of the match set.
    """

    type: Literal["resource_property"]
    kind: str
    resource_name: str | None = None
    selector: str | None = None
    namespace: str | None = None
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    # Quantifies over the elements of path's LAST wildcard-like segment
    # (`[*]`, a slice, or a filter), not over the flattened values the path
    # resolves to. `every`: every element must resolve the suffix and every
    # resolved value must satisfy `op`; an element that does not resolve the
    # suffix FAILS. `none`: no element may resolve a value satisfying `op`;
    # an element missing the field trivially conforms. When path has no such
    # segment, or across_matches is unset, this reduces to the plain
    # exactly-one-match behaviour below.
    across_matches: Literal["every", "none"] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> ResourcePropertyVerifier:
        """Reject combinations that cannot mean anything at evaluation time."""
        if self.resource_name and self.selector:
            msg = "resource_property takes 'resource_name' or 'selector', not both"
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
        # Scoped deliberately: `exists` WITH a path is an ordinary per-object
        # predicate and a reduction over it is meaningful. Only pathless
        # `exists` returns before any reduction runs (step 3 of _check), so
        # accepting `across_matches` there would silently discard it. Do not
        # widen this to cover `exists` WITH a path.
        if self.op == "exists" and not self.path and self.across_matches:
            msg = (
                "op 'exists' without 'path' applies to the matched object set "
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

    @property
    def _target_name(self) -> str | None:
        """The object name to fetch by: ``resource_name``, else ``name``.

        ``resource_name`` always wins when set explicitly. Otherwise, ``name``
        is used as the object name, unless ``selector`` is set, in which case
        ``name`` stays a pure result label and the selector alone scopes the
        fetch (unchanged from before). This is what makes a check written as
        ``{kind: deployment, name: ingest}`` fetch just ``ingest`` instead of
        listing every deployment in the namespace.
        """
        if self.resource_name is not None:
            return self.resource_name
        if self.selector is not None:
            return None
        return self.name

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the property until it holds or the budget runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: fetch, resolve matches, apply the operator."""
        target_name = self._target_name
        # A single named object's absence is always a well-defined
        # observation, not a check error: "does this object exist" for
        # `absent`/`exists`, and "the object it would read from is gone" for
        # every property-value op too (a deleted ResourceQuota cannot hold an
        # `eq` on its spec any more than a present-but-wrong one can). Scoped
        # to name-mode only; selector mode already reports absence as an
        # empty list rather than a kubectl error, so it is left unchanged.
        not_found_aware = self.selector is None and target_name is not None
        try:
            payload = get_resource(
                self.kind,
                target_name,
                selector=self.selector,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                context=self.context,
                timeout=single_call_timeout(timeout_sec),
                ignore_not_found=not_found_aware,
            )
        except Exception as exc:  # noqa: BLE001 - a kubectl failure is a check error
            return "error", f"kubectl get {self.kind} failed: {exc}", None

        if not_found_aware and not payload:
            raw = {"matched": 0, "names": []}
            ns_desc = f" in {self.namespace}" if self.namespace else ""
            if self.op == "absent":
                return (
                    "pass",
                    f"{self.kind} {target_name} not found{ns_desc}, as required",
                    raw,
                )
            if self.path is not None:
                # A property-value op (eq/ne/gt/gte/lt/lte/contains/matches):
                # the object it would read `path` from does not exist, so the
                # property cannot hold. This must FAIL, not error, so that
                # deleting the guarded object is caught at least as reliably
                # as patching it.
                return (
                    "fail",
                    f"{self.kind}/{target_name} not found{ns_desc}; property check cannot hold",
                    raw,
                )
            return "fail", f"{self.kind} {target_name} not found{ns_desc}", raw

        items = payload.get("items")
        objects = items if isinstance(items, list) else [payload]
        names = [_object_name(obj) for obj in objects]

        return evaluate_matched_objects(
            self.op, self.value, self.across_matches, self.path, objects, names, self.kind
        )
