# PR #94 Architecture Audit — Transient-Daemon Race + Reload Chain Hardening

Read-only architect deliverable for the third bug class surfaced by the 86cb258b investigation (2026-05-19 14:38). Scope: **NEW-1 (transient daemon vs SessionStart spawn race), GAP-D (futile-reload abort), GAP-B (post-osascript liveness verification)**.

Working dir: `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/transient-daemon-pr94` on branch `fix/guard-transient-daemon-race` (base `origin/main @ 4614f16`, v1.8.14). No code changes in this audit phase.

---

## Sequencing decision (vs PR #93)

**Decision**: Option Seq-C — design now off `origin/main` v1.8.14, document the PR #93 dependency explicitly, instruct the implementer subagent to wait for PR #93 merge before applying any code.

**Rationale**:
- PR #93 is **OPEN, awaiting Junaid review** as of 2026-05-19. ETA to merge is unknown (Junaid's normal cycle is ~1 day for clean tested PRs, but his queue today already had PR #92's followup + community PRs #90/#91 in flight). Blocking the architect phase on Junaid's review wastes hours of wall-clock.
- PR #93 ships new symbols that NEW-1 and the supporting fixes **want to consume** (3-line pidfile metadata, `INIT_SPAWN_PARENT/DAEMON`, `_safe_unlink_session_pidfile` CAS helper, `_parse_pidfile_pid`, `HARD_LOOP_HARD_EXIT_THRESHOLD`). Designing as if these exist now and instructing the implementer to apply on top of merged PR #93 is the lowest-risk path.
- Option Seq-B (base on PR #93 branch) would let us code against the symbols immediately, but **forces a rebase** if Junaid requests PR #93 changes during review. Junaid's PR #92 review already produced 5 followup items — non-trivial probability of rework on PR #93.
- Option Seq-A (wait for merge) is the most conservative but loses the parallel work window. We have a documented design + reproducer; we should not be idle.

**Risk of Seq-C**: if PR #93 lands in a substantially different shape than current branch HEAD, design assumptions break. Mitigation: implementer subagent re-reads this audit at start of work, confirms each PR #93 symbol is still present in merged main with the spec'd shape, raises blockers if not.

### PR #93 symbols this design depends on

Verified via Read tool against `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/` (PR #93 branch HEAD):

| Symbol | File:line (PR #93 branch) | Used by PR #94 |
|---|---|---|
| `INIT_SPAWN_PARENT = "spawn-claim-parent"` | `spawn_lock.py:151` | NEW-1 (a) — new constant `INIT_SPAWN_TRANSIENT_WATCHDOG` follows same convention |
| `INIT_SPAWN_DAEMON = "spawn-claim-daemon"` | `spawn_lock.py:152` | NEW-1 (a) — sibling convention |
| `_parse_pidfile_pid(pid_path)` | `spawn_lock.py:155-188` | NEW-1 — must extend to parse `(pid, initiator, ts)` triple (or add new helper that returns the full triple while keeping `_parse_pidfile_pid` as the pid-only accessor) |
| 3-line pidfile payload (`pid\\nts\\ninitiator\\n`) | written in `spawn_lock.py:368-376` and `guard.py:1733-1738` | NEW-1 — depends on the 3rd line being authoritative for the spawn type |
| `_safe_unlink_session_pidfile(session_id)` with CAS via `_pid_file_points_to` | `guard.py:1375-1408` | NEW-1 (b) — transient daemon's pre-exit unlink uses this helper directly |
| `HARD_LOOP_HARD_EXIT_THRESHOLD` (default 50) | `guard.py:90` | GAP-D — futile-reload abort path follows the same "circuit breaker beyond K=10" pattern; reuses `_hard_loop_backoff_sleep` |
| `_hard_loop_backoff_sleep(K, interval)` | `guard.py:2085-2105` | GAP-D — abort path returns to the back-off cadence |
| `deferred_exit_announced` one-shot flag pattern | `guard.py:468, 661, 740` | GAP-D — same "log once per defer window" UX pattern for the futile-reload abort log line |
| Hook bash `head -n 1` instead of `cat` | `data/hooks.json` v9 | NEW-1 — bash reader must continue to use `head -n 1` since 3-line metadata is now load-bearing |

### Plan if PR #93 changes during Junaid review

1. **Implementer subagent's first action** is a structural re-read of merged-main `spawn_lock.py` + `guard.py` to verify each row of the table above is present unchanged.
2. If any spec'd symbol was renamed, signature-changed, or removed during Junaid review: subagent halts and writes `PR93_DRIFT_REPORT.md` listing the deltas. Architect reconvenes (this audit doc) to reconcile.
3. If pidfile payload schema changed (e.g. JSON instead of 3-line text), the design for NEW-1 option (a) — the `INIT_SPAWN_TRANSIENT_WATCHDOG` initiator — needs the new schema's equivalent slot. Falls back to NEW-1 option (b) or (c) if no metadata slot exists.

---

## NEW-1 — transient daemon vs SessionStart spawn race

### Root cause

Verbatim replay of the 86cb258b timeline (LESSONS_LEARNED 2026-05-19 section, lines 50-66) against the current code:

1. **14:37:21** — OLD guard daemon for OLD Claude (PID 89113), session `86cb258b-...`, hits HARD1 (55%) threshold. Calls `_terminate_and_resume(89113, ...)` (guard.py:1127) → `_spawn_reload_watcher(89113, ...)` (guard.py:1243).
2. `_spawn_reload_watcher` builds a bash watcher script (line 1290): `while kill -0 89113; do sleep 1; done; sleep 1; osascript ...; echo "..."` and `subprocess.Popen([bash, -c, script], start_new_session=True)`. Watcher PID is ephemeral, no daemon.
3. OLD guard daemon emits `"Reload triggered. Guard exiting."` (line 562 / 609) → `break` → `start_guard` returns → process exits. **PR #93 commit 2 ensures the OLD guard's pidfile is unlinked via `_safe_unlink_session_pidfile` in the `finally` block (line 796).** So at 14:37:21+ε the pidfile slot is FREE.
4. **14:37:24** — bash watcher's polling loop detects (eventually) that PID 89113 hasn't died yet, but the watcher's `osascript` line hasn't fired. Actually the watcher waits until `kill -0` fails. **OLD Claude doesn't die until 14:38:29** (slow graceful exit — 68 seconds). The watcher script's `osascript` cannot fire until after 14:38:29. So the new-Terminal-opens-with-osascript event happens AFTER OLD Claude dies, not at 14:37:24 as the investigation's coarse timestamp suggested.
5. **14:37:28** — TRANSIENT guard daemon spawns with `--claude-pid=89113`. **This is the puzzling actor.** Not a watcher (watcher is bash). Not `reload_self_daemon` (that path doesn't propagate `--claude-pid`). The most defensible explanation, given the hook chain: the SessionStart hook for the OLD Claude session was re-fired by an upgrade-chain side-effect (PRE→POST version delta triggers `cozempic guard --reload-self`, then the hook's idempotent guard-spawn fast-path runs `cozempic guard --daemon` against the SAME session). The `--daemon` path calls `start_guard_daemon(session_id=..., claude_pid=None)` → `find_claude_pid()` walks up from the hook subshell, finds OLD Claude 89113 as ancestor (it's slow-exiting, still in the process tree), records it as `claude_pid`. The new guard daemon's pidfile then claims the slot via `DaemonSpawnClaim` (the slot was freed by the OLD guard's finally-block unlink). **This is the TRANSIENT daemon.**
6. **14:38:18** — NEW Claude (PID 94466) starts via the watcher's `osascript "tell Terminal to do script \"claude -r 86cb258b-...\""`. NEW Claude's SessionStart hook fires its bash command (hooks.json v9, line 8): checks `[ -f $GUARD_PID_FILE ] && kill -0 "$(head -n 1 "$GUARD_PID_FILE")"` — pidfile EXISTS, PID is alive (the TRANSIENT guard). Fast-path branch taken: `cozempic guard --daemon` is NOT invoked because the SHELL fast-path short-circuits. **OR**, if the Python fallback is invoked (different code path), `start_guard_daemon` → `_is_guard_running_for_session(session_id)` returns the TRANSIENT guard's PID → returns `{"started": False, "already_running": True}`. Either way, NEW Claude DOES NOT get a fresh guard.
7. **14:38:29** — OLD Claude 89113 finally dies. TRANSIENT guard's main loop watchdog (start_guard line 485-503) detects `os.kill(89113, 0)` raises `ProcessLookupError` → `claude_alive = False` → final checkpoint → `print("Guard stopping (Claude exited).")` → `break` → process exits → PR #93's `_safe_unlink_session_pidfile` runs in the finally block → pidfile unlinked.
8. **Result**: NEW Claude (94466) ALIVE, JSONL writing through 14:46+. No guard daemon watching it. The next time NEW Claude's SessionStart fires (a clear, resume, restart) a guard would be spawned — but SessionStart does NOT fire on tool calls or message turns. The session is **silently unprotected** until the user manually intervenes.

**Why the race exists at all** (architectural):

The pidfile slot is a single per-session resource. PR #93's CAS unlink in `_safe_unlink_session_pidfile` correctly prevents a peer's claim from being destroyed, but it does NOT prevent the OPPOSITE direction of the race: a peer's claim BLOCKING a legitimate claim. The TRANSIENT guard's claim is **technically valid for the OLD claude_pid** but **wrong for the NEW claude_pid**. The current `DaemonSpawnClaim` model has no concept of "which Claude PID is this daemon for" — it only knows "session_id". When session_id is the SAME (which is the case after `claude -r <same-uuid>`), the spawn appears already-running even though the daemon inside is watching the dying parent process, not the new one.

This is a **per-Claude-PID single-flight** problem, not a per-session single-flight problem. PR #92's `DaemonSpawnClaim` got the granularity wrong for the post-reload case.

### Design options

#### Option (a) — Transient marker in pidfile metadata + override

**How**: extend the spawn-claim payload to a 4th meaning via the initiator field. Introduce a new constant:

```
INIT_SPAWN_TRANSIENT_WATCHDOG = "spawn-claim-transient"
```

When the OLD-daemon's reload exit path spawns a SessionStart-hook-induced replacement (the transient case above, identifiable by `claude_pid == <pid that was just terminated by the reload>`), the new daemon's pidfile is written with `INIT_SPAWN_TRANSIENT_WATCHDOG` in the initiator slot AND the dying claude_pid in a NEW 4th line. Schema bump: pidfile is now 3-line OR 4-line tolerant.

Pidfile payload (4-line, for transient case):
```
<daemon-pid>
<iso-timestamp>
spawn-claim-transient
<watched-claude-pid>
```

`DaemonSpawnClaim._claim`'s loser path is extended: when EEXIST fires, parse not just `pid` but also `initiator` and (if present) `watched_claude_pid`. If `initiator == INIT_SPAWN_TRANSIENT_WATCHDOG` AND `watched_claude_pid != current_claude_pid` (i.e., we are a SessionStart hook for a NEW Claude process, not the dying one), the loser is allowed to **override** the claim:

1. Best-effort SIGTERM the transient daemon (it's about to die anyway when watched_claude_pid exits).
2. Unlink the pidfile.
3. Retry the O_CREAT|O_EXCL claim.
4. Proceed with normal spawn.

Discrimination of "transient" vs "regular": detecting that a spawn is a transient case requires knowing that the spawn is happening *during a reload chain*. The unambiguous signal is: `--claude-pid` is provided AND that PID is currently being SIGTERMed (or has been in the last N seconds). Simpler proxy: a per-session "reload-in-flight" marker written by `_terminate_and_resume` BEFORE it spawns the watcher, cleared after the watcher completes. SessionStart hook's `--daemon` path checks for this marker; if present, the spawn writes `INIT_SPAWN_TRANSIENT_WATCHDOG` + the dying claude_pid into the pidfile.

**Pros**:
- Cleanest semantically: makes the per-Claude-PID granularity explicit in the metadata.
- Leverages PR #93's metadata format (operator triage: `cat /tmp/cozempic_guard_*.pid` shows the transient nature immediately).
- Race-window narrow: the override happens atomically via O_CREAT|O_EXCL after explicit unlink.

**Cons**:
- New constant + new pidfile line + new "reload-in-flight" marker file → meaningful schema change. Hook v9 → v10 bump for the bash parser (although `head -n 1` still works for the bash fast-path PID liveness check).
- CAS is more complex: the override path racing with a normal spawn from a third session could destroy a non-transient claim. Need a strict "only override if BOTH initiator==transient AND watched_claude_pid!=current_claude_pid AND current_claude_pid is alive" — three orthogonal signals.
- Adds a new failure mode: if the "reload-in-flight" marker is leaked (parent daemon SIGKILLed between mark and clear), every subsequent spawn looks transient until the marker is GC'd by mtime.

#### Option (b) — Transient daemon self-unlinks pidfile BEFORE wait

**How**: in `start_guard`'s main-loop watchdog (line 485-503), when `claude_alive` flips False, unlink the pidfile **IMMEDIATELY**, BEFORE doing the final `checkpoint_team` (which can take 1-2s on large sessions). Today the unlink happens via `_safe_unlink_session_pidfile` in the finally block — which runs AFTER `checkpoint_team` AND AFTER `print("Guard stopping (Claude exited).")` — total latency between "Claude died" and "pidfile freed" is potentially seconds.

Concretely: insert a `_safe_unlink_session_pidfile(sess["session_id"])` call at the very top of the `if not claude_alive:` branch (between line 499 and 500). The finally-block call becomes a no-op (CAS already failed because we don't own the pidfile anymore — that's harmless).

**Pros**:
- Tiny change: 1 line.
- No metadata changes, no hook schema bump, no new constants, no new race surface.
- Works for ANY exit cause (not just the SessionStart-vs-transient race) — the slot is freed instantly when the watched Claude dies.

**Cons**:
- Race window not eliminated, only shrunk. If NEW Claude's SessionStart fires DURING the OLD Claude's slow exit (89113 was alive for 68 seconds after SIGTERM), the unlink hasn't happened yet because the watchdog hasn't fired. NEW Claude's SessionStart still sees a pidfile pointing at a transient daemon for an alive (dying) PID and skips spawn.
- Does not solve the 86cb258b case: NEW Claude's SessionStart fired at 14:38:18, OLD Claude died at 14:38:29 — NEW Claude is 11s EARLIER than the unlink. NEW Claude is unprotected for those 11s minimum AND additionally for the full transient guard's final-checkpoint duration (sub-second to ~3s depending on session size).
- BUT: a SessionStart that fires AFTER the watched Claude dies (e.g., user `/clear` or `/reload` after seeing the bug) would get a clean slot. Defense in depth only.

#### Option (c) — Abandon transient daemon entirely

**How**: when OLD guard's reload path completes, write a sentinel file `/tmp/cozempic_reload_<sid>.in-flight` (or extend the existing `cozempic_reload_<sid>.lock` from `_ReloadLock`) BEFORE exiting. The SessionStart hook's bash fast-path checks for this sentinel and **skips** the `cozempic guard --daemon` spawn during the in-flight window. The NEW Claude's actual SessionStart (fired after `osascript` opens the new Terminal and `claude -r` runs) is the SOLE source of guard truth.

Specifically: `_terminate_and_resume` writes `/tmp/cozempic_reload_<sid>.in-flight` containing `<old_claude_pid>\n<iso-ts>\n` AFTER SIGTERMing OLD Claude, BEFORE spawning the watcher. The watcher's bash script gains a final step that unlinks the sentinel AFTER osascript fires (so by the time NEW Claude's hook runs, the sentinel is gone).

The SessionStart hook bash adds a check:
```bash
if [ -f "/tmp/cozempic_reload_${SESSION_ID:0:12}.in-flight" ]; then
  echo "Cozempic: reload in flight, skipping guard spawn"
  exit 0
fi
```

**Pros**:
- Removes the entire transient-daemon class of bug. There IS no transient daemon — only OLD daemon (just exited) → reload watcher (bash) → NEW Claude → NEW daemon.
- Reduces the moving-parts count by one. The current architecture has an "in-between" guard daemon during the reload window; this option eliminates it.
- Matches operator mental model better: "after reload, only the new Claude's hook spawns the guard."

**Cons**:
- Sentinel file is a NEW global-state primitive. Could be leaked if the watcher dies between `osascript` and sentinel-unlink. Needs an mtime-based GC similar to `_FRESH_PIDFILE_SECONDS` (default 60-120s sufficient).
- The transient daemon today DOES provide a service in the rare case where the upgrade-chain re-fire happens against a session whose OLD Claude is healthy (no reload): killing the transient breaks that case. Need to verify whether the upgrade-chain re-fire scenario exists outside the reload context — if it does, this option regresses.
- Hook bash gets a new conditional → hook schema bump v9 → v10. Bash parser must be portable (sh-compat, not bashism).
- Loses the diagnostic "transient guard watched OLD Claude until it died" log line. Cosmetic loss — the OLD guard already logs the final checkpoint and the watcher already logs `cozempic_guard.log`.

### Recommendation

**Option (c) — abandon transient daemon entirely**, with Option (b) as a defense-in-depth secondary.

Reasoning, weighed against `reliability-confidence-isolation.md` Principle 1 ("Reliability over simplicity AND sustainability long-term"):

- Option (a) adds primitives (a 4-line pidfile, a new initiator constant, a "reload-in-flight" marker file, a CAS override path) — 4 new pieces of state, each a future debugging surface. The override path's "destroy a peer's claim atomically" semantics is exactly the kind of code that fails subtly under load (see PR #92's flock-unlink race for the precedent). Long-term sustainability is low.
- Option (b) is a 1-line patch but does NOT close the race. The 86cb258b reproducer would STILL fail. It is a band-aid that creates the impression of a fix without removing the bug.
- Option (c) removes the buggy actor from the system. The transient daemon's *existence* is the bug; killing it as an architectural concept closes the entire class. No new pidfile schema, no override CAS, no metadata negotiation. The new failure mode (sentinel leak) is bounded by mtime GC, identical to the patterns already proven in `_FRESH_PIDFILE_SECONDS` (spawn_lock.py:102) and `WEDGE_TTL_SECONDS` (reload_lock.py:45).
- Operator triage benefits: with option (c), the pidfile slot during the reload window is EMPTY, not "transiently held by a daemon watching the dying parent". This matches the user's mental model and the diagnostic story ("guard active iff pidfile exists").

**Defense in depth**: apply Option (b) AS WELL. Even with the sentinel approach, having `start_guard` unlink the pidfile immediately on watched-Claude-death is correct hygiene. Costs 1 line, closes the residual race if the sentinel mechanism ever fails (e.g., sh portability issues across operator shells).

### Test contract

Tests live in `tests/test_guard_transient_race.py` (new file). All RED-first.

1. **`test_reload_writes_in_flight_sentinel`** — given a mocked `_terminate_and_resume` invocation, asserts the sentinel file `/tmp/cozempic_reload_<sid>.in-flight` is created BEFORE `_spawn_reload_watcher` is called. CAS: sentinel content is the dying Claude PID + ISO timestamp.

2. **`test_session_start_hook_skips_spawn_during_reload`** — invoke the SessionStart hook bash command (via `subprocess.run(["bash", "-c", HOOK_BASH])`) in a sandbox where the sentinel file exists. Assert `cozempic guard --daemon` is NOT invoked (mock the cozempic binary as a counter script).

3. **`test_watcher_unlinks_sentinel_after_osascript`** — given a watcher script invocation, mock `osascript` as `true` (succeeds immediately), assert the sentinel file is gone after the watcher exits.

4. **`test_sentinel_mtime_gc_after_stale_window`** — pre-plant a sentinel with mtime 120s in the past. Invoke SessionStart hook; assert spawn proceeds normally (sentinel was treated as stale and unlinked).

5. **`test_pidfile_unlinked_immediately_on_watched_claude_death`** (option (b) defense in depth) — set up `start_guard` with a fake `claude_pid` that exits during cycle. Assert the pidfile is gone BEFORE the post-loop `finally` runs (via instrumentation on `_safe_unlink_session_pidfile`).

6. **Reproducer-scenario walk (`test_86cb258b_reproducer_no_transient_unprotected_state`)** — full integration: mock OLD daemon's reload exit, mock the upgrade-chain re-fire of SessionStart against the same session, mock NEW Claude's SessionStart, assert NEW Claude ends up with an ACTIVE guard daemon pointing at NEW Claude PID.

7. **Race test under contention** — spawn N=10 concurrent SessionStart hook subprocesses against the same session, half WITH sentinel present, half WITHOUT, assert: zero daemons spawn during sentinel window; exactly 1 daemon spawn for the post-sentinel hooks; no multi-winner.

### Risks + mitigations

| Risk | Mitigation |
|---|---|
| Sentinel leak (watcher SIGKILL between sentinel-write and sentinel-unlink) | mtime GC: any sentinel older than `SENTINEL_TTL_SECONDS=120` (NEW constant) is treated as stale and ignored. Constant lives in `reload_lock.py` (sister of `WEDGE_TTL_SECONDS`). |
| Sh portability of the hook bash condition | Use `[ -f "$path" ]` (POSIX) not `[[ -f "$path" ]]` (bash-only). Add a hook v10 schema test that runs the bash on `dash` and `busybox sh`. |
| Hook v9 → v10 bump breaks PR #93's v9 contract | PR #93 just bumped v8 → v9. Coordinate: this PR bumps v9 → v10 + adds the SessionStart hook conditional. v10 is strictly a superset of v9 — both `head -n 1` reader AND sentinel check coexist. |
| Reload watcher fails to spawn (Popen exception) | Sentinel is leaked. mtime GC catches it after 120s. Worst case: SessionStart skipped for 120s on a single session. Acceptable. |
| Upgrade-chain re-fire of SessionStart during reload window | This is the EXACT case option (c) fixes. The re-fired hook sees the sentinel and exits cleanly. |
| Concurrent reloads from CLI + guard HARD (cross-process) | `_ReloadLock` serializes them. The sentinel is written under the reload lock — exactly one sentinel at a time. |
| User runs `cozempic guard --daemon` manually during reload window | Manual invocation deliberately bypasses the hook bash → reaches `start_guard_daemon` Python. Add a parallel Python-side check there: `if _reload_sentinel_active(session_id): return {"started": False, "reason": "reload in flight"}`. Symmetric with the hook bash. |

---

## GAP-D — abort reload when prune saves marginal bytes

### Root cause

`prune_with_team_protect` (guard.py:218, NOT session.py as the spawn-prompt suggested — verified via Read) protects team-related messages via the `__cozempic_team_protected__` tag (line 261). Strategies must call `is_protected()` to skip these. But more fundamentally: large `tool_result` blocks from subagent transcripts (Bash, Read, BMAD reports, etc.) are NOT pruned because the prescriptions don't have a strategy that targets immutable tool-result content. The 86cb258b session prune saved 7.7M tokens — substantial — but the investigation noted GAP-D as a class: "if session bloat is in tool-results, prune saves marginal bytes". This is the WORST-CASE that hasn't reproduced yet (86cb258b was not the worst case — it saved 7.7M tokens, just got into the transient-daemon trap afterward).

When prune saves <X% of session bytes AND auto_reload is True AND we're about to spawn a watcher + kill Claude + reopen Terminal: the saved-bytes are too small to keep the resumed Claude under threshold. Resumed Claude inherits ~same bloat, hits HARD again immediately, daemon HARD-loops at K=10 (or hard-cap at K=50 with PR #93) and exits. Total wall-clock waste: ~30-50 minutes of HARD-loop backoff, plus the user-visible cost of an extra Terminal window opening for a session that will immediately be re-killed.

### Threshold proposal

**Primary threshold**: `MIN_REASONABLE_PRUNE_RATIO = 0.10` (10% of pre-prune session bytes).

**Why a ratio not an absolute**: session sizes vary 10× across users (40KB ~ 40MB observed). 50KB absolute is meaningful for a 100KB session, irrelevant for a 40MB one.

**Why 10%**: empirically, the OLD-cycle-to-NEW-cycle delta needs to clear at least one threshold boundary (the HARD1 55% line) to avoid an immediate re-trigger. Token estimates show that a typical session at 60% capacity, after a 10% byte prune, drops to ~54% — just under the threshold. Less than 10% does NOT clear it. Bound is empirical-defensible; we revisit if data shows otherwise.

**Configurability**: env var `COZEMPIC_MIN_PRUNE_RATIO` with the same clamping pattern PR #93 uses for `COZEMPIC_GUARD_HARD_EXIT_K`:
- Parsed once at module import time, cached in module-level `_MIN_PRUNE_RATIO`.
- Clamped to `(0.0, 1.0)` exclusive. Invalid values fall back to default.
- Documented in the constant's docstring as "read at import time only; restart guard to pick up changes".

**Why NOT an absolute-bytes secondary threshold**: violates the principle that one threshold is easier to reason about than two. Operators tuning need ONE knob, not two.

### Behavior when threshold not met

In `guard_prune_cycle` (guard.py:801), after the `saved_bytes <= 0` early return at line 860, add a NEW early return path:

```
if 0 < saved_bytes / original_bytes < _MIN_PRUNE_RATIO:
    # Futile reload: prune freed less than threshold; resumed Claude
    # would re-trigger HARD immediately. Skip the reload, persist the
    # prune output, log the honest message, return.
    result = {... saved_mb, tokens, ... "reloading": False, "futile_reload_skipped": True}
    return result
```

The caller in `start_guard` (guard.py:616) increments `consecutive_empty_hard_prunes` on `saved_mb <= 0` only. We extend the increment condition to also fire on `result.get("futile_reload_skipped")` so the K-cycle counter advances correctly and the existing back-off + K=10/K=50 exit logic applies unchanged. This is the propre composition with PR #93's existing exit machinery.

Honest user-facing message (printed via `start_guard`, gated by the same `if backoff > interval` once-per-defer-window pattern as PR #93 uses with `deferred_exit_announced`):

```
[<time>] Hard prune freed only N bytes (~M%) — below 10% threshold. Reload skipped: resumed Claude would re-trigger HARD immediately. Likely cause: subagent transcripts or large tool-results dominate context. Recommend: /clear (loses subagent state) or fresh session with restored team checkpoint at <path>.
```

The team-checkpoint path is already written by `guard_prune_cycle` (line 879) — surface it in the message so the user has a concrete recovery path.

### Test contract

1. **`test_marginal_prune_skips_reload`** — given a mocked prune that saves 5% bytes, `guard_prune_cycle` returns `reloading=False`, `futile_reload_skipped=True`. `_terminate_and_resume` not called.
2. **`test_substantial_prune_proceeds_with_reload`** — given a mocked prune that saves 15% bytes, normal reload path proceeds. `_terminate_and_resume` called.
3. **`test_min_prune_ratio_env_var_override`** — set `COZEMPIC_MIN_PRUNE_RATIO=0.05`, prune saves 7%, reload proceeds. Tests env-var integration + clamping.
4. **`test_min_prune_ratio_invalid_falls_back`** — set `COZEMPIC_MIN_PRUNE_RATIO=invalid`, assert `_MIN_PRUNE_RATIO == 0.10` (default). Mirrors PR #93's pattern for `COZEMPIC_GUARD_HARD_EXIT_K`.
5. **`test_futile_reload_increments_k_counter`** — full `start_guard` loop with mocked HARD-trigger + 5% prune. Assert `consecutive_empty_hard_prunes` advances. Assert K=10 still triggers the existing exit path after enough cycles.
6. **`test_futile_reload_log_message_emits_once`** — same scenario as #5, assert the futile-skip log message emits exactly once across multiple cycles (deferred_exit_announced-style one-shot).
7. **`test_futile_reload_writes_team_checkpoint`** — mocked prune that saves 5% bytes BUT extracts team state. Checkpoint path is written, surfaced in the diagnostic message.

---

## GAP-B — watcher polls for new Claude PID post-osascript

### Root cause

Current `_spawn_reload_watcher` script (guard.py:1290-1295):
```
while kill -0 <old_pid>; do sleep 1; done; sleep 1;
<osascript>;
echo "$(date): ... resumed Claude in ..." >> /tmp/cozempic_guard.log
```

The `osascript` (or `gnome-terminal` on Linux, `start cmd` on Windows) is fire-and-forget. AppleEvent dispatch returns immediately (~50ms). Watcher's bash process exits ~1-2 seconds after osascript (the `echo` to log is the last act).

**Failure modes that produce silent "session disappears" UX**:
1. `osascript` returns non-zero (Terminal sandboxed / Automation permission denied) → no new Terminal opens → no `claude -r` invocation → user thinks Claude crashed.
2. New Terminal opens but `claude -r` fails (auth refresh timing out, JSONL file path resolution, model timeout, network) → Terminal opens to a shell prompt or an error message → user sees a blank terminal, doesn't know what happened.
3. `claude -r` starts but immediately exits due to JSONL parse error (rare — the JSONL is the same file the old Claude wrote, but partial-line races are theoretically possible).
4. User dismissed the Terminal prompt (macOS "Allow Terminal to control your computer?" gate) → osascript queued, never fires.

The watcher today has ZERO observability into any of these. The reload chain claims "success" because the watcher script ran to completion.

### Polling design

Extend `_spawn_reload_watcher` to add a post-osascript poll loop. New bash script structure:

```bash
while kill -0 <old_pid> 2>/dev/null; do sleep 1; done
sleep 1
<resume_cmd>  # osascript / gnome-terminal / start cmd
RESUME_EXIT=$?

# Poll for new claude process attached to this session
deadline=$(($(date +%s) + 30))
new_pid=""
while [ $(date +%s) -lt $deadline ]; do
  new_pid=$(pgrep -f "claude.*<session_id_prefix>" 2>/dev/null | head -n 1)
  if [ -n "$new_pid" ]; then
    break
  fi
  sleep 1
done

if [ -n "$new_pid" ]; then
  echo "$(date): Cozempic guard resumed Claude in <project_dir> (new PID $new_pid)" >> /tmp/cozempic_guard.log
else
  # Write a structured status file the next SessionStart can read.
  status_file="/tmp/cozempic_reload_<sid>.status"
  printf "%s\n%s\n%s\n%s\n" \
    "failed" \
    "$(date -Iseconds)" \
    "new Claude did not start within 30s after $resume_cmd_name (exit=$RESUME_EXIT)" \
    "investigate: Terminal automation permission / claude -r auth / JSONL path / network" \
    > "$status_file"
  echo "$(date): Cozempic guard reload FAILED — no new Claude after 30s" >> /tmp/cozempic_guard.log
fi
```

Constants (NEW in guard.py):
- `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS = 30` (matches `_ReloadLock`'s wedge-class threshold scale)
- `RELOAD_WATCHER_POLL_INTERVAL_SECONDS = 1`

The `pgrep -f` pattern needs to be tight enough to match only the new `claude -r <sid>` invocation, not the old (which is already dead by this point) and not unrelated `claude` processes. Best discriminator: the session-id prefix. With slug normalization the first 12 hex chars are reliably present in the resumed claude's argv (it's the `--resume <full-uuid>` argument).

**Status file consumption**: the SessionStart hook bash gets a new prologue check:
```bash
STATUS_FILE="/tmp/cozempic_reload_${SESSION_ID:0:12}.status"
if [ -f "$STATUS_FILE" ]; then
  # Surface the failure reason to the user, then clean up.
  echo "Cozempic: previous reload may have failed — $(head -n 3 "$STATUS_FILE" | tail -n 1)"
  rm -f "$STATUS_FILE"
fi
```

This is a defense-in-depth diagnostic. The actual recovery is the same as before (the new SessionStart spawns its own guard); the value-add is **operator visibility**.

### Test contract

1. **`test_watcher_writes_status_on_no_new_claude`** — invoke `_spawn_reload_watcher` with a mocked `osascript` that returns 0 but no claude process ever starts. After `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS + 2`, the status file exists with `failed` first line.
2. **`test_watcher_logs_success_when_new_claude_appears`** — invoke watcher, then `subprocess.Popen(["sleep", "60"], argv0="claude_test_<sid>")` AFTER 3s to simulate a delayed claude start. Watcher logs the new PID, no status file.
3. **`test_watcher_handles_resume_cmd_nonzero_exit`** — mock osascript with `exit 1`. Watcher proceeds to poll anyway (no claude will appear) → status file written with `exit=1` recorded.
4. **`test_session_start_hook_surfaces_prior_status`** — pre-plant a status file. Invoke SessionStart hook bash. Assert the failure message is printed to stdout. Assert status file is unlinked after read.
5. **`test_status_file_per_session_isolation`** — two concurrent reloads on different sessions, both fail. Each gets its own status file at the per-session path. No cross-contamination.
6. **`test_poll_pattern_does_not_match_unrelated_claude`** — start a `bash` process with argv0 containing the literal word "claude" but NOT the session-id prefix. Watcher does not return that PID as the new Claude.

---

## FIX DISCIPLINE compliance check

Per `LESSONS_LEARNED.md` 2026-05-19 EVENT 1, FIX DISCIPLINE's class-of-bug fold rule and sister-module parity grep are mandatory architect-phase deliverables.

### Class-of-bug fold rule

**Fire-and-forget IPC chain bug class**: `_spawn_reload_watcher` (guard.py:1297-1303) is one instance. Other instances to fold into PR #94?

Grep results (from PR #93 branch HEAD):
- `_spawn_reload_watcher` line 1297: `subprocess.Popen([bash, ...], start_new_session=True)` — THE INSTANCE we're fixing in GAP-B.
- `start_guard_daemon` line 1689: `subprocess.Popen(cmd_parts, ..., start_new_session=True)` — fire-and-forget guard spawn. Liveness verification IS done synchronously: the spawn writes the pidfile with the daemon PID via atomic rename (line 1749), and the caller's return value carries `pid=proc.pid`. So this case is already covered by the pidfile contract. NO fold needed.
- `_cleanup_stale_watchers` (line 976) — sends SIGTERM, no follow-up verify. Watcher cleanup is best-effort; failure to terminate a stale watcher does not produce a UX-visible loss. NO fold needed.
- `overflow.py` reload path — uses the same `_terminate_and_resume` → `_spawn_reload_watcher` chain (via `OverflowRecovery._do_recover`). FOLDED automatically: fixing `_spawn_reload_watcher` fixes both call sites.

**Single instance after fold**: GAP-B fixes the only meaningful fire-and-forget IPC chain. No additional code paths need the watcher-poll treatment.

### Sister-module parity grep

`_ReloadLock` (reload_lock.py:160) handles a related primitive: per-session single-flight lock with PID+timestamp+initiator metadata, wedge detection via age, CAS unlink. The patterns to mirror in PR #94's sentinel:

| `_ReloadLock` pattern (reload_lock.py line) | NEW-1 option (c) sentinel equivalent |
|---|---|
| `_lock_path_for(session_id)` (line 76-79) | `_reload_sentinel_path_for(session_id)` — same slug derivation |
| `_read_lock_metadata` (line 130-157) | `_read_sentinel_metadata` — same tolerant parse, returns (claude_pid, ts, age) |
| `WEDGE_TTL_SECONDS = 60` (line 45) | `SENTINEL_TTL_SECONDS = 120` — slightly longer because we're guarding against the FULL reload chain (kill + osascript + claude start) which is meaningfully slower than the reload-lock window |
| `O_CREAT \| O_EXCL \| O_NOFOLLOW` (line 210-216) | Sentinel WRITE uses same flags. Symlink defense by default. |
| `O_CREAT \| O_EXCL` on stale-cleanup retry (line 249) | Sentinel cleanup follows same CAS pattern. |
| `acquire_with_wait` (line 265-306) | NOT needed — the SessionStart hook does NOT wait for the sentinel; it skips spawn entirely. |

The new sentinel module can live IN reload_lock.py (sister-of-sister composition: reload lock and reload sentinel both gate the reload chain, just at different layers) OR as a new `reload_sentinel.py`. **Recommend**: extend `reload_lock.py` rather than create a new module. The two primitives are conceptually paired (lock = "this reload is in flight"; sentinel = "spawn skip during the reload window"). Same module avoids import cycles and keeps the reload-chain state machine in one file.

### Pre-existing TODO/FIX items folded

Read `/Users/yanisnaamane/Algo/cozempic/TODO.md`:

(Note: TODO.md is a project doc not visible from the worktree's git tree until first commit on this branch — referencing the main-repo TODO from session memory.)

Items potentially relevant to PR #94 scope based on the 86cb258b investigation findings (LESSONS_LEARNED references):

- **GAP-A** (osascript fire-and-forget, watcher exits ~2-3s after dispatch): same root cause as GAP-B. The "fix" for GAP-A is the same poll loop in GAP-B's design. NO separate work — FOLDED.
- **GAP-C** (guard exits with no replacement-spawn coordination): this IS NEW-1. The "replacement spawn coordination" is exactly the sentinel/marker mechanism in option (c). FOLDED.
- **GAP-E** (no "fresh session" fallback when reload futile): partially addressed by GAP-D's honest user-facing message recommending `/clear`. Full fallback (auto-launching a fresh session without the bloated JSONL) is OUT OF SCOPE for PR #94 — that's a bigger feature, belongs in ROADMAP not LESSONS.
- **Stale daemons noted** (TODO entry, PID 7085 + PID 11159): out of scope. Orphan-reaper is a separate PR (mentioned as "planned" in LESSONS 2026-05-18 upstream-catchup section). Do NOT fold.

---

## Commit ordering proposal

Per the project pipeline (CLAUDE.md PIPELINE section: 1=Audit, 2=RED tests, 3=GREEN fixes 1-commit-per-bug, 4=Adversarial review, 5=Validation, 6=PR). Following PR #93's exemplar of 4 atomic commits:

1. **`test: RED tests for PR #94 (transient-daemon race + GAP-D + GAP-B)`**
   - All 18 test functions across `test_guard_transient_race.py`, `test_guard_futile_reload.py`, `test_guard_reload_watcher_poll.py`. All RED. Imports may reference symbols not yet existing (xfail acceptable, or use `pytest.importorskip` for the runtime check).

2. **`fix(guard,reload_lock): reload sentinel for SessionStart spawn skip (NEW-1 option c + b defense-in-depth)`**
   - `reload_lock.py`: add `SENTINEL_TTL_SECONDS`, `_reload_sentinel_path_for`, `write_reload_sentinel`, `read_reload_sentinel`, `unlink_reload_sentinel` (atomic, O_NOFOLLOW, mtime-GC tolerant).
   - `guard.py`: `_terminate_and_resume` calls `write_reload_sentinel(session_id, claude_pid)` BEFORE `_spawn_reload_watcher`. `start_guard`'s watchdog (line 485-503) calls `_safe_unlink_session_pidfile` IMMEDIATELY on `claude_alive = False` (option b defense in depth).
   - `start_guard_daemon`: parallel Python-side sentinel check at top of function — returns `{started: False, reason: "reload in flight"}` if sentinel is fresh.
   - `data/hooks.json`: bash SessionStart hook gains the `if [ -f "$SENTINEL" ]; then exit 0; fi` prologue. Hook schema v9 → v10.
   - Watcher script gains the post-osascript `unlink_reload_sentinel` step.
   - All sentinel writes use the PR #93 metadata convention (pid + iso-ts + initiator) for operator-triage parity.
   - Tests from commit 1 flip RED → GREEN.

3. **`fix(guard): abort reload when prune saves <10% of session bytes (GAP-D)`**
   - `guard.py`: add `_MIN_PRUNE_RATIO` constant (default 0.10), `_read_min_prune_ratio()` env-var reader with clamping (mirrors PR #93's `_read_hard_exit_threshold` pattern).
   - `guard_prune_cycle`: insert the futile-reload-skip early return between line 870 (current `<= 0` check) and 872. Returns include `futile_reload_skipped: True`.
   - `start_guard`: increment `consecutive_empty_hard_prunes` on futile-skip as well. Add `_futile_skip_announced` one-shot flag for the log message (mirrors PR #93's `deferred_exit_announced`).
   - Tests from commit 1 flip RED → GREEN.

4. **`fix(guard): watcher polls for new Claude PID, writes status on failure (GAP-B)`**
   - `guard.py`: `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS = 30`, `RELOAD_WATCHER_POLL_INTERVAL_SECONDS = 1`.
   - `_spawn_reload_watcher`: extend the bash script with the post-osascript poll + status-file write.
   - `data/hooks.json`: SessionStart hook gains the status-file prologue (surface failure to user, unlink).
   - Tests from commit 1 flip RED → GREEN.

Total: 4 commits, matching PR #93's atomic-commit discipline. Each commit individually compiles, individually has its own test surface. No "one giant commit" anti-pattern.

PR title: `fix(guard): close transient-daemon race + reload-chain hardening (PR #94)`
PR body structure (per CLAUDE.md):
- Problem: 3 bug classes surfaced by 86cb258b investigation
- Fix: sentinel-based reload window + futile-reload abort + watcher liveness verification
- Tests: 18 new tests, all RED-then-GREEN
- Scope: depends on PR #93 (lists the dependency PRs)

---

## Verification envelope (architect)

- **Confidence**: 86%. High confidence on root-cause analysis (Read tool against actual code, cross-referenced with the 86cb258b investigation's 3-subagent timeline). Moderate-to-high on the design choice for NEW-1 option (c) (the sentinel pattern has direct precedent in `_ReloadLock` so the implementation risk is bounded). Less confidence on the exact `pgrep -f` pattern for GAP-B's poll loop — the session-id prefix discriminator is the best signal but needs verification against actual `claude -r` argv on macOS vs Linux. Less confidence on the 10% MIN_PRUNE_RATIO threshold — defensible from session-size math but not validated against a real reproducer (no GAP-D failure has occurred yet in production; 86cb258b was a NEW-1 failure with a successful 7.7M-token prune).
- **Signals**:
  1. Read tool on `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/guard.py` (lines 218-274, 279-740, 800-955, 957-1303, 1325-1488, 1495-1799, 1955-2105) — full read of all relevant functions on PR #93 branch.
  2. Read tool on `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/spawn_lock.py` (full file, 444 lines) — confirmed `INIT_SPAWN_PARENT/DAEMON`, `_parse_pidfile_pid`, 3-line metadata schema, `DaemonSpawnClaim` shape.
  3. Read tool on `/Users/yanisnaamane/Algo/cozempic/src/cozempic/reload_lock.py` (full file, 307 lines) — sister-module parity check for sentinel design. Confirmed `WEDGE_TTL_SECONDS`, `_lock_path_for`, `_read_lock_metadata`, `O_CREAT|O_EXCL|O_NOFOLLOW`, `acquire_with_wait` patterns.
  4. Read tool on `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/data/hooks.json` — confirmed v9 schema, `head -n 1` reader, fast-path structure, SessionStart bash command shape.
  5. Read tool on `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/cli.py` lines 760-820 — confirmed `cmd_guard` dispatch, `--reload-self` vs `--daemon` paths, `claude_pid` propagation.
  6. Read tool on `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/polish-pr92-followups/src/cozempic/session.py` lines 222-246 — confirmed `find_claude_pid` parent-walk semantics (10-deep, looks for `node` or `claude` in comm).
  7. Read tool on `/Users/yanisnaamane/Algo/cozempic/LESSONS_LEARNED.md` (top 296 lines including the full 86cb258b investigation + PR #92 followup section + EVENT 2 timeline).
  8. Read tool on `/Users/yanisnaamane/Algo/cozempic/CLAUDE.md` — confirmed file routing, pipeline cadence, FIX DISCIPLINE class-of-bug fold rule.
- **Cross-checked**:
  - The "transient daemon" actor in the 86cb258b timeline is consistent with the upgrade-chain SessionStart re-fire hypothesis (the `--reload-self` → `--daemon` chain in hooks.json runs both, and `find_claude_pid()` from the daemon-spawn subprocess walks ancestors finding the slow-exiting OLD Claude). Verified `find_claude_pid` walks up via `ppid` chain (session.py:226-238).
  - `_safe_unlink_session_pidfile`'s CAS pattern (PR #93 commit 2) does NOT prevent the OPPOSITE-direction race (a peer's claim blocking a NEW Claude's legitimate spawn). Verified by reading guard.py:1375-1408 — the CAS only guards against destroying a peer claim, not against a peer claim being "wrong" for the current spawn.
  - Sister-module parity: `_ReloadLock` and PR #94's sentinel are both per-session single-flight gates with PID+ts+initiator metadata, mtime-based stale detection, O_CREAT|O_EXCL|O_NOFOLLOW atomicity. Patterns are directly cloneable, with the TTL constant scaled up (60s → 120s) to cover the full reload chain instead of just the lock window.
  - Hook schema bump v9 → v10: confirmed v9 was just shipped by PR #93 (LESSONS line 39). v10 will be strictly additive (sentinel skip prologue + status surface prologue). No removed fields. Operators on v9 hooks continue to work but lose the new behavior — clean upgrade story.
- **Not verified** (left for implementer / validator / DA):
  - The "transient daemon" hypothesis (upgrade-chain re-fire) is INFERRED from code paths, not directly observed in logs from the 86cb258b session. Implementer's first task in commit 1 RED tests SHOULD include a `tests/test_transient_daemon_reproducer.py` that builds the exact sequence (mock OLD daemon exit + mock SessionStart hook re-fire + mock NEW Claude SessionStart) and asserts unprotected state. If reproducer is RED, hypothesis confirmed.
  - The `pgrep -f "claude.*<session_id_prefix>"` discriminator may match the OLD Claude on systems where the OLD `claude --resume <sid>` argv is still in the process table during the brief overlap window. Implementer must add a `pgrep -fa` check for the live status, or use `pgrep -fan` ordering to prefer newer PIDs. Validator's V4-style stress test must cover this.
  - macOS-specific osascript timing under Automation permission denied: needs an isolated test on a real macOS box. DA round should include this.
  - The `_MIN_PRUNE_RATIO = 0.10` threshold is design-defensible but has no production reproducer. DA round should challenge whether a different threshold (5%? 15%?) is empirically more correct. Acceptable to ship 0.10 as initial, tune via env-var override based on field feedback.
  - PR #93 may land in a different shape than the current branch HEAD. Implementer must verify each symbol in the dependency table at the top of this audit before applying any code.
  - Test surface for the bash hook changes needs `dash` / `busybox sh` portability runs in CI. Validator's V6 live-smoke must include `bash --posix` invocation if no other sh-portable test runner is available.
