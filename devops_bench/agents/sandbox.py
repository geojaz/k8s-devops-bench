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
"""Run a CLI agent inside a container with a scoped cluster identity.

WHY THIS EXISTS. The CLI agent harnesses invoke the agent binary as a plain
subprocess inheriting the parent environment, so ``run_shell_command`` under
``--approval-mode yolo`` has the operator's entire filesystem. Observed in real
runs: an agent read another task's ``seed.sh`` and ``verify.sh``, searched the
home directory for its own fixtures by namespace name, and copied an unrelated
archive out of ``~/Downloads`` before running ``rm -rf``. The agent's own
workspace sandbox does not help, because it only guards the native file tools
and not the shell.

Two boundaries, and they solve different problems:

* The CONTAINER removes the host filesystem and the operator's environment. That
  closes the on-disk answer-key channel and the safety hazard.
* The SCOPED TOKEN decides what the agent may do to the cluster. That is task
  design, not containment: it makes the agent's identity part of the topology.

Neither closes the third channel. An agent that can read a cluster can read
anything a task put IN that cluster, and a task that needs the agent to inspect a
workload cannot use RBAC to hide that workload's own definition. Answer material
must not be seeded into the cluster in the first place; see the factory's
``answer-leakage.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from devops_bench.core import get_logger
from devops_bench.core.errors import SubprocessError
from devops_bench.core.subprocess import run

__all__ = [
    "sandbox_enabled",
    "build_agent_kubeconfig",
    "wrap_argv",
    "container_name_for_workspace",
    "kill_container",
    "container_guard",
    "sweep_stray_containers",
]

_log = get_logger("agents.sandbox")

# The ServiceAccount a task may seed to declare what its agent is allowed to do.
# Absent, the agent falls back to the operator's own context, which is what the
# harness did unconditionally before this module existed.
AGENT_SA_NAME = os.environ.get("BENCH_AGENT_SA", "bench-agent")
AGENT_SA_NAMESPACE = os.environ.get("BENCH_AGENT_SA_NAMESPACE", "bench-system")
TOKEN_DURATION = os.environ.get("BENCH_AGENT_TOKEN_DURATION", "2h")

# Every sandboxed container this harness starts carries this name prefix,
# followed by its run workspace's own directory name (see
# ``container_name_for_workspace``). The prefix is what lets
# ``sweep_stray_containers`` find and reap containers this harness itself
# created, and only those: a name match is the entire authorization to kill
# something, so it must never be able to match a container this harness did
# not start.
_CONTAINER_NAME_PREFIX = "devops-bench-agent-"

# Docker rejects, rather than clamps, a --user outside int32 range.
_DOCKER_MAX_ID = 2147483647

# Grace period after SIGTERM before escalating to SIGKILL on container teardown.
# Mirrors the port-forward teardown grace period in k8s.kubectl
# (_PORT_FORWARD_TERM_GRACE_SEC).
_CONTAINER_TERM_GRACE_SEC = 5


def _docker_user_spec() -> tuple[str, bool, int, int]:
    """uid:gid for `docker run --user`, falling back to root when the
    operator's ids are outside Docker's accepted range.

    Directory-backed identity services (GCE OS Login, LDAP, AD) hand out
    uids above Docker's int32 ceiling, and Docker rejects the whole run
    rather than clamping. Falling back to root keeps the container the
    isolation boundary, which is what this wrapper actually relies on: the
    repository, HOME, the Docker socket and ADC are all still unmounted.

    Returns a ``(spec, ran_as_root, uid, gid)`` tuple. ``ran_as_root`` tells
    :func:`wrap_argv` whether it must also arrange to hand workspace
    ownership back to the operator before the container exits: a root
    process inside the container writes files into the bind-mounted
    workspace as host uid/gid 0, and tools like ``kubectl`` and the Gemini
    CLI create some of their own state directories mode 0700/0750, which
    the (non-root) operator can then no longer read back on the host side.
    See :func:`wrap_argv` for how that ownership is reclaimed. ``uid``/``gid``
    are the operator's own resolved ids (never ``0:0``, even when
    ``ran_as_root`` is True), so callers needing the chown-back target reuse
    this function's own resolution instead of calling ``os.getuid()``/
    ``os.getgid()`` a second time.
    """
    uid, gid = os.getuid(), os.getgid()
    if 0 <= uid <= _DOCKER_MAX_ID and 0 <= gid <= _DOCKER_MAX_ID:
        return f"{uid}:{gid}", False, uid, gid
    _log.warning(
        "operator uid:gid %d:%d exceeds Docker's max id %d; running container as root",
        uid,
        gid,
        _DOCKER_MAX_ID,
    )
    return "0:0", True, uid, gid


def sandbox_enabled() -> bool:
    """True when the agent should run containerised.

    Opt-in rather than default so it can be A/B'd against the current behaviour
    while tasks are still being debugged.
    """
    return os.environ.get("BENCH_AGENT_SANDBOX", "").strip().lower() in {"docker", "1", "true"}


def current_cluster_name() -> str | None:
    """Derive the kind cluster name from the active kubectl context.

    The agent harness is handed a workspace and a prompt, never the cluster name,
    so rather than widen that interface we recover it from the context kind wrote:
    ``kind-<cluster>``. That is also exactly the prefix of the control-plane
    container name the container needs to reach, so the two stay consistent by
    construction. Returns None for a non-kind context, which is the signal that
    this sandbox's networking assumptions do not apply.
    """
    ctx = run(["kubectl", "config", "current-context"], check=False).stdout or ""
    ctx = ctx.strip()
    if not ctx.startswith("kind-"):
        _log.error("context %r is not a kind context; the docker-network sandbox assumes kind", ctx)
        return None
    return ctx[len("kind-") :]


def _kubectl_json(*args: str) -> dict:
    completed = run(["kubectl", *args, "-o", "json"], check=False)
    if completed.returncode != 0:
        return {}
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def build_agent_kubeconfig(cluster_name: str, dest_dir: Path) -> Path | None:
    """Write a kubeconfig the container can use, and return its path.

    Two things have to change from the operator's own kubeconfig:

    1. The SERVER. ``kind`` writes ``https://127.0.0.1:<port>``, which means
       nothing inside a container. ``kind`` also creates a Docker network named
       ``kind``, so a container joined to it reaches the API server at
       ``https://<cluster>-control-plane:6443``. The API server certificate
       covers the control-plane node name, so TLS still verifies and no
       ``--insecure-skip-tls-verify`` is needed.
    2. The CREDENTIAL. The operator's context authenticates with an admin client
       certificate. If the task seeded an agent ServiceAccount we mint a short
       lived token for it instead, so the agent holds exactly the permissions the
       task chose to give it. If it did not, we fall back to the admin
       credential and say so loudly, because that is a strictly larger grant than
       most tasks intend.

    Returns None when no kubeconfig could be built, in which case the caller
    should refuse to run rather than silently fall back to the host.
    """
    ca = run(
        [
            "kubectl",
            "config",
            "view",
            "--raw",
            "--minify",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        ],
        check=False,
    ).stdout
    if not ca:
        _log.error("could not read cluster CA from the current context; refusing to sandbox")
        return None

    server = f"https://{cluster_name}-control-plane:6443"

    sa_exists = (
        run(
            ["kubectl", "-n", AGENT_SA_NAMESPACE, "get", "sa", AGENT_SA_NAME],
            check=False,
        ).returncode
        == 0
    )

    if sa_exists:
        token = run(
            [
                "kubectl",
                "-n",
                AGENT_SA_NAMESPACE,
                "create",
                "token",
                AGENT_SA_NAME,
                f"--duration={TOKEN_DURATION}",
            ],
            check=False,
        ).stdout
        if not token:
            _log.error(
                "ServiceAccount %s/%s exists but token minting failed",
                AGENT_SA_NAMESPACE,
                AGENT_SA_NAME,
            )
            return None
        user_block = f"user: {{token: {token.strip()}}}"
        _log.info(
            "agent identity: ServiceAccount %s/%s (scoped by the task)",
            AGENT_SA_NAMESPACE,
            AGENT_SA_NAME,
        )
    else:
        # No task-declared identity. Reuse the operator's client cert. This is
        # cluster-admin on kind, so the container boundary is doing all the work
        # and the RBAC boundary is doing none.
        cert = run(
            [
                "kubectl",
                "config",
                "view",
                "--raw",
                "--minify",
                "-o",
                "jsonpath={.users[0].user.client-certificate-data}",
            ],
            check=False,
        ).stdout
        key = run(
            [
                "kubectl",
                "config",
                "view",
                "--raw",
                "--minify",
                "-o",
                "jsonpath={.users[0].user.client-key-data}",
            ],
            check=False,
        ).stdout
        if not (cert and key):
            _log.error("no agent ServiceAccount and no client cert in the current context")
            return None
        user_block = f"user: {{client-certificate-data: {cert}, client-key-data: {key}}}"
        _log.warning(
            "no ServiceAccount %s/%s: agent runs with the operator's admin credential. "
            "Seed one in the task's stack to scope it.",
            AGENT_SA_NAMESPACE,
            AGENT_SA_NAME,
        )

    path = dest_dir / "kubeconfig"
    path.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        f"clusters: [{{name: c, cluster: {{server: {server}, certificate-authority-data: {ca}}}}}]\n"
        f"users: [{{name: u, {user_block}}}]\n"
        "contexts: [{name: ctx, context: {cluster: c, user: u}}]\n"
        "current-context: ctx\n"
    )
    path.chmod(0o600)
    return path


def wrap_argv(
    argv: list[str],
    *,
    workspace: Path,
    kubeconfig: Path,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
    container_name: str | None = None,
) -> list[str]:
    """Wrap an agent command line in ``docker run``.

    The mount set is deliberately short, and what is ABSENT matters more than
    what is present:

    * the repository is not mounted, so task definitions, seed scripts and
      scoring rubrics are unreachable
    * ``$HOME`` is not mounted, and HOME is repointed inside the container so a
      bare ``~`` cannot resolve to the operator's profile
    * the Docker socket is not mounted; with it the container boundary would be
      decorative, and it is tempting precisely because the cluster is Docker-hosted
    * Application Default Credentials are not mounted. Model access should use a
      credential scoped to model access; ADC is the operator's whole cloud identity
      and is a larger grant than the filesystem access this wrapper removes.

    When the operator's uid/gid is out of Docker's accepted range (see
    :func:`_docker_user_spec`), the container runs as root, and the command is
    wrapped in a small ``sh -c`` shim that ``chown -R``s ``/workspace`` back to
    the operator's uid/gid via an EXIT trap. That trap fires on a clean exit, a
    non-zero exit, or any trappable signal, SIGTERM included, so a container
    torn down through :func:`kill_container`'s normal SIGTERM path still hands
    ownership back. It does NOT fire on SIGKILL: SIGKILL cannot be caught by a
    shell trap, so a container that has to be escalated all the way to SIGKILL
    (see :func:`kill_container`) still loses the chown-back and leaves
    ``/workspace`` root-owned. Root inside the container can ``chown`` to any
    uid, including one outside Docker's own ``--user`` validation range,
    because that ceiling is a client/daemon argument-parsing limitation, not a
    kernel one; the raw ``chown(2)`` syscall accepts the full uid range.
    Without this, files a root process creates (some of them mode 0700/0750,
    e.g. ``kubectl``'s and the Gemini CLI's own cache/log directories) are
    unreadable by the operator once the container exits, and the host-side
    artifact collector silently loses them.

    Args:
        container_name: When given, passed as ``--name``, giving the running
            container a deterministic identity a caller can reap by name (see
            :func:`container_guard` / :func:`sweep_stray_containers`) even
            after the local ``docker run`` client process is gone. Normally
            :func:`container_name_for_workspace` derived from ``workspace``.
    """
    image = image or os.environ.get("BENCH_AGENT_IMAGE", "")
    if not image:
        raise ValueError("BENCH_AGENT_IMAGE must name an image containing the agent CLI")

    # Only the caller's resolved overlay crosses the boundary. Deliberately NOT
    # scraping os.environ for well-known credential names: that would reinstate
    # "inherit whatever the operator happened to export", which is the behaviour
    # this wrapper exists to remove. If a credential is not in the resolved
    # config, the agent does not get it.
    env_flags: list[str] = []
    for key, value in (extra_env or {}).items():
        env_flags += ["-e", f"{key}={value}"]

    name_flags = ["--name", container_name] if container_name else []

    user_spec, ran_as_root, uid, gid = _docker_user_spec()
    command = list(argv)
    if ran_as_root:
        # Root can chown to any uid/gid, including one Docker's own --user flag
        # refused (see _docker_user_spec). Reclaim the workspace for the
        # operator on a clean exit, a non-zero exit, or a trappable signal
        # (SIGTERM included), not only a clean exit. "$@" (after "--") is the
        # original argv untouched; the trap runs after it returns or the shell
        # is signalled. SIGKILL cannot be trapped, so this does not cover
        # kill_container's SIGKILL escalation; see the docstring above.
        chown_back = f"trap 'chown -R {uid}:{gid} /workspace' EXIT; \"$@\""
        command = ["sh", "-c", chown_back, "--", *argv]

    return [
        # No -i. Keeping stdin open gives the agent an open, non-TTY stdin to block
        # on, and a headless `-p <prompt>` run never reads it. Combined with
        # stdin=DEVNULL in core.subprocess this closes the channel at both ends.
        "docker",
        "run",
        "--rm",
        *name_flags,
        "--network",
        "kind",
        "--user",
        user_spec,
        "-v",
        f"{workspace}:/workspace",
        "-v",
        f"{kubeconfig}:/kubeconfig:ro",
        "-e",
        "KUBECONFIG=/kubeconfig",
        "-e",
        "HOME=/workspace",
        "-w",
        "/workspace",
        *env_flags,
        image,
        *command,
    ]


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic ``docker run --name`` for one run's sandboxed agent.

    Ties the container 1:1 to the run's own workspace directory name (already
    unique per run: it comes from ``mint_dir(...)`` / ``TemporaryDirectory``),
    so a reaper can find and kill a stray container purely from its name,
    without threading a separate run id through the agent harness.
    """
    return f"{_CONTAINER_NAME_PREFIX}{workspace.name}"


def kill_container(name: str) -> None:
    """Best-effort ``docker kill`` by name, SIGTERM first. Never raises.

    ``--rm`` only removes a container once *the container's own process*
    exits; a ``docker run`` client killed out from under it (which is exactly
    what happens when ``core.subprocess.run`` hits its timeout and SIGKILLs
    the local wrapper process) leaves the container itself running in the
    daemon, unaffected, silently burning whatever quota the agent inside it
    is still spending. This is what actually stops it, independent of what
    happened to the local ``docker run`` process. A container that is already
    gone (the common case, when the agent exited cleanly and ``--rm`` already
    reaped it) fails harmlessly.

    Sends SIGTERM first and only escalates to SIGKILL if the container is
    still running after :data:`_CONTAINER_TERM_GRACE_SEC`: a bare
    ``docker kill`` sends SIGKILL, which cannot be caught by the ``sh -c EXIT``
    trap :func:`wrap_argv` installs for a root-fallback run, so jumping
    straight to SIGKILL was skipping that trap on exactly the path (a
    timed-out or hung agent) it exists to cover. Mirrors the SIGTERM-then-
    SIGKILL escalation in ``k8s.kubectl``'s port-forward teardown.
    """
    result = run(["docker", "kill", "--signal=TERM", name], check=False)
    if result.returncode != 0:
        # Already gone (the common case) or the daemon errored; nothing
        # further to do either way.
        return

    try:
        # `docker wait` blocks until the container's own process exits,
        # mirroring how kubectl.py's port-forward teardown blocks on
        # process.wait(timeout=...) after terminate().
        run(["docker", "wait", name], check=False, timeout=_CONTAINER_TERM_GRACE_SEC)
    except SubprocessError:
        _log.warning("sandbox container %s ignored SIGTERM, escalating to SIGKILL", name)
        run(["docker", "kill", name], check=False)
        try:
            run(["docker", "wait", name], check=False, timeout=_CONTAINER_TERM_GRACE_SEC)
        except SubprocessError:
            _log.error("sandbox container %s survived SIGKILL; abandoning it", name)
        else:
            _log.info("reaped sandbox container %s", name)
    else:
        _log.info("reaped sandbox container %s", name)


@contextlib.contextmanager
def container_guard(name: str) -> Iterator[None]:
    """Guarantee :func:`kill_container` runs on every exit path.

    Wrap the ``docker run`` invocation in this so a normal return, an
    exception, and a timeout (which ``core.subprocess.run`` turns into a
    ``SubprocessError``) all still reap the container by name. ``--rm``
    already handles the graceful-exit case on its own; this closes every
    other one.
    """
    try:
        yield
    finally:
        kill_container(name)


def sweep_stray_containers() -> None:
    """Best-effort reap of containers this harness left running from a prior run.

    Intended to run once at harness start (before any run's own container
    exists) so a container orphaned by a prior crash or a killed harness
    process gets cleaned up before it burns any more quota. Matches
    exclusively on :data:`_CONTAINER_NAME_PREFIX`, this harness's own naming
    convention, so it can never reap a container it did not itself create.
    """
    listed = run(
        ["docker", "ps", "-q", "--filter", f"name=^{_CONTAINER_NAME_PREFIX}"],
        check=False,
    )
    if listed.returncode != 0:
        return
    stray_ids = [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()]
    for container_id in stray_ids:
        result = run(["docker", "kill", container_id], check=False)
        if result.returncode == 0:
            _log.warning(
                "reaped stray sandbox container %s left running from a prior run", container_id
            )


def make_workspace() -> Path:
    """A world-writable scratch dir the container's non-root user can write to."""
    path = Path(tempfile.mkdtemp(prefix="devops-bench-sandbox-"))
    path.chmod(0o777)
    return path


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
