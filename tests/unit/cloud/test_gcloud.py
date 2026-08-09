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

"""Unit tests for the GCP resource fetcher: argv shape and error classification.

``gcloud`` is faked via a stubbed ``run``; no real CLI or network calls.
"""

from __future__ import annotations

import subprocess

import pytest

from devops_bench.cloud.base import CloudProviderError, ResourceAbsentError
from devops_bench.cloud.gcloud import (
    GcloudResourceFetcher,
    known_resource_types,
    required_scope_fields,
)
from devops_bench.core import ConfigError

_MODULE = "devops_bench.cloud.gcloud"


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- registry shape ----------------------------------------------------------


def test_known_resource_types_has_the_four_seeded_types() -> None:
    assert known_resource_types() == {
        "project_iam_policy",
        "compute_subnet",
        "service_usage",
        "compute_router_nat",
    }


def test_required_scope_fields_for_project_scoped_type_is_empty() -> None:
    assert required_scope_fields("project_iam_policy") == ()


def test_required_scope_fields_for_regional_type() -> None:
    assert required_scope_fields("compute_subnet") == ("region",)


def test_required_scope_fields_for_router_scoped_type() -> None:
    assert required_scope_fields("compute_router_nat") == ("router", "region")


def test_required_scope_fields_unknown_type_raises_key_error() -> None:
    with pytest.raises(KeyError):
        required_scope_fields("not_a_real_type")


# --- argv shape per resource type --------------------------------------------


def test_project_iam_policy_argv(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed('{"bindings": []}'))
    GcloudResourceFetcher().fetch(
        "project_iam_policy", project="my-proj", scope={}, resource_name=None
    )
    argv = mock_run.call_args.args[0]
    assert argv == ["gcloud", "projects", "get-iam-policy", "my-proj", "--format=json"]


def test_compute_subnet_argv(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed("{}"))
    GcloudResourceFetcher().fetch(
        "compute_subnet",
        project="my-proj",
        scope={"region": "us-central1"},
        resource_name="my-subnet",
    )
    argv = mock_run.call_args.args[0]
    assert argv == [
        "gcloud",
        "compute",
        "networks",
        "subnets",
        "describe",
        "my-subnet",
        "--project",
        "my-proj",
        "--region",
        "us-central1",
        "--format=json",
    ]


def test_service_usage_argv(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed("[]"))
    GcloudResourceFetcher().fetch("service_usage", project="my-proj", scope={}, resource_name=None)
    argv = mock_run.call_args.args[0]
    assert argv == [
        "gcloud",
        "services",
        "list",
        "--enabled",
        "--project",
        "my-proj",
        "--format=json",
    ]


def test_compute_router_nat_argv(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed("[]"))
    GcloudResourceFetcher().fetch(
        "compute_router_nat",
        project="my-proj",
        scope={"router": "my-router", "region": "us-central1"},
        resource_name=None,
    )
    argv = mock_run.call_args.args[0]
    assert argv == [
        "gcloud",
        "compute",
        "routers",
        "nats",
        "list",
        "--router",
        "my-router",
        "--region",
        "us-central1",
        "--project",
        "my-proj",
        "--format=json",
    ]


def test_compute_subnet_without_resource_name_raises_config_error(mocker) -> None:
    mocker.patch(f"{_MODULE}.run", return_value=_completed("{}"))
    with pytest.raises(ConfigError, match="resource_name"):
        GcloudResourceFetcher().fetch(
            "compute_subnet", project="my-proj", scope={"region": "us-central1"}, resource_name=None
        )


# --- project resolution -------------------------------------------------------


def test_explicit_project_is_used(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed('{"bindings": []}'))
    GcloudResourceFetcher().fetch(
        "project_iam_policy", project="explicit-proj", scope={}, resource_name=None
    )
    assert "explicit-proj" in mock_run.call_args.args[0]


def test_project_falls_back_to_env_var(mocker, monkeypatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "env-proj")
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed('{"bindings": []}'))
    GcloudResourceFetcher().fetch("project_iam_policy", project=None, scope={}, resource_name=None)
    assert "env-proj" in mock_run.call_args.args[0]


def test_project_unresolved_raises_config_error(mocker, monkeypatch) -> None:
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    mocker.patch(f"{_MODULE}.run", return_value=_completed('{"bindings": []}'))
    with pytest.raises(ConfigError, match="GCP_PROJECT_ID"):
        GcloudResourceFetcher().fetch(
            "project_iam_policy", project=None, scope={}, resource_name=None
        )


# --- the absent-vs-permission-denied distinction ------------------------------
# This is the case with no analogue in resource_property: a 403 must never
# read as absent, or a task could pass for the wrong reason.


def test_not_found_stderr_raises_resource_absent_error(mocker) -> None:
    mocker.patch(
        f"{_MODULE}.run",
        return_value=_completed(
            "", returncode=1, stderr="ERROR: (gcloud) NOT_FOUND: was not found"
        ),
    )
    with pytest.raises(ResourceAbsentError):
        GcloudResourceFetcher().fetch(
            "compute_subnet", project="p", scope={"region": "us-central1"}, resource_name="ghost"
        )


def test_permission_denied_stderr_raises_cloud_provider_error_not_absent(mocker) -> None:
    mocker.patch(
        f"{_MODULE}.run",
        return_value=_completed(
            "",
            returncode=1,
            stderr="ERROR: (gcloud) PERMISSION_DENIED: caller does not have permission",
        ),
    )
    with pytest.raises(CloudProviderError):
        GcloudResourceFetcher().fetch(
            "project_iam_policy", project="p", scope={}, resource_name=None
        )


def test_other_failure_raises_cloud_provider_error(mocker) -> None:
    mocker.patch(
        f"{_MODULE}.run",
        return_value=_completed("", returncode=1, stderr="ERROR: (gcloud) invalid flag"),
    )
    with pytest.raises(CloudProviderError):
        GcloudResourceFetcher().fetch(
            "project_iam_policy", project="p", scope={}, resource_name=None
        )


def test_malformed_json_propagates_json_decode_error(mocker) -> None:
    import json

    mocker.patch(f"{_MODULE}.run", return_value=_completed("not json"))
    with pytest.raises(json.JSONDecodeError):
        GcloudResourceFetcher().fetch(
            "project_iam_policy", project="p", scope={}, resource_name=None
        )


def test_timeout_is_forwarded_to_run(mocker) -> None:
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed('{"bindings": []}'))
    GcloudResourceFetcher().fetch(
        "project_iam_policy", project="p", scope={}, resource_name=None, timeout=42.0
    )
    assert mock_run.call_args.kwargs["timeout"] == 42.0


def test_check_is_false_so_run_never_raises_on_nonzero_exit(mocker) -> None:
    # fetch() classifies the failure itself; run() must not raise first.
    mock_run = mocker.patch(f"{_MODULE}.run", return_value=_completed("", returncode=1, stderr="x"))
    with pytest.raises(CloudProviderError):
        GcloudResourceFetcher().fetch(
            "project_iam_policy", project="p", scope={}, resource_name=None
        )
    assert mock_run.call_args.kwargs["check"] is False
