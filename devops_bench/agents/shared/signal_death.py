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

"""Signal-death classification shared by the CLI agents.

128+N is the shell/subprocess convention for "killed by signal N" (137 =
128+9 = SIGKILL, 143 = 128+15 = SIGTERM), covering every signal generically
rather than special-casing the two most common ones. A signal death means
something OUTSIDE the agent's own reasoning ended the process: an OOM kill,
an operator Ctrl-C, or the sandbox reaper killing the container. It must not
be indistinguishable from the agent genuinely failing on its own, or an
infra kill silently corrupts the "did the agent fail" evidence in
results.json.

This does not introduce a new status value: ``agent_error``/``failed`` stay
load-bearing in the harness's ``_score`` exclusion list. Callers record the
returned ``signal_death`` dict as an additional metadata field alongside the
existing status, never as a replacement for it.
"""

from __future__ import annotations

import signal

__all__ = ["classify_returncode"]


def classify_returncode(binary_label: str, returncode: int, stderr: str) -> tuple[dict | None, str]:
    """Classify a non-zero subprocess exit for ``binary_label``.

    Args:
        binary_label: Short name of the binary for the error message (e.g.
            ``"gemini"``, ``"agy"``, ``"oc agent"``).
        returncode: The subprocess's non-zero exit code.
        stderr: Stripped stderr text, or ``""`` when there was none.

    Returns:
        A ``(signal_death, message)`` pair. ``signal_death`` is ``None`` for
        an ordinary non-zero exit, else a dict with ``signal``,
        ``signal_name``, and ``returncode`` keys. ``message`` is a one-line
        description suitable for ``AgentResult.errors``.
    """
    signal_num = returncode - 128 if returncode > 128 else None
    if signal_num is None:
        return None, f"{binary_label} exited {returncode}: {stderr or '<no stderr>'}"
    try:
        signal_name = signal.Signals(signal_num).name
    except ValueError:
        signal_name = f"SIG{signal_num}"
    signal_death = {
        "signal": signal_num,
        "signal_name": signal_name,
        "returncode": returncode,
    }
    message = (
        f"{binary_label} killed by signal {signal_num} ({signal_name}), "
        f"exit {returncode}: {stderr or '<no stderr>'}"
    )
    return signal_death, message
