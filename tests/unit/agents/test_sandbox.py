# Copyright 2026 Google LLC
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

"""Unit tests for devops_bench.agents.sandbox: container naming and reaping."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from devops_bench.agents import sandbox
from devops_bench.core.errors import SubprocessError


def test_container_name_for_workspace_is_deterministic_and_prefixed() -> None:
    name = sandbox.container_name_for_workspace(Path("/tmp/workspace-abc123"))
    assert name == "devops-bench-agent-workspace-abc123"


def test_container_name_for_workspace_differs_per_workspace() -> None:
    a = sandbox.container_name_for_workspace(Path("/tmp/workspace-a"))
    b = sandbox.container_name_for_workspace(Path("/tmp/workspace-b"))
    assert a != b


def test_wrap_argv_includes_name_flag_when_container_name_given() -> None:
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
        container_name="devops-bench-agent-ws",
    )
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "devops-bench-agent-ws"


def test_wrap_argv_never_puts_extra_env_values_in_argv() -> None:
    """The structural fix: a secret handed in via extra_env must never appear
    as a value anywhere in the returned argv, only its NAME. This is what
    makes the value unreachable by string-formatting the command, independent
    of any redaction layer."""
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
        extra_env={"GEMINI_API_KEY": secret, "GOOGLE_API_KEY": secret, "GEMINI_MODEL": "pro"},
    )
    assert secret not in " ".join(argv)
    assert "pro" not in argv  # even a non-secret extra_env value stays out of argv


def test_wrap_argv_emits_bare_dash_e_flag_per_extra_env_key() -> None:
    """Each extra_env key becomes a bare `-e KEY` (no `=value`): this is what
    tells `docker run` to forward the value from its own client process
    environment into the container, rather than taking it from argv."""
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
        extra_env={"GEMINI_API_KEY": "whatever", "GEMINI_MODEL": "pro"},
    )
    assert "-e" in argv
    e_indices = [i for i, arg in enumerate(argv) if arg == "-e"]
    flag_values = [argv[i + 1] for i in e_indices]
    assert "GEMINI_API_KEY" in flag_values
    assert "GEMINI_MODEL" in flag_values
    # bare, not `KEY=value`
    assert "GEMINI_API_KEY=whatever" not in argv
    assert "GEMINI_MODEL=pro" not in argv


def test_wrap_argv_omits_name_flag_when_none_given() -> None:
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
    )
    assert "--name" not in argv


def test_kill_container_sends_sigterm_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-ws")

    assert calls[0] == ["docker", "kill", "--signal=TERM", "devops-bench-agent-ws"]


def test_kill_container_does_not_escalate_when_container_stops_within_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal case: the container stops on SIGTERM, no SIGKILL needed."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-ws")

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == [["docker", "kill", "--signal=TERM", "devops-bench-agent-ws"]]


def test_kill_container_escalates_to_sigkill_after_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container that ignores SIGTERM through the grace period gets SIGKILLed."""
    calls: list[list[str]] = []
    wait_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal wait_calls
        calls.append(argv)
        if argv[:2] == ["docker", "wait"]:
            wait_calls += 1
            if wait_calls == 1:
                raise SubprocessError(argv, returncode=-1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-ws")

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == [
        ["docker", "kill", "--signal=TERM", "devops-bench-agent-ws"],
        ["docker", "kill", "devops-bench-agent-ws"],
    ]
    assert wait_calls == 2


def test_kill_container_logs_error_when_container_survives_sigkill(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "wait"]:
            raise SubprocessError(argv, returncode=-1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with caplog.at_level("ERROR"):
        sandbox.kill_container("devops-bench-agent-ws")  # must not raise

    assert any("survived SIGKILL" in message for message in caplog.messages)


def test_kill_container_never_raises_when_docker_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Killing an already-gone container (the common case, ``--rm`` beat us to
    it) must be a harmless no-op, not a crash."""

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No such container")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-gone")  # must not raise


def test_container_guard_kills_container_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    with sandbox.container_guard("devops-bench-agent-ws"):
        pass

    assert killed == ["devops-bench-agent-ws"]


def test_container_guard_kills_container_on_exception_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash inside the guarded block must still reap the container."""
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    with (
        pytest.raises(RuntimeError, match="boom"),
        sandbox.container_guard("devops-bench-agent-ws"),
    ):
        raise RuntimeError("boom")

    assert killed == ["devops-bench-agent-ws"]


def test_container_guard_kills_container_on_timeout_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SubprocessError raised by a timed-out ``core.subprocess.run`` call
    inside the guarded block must still reap the container, exactly as the
    gemini_cli agent's own timeout handling relies on."""
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    timeout_exc: SubprocessError | None = None
    with sandbox.container_guard("devops-bench-agent-ws"):
        try:
            raise SubprocessError(["docker", "run"], returncode=-1, stdout="partial", stderr="")
        except SubprocessError as exc:
            # Mirrors how the agent harness swallows the timeout inside the
            # guarded block rather than letting it propagate.
            timeout_exc = exc

    assert timeout_exc is not None
    assert killed == ["devops-bench-agent-ws"]


def test_sweep_stray_containers_lists_by_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name-prefix filter still narrows candidates (it's just no longer
    the sole authorization to kill; see the owner-label tests below)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    list_call = calls[0]
    assert list_call[0:2] == ["docker", "ps"]
    assert any("devops-bench-agent-" in arg for arg in list_call)


def _ps_line(container_id: str, pid: str, boot_id: str) -> str:
    return f"{container_id}\t{pid}\t{boot_id}"


def test_sweep_stray_containers_does_not_kill_container_whose_owner_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core race: a container whose owning process is a live sibling run
    must never be reaped, even though its name matches the sweep's prefix
    filter."""
    own_pid = os.getpid()  # guaranteed alive: it's this test process
    monkeypatch.setattr(sandbox, "_boot_id", lambda: "boot-a")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0, stdout=_ps_line("live123", str(own_pid), "boot-a") + "\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == []


def test_sweep_stray_containers_kills_container_whose_owner_pid_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container recorded as owned by a PID that no longer exists, on the
    current boot, is provably orphaned: reap it."""
    monkeypatch.setattr(sandbox, "_boot_id", lambda: "boot-a")
    monkeypatch.setattr(sandbox, "_pid_alive", lambda pid: False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0, stdout=_ps_line("dead456", "999999", "boot-a") + "\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    # kill_container's own SIGTERM-first path: first call is the graceful kill.
    assert kill_calls[0] == ["docker", "kill", "--signal=TERM", "dead456"]


def test_sweep_stray_containers_kills_container_from_a_different_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot id mismatch means the owner's entire process tree ended with
    that boot, regardless of whether the recorded PID happens to be reused
    by an unrelated live process on THIS boot. Must reap without even
    consulting PID liveness."""
    monkeypatch.setattr(sandbox, "_boot_id", lambda: "boot-current")
    own_pid = os.getpid()  # alive on this boot, but under a stale boot id
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_ps_line("stale789", str(own_pid), "boot-old") + "\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls[0] == ["docker", "kill", "--signal=TERM", "stale789"]


def test_sweep_stray_containers_skips_unlabeled_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No owner labels (predates this change) means liveness cannot be
    proven either way; the safe default is to leave it alone rather than
    risk killing something live."""
    monkeypatch.setattr(sandbox, "_boot_id", lambda: "boot-a")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="unlabeled000\t\t\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == []


def test_sweep_stray_containers_handles_docker_ps_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="docker daemon not running")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()  # must not raise


def test_docker_user_spec_in_range_returns_uid_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)
    assert sandbox._docker_user_spec() == ("1000:1000", False, 1000, 1000)


def test_docker_user_spec_uid_above_ceiling_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: sandbox._DOCKER_MAX_ID + 1)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)
    assert sandbox._docker_user_spec() == ("0:0", True, sandbox._DOCKER_MAX_ID + 1, 1000)


def test_docker_user_spec_gid_above_ceiling_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: sandbox._DOCKER_MAX_ID + 1)
    assert sandbox._docker_user_spec() == ("0:0", True, 1000, sandbox._DOCKER_MAX_ID + 1)


def test_docker_user_spec_both_above_ceiling_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: sandbox._DOCKER_MAX_ID + 1)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: sandbox._DOCKER_MAX_ID + 1)
    assert sandbox._docker_user_spec() == (
        "0:0",
        True,
        sandbox._DOCKER_MAX_ID + 1,
        sandbox._DOCKER_MAX_ID + 1,
    )


def test_docker_user_spec_boundary_max_id_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: sandbox._DOCKER_MAX_ID)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: sandbox._DOCKER_MAX_ID)
    assert sandbox._docker_user_spec() == (
        f"{sandbox._DOCKER_MAX_ID}:{sandbox._DOCKER_MAX_ID}",
        False,
        sandbox._DOCKER_MAX_ID,
        sandbox._DOCKER_MAX_ID,
    )


def test_docker_user_spec_boundary_one_above_max_id_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: sandbox._DOCKER_MAX_ID + 1)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 0)
    assert sandbox._docker_user_spec() == ("0:0", True, sandbox._DOCKER_MAX_ID + 1, 0)


def test_docker_user_spec_fallback_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: sandbox._DOCKER_MAX_ID + 1)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: sandbox._DOCKER_MAX_ID + 1)
    with caplog.at_level("WARNING"):
        sandbox._docker_user_spec()
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert str(sandbox._DOCKER_MAX_ID + 1) in message
    assert str(sandbox._DOCKER_MAX_ID) in message
    assert "root" in message


def test_docker_user_spec_in_range_does_not_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)
    with caplog.at_level("WARNING"):
        sandbox._docker_user_spec()
    assert caplog.records == []


def test_wrap_argv_uses_docker_user_spec_right_after_user_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_docker_user_spec", lambda: ("1000:1000", False, 1000, 1000))
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
    )
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_wrap_argv_runs_command_unwrapped_when_not_root_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ownership problem when the container runs as the real operator uid,
    so the original argv should reach the image untouched, not wrapped in a
    shell."""
    monkeypatch.setattr(sandbox, "_docker_user_spec", lambda: ("1000:1000", False, 1000, 1000))
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
    )
    assert argv[-3:] == ["gemini", "-p", "hi"]


def test_wrap_argv_chowns_workspace_back_when_root_fallback_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root-fallback run must reclaim the operator's ownership of /workspace
    on exit, or the host-side artifact collector cannot read what root wrote
    (this is the bug that silently dropped generated_files/.kube and part of
    generated_files/.gemini in production runs)."""
    monkeypatch.setattr(
        sandbox, "_docker_user_spec", lambda: ("0:0", True, 3998470835, 3998470835)
    )
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
    )
    # The image's CMD is the shell shim, not the raw agent argv.
    image_index = argv.index("agent-image")
    command = argv[image_index + 1 :]
    assert command[0] == "sh"
    assert command[1] == "-c"
    assert "chown -R 3998470835:3998470835 /workspace" in command[2]
    assert "trap" in command[2]
    assert "EXIT" in command[2]
    assert command[3] == "--"
    assert command[4:] == ["gemini", "-p", "hi"]


def test_sweep_stray_containers_is_a_noop_when_none_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == []


def test_wrap_argv_emits_owner_pid_and_boot_id_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner labels sweep_stray_containers relies on must actually be
    stamped onto every container this harness launches."""
    monkeypatch.setattr(sandbox.os, "getpid", lambda: 4242)
    monkeypatch.setattr(sandbox, "_boot_id", lambda: "boot-xyz")
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
        container_name="devops-bench-agent-ws",
    )
    label_indices = [i for i, arg in enumerate(argv) if arg == "--label"]
    label_values = [argv[i + 1] for i in label_indices]
    assert "devops-bench.owner-pid=4242" in label_values
    assert "devops-bench.owner-boot-id=boot-xyz" in label_values


def test_pid_alive_true_for_current_process() -> None:
    assert sandbox._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_a_pid_that_does_not_exist() -> None:
    # An implausibly large pid does not correspond to any running process.
    assert sandbox._pid_alive(2**30) is False


def test_pid_alive_treats_permission_error_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pid owned by another user raises PermissionError on os.kill(pid, 0),
    but the process very much still exists."""

    def fake_kill(pid, sig):
        raise PermissionError("not your process")

    monkeypatch.setattr(sandbox.os, "kill", fake_kill)
    assert sandbox._pid_alive(1) is True
