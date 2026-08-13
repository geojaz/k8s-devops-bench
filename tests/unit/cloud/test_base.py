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

"""Unit tests for the cloud resource-fetcher registry and error types."""

from __future__ import annotations

import pytest

from devops_bench.cloud.base import RESOURCE_FETCHERS, CloudProviderError, ResourceAbsentError
from devops_bench.core import NotRegisteredError


def test_resource_absent_and_provider_error_are_distinct_exception_types() -> None:
    # The whole point of the split: a caller must be able to catch one
    # without catching the other.
    assert not issubclass(ResourceAbsentError, CloudProviderError)
    assert not issubclass(CloudProviderError, ResourceAbsentError)


def test_unregistered_provider_raises_not_registered_error() -> None:
    with pytest.raises(NotRegisteredError, match="definitely_not_a_provider"):
        RESOURCE_FETCHERS.get("definitely_not_a_provider")


def test_register_and_get_round_trip() -> None:
    @RESOURCE_FETCHERS.register("_test_provider_base")
    class _FakeFetcher:
        def fetch(self, resource_type, *, project, scope, resource_name, timeout=None):
            return {"resource_type": resource_type}

    try:
        fetcher_cls = RESOURCE_FETCHERS.get("_test_provider_base")
        result = fetcher_cls().fetch("anything", project="p", scope={}, resource_name=None)
        assert result == {"resource_type": "anything"}
    finally:
        # Keep the global registry clean for sibling tests, same convention
        # test_verifier_registry.py uses for VERIFIERS.
        RESOURCE_FETCHERS._items.pop("_test_provider_base", None)
