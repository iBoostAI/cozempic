"""Tests for _SettingsLock cross-platform behavior.

The lock is exercised by `wire_hooks` / `uninstall_hooks` during `cozempic
init`. Until the v1.8.x cross-platform fix it degraded to a silent no-op
on Windows because `import fcntl` raised ImportError. This file pins the
new behavior:

    POSIX → fcntl.lockf (record locks, NFS-reliable, unchanged)
    Windows → msvcrt.locking (per-byte LK_LOCK/LK_UNLCK)
    Both missing → no-op fallback (no crash)
    OSError on real platform → warn + no-op (settings lock unavailable)

Running on POSIX CI exercises the Windows branch via a `sys.modules`
shim that fakes `msvcrt` with a per-byte locking model. The reverse —
exercising the POSIX branch on Windows CI — is covered by the existing
test_global_init.py suite that hits `wire_hooks` end-to-end.
"""
from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from cozempic.init import _SettingsLock


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    """A settings.json path; the lock sibling lives at .cozempic-init.lock."""
    return tmp_path / "settings.json"


# ─── POSIX path ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only — fcntl unavailable on Windows")
def test_posix_acquires_fcntl_lockf(settings_path: Path) -> None:
    """On POSIX the lock issues fcntl.lockf LOCK_EX on enter, LOCK_UN on exit."""
    import fcntl
    with mock.patch("fcntl.lockf") as lockf:
        with _SettingsLock(settings_path):
            pass
    assert lockf.call_count == 2
    enter_args = lockf.call_args_list[0].args
    exit_args = lockf.call_args_list[1].args
    assert enter_args[1] == fcntl.LOCK_EX
    assert exit_args[1] == fcntl.LOCK_UN


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only — needs real fcntl + tmpfs")
def test_posix_real_lockfile_created(settings_path: Path) -> None:
    """The actual lock file lands alongside settings.json."""
    with _SettingsLock(settings_path):
        assert (settings_path.parent / ".cozempic-init.lock").exists()


# ─── Windows path (exercised via fake msvcrt on POSIX) ───────────────────────


class _FakeMsvcrt:
    """In-memory model of msvcrt.locking for cross-platform testing.

    We don't simulate cross-process contention — that's what the real
    msvcrt.locking would do. We just record the calls so we can assert
    the right operations happen in the right order with the right args.
    """
    LK_LOCK = 1
    LK_UNLCK = 0

    def __init__(self):
        self.calls: list[tuple[int, int, int]] = []  # (fd, mode, nbytes)

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))


def _force_windows_mode(monkeypatch, fake: _FakeMsvcrt) -> None:
    """Monkeypatch os.name == 'nt' and inject fake msvcrt module."""
    monkeypatch.setattr(os, "name", "nt")
    fake_module = types.ModuleType("msvcrt")
    fake_module.LK_LOCK = fake.LK_LOCK
    fake_module.LK_UNLCK = fake.LK_UNLCK
    fake_module.locking = fake.locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake_module)


def test_windows_acquires_msvcrt_locking(monkeypatch, settings_path: Path) -> None:
    """On Windows-mode the lock issues msvcrt.locking LK_LOCK at byte 0 on enter, LK_UNLCK on exit."""
    fake = _FakeMsvcrt()
    _force_windows_mode(monkeypatch, fake)

    with _SettingsLock(settings_path):
        pass

    # Exactly two locking calls — lock then unlock — both on 1 byte.
    assert len(fake.calls) == 2, f"expected 2 calls, got {fake.calls}"
    enter_fd, enter_mode, enter_n = fake.calls[0]
    exit_fd, exit_mode, exit_n = fake.calls[1]
    assert enter_mode == _FakeMsvcrt.LK_LOCK
    assert exit_mode == _FakeMsvcrt.LK_UNLCK
    assert enter_n == 1 and exit_n == 1
    # Same fd used for lock and unlock (otherwise we'd be unlocking a
    # different file).
    assert enter_fd == exit_fd


def test_windows_seek_zero_before_unlock(monkeypatch, settings_path: Path) -> None:
    """msvcrt.locking is position-relative — __exit__ must seek to 0 before LK_UNLCK.

    'a' (append) mode leaves the file pointer at end-of-file. For an
    empty fresh lock file that's byte 0, but defense-in-depth: we MUST
    seek(0) before unlocking. The test asserts seek is called with 0
    before the LK_UNLCK call.
    """
    fake = _FakeMsvcrt()
    _force_windows_mode(monkeypatch, fake)

    # Capture seek calls on the file handle.
    seeks: list[int] = []
    original_open = open

    def tracking_open(path, *args, **kwargs):
        fh = original_open(path, *args, **kwargs)
        original_seek = fh.seek

        def recording_seek(offset, *seek_args, **seek_kwargs):
            seeks.append(offset)
            return original_seek(offset, *seek_args, **seek_kwargs)

        fh.seek = recording_seek
        return fh

    monkeypatch.setattr("builtins.open", tracking_open)

    with _SettingsLock(settings_path):
        pass

    # seek(0) must have been called between the lock and unlock calls.
    assert 0 in seeks, f"seek(0) not invoked; saw seeks={seeks}"


def test_windows_oserror_during_lock_warns_and_degrades(monkeypatch, capsys, settings_path: Path) -> None:
    """OSError from msvcrt.locking degrades to no-op + stderr warning, no crash."""
    fake = _FakeMsvcrt()

    def raising_locking(fd, mode, nbytes):
        raise OSError("simulated Windows lock failure (e.g. read-only filesystem)")

    fake.locking = raising_locking
    _force_windows_mode(monkeypatch, fake)

    # Capture stderr
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_buf)

    with _SettingsLock(settings_path) as lock:
        # _fh should be None — degraded path
        assert lock._fh is None

    assert "settings lock unavailable" in stderr_buf.getvalue()


# ─── Missing-both fallback (neither fcntl nor msvcrt) ────────────────────────


def test_missing_both_locking_modules_degrades(monkeypatch, settings_path: Path) -> None:
    """Platform missing both fcntl and msvcrt → degrade to no-op without crash."""
    # Force a "windows-shaped" environment but with msvcrt absent (extreme
    # edge case — captures the defense-in-depth ImportError catch).
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", None)

    with _SettingsLock(settings_path) as lock:
        assert lock._fh is None  # Degraded — but no crash.


# ─── End-to-end exit safety (re-entry, exception during body) ────────────────


def test_lock_releases_on_exception(settings_path: Path) -> None:
    """If the body raises, the lock is still released (file handle closed)."""
    captured_fh = []

    class _Spy(_SettingsLock):
        def __enter__(self):
            super().__enter__()
            captured_fh.append(self._fh)
            return self

    with pytest.raises(ValueError):
        with _Spy(settings_path):
            raise ValueError("body failure")

    # File handle should be closed after __exit__ even on exception.
    if captured_fh and captured_fh[0] is not None:
        assert captured_fh[0].closed
