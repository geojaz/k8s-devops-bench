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

"""The provider-dispatch contract every cloud resource fetcher implements.

A verifier looks up a :class:`CloudResourceFetcher` by provider name in
:data:`RESOURCE_FETCHERS` and calls its one method; it never imports a
provider module (e.g. ``devops_bench.cloud.gcloud``) directly. Adding a new
provider means writing a module that implements this protocol and registers
itself — no change to any verifier.
"""

from __future__ import annotations

from typing import Any, Protocol

from devops_bench.core import DevOpsBenchError, Registry

__all__ = [
    "RESOURCE_FETCHERS",
    "CloudProviderError",
    "CloudResourceFetcher",
    "ResourceAbsentError",
]


class CloudProviderError(DevOpsBenchError):
    """A cloud CLI call failed for a reason other than the resource being absent.

    Covers permission/auth failures (e.g. a 403), bad flags, and anything
    else that is not a genuine not-found. A verifier maps this to
    ``status="error"``, never ``"fail"``: an environmental failure and an
    observed-false condition are different claims.
    """


class ResourceAbsentError(DevOpsBenchError):
    """A cloud resource genuinely does not exist (a classified not-found response).

    Distinct from :class:`CloudProviderError`: on GCP in particular, a 403
    (caller lacks permission to look) and a genuine not-found both surface as
    a nonzero ``gcloud`` exit, and they are not the same claim. A fetcher
    raises this only for a classified not-found; anything else raises
    :class:`CloudProviderError` (or lets a lower-level error, e.g.
    ``json.JSONDecodeError``, propagate).
    """


class CloudResourceFetcher(Protocol):
    """A provider's ability to fetch one cloud resource type as parsed JSON."""

    def fetch(
        self,
        resource_type: str,
        *,
        project: str | None,
        scope: dict[str, str],
        resource_name: str | None,
        timeout: float | None = None,
    ) -> Any:
        """Fetch ``resource_type`` and return its parsed JSON representation.

        Args:
            resource_type: A key in this provider's own resource-type
                registry, e.g. ``"project_iam_policy"``.
            project: Explicit project/account identifier, or ``None`` to fall
                back to this provider's own default resolution (e.g. an
                environment variable). Resolution — and the error raised when
                nothing resolves — is this fetcher's own responsibility, not
                the caller's.
            scope: Additional scoping fields the resource type requires (e.g.
                ``region``, ``router``). Never contains ``project``.
            resource_name: Optional specific resource name.
            timeout: Optional seconds before the underlying call is killed.

        Returns:
            The parsed JSON payload (a ``dict`` or ``list``, depending on the
            resource type).

        Raises:
            ResourceAbsentError: If the resource genuinely does not exist.
            CloudProviderError: If the call fails for any other reason
                (permission, auth, a bad flag).
        """
        ...


# Keyed by provider name (``"gcp"`` today). Entry-point discovery lets an
# out-of-tree provider register without touching this tree, same as
# VERIFIERS and PROVIDERS.
RESOURCE_FETCHERS: Registry[type[CloudResourceFetcher]] = Registry(
    "cloud_resource_fetchers", entry_point_group="devops_bench.cloud_resource_fetchers"
)
