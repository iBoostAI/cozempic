# PR #93 Architecture Audit — Junaid PR #92 Followups (items 2-5)

**Worktree:** `.claude/worktrees/polish-pr92-followups`
**Base:** v1.8.14 (commit `4614f16`, merged 2026-05-19)
**Author:** architect subagent
**Date:** 2026-05-19
**Status:** DESIGN ONLY — no production code modified in this commit.

Scope: items 2-5 from Junaid's review. Item 1 (PR body v6→v7 vs actual v8) is documentation-only and is already captured in `LESSONS_LEARNED.md` (root, local-only); not in scope here.

The audit is grounded in direct `Read` of `src/cozempic/guard.py` (1918 lines), `src/cozempic/spawn_lock.py` (360 lines), `src/cozempic/reload_lock.py` (307 lines), `src/cozempic/session.py:103-141` (`_PruneLock`), `src/cozempic/doctor.py:320-336, 1060-1075` (pidfile readers), and `plugin/hooks/hooks.json:9` (bash hook pidfile reader).

---

## Item #2 — Remove dead `_spawn_locks` dict

### Current state (file:line refs)

`src/cozempic/guard.py:35-36` — module-level state:

```python
_spawn_locks: dict[str, threading.Lock] = {}
_spawn_locks_mu = threading.Lock()
```

`src/cozempic/guard.py:1283-1292` — sole consumer, inside `_is_guard_running_for_session` when the pidfile contains `pid <= 0`:

```python
with _spawn_locks_mu:
    lock = _spawn_locks.get(norm_sid)
if lock is not None and not lock.acquire(blocking=False):
    # Lock is held → spawner is in-flight; placeholder is live.
    return None
if lock is not None:
    lock.release()
pid_path.unlink(missing_ok=True)
return None
```

**No producer exists in the current tree.** `grep -n '_spawn_locks\[' src/cozempic/` returns zero hits — nothing ever assigns into the dict. Junaid's claim is accurate: this is a consumer branch on a dict that is provably empty for the entire process lifetime. `_spawn_locks.get(norm_sid)` always returns `None`, the `if lock is not None` branches are dead, and the only reachable line is `pid_path.unlink(missing_ok=True)`.

Origin: per `tests/test_guard_race_2026_05_18.py:12,123` comments, an earlier iteration populated `_spawn_locks` inside the in-process spawn path. PR #92 replaced that whole mechanism with the kernel-mediated `DaemonSpawnClaim` (see `spawn_lock.py` module docstring + `guard.py:1466-1471`). The dict was retained "as a fast-path for single-process scenarios" (docstring lines 32-34) but no producer was reinstated, so the consumer is a no-op.

### Proposed change

1. Delete `_spawn_locks` and `_spawn_locks_mu` (lines 29-36, including the 7-line comment block).
2. In `_is_guard_running_for_session` (lines 1283-1293), collapse the `pid <= 0` branch to:

```python
if pid <= 0:
    # Pidfile contains a sentinel/placeholder — treat as stale.
    # Cross-process freshness is enforced by DaemonSpawnClaim's
    # O_CREAT|O_EXCL + _FRESH_PIDFILE_SECONDS gate, not by this branch.
    pid_path.unlink(missing_ok=True)
    return None
```

3. Also drop the unused `norm_sid` local at line 1269 if no other use remains. (It's currently only consumed by `_spawn_locks.get(norm_sid)`. Grepping for `norm_sid` confirms — its only reference in this function is the deleted lookup.)
4. Remove the `threading` import if it becomes unused. `grep -n 'threading' src/cozempic/guard.py` shows two other uses (line 391 — `import threading` for the watcher thread, line 409 — `threading.Thread(...)`), so the top-level import at line 25 **stays**.

Net: ~12 LOC deleted (constants + mu + consumer branch + unused local), no logical behaviour change because the dict is empty.

### Test contract

Single RED → GREEN test in `tests/test_guard_polish_pr93.py`:

`TestPolishPR93_SpawnLocksDictRemoved`:
- RED: `from cozempic import guard; assert not hasattr(guard, '_spawn_locks')` — currently fails (attr exists).
- GREEN: after deletion, attribute is gone.

Plus a regression test that `_is_guard_running_for_session` still treats `pid <= 0` as stale and unlinks the file (covers the surviving branch):

`TestPolishPR93_PlaceholderPidIsUnlinked` (extends the existing TestR3_1 pattern from `test_guard_hardening.py:1409`):
- Set up: write `"0\n"` (or `"-1\n"`) into a freshly-created pidfile, call `_is_guard_running_for_session(sid)`, assert returns `None` AND `pid_path.exists()` is False.

### Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| External caller relies on `_spawn_locks` symbol | LOW — module-private (`_`-prefixed); grep across `src/`, `tests/`, `plugin/`, `packaging/` shows only the two internal sites + test docstrings | None needed; underscore prefix is the contract. Test docstrings (`test_guard_race_2026_05_18.py:12,123`) describe historical behaviour and don't import the symbol. |
| In-process race re-introduced | LOW — `DaemonSpawnClaim` is process-level via O_CREAT|O_EXCL on the pidfile, which IS cross-process AND cross-thread atomic on POSIX | The kernel claim subsumes the in-process fast-path entirely; no race surface re-opens. |
| Tests asserting `_spawn_locks` exists | LOW — `grep '_spawn_locks' tests/` shows only descriptive comments, no `assertEqual`/`hasattr` checks | None needed. |

---

## Item #3 — Pidfile unlink on K=10 + `_graceful_shutdown` exits

### Current state

Exhaustive enumeration of `sys.exit` calls in `guard.py` + `spawn_lock.py` (grep evidence: zero `sys.exit` in spawn_lock.py, three in guard.py):

| File:Line | Caller | Exit code | Pidfile unlinked? |
|---|---|---|---|
| `guard.py:340` | `start_guard` — session-not-found early-out | `sys.exit(1)` | YES (line 334: `_pid_file_for_session(session_id).unlink(missing_ok=True)`) |
| `guard.py:420` | `_graceful_shutdown` — SIGTERM signal handler | `sys.exit(0)` | **NO** ← leak |
| `guard.py:612` | HARD-loop K=10 voluntary exit | `sys.exit(0)` | **NO** ← leak (new in PR #92) |

`_graceful_shutdown` leak was already known and parked: see `TODO.md:55` ("Post-PR#88 validator findings: `_graceful_shutdown` does NOT unlink pidfile on SIGTERM. Pre-existing, not a regression vs main."). PR #92 added the K=10 path with the same leak, which is exactly the **class-of-bug fold rule violation** the new CLAUDE.md FIX DISCIPLINE explicitly calls out (lines 120-122):

> "PR #92 added a NEW sys.exit(0) path (K=10) without unlinking pidfile, while the pre-existing `_graceful_shutdown` had the same leak. The right call was to fold the 1-line fix into both paths — not introduce a SECOND leak surface while leaving the first."

So this item MUST fix BOTH paths in the same commit per the fold rule.

Why the leak is not catastrophic: next spawn's stale-detection (`DaemonSpawnClaim._claim` → `_is_pidfile_fresh` + alive-PID check at `spawn_lock.py:271-289`) will clean it up. But `/tmp` accumulates one pidfile per dead session until next spawn for that specific session-id slug runs — for long-running operators with hundreds of distinct sessions, this is real cruft. More importantly, `doctor.py` enumerates `/tmp/cozempic_guard_*.pid` (line 322) and any stale entry becomes a confusing "stale pidfile" line in `cozempic doctor` output.

Third path candidate examined and ruled out: `start_guard:332-340` already unlinks BEFORE exit — no fold needed there.

### Proposed change (BOTH exit paths)

**Helper introduction** (avoid two ad-hoc try/except blocks):

Add at module level near `_pid_file_for_session` (~line 1230):

```python
def _safe_unlink_session_pidfile(session_id: str | None) -> None:
    """Best-effort pidfile unlink on daemon exit paths.

    Used by every daemon shutdown path (SIGTERM handler, K=10 voluntary
    exit, future panic exits). Swallows ValueError (malformed session_id)
    and OSError (pidfile already gone, /tmp unwritable). Never raises —
    the daemon is mid-shutdown; nothing useful to do on failure.

    Class-of-bug fold (PR #93): consolidates the unlink so adding a new
    sys.exit path requires touching ONE callsite, not N.
    """
    if not session_id:
        return
    try:
        _pid_file_for_session(session_id).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
```

**Path 1 — `_graceful_shutdown` (line 415-421):**

```python
def _graceful_shutdown(signum, frame):
    print(f"\n  [{_now()}] Signal {signum} received — final checkpoint...")
    checkpoint_team(session_path=session_path, quiet=False)
    if overflow_watcher:
        overflow_watcher.stop()
    _safe_unlink_session_pidfile(sess["session_id"])  # new
    sys.exit(0)
```

`sess` is in closure scope from `start_guard` line 330 — accessible.

**Path 2 — K=10 voluntary exit (line 590-612):**

```python
if consecutive_empty_hard_prunes >= HARD_LOOP_EXIT_THRESHOLD:
    try:
        checkpoint_team(session_path=session_path, quiet=True)
    except Exception:
        pass
    print(
        f"  [{_now()}] Guard powerless against live-context "
        # ... existing diagnostic message ...
        flush=True,
    )
    _safe_unlink_session_pidfile(sess["session_id"])  # new
    sys.exit(0)
```

### Test contract (RED + GREEN)

`tests/test_guard_polish_pr93.py::TestPolishPR93_PidfileUnlinkedOnExit`:

1. **`test_graceful_shutdown_unlinks_pidfile`** — write a fake pidfile via `_pid_file_for_session(sid)`, monkeypatch `checkpoint_team` to no-op, simulate SIGTERM by invoking the registered handler directly (`signal.getsignal(signal.SIGTERM)(15, None)`), catch the `SystemExit`, assert pidfile no longer exists. RED on current `main`.

2. **`test_k10_exit_unlinks_pidfile`** — drive `start_guard` with mocked `guard_prune_cycle` returning `saved_mb=0` ten times, catch `SystemExit(0)`, assert pidfile is gone. RED on current `main`.

3. **`test_safe_unlink_swallows_invalid_session_id`** — call `_safe_unlink_session_pidfile("not-a-uuid")`, assert no exception. Guards against the leak helper itself raising on malformed input during a shutdown path (worst possible time for an exception).

4. **`test_safe_unlink_handles_missing_pidfile`** — call helper when the file doesn't exist, assert no exception (uses `missing_ok=True`).

5. **Regression**: existing `TestPolishPR93_PlaceholderPidIsUnlinked` (item #2 above) must still pass — same path.

### Class-of-bug fold check

Per FIX DISCIPLINE class-of-bug fold rule, before merging I must verify:

**Q: Are there OTHER `sys.exit` / `os._exit` / `raise SystemExit` paths in the daemon process that would leak a pidfile?**

Audit:
- `guard.py:340` (`start_guard` session-not-found): ALREADY unlinks at line 334. ✅
- `guard.py:420` (`_graceful_shutdown` SIGTERM): leaks → this PR fixes.
- `guard.py:612` (K=10 voluntary): leaks → this PR fixes.
- `spawn_lock.py`: zero `sys.exit` calls. Cleanup is via `__exit__` + `handed_off` flag, not exit.
- `cli.py`, `overflow.py`, `watcher.py`: not the daemon process — they run in the parent Claude process or threads inside the daemon but don't terminate the daemon.

**Q: Other process-exit paths** (not `sys.exit` but equivalent)?

- KeyboardInterrupt (line 664): currently does NOT unlink. **This is a third leak surface.** Caught only by `cozempic guard` running in foreground (interactive). When the operator hits Ctrl-C on a foreground daemon, pidfile lingers. Lower priority than SIGTERM (SIGTERM is what tools/init systems send; Ctrl-C is rare against a backgrounded daemon) but per the fold rule, **fix in same commit** to avoid leaving the third surface for PR #94.
- `break` paths inside the main loop (lines 446-447, 467-468, 528, 575): these fall through to the `finally` at line 672 → return from `start_guard` → daemon process terminates with exit 0. Same leak — pidfile is never explicitly unlinked. The `finally` only stops the overflow watcher.
- `_cleanup_legacy_pid` exception paths: write to legacy CWD-keyed file, not session pidfile — unrelated.

**Decision**: Fold ALL daemon-exit surfaces in this PR. Concretely:

- Add `_safe_unlink_session_pidfile(sess["session_id"])` calls at:
  - Line 420 (`_graceful_shutdown` before `sys.exit(0)`)
  - Line 612 (K=10 voluntary exit before `sys.exit(0)`)
  - Inside the `KeyboardInterrupt` block at line 664 (after the checkpoint)
  - In the `finally` block at line 672, AFTER the overflow_watcher stop — as the belt-and-braces cleanup so EVERY `break` path is covered

The `finally` cleanup makes the call site list shorter and more defensible — it's the single guaranteed exit path for `start_guard`. With that, the explicit calls at SIGTERM/K=10/KeyboardInterrupt are redundant for normal flow (the finally runs after them), but I keep them anyway because:
1. `sys.exit(0)` raises `SystemExit`, which DOES trigger the `try/finally` — so the finally WILL run. Verified.
2. But the signal handler `_graceful_shutdown` runs in a signal context. Calling `sys.exit(0)` from a signal handler raises `SystemExit` in the main thread on its next bytecode boundary — the `try/finally` at line 435/672 catches it.
3. So technically a single `finally` cleanup is sufficient. The explicit calls are defense-in-depth (clearer intent at each callsite for code-review + class-of-bug fold tests).

**Final recommendation: helper + finally-only call.** Cleaner, single source of truth, covers all six exit paths. Explicit calls at SIGTERM/K=10/KeyboardInterrupt are optional documentation but not required for correctness.

```python
finally:
    if overflow_watcher:
        try:
            overflow_watcher.stop()
        except Exception:
            pass
    _safe_unlink_session_pidfile(sess["session_id"])  # NEW: covers all 6 exit paths
```

Add the dedicated tests for the K=10 and SIGTERM paths anyway (they're the regression tests for what Junaid called out), plus one explicit test for the KeyboardInterrupt path and one for each `break` (file disappeared, claude exited).

### Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Daemon crashes during checkpoint and `finally` runs before checkpoint completes, leaving stale pidfile but no checkpoint | LOW — finally always runs LAST | Order: checkpoint, then watcher stop, then unlink. Existing order preserved. |
| Reload-spawn race: helper unlinks pidfile while a fresh SessionStart hook is mid-spawn for the same session | LOW — `DaemonSpawnClaim` uses O_CREAT|O_EXCL on the same path; if we unlink ours after our exit AND a peer is mid-spawn, their claim either already won (file exists → we unlink theirs! bug) or hasn't fired yet | **CAS-style guard**: only unlink if pidfile still contains OUR PID. Use existing `_pid_file_points_to(session_id, os.getpid())` (line 1743) as the gate. Without this, we could destroy a peer's just-completed claim during their startup window. |
| Helper called twice (signal handler runs, then `finally` runs) | LOW — `unlink(missing_ok=True)` is idempotent | Already handled. |
| `sess["session_id"]` shape changes (becomes path) | LOW — `start_guard` normalizes via `_resolve_session_by_id` which returns `{session_id: <stem>}` for path inputs | Helper accepts both via `_pid_file_for_session` which calls `_normalize_session_id`. |

**Refined design**: use `_pid_file_points_to` as the unlink gate.

```python
def _safe_unlink_session_pidfile(session_id: str | None) -> None:
    if not session_id:
        return
    try:
        if _pid_file_points_to(session_id, os.getpid()):
            _pid_file_for_session(session_id).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
```

This is the same CAS pattern `reload_self_daemon` uses at lines 1802, 1809, 1823, 1829 (sister-module precedent — bonus parity). Test contract gains `test_safe_unlink_skips_when_pid_mismatch` for this case.

---

## Item #4 — agents-active K=10 deferral + hard cap (ARCHITECTURAL)

### Current state

`src/cozempic/guard.py:498-636` — the full HARD1+HARD2+SOFT phase decision tree.

The relevant K=10 sub-branch is inside the HARD1 phase (line 535-636). HARD1 is the only path that increments `consecutive_empty_hard_prunes`. HARD2 (line 498-533) ALWAYS reloads, regardless of `agents_active` — it explicitly states "EMERGENCY THRESHOLD" and "WARNING: Agents are active but compaction is imminent — reload required" (line 509).

`agents_active` is computed at line 491-496 from the live `state.subagents` — True if any subagent is in `("running", "unknown")`.

When `agents_active=True` at HARD1 (line 542-554):
- Prune runs WITHOUT reload (`auto_reload=False`)
- No process kill
- Returns to top of loop
- If `saved_mb == 0`, `consecutive_empty_hard_prunes += 1` (line 582)
- At K >= 10, `sys.exit(0)` (line 612) — **DAEMON DIES even though agents are mid-task**

This is the BMAD-with-subagents reproducer Junaid flagged AND the exact scenario the handoff at `~/sanofi/silc-data/.claude/handoffs/cozempic-guard-crash-2026-05-18.md` documents:

> "Spawn 4 parallel subagents in background, each producing 80–120K-token transcripts... Within 30s of the 4th completion, the guard's HARD THRESHOLD (55%) line appears... the lead process exits."

The handoff describes the v1.8.11 cascade (double-daemon race) which PR #92 fixed. But the K=10 exit BEHAVIOUR was added by PR #92 itself, and the new failure mode is: at K=10, `sys.exit(0)` fires, daemon dies, agents still running, no protection. The diagnostic message (line 599-611) tells the user to `/clear` — but `/clear` discards subagent state entirely. That advice is wrong for the agents_active case.

### Design decision: skip-K-increment vs defer-exit vs hybrid

#### Option A — Skip K-increment when agents_active

When `agents_active=True` AND prune returns `saved_mb=0`, do **NOT** increment `consecutive_empty_hard_prunes`. The counter only advances on cycles where we genuinely tried-and-failed, not on cycles where we couldn't try (because reload was suppressed for agent safety).

**Pros:**
- Trivial implementation (one `if` branch around the increment).
- Preserves K=10 semantics for the no-agents case (legitimate "daemon powerless" scenario).
- No new state to manage.

**Cons:**
- Agents could legitimately run for hours; daemon stays in exponential-backoff sleep forever, wasting cycles polling.
- No upper bound — if a session has perma-running agents, daemon never exits, never clears the K-state, never resets.
- Doesn't fix the underlying issue: at the next agent-quiesce moment, K still doesn't increment because we now have a 5-minute backoff and the counter is stuck.
- Doesn't actually defer the exit problem so much as defer the recognition of it.

#### Option B — Defer exit when agents_active

At K=10, instead of `sys.exit(0)`, check `agents_active`:
- If False → exit (current behaviour).
- If True → don't exit, but keep the backoff at the cap, log a one-time "deferred exit pending agent quiesce" message, re-check on every subsequent cycle.

When agents go quiet, **then** the normal K=10 exit fires.

**Pros:**
- Preserves the K=10 contract (daemon eventually exits when truly powerless) but respects the agents.
- Maps to the user's intuition: "don't kill the daemon while my subagents are working".
- Reuses the existing `agents_active` signal — no new computation.

**Cons:**
- Unbounded — if agents NEVER quiesce (perma-running BMAD that lasts the whole session), daemon never exits. /tmp pidfile sticks. But this is the same surface as a normal session that never crosses K=10 at all, so not a NEW leak.
- Diagnostic message must change to NOT say "type /clear" when agents are active.

#### Option C — Hybrid: defer-exit + hard cap at K=N

Combine B with an upper bound. Default: defer exit while agents_active, BUT after K reaches a HARD cap (e.g., HARD_LOOP_HARD_EXIT_THRESHOLD = 50, i.e., ~50 × cap_sleep = ~4 hours), exit regardless. The hard cap prevents the unbounded sleep loop from outliving the operator's intent.

**Pros:**
- Bounded — guarantees daemon eventually exits on truly stuck sessions.
- Respects in-flight agents for a reasonable window (4h is well past any normal subagent batch).
- Operator can override via env var if their use case is exotic (long-running BMAD spanning days).

**Cons:**
- Two constants instead of one; slightly more cognitive load.
- The hard-cap exit IS the same crash the user originally reported — just postponed. Doesn't fundamentally solve the problem, just buys time.

#### Recommended: **Option C (hybrid)** with conservative defaults

Rationale grounded in FIX DISCIPLINE (propre + long-term, not simple):

- **Option A alone** silently defers detection of a truly hung daemon. Operator never knows the K-state is wedged. Anti-pattern: "papers over" the problem.
- **Option B alone** has no upper bound. A bug in subagent state tracking (e.g., `state.subagents` perma-stale at `"unknown"`) would wedge the daemon forever. That's a real risk: `_is_team_message` / `extract_team_state` is heuristic and has had edge cases (BUG-G15 noted in TODO.md:86: "in-band mutation sentinel leaks to pruned messages").
- **Option C** gives us the agent-respectful behaviour Junaid asked for AND a guaranteed exit. The hard cap acts as a circuit breaker against agent-detection bugs.

Concretely, the recommended logic at the K-check (replacing `sys.exit(0)` at line 612):

```python
if consecutive_empty_hard_prunes >= HARD_LOOP_EXIT_THRESHOLD:
    # Wedge detection: check if subagents are still running. If so, defer
    # exit — killing the daemon while agents are mid-task would destroy
    # their work and tell the operator to /clear (which also destroys it).
    # Hard cap at HARD_LOOP_HARD_EXIT_THRESHOLD ensures we eventually exit
    # even if agents perma-run (defensive against extract_team_state bugs).
    if agents_active and consecutive_empty_hard_prunes < HARD_LOOP_HARD_EXIT_THRESHOLD:
        # Defer: stay alive, keep backoff at cap, emit one-time notice.
        if not deferred_exit_announced:
            print(
                f"  [{_now()}] K={consecutive_empty_hard_prunes} reached "
                f"normal exit threshold ({HARD_LOOP_EXIT_THRESHOLD}) but "
                f"{sum(1 for s in state.subagents if s.status in ('running', 'unknown'))} "
                f"subagents are still active. Deferring daemon exit until "
                f"agents quiesce or K reaches hard cap "
                f"({HARD_LOOP_HARD_EXIT_THRESHOLD}).",
                flush=True,
            )
            deferred_exit_announced = True
        # Continue to backoff sleep (already at cap by now); do NOT exit.
    else:
        # Normal K=10 exit, OR K=50 hard-cap exit even with agents.
        # The diagnostic message conditionally adapts.
        try:
            checkpoint_team(session_path=session_path, quiet=True)
        except Exception:
            pass
        if agents_active:
            # Hard cap reached with agents still active — different message.
            print(
                f"  [{_now()}] Guard hard-cap exit (K="
                f"{consecutive_empty_hard_prunes}, agents still active). "
                f"Subagent state will be lost on next compaction. Consider "
                f"finishing current subagents and starting a fresh session.",
                flush=True,
            )
        else:
            # Original K=10 message (existing text).
            print(...)
        _safe_unlink_session_pidfile(sess["session_id"])  # item #3
        sys.exit(0)
```

### Proposed constant values

```python
HARD_LOOP_EXIT_THRESHOLD = 10            # unchanged — normal exit when no agents
HARD_LOOP_HARD_EXIT_THRESHOLD = 50       # new — exit even with agents at K=50
```

Defaults reasoning:
- K=10 reached at: interval=30s, K=3-6 cap at 300s. Cumulative wall time to K=10 ≈ 30+30+60+120+240+300+300+300+300 ≈ 28 minutes. So K=10 is "the daemon has been useless for ~half an hour".
- K=50 reached at ≈ 28min + (40 × 300s) = ≈ 3.5 hours additional ≈ 4 hours total. Long enough for any reasonable subagent batch; short enough that a stuck session doesn't outlive an operator's coffee break + workday.
- Configurable via env var (sister-module precedent: `COZEMPIC_PIDFILE_FRESH_SECONDS` at `spawn_lock.py:106-115`). Add `COZEMPIC_GUARD_HARD_EXIT_K` with same clamp pattern (positive int, ≤ 1000, default 50).

### Test contract — MUST include reproducer-scenario walk

`tests/test_guard_polish_pr93.py::TestPolishPR93_K10AgentsActiveDefer`:

1. **`test_k10_exits_when_no_agents`** (regression of current v1.8.14 behaviour) — drive `start_guard` to K=10 with `state.subagents == []`, assert `SystemExit(0)`.

2. **`test_k10_defers_when_subagents_running`** — drive to K=10 with `state.subagents = [Subagent(status="running")]`, assert daemon does NOT exit, assert one-time "deferring daemon exit" log line emitted, assert K continues to increment, assert backoff stays at cap.

3. **`test_hard_cap_exits_with_agents_active`** — extend to K=50 (or env-var-overridden lower cap for test speed) with agents still running, assert `SystemExit(0)`, assert different diagnostic message ("hard-cap exit") that does NOT recommend `/clear`.

4. **`test_resumes_normal_after_agents_quiesce`** — at K=15 with deferred exit, transition `subagents[0].status = "completed"` on next cycle, assert exit fires immediately (no extra K cycles needed).

5. **`test_k_counter_resets_on_nonzero_prune`** (existing behaviour regression) — drive to K=8, return `saved_mb=10`, assert K resets to 0, no exit at next cycle.

6. **`test_env_var_overrides_hard_cap`** — set `COZEMPIC_GUARD_HARD_EXIT_K=20`, assert hard-cap fires at K=20.

7. **`test_env_var_invalid_falls_back_to_default`** — set `COZEMPIC_GUARD_HARD_EXIT_K=not-a-number`, assert default 50 still applies (matches `_read_fresh_window_seconds` failsafe).

**BMAD-with-subagents reproducer walk** (manual / integration-style, may be xfail on CI):

`tests/test_guard_polish_pr93_reproducer.py::TestReproducerBMAD`:

Simulates the handoff scenario:
1. Build a synthetic JSONL with 4 large subagent transcripts (each ~100KB of tool_use+tool_result blocks).
2. Mark all 4 subagents as `running` in the in-memory `TeamState`.
3. Set `threshold_tokens=550_000`, current tokens ≥ 550K (HARD1 hit).
4. Mock `guard_prune_cycle` to return `saved_mb=0` (immutable tool-result blocks dominate).
5. Run `start_guard` (with patched `time.sleep` to compress time).
6. Assert: at K=10, daemon does NOT exit (`agents_active=True`); at K=50, daemon exits with hard-cap message.
7. Also assert: pidfile is unlinked at the hard-cap exit (item #3 fold).

This is the test that catches the exact failure Junaid described.

### Cross-lock matrix check

For each lock the daemon process holds or interacts with, verify the new defer-exit logic does not deadlock or starve:

| Lock | Held during defer? | Risk | Verdict |
|---|---|---|---|
| `_ReloadLock` (`reload_lock.py:160`) | NO — only acquired inside `guard_prune_cycle` (line 817) for the duration of `_terminate_and_resume`. Released before returning to main loop. | The defer happens AFTER `guard_prune_cycle` returns. No overlap. | SAFE |
| `_PruneLock` (`session.py:103`) | NO — acquired inside `guard_prune_cycle` via `with _PruneLock(session_path)` (line 717). Released before main loop continues. | Same as above. | SAFE |
| `_HostFileLock` (`helpers.py:59`) | Used by `record_savings` inside `guard_prune_cycle` (line 786). Released within that call. | No overlap with defer logic. | SAFE |
| `_SettingsLock` (`init.py:223`) | Used by `cozempic init` flow, not the daemon loop. | Not on daemon hot path. | SAFE |
| `DaemonSpawnClaim` (`spawn_lock.py:201`) | Held by the daemon's PARENT (the SessionStart hook process that spawned us) during the few-ms window of `start_guard_daemon`. Released BEFORE `start_guard` body runs. | Defer logic is inside `start_guard` body. No overlap. | SAFE |
| `_spawn_locks` (`guard.py:35`) | DELETED in item #2. | N/A | N/A |

**Conclusion**: defer-exit holds no locks across the deferred cycles. The daemon process sleeps in `time.sleep(backoff)` at line 633 between cycles, holding nothing. The only persistent resource is the pidfile (which is the daemon's own identity, not a lock per se — its presence signals "this PID owns this session").

**Bonus check**: does deferring the exit prevent a new SessionStart hook from spawning a replacement? Yes — `_is_guard_running_for_session` will return our PID, `start_guard_daemon` returns `already_running=True`, no second spawn. That's the desired behaviour during defer (we ARE still alive and responsible for the session).

### Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `agents_active` stays True forever due to a bug in `extract_team_state` (BUG-G15 type) | MED | HARD cap at K=50 ensures eventual exit. |
| Hard cap fires while operator IS finishing subagents (false alarm) | LOW — 4h window is generous | Env var override + clearer "hard-cap exit, subagents may lose state" message. |
| Operator confused by "deferring" message — thinks daemon is hung | LOW | One-time print, includes current K and the hard cap target. |
| Defer logic interacts with backoff sleep (over-sleeps or under-sleeps) | LOW | Backoff already capped at 300s; defer just keeps cycling at that cap. No new sleep added. |
| New `deferred_exit_announced` state variable leaks across daemon restarts | NONE — module-local, dies with the process | N/A |
| Test fixtures (mocking `time.sleep`) hit recursion / hang | MED | Use `unittest.mock.patch('time.sleep')` to no-op + `cycle_count` limit in test driver. Standard pattern, already used in `tests/test_guard_*.py`. |

---

## Item #5 — DaemonSpawnClaim metadata parity with `_ReloadLock`

### Current state (DaemonSpawnClaim writes PID only)

`src/cozempic/spawn_lock.py:295-298` — claim write:

```python
try:
    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
finally:
    os.close(fd)
```

`src/cozempic/guard.py:1567-1576` — daemon-pid hand-off:

```python
_tmp_fd = os.open(str(tmp_path), _tmp_flags, 0o600)
try:
    os.write(_tmp_fd, f"{proc.pid}\n".encode("utf-8"))
finally:
    os.close(_tmp_fd)
os.rename(str(tmp_path), str(pid_path))
```

Both write a single line: `<pid>\n`. No timestamp, no initiator. Triage on operator boxes is hard: when you see `/tmp/cozempic_guard_abc123def456.pid` with PID 12345, you have no idea whether it was claimed by a SessionStart hook (normal), an in-process `reload_self_daemon` call (post-upgrade), or a stale crash artifact from yesterday. You must `ps -p 12345 -o lstart=` to recover the timestamp, and there's no record of WHO claimed it.

### Sister-module precedent

`src/cozempic/reload_lock.py:222-227` — `_ReloadLock._try_create` payload:

```python
payload = (
    f"{os.getpid()}\n"
    f"{datetime.now().isoformat(timespec='seconds')}\n"
    f"{self.initiator}\n"
)
os.write(fd, payload.encode("utf-8"))
```

Three lines: PID, ISO timestamp, initiator string (one of `cli-reload`, `guard-hard1`, `guard-hard2`, `overflow` — `reload_lock.py:48-51`).

Parser at `reload_lock.py:130-157` (`_read_lock_metadata`) handles arbitrary trailing metadata: splits on `\n`, takes first token as PID via `int(content[0].strip())`. Tolerates extra lines.

This is the convention to mirror.

### Proposed payload format

**Payload (3 lines, identical structure to `_ReloadLock`)**:

```
<pid>\n
<iso-timestamp>\n
<initiator>\n
```

Where `initiator` for `DaemonSpawnClaim` is one of:
- `spawn-claim-parent` — the SessionStart hook parent process writing its own PID at `_claim` time
- `spawn-claim-daemon` — the atomic rename hand-off writing the daemon's PID

These names follow `_ReloadLock`'s `INIT_*` constants pattern. Add to `spawn_lock.py`:

```python
# Initiator strings — mirrored from reload_lock.INIT_* conventions for
# operator-triage parity. The parent-vs-daemon distinction is the only
# meaningful split for spawn-claim payloads.
INIT_SPAWN_PARENT = "spawn-claim-parent"
INIT_SPAWN_DAEMON = "spawn-claim-daemon"
```

**Backward compatibility — pidfile readers MUST tolerate extra lines.**

Exhaustive enumeration of pidfile readers (grep evidence above):

| Reader | File:Line | Current parse | Safe with multi-line? |
|---|---|---|---|
| `guard._is_guard_running_for_session` | guard.py:1279, 1855 | `int(pid_path.read_text().strip())` | **NO** — `int("12345\n2026-05-19...\nspawn-claim-parent")` raises ValueError. Already-caught by `except ValueError` (line 1318), would treat the file as stale and likely unlink it. **BREAKS the claim.** |
| `guard._pid_file_points_to` | guard.py:1752 | `int(path.read_text().strip())` | Same — would return False (the existing `except` block catches it), causing CAS-unlink logic to skip cleanup. Less severe but wrong. |
| `DaemonSpawnClaim._read_existing_pid` | spawn_lock.py:315-330 | `first = content.split()[0]` → `int(first)` | ✅ ALREADY safe — splits and takes first token. |
| `doctor._is_live_guard_pid` caller path | doctor.py:1068 | `int(_Path(pidf).read_text().strip())` | **NO** — same break as `_is_guard_running_for_session`. Would treat the live daemon as dead and skip it in doctor's "live guards" tally. |
| `doctor._collect_pid_entries` | doctor.py:327 | `int(pid_path.read_text().strip())` | **NO** — would classify the file as stale and offer to remove it via `doctor --fix`. **Could destroy a live claim.** |
| `tests/test_hook_idempotency_shell.py:151` | tests | `int(self.pid_file.read_text().strip())` | NO — test would break. |
| `plugin/hooks/hooks.json:9` (bash) | hooks | `kill -0 "$(cat \"$GUARD_PID_FILE\" 2>/dev/null)"` | **NO** — `kill -0 12345\n2026-...\nspawn-claim-parent` is a multi-arg call: `kill -0` would be invoked with multiple args and likely succeed on the first valid PID, but the behaviour is shell-implementation-defined. **HIGH RISK.** |

**Conclusion**: I CANNOT change the pidfile format without updating ALL these readers. Every reader except `DaemonSpawnClaim._read_existing_pid` does `int(read_text().strip())` and would break or misclassify.

**Two implementation options:**

#### Option 5a — Add metadata, update all readers

Touch every reader (5 Python sites + 1 bash hook + N tests) to use `content.split()[0]` or `splitlines()[0]` semantics. Pidfile becomes the 3-line format.

Pros: True parity with `_ReloadLock`. Operator triage parity is real.
Cons: 6+ site touch. Bash hook is in `plugin/hooks/hooks.json` — that's a STRING inside JSON, error-prone to edit; also requires schema bump `cozempic-hook-schema=v8` → `v9` (PR #92 just took v8), with `cozempic init` migration. Three test files to update.

#### Option 5b — Sidecar metadata file (parity-equivalent without format change)

Keep the pidfile as `<pid>\n` (single line, all existing readers continue to work unchanged). Write metadata to a SIDECAR file `cozempic_guard_<slug>.pid.meta` containing the timestamp + initiator. Operator triage tools (and `doctor`) read the sidecar.

Pros:
- Zero risk to existing readers — they keep working.
- No hook schema bump.
- No test updates for the int-parse paths.
- Sidecar can be ignored safely (orphan if pidfile is gone — `doctor` already enumerates and cleans `/tmp/cozempic_guard_*` artifacts).

Cons:
- Two files instead of one.
- Metadata can drift from pidfile (sidecar write fails, race during rename, etc.) — needs to be best-effort and tolerated-stale.
- "Parity" is approximate, not literal — `_ReloadLock` carries metadata IN the lock file itself.

#### Recommended: **Option 5a** (true parity), gated behind the FIX DISCIPLINE pre-PR audit

Per FIX DISCIPLINE "Grep for sister-module parity": adopt the sister-module convention unless documented reason to diverge. Option 5b's "diverge" reason is "fewer sites to touch", which is exactly the "simple short-term" anti-pattern FIX DISCIPLINE forbids.

Pros for 5a beyond parity:
- Single source of truth — metadata cannot drift from pidfile.
- Doctor can show timestamp + initiator in `doctor` output without extra file lookups.
- Operator running `cat /tmp/cozempic_guard_*.pid` gets the full triage info immediately.

Implementation:

1. **Update `DaemonSpawnClaim._claim` (spawn_lock.py:295)**:
   ```python
   from datetime import datetime
   payload = (
       f"{os.getpid()}\n"
       f"{datetime.now().isoformat(timespec='seconds')}\n"
       f"{INIT_SPAWN_PARENT}\n"
   )
   os.write(fd, payload.encode("utf-8"))
   ```

2. **Update daemon-pid hand-off (guard.py:1573)**:
   ```python
   from datetime import datetime
   payload = (
       f"{proc.pid}\n"
       f"{datetime.now().isoformat(timespec='seconds')}\n"
       f"spawn-claim-daemon\n"
   )
   os.write(_tmp_fd, payload.encode("utf-8"))
   ```

3. **Helper `_parse_pidfile_pid(pidfile)` in spawn_lock.py** (new public-ish helper, mirrors `_read_lock_metadata` in reload_lock.py):
   ```python
   def _parse_pidfile_pid(pid_path: Path) -> int:
       """Read PID from a cozempic guard pidfile (1-line or 3-line format).

       Tolerates both legacy single-line `<pid>\\n` and the new 3-line
       `<pid>\\n<timestamp>\\n<initiator>\\n` format. Returns 0 on any
       parse failure (consistent with DaemonSpawnClaim._read_existing_pid).

       Bash callers (hooks.json) should keep using `awk 'NR==1'` or
       `head -1` — they only need line 1.
       """
       try:
           content = pid_path.read_text().strip()
       except OSError:
           return 0
       if not content:
           return 0
       first = content.splitlines()[0].strip()
       try:
           return int(first)
       except ValueError:
           return 0
   ```

4. **Migrate readers** to use the helper:
   - `guard.py:1279` (`_is_guard_running_for_session`): `pid = _parse_pidfile_pid(pid_path)` instead of `int(pid_path.read_text().strip())`.
   - `guard.py:1752` (`_pid_file_points_to`): same.
   - `guard.py:1855` (`reload_self_daemon` retry path): same.
   - `doctor.py:327` (`_collect_pid_entries`): same.
   - `doctor.py:1068` (`pids_alive`): same.
   - `DaemonSpawnClaim._read_existing_pid` (spawn_lock.py:315-330): already does first-token logic; either delegate to `_parse_pidfile_pid` for DRY, or leave it (slightly different first-token-vs-first-line behaviour, both correct here).

5. **Update bash hook (plugin/hooks/hooks.json:9)** — currently:
   ```bash
   kill -0 "$(cat \"$GUARD_PID_FILE\" 2>/dev/null)"
   ```

   Change to:
   ```bash
   kill -0 "$(head -1 \"$GUARD_PID_FILE\" 2>/dev/null)"
   ```

   `head -1` extracts the first line (POSIX; works in dash, bash, zsh). Bash-side change is minimal and zero-risk for v1.8.14 single-line files (the first line IS the whole file). Bump `cozempic-hook-schema=v8` → `v9` and add migration in `cozempic init` to rewrite installed `settings.json` hook commands.

6. **Test fixtures (tests/test_hook_idempotency_shell.py:151)** — update to `head -1`-equivalent or use `_parse_pidfile_pid`. Inventory all `int(...read_text().strip())` patterns in tests/ and update them.

Note on the hook schema bump: PR #92 already bumped v7→v8 for an unrelated reason (the SessionStart probe-before-spawn). The schema constants are documented in the in-line hook comment `# cozempic-hook-schema=v8`. v9 bump is a normal incremental change, `cozempic init` already has the migration scaffolding (see `init.py`).

### Test contract

`tests/test_guard_polish_pr93.py::TestPolishPR93_PidfileMetadataParity`:

1. **`test_spawn_claim_writes_3_line_payload`** — call `DaemonSpawnClaim(sid, path).__enter__()`, read file, assert 3 lines, assert line 1 parses as `os.getpid()`, line 2 parses as `datetime.fromisoformat`, line 3 == `INIT_SPAWN_PARENT`.

2. **`test_daemon_handoff_writes_3_line_payload`** — drive `start_guard_daemon` (with mocked Popen), assert post-rename pidfile is 3 lines, line 1 == daemon PID, line 3 == `"spawn-claim-daemon"`.

3. **`test_parse_pidfile_handles_legacy_1_line`** — write `"12345\n"`, assert `_parse_pidfile_pid(path) == 12345` (backward compat with already-installed v1.8.14 pidfiles).

4. **`test_parse_pidfile_handles_new_3_line`** — write `"12345\n2026-05-19T10:00:00\nspawn-claim-parent\n"`, assert `_parse_pidfile_pid(path) == 12345`.

5. **`test_parse_pidfile_handles_garbage`** — write `"not-a-number\nfoo\n"`, assert returns `0`.

6. **`test_parse_pidfile_handles_empty`** — write `""`, assert returns `0`.

7. **`test_is_guard_running_uses_new_parser`** — write a 3-line pidfile with a live PID (use `os.getpid()` of the test runner, which we know is alive), assert `_is_guard_running_for_session` returns the right PID (not None due to parse failure).

8. **`test_doctor_collect_uses_new_parser`** — write a 3-line pidfile, run `doctor._collect_pid_entries`, assert the entry is classified correctly (live vs stale based on PID liveness, not on parse failure).

9. **`test_bash_hook_head1_extracts_pid`** — shell test (or string-equivalent Python test): write a 3-line pidfile, run `bash -c "head -1 $path"`, assert output is just the PID line.

10. **`test_hook_schema_v9_in_settings`** (migration test, sister to existing `test_init_*` tests): after `cozempic init`, installed `settings.json` hook command contains `cozempic-hook-schema=v9` AND uses `head -1` rather than `cat`.

### Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Reader I missed — silently breaks on multi-line | LOW (I enumerated all 6 sites via grep) | The shared `_parse_pidfile_pid` helper makes it easy to audit; tests cover all known sites. Adversarial round should re-grep to catch additions. |
| Bash hook `head -1` not POSIX-portable | LOW — POSIX since 1992, all macOS / Linux / WSL shells support it | Documented; no fallback needed. |
| Hook schema migration breaks existing installs | LOW — `cozempic init` migration is well-tested (PRs #84, #92) | Adversarial round should test the migration path. |
| Operator manually inspects a pre-migration pidfile after `cozempic` upgrade and the daemon was started by the OLD version | LOW — `_parse_pidfile_pid` handles 1-line legacy format | Tested explicitly (test 3). |
| Metadata write fails partially (e.g., ENOSPC mid-write) | LOW — write is to a single `os.write` call which is atomic for small payloads on local FS | Existing exception path in `_claim` already unlinks the pidfile on raised exceptions (line 297 close in try/finally; FileExistsError handled). |
| Race: reader sees pidfile mid-write between parent's `os.write` and `os.close` | EXISTS in current code too (single-byte writes are not visible until close), the atomic guarantee comes from the O_CREAT|O_EXCL ordering — file is created BEFORE any read attempt because the kernel materializes the directory entry on open | Same as today; no new race. |
| Operator's external tooling parses pidfile as single PID | MED — third parties may have scripts | Documented in CHANGELOG that pidfile is now 3 lines; first line remains the PID (forward-compatible for scripts using `head -1` or `awk 'NR==1'`). |

---

## FIX DISCIPLINE compliance check (per `~/Algo/cozempic/CLAUDE.md`)

### Class-of-bug fold rule

**Q: Are there OTHER instances of "sys.exit without pidfile unlink" beyond `_graceful_shutdown` + K=10?**

Exhaustive grep + audit (item #3 above): 3 `sys.exit` sites in `guard.py`, 0 in `spawn_lock.py`.
- `guard.py:340` — already unlinks. ✅
- `guard.py:420` — leaks. → fix in PR #93.
- `guard.py:612` — leaks. → fix in PR #93.

PLUS adjacent process-exit surfaces I found while auditing:
- `KeyboardInterrupt` block at `guard.py:664` — falls through to `finally` block (line 672), which currently does NOT unlink. → fix in PR #93 via the `finally`-based helper.
- 4 `break` paths inside main loop (file disappeared, claude exited, hard2 reload break, hard1 reload break) — same fall-through. → fix via the same `finally`-based helper.

All 6 daemon-exit paths get covered by ONE `_safe_unlink_session_pidfile` call in the `finally` block, gated by `_pid_file_points_to(session_id, os.getpid())` CAS to prevent destroying a peer's claim.

**Q: Are there OTHER instances of "dead code claimed as fast-path retained" beyond `_spawn_locks`?**

Grep for "fast-path", "retained", "kept": three hits.
- `guard.py:32` — `_spawn_locks` "kept as a fast-path" → item #2 deletes it.
- `digest.py:41` — `_DEBUG` "kept as a module-level attribute for test monkeypatching" → legitimate, monkeypatched in tests. KEEP.
- `spawn_lock.py:151` — `_spawn_lock_path` "Kept as a separate helper for diagnostics + the legacy `daemon_spawn_lock` ctx-manager test surface" → used by `daemon_spawn_lock` (line 344) AND by tests. Legitimate. KEEP.
- `spawn_lock.py:323` — comment about "legacy formats in reload_lock.py have trailing metadata" → describes the parser tolerance, not dead code. KEEP.

Only `_spawn_locks` is genuinely dead. No further folds needed.

### Sister-module parity grep

For each new payload format or constant introduced:

| New thing in PR #93 | Sister module | Convention adopted |
|---|---|---|
| `_safe_unlink_session_pidfile` helper | `reload_lock._ReloadLock.__exit__` (`reload_lock.py:193-199`) unlinks lock file on exit | YES — similar try/except OSError pattern. |
| CAS guard via `_pid_file_points_to` | `guard.reload_self_daemon` (lines 1802, 1809, 1823, 1829) | YES — same CAS pattern. |
| `HARD_LOOP_HARD_EXIT_THRESHOLD` constant | `reload_lock.WEDGE_TTL_SECONDS` (line 45) — "wedge" threshold for an upper bound | YES — same "circuit breaker on upper bound" pattern. |
| `COZEMPIC_GUARD_HARD_EXIT_K` env var | `spawn_lock._read_fresh_window_seconds` env var pattern (lines 105-115) | YES — same clamp + fallsafe pattern. |
| `INIT_SPAWN_PARENT` / `INIT_SPAWN_DAEMON` constants | `reload_lock.INIT_CLI_RELOAD` / `INIT_GUARD_HARD1` etc. (lines 48-51) | YES — same naming convention. |
| 3-line pidfile payload | `reload_lock._try_create` payload (lines 222-227) | YES — identical structure. |
| `_parse_pidfile_pid` helper | `reload_lock._read_lock_metadata` (lines 130-157) | YES — same tolerant-parse pattern, returns 0 on failure. |

All proposed new APIs follow existing conventions. No invented patterns.

### Pre-existing items folded

From `TODO.md` sweep against files touched in PR #93:

| TODO item | File/class touched | Fold decision |
|---|---|---|
| TODO.md:55 — `_graceful_shutdown` does NOT unlink pidfile | `guard.py:_graceful_shutdown` | **FOLD** — exactly item #3 scope. Mark DONE in TODO.md post-merge. |
| TODO.md:23 — FIX-O1: fail-closed at `guard.py:398` when `claude_pid=None` after re-check → `sys.exit()` | `guard.py:398` (close to K=10 path) | **DO NOT fold** — different scope (orphan reaper PR). The K=10 sys.exit addition is OK to coexist; mention in FIX-O1 note that it must be aware of the K=10 exit path (already noted in TODO). |
| TODO.md:31 — N3: `COZEMPIC_PIDFILE_FRESH_SECONDS` docstring should clarify "requires daemon restart" | `spawn_lock.py:101-115` | **Consider folding** — touching `spawn_lock.py` in item #5 anyway. 2-line docstring tweak. Low cost, fits the FIX DISCIPLINE "fold if touching" rule. **YES, fold.** |
| TODO.md:32 — M1: `os.rename(.pid.tmp, .pid)` lacks `os.fsync` on parent dir | `guard.py:1576` | **Consider folding** — touching this exact rename in item #5 (hand-off payload). Adding `os.fsync(os.open(parent_dir, O_RDONLY))` is ~3 LOC. **YES, fold** — durability + fresh edit in same hunk is cleaner. |
| TODO.md:33 — M2: `_is_pidfile_fresh` returns False on `stat()` EACCES | `spawn_lock.py:309-313` | **Consider folding** — touching `spawn_lock.py` anyway. Change `except OSError: return False` to `except PermissionError: return True` + keep `except OSError: return False`. 2 LOC. **YES, fold.** |
| TODO.md:34 — M3: `_is_cozempic_guard_process` ProcessLookupError ambiguity | `guard.py:1617-1662` | **DO NOT fold** — different file area, larger refactor (return tri-state), best done as its own audited change. Out of scope. |
| TODO.md:35 — M5: macOS without `brew install flock` documentation | hook docs | **DO NOT fold** — docs-only, separate. |
| TODO.md:56 — CLI `cmd_guard --daemon` invalid session_id prints nothing + exits 0 | `cli.py` | **DO NOT fold** — different file, different concern (CLI UX). |
| TODO.md:57 — `_digest_session` literal-path vs session ID | `cli.py:971-983` | **DO NOT fold** — different file, different concern. |

**Total folded**: 4 items (item #3 + N3 + M1 + M2 from the deferred-DA-findings list).

This aligns with "When introducing a new instance of a known bug class, the same PR MUST also fix the pre-existing known instances" — TODO.md:55 is the pre-existing leak that the K=10 leak added a second instance of. Folding it is mandatory per the rule.

---

## Commit ordering

| Commit | Item | Scope | Risk |
|---|---|---|---|
| 1 | RED tests | `tests/test_guard_polish_pr93.py` + `tests/test_guard_polish_pr93_reproducer.py` (all 4 items: spawn-locks deletion, pidfile unlink, K=10 defer, metadata parity). All tests RED on current `4614f16`. | LOW — tests only. |
| 2 | Item #2 + Item #3 | Mechanical: delete dead `_spawn_locks`, add `_safe_unlink_session_pidfile` helper + CAS gate + wire into `finally`. RED tests #2, #3 (item #2 + #3 paths) flip GREEN. | LOW — mechanical, narrow blast radius. |
| 3 | Item #5 + folded TODO items | `spawn_lock.py` payload + `guard.py` hand-off payload + new `_parse_pidfile_pid` helper + migrate all 5 Python readers + bash hook `head -1` + schema v8→v9 + `cozempic init` migration + folded N3 (docstring) + M1 (fsync) + M2 (stat EACCES). RED tests for item #5 flip GREEN. | MED — touches hook surface + init migration. Requires adversarial round. |
| 4 | Item #4 (architectural) | `HARD_LOOP_HARD_EXIT_THRESHOLD` + env var + `agents_active` defer logic + diagnostic message variants. RED tests for item #4 + reproducer scenario flip GREEN. | MED — new behaviour on a hot path. Most reviewable concern. Requires reproducer-scenario walk per FIX DISCIPLINE "Behavior verification on reproducer scenario". |

**Rationale for ordering** (per FIX DISCIPLINE "Multi-PR sequencing: trust-building order: mechanical → defensive → architectural"):

- Tests first (commit 1): standard RED-then-GREEN cadence (proven pattern on PR #92 6-step pipeline).
- Mechanical cleanup (commit 2): zero-risk dead code + leak fix. Easy to verify in review.
- Format change (commit 3): touches more sites + hook schema. Riskier, but constrained to format-change semantics. Verify with adversarial round.
- Architectural change (commit 4): only one behaviour-change commit, isolates the BMAD-with-subagents reproducer test. Architects can focus their review here.

**Total: 4 commits.** Well under the FIX DISCIPLINE 6-commit threshold for multi-PR splitting. One PR (PR #93) is correct; no split required.

Each commit is independently shippable in principle (could revert commit 4 if architectural concern surfaces; commits 1-3 stand on their own).

---

## Verification envelope (architect)

- **Confidence**: 90% (design is fully grounded; risk is in implementation drift during impl + adversarial discovery of a sister-module convention I missed).
- **Signals**:
  - Read tool on `src/cozempic/guard.py` (full file, 1918 lines, in 4 chunks)
  - Read tool on `src/cozempic/spawn_lock.py` (full file, 360 lines)
  - Read tool on `src/cozempic/reload_lock.py` (full file, 307 lines)
  - Read tool on `src/cozempic/session.py:90-141` (`_PruneLock` for cross-lock matrix)
  - Read tool on `src/cozempic/doctor.py:320-335, 1060-1075` (pidfile readers enumeration)
  - Read tool on `~/sanofi/silc-data/.claude/handoffs/cozempic-guard-crash-2026-05-18.md` (full BMAD-with-subagents reproducer)
  - Read tool on `~/Algo/cozempic/CLAUDE.md` (FIX DISCIPLINE rule full text)
  - Read tool on `~/Algo/cozempic/TODO.md` (full file — fold candidates enumeration)
  - Grep for `sys\.exit|os\._exit|SystemExit` in `guard.py` + `spawn_lock.py` (3 hits, all classified)
  - Grep for `_spawn_locks` across `src/` + `tests/` (zero producer hits, confirms dead code)
  - Grep for `pid_path\.read_text|pid_file\.read_text|_pid_file_for_session|_pid_file_for_cwd` across `src/` + `tests/` (6 reader sites enumerated)
  - Grep for `class _.*Lock|class .*Claim` across `src/` (5 sister-module classes for parity grep)
  - Grep for `fast-path|fast path|retained|kept as|legacy` across `src/` (4 hits, only `_spawn_locks` confirmed dead)
- **Cross-checked**:
  - Class-of-bug fold: enumerated ALL sys.exit paths, not just K=10 + _graceful_shutdown — found 4 additional process-exit surfaces (`KeyboardInterrupt` + 4 `break`s) that the `finally`-based helper covers.
  - Sister-module parity: every new constant, helper, payload format mirrors an existing convention (no inventions).
  - BMAD reproducer: walked the exact sequence from the handoff against Option C — verified the daemon stays alive while subagents are running, then exits hard-cap if they perma-run.
  - Pidfile reader enumeration: bash hook (`plugin/hooks/hooks.json:9`) inclusion is critical — would have been silently broken by Option 5a without the `head -1` migration. Caught.
  - TODO.md sweep: 4 pre-existing items folded into the PR scope (per FIX DISCIPLINE pre-PR audit gate).
- **Not verified** (left for implementer / validator / adversarial):
  - Test fixture mechanics for `time.sleep` mocking on the K=50 reproducer test (might need `freezegun` or a manual cycle-driver — implementer's call).
  - Hook schema v9 migration on a real `settings.json` with non-default keys (the migration code in `init.py` has handled v6→v7→v8 before, so the path is well-traveled, but adversarial should test on a corrupted/partial schema).
  - Whether the new `head -1` hook line interacts with existing `flock` invocations in the same command string (the bash command is one giant pipeline — needs eyes-on validation).
  - Whether any third-party tooling parses cozempic pidfiles externally (low probability, but a sweep of `~/.claude*/hooks/*.sh` on the operator's box is recommended pre-merge).
  - Empirical: does the BMAD-with-subagents reproducer in `tests/test_guard_polish_pr93_reproducer.py` actually replay the handoff's failure mode under the new code? Needs a live-daemon smoke similar to PR #92's V4-V6 validators.
  - Whether `extract_team_state` reliably classifies in-flight subagents as `running` vs `unknown` vs `completed` in the BMAD-with-subagents path — the defer logic depends on it. If there's a known fluke (BUG-G15-adjacent), the hard cap saves us, but adversarial should probe this.
