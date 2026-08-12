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

"""Credential scanner for measuring, not fixing, leaked secrets in artifacts.

This module is a standalone measurement tool. It is not wired into any run
path (evalharness.reporter, run_sweep, artifacts, etc.) and must not be
imported from one without a deliberate follow-up decision: its rules are
intentionally broader than the redaction already applied at write time (see
``devops_bench.core.errors.redact``), so it is expected to surface things a
narrower, name-suffix-based redactor misses.

Contract for anything that calls this module: a :class:`Finding` carries a
path, a line number, and the *name* of the rule that matched. It never
carries the matched text, a prefix or suffix of it, its length, or a hash of
it. Do not add a field that would change that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "Finding",
    "ScanResult",
    "ScanStatus",
    "UnscannableFileError",
    "scan_file",
    "scan_tree",
]

# Files above this size are treated as unclassifiable rather than scanned.
# Run artifacts should never legitimately be this large as plain text; a file
# this size is more likely a binary blob or a pathological log than a
# scannable text file, and reading it whole is wasteful either way.
MAX_SCAN_BYTES = 5 * 1024 * 1024

# Name fragments that mark a key/value pair as secret-shaped regardless of
# where the fragment appears in the name (CONTAINS, not ENDSWITH). The prior
# redactor (devops_bench.core.errors.redact) anchors its name check to a
# suffix by construction of its regex ordering; a compound name such as
# GEMINI_API_KEY_STAGING still contains API_KEY but the value that follows it
# is scanned in isolation from the name once it is inside a JSON payload, so
# nothing in that path ever asks whether the name matches at all. That gap is
# exactly what the structured-secret rule below closes.
_SECRET_NAME_KEYWORDS = (
    "API_KEY",
    "ACCESS_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "BEARER",
)

_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[0-9A-Za-z_-]{20,}")
_OPENAI_KEY_RE = re.compile(r"sk-proj-[0-9A-Za-z_-]{20,}|sk-(?!ant-)[0-9A-Za-z]{20,}")

# Bare `NAME=value` shape, standalone or after a flag like `-e`.
#
# Captured process output (e.g. an `env` dump) is often stored as a single
# JSON string with literal `\n` escapes rather than real newlines, so an
# entire environment dump can appear as one physical line. A plain `\S+`
# value would greedily swallow a literal backslash-n (two non-whitespace
# characters) and everything after it, hiding every later `KEY=` on the
# same line. Stop the value at a literal backslash-n as well as at real
# whitespace.
_ENV_ASSIGNMENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*((?:(?!\\n)\S)+)")

# JSON or Python-dict-repr `"NAME": "value"` / `'NAME': 'value'` shape.
_STRUCTURED_SECRET_RE = re.compile(
    r"""["']([A-Za-z_][A-Za-z0-9_]*)["']\s*:\s*["']([^"']*)["']"""
)

# A long, high-entropy run of base64/hex characters immediately following a
# known "here comes a credential" marker, independent of any provider-prefix
# shape (AIza..., sk-...). This is intentionally the loosest rule here and
# will also catch things like git commit SHAs or image digests after a bare
# ":" or "="; that tradeoff is deliberate per the task spec, which asks for a
# prefix-independent bearer-token rule.
_BEARER_TOKEN_RE = re.compile(
    r"(?:--api-key[= ]|--token[= ]|Bearer\s+|[:=]\s*)([A-Za-z0-9+/_-]{32,}={0,2})"
)


def _name_has_secret_keyword(name: str) -> bool:
    upper = name.upper()
    return any(keyword in upper for keyword in _SECRET_NAME_KEYWORDS)


def _is_redacted_placeholder(value: str) -> bool:
    """True if ``value`` is already an obviously-redacted placeholder.

    Only the two forms the task calls out: the literal string ``<redacted>``
    (case-insensitive) or a value made entirely of asterisks. Anything else
    is treated as live content, even if it looks like it might be masked in
    some other convention this scanner does not know about.
    """
    stripped = value.strip().strip("\"',")
    if not stripped:
        return False
    if stripped.lower() == "<redacted>":
        return True
    if all(char == "*" for char in stripped):
        return True
    return False


@dataclass
class Finding:
    path: Path
    line: int
    rule: str


class ScanStatus(Enum):
    CLEAN = "CLEAN"
    MATCHED = "MATCHED"
    UNCLASSIFIABLE = "UNCLASSIFIABLE"


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    unclassifiable: list[Path] = field(default_factory=list)

    @property
    def status(self) -> ScanStatus:
        # Unclassifiable outranks matched, which outranks clean. A caller
        # that only checks for findings and defaults to clean otherwise is
        # exactly the bug this scanner exists to not repeat: an unreadable
        # or binary file must never present as clean just because nothing in
        # it happened to match a rule.
        if self.unclassifiable:
            return ScanStatus.UNCLASSIFIABLE
        if self.findings:
            return ScanStatus.MATCHED
        return ScanStatus.CLEAN

    def merge(self, other: "ScanResult") -> None:
        self.findings.extend(other.findings)
        self.unclassifiable.extend(other.unclassifiable)


class UnscannableFileError(Exception):
    """Raised by scan_file when a file cannot be meaningfully scanned.

    Covers: unreadable (permissions, I/O error), above MAX_SCAN_BYTES,
    containing a null byte (treated as binary), or not valid UTF-8.
    """

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise UnscannableFileError(path, f"stat failed: {exc.__class__.__name__}") from exc

    if size > MAX_SCAN_BYTES:
        raise UnscannableFileError(path, "exceeds size cap")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UnscannableFileError(path, f"read failed: {exc.__class__.__name__}") from exc

    if b"\x00" in raw:
        raise UnscannableFileError(path, "binary (null byte)")

    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise UnscannableFileError(path, f"not valid utf-8: {exc}") from exc


def _scan_line(path: Path, line_no: int, line: str) -> list[Finding]:
    findings: list[Finding] = []

    if _GOOGLE_KEY_RE.search(line):
        findings.append(Finding(path, line_no, "google-api-key"))
    if _ANTHROPIC_KEY_RE.search(line):
        findings.append(Finding(path, line_no, "anthropic-key"))
    if _OPENAI_KEY_RE.search(line):
        findings.append(Finding(path, line_no, "openai-key"))

    for match in _ENV_ASSIGNMENT_RE.finditer(line):
        name, value = match.group(1), match.group(2)
        if _name_has_secret_keyword(name) and not _is_redacted_placeholder(value):
            findings.append(Finding(path, line_no, "env-assignment"))
            break

    for match in _STRUCTURED_SECRET_RE.finditer(line):
        name, value = match.group(1), match.group(2)
        if _name_has_secret_keyword(name) and not _is_redacted_placeholder(value):
            findings.append(Finding(path, line_no, "structured-secret"))
            break

    for match in _BEARER_TOKEN_RE.finditer(line):
        value = match.group(1)
        if not _is_redacted_placeholder(value):
            findings.append(Finding(path, line_no, "bearer-token"))
            break

    return findings


def scan_file(path: Path) -> list[Finding]:
    """Scan a single file, returning every finding.

    Raises UnscannableFileError if the file cannot be meaningfully scanned
    (binary, not UTF-8, unreadable, or above the size cap). Callers must not
    treat that exception as "no findings"; scan_tree below surfaces it as a
    distinct outcome for exactly this reason.
    """
    text = _read_text(path)
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        findings.extend(_scan_line(path, line_no, line))
    return findings


def scan_tree(root: Path) -> ScanResult:
    """Scan every regular file under root, recursively.

    Symlinks are not followed (Path.is_file() on a broken symlink is False,
    and rglob does not traverse into a symlinked directory by default), which
    keeps this from escaping root or double-scanning shared artifact stores.
    """
    result = ScanResult()
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            findings = scan_file(candidate)
        except UnscannableFileError:
            result.unclassifiable.append(candidate)
            continue
        result.findings.extend(findings)
    return result
