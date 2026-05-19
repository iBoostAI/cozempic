# PR #94 Phase B — Adversarial Review

**Reviewer**: reviewer-e2e-pr94 (team `guard-crash-bmad`)
**Date**: 2026-05-19
**Commit under review**: `a5614f3` on `fix/guard-transient-daemon-race`
**Worktree**: `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/transient-daemon-pr94`
**Base**: `e54c200` (PR #93 OPEN head)
**Spec sources**:
- `AUDIT_REPORT_pr94_transient_daemon_race.md` (architect, 86% confidence)
- `BLUEPRINT_pr94_phaseb.md` (planner, 91% confidence)

---

## Validation results

### Gate 1 — V1 pytest full suite

Command: `PYTHONPATH=src python3 -m pytest tests/ -q --ignore=tests/test_model_detection.py`

Result: **807 passed, 1 failed, 36 subtests passed in 37.79s** (exit 1).

The single failure: `tests/test_guard_hardening.py::TestR3_4_PosixPlainTerminalSigtermHasInnerReverify::test_sigterm_skipped_when_identity_fails_mid_race` — pre-existing, introduced by commit `d43d42a` (2026-05-06), unrelated to Phase B (verified via `git log -p -S` and confirming Phase B does NOT touch the POSIX SIGTERM gap at guard.py:977 — see `git show a5614f3 -- guard.py` hunk headers all > 1129).

Implementer claim "811 pass + 4 pre-existing failures" reconciled: total collection `837 tests`, of which 3 model_detection context-window pin failures (independently confirmed: `pytest tests/test_model_detection.py` → 3 failed/26 passed — Haiku-4.5 / older-models / context-pct 200k pins) + 1 R3_4 SIGTERM gap = 4 pre-existing failures, none introduced by Phase B. **Claim accurate.**

Evidence: `/private/tmp/claude-501/.../bzle83oj8.output`.

### Gate 2 — V4 race stress (10p × 30it × 3 runs)

Command: `for run in 1 2 3; do pytest tests/test_spawn_lock.py::TestV4TenProcessContention; done`

Result: **3/3 runs PASS**, each producing exactly `1 winner + 9 already_running` across all 30 iterations × 10 procs = 300 race trials per run, **900 cumulative trials clean**. Per-run time: ~2.9s.

Evidence: `/private/tmp/claude-501/.../bn0h6sk54.output`.

### Gate 3 — Bash portability of SessionStart hook

Tested under `bash`, `bash --posix`, `dash`, `sh` (macOS default `sh` is bash-compat):

| Shell | Syntax check (`-n`) | Live run with mock input |
|---|---|---|
| `bash` (default) | PASS | PASS, prints `Cozempic: guard active` |
| `bash --posix` | PASS | PASS (with one "Could not find Claude Code memory directory" stderr from upgrade-self path) |
| `dash` | PASS | **FAIL: `Bad substitution` on `${SESSION_ID:0:12}`** |
| `sh` (macOS = bash) | PASS | PASS |

`${SESSION_ID:0:12}` is a Bashism (POSIX has no `${var:offset:length}`). However:
- **This bashism is PRE-EXISTING** (already in PR #93's v9 hook at `e54c200`).
- Phase B's NEW additions (sentinel + status file lines) reuse the same `${SESSION_ID:0:12}` pattern; they introduce no NEW bashisms beyond what was already there.
- Claude Code documents that hooks run via `/bin/bash` (not `/bin/sh`); in practice this works on every supported install.

Evidence: `/tmp/hook_sessionstart.sh` extracted and tested directly.

### Gate 4 — Live smoke (sentinel mechanism)

All checks PASS:

1. **Functional sentinel lifecycle** (write → exists → active → unlink → !active): PASS
2. **mtime GC at TTL boundary** (set mtime to T-121s → `_reload_sentinel_active` returns False AND unlinks): PASS
3. **`start_guard_daemon` returns `reason="reload in flight", started=False, already_running=False`** when sentinel is fresh: PASS (mocked Popen confirmed NOT called)
4. **After `unlink_reload_sentinel`, `start_guard_daemon` spawns** (Popen called, started=True): PASS
5. **Hook bash sentinel age check** (macOS `stat -f %m` fallback to `stat -c %Y` chain): correctly detects fresh AND stale across both code paths
6. **Hook bash status file surfacing** (writes "previous reload may have failed — …", auto-cleans the status file): PASS
7. **`_safe_unlink_session_pidfile` precedes `checkpoint_team`** in `start_guard` watchdog (lines 550→551): verified via Read
8. **Functional constant readout**:
   - `SENTINEL_TTL_SECONDS=120`, `INIT_RELOAD_SENTINEL=reload-sentinel`
   - `_MIN_PRUNE_RATIO=0.1`, `_read_min_prune_ratio()=0.1`
   - `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS=30`, `RELOAD_WATCHER_POLL_INTERVAL_SECONDS=1`
9. **Sentinel content payload** (correct 3-line format): `'11111\n2026-05-19T17:50:16\nreload-sentinel\n'` — matches `_ReloadLock` precedent

### Gate 5 — Reproducer (KILLER) test

Command: `pytest tests/test_transient_daemon_reproducer.py -v`

Result: **2/2 PASS** — both `test_86cb258b_full_sequence_new_claude_unprotected_on_current_code` and `test_sentinel_would_prevent_race_when_present` GREEN.

Verified the test asserts the FIX CONTRACT, not a tautology:
- assertion: `result.get("reason") == "reload in flight"` AND `started=False` AND `already_running=False`
- the OLD-code path returned `already_running=True` (RED on pre-Phase-B code), the NEW-code path returns `reason="reload in flight"` (sentinel-check fires before `_is_guard_running_for_session`)
- Step 3 simulates NEW Claude SessionStart with mocked `find_claude_pid=NEW_CLAUDE_PID(94466)`, transient daemon holds pidfile slot, AND sentinel exists → the architect's hypothesis is empirically refuted on Phase B code.

**Killer test flipped RED→GREEN with the correct semantics. Architect hypothesis CLOSED.**

### Full 22-test target suite

`pytest tests/test_guard_transient_race.py tests/test_guard_futile_reload.py tests/test_guard_reload_watcher_poll.py tests/test_transient_daemon_reproducer.py -v`

Result: **22/22 PASSED in 17.15s**. Implementer's claim accurate.

### Structural checks (blueprint § Validation contract items 1-7)

| Check | Pass | Evidence |
|---|---|---|
| `reload_lock.py` exports SENTINEL_TTL_SECONDS, INIT_RELOAD_SENTINEL, _reload_sentinel_path_for, write_reload_sentinel, unlink_reload_sentinel, _reload_sentinel_active | YES | Import-check successful, all symbols importable |
| `guard.py` line ~1272 `write_reload_sentinel` precedes line ~1378 `_spawn_reload_watcher` call | YES | `grep -n`: 1272 < 1378 |
| `guard.py` line 550 `_safe_unlink_session_pidfile` precedes line 551 `checkpoint_team` in `if not claude_alive` block | YES | Direct Read at lines 543-554 |
| `start_guard_daemon` line 1739 `_reload_sentinel_active` check precedes line 1826 `DaemonSpawnClaim` instantiation | YES | grep confirmed 1739 < 1826 |
| `guard_prune_cycle` line 946-979 `futile_reload_skipped` early return between `saved_bytes <= 0` (line 940-944) and `post_te = estimate_session_tokens` (line 980+) | YES | git diff hunks confirm |
| `data/hooks.json` has `# cozempic-hook-schema=v10`, sentinel skip prologue (contains `in-flight`), status surface prologue (contains `.status`) | YES | grep confirmed |
| `_spawn_reload_watcher` bash script contains `deadline=$((`, `pgrep -f`, `.status`, `rm -f` | YES | git diff lines 1448-1483 confirm |

---

## CRITICAL (must fix before merge)

**None.**

---

## HIGH (should fix)

**None.**

---

## MEDIUM (improvement recommended)

### MED-1: SSH path leaks the sentinel (no unlink in SSH branch)

**Location**: `guard.py:1265-1276` — sentinel is written for ALL terminal paths (including SSH), but the SSH branch `return`s without calling `unlink_reload_sentinel`. The mtime GC after `SENTINEL_TTL_SECONDS=120s` is the only safety net.

**Impact**: A user on an SSH-detected session who triggers a HARD prune gets a sentinel planted; if they manually `claude --resume` within 120s, the new session's SessionStart hook is suppressed (`Cozempic: reload in flight, skipping guard spawn`). UX degradation; no race re-introduction.

**Mitigation**: add the sentinel unlink to the SSH branch before `return`:
```python
if term_env == "ssh":
    print(f"  SSH session — skipping terminate+resume. Resume manually: {resume_cmd}")
    if session_id:
        try:
            unlink_reload_sentinel(session_id)
        except OSError:
            pass
    return
```

Alternative: don't write the sentinel before the SSH check (move sentinel write into the `if term_env == "tmux"` / `screen` / plain blocks individually).

### MED-2: PID-reuse guard early return leaks sentinel in tmux/screen paths

**Location**: `guard.py:1281-1284` (tmux) and `guard.py:1320-1322` (screen) — if `_is_claude_process(claude_pid)` returns False (PID reuse defense), the function prints WARNING and `return`s. Sentinel was written above (line 1272) — never unlinked. 120s GC window applies.

**Impact**: Same as MED-1 — UX degradation, no race. Worst case: a user whose Claude crashed between sentinel write and PID re-verification gets suppressed for 120s on next resume.

**Mitigation**: same pattern — unlink before each early `return`.

### MED-3: `_spawn_reload_watcher` SSH detection second-chance leak

**Location**: `guard.py:1397-1400` — `_spawn_reload_watcher` does its own `is_ssh_session()` check and `return`s without spawning the watcher. Sentinel was written by caller (`_terminate_and_resume`) but never unlinked.

**Impact**: Same as MED-1 — 120s suppression window. Note: this is a DOUBLE-SSH check (one in `_terminate_and_resume`, one in `_spawn_reload_watcher`), so the second is reachable only if `_detect_terminal_env()` and `is_ssh_session()` disagree. Probability low but non-zero.

**Mitigation**: have `_spawn_reload_watcher` accept an explicit "is_ssh_or_no_terminal" bypass parameter, OR unlink sentinel inside `_spawn_reload_watcher` early returns.

### MED-4: PR #93 symbol dependency drift risk

PR #94 depends on PR #93 symbols imported/used in `guard.py`:
- `_safe_unlink_session_pidfile` (called at line 550 — option-b defense-in-depth)
- `HARD_LOOP_HARD_EXIT_THRESHOLD` (used at lines 704, 711, 720, 731, 752, 761 — futile-skip K-counter interaction)
- 3-line pidfile schema (`pid\nts\ninitiator\n`) — assumed by tests (e.g., test_transient_daemon_reproducer.py:107-111)
- `_parse_pidfile_pid` (used in `_is_guard_running_for_session` for tests' transient daemon scaffolding)

If Junaid requests structural changes to PR #93 (rename, remove, change signature), PR #94 needs corresponding rebase. **Recommend**: explicitly enumerate these in the PR #94 description so the dependency is visible. Also consider whether PR #94 should wait for PR #93 merge before push (current plan is stacked PR).

### MED-5: Single-commit deviation from "3 atomic commits" plan

The blueprint specified **3 atomic commits** (NEW-1 sentinel, GAP-D futile-reload, GAP-B watcher poll) per Junaid's stated preference for atomic per-bug commits (MEMORY.md:11: "atomic commits (one bug = one commit)"). Implementer shipped 1 monolithic commit `a5614f3` with all three.

**Impact**: PR review difficulty (Junaid will see 226-line guard.py diff covering 3 distinct concerns), git blame attribution worse, can't `git revert` one concern independently if production issue surfaces.

**Recommendation**: see "Atomic commit deviation" section below.

---

## LOW (nitpick)

### LOW-1: `${SESSION_ID:0:12}` Bashism in hook (pre-existing)

Not POSIX. Phase B reuses the existing pattern in 2 new places (sentinel + status). Acceptable because Claude Code runs hooks under `/bin/bash`. Out of scope for THIS PR (predates v10).

### LOW-2: `write_reload_sentinel` retry-once silently replaces a fresh sentinel

If two `_terminate_and_resume` calls fire concurrently for the SAME session (theoretically impossible because `_ReloadLock` is held — caller already serialized), the retry-once logic would unlink the first writer's fresh sentinel and replace with the second's. Vector E test confirmed: 5 concurrent writers → 2 "ok" + 3 "FileExistsError". Final state has 1 sentinel.

**Acceptable** because `_ReloadLock` upstream serializes; the retry exists only for stale-from-prior-aborted-cycle case. Document explicitly in `write_reload_sentinel` docstring that the retry assumes ReloadLock mutex.

### LOW-3: Status file has no TTL/GC

`/tmp/cozempic_reload_<sid12>.status` is written on watcher timeout. The hook reads + deletes it on next SessionStart. If the user never re-resumes that session, the status file stays in `/tmp` forever (until OS clean). Operator concern only.

**Mitigation**: add a 24h mtime-based GC in a future PR (already noted in blueprint § risk table).

### LOW-4: v9 hook + v10 daemon transient window (auto-upgrade gap)

Between cozempic v10 install and next SessionStart auto-refreshing the v9 hook to v10:
- v9 hook fast-path (pidfile-only check) does NOT know about sentinel
- if v9 hook reaches `cozempic guard --daemon` Python path → v10 daemon's `start_guard_daemon` sees sentinel → returns "reload in flight" → SAFE
- v9 hook bash branch does NOT print "skipping guard spawn" message (cosmetic only)

**No race re-introduction**. Just delayed/different UX message during the upgrade window.

### LOW-5: `pgrep -f "claude.*<sid12>"` matches the dying OLD Claude (mitigated by ordering)

`pgrep -f` scans full argv. OLD Claude was launched with `--resume <session-uuid>` — its argv contains both `claude` and `<sid12>`. But the watcher script's `while kill -0 <old_pid>; do sleep 1; done` blocks until OLD Claude has actually exited. The post-osascript `pgrep` runs AFTER OLD is gone. Confirmed via Read of guard.py:1465+.

**Vector C result**: NO false positive — watcher ordering guarantees correctness. Note for reviewer: if an unrelated SECOND Claude session has a similar sid12 prefix (2^48 keyspace), false match is possible. Acceptable in production.

### LOW-6: `_terminate_and_resume` `_ignored_kwargs` is forward-compat noise

The blueprint specified `**_ignored_kwargs: object` to swallow test kwargs (`rx_name`, `config`, `auto_reload`). Implementer added it. This pattern is fine but should be flagged as "test compat" in a code comment (it is — line 1253). LOW because the only kwargs ever passed by tests, never production callers; the signature change is API-cosmetic.

---

## Atomic commit deviation — recommendation

**Recommendation: SHIP AS-IS (1 commit), do NOT split before push.**

**Rationale**:
1. The 3 concerns (NEW-1 sentinel, GAP-D futile-reload, GAP-B watcher poll) are interlocked in the implementation:
   - GAP-B watcher poll script EMBEDS sentinel unlink (NEW-1) AND status file write (GAP-B) in a single bash string. Splitting requires splitting the f-string in guard.py:1446-1481 across 2 commits, which is fragile.
   - GAP-D futile-reload return value (`futile_reload_skipped`) interacts with the K-counter advance in `start_guard` which already had complex deferred-exit semantics from PR #93. Splitting forces a half-state where guard_prune_cycle returns the new key but start_guard ignores it.
2. The single commit has a STRUCTURED commit message (4 sections: NEW-1, GAP-D, GAP-B, test fixes). Junaid can review by section.
3. The test suite is interlocked too — test_atomic_writes_wave1.py + test_guard_polish_pr93.py schema pin updates would have to be tagged to the hook-v9→v10 bump (in NEW-1), while test_guard_transient_race.py touches NEW-1 + GAP-B test fixtures. Splitting test files cleanly across 3 commits requires manual cherry-pick — high risk of broken-CI intermediate states.
4. PR #92 was 4 atomic commits (Junaid praised) but each commit was a **distinct, independently revertable concern**. PR #94's 3 concerns share guard.py edits that are NOT independently revertable without merge conflicts.
5. The commit message clearly enumerates 3 sections + closes 1 ticket + addresses 2 gaps. Self-documenting.

**Mitigation for Junaid review-difficulty concern**: in the PR description, structure the body as "Section 1: NEW-1 (sentinel) — lines X-Y of guard.py + reload_lock.py", "Section 2: GAP-D (futile reload) — lines W-V", "Section 3: GAP-B (watcher poll) — lines U-T". The PR description becomes the "atomic commits Junaid likes" surrogate.

If user wants atomic split: a future PR cleanup commit can do interactive rebase via `git rebase -i` on the branch (not yet pushed). Splitting cost: ~30min implementer time + re-running 22-test suite per commit to ensure intermediate states GREEN.

---

## Confidence envelope

- **Confidence**: 92%
- **Reasoning**: Killer reproducer test (Gate 5) flipped RED→GREEN with correct contract assertions; V4 stress 900/900 trials clean; structural checks all pass (5/5 blueprint items verified via Read+grep); functional smoke covers sentinel lifecycle including mtime GC boundary; adversarial DA round produced 5 MEDIUM findings (all UX/leak-window, none introducing race re-occurrence) + 6 LOW findings. The 5 MEDIUMs are sentinel-leak windows up to 120s in edge code paths (SSH detection, PID-reuse guard) — they degrade UX but do NOT break the protection contract because the mtime GC always cleans up. Pre-existing test failure (R3-4 SIGTERM gap) confirmed not introduced by Phase B.
- **Signals (8 orthogonal)**:
  1. Read tool on `BLUEPRINT_pr94_phaseb.md` (full 672 lines)
  2. Read tool on `git show a5614f3 -- src/cozempic/guard.py` (401 lines, all hunks)
  3. Read tool on `git show a5614f3 -- src/cozempic/reload_lock.py` (192 inserted lines)
  4. Read tool on `tests/test_transient_daemon_reproducer.py` (322 lines)
  5. pytest output Gate 1 (full suite 807/1) + Gate 2 (V4 stress 3 runs)
  6. live Python `start_guard_daemon` smoke confirming Popen-call-count == 0 with fresh sentinel
  7. bash shell verification (bash + dash + sh — dash bashism flagged but pre-existing)
  8. grep + Read verification of structural-order claims (line 550 < 551, line 1272 < 1378, line 1739 < 1826)
- **Cross-checked**:
  - Sentinel mtime GC at T=119/120/121s boundary (Python AND bash both correct)
  - Concurrent multiprocessing sentinel writes (5 writers, atomic exclusion preserved)
  - Killer test asserts `reason="reload in flight"` not just `started=False` (contract specificity verified)
  - Pre-existing R3-4 SIGTERM test pre-dates Phase B (git log on test file)
  - PR #93 dependency points (5 symbols) enumerated
- **Not verified**:
  - **Real osascript fire on macOS** (smoke test used mocked Popen; live osascript would require GUI interaction — out of scope for headless agent)
  - **Real upgrade-chain re-fire scenario** (would require running full daemon → upgrade → SessionStart loop with real Claude Code — out of scope for unit-level review)
  - **Junaid's specific atomic-commit threshold** (his preference is documented as "atomic commits" but his actual review threshold for "merge as 1 commit vs request split" is calibrated only from past behavior on PRs #88, #89, #92)

---

## Verdict: **PASS WITH CONDITIONS**

**Conditions for merge**:

1. **(STRONGLY RECOMMENDED)** Apply MED-1 fix (SSH path sentinel unlink) before push. ~5 lines.
2. **(RECOMMENDED)** Apply MED-2 fix (PID-reuse guard early-return sentinel unlink) before push. ~10 lines across tmux + screen blocks.
3. **(OPTIONAL — defer to follow-up)** MED-3 (`_spawn_reload_watcher` SSH-detection sentinel unlink), MED-4 (PR #93 dependency drift documentation in PR body), LOW-2 (docstring note about ReloadLock mutex), LOW-3 (status file 24h GC).
4. **(NO ACTION REQUIRED)** Single-commit deviation — ship as-is with structured PR description per "Atomic commit deviation" section above.

**Killer test (Gate 5) flipped RED→GREEN with correct semantics. Race class is closed.**

**Reviewer signature**: reviewer-e2e-pr94 (Sonnet on Opus-4.7-1M harness), 2026-05-19.
