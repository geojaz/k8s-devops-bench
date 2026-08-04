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

"""Unit tests for :mod:`devops_bench.evalharness.safeguard_monitor`.

Fake leaves stand in for real cluster I/O so these tests run fast and never
touch kubectl. ``_FlipThenRestore`` is the ``_Countdown``-shaped test double
from ``tests/unit/verification/test_combinators.py``, adapted to the exact
shape of the motivating bug: fails once, mid-run, then recovers before the
run ends.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Literal

import pytest

from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.evalharness.safeguard_monitor import HoldObservation, SafeguardMonitor
from devops_bench.tasks import Task
from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.spec import VerificationEntry, parse_entries

_POLL_INTERVAL_SEC = 0.02
_SAMPLE_WINDOW_SEC = 0.15


@VERIFIERS.register("sg_always_pass")
class _AlwaysPass(BaseVerifier):
    """Test double that always reports the condition holding."""

    type: Literal["sg_always_pass"] = "sg_always_pass"
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)


@VERIFIERS.register("sg_flip")
class _FlipThenRestore(BaseVerifier):
    """Fails on sample number ``fail_at`` only, holds on every other sample.

    Models the actual T-024 failure: the safeguard is violated mid-run and
    restored before the run ends, so a check that only samples at the end
    never sees it.
    """

    type: Literal["sg_flip"] = "sg_flip"
    fail_at: int = 2
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        if self.calls == self.fail_at:
            return VerificationResult(
                success=False, elapsed_time=0.0, reason="dropped mid-run", name=self.name
            )
        return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)


@VERIFIERS.register("sg_error")
class _AlwaysErrors(BaseVerifier):
    """Test double that always reports a check-could-not-run error, never a violation."""

    type: Literal["sg_error"] = "sg_error"
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        return VerificationResult(
            success=False,
            status="error",
            elapsed_time=0.0,
            reason="transient kubectl failure",
            name=self.name,
        )


@VERIFIERS.register("sg_raise")
class _RaisingLeaf(BaseVerifier):
    """Test double whose ``verify`` always raises, to prove the monitor survives it."""

    type: Literal["sg_raise"] = "sg_raise"

    def verify(self, timeout_sec: float) -> VerificationResult:
        raise RuntimeError("boom")


@VERIFIERS.register("sg_false_then_true")
class _FalseThenTrue(BaseVerifier):
    """Fails until sample ``true_from``, then holds forever after.

    Models an attain-then-hold row: the state the agent must BRING ABOUT is
    false when the turn starts, becomes true once the agent does the work, and
    must not regress afterwards.
    """

    type: Literal["sg_false_then_true"] = "sg_false_then_true"
    true_from: int = 3
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        if self.calls < self.true_from:
            return VerificationResult(
                success=False, elapsed_time=0.0, reason="not yet attained", name=self.name
            )
        return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)


@VERIFIERS.register("sg_always_fail")
class _AlwaysFail(BaseVerifier):
    """Test double that never holds, for the never-armed case."""

    type: Literal["sg_always_fail"] = "sg_always_fail"
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        return VerificationResult(
            success=False, elapsed_time=0.0, reason="never attained", name=self.name
        )


def _hold_entry(check: dict[str, Any], **extra: Any) -> VerificationEntry:
    payload = {
        "name": "e",
        "role": "safeguard",
        "severity": "catastrophic",
        "mode": "hold",
        "check": check,
    }
    payload.update(extra)
    entries, errors = parse_entries([payload])
    assert errors == []
    return entries[0]


def test_hold_that_holds_throughout_is_not_reported_as_violated() -> None:
    entry = _hold_entry({"type": "sg_always_pass"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is False
    assert obs.error_count == 0
    assert obs.sample_count >= 2


def test_a_violation_restored_before_the_run_ends_still_fails_the_hold_entry() -> None:
    """Regression test for the T-024 replica-floor bug this monitor exists to fix."""
    entry = _hold_entry(
        {"type": "sg_flip", "fail_at": 2}, hold_poll_interval_sec=_POLL_INTERVAL_SEC
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)  # several samples: pass, FAIL, pass, pass, ...
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is True
    assert obs.first_violation_reason == "dropped mid-run"
    assert obs.first_violation_at_sec is not None
    # The condition recovered and later samples kept passing; violated must
    # not be cleared by a later, healthy sample.
    assert obs.sample_count >= 3


def test_a_check_that_errors_repeatedly_is_not_reported_as_a_violation() -> None:
    entry = _hold_entry({"type": "sg_error"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is False
    assert obs.sample_count >= 2
    assert obs.error_count == obs.sample_count


def test_a_leaf_that_raises_does_not_crash_the_monitor_thread() -> None:
    """An unexpected exception inside a sample must not propagate or stop sampling."""
    entry = _hold_entry({"type": "sg_raise"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    # Sampling more than once after the first raise proves the loop survived
    # it rather than dying silently on the first exception.
    assert obs.sample_count >= 2
    assert obs.error_count == obs.sample_count
    assert obs.violated is False


def test_hold_entry_with_zero_samples_does_not_silently_pass() -> None:
    entry = _hold_entry({"type": "sg_always_pass"})

    never_sampled = DefaultEvalHarness._hold_report_entry(entry, None)  # noqa: SLF001
    zero_samples = DefaultEvalHarness._hold_report_entry(  # noqa: SLF001
        entry, HoldObservation()
    )

    for row in (never_sampled, zero_samples):
        assert row["success"] is False
        assert row["status"] == "error"
        assert "never sampled" in row["reason"]
        assert row["hold_sample_count"] == 0


def test_get_observations_returns_a_snapshot_independent_of_further_sampling() -> None:
    entry = _hold_entry({"type": "sg_always_pass"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    snapshot = monitor.get_observations()
    snapshot[entry.name].sample_count = 999
    monitor.stop()

    assert monitor.get_observations()[entry.name].sample_count != 999


def test_start_is_a_no_op_with_no_hold_entries() -> None:
    monitor = SafeguardMonitor([])
    monitor.start()
    monitor.stop()
    assert monitor.get_observations() == {}


def test_sampling_thread_tags_subprocess_log_lines_as_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monitor's own thread must be distinguishable from the agent's in the log.

    Confirms the monitor's scheduling thread carries the ``core.subprocess``
    thread-local tag by asserting the tag observed inside a verify() call made
    from that thread, rather than by parsing log output here.
    """
    from devops_bench.core import subprocess as bench_subprocess

    observed_tags: list[str | None] = []

    @VERIFIERS.register("sg_capture_tag")
    class _CaptureTag(BaseVerifier):
        type: Literal["sg_capture_tag"] = "sg_capture_tag"

        def verify(self, timeout_sec: float) -> VerificationResult:
            observed_tags.append(getattr(bench_subprocess._thread_local, "tag", None))  # noqa: SLF001
            return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)

    entry = _hold_entry({"type": "sg_capture_tag"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    assert observed_tags
    assert all(tag == "sample" for tag in observed_tags)
    assert getattr(bench_subprocess._thread_local, "tag", None) is None  # noqa: SLF001


def test_mode_hold_now_parses_instead_of_raising() -> None:
    """Was rejected outright at the schema level; hold now parses like any other mode."""
    entry = _hold_entry({"type": "sg_always_pass"})
    assert entry.resolved_mode == "hold"


def test_run_one_stops_and_joins_the_safeguard_monitor_when_the_agent_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: an agent exception must not leak the monitor's background thread."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    def _boom(prompt: str, ctx: Any) -> Any:
        raise RuntimeError("agent crashed")

    monkeypatch.setattr(harness, "execute_agent", _boom)
    monkeypatch.setattr(harness, "_run_verification", lambda entries, **kwargs: [])
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {"deployer": "noop"},
            "verification_spec": [
                {
                    "name": "no-scale-down",
                    "role": "safeguard",
                    "severity": "catastrophic",
                    "mode": "hold",
                    "check": {"type": "sg_always_pass"},
                }
            ],
        }
    )

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert record["status"] == "failed"
    assert not any(t.name == "safeguard-monitor" for t in threading.enumerate())


# -- arm_on: first_true (attain-then-hold) ---------------------------------
#
# Motivating failure: the overnight sweep of 2026-08-03/04 (finding F6). Three
# blueprints authored a hold row for a state the AGENT was supposed to bring
# about. The monitor arms before the agent's turn and its violations are
# sticky, so the very first sample recorded a violation that could never be
# cleared, and all three tasks aborted at setup before an agent ever ran.


def test_first_true_ignores_failing_samples_until_the_state_is_attained() -> None:
    entry = _hold_entry(
        {"type": "sg_false_then_true", "true_from": 3},
        hold_poll_interval_sec=_POLL_INTERVAL_SEC,
        arm_on="first_true",
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.armed is True
    assert obs.violated is False, "samples before arming must not count as violations"
    assert obs.pre_arm_sample_count == 2
    assert obs.armed_at_sec is not None


def test_first_true_still_catches_a_regression_after_arming() -> None:
    """Arming must not disable stickiness: once attained, a later drop still fails."""
    entry = _hold_entry(
        {"type": "sg_flip", "fail_at": 2}, hold_poll_interval_sec=_POLL_INTERVAL_SEC,
        arm_on="first_true",
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)  # pass (arms), FAIL, pass, pass...
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.armed is True
    assert obs.violated is True
    assert obs.first_violation_reason == "dropped mid-run"


def test_a_never_attained_first_true_entry_stays_disarmed() -> None:
    entry = _hold_entry(
        {"type": "sg_always_fail"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC,
        arm_on="first_true",
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.armed is False
    assert obs.violated is False, "never attaining is not the same as violating"
    assert obs.pre_arm_sample_count >= 2


def test_a_never_armed_entry_is_reported_as_a_failure_not_an_error() -> None:
    """Never doing the work must not fall out of the denominator as an error."""
    entry = _hold_entry(
        {"type": "sg_always_fail"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC,
        arm_on="first_true",
    )
    obs = HoldObservation(armed=False, sample_count=4, pre_arm_sample_count=4)
    row = DefaultEvalHarness._hold_report_entry(entry, obs)  # noqa: SLF001

    assert row["success"] is False
    assert row["status"] == "fail"
    assert "never armed" in row["reason"]


def test_an_error_sample_does_not_arm_a_disarmed_entry() -> None:
    """An error tells us nothing either way, so it must not stand in for attainment."""
    entry = _hold_entry(
        {"type": "sg_error"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC,
        arm_on="first_true",
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.armed is False
    assert obs.error_count >= 2
    assert obs.pre_arm_sample_count == 0


def test_default_arm_on_is_unchanged_start_behaviour() -> None:
    """Every entry authored before arm_on existed must behave exactly as before."""
    entry = _hold_entry({"type": "sg_always_fail"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    assert entry.arm_on is None
    assert entry.arms_on_first_true is False

    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.armed is True
    assert obs.violated is True, "an arm_on-less entry still violates on the first failing sample"


def test_arm_on_is_rejected_on_a_non_hold_entry() -> None:
    """A silent no-op is how a misconfigured entry hides; reject it by name."""
    _, errors = parse_entries(
        [
            {
                "name": "e",
                "role": "objective",
                "mode": "converge",
                "check": {"type": "sg_always_pass"},
                "arm_on": "first_true",
            }
        ]
    )
    assert errors
    assert any("arm_on is only valid when mode is 'hold'" in e["reason"] for e in errors)
