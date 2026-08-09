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

"""Unit tests for devops_bench.evalharness.artifacts: generated-file collection."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from devops_bench.evalharness.artifacts import collect_generated_files, snapshot_dir


def test_collect_generated_files_copies_new_entries(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    before = snapshot_dir(source)
    (source / "output.txt").write_text("hello")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    copied, failures = collect_generated_files(before, run_dir, source_dir=source)

    assert copied == ["output.txt"]
    assert failures == []
    assert (run_dir / "generated_files" / "output.txt").read_text() == "hello"


def test_collect_generated_files_returns_no_failures_when_nothing_new(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    before = snapshot_dir(source)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    copied, failures = collect_generated_files(before, run_dir, source_dir=source)

    assert copied == []
    assert failures == []
    assert not (run_dir / "generated_files").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits; cannot force EACCES")
def test_collect_generated_files_survives_one_unreadable_entry_and_still_collects_the_rest(
    tmp_path: Path,
) -> None:
    """Regression test for the defect that silently dropped a run's Gemini
    session transcript: a root-owned, mode-0700 directory a sandboxed
    container left behind (e.g. `.kube`) raised PermissionError partway
    through collection, and because ``new_entries`` iterates an unordered
    ``set``, whichever entry that failure landed on could abort collection of
    every other entry — including ones, like the transcript, that would
    otherwise have copied cleanly. Collection must now isolate a failure to
    just the entry that caused it and keep going.
    """
    source = tmp_path / "workspace"
    source.mkdir()
    before = snapshot_dir(source)

    good_dir = source / "good_dir"
    good_dir.mkdir()
    (good_dir / "transcript.jsonl").write_text('{"session": "data"}')

    locked_dir = source / "locked_dir"
    locked_dir.mkdir()
    (locked_dir / "secret").write_text("unreachable")
    locked_dir.chmod(0o000)  # unreadable, unlistable by anyone but its owner

    try:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        copied, failures = collect_generated_files(before, run_dir, source_dir=source)

        assert "good_dir" in copied
        assert (run_dir / "generated_files" / "good_dir" / "transcript.jsonl").exists()

        assert len(failures) == 1
        assert failures[0]["name"] == "locked_dir"
        assert "locked_dir" not in copied
    finally:
        # Restore permissions so tmp_path cleanup can remove the directory.
        locked_dir.chmod(stat.S_IRWXU)
