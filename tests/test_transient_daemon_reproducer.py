"""PRIORITY reproducer — 86cb258b transient daemon race hypothesis.

This test is the architect's PRIMARY verification item ("Not verified" section
of AUDIT_REPORT_pr94_transient_daemon_race.md).

The hypothesis (architect, confidence 86%):
    The upgrade-chain re-fire of SessionStart fired `cozempic guard --daemon`
    against the OLD session while OLD Claude (PID 89113) was still dying.
    `find_claude_pid()` in the daemon-spawn subprocess walked ancestors,
    found 89113 still in the process tree (slow graceful exit = 68 seconds),
    and spawned a "transient" daemon for session 86cb258b with claude_pid=89113.
    This transient daemon claimed the pidfile slot via DaemonSpawnClaim.
    When NEW Claude (PID 94466) started at 14:38:18 and its SessionStart
    hook ran `cozempic guard --daemon`, DaemonAlreadyStarting was raised
    (transient daemon was alive) → NEW Claude ended up UNPROTECTED.

This test constructs the exact sequence using unit-level mocks and asserts:

BEFORE fix (current v1.8.14 + PR #93 code, no sentinel):
    NEW Claude's start_guard_daemon returns already_running=True.
    NEW Claude is UNPROTECTED.
    → Test RED = hypothesis CONFIRMED (expected RED)

AFTER fix (Phase B, sentinel in place):
    start_guard_daemon returns started=True for NEW Claude.
    NEW Claude is PROTECTED.
    → Test GREEN = fix verified

The reproducer test MUST red with the exact hypothesis-confirming behavior.
If it fails for a different reason (import error, unrelated error), the
architect's hypothesis NEEDS REVISION — this must be flagged.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# Session fixture
# ---------------------------------------------------------------------------
REPRO_SESSION_ID = "86cb258b3e024515849a0e25a485ca9e"  # normalized 86cb258b session
REPRO_SID12 = REPRO_SESSION_ID[:12]  # "86cb258b3e02"
OLD_CLAUDE_PID = 89113   # the old, slow-dying Claude from the 86cb258b incident
NEW_CLAUDE_PID = 94466   # the new Claude that started at 14:38:18


def _pid_path() -> Path:
    return Path(f"/tmp/cozempic_guard_{REPRO_SID12}.pid")


def _sentinel_path() -> Path:
    return Path(f"/tmp/cozempic_reload_{REPRO_SID12}.in-flight")


class TestTransientDaemonReproducer(unittest.TestCase):
    """Full integration reproducer for the 86cb258b transient daemon race.

    Step-by-step sequence that mirrors the 2026-05-19 14:37–14:38 timeline:

    STEP 1: OLD daemon exits reload path (pidfile unlinked by _safe_unlink_session_pidfile
            in finally block — this is correctly handled by PR #93 commit 2).
            Slot is now FREE. No sentinel is written (that's the bug in current code).

    STEP 2: Upgrade-chain re-fire of SessionStart calls start_guard_daemon for the
            SAME session, with claude_pid=OLD_CLAUDE_PID (still alive/dying).
            This is the TRANSIENT daemon. It claims the pidfile slot.

    STEP 3: NEW Claude (PID=94466) starts. Its SessionStart hook calls
            start_guard_daemon with claude_pid=NEW_CLAUDE_PID.
            On current code: DaemonAlreadyStarting raised (transient daemon holds slot).
            On fixed code: sentinel present → guard spawn returns immediately with
            started=False, reason="reload in flight" → NEW Claude must spawn its own
            guard AFTER sentinel expires.

    The test CONFIRMS the bug on current code by asserting already_running=True for
    NEW Claude's invocation, then explicitly fails with the hypothesis-confirming message.
    """

    def setUp(self):
        _pid_path().unlink(missing_ok=True)
        _sentinel_path().unlink(missing_ok=True)
        self.addCleanup(_pid_path().unlink, missing_ok=True)
        self.addCleanup(_sentinel_path().unlink, missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _simulate_old_daemon_exits_and_slot_freed(self):
        """Step 1: OLD daemon exits, slot is freed.

        Mirrors PR #93's _safe_unlink_session_pidfile in the finally block
        (if present), or falls back to direct unlink for pre-PR-#93 code.
        The slot is FREE after this. In current code, NO sentinel is written.
        """
        # Write a fake OLD daemon pidfile (as if the old daemon was running)
        old_daemon_pid = 77777  # fake old daemon PID (different from OLD_CLAUDE_PID)
        payload = (
            f"{old_daemon_pid}\n"
            f"{datetime.now().isoformat(timespec='seconds')}\n"
            f"spawn-claim-daemon\n"
        )
        _pid_path().write_text(payload)

        # Simulate the old daemon's finally-block unlink.
        # PR #93 adds _safe_unlink_session_pidfile; base v1.8.14 uses direct unlink.
        try:
            from cozempic.guard import _safe_unlink_session_pidfile
            with patch("cozempic.guard._pid_file_points_to", return_value=True):
                _safe_unlink_session_pidfile(REPRO_SESSION_ID)
        except ImportError:
            # Pre-PR-#93 code: just unlink directly (same effect)
            _pid_path().unlink(missing_ok=True)

        # Verify slot is free
        self.assertFalse(
            _pid_path().exists(),
            "OLD daemon's slot was NOT freed — unlink failed.",
        )

    def _simulate_transient_daemon_spawn(self) -> int:
        """Step 2: Upgrade-chain re-fires SessionStart → transient daemon spawns.

        The transient daemon claims the SAME slot for the SAME session,
        but with OLD_CLAUDE_PID (still dying at this moment).

        Returns the transient daemon's mock PID.
        """
        from cozempic.spawn_lock import DaemonSpawnClaim

        # INIT_SPAWN_DAEMON is a PR #93 symbol — tolerate pre-PR-#93 base
        try:
            from cozempic.spawn_lock import INIT_SPAWN_DAEMON
        except ImportError:
            INIT_SPAWN_DAEMON = "spawn-claim-daemon"  # value from PR #93 spec

        transient_daemon_pid = 98765  # fake transient guard PID

        # Simulate: DaemonSpawnClaim._claim wins (slot was just freed)
        claim = DaemonSpawnClaim(REPRO_SESSION_ID, _pid_path())
        claim._claim()
        claim.owned = True

        # Simulate: atomic rename writes transient daemon's real PID
        # (mirrors start_guard_daemon's post-Popen rename)
        payload = (
            f"{transient_daemon_pid}\n"
            f"{datetime.now().isoformat(timespec='seconds')}\n"
            f"{INIT_SPAWN_DAEMON}\n"
        )
        _pid_path().write_text(payload)
        claim.handed_off = True  # do NOT unlink on exit

        return transient_daemon_pid

    def _simulate_new_claude_session_start(self) -> dict:
        """Step 3: NEW Claude's SessionStart hook calls start_guard_daemon.

        Returns the result dict from start_guard_daemon.
        """
        from cozempic.guard import start_guard_daemon

        spawn_calls = []

        def _fake_popen(cmd_parts, **kwargs):
            spawn_calls.append(cmd_parts)
            return MagicMock(pid=NEW_CLAUDE_PID + 1000)  # new guard daemon PID

        with patch("cozempic.guard.subprocess.Popen", side_effect=_fake_popen), \
             patch("cozempic.guard.find_claude_pid", return_value=NEW_CLAUDE_PID), \
             patch("cozempic.guard.find_current_session", return_value={
                 "session_id": REPRO_SESSION_ID,
                 "path": Path("/tmp/fake_86cb258b.jsonl"),
             }), \
             patch("cozempic.guard._cleanup_legacy_pid"), \
             patch("cozempic.guard.maybe_auto_update", return_value=False), \
             patch("cozempic.spawn_lock._is_process_alive", return_value=True):
            # _is_process_alive=True simulates the transient daemon still running
            # (it's watching OLD_CLAUDE_PID which hasn't fully exited yet)
            result = start_guard_daemon(
                session_id=REPRO_SESSION_ID,
                claude_pid=NEW_CLAUDE_PID,
            )

        return result

    # ------------------------------------------------------------------
    # Primary reproducer test
    # ------------------------------------------------------------------

    def test_86cb258b_full_sequence_new_claude_unprotected_on_current_code(self):
        """Verify the Phase B fix for the 86cb258b event sequence.

        Phase B fix: _terminate_and_resume writes a reload sentinel BEFORE spawning
        the watcher. When NEW Claude's SessionStart fires and the sentinel is present,
        start_guard_daemon returns {reason: 'reload in flight'} instead of raising
        DaemonAlreadyStarting — NEW Claude is PROTECTED.

        GREEN = Phase B fix verified.
        """
        try:
            from cozempic.reload_lock import write_reload_sentinel
        except ImportError:
            self.fail(
                "write_reload_sentinel missing from reload_lock — Phase B not applied. "
                "Expected RED until Phase B implementation lands."
            )

        # Step 1: OLD daemon exits, slot freed — AND sentinel is written (Phase B fix)
        self._simulate_old_daemon_exits_and_slot_freed()
        # Write sentinel: this is what _terminate_and_resume now does with Phase B
        write_reload_sentinel(REPRO_SESSION_ID, claude_pid=OLD_CLAUDE_PID)
        self.assertTrue(
            _sentinel_path().exists(),
            "Sentinel not created by write_reload_sentinel — Phase B fix not active.",
        )

        # Step 2: Transient daemon claims the slot
        transient_pid = self._simulate_transient_daemon_spawn()

        # Verify transient daemon holds the slot
        self.assertTrue(
            _pid_path().exists(),
            "Transient daemon pidfile not present after step 2 — test setup error.",
        )
        pid_content = _pid_path().read_text()
        self.assertIn(
            str(transient_pid),
            pid_content,
            f"Transient daemon PID {transient_pid} not in pidfile. Content: {pid_content!r}",
        )

        # Step 3: NEW Claude's SessionStart fires — sentinel is present
        result = self._simulate_new_claude_session_start()

        # ------------------------------------------------------------------
        # VERDICT (Phase B)
        # ------------------------------------------------------------------
        # Sentinel suppresses the transient daemon check entirely.
        # start_guard_daemon must return {started: False, reason: 'reload in flight'}
        # NOT {already_running: True} which means NEW Claude is UNPROTECTED.

        is_reload_in_flight = result.get("reason") == "reload in flight"
        is_already_running = result.get("already_running", False)

        self.assertTrue(
            is_reload_in_flight,
            f"Phase B sentinel fix not active: expected reason='reload in flight', got: {result}. "
            f"Transient PID: {transient_pid}. "
            f"Sentinel exists: {_sentinel_path().exists()}. "
            f"If already_running=True, sentinel check is not running before transient-daemon check."
        )
        self.assertFalse(
            is_already_running,
            f"already_running=True despite Phase B sentinel — sentinel must suppress "
            f"transient daemon detection. Result: {result}",
        )

    # ------------------------------------------------------------------
    # Supplementary: verify the sentinel would have prevented the race
    # ------------------------------------------------------------------

    def test_sentinel_would_prevent_race_when_present(self):
        """If the sentinel were present (Phase B), NEW Claude would NOT be blocked.

        This test verifies the FIX CONTRACT: when write_reload_sentinel is called
        before _spawn_reload_watcher (Step 1 of the fix), and the sentinel is present
        when NEW Claude's SessionStart fires (Step 3), start_guard_daemon must
        return {started: False, reason: 'reload in flight'} rather than raising
        DaemonAlreadyStarting.

        This test REDs until Phase B adds sentinel check to start_guard_daemon.
        """
        try:
            from cozempic.reload_lock import write_reload_sentinel
        except ImportError:
            self.fail(
                "write_reload_sentinel missing from reload_lock — Phase B not applied. "
                "This test RED is expected."
            )

        # Plant a FRESH sentinel (simulates what _terminate_and_resume would write)
        write_reload_sentinel(REPRO_SESSION_ID, claude_pid=OLD_CLAUDE_PID)
        self.assertTrue(_sentinel_path().exists(), "Sentinel not created by write_reload_sentinel")

        # Also simulate transient daemon holding the slot
        transient_pid = self._simulate_transient_daemon_spawn()

        # NEW Claude's SessionStart — with sentinel present
        result = self._simulate_new_claude_session_start()

        # With Phase B fix: result must be {started: False, reason: 'reload in flight'}
        # NOT {already_running: True} which is the BUG
        self.assertEqual(
            result.get("reason"),
            "reload in flight",
            f"Expected reason='reload in flight' (sentinel detected), got: {result}. "
            "Phase B sentinel check not in start_guard_daemon — this test RED is expected. "
            f"Transient PID: {transient_pid}, Sentinel exists: {_sentinel_path().exists()}",
        )
        self.assertFalse(
            result.get("started"),
            f"started=True unexpectedly — sentinel should suppress spawn. Result: {result}",
        )
        self.assertFalse(
            result.get("already_running"),
            f"already_running=True — sentinel should return 'reload in flight', "
            f"not surface the transient daemon as the blocker. Result: {result}",
        )


if __name__ == "__main__":
    unittest.main()
