"""Shell-level idempotency test for the SessionStart hook command.

Fires the canonical SessionStart hook command twice concurrently with the
same SESSION_ID and counts how many `cozempic guard --daemon ...` spawns
actually happen. The contract introduced in hook-schema v7 is: at most ONE
spawn per session, regardless of how many concurrent SessionStart hook fires
the harness produces (resume + auto-compact + clear can all race).

Validation strategy: replace the real `cozempic` binary on $PATH with a
counter stub that logs every invocation to a file, then drive the hook
command twice in parallel. After both finish, assert the stub recorded
`guard --daemon` AT MOST ONCE for the test session.

This is the test the 2026-05-18 crash report asked for explicitly:

  > Validate: write a tiny shell test that fires the hook command twice
  > concurrently with the same SESSION_ID and counts the spawned daemons
  > — must be <= 1.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "src" / "cozempic" / "data" / "hooks.json"


def _load_session_start_command() -> str:
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    return hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]


class TestHookSpawnIdempotency(unittest.TestCase):
    """Drive the real SessionStart command twice concurrently with the same
    SESSION_ID and verify only one `guard --daemon` spawn lands.

    Uses a stub `cozempic` binary that:
      - prints a fixed version on `--version` so the upgrade short-circuits
      - records every invocation (with all args) to a log file
      - on `guard --daemon`, writes a PID file the way the real binary would
        (so a second concurrent run sees the fast-path)
      - exits 0 immediately (no actual daemon backgrounding)

    The test isolates everything in a tmpdir; nothing touches real /tmp
    PID files or the user's installed cozempic.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cozempic_hook_idem_"))
        # A real-looking lowercase UUID. The 12-char prefix the PID-file uses
        # is deterministic; we use one we can assert against.
        self.session_id = "abcdef01-2345-4678-9abc-deadbeefcafe"
        self.sid12 = self.session_id[:12]  # "abcdef01-234"
        # Per-test isolated TMPDIR so /tmp/cozempic_guard_* paths created
        # by the hook go into this dir, not the real /tmp.
        self.fake_tmp = self.tmpdir / "tmp"
        self.fake_tmp.mkdir()
        # We *cannot* redirect the hook's hard-coded "/tmp/cozempic_guard_..."
        # path via $TMPDIR (the hook string is literal "/tmp/..."). So we use
        # a unique session_id whose first-12 chars are unlikely to collide
        # with any real session on this host, and we clean up after.
        self.pid_file = Path(f"/tmp/cozempic_guard_{self.sid12}.pid")
        self.hook_lock = Path(f"/tmp/cozempic_hook_{self.sid12}.lock")
        for p in (self.pid_file, self.hook_lock):
            if p.exists():
                p.unlink()

        self.invocation_log = self.tmpdir / "invocations.log"
        self.stub_bin_dir = self.tmpdir / "bin"
        self.stub_bin_dir.mkdir()
        self._install_stub_cozempic()

    def tearDown(self):
        self._kill_pid_file_daemon()
        for p in (self.pid_file, self.hook_lock):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install_stub_cozempic(self):
        """Drop a fake `cozempic` executable in self.stub_bin_dir.

        Records every call's argv into invocation_log. On `guard --daemon`
        and `guard --reload-self`, spawns a long-lived sleeper subprocess
        and writes ITS pid to the PID file — mimicking the real daemon
        whose PID file points to a process that stays alive. Without this,
        the fast-path's `kill -0` would always see the stub's own pid
        (already exited by then) and decide the daemon is dead.

        Also stubs `uv` and `pip` so the upgrade chain is a no-op (otherwise
        the chain would attempt a real pip install of cozempic, polluting
        the host environment and slowing the test).
        """
        stub_path = self.stub_bin_dir / "cozempic"
        stub_path.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Log every invocation with PID + args. Append is atomic for
            # short writes on POSIX so concurrent stub calls don't garble.
            printf '%s\\n' "$$ $*" >> {self.invocation_log!s}
            case "$1" in
              --version)
                echo "cozempic 99.0.0"
                ;;
              guard)
                if [[ "$2" == "--daemon" || "$2" == "--reload-self" ]]; then
                  # Spawn a long-lived sleeper so the PID file points to a
                  # process that's still alive when the next hook fires.
                  # Redirect stdio so the sleep survives parent exit; nohup
                  # so it isn't reaped by SIGHUP when the calling subshell
                  # closes. macOS lacks setsid in /usr/bin, so don't use it.
                  nohup sleep 30 </dev/null >/dev/null 2>&1 &
                  printf '%s' "$!" > {self.pid_file!s}
                fi
                ;;
              *)
                : # checkpoint/remind/digest/etc: no-op
                ;;
            esac
            exit 0
        """))
        stub_path.chmod(
            stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )

        # Neutralize upgrade chain: stub `uv` and `pip` to no-ops so the
        # SessionStart hook's `uv pip install --upgrade cozempic ...` and
        # `pip install --upgrade cozempic ...` fallback don't actually touch
        # the host Python. Both succeed silently → the PRE/POST version
        # check sees PRE == POST (both "99.0.0") → reload-self is skipped.
        for name in ("uv", "pip"):
            stub = self.stub_bin_dir / name
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _kill_pid_file_daemon(self):
        """Reap the sleeper subprocess that backs the PID file, if any."""
        if not self.pid_file.exists():
            return
        try:
            pid = int(self.pid_file.read_text().strip())
            os.kill(pid, 9)
        except (ValueError, ProcessLookupError, OSError):
            pass

    def _run_hook_once(self, hook_cmd: str) -> subprocess.CompletedProcess:
        """Pipe a synthetic SessionStart payload into the hook command."""
        env = os.environ.copy()
        # Put the stub bin FIRST on PATH so `cozempic` resolves to it.
        env["PATH"] = f"{self.stub_bin_dir}:{env.get('PATH', '')}"
        # Prevent the hook from calling out to a real Python `cozempic` install
        # via the python3 -m fallback. The python3 fallback runs only when
        # the bare `cozempic` call fails; our stub always succeeds, so the
        # fallback is unreachable. But disabling the cozempic package
        # discovery in PYTHONPATH belt-and-suspenders for hermeticity.
        env["PYTHONPATH"] = ""
        payload = json.dumps(
            {
                "session_id": self.session_id,
                "transcript_path": f"/tmp/{self.session_id}.jsonl",
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        )
        return subprocess.run(
            ["bash", "-c", hook_cmd],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def _count_daemon_spawns(self) -> int:
        if not self.invocation_log.exists():
            return 0
        spawns = 0
        for line in self.invocation_log.read_text().splitlines():
            # Format: "<pid> <args...>"
            args = line.split(None, 1)[1] if " " in line else ""
            if args.startswith("guard --daemon"):
                spawns += 1
        return spawns

    def _wait_for_background_subshells(self):
        """The hook backgrounds the upgrade+daemon subshell with `&`. Give it
        time to complete + write its PID file before we count."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if self.invocation_log.exists():
                # Wait for the log to stop growing (proxy for subshells done)
                size = self.invocation_log.stat().st_size
                time.sleep(0.5)
                if self.invocation_log.stat().st_size == size:
                    return

    def test_single_invocation_spawns_one_daemon(self):
        """Baseline: one hook fire → one `guard --daemon` spawn."""
        cmd = _load_session_start_command()
        result = self._run_hook_once(cmd)
        self.assertEqual(
            result.returncode, 0, f"hook exited non-zero. stderr={result.stderr!r}"
        )
        self._wait_for_background_subshells()
        spawns = self._count_daemon_spawns()
        self.assertEqual(
            spawns,
            1,
            f"Expected exactly 1 daemon spawn on cold start, got {spawns}. "
            f"Log: {self.invocation_log.read_text() if self.invocation_log.exists() else '(empty)'}",
        )

    def test_concurrent_fires_spawn_at_most_one(self):
        """The 2026-05-18 crash signature: two SessionStart hooks fire 2 ms
        apart for the same SESSION_ID. v7 contract: net spawn count <= 1."""
        cmd = _load_session_start_command()
        env = os.environ.copy()
        env["PATH"] = f"{self.stub_bin_dir}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = ""
        payload = json.dumps(
            {
                "session_id": self.session_id,
                "transcript_path": f"/tmp/{self.session_id}.jsonl",
                "hook_event_name": "SessionStart",
                "source": "resume",
            }
        )
        # Fire two processes nearly simultaneously. We start both then wait
        # for both to return; the hook backgrounds its subshell so the parent
        # bash exits quickly (~50 ms).
        p1 = subprocess.Popen(
            ["bash", "-c", cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        p2 = subprocess.Popen(
            ["bash", "-c", cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        p1.communicate(payload, timeout=30)
        p2.communicate(payload, timeout=30)
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self._wait_for_background_subshells()
        spawns = self._count_daemon_spawns()
        self.assertLessEqual(
            spawns,
            1,
            f"Concurrent SessionStart fires spawned {spawns} daemons for "
            f"session {self.session_id} (expected <=1). "
            f"This is the bug from /tmp/cozempic_guard_c492ae5e-971.log "
            f"where two `--- Guard daemon started ---` lines appeared 2 ms apart. "
            f"Log: {self.invocation_log.read_text() if self.invocation_log.exists() else '(empty)'}",
        )

    def test_fast_path_skips_spawn_when_pid_file_live(self):
        """If the guard PID file already points to a live process for THIS
        session, the hook MUST NOT spawn another daemon. This is the
        narrow contract: an external `guard --daemon` (or a survivor from
        a prior reload-self) leaves a healthy PID; the next SessionStart
        respects it.

        Plants a long-lived sleeper as the "existing daemon" rather than
        the pytest PID. Using pytest's own PID risks the subprocess.run
        hanging on pipe-inheritance (the backgrounded hook subshell sees
        pytest as its parent group leader and keeps stdio descriptors open
        until pytest itself exits — a classic POSIX subprocess gotcha).
        A standalone sleeper is reaped in tearDown so it doesn't leak.
        """
        # Spawn an actual sleeper to back the PID file. setsid would be ideal
        # but macOS lacks /usr/bin/setsid, so use Popen with start_new_session.
        sleeper = subprocess.Popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self.pid_file.write_text(str(sleeper.pid))
            cmd = _load_session_start_command()
            result = self._run_hook_once(cmd)
            self.assertEqual(result.returncode, 0)
            self._wait_for_background_subshells()
            spawns = self._count_daemon_spawns()
            self.assertEqual(
                spawns,
                0,
                f"Expected 0 daemon spawns when PID file points to live process, "
                f"got {spawns}. The fast-path is broken — every SessionStart will "
                f"redundantly respawn the daemon, defeating the v7 idempotency "
                f"contract. Log: {self.invocation_log.read_text() if self.invocation_log.exists() else '(empty)'}",
            )
        finally:
            sleeper.terminate()
            try:
                sleeper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sleeper.kill()
                sleeper.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
