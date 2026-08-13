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

"""GCP resource fetcher: a resource-type registry over ``gcloud``, plus error classification.

Implements :class:`~devops_bench.cloud.base.CloudResourceFetcher` for GCP.
Mirrors ``devops_bench.k8s.kubectl``'s wrapper style (argv as a list, never
``shell=True``, ``json.loads`` on stdout) with two things layered on top: a
small in-code map from ``resource_type`` to its ``gcloud`` argv shape and
required scope fields, and stderr classification so a permission/auth
failure (:class:`~devops_bench.cloud.base.CloudProviderError`) is never
mistaken for a genuine not-found
(:class:`~devops_bench.cloud.base.ResourceAbsentError`) — the two surface
identically as a nonzero ``gcloud`` exit, but they are not the same claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from devops_bench.cloud.base import RESOURCE_FETCHERS, CloudProviderError, ResourceAbsentError
from devops_bench.core import ConfigError, get_env, get_logger
from devops_bench.core.subprocess import run

__all__ = ["GcloudResourceFetcher", "known_resource_types", "required_scope_fields"]

_log = get_logger("cloud.gcloud")

# Stderr substrings (case-insensitive) that mean "the resource genuinely does
# not exist," as opposed to any other failure. Checked before the permission
# markers below.
_NOT_FOUND_MARKERS = ("was not found", "not_found", "no such", "does not exist")

# Stderr substrings (case-insensitive) that mean the caller could not look,
# not that the resource is absent. A 403 read as absent would let a task
# pass for the wrong reason.
_PERMISSION_MARKERS = (
    "permission_denied",
    "does not have permission",
    "insufficient authentication scopes",
    "forbidden",
)


@dataclass(frozen=True)
class _ResourceTypeSpec:
    """One GCP resource type's ``gcloud`` argv shape."""

    argv: Callable[[str, dict[str, str], str | None], list[str]]
    required_scope: tuple[str, ...] = field(default_factory=tuple)


def _require_resource_name(resource_name: str | None, resource_type: str) -> str:
    if not resource_name:
        raise ConfigError(f"resource_type {resource_type!r} requires 'resource_name'")
    return resource_name


def _project_iam_policy_argv(
    project: str, scope: dict[str, str], resource_name: str | None
) -> list[str]:
    return ["projects", "get-iam-policy", project, "--format=json"]


def _compute_subnet_argv(
    project: str, scope: dict[str, str], resource_name: str | None
) -> list[str]:
    return [
        "compute",
        "networks",
        "subnets",
        "describe",
        _require_resource_name(resource_name, "compute_subnet"),
        "--project",
        project,
        "--region",
        scope["region"],
        "--format=json",
    ]


def _service_usage_argv(
    project: str, scope: dict[str, str], resource_name: str | None
) -> list[str]:
    return ["services", "list", "--enabled", "--project", project, "--format=json"]


def _compute_router_nat_argv(
    project: str, scope: dict[str, str], resource_name: str | None
) -> list[str]:
    return [
        "compute",
        "routers",
        "nats",
        "list",
        "--router",
        scope["router"],
        "--region",
        scope["region"],
        "--project",
        project,
        "--format=json",
    ]


_GCP_RESOURCE_TYPES: dict[str, _ResourceTypeSpec] = {
    "project_iam_policy": _ResourceTypeSpec(argv=_project_iam_policy_argv),
    "compute_subnet": _ResourceTypeSpec(argv=_compute_subnet_argv, required_scope=("region",)),
    "service_usage": _ResourceTypeSpec(argv=_service_usage_argv),
    "compute_router_nat": _ResourceTypeSpec(
        argv=_compute_router_nat_argv, required_scope=("router", "region")
    ),
}


def known_resource_types() -> frozenset[str]:
    """GCP resource-type keys this fetcher knows how to build ``gcloud`` argv for."""
    return frozenset(_GCP_RESOURCE_TYPES)


def required_scope_fields(resource_type: str) -> tuple[str, ...]:
    """Scope fields ``resource_type`` requires, for eager spec validation.

    Raises:
        KeyError: If ``resource_type`` is not in :func:`known_resource_types`.
    """
    return _GCP_RESOURCE_TYPES[resource_type].required_scope


def _resolve_project(project: str | None) -> str:
    """Resolve the GCP project: the explicit field, else ``GCP_PROJECT_ID``.

    Never shells out to ``gcloud config get-value project``: grading must not
    depend on ambient developer state.

    Raises:
        ConfigError: If neither is set.
    """
    resolved = project or get_env("GCP_PROJECT_ID")
    if not resolved:
        raise ConfigError(
            "GCP project not found: set 'project' on the spec or the GCP_PROJECT_ID "
            "environment variable."
        )
    return resolved


def _classify_failure(stderr: str) -> None:
    """Raise the right exception for a nonzero ``gcloud`` exit. Never returns normally."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        raise ResourceAbsentError(stderr.strip() or "resource not found")
    raise CloudProviderError(stderr.strip() or "gcloud command failed")


@RESOURCE_FETCHERS.register("gcp")
class GcloudResourceFetcher:
    """Fetches a GCP resource's JSON representation via ``gcloud``."""

    def fetch(
        self,
        resource_type: str,
        *,
        project: str | None,
        scope: dict[str, str],
        resource_name: str | None,
        timeout: float | None = None,
    ) -> Any:
        """Run the ``gcloud`` command for ``resource_type`` and parse its JSON output.

        Raises:
            ResourceAbsentError: If the resource genuinely does not exist.
            CloudProviderError: If the call fails for any other reason.
            ConfigError: If the project cannot be resolved, or a required
                ``resource_name`` is missing.
            json.JSONDecodeError: If stdout is not valid JSON.
        """
        resolved_project = _resolve_project(project)
        spec = _GCP_RESOURCE_TYPES[resource_type]
        argv = ["gcloud", *spec.argv(resolved_project, scope, resource_name)]
        completed = run(argv, check=False, timeout=timeout)
        if completed.returncode != 0:
            _classify_failure(completed.stderr or "")
        return json.loads(completed.stdout)
