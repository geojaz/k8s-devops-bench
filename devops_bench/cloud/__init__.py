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

"""Provider-agnostic cloud resource-fetch abstraction, for verification checks.

A peer of ``devops_bench.k8s``, not a submodule of ``devops_bench.providers``:
that package's ``Provider``/``PROVIDERS`` is a different abstraction (stack
provisioning — credentials, cluster access, OpenTofu variables). This package
answers a narrower question a verifier asks at check time: "fetch this cloud
resource's JSON representation."
"""

from __future__ import annotations

# Import for its registration side effect so RESOURCE_FETCHERS is populated.
from devops_bench.cloud import gcloud as _gcloud  # noqa: F401
from devops_bench.cloud.base import (
    RESOURCE_FETCHERS,
    CloudProviderError,
    CloudResourceFetcher,
    ResourceAbsentError,
)

__all__ = [
    "RESOURCE_FETCHERS",
    "CloudProviderError",
    "CloudResourceFetcher",
    "ResourceAbsentError",
]
