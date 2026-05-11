# guard.py Audit Report — v1.8.7

Scope: `src/cozempic/guard.py` @ branch `audit/guard-py-hardening` (worktree `e-guard-audit`), 1346 LOC.
Methodology: full read + cross-file trace of helpers (`session.py`, `helpers.py`) + git log review to skip already-fixed regressions + existing `tests/test_guard_robustness.py` coverage check.

---

## Confirmed bugs

Severity scale: CRITICAL (data loss / kill-wrong-process / security), HIGH (operational regression, silent unprotected session), MED (resource leak, resilience gap), LOW (edge-case hygiene).

Existing robustness test (`tests/test_guard_robustness.py`) covers: SIGTERM constant, backup-cleanup importability, `reload_self_daemon` short-circuit when no daemon, `start_guard_daemon` passes `--claude-pid`. NONE of the bugs below are covered.

---

### BUG-G1 — `_cleanup_legacy_pid` SIGTERMs an unverified PID (confused deputy)

**Severity**: CRITICAL
**Location**: `guard.py:962-977`

```
962  def _cleanup_legacy_pid(cwd: str) -> None:
...
967          pid = int(legacy.read_text().strip())
968          os.kill(pid, 0)
969          # Still alive — kill it so session-scoped guard can take over
970          os.kill(pid, signal.SIGTERM)
```

**Issue**: The function reads a PID from a pre-1.6.13 legacy pid file keyed by CWD hash, confirms the process is alive with `os.kill(pid, 0)`, then SIGTERMs it — with **zero verification that the PID is actually a cozempic guard daemon**. The codebase already has `_is_cozempic_guard_process` (line 1161) introduced specifically to defend against this kind of confused-deputy bug, and `reload_self_daemon` at lines 1249/1268 uses it correctly. `_cleanup_legacy_pid` does not.

**Repro**:
1. Install cozempic pre-1.6.13 (creates `/tmp/cozempic_guard_<md5(cwd)>.pid`).
2. Crash / uninstall without cleanup.
3. OS recycles the dead PID to an unrelated user process (e.g., a Node server, a long-running editor).
4. User installs cozempic ≥1.6.13 and opens any session with the same CWD → `start_guard_daemon` → `_cleanup_legacy_pid` → SIGTERM sent to the recycled-PID process.

**Fix direction**: Before line 970, gate with `if _is_cozempic_guard_process(pid)`. Otherwise just unlink the legacy file.

**Not covered by existing tests**.

---

### BUG-G2 — `_cleanup_stale_watchers` SIGTERMs on substring-only pgrep match

**Severity**: CRITICAL
**Location**: `guard.py:711-729`

```
717      result = subprocess.run(
718          ["pgrep", "-f", "cozempic.*resumed Claude"],
719          ...
720      )
721      for pid_str in result.stdout.strip().split("\n"):
722          if pid_str:
723              try:
724                  os.kill(int(pid_str), signal.SIGTERM)
```

**Issue**: `pgrep -f` matches the **entire command line** of any process. The pattern `cozempic.*resumed Claude` is a regex — any process whose argv contains those tokens in any order along with any characters between them matches. Concrete false positives:

- `vim ~/notes/cozempic_guard_resumed_Claude_debug.md` (argv contains `cozempic` + `resumed Claude`).
- A user script named `cozempic_upgrade_resumed_claude_log.sh`.
- Another Python process that loaded the watcher string into memory and passes through `ps` (unlikely but possible if argv contains a log message).

Even without false positives, the function still doesn't call `_is_cozempic_guard_process` — any matching PID is unconditionally SIGTERM'd.

**Repro**: `sleep 999 & pgrep -f "cozempic.*resumed Claude"` — run a dummy process whose argv contains both tokens, start guard, watch it get killed.

**Fix direction**: Either (a) make the pattern the actual watcher script shape used at lines 933-938 (`echo "$(date): Cozempic guard resumed Claude in ..."`) AND verify each match's full argv matches that shape, or (b) record watcher PIDs in a dedicated registry file so cleanup doesn't need heuristic pgrep.

**Not covered by existing tests**.

---

### BUG-G3 — `_is_guard_running_for_session` missing PID-reuse defence (silent unprotected session)

**Severity**: HIGH
**Location**: `guard.py:980-995`

```
980  def _is_guard_running_for_session(session_id: str) -> int | None:
...
989      try:
990          pid = int(pid_path.read_text().strip())
991          os.kill(pid, 0)
992          return pid
```

**Issue**: Only a liveness probe (`os.kill(pid, 0)`), **no argv verification**. Called by `start_guard_daemon` at lines 1060 and 1076 to decide `"already_running": True`. If the old daemon crashed and its PID was recycled to an unrelated process, this returns the recycled PID → `start_guard_daemon` returns `already_running=True` and **does not spawn a fresh daemon** → session is permanently unprotected. The asymmetry is striking: `reload_self_daemon` (line 1249) does verify via `_is_cozempic_guard_process`; `start_guard_daemon` does not.

The related comment at line 1284-1285 (`# not on already_running (that means a concurrent SessionStart hook already spawned...)`) assumes that `already_running=True` means a real daemon is up — which is invalidated by PID reuse.

**Repro**:
1. Guard daemon crashes (uncaught exception, OOM, `kill -9`).
2. `/tmp/cozempic_guard_<sess>.pid` still contains the dead PID.
3. OS recycles that PID to any user process (browser tab, editor — common on Linux with high PID turnover).
4. Next SessionStart hook → `start_guard_daemon` → `_is_guard_running_for_session` returns the recycled PID → session runs unprotected indefinitely.

**Fix direction**: After the `os.kill(pid, 0)` probe, call `_is_cozempic_guard_process(pid)`; if it returns False, `pid_path.unlink(missing_ok=True)` and return None so caller respawns.

**Not covered by existing tests** — `test_start_guard_daemon_passes_explicit_claude_pid_to_child` patches `_is_guard_running_for_session` entirely.

---

### BUG-G4 — TOCTOU race in PID file creation (orphan daemon)

**Severity**: HIGH
**Location**: `guard.py:1058-1150`

The window between the `_is_guard_running_for_session` check (line 1060 or 1076) and the unconditional `pid_path.write_text(str(proc.pid))` at line 1150 is not atomic. If two SessionStart hooks fire concurrently (the docs say this happens on multi-pane tmux / IDE + CLI startup):

1. Hook A: check → None → spawn Popen (PID 1001).
2. Hook B: check → None (still no PID file) → spawn Popen (PID 1002).
3. Hook A: write pid_file = "1001".
4. Hook B: write pid_file = "1002".

Result: two daemons running, pid_file points only at daemon B. Daemon A is **orphaned** — unreachable by `reload_self_daemon`, `_is_guard_running_for_session`, or doctor cleanup. It runs forever (or until its parent Claude exits via the line 414-422 watchdog). Both daemons race on the same prune lock and checkpoint file.

**Repro**: Start two `cozempic guard` invocations in tight succession (e.g., `cozempic guard --session X & cozempic guard --session X &`) — confirm two PIDs, one pid-file.

**Fix direction**: Create the pid file with `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` **before** spawning Popen; if EEXIST, fall back to re-checking `_is_guard_running_for_session`. Write the spawned PID atomically via temp+rename.

**Not covered by existing tests**.

---

### BUG-G5 — `_terminate_and_resume` SIGTERMs unverified `claude_pid` (PID reuse risk)

**Severity**: HIGH
**Location**: `guard.py:804-894` (specifically 838, 862, 880, 890)

```
837          if not _wait_for_exit(claude_pid, timeout=10.0):
838              os.kill(claude_pid, signal.SIGTERM)
```

**Issue**: `claude_pid` is resolved once at guard startup (line 388-389 via `find_claude_pid()`), stored across the whole daemon lifetime (cycles = hours or days). If Claude exits and respawns (user restarts), and the OS recycles the old Claude PID to another process, the next hard-prune cycle calls `_terminate_and_resume(stale_pid, ...)` → SIGTERM + SIGKILL on a recycled unrelated PID. The defence in `reload_self_daemon` (line 1268) is absent here.

Also, the tmux/screen paths (838, 862) SIGTERM the PID **without even re-checking liveness first** — only the plain-terminal path at line 880 is inside a `try/except ProcessLookupError`.

**Repro**:
1. Start guard → find_claude_pid returns 5000.
2. User SIGKILLs Claude externally. OS reassigns PID 5000 to another long-lived process.
3. Session file grows (new Claude somewhere else writing to it, or manual append). Guard hits 55% threshold.
4. Guard calls `_terminate_and_resume(5000, ...)` → SIGTERM to unrelated process.

**Fix direction**: In `_terminate_and_resume`, before any signal, verify the PID is still a Claude Code process. Easiest check: `ps -p <pid> -o comm=` should contain `node` or `claude`. Mirror the same defence `reload_self_daemon` already has for guards.

Related: BUG-G8 below (watchdog at 414-422 correctly handles Claude-exit but `_terminate_and_resume` doesn't re-query fresh PID).

**Not covered by existing tests**.

---

### BUG-G6 — `_spawn_reload_watcher` shell-injection sink on Windows + unquoted project_dir

**Severity**: HIGH (Windows), MED (cross-platform)
**Location**: `guard.py:925-928, 912-923`

```
925      elif system == "Windows":
926          resume_cmd = (
927              f"start cmd /c \"cd /d {project_dir} && claude {resume_flag}\""
928          )
```

**Issue**: `project_dir` is interpolated **unquoted** into a Windows `cmd /c` shell string. A path containing `& shutdown /s /t 0 &` (or any cmd metachar) executes arbitrary commands. Paths with spaces also break. Linux/macOS paths go through `shell_quote`, but Windows does not. Concretely, any user whose project path contains `&`, `|`, `>`, `%`, or `^` triggers cmd injection at reload time.

Additionally, `resume_flag` on all platforms is constructed from `_detect_claude_flags(claude_pid)` output — **`original_flags` is not shell-quoted** (see BUG-G7). If the user launched Claude with a flag value containing `"` or `;`, those characters flow through `resume_cmd` into a shell string.

**Repro**: Rename a Windows project dir to `My Project & calc &`; start guard; trigger threshold → `calc` launches.

**Fix direction**: Quote `project_dir` on Windows with `subprocess.list2cmdline([...])` or pass via argv instead of a shell string. On all platforms, `shell_quote` each token of `original_flags` separately, not the whole joined string.

**Not covered by existing tests**.

---

### BUG-G7 — `_detect_claude_flags` round-trip breaks on spaces/metachar

**Severity**: MED
**Location**: `guard.py:738-778`, consumed at `816`, `902`

```
749      args = result.stdout.strip()
...
754      parts = args.split()
...
776      return " ".join(cleaned)
```

**Issue**: `parts = args.split()` uses default whitespace split. `ps -o args=` returns a single space-separated line with no shell quoting preserved — a flag value containing a space (e.g., `--add-dir "/Users/foo/My Project"`) becomes two tokens `--add-dir` and `"/Users/foo/My`. The subsequent filter pipeline doesn't reconstitute them. The rejoined `original_flags` string is then interpolated into shell commands at lines 816, 846, 869, 914-923, 927 — **the shell re-parses the broken tokens** yielding either wrong args or, with embedded shell metachar (backticks, `$(...)`, `;`), command injection.

Also line 772 (`if len(f) >= 32 and "-" in f and not f.startswith("-")`) treats any 32+ char token with dashes as a "session-id-like" argument and drops it — this eats legitimate long arguments (long file paths with dashes, repository names) silently.

**Repro**: `claude --add-dir "/tmp/my project"` → start guard → reload → spawned `claude` receives `--add-dir /tmp/my` and loses the rest.

**Fix direction**: Use `/proc/<pid>/cmdline` (NUL-separated, preserves boundaries) on Linux and the `argv` parsing trick on macOS (`ps` doesn't preserve argv boundaries reliably; consider `psutil.Process(pid).cmdline()` which correctly uses `proc_pidinfo` syscall).

**Not covered by existing tests**.

---

### BUG-G8 — Claude watchdog (line 414-422) uses PID but doesn't verify it's still Claude

**Severity**: MED
**Location**: `guard.py:414-422`

```
414              if claude_pid and claude_alive:
415                  try:
416                      os.kill(claude_pid, 0)
417                  except (ProcessLookupError, PermissionError):
418                      claude_alive = False
```

**Issue**: Only liveness is checked. If Claude exited and its PID was recycled to a different long-running process, `os.kill(claude_pid, 0)` returns success → watchdog thinks Claude is still alive → guard runs forever checkpointing a dead Claude's session. Then when the 80% threshold hits, `_terminate_and_resume` SIGKILLs the recycled unrelated process (BUG-G5).

**Repro**: same as BUG-G5. This is the upstream cause that propagates into BUG-G5.

**Fix direction**: Instead of `os.kill(claude_pid, 0)`, verify the PID still corresponds to a Claude Code process (reuse the same `ps -p <pid> -o comm=` pattern as the `_is_cozempic_guard_process` helper, but match `node`/`claude`).

**Not covered by existing tests**.

---

### BUG-G9 — `/tmp` log files grow unbounded across sessions

**Severity**: MED
**Location**: `guard.py:1099-1099`, `923` (watcher log)

```
1099  log_file = Path("/tmp") / f"cozempic_guard_{pid_key}.log"
...
1129      with open(log_file, "a", encoding="utf-8") as lf:
```

**Issue**: Log file is opened in append mode for every daemon spawn. Over many sessions, logs accumulate. On Linux (where `/tmp` is not tmpfs-cleared on reboot by default on some distros) and on long-uptime machines, these grow indefinitely. There's no rotation, no size cap, no cleanup on guard exit. Similarly, `/tmp/cozempic_guard.log` at line 923, 937 (the watcher log) is appended to by every reload.

**Repro**: Run guard for a week with daily sessions; `du -sh /tmp/cozempic_guard_*.log` grows monotonically.

**Fix direction**: Either (a) add a size check at daemon startup that truncates if the log exceeds N MB, or (b) use `RotatingFileHandler` via the logging module, or (c) use `~/.cache/cozempic/logs/` with a clean-on-startup policy. If `/tmp` is mounted `noexec` + `noatime`, opening for append still works; if it's read-only (rare), the `open(... "a")` at line 1129 raises and kills `start_guard_daemon` — BUG-G14 below.

**Not covered by existing tests**.

---

### BUG-G10 — `watcher_script` runs forever on recycled PID

**Severity**: MED
**Location**: `guard.py:933-946`

```
933      watcher_script = (
934          f"while kill -0 {claude_pid} 2>/dev/null; do sleep 1; done; "
935          f"sleep 1; "
936          f"{resume_cmd}; "
937          ...
```

**Issue**: The detached watcher polls `kill -0 claude_pid` every second. If Claude exits and the OS recycles `claude_pid` to a long-running unrelated process, the `while kill -0` loop never terminates — the watcher hangs forever consuming a shell process + 1s wake-ups. On a long-uptime machine, many reload cycles leak many zombies (well, orphaned-to-init bash processes — not zombies, but still leaked processes). Also, when `claude_pid` eventually does exit, the watcher unconditionally spawns `claude --resume` — even though the operator may not want another session.

Also: `kill -0 {claude_pid}` interpolates an untrusted integer. The type annotation is `int`, but `_terminate_and_resume(claude_pid: int, ...)` gets its argument from `reload_pid = claude_pid if claude_pid is not None else find_claude_pid()` (line 699-702). If a future caller passes a non-int, this becomes a shell injection sink. LOW within current callers, but the contract is not defended (no `int(claude_pid)` guard).

**Fix direction**: Add a maximum lifetime (e.g., `for i in $(seq 1 3600); do kill -0 ... || break; sleep 1; done` = 1-hour cap). Coerce `claude_pid` with `int(...)` before interpolation.

**Not covered by existing tests**.

---

### BUG-G11 — Reactive watcher thread is unreachable from signal handler (no join, orphaned state)

**Severity**: MED
**Location**: `guard.py:352-385`

```
355      if reactive:
...
373          watcher_thread = threading.Thread(
374              target=overflow_watcher.start, daemon=True, name="cozempic-watcher",
375          )
376          watcher_thread.start()
...
379      def _graceful_shutdown(signum, frame):
...
382          if overflow_watcher:
383              overflow_watcher.stop()
384          sys.exit(0)
```

**Issue**: On SIGTERM, `_graceful_shutdown` calls `overflow_watcher.stop()` then `sys.exit(0)` — but `sys.exit()` in a signal handler raises `SystemExit` in the main thread. If the watcher is mid-callback (holding the session file open for a `recovery.on_file_growth` call), its cleanup is short-circuited. The daemon-thread semantics (`daemon=True` at 374) mean Python terminates it abruptly when main exits, bypassing any finally-block cleanup in `JsonlWatcher.start`. In practice this could leave open file handles on NFS / FUSE filesystems.

Separately, `_graceful_shutdown` is only installed for SIGTERM (line 385). SIGINT (Ctrl-C) hits the `except KeyboardInterrupt` branch at line 569 which DOES call `overflow_watcher.stop()` but NOT in a signal-safe way (the `KeyboardInterrupt` is raised between bytecodes and the `except` runs in main thread). Mostly OK, but the asymmetry is a code smell — SIGHUP, SIGQUIT are not handled at all; they just terminate the daemon, leaking the watcher thread and any in-flight prune cycle.

**Fix direction**: Register the same handler for SIGINT, SIGHUP, SIGQUIT. Use `threading.Event` to signal the watcher and `join(timeout=N)` before sys.exit. Move the signal installation earlier so it's active before the watcher thread starts.

**Not covered by existing tests**.

---

### BUG-G12 — `_graceful_shutdown` signal handler does non-reentrant I/O

**Severity**: MED
**Location**: `guard.py:379-385`

```
379      def _graceful_shutdown(signum, frame):
380          print(f"\n  [{_now()}] Signal {signum} received — final checkpoint...")
381          checkpoint_team(session_path=session_path, quiet=False)
```

**Issue**: The handler calls `print()` and `checkpoint_team()` — both do buffered I/O. If the main thread was holding the stdio lock (e.g., mid-`print(...)` or mid-`load_messages`) when the signal arrived, the handler deadlocks on the same lock. Python's GIL means handler re-entry is only between bytecodes, but once it runs, any `print` / file write contends with whatever lock the interrupted code was holding. This is a well-known Python hazard — see CPython `PEP 656` / `signal.signal` docs.

A second SIGTERM arriving during the first handler would re-enter `checkpoint_team` → two writers to the same checkpoint file.

**Fix direction**: Signal handler should only set a flag (`shutdown_requested = True`) and return. Main loop checks the flag after `time.sleep(interval)` at line 401 and performs the checkpoint + exit. This is the documented safe pattern.

**Not covered by existing tests**.

---

### BUG-G13 — `_pid_file_for_session` collision on 12-char truncation

**Severity**: LOW
**Location**: `guard.py:949-952`

```
949  def _pid_file_for_session(session_id: str) -> Path:
950      """Return the PID file path for a guard daemon watching a specific session."""
951      session_id = _normalize_session_id(session_id)
952      return Path("/tmp") / f"cozempic_guard_{session_id[:12]}.pid"
```

**Issue**: UUIDs are 36 chars. Truncating to 12 hex chars gives 48 bits of entropy. The probability of collision among two concurrent sessions on the same user is negligible (~1e-14 for 10 sessions), but the pid-file path is also used as a lock namespace. A deterministic truncation means two different full UUIDs that happen to share their first 12 chars (impossible in normal UUIDv4 but possible with crafted session IDs from tests / hooks passing unusual strings) both map to the same pid file → the second start silently returns `already_running=True` pointing at the wrong daemon.

The function is also vulnerable to `session_id` values shorter than 12 chars — `session_id[:12]` returns `session_id` unchanged, which is fine, but the `_normalize_session_id` helper (line 53) only strips `.jsonl` suffix, not path components. If a caller passes `session_id="../../etc/passwd"`, the pid file becomes `/tmp/cozempic_guard_../../etc.pid` → the `..` components are preserved in the Path and could be used to probe arbitrary /tmp locations. Low impact (attack requires ability to pass arbitrary session_id), but worth tightening.

**Fix direction**: Validate `session_id` is a UUID hex string before using it as a filename component. Use the full UUID (no truncation) — 36 extra bytes in the filename is cheap.

**Not covered by existing tests**.

---

### BUG-G14 — `start_guard_daemon` dies if `/tmp` is readonly (no error surfacing)

**Severity**: LOW
**Location**: `guard.py:1129-1150`

```
1129      with open(log_file, "a", encoding="utf-8") as lf:
...
1150      pid_path.write_text(str(proc.pid))
```

**Issue**: If `/tmp` is readonly (unusual but happens: Docker containers with `--read-only`, hardened systemd units with `PrivateTmp=yes` gone wrong, macOS SIP edge cases), `open(log_file, "a")` raises `PermissionError`. The function has no try/except → the exception propagates to the caller. If the caller is a hook (non-interactive, no stderr visible to user), the SessionStart hook fails silently → no guard protection and no user-visible error.

Similarly, at line 1150 `pid_path.write_text(str(proc.pid))` runs AFTER `subprocess.Popen` has already spawned the daemon. If write fails, the daemon is orphaned (running with no pid file → unreachable).

**Fix direction**: Catch filesystem errors around log/pid setup; fall back to `~/.cache/cozempic/` or the project dir; if all locations fail, return `{"started": False, "reason": "tmp unwritable"}` instead of raising.

**Not covered by existing tests**.

---

### BUG-G15 — `prune_with_team_protect` mutates caller's messages list in place

**Severity**: LOW
**Location**: `guard.py:189-204`

```
189      # 3. Tag team messages as protected (strategies skip via is_protected())
190      tagged_indices: list[int] = []
191      for _, msg_dict, _ in messages:
192          if _is_team_message(msg_dict, pending_task_ids):
193              msg_dict["__cozempic_team_protected__"] = True
194              tagged_indices.append(id(msg_dict))
...
200      for _, msg_dict, _ in pruned_messages:
201          msg_dict.pop("__cozempic_team_protected__", None)
```

**Issue**: The function tags messages in the caller's `messages` list with an in-band sentinel key. It only removes the tag from `pruned_messages` (which is the subset of survivors). Any messages DROPPED by `run_prescription` still carry the `__cozempic_team_protected__` key in memory — not a problem if they're garbage-collected, but if the caller retains a reference to the original list or if the messages dict identity leaks into other data structures, those dicts are left polluted.

Much more important: the tag is written to `msg_dict` — which is the SAME dict object held in the loaded JSONL. If any downstream consumer serializes this dict back to disk (e.g., an error-path that writes the original messages after a PruneConflictError), the sentinel key `__cozempic_team_protected__` gets persisted in the on-disk session file. Claude Code itself won't interpret it, but it pollutes the on-disk format.

The `tagged_indices` list is built but never used — dead code.

**Fix direction**: Use a parallel `set()` of `id(msg_dict)` values as the protected-set instead of in-band mutation. `is_protected()` checks against the set. Zero mutation of caller data, no persistence risk.

**Not covered by existing tests** — `test_guard_robustness.py` does not exercise `prune_with_team_protect`.

---

### BUG-G16 — `_is_guard_running` (legacy alias at line 1003-1005) always returns None

**Severity**: LOW
**Location**: `guard.py:998-1005`

```
1003  def _is_guard_running(cwd: str) -> int | None:
1004      """Legacy check — scans for any guard PID file matching this CWD."""
1005      return _is_guard_running_for_session(cwd)  # Won't match, but keeps signature
```

**Issue**: Comment says outright "Won't match". This is a broken compatibility shim — any legacy caller of `_is_guard_running(cwd)` gets None regardless of daemon state. The function should be either (a) removed if no external callers exist, or (b) implemented to actually scan for matching pid files. Leaving it as a silent no-op is worse than removing it: callers who still depend on it get misleading results.

**Fix direction**: `grep -r '_is_guard_running\b' src/` to see if any live caller uses it. If none, delete. If yes, implement correctly.

**Not covered by existing tests**.

---

### BUG-G17 — Reactive `OverflowRecovery` uses stale `claude_pid` captured at startup

**Severity**: LOW
**Location**: `guard.py:363-376`

```
363      breaker = CircuitBreaker(session_id=sess["session_id"])
364      recovery = OverflowRecovery(
365          session_path, sess["session_id"], cwd or os.getcwd(), breaker,
366          danger_threshold_mb=danger_mb,
367          danger_threshold_tokens=danger_tokens,
368          claude_pid=claude_pid,
369      )
```

**Issue**: `claude_pid` is passed by value to `OverflowRecovery` at daemon startup. The watcher thread runs for the daemon's entire lifetime. If Claude exits and is respawned (user resumed with a different PID, or upgraded), `OverflowRecovery.on_file_growth` still operates against the original PID — so when it triggers an emergency reload, it kills the wrong PID (or nothing at all if the PID is dead). The main loop has a Claude-exit watchdog at line 414-422, but it BREAKS OUT OF THE LOOP on exit — it does NOT update `claude_pid` if Claude respawns.

**Fix direction**: Pass a `claude_pid_provider` callable (e.g., `find_claude_pid`) to `OverflowRecovery` instead of a static int. Let it re-resolve on each growth event.

**Not covered by existing tests**.

---

## Non-bugs ruled out (with reasoning)

- **`subprocess.run(..., shell=True)`**: none present — all `subprocess.run` calls pass argv lists (verified via `grep 'shell=True'` returned 0 results in guard.py). Subprocess output for `ps`, `pgrep`, `tmux`, `screen`, `osascript` uses argv directly.
- **Signal re-entry on `SIGTERM` while handler runs**: Python serializes signal handlers — a second SIGTERM during the first handler's `checkpoint_team` call is queued, not re-entrant. Still, the handler does unsafe I/O (see BUG-G12) so this becomes moot.
- **`flock` cross-filesystem issues**: `_PruneLock.__enter__` correctly handles `ImportError` (Windows) by setting `_fh = None`. On NFS, `fcntl.flock` typically silently succeeds without actual locking — real risk, but low concrete probability for `~/.claude/` (typically local). Flagged as a general concern but not a concrete bug.
- **`find_claude_pid` walking up the process tree — infinite loop**: loop capped at 10 iterations (line 223) with `if pid <= 1: break` guard. OK.
- **Backup cleanup at line 406 (`cleanup_old_backups(..., keep=3)`)**: correctly capped at 3 backups. OK.
- **`_wait_for_exit` polling interval 0.2s**: bounded by `timeout` parameter. OK.
- **`ping_install_if_new` + `maybe_auto_update(force=True)` at line 330-331**: does network I/O at daemon start — blocks startup. Could hang SessionStart hook. Not a concrete bug under normal network conditions but a resilience gap (not flagged here because outside guard.py scope and requires updater.py review).
- **`consecutive_empty_hard_prunes` counter at line 534-538**: correctly implements exponential-ish backoff. OK.
- **`_spawn_reload_watcher` `start_new_session=True`**: correctly detaches to init → no zombies.
- **Integer overflow / unsigned wrapping**: no concrete risks identified — Python ints are arbitrary-precision; no C-int boundary.

---

## Summary

- **Total**: 17 concrete bugs (**2 CRITICAL, 6 HIGH, 7 MED, 2 LOW**). All 17 uncovered by `tests/test_guard_robustness.py`.
- **Theme**: The codebase has been hardened against PID reuse in `reload_self_daemon` (via `_is_cozempic_guard_process`), but the same defence was never propagated to `_cleanup_legacy_pid`, `_cleanup_stale_watchers`, `_is_guard_running_for_session`, `_terminate_and_resume`, and the main-loop Claude watchdog. This is the dominant bug class (G1, G2, G3, G5, G8) and represents the most exploitable surface — any PID-recycling scenario turns the daemon into a confused-deputy that signals unrelated user processes.
- **Recommended fix-order**:
  1. **CRITICAL first (G1, G2)**: gate every `os.kill(..., SIGTERM)` on `_is_cozempic_guard_process` (or a watcher-process equivalent). Smallest diff, biggest blast-radius reduction.
  2. **HIGH (G3, G5, G8)**: propagate the same check to `_is_guard_running_for_session`, `_terminate_and_resume`, and the main-loop watchdog. Then (G4) harden PID-file creation to `O_CREAT|O_EXCL`. Then (G6, G7) quote/argv-sanitize all shell-interpolated strings.
  3. **MED (G9-G12, G17)**: log rotation, watcher-lifetime cap, watcher-thread shutdown, signal-handler-does-only-set-flag refactor, dynamic PID re-resolution for reactive path.
  4. **LOW (G13-G16)**: PID-filename hardening, fs-error handling in `start_guard_daemon`, clean up `prune_with_team_protect` in-band mutation, delete or fix `_is_guard_running`.
- All 17 bugs are testable in isolation with mocks of `os.kill`, `subprocess.run`, `Path.exists`, `Path.write_text`. None require a live Claude process to repro.
