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

"""Unit tests for devops_bench.core.subprocess.

These exercise the wrapper against real, portable shell-free commands
(``python -c ...``) so behavior is verified end to end without mocking out
the standard library.
"""

import logging
import sys
import threading
from pathlib import Path

import pytest

from devops_bench.core import subprocess as bench_subprocess
from devops_bench.core.errors import SubprocessError


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_captures_stdout():
    result = bench_subprocess.run(_py("print('hello')"))
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_run_raises_on_nonzero_exit():
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py("import sys; sys.exit(3)"))
    assert exc_info.value.returncode == 3


def test_run_check_false_returns_completed_process():
    result = bench_subprocess.run(_py("import sys; sys.exit(5)"), check=False)
    assert result.returncode == 5


def test_run_captures_stderr_in_error():
    code = "import sys; sys.stderr.write('kaboom'); sys.exit(1)"
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py(code))
    assert "kaboom" in exc_info.value.stderr
    assert "kaboom" in str(exc_info.value)


def test_run_extra_env_overlays_parent(monkeypatch):
    monkeypatch.setenv("BASE_VAR", "from-parent")
    result = bench_subprocess.run(
        _py("import os; print(os.environ.get('EXTRA'), os.environ.get('BASE_VAR'))"),
        extra_env={"EXTRA": "from-extra"},
    )
    assert result.stdout.strip() == "from-extra from-parent"


def test_run_env_replaces_parent():
    result = bench_subprocess.run(
        _py("import os; print(os.environ.get('ONLY'))"),
        env={"ONLY": "isolated"},
    )
    assert result.stdout.strip() == "isolated"


def test_run_cwd(tmp_path):
    result = bench_subprocess.run(_py("import os; print(os.getcwd())"), cwd=tmp_path)
    # Resolve to defeat macOS /var -> /private/var symlinks.
    assert result.stdout.strip().endswith(tmp_path.name)


def test_run_input_is_forwarded():
    result = bench_subprocess.run(
        _py("import sys; sys.stdout.write(sys.stdin.read().upper())"),
        input="abc",
    )
    assert result.stdout == "ABC"


def test_run_timeout_raises_subprocess_error():
    with pytest.raises(SubprocessError):
        bench_subprocess.run(_py("import time; time.sleep(5)"), timeout=0.2)


def test_run_accepts_path_objects_in_cmd():
    result = bench_subprocess.run([Path(sys.executable), "-c", "print('hi')"])
    assert result.stdout.strip() == "hi"


def test_run_error_renders_path_command():
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run([Path(sys.executable), "-c", "import sys; sys.exit(1)"])
    assert sys.executable in str(exc_info.value)


def test_run_decodes_non_utf8_output_on_failure():
    code = r"import sys; sys.stderr.buffer.write(b'\xff\xfe'); sys.exit(1)"
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py(code), text=False)
    assert isinstance(exc_info.value.stderr, str)


def test_as_text_handles_bytes_and_none():
    assert bench_subprocess._as_text(None) is None
    assert bench_subprocess._as_text("text") == "text"
    assert isinstance(bench_subprocess._as_text(b"\xff\xfe"), str)


def _api_key(char: str) -> str:
    return "AIza" + char * 35


def test_redact_masks_secret_env_values_and_preserves_benign_env():
    key1 = _api_key("X")
    key2 = _api_key("Y")
    cmd = (
        f"docker run --rm -e GEMINI_API_KEY={key1} "
        f"-e GOOGLE_API_KEY={key2} -e BENIGN_VAR=hello image:tag"
    )
    redacted = bench_subprocess.redact(cmd)
    assert key1 not in redacted
    assert key2 not in redacted
    assert "GEMINI_API_KEY=AIza****" in redacted
    assert "GOOGLE_API_KEY=AIza****" in redacted
    assert "BENIGN_VAR=hello" in redacted


def test_redact_masks_bare_secret_shapes_outside_env_assignments():
    key = _api_key("Z")
    redacted = bench_subprocess.redact(f"curl https://example.com?key={key}")
    assert key not in redacted
    assert "AIza****" in redacted


def test_redact_does_not_affect_the_executed_command():
    key = _api_key("W")
    result = bench_subprocess.run(["echo", f"MY_SECRET_TOKEN={key}"])
    assert key in result.stdout


def test_running_command_log_is_redacted(caplog: pytest.LogCaptureFixture):
    key = _api_key("V")
    cmd = ["echo", "-e", f"MY_SECRET_TOKEN={key}", "-e", "BENIGN=ok"]
    with caplog.at_level(logging.DEBUG):
        bench_subprocess.run(cmd, check=False)
    assert key not in caplog.text
    assert "MY_SECRET_TOKEN=AIza****" in caplog.text
    assert "BENIGN=ok" in caplog.text


def test_tag_current_thread_marks_the_log_line_only_for_that_thread(
    caplog: pytest.LogCaptureFixture,
):
    def _tagged() -> None:
        bench_subprocess.tag_current_thread("sample")
        bench_subprocess.run(_py("print('from tagged thread')"))

    with caplog.at_level(logging.DEBUG):
        t = threading.Thread(target=_tagged)
        t.start()
        t.join()
        bench_subprocess.run(_py("print('from main thread')"))

    running_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("running command")
    ]
    assert any(line.startswith("running command [sample]:") for line in running_lines)
    assert any(line.startswith("running command:") for line in running_lines)


def test_child_does_not_inherit_parent_stdin() -> None:
    """A child reading stdin must see EOF immediately, not block on the terminal.

    Regression guard. With subprocess.run's default of ``stdin=None`` the child
    inherits the parent's stdin, and a CLI that inspects it can hang until the
    caller's timeout. ``cat`` blocks until EOF, so this only returns promptly when
    stdin has been closed for the child.
    """
    completed = bench_subprocess.run(
        ["sh", "-c", "cat > /dev/null; echo reached-eof"], check=False, timeout=10
    )
    assert completed.returncode == 0
    assert "reached-eof" in completed.stdout
