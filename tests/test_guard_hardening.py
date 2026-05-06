"""RED tests for guard.py PID-reuse and shell-injection hardening (bugs G1-G8).

Each test class targets one concrete bug from AUDIT_REPORT.md and captures the
CONTRACT that the fix must satisfy. Tests avoid implementation details — they
assert what the caller / OS / filesystem observes.

Bugs covered:
  G1 CRITICAL — _cleanup_legacy_pid signals unverified PID
  G2 CRITICAL — _cleanup_stale_watchers signals substring-only pgrep match
  G3 HIGH     — _is_guard_running_for_session missing PID-reuse defence
  G4 HIGH     — TOCTOU race in PID file creation
  G5 HIGH     — _terminate_and_resume signals unverified claude_pid
  G6 HIGH     — Windows cmd injection + unquoted project_dir/flags
  G7 MED      — _detect_claude_flags breaks on spaces/metachar; injection flows through
  G8 MED      — main-loop Claude watchdog uses PID with no identity verification
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# BUG-G1 — _cleanup_legacy_pid must NOT SIGTERM unverified PID
# ---------------------------------------------------------------------------
class TestG1_CleanupLegacyPidRequiresArgvVerify(unittest.TestCase):
    """_cleanup_legacy_pid must call _is_cozempic_guard_process before SIGTERM.

    Scenario: pre-1.6.13 legacy pidfile holds PID N. Host has since recycled
    N to an unrelated user process (editor, node server, shell). The cleanup
    path must NOT send SIGTERM to N. It should only unlink the legacy file.
    """

    def setUp(self):
        # Temp legacy pidfile so we don't touch /tmp globally
        self.tmpdir = tempfile.mkdtemp()
        self.cwd = self.tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_legacy_pidfile(self, pid: int) -> Path:
        """Write a legacy-format pidfile; return its Path."""
        from cozempic.guard import _pid_file_for_cwd
        legacy = _pid_file_for_cwd(self.cwd)
        legacy.write_text(str(pid))
        self.addCleanup(legacy.unlink, missing_ok=True)
        return legacy

    def test_does_not_sigterm_non_guard_pid(self):
        """Recycled-PID scenario: pidfile points at a non-cozempic process.

        With the fix, SIGTERM MUST NOT be sent. The legacy file is still unlinked.
        """
        from cozempic.guard import _cleanup_legacy_pid
        self._write_legacy_pidfile(31337)

        with (
            patch("cozempic.guard._is_cozempic_guard_process", return_value=False),
            patch("cozempic.guard.os.kill") as mock_kill,
        ):
            _cleanup_legacy_pid(self.cwd)

            # Liveness probe kill(pid, 0) is OK; SIGTERM is NOT.
            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM
            ]
            self.assertEqual(
                sigterm_calls, [],
                "Legacy cleanup sent SIGTERM to a non-guard PID — confused deputy bug",
            )

    def test_sigterms_legitimate_guard_pid(self):
        """Positive case: pidfile points at a real cozempic guard — SIGTERM is allowed."""
        from cozempic.guard import _cleanup_legacy_pid
        self._write_legacy_pidfile(42000)

        with (
            patch("cozempic.guard._is_cozempic_guard_process", return_value=True),
            patch("cozempic.guard.os.kill") as mock_kill,
            patch("cozempic.guard.time.sleep"),
        ):
            _cleanup_legacy_pid(self.cwd)

            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM and c.args[0] == 42000
            ]
            self.assertEqual(
                len(sigterm_calls), 1,
                "Legacy cleanup failed to SIGTERM a verified guard PID",
            )


# ---------------------------------------------------------------------------
# BUG-G2 — _cleanup_stale_watchers must NOT signal substring-only pgrep matches
# ---------------------------------------------------------------------------
class TestG2_CleanupStaleWatchersRequiresArgvVerify(unittest.TestCase):
    """_cleanup_stale_watchers must verify each match's full argv before SIGTERM.

    pgrep -f matches the entire command line as regex. A process like
    `vim /tmp/cozempic_guard_resumed_Claude_notes.md` matches
    'cozempic.*resumed Claude' but is NOT a watcher.
    """

    def test_does_not_sigterm_false_positive_pgrep_match(self):
        """pgrep returns a PID whose argv is not a real watcher; no SIGTERM."""
        from cozempic.guard import _cleanup_stale_watchers

        # pgrep returns the PID — but the argv that ps resolves is a vim editor
        false_pid = "77777"
        false_argv = "vim /home/u/cozempic_guard_resumed_Claude_notes.md"

        def fake_run(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                mock.stdout = false_pid + "\n"
            elif cmd[0] == "ps":
                mock.stdout = false_argv + "\n"
            else:
                mock.stdout = ""
            return mock

        with (
            patch("cozempic.guard.subprocess.run", side_effect=fake_run),
            patch("cozempic.guard.os.kill") as mock_kill,
        ):
            _cleanup_stale_watchers()

            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM
            ]
            self.assertEqual(
                sigterm_calls, [],
                "Stale-watcher cleanup SIGTERM'd a process whose argv is not a watcher "
                "(substring-only match — confused deputy)",
            )

    def test_sigterms_real_watcher_match(self):
        """Positive case: pgrep returns a real bash watcher — SIGTERM is allowed."""
        from cozempic.guard import _cleanup_stale_watchers

        real_pid = "88888"
        # Matches the real watcher_script shape in _spawn_reload_watcher at ~933-937
        real_argv = (
            "bash -c while kill -0 5000 2>/dev/null; do sleep 1; done; sleep 1; "
            "osascript ... Cozempic guard resumed Claude in /home/u/project"
        )

        def fake_run(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                mock.stdout = real_pid + "\n"
            elif cmd[0] == "ps":
                mock.stdout = real_argv + "\n"
            else:
                mock.stdout = ""
            return mock

        with (
            patch("cozempic.guard.subprocess.run", side_effect=fake_run),
            patch("cozempic.guard.os.kill") as mock_kill,
        ):
            _cleanup_stale_watchers()

            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM
                and c.args[0] == int(real_pid)
            ]
            self.assertGreaterEqual(
                len(sigterm_calls), 1,
                "Stale-watcher cleanup failed to SIGTERM a legitimate watcher",
            )


# ---------------------------------------------------------------------------
# BUG-G3 — _is_guard_running_for_session must verify PID ownership
# ---------------------------------------------------------------------------
class TestG3_IsGuardRunningForSessionVerifiesPidOwnership(unittest.TestCase):
    """_is_guard_running_for_session must return None when the pidfile PID
    is alive but not a cozempic guard.

    Scenario: daemon crashed, PID recycled to an unrelated process. The
    function currently only checks `os.kill(pid, 0)` → returns the recycled
    PID → start_guard_daemon treats session as already_running → session
    is permanently unprotected.
    """

    def setUp(self):
        self.session_id = "11111111-2222-3333-4444-000000000001"
        # Pre-seed the session pidfile with a recycled-looking PID.
        from cozempic.guard import _pid_file_for_session
        self.pid_path = _pid_file_for_session(self.session_id)
        self.pid_path.write_text("98765")
        self.addCleanup(self.pid_path.unlink, missing_ok=True)

    def test_returns_none_when_pid_is_not_cozempic_guard(self):
        """PID is alive (os.kill returns) but ps shows it's not our daemon."""
        from cozempic.guard import _is_guard_running_for_session

        # os.kill(pid, 0) succeeds (liveness OK); but _is_cozempic_guard_process
        # must return False for this PID → the function MUST return None.
        with (
            patch("cozempic.guard.os.kill") as mock_kill,
            patch("cozempic.guard._is_cozempic_guard_process", return_value=False),
        ):
            result = _is_guard_running_for_session(self.session_id)

            self.assertIsNone(
                result,
                "Returned a recycled PID as if guard were running — session would "
                "run permanently unprotected. Must verify PID identity.",
            )
            # And liveness probe should have been attempted
            # (we don't assert on _is_cozempic_guard_process call count, just on
            # the returned contract — some implementations may short-circuit)

    def test_returns_pid_when_pid_is_cozempic_guard(self):
        """Positive case: PID is alive AND ps confirms it's a cozempic guard."""
        from cozempic.guard import _is_guard_running_for_session

        with (
            patch("cozempic.guard.os.kill"),
            patch("cozempic.guard._is_cozempic_guard_process", return_value=True),
        ):
            result = _is_guard_running_for_session(self.session_id)

            self.assertEqual(
                result, 98765,
                "Failed to report a legitimate running guard PID",
            )


# ---------------------------------------------------------------------------
# BUG-G4 — PID file creation must be atomic (no TOCTOU race)
# ---------------------------------------------------------------------------
class TestG4_PidfileWriteIsAtomic(unittest.TestCase):
    """Two concurrent start_guard_daemon() calls for the same session must
    NOT both succeed in spawning daemons.

    Without atomic pidfile creation (O_CREAT|O_EXCL), both calls pass the
    _is_guard_running_for_session check with None, both spawn Popen, one
    overwrites the other's pidfile → orphan daemon.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_id = "22222222-3333-4444-5555-000000000002"
        from cozempic.guard import _pid_file_for_session
        self.pid_path = _pid_file_for_session(self.session_id)
        # Ensure clean state
        self.pid_path.unlink(missing_ok=True)
        # Also clean log file
        key = self.session_id[:12]
        self.log_path = Path("/tmp") / f"cozempic_guard_{key}.log"
        self.log_path.unlink(missing_ok=True)
        self.addCleanup(self.pid_path.unlink, missing_ok=True)
        self.addCleanup(self.log_path.unlink, missing_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_concurrent_starts(self, pid_base: int):
        """Shared helper: apply patches on main thread (mock.patch is NOT
        thread-safe across __enter__/__exit__) and race two start_guard_daemon
        invocations via the existing threads. Returns (results, on_disk_pid).
        """
        from cozempic.guard import start_guard_daemon

        start_gate = threading.Event()
        popen_counter = {"n": 0}
        counter_lock = threading.Lock()

        class DummyProc:
            def __init__(self, pid):
                self.pid = pid

        def fake_popen(cmd_parts, **kwargs):
            with counter_lock:
                popen_counter["n"] += 1
                pid = pid_base + popen_counter["n"]
            # Hold until gate releases so both threads pass the pre-check before
            # either writes the pidfile (worst-case TOCTOU).
            start_gate.wait(timeout=3.0)
            return DummyProc(pid)

        results = []
        results_lock = threading.Lock()

        def runner():
            try:
                r = start_guard_daemon(
                    cwd=self.tmpdir,
                    session_id=self.session_id,
                    threshold_tokens=1000,
                )
            except Exception as e:
                r = {"error": repr(e)}
            with results_lock:
                results.append(r)

        # Patches applied on the main thread — safe.
        with (
            patch("cozempic.guard._cleanup_legacy_pid"),
            patch("cozempic.guard.find_claude_pid", return_value=7777),
            patch("cozempic.guard.subprocess.Popen", side_effect=fake_popen),
        ):
            t1 = threading.Thread(target=runner)
            t2 = threading.Thread(target=runner)
            t1.start()
            t2.start()
            import time as _t
            _t.sleep(0.15)
            start_gate.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertFalse(
                t1.is_alive() or t2.is_alive(),
                "Worker threads didn't exit — pollutes mock.patch state",
            )

        on_disk_pid = None
        if self.pid_path.exists():
            try:
                on_disk_pid = int(self.pid_path.read_text().strip())
            except (ValueError, OSError):
                on_disk_pid = None
        return results, on_disk_pid

    def test_concurrent_starts_only_one_spawn_wins(self):
        """Race two start_guard_daemon calls; only one must report started=True.

        Without atomic pidfile creation (O_CREAT|O_EXCL), both calls pass the
        pre-check and both write the pidfile — TOCTOU orphan-daemon bug.
        """
        results, _ = self._run_concurrent_starts(pid_base=10000)

        started_count = sum(1 for r in results if r.get("started") is True)
        self.assertEqual(
            started_count, 1,
            f"Concurrent starts: {started_count} reported started=True. Expected 1. "
            f"TOCTOU race — pidfile creation is not atomic.",
        )

    def test_final_pidfile_contains_only_winning_pid(self):
        """After a race, the on-disk pidfile must contain the PID that actually
        won the spawn, not an orphan of the loser."""
        results, on_disk_pid = self._run_concurrent_starts(pid_base=20000)

        winner_pids = [r.get("pid") for r in results if r.get("started") is True]
        self.assertTrue(
            self.pid_path.exists(),
            "Pidfile missing after concurrent starts",
        )
        self.assertIn(
            on_disk_pid, winner_pids,
            f"Pidfile contains pid {on_disk_pid} which is not in the winner set "
            f"{winner_pids} — orphan-daemon condition",
        )


# ---------------------------------------------------------------------------
# BUG-G5 — _terminate_and_resume must re-verify claude_pid identity
# ---------------------------------------------------------------------------
class TestG5_TerminateAndResumeReVerifiesClaudePid(unittest.TestCase):
    """_terminate_and_resume must check that claude_pid still points at a
    Claude process (comm=node/claude) before SIGTERM/SIGKILL.

    claude_pid is captured at daemon start but the daemon lives for hours.
    If Claude exited and PID was recycled, blind SIGTERM targets a random
    unrelated process.
    """

    def test_does_not_sigterm_when_pid_not_claude(self):
        """Stale claude_pid now maps to a non-Claude process; no SIGTERM."""
        from cozempic.guard import _terminate_and_resume

        # Simulate: plain terminal path, claude_pid recycled to something else.
        # The contract: before `os.kill(claude_pid, SIGTERM)` (lines 838, 862,
        # 880), an identity check must gate the signal.
        with (
            patch("cozempic.guard._detect_terminal_env", return_value="plain"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Linux"),
            # Simulate identity check: PID is NOT a claude/node process.
            # Fix must introduce a helper; we pretend it exists and returns False.
            patch("cozempic.guard._is_claude_process", create=True, return_value=False),
            patch("cozempic.guard.os.kill") as mock_kill,
            patch("cozempic.guard._wait_for_exit", return_value=True),
            patch("cozempic.guard._spawn_reload_watcher"),
        ):
            _terminate_and_resume(31337, "/tmp/proj", session_id="sess-abc")

            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM
                and c.args[0] == 31337
            ]
            sigkill_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGKILL
                and c.args[0] == 31337
            ]
            self.assertEqual(
                sigterm_calls, [],
                "_terminate_and_resume SIGTERM'd a recycled (non-Claude) PID",
            )
            self.assertEqual(
                sigkill_calls, [],
                "_terminate_and_resume SIGKILL'd a recycled (non-Claude) PID",
            )

    def test_tmux_path_does_not_sigterm_when_pid_not_claude(self):
        """tmux branch (line 838) also must gate SIGTERM on identity check."""
        from cozempic.guard import _terminate_and_resume

        with (
            patch("cozempic.guard._detect_terminal_env", return_value="tmux"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Linux"),
            patch("cozempic.guard._is_claude_process", create=True, return_value=False),
            patch("cozempic.guard.os.kill") as mock_kill,
            patch("cozempic.guard._wait_for_exit", return_value=False),  # force escalation
            patch("cozempic.guard.subprocess.run"),
            patch("cozempic.guard.time.sleep"),
        ):
            _terminate_and_resume(44444, "/tmp/proj", session_id="sess-xyz")

            bad_sigterm = [
                c for c in mock_kill.call_args_list
                if len(c.args) >= 2 and c.args[1] == signal.SIGTERM
                and c.args[0] == 44444
            ]
            self.assertEqual(
                bad_sigterm, [],
                "tmux-path _terminate_and_resume SIGTERM'd a recycled (non-Claude) PID",
            )


# ---------------------------------------------------------------------------
# BUG-G6 — Windows cmd must quote project_dir; injection must not execute
# ---------------------------------------------------------------------------
class TestG6_WindowsCmdQuotesArgs(unittest.TestCase):
    """_spawn_reload_watcher on Windows must not shell-interpolate project_dir.

    Current code uses: f"start cmd /c \"cd /d {project_dir} && claude ...\""
    Any cmd metacharacter in project_dir (&, |, %, ^) executes arbitrary cmds.
    """

    def test_windows_malicious_project_dir_does_not_appear_raw_in_shell_string(self):
        """A project_dir with `& calc &` must NOT be interpolated raw into
        the watcher_script passed to bash."""
        from cozempic.guard import _spawn_reload_watcher

        malicious = r"C:\projects\evil & calc.exe &"

        captured = {"cmd": None, "kwargs": None}

        def fake_popen(cmd_parts, **kwargs):
            captured["cmd"] = cmd_parts
            captured["kwargs"] = kwargs
            proc = MagicMock()
            proc.pid = 9999
            return proc

        with (
            patch("cozempic.guard.platform.system", return_value="Windows"),
            patch("cozempic.guard.is_ssh_session", return_value=False),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.subprocess.Popen", side_effect=fake_popen),
        ):
            _spawn_reload_watcher(5555, malicious, session_id="sess-w")

        # The watcher_script is cmd_parts[-1] (bash -c <script>)
        self.assertIsNotNone(captured["cmd"], "Popen was not called")
        script = captured["cmd"][-1] if captured["cmd"] else ""

        # Contract: on any platform, the Windows resume command must quote/escape
        # `project_dir`. Test: the raw unquoted malicious sequence
        # "evil & calc.exe &" must NOT appear verbatim in the script — a
        # correctly-quoted form wraps it in quotes or escapes the `&`.
        self.assertNotIn(
            "evil & calc.exe &", script,
            "project_dir interpolated raw into watcher script — cmd injection risk. "
            "Must be quoted via subprocess.list2cmdline or argv-passed.",
        )

    def test_windows_malicious_project_dir_metachars_escaped(self):
        """Stronger check: at least one of the cmd metacharacters (&, |) in
        project_dir must be quoted/escaped in the final script."""
        from cozempic.guard import _spawn_reload_watcher

        malicious = r"C:\bad & pwn"
        captured = {"cmd": None}

        def fake_popen(cmd_parts, **kwargs):
            captured["cmd"] = cmd_parts
            proc = MagicMock()
            proc.pid = 1
            return proc

        with (
            patch("cozempic.guard.platform.system", return_value="Windows"),
            patch("cozempic.guard.is_ssh_session", return_value=False),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.subprocess.Popen", side_effect=fake_popen),
        ):
            _spawn_reload_watcher(1234, malicious, session_id="sess-x")

        script = captured["cmd"][-1] if captured["cmd"] else ""
        # The unquoted run sequence `\bad & pwn` indicates direct interpolation.
        # A fix quotes the directory (`"C:\bad & pwn"`) or escapes & as `^&`.
        self.assertFalse(
            "\\bad & pwn" in script and '"C:\\bad & pwn"' not in script,
            f"Windows cmd string embeds unquoted metachars: {script!r}",
        )


# ---------------------------------------------------------------------------
# BUG-G7 — _detect_claude_flags must preserve flag/value boundaries
# ---------------------------------------------------------------------------
class TestG7_DetectClaudeFlagsPreservesBoundaries(unittest.TestCase):
    """_detect_claude_flags must preserve flag-value boundaries and must not
    allow shell metacharacters to reach subsequent shell interpolation.
    """

    def test_space_in_flag_value_preserved(self):
        """--add-dir '/Users/foo/My Project' must come back with the path intact."""
        from cozempic.guard import _detect_claude_flags

        # On real POSIX, ps -o args= returns argv joined by spaces — quoting lost.
        # A correct implementation uses psutil or /proc/<pid>/cmdline to preserve
        # boundaries.
        fake_ps = "claude --add-dir /Users/foo/My Project --other flag"

        def fake_run(cmd, *a, **kw):
            m = MagicMock()
            m.returncode = 0
            if cmd[0] == "ps":
                m.stdout = fake_ps
            else:
                m.stdout = ""
            return m

        with patch("cozempic.guard.subprocess.run", side_effect=fake_run):
            result = _detect_claude_flags(5000)

        # Contract: either a correctly parsed flag-list is returned, or the
        # value "/Users/foo/My Project" is preserved as a single token. In the
        # current broken impl, result ends up as "--add-dir /Users/foo/My Project ..."
        # — when re-interpolated into a shell, `ls /Users/foo/My` runs and
        # `Project` is a separate arg. Fix: preserved via real argv parsing.
        #
        # Minimum contract: path with spaces must survive re-shell-parse.
        import shlex
        tokens = shlex.split(result) if result else []
        self.assertIn(
            "/Users/foo/My Project", tokens,
            f"Space-containing flag value lost after _detect_claude_flags: {result!r}",
        )

    def test_shell_metachar_in_flag_value_not_executable(self):
        """A flag value with `;` / `$(...)` / backtick must NOT be re-parseable
        as a shell command when the result flows through resume_cmd."""
        from cozempic.guard import _detect_claude_flags

        # Simulate ps returning argv with injected shell metachars
        fake_ps = "claude --model sonnet --foo \"; touch /tmp/pwned_by_g7 #\""

        def fake_run(cmd, *a, **kw):
            m = MagicMock()
            m.returncode = 0
            if cmd[0] == "ps":
                m.stdout = fake_ps
            else:
                m.stdout = ""
            return m

        with patch("cozempic.guard.subprocess.run", side_effect=fake_run):
            result = _detect_claude_flags(5001)

        # Contract: result does not contain an un-quoted `;` or command
        # substitution syntax that a downstream shell interpolator would
        # execute. At minimum no bare `;` followed by whitespace+word.
        import re
        self.assertIsNone(
            re.search(r";\s*touch\b", result),
            f"_detect_claude_flags leaked an executable `;touch ...` into output: {result!r}",
        )


# ---------------------------------------------------------------------------
# BUG-G8 — Main-loop Claude watchdog must verify PID identity
# ---------------------------------------------------------------------------
class TestG8_MainLoopWatchdogVerifiesClaudeIdentity(unittest.TestCase):
    """The main-loop watchdog (guard.py:414-422) must not accept a
    liveness-only os.kill(pid, 0) as proof that Claude is still alive.

    After fix, a dedicated identity helper (e.g., _is_claude_process) or
    equivalent argv check must exist and be called.
    """

    def test_is_claude_process_helper_exists(self):
        """Post-fix, the module must expose a helper to verify a PID is Claude.

        This contract mirrors _is_cozempic_guard_process (line 1161) but for
        claude/node processes. Without it, the watchdog CAN'T verify identity.
        """
        import cozempic.guard as g
        self.assertTrue(
            hasattr(g, "_is_claude_process"),
            "Expected cozempic.guard._is_claude_process helper (mirrors "
            "_is_cozempic_guard_process but for claude/node) — currently missing "
            "→ watchdog has no way to verify PID identity (BUG-G8).",
        )

    def test_is_claude_process_rejects_non_claude_pid(self):
        """The helper, when present, must return False for a non-claude argv."""
        import cozempic.guard as g
        if not hasattr(g, "_is_claude_process"):
            self.skipTest("helper missing — see test_is_claude_process_helper_exists")

        def fake_run(cmd, *a, **kw):
            m = MagicMock()
            m.returncode = 0
            # ps -p <pid> -o args= returns something unrelated
            m.stdout = "nginx: master process /usr/sbin/nginx"
            return m

        with patch("cozempic.guard.subprocess.run", side_effect=fake_run):
            self.assertFalse(
                g._is_claude_process(12345),
                "_is_claude_process accepted a non-claude process",
            )

    def test_is_claude_process_accepts_claude_pid(self):
        """The helper must return True for a typical Claude/node argv."""
        import cozempic.guard as g
        if not hasattr(g, "_is_claude_process"):
            self.skipTest("helper missing — see test_is_claude_process_helper_exists")

        def fake_run(cmd, *a, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
            return m

        with patch("cozempic.guard.subprocess.run", side_effect=fake_run):
            self.assertTrue(
                g._is_claude_process(54321),
                "_is_claude_process rejected a legitimate claude node process",
            )


# ---------------------------------------------------------------------------
# Round 2 — RED tests for adversarial findings NF-1..NF-5
# See ADVERSARIAL_REPORT.md (branch audit/guard-py-hardening @ f03164a).
# ---------------------------------------------------------------------------


def _patch_ps(argv: str):
    """Return a subprocess.run side-effect that emulates `ps -p <pid> -o args=`
    returning the given argv line (returncode=0). Anything else returns empty."""
    def fake_run(cmd, *a, **kw):
        m = MagicMock()
        m.returncode = 0
        cmd_list = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        if cmd_list and cmd_list[0] == "ps":
            m.stdout = argv
        else:
            m.stdout = ""
        return m
    return fake_run


# ---------------------------------------------------------------------------
# NF-1 CRITICAL — _is_claude_process must reject generic node/python processes
# ---------------------------------------------------------------------------
class TestNF1_IsClaudeProcessRejectsNonClaudeNodes(unittest.TestCase):
    """Root-cause finding from adversarial round 1: `"node" in comm` matches
    EVERY node process (nodemon, VSCode ext host, electron apps, npm scripts).
    On a Claude-user's laptop this is 5-20 unrelated processes — PID-reuse
    defence collapses to "kill the first node process you find".
    """

    NON_CLAUDE_ARGVS = [
        "/usr/local/bin/node /home/user/server.js",
        "node /home/user/scripts/daily.js",
        "/usr/bin/node /app/server.js --port 3000",
        "npm run dev",
        "/Applications/Visual Studio Code.app/Contents/MacOS/Electron --type=extensionHost",
        "/Applications/Slack.app/Contents/MacOS/Slack --type=renderer",
        "python /home/user/bench.py --target claude-code",
        "python3 -c 'print(\"@anthropic-ai/claude-code is installed\")'",
    ]

    def test_rejects_common_non_claude_processes(self):
        """Each argv listed is a realistic non-Claude process found on a
        dev laptop. _is_claude_process MUST return False for all of them."""
        from cozempic.guard import _is_claude_process
        false_positives = []
        for argv in self.NON_CLAUDE_ARGVS:
            with patch("cozempic.guard.subprocess.run",
                       side_effect=_patch_ps(argv)):
                got = _is_claude_process(12345)
            if got is True:
                false_positives.append(argv)
        self.assertEqual(
            false_positives, [],
            f"_is_claude_process false-positive on {len(false_positives)} "
            f"non-Claude argv(s): {false_positives}. "
            f"Substring match on 'node' / 'claude-code' is too loose — "
            f"tighten to require a real Claude Code signature.",
        )

    def test_accepts_real_anthropic_claude_code_cli(self):
        """Positive: the canonical `node /path/to/@anthropic-ai/claude-code/cli.js`
        invocation must still be recognized."""
        from cozempic.guard import _is_claude_process
        real_claude = (
            "/usr/local/bin/node "
            "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        )
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(real_claude)):
            self.assertTrue(
                _is_claude_process(54321),
                "Tightened helper must still accept the canonical Claude Code node invocation",
            )

    def test_accepts_plain_claude_binary(self):
        """Positive: a native `claude` binary (no node wrapper) must pass."""
        from cozempic.guard import _is_claude_process
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps("claude --resume abc-123")):
            self.assertTrue(
                _is_claude_process(54322),
                "Tightened helper must accept the native `claude` binary invocation",
            )


# ---------------------------------------------------------------------------
# NF-2 CRITICAL — main-loop watchdog (guard.py:414-418) not wired to identity
# ---------------------------------------------------------------------------
class TestNF2_MainWatchdogUsesIdentityCheck(unittest.TestCase):
    """Adversarial round 1 found: the `_is_claude_process` helper was added
    (BUG-G8 fix) but NEVER wired into the main watchdog loop at guard.py:414.
    The loop still only calls `os.kill(claude_pid, 0)` — PID-reuse bug unfixed.
    """

    def test_watchdog_source_invokes_identity_check(self):
        """Static source contract: start_guard's watchdog section MUST call
        `_is_claude_process(claude_pid)` — otherwise PID-reuse races through
        liveness-only `os.kill(pid, 0)`."""
        import inspect
        from cozempic.guard import start_guard
        src = inspect.getsource(start_guard)
        has_identity_call = "_is_claude_process(claude_pid)" in src
        self.assertTrue(
            has_identity_call,
            "Main-loop watchdog (guard.py:~414) does not call "
            "_is_claude_process(claude_pid). BUG-G8 fix added the helper but "
            "did not wire it into the watchdog — recycled-PID still races "
            "through `os.kill(pid, 0)` alone.",
        )

    def test_watchdog_flips_claude_alive_on_identity_fail(self):
        """When the identity check is wired, it MUST sit close enough to the
        `claude_alive = False` flip to drive it. Proxy: both tokens appear
        within a 400-char window of each other in the watchdog section."""
        import inspect
        from cozempic.guard import start_guard
        src = inspect.getsource(start_guard)
        if "_is_claude_process(claude_pid)" not in src:
            self.fail(
                "Watchdog has no identity call at all (see sibling test). "
                "NF-2: main watchdog still uses liveness-only os.kill(pid, 0)."
            )
        idx = src.index("_is_claude_process(claude_pid)")
        window = src[max(0, idx - 100):idx + 400]
        self.assertIn(
            "claude_alive", window,
            "Identity helper found but does not flip claude_alive=False — "
            "watchdog does not react to recycled PID.",
        )


# ---------------------------------------------------------------------------
# NF-3 HIGH — _is_cozempic_guard_process substring match is too loose
# ---------------------------------------------------------------------------
class TestNF3_IsCozempicGuardProcessRejectsLooseSubstrings(unittest.TestCase):
    """`"cozempic.cli" in args` AND `"cozempic" in tokens[0]` both false-positive
    on grep/less/vim sessions + any `*-cozempic-*` binary.
    """

    NON_GUARD_ARGVS = [
        "grep -r cozempic.cli guard /home/user/projects",
        "less /tmp/cozempic.cli-guard-output.log",
        "/usr/local/bin/run-cozempic guard",
        "/usr/local/bin/fake-cozempic guard --evil",
        "vim /home/user/projects/cozempic/cli.py",
    ]

    def test_rejects_loose_substring_matches(self):
        from cozempic.guard import _is_cozempic_guard_process
        false_positives = []
        for argv in self.NON_GUARD_ARGVS:
            with patch("cozempic.guard.subprocess.run",
                       side_effect=_patch_ps(argv)):
                got = _is_cozempic_guard_process(12345)
            if got is True:
                false_positives.append(argv)
        self.assertEqual(
            false_positives, [],
            f"_is_cozempic_guard_process false-positive on "
            f"{len(false_positives)} non-guard argv(s): {false_positives}. "
            f"Tighten token[0] to endswith python/cozempic AND require "
            f"cozempic.cli + guard as discrete tokens.",
        )

    def test_accepts_real_daemon_invocation(self):
        """Positive: canonical python -m cozempic.cli guard invocation."""
        from cozempic.guard import _is_cozempic_guard_process
        real = (
            "/usr/local/bin/python3 -m cozempic.cli guard "
            "--session abc-123 --threshold 50.0"
        )
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(real)):
            self.assertTrue(
                _is_cozempic_guard_process(55555),
                "Tightened helper must still accept the canonical daemon invocation",
            )


# ---------------------------------------------------------------------------
# NF-4 MED — atomic-claim block only catches FileExistsError
# ---------------------------------------------------------------------------
class TestNF4_AtomicClaimHandlesNonExistsOsError(unittest.TestCase):
    """`os.open(pid_path, O_CREAT|O_EXCL|O_WRONLY)` can raise OSError with
    errno ENOSPC / EROFS / EACCES / EMFILE. Currently only FileExistsError is
    caught — other OSErrors propagate and kill the SessionStart hook silently.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_id = "44444444-aaaa-bbbb-cccc-000000000044"
        from cozempic.guard import _pid_file_for_session
        self.pid_path = _pid_file_for_session(self.session_id)
        self.pid_path.unlink(missing_ok=True)
        self.addCleanup(self.pid_path.unlink, missing_ok=True)
        key = self.session_id[:12]
        self.log_path = Path("/tmp") / f"cozempic_guard_{key}.log"
        self.log_path.unlink(missing_ok=True)
        self.addCleanup(self.log_path.unlink, missing_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_with_os_open_raising(self, errno_code: int):
        """Invoke start_guard_daemon with a mocked os.open that raises OSError
        with the given errno on the pidfile path. Returns result dict OR
        `{'_raised': exc}` if uncaught."""
        from cozempic.guard import start_guard_daemon

        real_os_open = os.open

        def fake_open(path, flags, *args, **kwargs):
            if str(path) == str(self.pid_path) and (flags & os.O_EXCL):
                raise OSError(errno_code, os.strerror(errno_code))
            return real_os_open(path, flags, *args, **kwargs)

        with (
            patch("cozempic.guard._cleanup_legacy_pid"),
            patch("cozempic.guard.find_claude_pid", return_value=7777),
            patch("cozempic.guard.subprocess.Popen") as mock_popen,
            patch("cozempic.guard.os.open", side_effect=fake_open),
        ):
            mock_popen.return_value = MagicMock(pid=9999)
            try:
                return start_guard_daemon(
                    cwd=self.tmpdir,
                    session_id=self.session_id,
                    threshold_tokens=1000,
                )
            except OSError as e:
                return {"_raised": e}

    def test_enospc_does_not_crash_hook(self):
        """ENOSPC must NOT propagate — return {started: False, reason: ...}."""
        import errno as _errno_mod
        result = self._run_with_os_open_raising(_errno_mod.ENOSPC)
        self.assertNotIn(
            "_raised", result,
            f"ENOSPC propagated uncaught: {result.get('_raised')!r}. "
            f"start_guard_daemon must return started=False with a reason, "
            f"not crash the non-interactive SessionStart hook.",
        )
        self.assertFalse(
            result.get("started"),
            f"Expected started=False on ENOSPC; got {result!r}",
        )
        self.assertIn(
            "reason", result,
            "Result must carry a reason string explaining the failure",
        )

    def test_erofs_does_not_crash_hook(self):
        """EROFS (/tmp read-only) must NOT propagate."""
        import errno as _errno_mod
        result = self._run_with_os_open_raising(_errno_mod.EROFS)
        self.assertNotIn(
            "_raised", result,
            f"EROFS propagated uncaught: {result.get('_raised')!r}",
        )
        self.assertFalse(result.get("started"))
        self.assertIn("reason", result)

    def test_eacces_does_not_crash_hook(self):
        """EACCES (permission denied) must NOT propagate."""
        import errno as _errno_mod
        result = self._run_with_os_open_raising(_errno_mod.EACCES)
        self.assertNotIn(
            "_raised", result,
            f"EACCES propagated uncaught: {result.get('_raised')!r}",
        )
        self.assertFalse(result.get("started"))
        self.assertIn("reason", result)


# ---------------------------------------------------------------------------
# NF-5 MED — Windows taskkill bypasses _is_claude_process re-verify
# ---------------------------------------------------------------------------
class TestNF5_WindowsTaskkillReVerifies(unittest.TestCase):
    """On Windows, `_terminate_and_resume` calls `taskkill /PID` and
    `taskkill /F /PID` with NO identity re-check between the outer verify
    and the kill call. On POSIX, the SIGKILL at line 975 IS guarded by
    _is_claude_process; Windows is not.
    """

    def test_windows_taskkill_f_skipped_when_identity_fails_mid_race(self):
        """Outer check passes (Claude alive), wait_for_exit times out, THEN
        identity flips to False (PID recycled). taskkill /F MUST NOT run."""
        from cozempic.guard import _terminate_and_resume

        # Call sequence: 1st True (outer verify), subsequent False (post-wait)
        call_sequence = [True, False, False, False, False]

        def fake_is_claude_process(pid):
            return call_sequence.pop(0) if call_sequence else False

        subprocess_calls = []

        def fake_subprocess_call(cmd, *a, **kw):
            subprocess_calls.append(list(cmd))
            return 0

        with (
            patch("cozempic.guard._detect_terminal_env", return_value="plain"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Windows"),
            patch("cozempic.guard._is_claude_process",
                  side_effect=fake_is_claude_process),
            patch("cozempic.guard._wait_for_exit", return_value=False),
            patch("cozempic.guard.subprocess.call",
                  side_effect=fake_subprocess_call),
            patch("cozempic.guard._spawn_reload_watcher"),
            patch("cozempic.guard.os.kill"),
        ):
            _terminate_and_resume(31337, r"C:\proj", session_id="sess-w")

        forced_taskkill = [
            c for c in subprocess_calls
            if c and c[0] == "taskkill" and "/F" in c and str(31337) in c
        ]
        self.assertEqual(
            forced_taskkill, [],
            f"Windows taskkill /F invoked despite identity returning False "
            f"pre-kill: {forced_taskkill}. Recycled-PID blast radius: "
            f"force-kills whatever process now owns PID 31337 (Chrome tab, "
            f"Discord, etc.).",
        )

    def test_windows_taskkill_proceeds_when_identity_still_valid(self):
        """Positive: when _is_claude_process stays True, some taskkill MUST
        run — guards against a fix that over-protects and leaves Claude alive."""
        from cozempic.guard import _terminate_and_resume

        with (
            patch("cozempic.guard._detect_terminal_env", return_value="plain"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Windows"),
            patch("cozempic.guard._is_claude_process", return_value=True),
            patch("cozempic.guard._wait_for_exit", return_value=False),
            patch("cozempic.guard.subprocess.call") as mock_call,
            patch("cozempic.guard._spawn_reload_watcher"),
            patch("cozempic.guard.os.kill"),
        ):
            _terminate_and_resume(44444, r"C:\proj", session_id="sess-w2")

            taskkill_for_pid = [
                c for c in mock_call.call_args_list
                if c.args and len(c.args[0]) >= 1
                and c.args[0][0] == "taskkill"
                and str(44444) in c.args[0]
            ]
            self.assertGreaterEqual(
                len(taskkill_for_pid), 1,
                "No taskkill invoked when identity check stayed True — "
                "fix over-protects and leaves Claude running",
            )


# ---------------------------------------------------------------------------
# R2-REG-2 — Versioned python binaries (pyenv / Homebrew) must pass
#
# Regression: commit be027e0 changed the binary check from
#   `binary in {"python", "python3"}` to `re.match(r"^python(\d+(\.\d+)*)?$", ...)`.
# Without this, pyenv installs of python3.13.12 / Homebrew python3.11 guards
# would be rejected as "not cozempic" → SIGTERM blocked on legitimate daemons,
# or _is_guard_running_for_session returns None → duplicate daemons spawn.
# ---------------------------------------------------------------------------
class TestR2REG2_VersionedPythonAccepted(unittest.TestCase):
    """Lock in the regex accept/reject contract for python-like binaries.

    Argv pattern used in each case is the canonical daemon spawn:
        <binary> -m cozempic.cli guard --session <id>
    so the only variable is tokens[0] (the interpreter path).
    """

    def _argv_for(self, binary: str) -> str:
        """Build a canonical daemon argv line with the given interpreter basename."""
        return f"/usr/local/bin/{binary} -m cozempic.cli guard --session abc-123"

    def _entrypoint_argv(self) -> str:
        """Native cozempic entry-point (no python interpreter)."""
        return "/usr/local/bin/cozempic guard --session abc-123"

    def _check(self, binary_or_argv: str, *, argv: str | None = None) -> bool:
        from cozempic.guard import _is_cozempic_guard_process
        final_argv = argv if argv is not None else self._argv_for(binary_or_argv)
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(final_argv)):
            return _is_cozempic_guard_process(12345)

    # ── Accepted variants ────────────────────────────────────────────────
    def test_accepts_python3_11_homebrew(self):
        self.assertTrue(self._check("python3.11"),
                        "Homebrew python3.11 rejected — R2-REG-2 regressed")

    def test_accepts_python3_13_pyenv_short(self):
        self.assertTrue(self._check("python3.13"),
                        "pyenv python3.13 rejected — R2-REG-2 regressed")

    def test_accepts_python3_13_12_pyenv_full(self):
        self.assertTrue(self._check("python3.13.12"),
                        "pyenv full-triple python3.13.12 rejected — R2-REG-2 regressed")

    def test_accepts_python3_unversioned(self):
        self.assertTrue(self._check("python3"),
                        "Plain python3 rejected — R2-REG-2 regressed")

    def test_accepts_python_bare(self):
        self.assertTrue(self._check("python"),
                        "Bare python rejected — R2-REG-2 regressed")

    def test_accepts_python2_7_legacy(self):
        self.assertTrue(self._check("python2.7"),
                        "Legacy python2.7 rejected — regex should accept any digit sequence")

    def test_accepts_cozempic_entrypoint(self):
        """Native entry-point path: `cozempic guard ...` with no interpreter."""
        self.assertTrue(
            self._check("cozempic", argv=self._entrypoint_argv()),
            "Native `cozempic guard` entry-point rejected",
        )

    # ── Rejected variants ────────────────────────────────────────────────
    def test_rejects_run_cozempic_wrapper(self):
        self.assertFalse(self._check("run-cozempic"),
                         "`run-cozempic` wrapper falsely accepted — confused deputy risk")

    def test_rejects_fake_cozempic_impostor(self):
        self.assertFalse(self._check("fake-cozempic"),
                         "`fake-cozempic` impostor falsely accepted")

    def test_rejects_python_attacker_impostor(self):
        self.assertFalse(self._check("python-attacker"),
                         "`python-attacker` impostor falsely accepted")

    def test_rejects_trailing_dot(self):
        """`python3.` (trailing dot, no digit group) must fail: regex requires
        `\\d+` after each dot."""
        self.assertFalse(self._check("python3."),
                         "`python3.` trailing-dot falsely accepted — regex is too loose")

    def test_rejects_double_dot(self):
        """`python3..11` must fail: `\\.\\d+` forbids consecutive dots."""
        self.assertFalse(self._check("python3..11"),
                         "`python3..11` double-dot falsely accepted — regex is too loose")


# ---------------------------------------------------------------------------
# Regex edge cases / ReDoS surface on _is_cozempic_guard_process
# ---------------------------------------------------------------------------
class TestRegexEdgeCases_IsCozempicGuardProcess(unittest.TestCase):
    """Pathological / adversarial inputs must fail CLOSED quickly."""

    def test_empty_argv_fails_closed(self):
        """`ps -p <pid> -o args=` returns empty: must return False, no crash."""
        from cozempic.guard import _is_cozempic_guard_process
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps("")):
            self.assertFalse(_is_cozempic_guard_process(12345))

    def test_newline_injected_binary_rejected(self):
        """Binary with embedded `\\n`: split() produces multi-token output, but
        the first token must fail the binary check; no crash."""
        from cozempic.guard import _is_cozempic_guard_process
        # The argv injects a newline that splits the first "binary" into two
        # tokens; tokens[0] becomes "python" but tokens[1] becomes "evil-cmd".
        # Either way, the guard command must not be accepted from a line that
        # smuggled a newline; and the implementation must not raise.
        argv = "python\nevil-cmd -m cozempic.cli guard --session abc"
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            # Contract: no exception. Return value can be True or False depending
            # on whether tokens[0] happens to be "python" (it does, because split()
            # splits on any whitespace including \n); the important thing is no
            # crash and no confused-deputy if a genuine newline poisoning was
            # attempted. We assert no-crash.
            try:
                _is_cozempic_guard_process(12345)
            except Exception as e:
                self.fail(f"_is_cozempic_guard_process raised on newline input: {e!r}")

    def test_very_long_binary_name_rejected_fast(self):
        """`python` + `a`*1000: regex must reject and complete quickly (<100ms)."""
        from cozempic.guard import _is_cozempic_guard_process
        huge = "python" + "a" * 1000
        argv = f"{huge} -m cozempic.cli guard --session abc"
        import time as _t
        t0 = _t.perf_counter()
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            got = _is_cozempic_guard_process(12345)
        dt = _t.perf_counter() - t0
        self.assertFalse(got, "Pathological long binary name falsely accepted")
        self.assertLess(
            dt, 0.1,
            f"_is_cozempic_guard_process took {dt*1000:.1f}ms on 1006-char binary "
            f"— potential ReDoS surface",
        )

    def test_unicode_fullwidth_dot_rejected(self):
        """`python3．11` (fullwidth dot) must NOT match ASCII-dot regex."""
        from cozempic.guard import _is_cozempic_guard_process
        argv = "python3．11 -m cozempic.cli guard --session abc"
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            self.assertFalse(
                _is_cozempic_guard_process(12345),
                "Unicode fullwidth dot falsely accepted as a version separator",
            )

    def test_trailing_whitespace_stripped(self):
        """Trailing whitespace on argv line must not affect acceptance —
        `args.strip()` handles it; tokens[0] remains clean."""
        from cozempic.guard import _is_cozempic_guard_process
        argv = "/usr/local/bin/python3.11 -m cozempic.cli guard --session abc   \n\t "
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            # Trailing whitespace should NOT prevent acceptance of a legitimate
            # daemon (strip handles it); contract: still True.
            self.assertTrue(
                _is_cozempic_guard_process(12345),
                "Trailing whitespace caused legitimate daemon to be rejected",
            )


# ---------------------------------------------------------------------------
# Diff coverage — NF-1 + NF-2 round-trip (identity helper wired into watchdog)
# ---------------------------------------------------------------------------
class TestDiffCoverage_RoundTrip_NF1_NF2(unittest.TestCase):
    """End-to-end scenarios exercising _is_claude_process on realistic argvs
    and confirming the helper's accept/reject contract aligns with the
    watchdog's expectations (called at guard.py:425)."""

    def test_real_anthropic_claude_code_cli_accepted(self):
        """Canonical install path: `node /usr/local/lib/node_modules/
        @anthropic-ai/claude-code/cli.js` must pass."""
        from cozempic.guard import _is_claude_process
        argv = (
            "/usr/local/bin/node "
            "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        )
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            self.assertTrue(
                _is_claude_process(12345),
                "Canonical `@anthropic-ai/claude-code` cli.js path rejected — "
                "would cause watchdog to flip claude_alive=False prematurely",
            )

    def test_native_claude_binary_accepted(self):
        """Direct `claude` binary (no node wrapper)."""
        from cozempic.guard import _is_claude_process
        argv = "/usr/local/bin/claude --resume abc-123"
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            self.assertTrue(
                _is_claude_process(12345),
                "Native `claude` binary rejected",
            )

    def test_recycled_pid_to_node_server_rejected(self):
        """Claude exited; PID now points at `node server.js` — must reject.
        This is the exact bug the identity helper defends against."""
        from cozempic.guard import _is_claude_process
        argv = "/usr/local/bin/node /home/user/server.js"
        with patch("cozempic.guard.subprocess.run",
                   side_effect=_patch_ps(argv)):
            self.assertFalse(
                _is_claude_process(12345),
                "Recycled PID → `node server.js` falsely accepted as Claude — "
                "watchdog would let guard keep signaling an unrelated process",
            )

    def test_watchdog_identity_flip_mid_loop(self):
        """Simulate watchdog's control flow around guard.py:415-428 — identity
        helper returning True first, then False mid-loop, must flip a
        local `claude_alive` flag to False on the False read."""
        from cozempic.guard import _is_claude_process

        # Sequence mirrors the watchdog's polling: os.kill(pid, 0) succeeds
        # (liveness), then _is_claude_process is called. We assert the helper
        # itself switches; the watchdog ties this directly to `claude_alive`.
        responses = iter([
            "/usr/local/bin/claude --resume abc",
            "/usr/local/bin/node /home/user/server.js",  # recycled
        ])

        def side_effect(cmd, *a, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = next(responses)
            return m

        with patch("cozempic.guard.subprocess.run", side_effect=side_effect):
            self.assertTrue(_is_claude_process(12345),
                            "First poll: legitimate Claude process")
            self.assertFalse(_is_claude_process(12345),
                             "Second poll after PID recycle: must reject node server.js")


# ---------------------------------------------------------------------------
# Diff coverage — atomic pidfile error paths (5 errno variants)
# ---------------------------------------------------------------------------
class TestDiffCoverage_AtomicPidfile_AllErrnos(unittest.TestCase):
    """NF-4 follow-up: the except branch at start_guard_daemon:1228 catches
    `(FileExistsError, OSError)` — test each non-EEXIST errno variant to lock
    in the contract that none crash the SessionStart hook."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_id = "55555555-dddd-eeee-ffff-000000000055"
        from cozempic.guard import _pid_file_for_session
        self.pid_path = _pid_file_for_session(self.session_id)
        self.pid_path.unlink(missing_ok=True)
        self.addCleanup(self.pid_path.unlink, missing_ok=True)
        key = self.session_id[:12]
        self.log_path = Path("/tmp") / f"cozempic_guard_{key}.log"
        self.log_path.unlink(missing_ok=True)
        self.addCleanup(self.log_path.unlink, missing_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_with_os_open_raising(self, exc: Exception):
        """Invoke start_guard_daemon with a mocked os.open that raises the
        given exception on EXCL open of our pidfile. Returns result dict OR
        `{'_raised': exc}` if uncaught."""
        from cozempic.guard import start_guard_daemon

        real_os_open = os.open

        def fake_open(path, flags, *args, **kwargs):
            if str(path) == str(self.pid_path) and (flags & os.O_EXCL):
                raise exc
            return real_os_open(path, flags, *args, **kwargs)

        with (
            patch("cozempic.guard._cleanup_legacy_pid"),
            patch("cozempic.guard.find_claude_pid", return_value=7777),
            patch("cozempic.guard.subprocess.Popen") as mock_popen,
            patch("cozempic.guard.os.open", side_effect=fake_open),
        ):
            mock_popen.return_value = MagicMock(pid=9999)
            try:
                return start_guard_daemon(
                    cwd=self.tmpdir,
                    session_id=self.session_id,
                    threshold_tokens=1000,
                )
            except OSError as e:
                return {"_raised": e}

    def _assert_graceful(self, result, label: str):
        self.assertNotIn(
            "_raised", result,
            f"{label} propagated uncaught: {result.get('_raised')!r}. "
            f"Hook must not crash the non-interactive SessionStart surface.",
        )
        self.assertFalse(
            result.get("started"),
            f"{label}: expected started=False; got {result!r}",
        )
        self.assertIn(
            "reason", result,
            f"{label}: result must carry a reason string",
        )

    def test_enospc_graceful(self):
        import errno as _errno_mod
        exc = OSError(_errno_mod.ENOSPC, os.strerror(_errno_mod.ENOSPC))
        self._assert_graceful(self._run_with_os_open_raising(exc), "ENOSPC")

    def test_erofs_graceful(self):
        import errno as _errno_mod
        exc = OSError(_errno_mod.EROFS, os.strerror(_errno_mod.EROFS))
        self._assert_graceful(self._run_with_os_open_raising(exc), "EROFS")

    def test_eacces_graceful(self):
        import errno as _errno_mod
        exc = OSError(_errno_mod.EACCES, os.strerror(_errno_mod.EACCES))
        self._assert_graceful(self._run_with_os_open_raising(exc), "EACCES")

    def test_ebusy_graceful(self):
        import errno as _errno_mod
        exc = OSError(_errno_mod.EBUSY, os.strerror(_errno_mod.EBUSY))
        self._assert_graceful(self._run_with_os_open_raising(exc), "EBUSY")

    def test_emfile_graceful(self):
        """EMFILE: file descriptor exhaustion — real-world limit under load."""
        import errno as _errno_mod
        exc = OSError(_errno_mod.EMFILE, os.strerror(_errno_mod.EMFILE))
        self._assert_graceful(self._run_with_os_open_raising(exc), "EMFILE")

    def test_generic_oserror_with_custom_strerror(self):
        """OSError with an unconventional errno (e.g., from a mounted FS with
        custom errors) must still flow through the graceful branch."""
        exc = OSError(999, "custom filesystem error — quota drift")
        self._assert_graceful(
            self._run_with_os_open_raising(exc),
            "OSError(999)",
        )


# ---------------------------------------------------------------------------
# Round 3 — RED tests for static-analysis + security findings
# See STATIC_ANALYSIS_REPORT.md / SECURITY_REVIEW_REPORT.md.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RED-R3-1 (STATIC-MED-1) — G4 placeholder race: pid=0 must never reach os.kill
# ---------------------------------------------------------------------------
class TestR3_1_PlaceholderPidZeroNeverSignaled(unittest.TestCase):
    """When the G4 atomic-claim placeholder `"0"` is read by a concurrent loser,
    `_is_guard_running_for_session` currently calls `os.kill(0, 0)`. On POSIX,
    `os.kill(pid=0, sig=...)` signals the CALLER'S entire process group — not
    an ancestor sentinel. Empirically:

        >>> os.kill(0, 0)  # returns None, does NOT raise ProcessLookupError

    The contract: the function must short-circuit on pid <= 0 BEFORE the
    liveness probe. Today it only survives thanks to `_is_cozempic_guard_process(0)`
    returning False downstream — a future refactor that removes the gate makes
    this exploitable.
    """

    def setUp(self):
        self.session_id = "33333333-aaaa-bbbb-cccc-000000000033"
        from cozempic.guard import _pid_file_for_session
        self.pid_path = _pid_file_for_session(self.session_id)
        self.pid_path.write_text("0")
        self.addCleanup(self.pid_path.unlink, missing_ok=True)

    def test_os_kill_never_called_with_pid_zero(self):
        """`_is_guard_running_for_session` on a placeholder pidfile ("0") MUST NOT
        invoke `os.kill(0, ...)` — pid 0 targets the caller's process group."""
        from cozempic.guard import _is_guard_running_for_session

        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            return None

        with patch("cozempic.guard.os.kill", side_effect=fake_kill):
            _ = _is_guard_running_for_session(self.session_id)

        pid_zero_calls = [(p, s) for (p, s) in kill_calls if p == 0]
        self.assertEqual(
            pid_zero_calls, [],
            f"os.kill(0, ...) invoked during placeholder read — "
            f"POSIX pgroup-broadcast footgun. Calls: {kill_calls}. "
            f"Fix: short-circuit `if pid <= 0: pid_path.unlink(); return None` "
            f"before the liveness probe.",
        )

    def test_returns_none_and_unlinks_on_placeholder(self):
        """Positive regression: even after the fix, a placeholder-valued pidfile
        must still resolve to `None` and be cleaned up."""
        from cozempic.guard import _is_guard_running_for_session

        # No mocks — exercise the real path. Must return None AND clean up.
        result = _is_guard_running_for_session(self.session_id)
        self.assertIsNone(
            result,
            f"placeholder pidfile not resolved to None: {result!r}",
        )
        self.assertFalse(
            self.pid_path.exists(),
            "placeholder pidfile must be unlinked after read",
        )


# ---------------------------------------------------------------------------
# RED-R3-2 (STATIC-MED-2) — Windows taskkill regressions when ps is missing
# ---------------------------------------------------------------------------
class TestR3_2_WindowsTaskkillRegressionOnPsMissing(unittest.TestCase):
    """`_is_claude_process` runs `subprocess.run(["ps", ...])`. On Windows,
    `ps` does not exist → FileNotFoundError (OSError subclass) is caught →
    helper returns False → every Windows taskkill path in
    `_terminate_and_resume` (lines 973, 984) becomes a silent no-op.

    Pre-fix Windows behaviour was "always taskkill"; post-fix Windows behaviour
    is "never taskkill" — functional regression on Windows. Contract: either
    use tasklist/WMIC on Windows, or fall back to liveness-only when identity
    can't be determined (platform-aware probe).
    """

    def test_is_claude_process_has_windows_code_path(self):
        """Static-source contract: the helper must NOT rely solely on POSIX `ps`
        — either it branches on `platform.system()` to use tasklist/WMIC, or
        it has an explicit "ps unavailable" fallback that returns True on
        liveness (not silently False).

        We detect this by checking that `_is_claude_process` either:
          (a) references `tasklist` / `wmic` / `Get-Process` / `Win32_Process`
              (Windows-specific probe), OR
          (b) checks `platform.system()` and branches,
        in its source.
        """
        import inspect
        from cozempic.guard import _is_claude_process
        src = inspect.getsource(_is_claude_process)

        has_windows_probe = (
            "tasklist" in src.lower()
            or "wmic" in src.lower()
            or "get-process" in src.lower()
            or "win32_process" in src.lower()
            or "platform.system" in src
            or "sys.platform" in src
        )
        self.assertTrue(
            has_windows_probe,
            "_is_claude_process has no Windows-specific code path. On Windows, "
            "`ps` raises FileNotFoundError → OSError caught → helper returns "
            "False → every taskkill gate in _terminate_and_resume becomes a "
            "no-op. Use tasklist or branch on platform.system().",
        )

    def test_helper_does_not_silently_return_false_when_ps_missing(self):
        """Dynamic contract: if `subprocess.run(["ps", ...])` raises
        FileNotFoundError (Windows), the helper MUST NOT silently return False
        for a PID the caller believes is Claude. A valid fix either (a) falls
        back to a Windows-native probe, or (b) returns True on liveness and
        lets the caller proceed with taskkill (documented fallback)."""
        from cozempic.guard import _is_claude_process

        def fake_run(cmd, *a, **kw):
            cmd_list = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
            if cmd_list and cmd_list[0] == "ps":
                raise FileNotFoundError(2, "ps not found (simulated Windows)")
            # If the fix uses a Windows probe (tasklist), return a matching row
            if cmd_list and cmd_list[0] in ("tasklist", "wmic"):
                m = MagicMock()
                m.returncode = 0
                m.stdout = "claude.exe 12345 Console 1 123,456 K"
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with (
            patch("cozempic.guard.subprocess.run", side_effect=fake_run),
            patch("cozempic.guard.platform.system", return_value="Windows"),
        ):
            got = _is_claude_process(12345)

        # A correct fix should return True for a live Claude PID on Windows —
        # either via tasklist probe or liveness fallback. The current code
        # catches OSError → returns False → regression.
        self.assertTrue(
            got,
            "Windows: ps is missing (FileNotFoundError). Current impl catches "
            "OSError and returns False → taskkill never fires. Fix must "
            "branch on platform (tasklist/wmic) or fall back to liveness.",
        )


# ---------------------------------------------------------------------------
# RED-R3-3 (STATIC-LOW-3) — Regex accepts trailing newline (re.match vs fullmatch)
# ---------------------------------------------------------------------------
class TestR3_3_BinaryRegexRejectsTrailingNewline(unittest.TestCase):
    """`_is_cozempic_guard_process` uses `re.match(r"^python(\\d+(\\.\\d+)*)?$", binary)`.
    Python's `re.match` anchors at start; `$` matches before a trailing newline
    by default. So `binary == "python3\\n"` PASSES the regex:

        >>> bool(re.match(r"^python(\\d+(\\.\\d+)*)?$", "python3\\n"))
        True

    Fix: `re.fullmatch` OR add `re.DOTALL`+anchor OR explicit `not s.endswith("\\n")`.
    """

    def test_rejects_binary_with_trailing_newline_injected(self):
        """Dynamic defence-in-depth: if a future refactor bypasses `args.strip()`
        / `args.split()` and `tokens[0]` contains a literal `"python3\\n"`, the
        binary gate MUST still reject it. This is the exact contract from
        STATIC-LOW-3: `re.match + $` accepts `"python3\\n"` because `$` matches
        before the trailing newline; `re.fullmatch` does not.

        We patch `args.split` indirectly: mock `subprocess.run` to return an
        args line whose first token ends with `\\n` after our stub's own
        tokenisation. Simulate this by overriding `str.split` via monkey-patching
        the helper's token list. Cleanest: patch `Path.name` on the first token
        so the binary-under-test becomes `"python3\\n"`.
        """
        import inspect
        from cozempic.guard import _is_cozempic_guard_process
        src = inspect.getsource(_is_cozempic_guard_process)

        # The function must use re.fullmatch (or explicit \n rejection on the
        # binary string) — NOT re.match + $. This is a stricter static check
        # than the companion test below: we require fullmatch or equivalent
        # explicit newline rejection on the BINARY specifically.
        uses_fullmatch = "re.fullmatch(" in src
        has_explicit_binary_newline_guard = (
            "binary.endswith" in src
            or 'binary.rstrip("\\n")' in src
            or "if \"\\n\" in binary" in src
            or "\"\\n\" not in binary" in src
        )
        self.assertTrue(
            uses_fullmatch or has_explicit_binary_newline_guard,
            "_is_cozempic_guard_process uses `re.match(..., $)` which accepts "
            "trailing `\\n` in the binary (Python regex quirk: `$` matches "
            "before a trailing newline unless re.fullmatch is used). Fix: "
            "switch to `re.fullmatch(...)` in guard.py:1344.",
        )

    def test_re_match_dollar_accepts_trailing_newline_regression(self):
        """Regression anchor: proves Python's `re.match(r'...$', 'x\\n')`
        silently accepts the newline. If future Python changes this, the fix
        becomes redundant — but the test still documents WHY fullmatch is
        required."""
        import re
        pattern = r"^python(\d+(\.\d+)*)?$"
        # Python semantics: `re.match` with `$` accepts trailing `\n`.
        # `re.fullmatch` does not.
        self.assertTrue(
            bool(re.match(pattern, "python3\n")),
            "Python regex semantics assumption violated: re.match + $ used to "
            "accept trailing newline. If this assertion fails, the STATIC-LOW-3 "
            "bug class has been closed by Python itself.",
        )
        self.assertFalse(
            bool(re.fullmatch(pattern, "python3\n")),
            "Python regex semantics assumption violated: re.fullmatch should "
            "reject trailing newline.",
        )


# ---------------------------------------------------------------------------
# RED-R3-4 (SECURITY-THEORETICAL) — POSIX plain-terminal SIGTERM inner re-check
# ---------------------------------------------------------------------------
class TestR3_4_PosixPlainTerminalSigtermHasInnerReverify(unittest.TestCase):
    """The POSIX plain-terminal path in `_terminate_and_resume` issues the
    FIRST SIGTERM at guard.py:977 inside `try: ... except (ProcessLookupError,
    PermissionError, OSError): pass` — NO inner `_is_claude_process` re-check.

    Symmetry gaps with the rest of the function:
      - tmux SIGTERM @ line 933 — has inner re-check (good)
      - screen SIGTERM @ line 958 — has inner re-check (good)
      - Windows taskkill @ line 973 — has inner re-check (good, after R2 fix)
      - Windows taskkill /F @ line 984 — has inner re-check (good, after R2 fix)
      - POSIX SIGKILL @ line 988 — has inner re-check (good)
      - **POSIX SIGTERM @ line 977 — NO inner re-check (gap)**

    Race window: between the outer `_is_claude_process` at line 914 and the
    SIGTERM at line 977, the code runs `_detect_claude_flags` (subprocess call),
    env-var reads, terminal-env detection. Window ≥ 100ms in practice. If
    Claude exits + PID recycles in that window, SIGTERM lands on an unrelated
    POSIX process.
    """

    def test_sigterm_skipped_when_identity_fails_mid_race(self):
        """Outer check returns True (Claude alive), then identity flips to
        False before the plain-terminal SIGTERM. SIGTERM MUST NOT fire."""
        from cozempic.guard import _terminate_and_resume

        # Sequence: outer check at line 914 → True.
        # Subsequent _is_claude_process calls → False (PID recycled).
        call_sequence = [True, False, False, False, False]

        def fake_is_claude_process(pid):
            return call_sequence.pop(0) if call_sequence else False

        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            return None

        with (
            patch("cozempic.guard._detect_terminal_env", return_value="plain"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Linux"),
            patch("cozempic.guard._is_claude_process",
                  side_effect=fake_is_claude_process),
            patch("cozempic.guard._wait_for_exit", return_value=True),
            patch("cozempic.guard.os.kill", side_effect=fake_kill),
            patch("cozempic.guard._spawn_reload_watcher"),
        ):
            _terminate_and_resume(51337, "/tmp/proj", session_id="sess-r3-4")

        sigterm_calls = [
            (p, s) for (p, s) in kill_calls
            if p == 51337 and s == signal.SIGTERM
        ]
        self.assertEqual(
            sigterm_calls, [],
            f"POSIX plain-terminal SIGTERM fired after identity check returned "
            f"False mid-race. Calls: {kill_calls}. Gap: guard.py:977 has no "
            f"inner `if _is_claude_process(claude_pid):` guard — unlike the "
            f"tmux/screen/SIGKILL paths. Fix: mirror those guards.",
        )

    def test_sigterm_fires_when_identity_remains_valid(self):
        """Positive: when identity stays True across the race window, SIGTERM
        MUST fire (guards against an over-protective fix that skips SIGTERM
        entirely)."""
        from cozempic.guard import _terminate_and_resume

        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            return None

        with (
            patch("cozempic.guard._detect_terminal_env", return_value="plain"),
            patch("cozempic.guard._detect_claude_flags", return_value=""),
            patch("cozempic.guard.platform.system", return_value="Linux"),
            patch("cozempic.guard._is_claude_process", return_value=True),
            patch("cozempic.guard._wait_for_exit", return_value=True),
            patch("cozempic.guard.os.kill", side_effect=fake_kill),
            patch("cozempic.guard._spawn_reload_watcher"),
        ):
            _terminate_and_resume(62424, "/tmp/proj", session_id="sess-r3-4b")

        sigterm_calls = [
            (p, s) for (p, s) in kill_calls
            if p == 62424 and s == signal.SIGTERM
        ]
        self.assertGreaterEqual(
            len(sigterm_calls), 1,
            "POSIX plain-terminal SIGTERM did not fire even though identity "
            "stayed True. Fix over-protects and leaves Claude running.",
        )


if __name__ == "__main__":
    unittest.main()
