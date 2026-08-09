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

"""Unit tests for ``CloudResourcePropertyVerifier``.

The GCP fetcher is faked by patching ``RESOURCE_FETCHERS`` at this module's
own import site; no real ``gcloud`` calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.cloud.base import CloudProviderError, ResourceAbsentError
from devops_bench.verification.verifiers.cloud_resource_property import (
    CloudResourcePropertyVerifier,
)

_MODULE = "devops_bench.verification.verifiers.cloud_resource_property"


class _FakeFetcherCls:
    """Registry entries are classes; the verifier does ``RESOURCE_FETCHERS.get(...)()``."""

    result: object = None
    exc: Exception | None = None

    def fetch(self, resource_type, *, project, scope, resource_name, timeout=None):
        if self.exc is not None:
            raise self.exc
        return self.result


def _fetcher_returning(result: object):
    cls = type("_Fetcher", (_FakeFetcherCls,), {"result": result, "exc": None})
    return cls


def _fetcher_raising(exc: Exception):
    cls = type("_Fetcher", (_FakeFetcherCls,), {"result": None, "exc": exc})
    return cls


def _patched_registry(fetcher_cls):
    return patch(f"{_MODULE}.RESOURCE_FETCHERS.get", return_value=fetcher_cls)


# --- happy path per seeded resource type -------------------------------------


def test_project_iam_policy_eq_pass() -> None:
    policy = {
        "bindings": [
            {"role": "roles/owner", "members": ["serviceAccount:sa@p.iam.gserviceaccount.com"]}
        ]
    }
    with _patched_registry(_fetcher_returning(policy)):
        result = CloudResourcePropertyVerifier(
            resource_type="project_iam_policy",
            project="my-proj",
            path='bindings[?(@.role="roles/owner")].role',
            op="eq",
            value="roles/owner",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"


def test_compute_subnet_property_check() -> None:
    subnet = {"name": "my-subnet", "privateIpGoogleAccess": False}
    with _patched_registry(_fetcher_returning(subnet)):
        result = CloudResourcePropertyVerifier(
            resource_type="compute_subnet",
            project="my-proj",
            scope={"region": "us-central1"},
            resource_name="my-subnet",
            path="privateIpGoogleAccess",
            op="eq",
            value=True,
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"


def test_service_usage_across_matches_none_fails_when_one_matches() -> None:
    # Each element of the fetched list is one top-level "object" in this
    # verifier's model (unlike resource_property's k8s object + wildcard
    # path, a cloud list response IS the object list) -- so the JSONPath is
    # relative to ONE service entry (`config.name`), not `[*].config.name`.
    # across_matches quantifies over the objects directly.
    enabled = [
        {"config": {"name": "compute.googleapis.com"}},
        {"config": {"name": "iam.googleapis.com"}},
    ]
    with _patched_registry(_fetcher_returning(enabled)):
        result = CloudResourcePropertyVerifier(
            resource_type="service_usage",
            project="my-proj",
            path="config.name",
            op="eq",
            value="iam.googleapis.com",
            across_matches="none",
        ).verify(timeout_sec=0)

    # "none" over all names equal to iam.googleapis.com: one element DOES
    # equal it, so across_matches="none" is violated.
    assert result.success is False
    assert result.status == "fail"


def test_compute_router_nat_absent_op() -> None:
    with _patched_registry(_fetcher_returning([])):
        result = CloudResourcePropertyVerifier(
            resource_type="compute_router_nat",
            project="my-proj",
            scope={"router": "r1", "region": "us-central1"},
            op="absent",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"


# --- error vs fail -------------------------------------------------------


def test_cloud_provider_error_is_status_error_not_fail() -> None:
    with _patched_registry(_fetcher_raising(CloudProviderError("PERMISSION_DENIED"))):
        result = CloudResourcePropertyVerifier(
            resource_type="project_iam_policy",
            project="my-proj",
            path="bindings",
            op="exists",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert result.raw is None


def test_malformed_json_is_status_error() -> None:
    import json

    with _patched_registry(_fetcher_raising(json.JSONDecodeError("bad", "doc", 0))):
        result = CloudResourcePropertyVerifier(
            resource_type="project_iam_policy",
            project="my-proj",
            path="bindings",
            op="exists",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert result.raw is None


# --- the absent-vs-permission-denied distinction (no analogue in resource_property) --


def test_resource_absent_error_is_treated_as_empty_match_set_not_error() -> None:
    with _patched_registry(_fetcher_raising(ResourceAbsentError("not found"))):
        result = CloudResourcePropertyVerifier(
            resource_type="compute_subnet",
            project="my-proj",
            scope={"region": "us-central1"},
            resource_name="ghost-subnet",
            op="absent",
        ).verify(timeout_sec=0)

    # The subnet genuinely does not exist, and op="absent" asserts exactly
    # that -- this must be status="pass", never "error".
    assert result.success is True
    assert result.status == "pass"


def test_permission_denied_never_reads_as_absent() -> None:
    # A 403 must not let an op="absent" check pass for the wrong reason.
    with _patched_registry(_fetcher_raising(CloudProviderError("PERMISSION_DENIED"))):
        result = CloudResourcePropertyVerifier(
            resource_type="compute_subnet",
            project="my-proj",
            scope={"region": "us-central1"},
            resource_name="secret-subnet",
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "error"
    assert result.success is False


# --- eager validation at spec-load time --------------------------------------


def test_unknown_resource_type_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError, match="not known"):
        CloudResourcePropertyVerifier(resource_type="not_a_real_type", op="exists")


def test_missing_required_scope_field_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError, match="region"):
        CloudResourcePropertyVerifier(
            resource_type="compute_subnet", resource_name="x", op="exists"
        )


def test_bad_jsonpath_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError, match="not a valid JSONPath"):
        CloudResourcePropertyVerifier(
            resource_type="project_iam_policy", path="[[[", op="eq", value="x"
        )


def test_provider_defaults_to_gcp() -> None:
    verifier = CloudResourcePropertyVerifier(
        resource_type="project_iam_policy", op="exists", path="bindings"
    )
    assert verifier.provider == "gcp"
