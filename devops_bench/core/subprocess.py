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

"""Single entry point for running external commands."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import IO

from devops_bench.core.errors import SubprocessError
from devops_bench.core.logging import get_logger

__all__ = ["run", "redact", "tag_current_thread", "CompletedProcess"]

type CompletedProcess = subprocess.CompletedProcess[str]

_log = get_logger("core.subprocess")

# Names that mark an env-var assignment's value as secret-shaped, regardless
# of provider (GEMINI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY, ...).
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)

# `NAME=value` assignments, whether standalone (`FOO=bar cmd`) or following a
# docker-style `-e` flag (`-e FOO=bar`): both render as the same substring in
# the space-joined command string this module logs.
_ENV_ASSIGNMENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(\S+)")

# Bare secret shapes to catch even outside an env-var assignment, e.g. a key
# embedded in a URL or CLI flag value. Google API keys today; extend as other
# providers' key shapes turn up in a logged command.
_BARE_SECRET_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_-]{35}")]

_thread_local = threading.local()


def _mask(value: str) -> str:
    return f"{value[:4]}****"


def redact(cmd_str: str) -> str:
    """Mask secret-shaped values in a rendered command string, for logging only.

    Never apply this to the command actually executed; it exists solely to
    keep secrets out of log output.

    Masks two things:
        * The value of any ``NAME=value`` assignment (bare or after ``-e``)
          whose name matches a secret pattern (KEY, TOKEN, SECRET, PASSWORD,
          CREDENTIAL, case-insensitive).
        * Any bare token matching a known secret key shape, whether or not it
          appears in a ``NAME=value`` assignment.

    Args:
        cmd_str: The space-joined command string about to be logged.

    Returns:
        The same string with secret-shaped values masked, preserving a
        4-character prefix (e.g. ``AIza****``).
    """

    def _replace_env(match: re.Match[str]) -> str:
        name, value = match.group(1), match.group(2)
        if _SECRET_NAME_RE.search(name):
            return f"{name}={_mask(value)}"
        return match.group(0)

    redacted = _ENV_ASSIGNMENT_RE.sub(_replace_env, cmd_str)
    for pattern in _BARE_SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: _mask(m.group(0)), redacted)
    return redacted


def tag_current_thread(tag: str | None) -> None:
    """Set (or clear) a tag applied to every "running command" log line from this thread.

    :class:`~devops_bench.evalharness.safeguard_monitor.SafeguardMonitor` runs
    its periodic sampling on its own daemon thread; without a tag, its
    ``kubectl`` calls are indistinguishable in the log from the agent's own
    activity. A thread-local flag needs no plumbing through the sampling call
    chain (verifier -> get_resource -> this module) to reach the log line.

    Args:
        tag: Short label included in the log line, e.g. ``"sample"``.
            ``None`` clears any tag set on the current thread.
    """
    if tag is None:
        if hasattr(_thread_local, "tag"):
            del _thread_local.tag
    else:
        _thread_local.tag = tag


def _build_env(
    env: Mapping[str, str] | None,
    extra_env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if env is None and extra_env is None:
        return None
    base = dict(os.environ if env is None else env)
    if extra_env:
        base.update(extra_env)
    return base


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    timeout: float | None = None,
    input: str | None = None,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process.

    Args:
        cmd: Command and arguments as a sequence, never a shell string.
        cwd: Working directory.
        env: Full child environment; defaults to the parent's.
        extra_env: Variables overlaid on ``env`` (or the parent environment).
        check: Raise on a non-zero exit code.
        capture: Capture stdout and stderr.
        text: Decode output as text.
        timeout: Seconds before terminating the command.
        input: Data written to stdin.
        stream: Log stdout and stderr line-by-line as they arrive, while still
            returning the full captured text. Without this a long-running child
            is completely opaque until it exits, so an agent that wedges is
            indistinguishable from one that is working. Requires ``text``.

    Returns:
        The completed process.

    Raises:
        SubprocessError: If the command exits non-zero (when ``check``) or times out.
        ValueError: If ``stream`` is set without ``text``.
    """
    rendered = " ".join(str(arg) for arg in cmd)
    tag = getattr(_thread_local, "tag", None)
    prefix = f"running command [{tag}]:" if tag else "running command:"
    _log.debug("%s %s (cwd=%s)", prefix, redact(rendered), cwd or os.getcwd())

    if stream:
        if not text:
            raise ValueError("stream=True requires text=True")
        return _run_streamed(
            cmd,
            rendered=rendered,
            cwd=cwd,
            env=_build_env(env, extra_env),
            check=check,
            timeout=timeout,
            input=input,
        )

    try:
        completed = subprocess.run(
            list(cmd),
            cwd=cwd,
            env=_build_env(env, extra_env),
            capture_output=capture,
            text=text,
            timeout=timeout,
            input=input,
            # Same reasoning as _run_streamed: never let a child inherit the
            # operator's terminal as stdin. `input=` manages stdin itself, so only
            # override when there is none.
            stdin=subprocess.DEVNULL if input is None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log.error("command timed out after %ss: %s", timeout, redact(rendered))
        raise SubprocessError(
            cmd,
            returncode=-1,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        ) from exc

    if check and completed.returncode != 0:
        _log.error("command exited with %s: %s", completed.returncode, redact(rendered))
        raise SubprocessError(
            cmd,
            returncode=completed.returncode,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
        )

    return completed


def _pump(pipe: IO[str] | None, name: str, sink: list[str]) -> None:
    """Drain one pipe, logging every line as it arrives and buffering it for the caller.

    Reading both pipes on their own threads is what keeps this from deadlocking: a
    child that fills its stderr buffer while the parent is blocked reading stdout
    stops making progress, which is a hang with no diagnostic at all.
    """
    if pipe is None:
        return
    with pipe:
        for line in pipe:
            sink.append(line)
            _log.debug("[%s] %s", name, line.rstrip("\n"))


def _run_streamed(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    rendered: str,
    cwd: str | os.PathLike[str] | None,
    env: dict[str, str] | None,
    check: bool,
    timeout: float | None,
    input: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, logging stdout/stderr line-by-line as it arrives.

    Same contract as :func:`run`: the returned CompletedProcess carries the full
    captured text, and a non-zero exit or a timeout raises SubprocessError with
    whatever was collected before the process ended. The difference is purely that
    the caller sees output live rather than only on exit.
    """
    out_buf: list[str] = []
    err_buf: list[str] = []

    proc = subprocess.Popen(  # noqa: S603 - cmd is a sequence, never a shell string
        list(cmd),
        cwd=cwd,
        env=env,
        # DEVNULL, never None. `None` makes the child INHERIT the harness's stdin,
        # which in an interactive run is the operator's terminal. A CLI agent that
        # finds an open, non-TTY stdin can sit reading it forever: observed with the
        # gemini CLI, which prints "256-color support not detected" and then blocks,
        # looking exactly like a model call that never returns. It burned the full
        # agent timeout every time. No subprocess this harness launches has any
        # business reading the operator's keyboard.
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out_thread = threading.Thread(target=_pump, args=(proc.stdout, "out", out_buf), daemon=True)
    err_thread = threading.Thread(target=_pump, args=(proc.stderr, "err", err_buf), daemon=True)
    out_thread.start()
    err_thread.start()

    if input is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input)
        finally:
            proc.stdin.close()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill first, THEN join the pumps: they only exit when the pipes close, and
        # the pipes only close when the child dies. Joining first would hang here
        # for exactly as long as the child would have run anyway.
        proc.kill()
        proc.wait()
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        _log.error("command timed out after %ss: %s", timeout, redact(rendered))
        raise SubprocessError(
            cmd,
            returncode=-1,
            stdout="".join(out_buf),
            stderr="".join(err_buf),
        ) from exc

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)

    if check and returncode != 0:
        _log.error("command exited with %s: %s", returncode, redact(rendered))
        raise SubprocessError(
            cmd,
            returncode=returncode,
            stdout="".join(out_buf),
            stderr="".join(err_buf),
        )

    return subprocess.CompletedProcess(
        args=list(cmd),
        returncode=returncode,
        stdout="".join(out_buf),
        stderr="".join(err_buf),
    )


def _as_text(value: str | bytes | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")
