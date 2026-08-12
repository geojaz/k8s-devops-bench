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

"""Tests for the tri-state ``sandboxed`` field on Manifest and ResultRow."""

from devops_bench.results import SCHEMA_VERSION, Manifest, ResultRow


def _manifest(**overrides):
    base = dict(
        schema_version=SCHEMA_VERSION,
        run_id="run_20260601_000000",
        t="2026-06-01T00:00:00Z",
        setup_id="m-h",
        model="m",
        harness="h",
        augmentation=[],
    )
    base.update(overrides)
    return Manifest(**base)


def _row(**overrides):
    base = dict(
        setup_id="m-h",
        model="m",
        harness="h",
        augmentation=[],
        run_id="run_20260601_000000",
        t="2026-06-01T00:00:00Z",
        task_folder="f",
        task_name="t",
        iteration=0,
        outcome_score=None,
        tool_score=None,
        latency_sec=0.0,
        input_tokens=None,
        output_tokens=None,
        status="success",
    )
    base.update(overrides)
    return ResultRow(**base)


def test_manifest_sandboxed_defaults_to_unknown():
    manifest = _manifest()
    assert manifest.sandboxed is None
    assert manifest.sandbox_image is None


def test_result_row_sandboxed_defaults_to_unknown():
    row = _row()
    assert row.sandboxed is None
    assert row.sandbox_image is None


def test_manifest_sandboxed_unknown_serializes_to_json_null():
    d = _manifest().to_dict()
    assert "sandboxed" in d
    assert d["sandboxed"] is None
    assert "sandboxImage" in d
    assert d["sandboxImage"] is None


def test_result_row_sandboxed_unknown_serializes_to_json_null():
    d = _row().to_dict()
    assert "sandboxed" in d
    assert d["sandboxed"] is None
    assert "sandboxImage" in d
    assert d["sandboxImage"] is None


def test_manifest_sandboxed_true_round_trips():
    d = _manifest(sandboxed=True, sandbox_image="devops-bench/agent-sandbox:dev").to_dict()
    assert d["sandboxed"] is True
    assert d["sandboxImage"] == "devops-bench/agent-sandbox:dev"
    restored = Manifest.model_validate(d)
    assert restored.sandboxed is True
    assert restored.sandbox_image == "devops-bench/agent-sandbox:dev"


def test_manifest_sandboxed_false_round_trips():
    d = _manifest(sandboxed=False).to_dict()
    assert d["sandboxed"] is False
    restored = Manifest.model_validate(d)
    assert restored.sandboxed is False


def test_result_row_sandboxed_true_round_trips():
    d = _row(sandboxed=True, sandbox_image="devops-bench/agent-sandbox:dev").to_dict()
    assert d["sandboxed"] is True
    assert d["sandboxImage"] == "devops-bench/agent-sandbox:dev"
    restored = ResultRow.model_validate(d)
    assert restored.sandboxed is True
    assert restored.sandbox_image == "devops-bench/agent-sandbox:dev"


def test_result_row_sandboxed_false_round_trips():
    d = _row(sandboxed=False).to_dict()
    assert d["sandboxed"] is False
    restored = ResultRow.model_validate(d)
    assert restored.sandboxed is False


def test_manifest_missing_sandboxed_key_parses_as_unknown():
    """Historical manifest.json written before this field existed has no
    ``sandboxed`` key at all; re-parsing it must recover ``None`` (unknown),
    never a fabricated ``False``."""
    legacy = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": "run_20260101_000000",
        "t": "2026-01-01T00:00:00Z",
        "setupId": "m-h",
        "model": "m",
        "harness": "h",
        "augmentation": [],
    }
    restored = Manifest.model_validate(legacy)
    assert restored.sandboxed is None
    assert restored.sandbox_image is None
