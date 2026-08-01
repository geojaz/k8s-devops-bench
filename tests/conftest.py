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
"""Suite-wide fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from devops_bench.core.logging import ROOT_LOGGER_NAME


@pytest.fixture(autouse=True)
def _restore_package_logger() -> Iterator[None]:
    """Undo any global logging configuration a test performs.

    ``configure_logging`` sets ``propagate = False`` on the ``devops_bench``
    logger and attaches a console handler. Both are process-global, so a single
    test that runs the CLI entry point silently reconfigures logging for every
    test that follows it.

    That matters because ``caplog`` captures through propagation. Once
    propagation is off, later tests asserting on log records see nothing and fail
    with no indication that an unrelated test caused it. The symptom is the
    confusing kind: the tests pass individually and fail as a suite, and which
    ones fail depends on collection order.

    Snapshot the three fields the configuration touches and restore them after
    every test, so the leak cannot cross a test boundary.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    try:
        yield
    finally:
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.handlers[:] = saved_handlers
