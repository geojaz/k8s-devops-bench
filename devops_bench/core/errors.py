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

"""Exception types raised across devops-bench."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

__all__ = [
    "DevOpsBenchError",
    "ConfigError",
    "RegistryError",
    "AlreadyRegisteredError",
    "InvalidKeyError",
    "NotRegisteredError",
    "MissingDependencyError",
    "SubprocessError",
    "redact",
]

# Names that mark an env-var assignment's value as secret-shaped, regardless
# of provider (GEMINI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY, ...). Lives
# here, not only in core.subprocess, because SubprocessError's own message
# (built below) is a second place a secret-bearing command can be rendered to
# a string; both need the same redaction, and this module has no dependency
# on core.subprocess so it can be the shared home without a circular import.
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)

# `NAME=value` assignments, whether standalone (`FOO=bar cmd`) or following a
# docker-style `-e` flag (`-e FOO=bar`): both render as the same substring in
# a space-joined command string.
_ENV_ASSIGNMENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(\S+)")

# Bare secret shapes to catch even outside an env-var assignment, e.g. a key
# embedded in a URL or CLI flag value. Google API keys today; extend as other
# providers' key shapes turn up in a logged command or message.
_BARE_SECRET_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_-]{35}")]


def _mask(value: str) -> str:
    return f"{value[:4]}****"


def redact(text: str) -> str:
    """Mask secret-shaped values in a rendered command or message string.

    Never apply this to a command actually about to be executed; it exists
    solely to keep secrets out of anything that gets logged, raised, or
    persisted as text.

    Masks two things:
        * The value of any ``NAME=value`` assignment (bare or after ``-e``)
          whose name matches a secret pattern (KEY, TOKEN, SECRET, PASSWORD,
          CREDENTIAL, case-insensitive).
        * Any bare token matching a known secret key shape, whether or not it
          appears in a ``NAME=value`` assignment.

    Args:
        text: The string about to be logged, raised, or serialized.

    Returns:
        The same string with secret-shaped values masked, preserving a
        4-character prefix (e.g. ``AIza****``).
    """

    def _replace_env(match: re.Match[str]) -> str:
        name, value = match.group(1), match.group(2)
        if _SECRET_NAME_RE.search(name):
            return f"{name}={_mask(value)}"
        return match.group(0)

    redacted = _ENV_ASSIGNMENT_RE.sub(_replace_env, text)
    for pattern in _BARE_SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: _mask(m.group(0)), redacted)
    return redacted


class DevOpsBenchError(Exception):
    """Base class for all errors raised by devops-bench."""


class ConfigError(DevOpsBenchError):
    """Raised when required configuration is missing or malformed."""


class RegistryError(DevOpsBenchError):
    """Base class for registry registration and lookup failures."""


class AlreadyRegisteredError(RegistryError):
    """Raised when registering a name that is already taken in a registry."""

    def __init__(self, registry_name: str, key: str) -> None:
        self.registry_name = registry_name
        self.key = key
        super().__init__(f"{key!r} is already registered in the {registry_name!r} registry")


class InvalidKeyError(RegistryError):
    """Raised when a key violates the key policy of a registry."""

    def __init__(self, registry_name: str, key: str, reason: str) -> None:
        self.registry_name = registry_name
        self.key = key
        self.reason = reason
        super().__init__(f"{key!r} is not a valid key for the {registry_name!r} registry: {reason}")


class NotRegisteredError(RegistryError):
    """Raised when looking up a name that is not present in a registry."""

    def __init__(self, registry_name: str, key: str, available: Sequence[str] = ()) -> None:
        self.registry_name = registry_name
        self.key = key
        self.available = tuple(available)
        message = f"{key!r} is not registered in the {registry_name!r} registry"
        if self.available:
            message += f"; available: {', '.join(sorted(self.available))}"
        super().__init__(message)


class MissingDependencyError(DevOpsBenchError):
    """Raised when a feature requires an optional dependency that is not installed."""

    def __init__(self, feature: str, extra: str) -> None:
        self.feature = feature
        self.extra = extra
        super().__init__(
            f"{feature} requires the optional dependency group {extra!r}. "
            f"Install it with: pip install devops-bench[{extra}]"
        )


class SubprocessError(DevOpsBenchError):
    """Raised when a subprocess exits non-zero or times out."""

    def __init__(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        returncode: int,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        self.cmd = [str(part) for part in cmd]
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        # redact() covers the command AND stderr: a failing command's own
        # stderr can echo back the invocation (or another secret-shaped
        # value) just as easily as the argv does. self.cmd itself is kept
        # unredacted for callers that need the real argv programmatically
        # (e.g. re-running it); only the string form is scrubbed.
        message = redact(f"command failed with exit code {returncode}: {' '.join(self.cmd)}")
        if stderr:
            message += f"\nstderr: {redact(stderr.strip())}"
        super().__init__(message)
