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

"""Tests for the credential scanner.

Every fixture value in this file is synthetic and obviously fake. Do not
replace any of them with a value copied out of a real artifact.
"""

from pathlib import Path

from devops_bench.results.credscan import ScanStatus, scan_file, scan_tree

FAKE_GOOGLE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE00"
FAKE_ANTHROPIC_KEY = "sk-ant-api03-NOTAREALKEYNOTAREALKEY-FAKE"
FAKE_OPENAI_KEY = "sk-NOTAREALOPENAIKEY1234567890FAKE"
FAKE_OPENAI_PROJ_KEY = "sk-proj-NOTAREALOPENAIKEY1234567890FAKE"
FAKE_BEARER_TOKEN = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_google_api_key_matches(tmp_path):
    path = _write(tmp_path, "run.log", f"agent called out to {FAKE_GOOGLE_KEY} today\n")
    findings = scan_file(path)
    assert [f.rule for f in findings] == ["google-api-key"]
    assert findings[0].line == 1


def test_anthropic_key_matches(tmp_path):
    path = _write(tmp_path, "run.log", f"export ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}\n")
    findings = scan_file(path)
    assert "anthropic-key" in [f.rule for f in findings]


def test_openai_key_matches(tmp_path):
    path = _write(tmp_path, "cmd.txt", f"--api-key {FAKE_OPENAI_KEY}\n")
    findings = scan_file(path)
    assert "openai-key" in [f.rule for f in findings]


def test_openai_proj_key_variant_matches(tmp_path):
    path = _write(tmp_path, "cmd.txt", f"OPENAI_API_KEY={FAKE_OPENAI_PROJ_KEY}\n")
    findings = scan_file(path)
    assert "openai-key" in [f.rule for f in findings]


def test_env_assignment_matches(tmp_path):
    path = _write(tmp_path, "env.txt", "MY_SECRET_TOKEN=abcdefghijklmnopqrstuvwxyz012345\n")
    findings = scan_file(path)
    assert "env-assignment" in [f.rule for f in findings]


def test_env_assignment_compound_name_matches(tmp_path):
    # This is the exact shape the old redactor (suffix-only match) missed:
    # a name that CONTAINS API_KEY but does not END with it.
    path = _write(
        tmp_path,
        "env.txt",
        "GEMINI_API_KEY_STAGING=abcdefghijklmnopqrstuvwxyz012345\n",
    )
    findings = scan_file(path)
    assert "env-assignment" in [f.rule for f in findings]


def test_structured_secret_matches_json_dict_shape(tmp_path):
    # This is the leading hypothesis for why results.json still matched
    # after the old redactor: an env dump serialized as a JSON/dict-repr
    # key/value pair rather than a bare NAME=value assignment.
    path = _write(
        tmp_path,
        "results.json",
        '{\n  "GEMINI_API_KEY_STAGING": "abcdefghijklmnopqrstuvwxyz012345"\n}\n',
    )
    findings = scan_file(path)
    assert "structured-secret" in [f.rule for f in findings]


def test_structured_secret_matches_python_dict_repr_shape(tmp_path):
    path = _write(
        tmp_path,
        "run.log",
        "errors: {'ACCESS_TOKEN': 'abcdefghijklmnopqrstuvwxyz012345'}\n",
    )
    findings = scan_file(path)
    assert "structured-secret" in [f.rule for f in findings]


def test_bearer_token_matches(tmp_path):
    path = _write(tmp_path, "run.log", f"Authorization: Bearer {FAKE_BEARER_TOKEN}\n")
    findings = scan_file(path)
    assert "bearer-token" in [f.rule for f in findings]


def test_redacted_placeholder_env_assignment_not_a_finding(tmp_path):
    path = _write(tmp_path, "env.txt", "API_KEY=<redacted>\n")
    findings = scan_file(path)
    assert findings == []


def test_redacted_placeholder_asterisks_not_a_finding(tmp_path):
    path = _write(tmp_path, "env.txt", "API_KEY=****************************\n")
    findings = scan_file(path)
    assert findings == []


def test_redacted_placeholder_structured_secret_not_a_finding(tmp_path):
    path = _write(tmp_path, "results.json", '{"API_KEY": "<redacted>"}\n')
    findings = scan_file(path)
    assert findings == []


def test_clean_file_has_no_findings(tmp_path):
    path = _write(tmp_path, "run.log", "the run completed successfully\n")
    findings = scan_file(path)
    assert findings == []


def test_binary_file_is_unclassifiable(tmp_path):
    path = tmp_path / "artifact.bin"
    path.write_bytes(bytes([0, 1, 2, 3, 4, 0, 255, 254]))
    result = scan_tree(tmp_path)
    assert result.status is ScanStatus.UNCLASSIFIABLE
    assert path in result.unclassifiable


def test_invalid_utf8_file_is_unclassifiable(tmp_path):
    path = tmp_path / "run.log"
    path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    result = scan_tree(tmp_path)
    assert result.status is ScanStatus.UNCLASSIFIABLE
    assert path in result.unclassifiable


def test_scan_tree_clean(tmp_path):
    _write(tmp_path, "run.log", "nothing to see here\n")
    result = scan_tree(tmp_path)
    assert result.status is ScanStatus.CLEAN
    assert result.findings == []
    assert result.unclassifiable == []


def test_scan_tree_matched(tmp_path):
    _write(tmp_path, "results.json", f'{{"leak": "{FAKE_GOOGLE_KEY}"}}\n')
    result = scan_tree(tmp_path)
    assert result.status is ScanStatus.MATCHED
    assert any(f.rule == "google-api-key" for f in result.findings)


def test_scan_tree_unclassifiable_outranks_matched(tmp_path):
    _write(tmp_path, "results.json", f'{{"leak": "{FAKE_GOOGLE_KEY}"}}\n')
    (tmp_path / "artifact.bin").write_bytes(bytes([0, 1, 2, 0]))
    result = scan_tree(tmp_path)
    assert result.status is ScanStatus.UNCLASSIFIABLE
    assert result.findings
    assert result.unclassifiable
