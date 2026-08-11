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

"""Unit tests for the ``ResultReporter`` sink.

The reporter is a thin sink: it writes the payload it is handed to
``results.json`` / ``rows.json`` / ``manifest.json`` under a per-run directory,
and mints unique, filesystem-safe run directories. The one exception to
"verbatim" is ``write``/``write_rows``, which redact secret-shaped strings
(anything that looks like ``SOME_KEY=<value>`` or a bare provider key) before
serializing: this is the last point before disk, and a credential-shaped
string reaching it, from any leak path, known or not, should not survive to
``results.json``. These tests pin that behavior directly against
``ResultReporter``, with no dependency on the eval harness.
"""

from __future__ import annotations

import json
from pathlib import Path

from devops_bench.evalharness.reporter import ResultReporter


def test_reporter_writes_results_json_with_indented_payload(tmp_path: Path) -> None:
    """The reporter writes ``results.json`` under the run dir with the input list."""
    reporter = ResultReporter(results_root=tmp_path)
    run_dir = reporter.new_run_dir()
    payload = [{"name": "demo", "status": "success"}]

    written = reporter.write(run_dir, payload)

    assert written == run_dir / "results.json"
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk == payload


def test_reporter_redacts_secret_shaped_value_in_results_json(tmp_path: Path) -> None:
    """A credential-shaped string in ANY field must never reach results.json
    in the clear, regardless of which leak path put it there."""
    reporter = ResultReporter(results_root=tmp_path)
    run_dir = reporter.new_run_dir()
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"
    payload = [
        {
            "name": "demo",
            "status": "error",
            "error": f"command failed: docker run -e GEMINI_API_KEY={secret} image",
            "nested": {"errors": [f"GOOGLE_API_KEY={secret}"]},
        }
    ]

    written = reporter.write(run_dir, payload)

    on_disk_text = written.read_text(encoding="utf-8")
    assert secret not in on_disk_text
    assert "GEMINI_API_KEY=" in on_disk_text  # the name survives, only the value is masked
    on_disk = json.loads(on_disk_text)
    assert on_disk[0]["status"] == "error"  # non-secret fields pass through untouched
    assert on_disk[0]["name"] == "demo"


def test_reporter_write_does_not_mutate_caller_payload(tmp_path: Path) -> None:
    """Redaction must not mutate the list/dicts the caller passed in."""
    reporter = ResultReporter(results_root=tmp_path)
    run_dir = reporter.new_run_dir()
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"
    payload = [{"error": f"TOKEN={secret}"}]

    reporter.write(run_dir, payload)

    assert payload[0]["error"] == f"TOKEN={secret}"


def test_reporter_writes_rows_json(tmp_path: Path) -> None:
    """``write_rows`` writes the flattened rows to ``rows.json`` verbatim."""
    reporter = ResultReporter(results_root=tmp_path)
    run_dir = reporter.new_run_dir()
    rows = [{"setupId": "m-h", "taskName": "demo", "outcomeScore": 0.9}]

    written = reporter.write_rows(run_dir, rows)

    assert written == run_dir / "rows.json"
    assert json.loads(written.read_text(encoding="utf-8")) == rows


def test_reporter_writes_manifest_json(tmp_path: Path) -> None:
    """``write_manifest`` writes the run-level manifest to ``manifest.json``."""
    reporter = ResultReporter(results_root=tmp_path)
    run_dir = reporter.new_run_dir()
    manifest = {"runId": run_dir.name, "model": "m", "augmentation": ["mcp"]}

    written = reporter.write_manifest(run_dir, manifest)

    assert written == run_dir / "manifest.json"
    assert json.loads(written.read_text(encoding="utf-8")) == manifest


def test_new_run_dir_returns_unique_path_under_root(tmp_path: Path) -> None:
    """Two reporters sharing a root produce directories underneath it."""
    reporter = ResultReporter(results_root=tmp_path)
    a = reporter.new_run_dir()
    assert a.parent == tmp_path
    assert a.name.startswith("run_")


def test_new_run_dir_records_last_run_dir(tmp_path: Path) -> None:
    """``new_run_dir`` exposes the most recent dir via ``last_run_dir``."""
    r = ResultReporter(results_root=tmp_path)
    assert r.last_run_dir is None
    d = r.new_run_dir()
    assert r.last_run_dir == d


def test_new_run_dir_appends_run_id(tmp_path: Path) -> None:
    """A supplied run id is appended so concurrent runs do not collide."""
    r = ResultReporter(results_root=tmp_path, run_id="20260101-120000-4242")
    d = r.new_run_dir()
    assert d.name.startswith("run_")
    assert d.name.endswith("_20260101-120000-4242")


def test_new_run_dir_sanitizes_run_id(tmp_path: Path) -> None:
    """Filesystem-unsafe characters in the run id are replaced."""
    r = ResultReporter(results_root=tmp_path, run_id="a/b c:d")
    d = r.new_run_dir()
    assert "/" not in d.name[len("run_") :]
    assert d.name.endswith("_a-b-c-d")
