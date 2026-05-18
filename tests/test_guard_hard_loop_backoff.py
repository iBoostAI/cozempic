"""Tests for the HARD-threshold back-off + exit-with-diagnostic contract.

Companion to ``test_guard_race_2026_05_18.py::TestR2_*`` (the original RED
test). These tests pin down the back-off curve, the counter-reset on a
successful prune, and the exit-after-threshold path at the unit level so
regressions are caught even if the integration-style R2 test masks them.
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def _stub_session(tmpdir: Path, session_id: str):
    path = tmpdir / "fake_session.jsonl"
    path.write_text('{"type":"user","message":{"content":"hi"}}\n')
    return {"session_id": session_id, "path": path}


class _StopAfterNSleeps(Exception):
    """Sentinel to break out of the guard loop deterministically."""


class _FakeState:
    subagents = []  # type: ignore[var-annotated]
    tasks = []  # type: ignore[var-annotated]
    message_count = 0

    def is_empty(self) -> bool:
        return True


class TestHardLoopBackoffHelper(unittest.TestCase):
    """Pure-function tests for ``_hard_loop_backoff_sleep`` so the curve is
    locked in without depending on the larger guard loop."""

    def test_returns_interval_below_backoff_start(self):
        from cozempic.guard import HARD_LOOP_BACKOFF_START, _hard_loop_backoff_sleep

        for k in range(HARD_LOOP_BACKOFF_START):
            self.assertEqual(_hard_loop_backoff_sleep(k, 30), 30)

    def test_exponential_backoff_curve(self):
        """Curve at the published defaults (start=3, cap=300, interval=30):
        K=3 → 60, K=4 → 120, K=5 → 240, K=6+ → 300 (capped)."""
        from cozempic.guard import _hard_loop_backoff_sleep

        expected = {3: 60, 4: 120, 5: 240, 6: 300, 7: 300, 8: 300, 9: 300}
        for k, want in expected.items():
            self.assertEqual(
                _hard_loop_backoff_sleep(k, 30),
                want,
                f"K={k}: expected {want}s, got {_hard_loop_backoff_sleep(k, 30)}",
            )

    def test_cap_respected_for_unusual_intervals(self):
        from cozempic.guard import (
            HARD_LOOP_BACKOFF_CAP_SECONDS,
            _hard_loop_backoff_sleep,
        )

        # A 5-minute interval would otherwise hit 300 * 8 = 2400 at K=5.
        self.assertEqual(
            _hard_loop_backoff_sleep(5, 300),
            HARD_LOOP_BACKOFF_CAP_SECONDS,
        )


class TestHardLoopExitAndReset(unittest.TestCase):
    """Integration-ish tests driving start_guard with mocked deps."""

    def _run_loop(
        self,
        prune_returns,
        token_estimate=600_000,
        interval=30,
        threshold_tokens=500_000,
    ):
        """Drive start_guard with a sequence of prune results.

        ``prune_returns`` is an iterable of saved_mb floats. After each is
        consumed the loop is forced to stop via ``_StopAfterNSleeps`` from
        time.sleep so the test terminates.
        """
        from cozempic import guard as guard_mod

        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        session = _stub_session(tmpdir, "cafe1234-5678-9abc-def0-2026051811bb")

        sleep_calls: list[float] = []
        # Each cycle does 1 baseline sleep + up to 1 back-off sleep — give
        # generous headroom so the test only stops the loop AFTER the
        # under-test path (exit or counter-reset) has had its chance.
        max_sleeps = (len(prune_returns) * 2) + 4

        def fake_sleep(duration):
            sleep_calls.append(float(duration))
            if len(sleep_calls) >= max_sleeps:
                raise _StopAfterNSleeps()

        prune_iter = iter(prune_returns)

        def fake_prune_cycle(**kwargs):
            try:
                saved = next(prune_iter)
            except StopIteration:
                saved = 0.0
            return {
                "saved_mb": float(saved),
                "original_tokens": 600_000,
                "final_tokens": 600_000 - int(saved * 1000),
                "team_name": None,
                "team_messages": 0,
                "checkpoint_path": None,
                "backup_path": None,
                "reloading": False,
            }

        # H4 fix: patch ``guard_mod.time.sleep`` explicitly rather than
        # replacing the whole ``time`` module on guard_mod with a Mock.
        # The prior full-module mock silently returned MagicMocks for any
        # ``time.*`` attribute the test forgot to back-patch (time.monotonic,
        # time.perf_counter, etc.), so any future code path under test that
        # added another time.* call would fail with a confusing TypeError
        # in arithmetic comparisons. Per-attribute patch leaves the rest of
        # the time module intact and only intercepts the one call we care
        # about. Matches the pattern used in 5 other test files in this
        # repo (search ``patch.object\(.*time.*sleep``).
        with (
            patch.object(guard_mod.time, "sleep", side_effect=fake_sleep),
            patch.object(guard_mod, "_resolve_session_by_id", return_value=session),
            patch.object(guard_mod, "find_current_session", return_value=session),
            patch.object(guard_mod, "find_claude_pid", return_value=None),
            patch.object(guard_mod, "checkpoint_team", return_value=_FakeState()),
            patch.object(guard_mod, "guard_prune_cycle", side_effect=fake_prune_cycle),
            patch.object(
                guard_mod, "quick_token_estimate", return_value=token_estimate
            ),
            patch.object(guard_mod, "load_messages", return_value=[]),
            patch("cozempic.session.record_session"),
            patch.object(guard_mod, "_cleanup_stale_watchers"),
            patch.object(guard_mod, "ping_install_if_new"),
            patch.object(guard_mod, "maybe_auto_update"),
            patch.object(guard_mod, "cleanup_old_backups"),
            patch("cozempic.tokens.detect_context_window", return_value=1_000_000),
        ):
            captured = io.StringIO()
            with patch.object(sys, "stdout", captured):
                try:
                    guard_mod.start_guard(
                        cwd=str(tmpdir),
                        threshold_mb=100.0,
                        soft_threshold_mb=50.0,
                        rx_name="standard",
                        interval=interval,
                        auto_reload=False,
                        reactive=False,
                        threshold_tokens=threshold_tokens,
                        soft_threshold_tokens=250_000,
                        session_id=session["session_id"],
                    )
                    raised = None
                    exit_code = None
                except _StopAfterNSleeps as e:
                    raised = e
                    exit_code = None
                except SystemExit as e:
                    raised = None
                    exit_code = e.code

            return {
                "sleeps": sleep_calls,
                "raised": raised,
                "exit_code": exit_code,
                "stdout": captured.getvalue(),
            }

    def test_exit_after_threshold_cycles(self):
        """10 consecutive 0-byte HARD prunes → sys.exit(0) with diagnostic."""
        from cozempic.guard import HARD_LOOP_EXIT_THRESHOLD

        result = self._run_loop([0.0] * (HARD_LOOP_EXIT_THRESHOLD + 2))
        self.assertEqual(
            result["exit_code"],
            0,
            f"Expected sys.exit(0); got exit_code={result['exit_code']!r}, "
            f"raised={result['raised']!r}",
        )
        self.assertIn(
            "powerless against live-context dominance",
            result["stdout"],
            "Diagnostic message was not printed before exit",
        )

    def test_counter_resets_on_successful_prune(self):
        """A successful prune (>0 saved_mb) resets the consecutive counter,
        so a subsequent burst of 0-byte prunes alone is NOT enough to exit."""

        # 5 zeros, then a successful prune, then 5 more zeros.
        # If the counter reset works, total 0-byte consecutive count tops out
        # at 5 — below exit threshold (10) — so the loop must run to the
        # _StopAfterNSleeps cap, NOT exit cleanly.
        sequence = [0.0] * 5 + [1.5] + [0.0] * 5
        result = self._run_loop(sequence)
        self.assertIsNone(
            result["exit_code"],
            f"Counter did not reset — loop exited at code={result['exit_code']}",
        )
        self.assertIsInstance(result["raised"], _StopAfterNSleeps)


if __name__ == "__main__":
    unittest.main()
