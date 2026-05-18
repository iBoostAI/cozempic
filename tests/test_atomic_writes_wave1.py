"""Wave 1 — atomic write hardening + concurrency tests.

Covers fixes for the production incidents where:
- _save_sidecar collided on a fixed `.tmp` filename when two guard daemons
  started simultaneously (FileNotFoundError on the loser's os.replace)
- save_messages had the same fixed-tmp bug
- record_savings was not atomic at all (direct write_text)
- cmd_reload/cmd_treat/cmd_strategy bypassed _PruneLock
- SessionStart hook fired guard --daemon twice (unflocked foreground + flocked)
- overflow_watcher thread leaked past normal-exit breaks
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


# ─── atomic_write_text (new shared primitive) ────────────────────────────────

class TestAtomicWriteText(unittest.TestCase):
    def test_basic_write(self):
        from cozempic.helpers import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            atomic_write_text(target, '{"key": "value"}')
            self.assertEqual(target.read_text(), '{"key": "value"}')

    def test_overwrite_existing(self):
        from cozempic.helpers import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            target.write_text("old")
            atomic_write_text(target, "new")
            self.assertEqual(target.read_text(), "new")

    def test_no_tmp_file_left_on_success(self):
        from cozempic.helpers import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            atomic_write_text(target, "data")
            # No .tmp* files should remain
            leftovers = list(Path(tmp).glob(".tmp.*"))
            self.assertEqual(leftovers, [], f"tmp files leaked: {leftovers}")

    def test_concurrent_writes_no_collision(self):
        """20 parallel writes to the SAME target succeed without
        FileNotFoundError (the production bug). Last writer wins; no crashes."""
        from cozempic.helpers import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            errors = []

            def writer(i):
                try:
                    atomic_write_text(target, f'{{"writer": {i}}}')
                except Exception as e:
                    errors.append((i, type(e).__name__, str(e)))

            with ThreadPoolExecutor(max_workers=20) as ex:
                list(ex.map(writer, range(20)))

            self.assertEqual(errors, [], f"Concurrent writes raised: {errors}")
            # Target is valid JSON (last writer wins)
            self.assertIsInstance(json.loads(target.read_text()), dict)
            # No tmp leftovers
            self.assertEqual(list(Path(tmp).glob(".tmp.*")), [])

    def test_failure_cleans_up_tmp(self):
        """If write fails (e.g. disk full), tmp file is unlinked."""
        from cozempic.helpers import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            # Simulate fdopen failure
            with patch("cozempic.helpers.os.fdopen", side_effect=OSError("simulated")):
                with self.assertRaises(OSError):
                    atomic_write_text(target, "data")
            self.assertEqual(list(Path(tmp).glob(".tmp.*")), [])


# ─── _save_sidecar — the production crash ────────────────────────────────────

class TestSaveSidecarConcurrent(unittest.TestCase):
    def test_concurrent_record_session_no_filenotfound(self):
        """The exact production race: 10 threads calling record_session
        simultaneously. Pre-fix produced FileNotFoundError on N-1 of them."""
        from cozempic.session import record_session
        with tempfile.TemporaryDirectory() as tmp:
            # Redirect sidecar to tmp
            with patch("cozempic.session.get_sidecar_path",
                       return_value=Path(tmp) / "cozempic-sessions.json"):
                errors = []

                def writer(i):
                    try:
                        record_session(f"session-{i:04d}", "/tmp/cwd", 1_000_000)
                    except Exception as e:
                        errors.append((i, type(e).__name__, str(e)))

                with ThreadPoolExecutor(max_workers=10) as ex:
                    list(ex.map(writer, range(10)))

                self.assertEqual(errors, [], f"record_session raised: {errors}")
                # All 10 sessions present (the _HostFileLock prevents
                # lost updates even though writes themselves are atomic)
                data = json.loads((Path(tmp) / "cozempic-sessions.json").read_text())
                self.assertEqual(len(data), 10,
                    f"Expected 10 sessions, got {len(data)} — lost updates!")


# ─── record_savings — lost-update protection ─────────────────────────────────

class TestRecordSavingsAtomic(unittest.TestCase):
    def test_concurrent_increments_no_lost_updates(self):
        """50 concurrent record_savings(1000) calls — final total must be 50000.
        Pre-fix used direct write_text with no lock → races lost updates."""
        from cozempic.helpers import record_savings
        with tempfile.TemporaryDirectory() as tmp:
            savings_file = Path(tmp) / ".cozempic_savings.json"
            with patch("cozempic.helpers._SAVINGS_FILE", savings_file):
                # Disable telemetry pings
                with patch.dict(os.environ, {"COZEMPIC_NO_TELEMETRY": "1"}):
                    def writer(_i):
                        record_savings(1000)

                    with ThreadPoolExecutor(max_workers=10) as ex:
                        list(ex.map(writer, range(50)))

                data = json.loads(savings_file.read_text())
                self.assertEqual(data["tokens_saved"], 50000,
                    f"Lost-update race: expected 50000, got {data['tokens_saved']}")
                self.assertEqual(data["prune_count"], 50)


# ─── save_messages — mkstemp protection ──────────────────────────────────────

class TestSaveMessagesMkstemp(unittest.TestCase):
    def test_save_uses_unique_tmp_filename(self):
        """tmp file is NOT path.with_suffix('.tmp') anymore — uses mkstemp."""
        from cozempic.session import save_messages
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text('{"a": 1}\n')
            messages = [(0, {"type": "user", "message": {"content": "hi"}}, 30)]

            # Capture which tmp filenames mkstemp generates
            tmp_names_seen = []
            real_mkstemp = tempfile.mkstemp

            def spy_mkstemp(*args, **kwargs):
                fd, name = real_mkstemp(*args, **kwargs)
                tmp_names_seen.append(name)
                return fd, name

            with patch("tempfile.mkstemp", side_effect=spy_mkstemp):
                save_messages(path, messages, create_backup=False)

            self.assertEqual(len(tmp_names_seen), 1)
            tmp_name = Path(tmp_names_seen[0]).name
            # Must NOT be the old fixed "session.tmp" form
            self.assertNotEqual(tmp_name, "session.tmp",
                "save_messages still uses fixed tmp filename!")
            # Must start with .tmp. prefix
            self.assertTrue(tmp_name.startswith(".tmp."), f"Unexpected tmp name: {tmp_name}")


# ─── _PruneLock + snapshot in cmd_treat / cmd_strategy / cmd_reload ──────────

class TestCmdReloadAcquiresPruneLock(unittest.TestCase):
    """cmd_reload now passes snapshot= to save_messages AND wraps the save
    in _PruneLock. The forensic report flagged this as the data-loss path
    when guard auto-fires concurrently with manual reload."""

    def test_imports_have_prunelock_and_snapshot(self):
        """Smoke test — cli.py imports _PruneLock, PruneConflictError,
        PruneLockError, snapshot_session from session."""
        from cozempic.cli import _PruneLock, PruneLockError, PruneConflictError, snapshot_session
        self.assertTrue(callable(_PruneLock))
        self.assertTrue(callable(snapshot_session))

    def test_cmd_treat_exits_2_when_prune_lock_held(self):
        """The exact race that hit production: cmd_treat --execute while
        guard holds _PruneLock on the same session. Must exit cleanly with
        code 2 and a clear error message — NOT silently overwrite the
        guard's pruned output."""
        import argparse
        from cozempic.cli import cmd_treat, _PruneLock
        from cozempic.session import save_messages as _real_save_messages

        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.jsonl"
            # Real session content — a single valid JSONL line
            session_path.write_text(
                '{"type":"user","message":{"content":"hi"},"uuid":"a","sessionId":"s"}\n'
            )

            # Acquire the prune lock in a background thread and HOLD it
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def hold_lock():
                with _PruneLock(session_path):
                    lock_acquired.set()
                    release_lock.wait(timeout=10)

            holder = threading.Thread(target=hold_lock, daemon=True)
            holder.start()
            try:
                self.assertTrue(lock_acquired.wait(timeout=5),
                    "Background thread failed to acquire _PruneLock")

                # Build the args object cmd_treat expects
                args = argparse.Namespace(
                    session=str(session_path),
                    project=None,
                    rx="standard",
                    execute=True,
                    thinking_mode=None,
                    force=True,  # skip the active-tasks prompt
                )

                # Patch resolve_session to return our test path directly
                with patch("cozempic.cli.resolve_session", return_value=session_path):
                    # cmd_treat must exit(2) when the lock is held
                    with self.assertRaises(SystemExit) as cm:
                        cmd_treat(args)
                    self.assertEqual(cm.exception.code, 2,
                        f"Expected exit code 2 (lock held), got {cm.exception.code}")
            finally:
                release_lock.set()
                holder.join(timeout=5)


class TestHostFileLockWindows(unittest.TestCase):
    """Coverage for the msvcrt branch of _HostFileLock — runs on POSIX too
    by monkey-patching os.name and injecting a fake msvcrt module."""

    def test_windows_branch_uses_msvcrt_locking(self):
        from cozempic.helpers import _HostFileLock
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"

            # Build a fake msvcrt module that records calls
            class FakeMsvcrt:
                LK_LOCK = 1
                LK_UNLCK = 0
                calls = []

                @classmethod
                def locking(cls, fd, mode, nbytes):
                    cls.calls.append((mode, nbytes))

            import sys as _sys
            with patch.object(os, "name", "nt"):
                # Inject fake msvcrt into sys.modules so the lazy `import msvcrt`
                # inside _HostFileLock picks it up.
                with patch.dict(_sys.modules, {"msvcrt": FakeMsvcrt}):
                    with _HostFileLock(target):
                        pass

            # Verify msvcrt.locking was called: once LK_LOCK on enter, once LK_UNLCK on exit
            self.assertEqual(len(FakeMsvcrt.calls), 2,
                f"Expected 2 msvcrt.locking calls (lock + unlock), got {FakeMsvcrt.calls}")
            self.assertEqual(FakeMsvcrt.calls[0], (FakeMsvcrt.LK_LOCK, 1),
                "First call should be LK_LOCK")
            self.assertEqual(FakeMsvcrt.calls[1], (FakeMsvcrt.LK_UNLCK, 1),
                "Second call should be LK_UNLCK")

    def test_windows_branch_no_msvcrt_degrades_gracefully(self):
        """If msvcrt import fails on Windows (shouldn't happen, but be safe),
        the lock degrades to no-op rather than crashing."""
        from cozempic.helpers import _HostFileLock
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"

            import sys as _sys
            with patch.object(os, "name", "nt"):
                # Remove msvcrt from sys.modules and block import
                # by setting it to a module that raises ImportError on access
                with patch.dict(_sys.modules, {"msvcrt": None}):
                    # Should NOT raise — degrades to _fh = None
                    with _HostFileLock(target) as lock:
                        self.assertIsNone(lock._fh,
                            "Expected lock to degrade to no-op when msvcrt unavailable")


# ─── Hook schema bump v5 → v6 ────────────────────────────────────────────────

class TestHookSchemaV6(unittest.TestCase):
    def test_schema_marker_bumped(self):
        from cozempic.init import HOOK_SCHEMA_VERSION
        self.assertEqual(HOOK_SCHEMA_VERSION, "v6")

    def test_no_unflocked_foreground_guard_daemon_call(self):
        """The unflocked foreground `cozempic guard --daemon` call is REMOVED.
        Hook should have exactly 2 occurrences of `cozempic guard --daemon`
        in SessionStart (1 flocked + 1 python3 fallback)."""
        hooks_path = Path(__file__).parent.parent / "src" / "cozempic" / "data" / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        ss_cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        count = ss_cmd.count("cozempic guard --daemon")
        self.assertEqual(count, 2,
            f"Expected 2 'cozempic guard --daemon' occurrences (1 primary + 1 python3 fallback inside flock), got {count}")

    def test_plugin_and_data_hooks_synced(self):
        plugin_path = Path(__file__).parent.parent / "plugin" / "hooks" / "hooks.json"
        data_path = Path(__file__).parent.parent / "src" / "cozempic" / "data" / "hooks.json"
        self.assertEqual(plugin_path.read_text(), data_path.read_text())


# ─── doctor.fix_corrupted_tool_use acquires _PruneLock ───────────────────────

class TestDoctorRespectsPruneLock(unittest.TestCase):
    def test_fix_corrupted_uses_prune_lock_and_reports_skipped(self):
        """fix_corrupted_tool_use must acquire _PruneLock and surface skipped
        sessions to the user. Source-inspection smoke test — behavioral test
        would require a corrupted session fixture which is expensive."""
        from cozempic.doctor import fix_corrupted_tool_use
        import inspect
        src = inspect.getsource(fix_corrupted_tool_use)
        self.assertIn("_PruneLock", src,
            "fix_corrupted_tool_use must use _PruneLock")
        self.assertIn("Skipped", src,
            "fix_corrupted_tool_use must report skipped sessions")

    def test_fix_orphaned_uses_prune_lock_snapshot_and_reports_skipped(self):
        """fix_orphaned_tool_results was the second unprotected save_messages
        site flagged by the architecture review. Must mirror fix_corrupted's
        protection: _PruneLock + snapshot + skip-on-conflict."""
        from cozempic.doctor import fix_orphaned_tool_results
        import inspect
        src = inspect.getsource(fix_orphaned_tool_results)
        self.assertIn("_PruneLock", src,
            "fix_orphaned_tool_results must use _PruneLock")
        self.assertIn("snapshot_session", src,
            "fix_orphaned_tool_results must take a pre-load snapshot")
        self.assertIn("Skipped", src,
            "fix_orphaned_tool_results must report skipped sessions")


class TestMcpTreatSessionPruneLock(unittest.TestCase):
    """The MCP plugin's treat_session tool was the third unprotected
    save_messages site flagged by the architecture review. It exposes the
    same data-loss race to users invoking /cozempic:treat via Claude Code."""

    def test_mcp_treat_session_uses_prune_lock_and_snapshot(self):
        import inspect
        # The plugin lives outside the src/ tree; load it via path
        plugin_path = Path(__file__).parent.parent / "plugin" / "servers" / "cozempic_mcp.py"
        src = plugin_path.read_text()
        # The treat_session function must contain _PruneLock + snapshot_session
        # + the abort messages for both error paths
        self.assertIn("_PruneLock", src,
            "MCP treat_session must use _PruneLock")
        self.assertIn("snapshot_session", src,
            "MCP treat_session must take a pre-load snapshot")
        self.assertIn("PruneLockError", src,
            "MCP treat_session must handle PruneLockError")
        self.assertIn("PruneConflictError", src,
            "MCP treat_session must handle PruneConflictError")


# ─── Overflow watcher cleanup in finally ─────────────────────────────────────

class TestOverflowWatcherCleanup(unittest.TestCase):
    def test_start_guard_has_finally_block_for_watcher(self):
        """The watcher.stop() must be in a finally so it fires on ALL exit
        paths (KeyboardInterrupt + 4 break paths)."""
        import inspect
        from cozempic.guard import start_guard
        src = inspect.getsource(start_guard)
        # Find the finally block near end
        self.assertIn("finally:", src)
        # Inside it, must reference overflow_watcher.stop()
        # Crude check: 'finally' followed by 'overflow_watcher' somewhere after
        finally_idx = src.rfind("finally:")
        tail = src[finally_idx:]
        self.assertIn("overflow_watcher", tail,
            "finally block must reference overflow_watcher")
        self.assertIn(".stop()", tail,
            "finally block must call .stop()")


if __name__ == "__main__":
    unittest.main()
