"""Windows-compatibility paths in guard.py (PR #91, re-applied on top of #93).

The repo has no Windows CI, so these mock ``os.name`` to exercise the nt-only
branches on a POSIX test host. They cover:

  - ``_guard_tmp_root`` — POSIX keeps ``/tmp`` (hook consistency); Windows uses
    the platform tempdir (no ``/tmp`` on Windows).
  - the ``except OSError`` branch in ``_is_guard_running_for_session`` —
    Windows ``os.kill(pid, 0)`` raises a bare ``OSError`` [WinError 87] for a
    non-existent PID instead of ``ProcessLookupError``; we treat it as dead on
    Windows and re-raise on POSIX.

Note: the Popen ``creationflags`` branch is intentionally not unit-tested here
because ``subprocess.DETACHED_PROCESS`` does not exist on POSIX, so forcing
that branch on a POSIX host would raise AttributeError unrelated to the logic.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic import guard


class TestGuardTmpRoot(unittest.TestCase):
    def test_posix_uses_slash_tmp(self):
        with patch.object(guard.os, "name", "posix"):
            self.assertEqual(guard._guard_tmp_root(), Path("/tmp"))

    def test_windows_uses_platform_tempdir(self):
        with patch.object(guard.os, "name", "nt"):
            self.assertEqual(guard._guard_tmp_root(), Path(tempfile.gettempdir()))

    def test_pid_file_for_session_posix_stays_in_tmp(self):
        # SessionStart shell hook hardcodes /tmp; Python must agree on POSIX so
        # the "guard already running" fast-path doesn't always miss on macOS.
        with patch.object(guard.os, "name", "posix"):
            p = guard._pid_file_for_session("abcdef012345")
            self.assertTrue(str(p).startswith("/tmp/cozempic_guard_"))


class TestIsGuardRunningWindowsOSError(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pid_path = Path(self._tmp.name) / "cozempic_guard_test.pid"
        self.pid_path.write_text("424242")  # plausible, > 0 pid

    def test_windows_oserror_treated_as_dead(self):
        # WinError 87 surfaces as a bare OSError (errno EINVAL), not
        # ProcessLookupError — must be treated as "no daemon", returning None.
        with patch.object(guard, "_pid_file_for_session", return_value=self.pid_path), \
             patch.object(guard.os, "kill", side_effect=OSError(22, "Invalid argument")), \
             patch.object(guard.os, "name", "nt"):
            self.assertIsNone(guard._is_guard_running_for_session("abcdef012345"))

    def test_posix_oserror_reraises(self):
        # On POSIX a bare OSError from os.kill is unexpected and must not be
        # silently masked — it should propagate.
        with patch.object(guard, "_pid_file_for_session", return_value=self.pid_path), \
             patch.object(guard.os, "kill", side_effect=OSError(22, "Invalid argument")), \
             patch.object(guard.os, "name", "posix"):
            with self.assertRaises(OSError):
                guard._is_guard_running_for_session("abcdef012345")


if __name__ == "__main__":
    unittest.main()
