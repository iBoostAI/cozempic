# AUDIT — Guard Memory Leak (PR #89)

Scope: empirical RSS leak in `cozempic guard` main loop (`guard.py:445`). Every 30 s the loop calls `checkpoint_team(session_path, quiet=True)` which full-reads the session JSONL via `load_messages()`. On the live victim PID 31934, RSS reached 3.13 GB after 22 h with a 40 MB JSONL. Pre-team repro: 120 cycles → +587 MB RSS, `gc.collect()` + `malloc_zone_pressure_relief` do not reclaim (libmalloc LARGE_REUSABLE cache holds freed chunks). FIX-L1 repro (byte-offset incremental + bounded cache): 50 cycles → +100 MB total, flat slope — **60× improvement**.

## 1. Caller classification

`grep -rn "load_messages" src/cozempic/` enumerates every call site. Table below classifies each caller as **scan-only** (reads → derives state → discards; safe for incremental cache) or **mutation** (reads → mutates → writes back via `save_messages(snapshot=…)`; MUST retain full-read to preserve append-safe semantics).

| file:line | caller | category | rationale |
|---|---|---|---|
| `session.py:471` | `load_messages` (def) | **definition** | public signature, stays unchanged |
| `guard.py:128` | `checkpoint_team` | **scan-only** ★ | hot path; called every 30 s from main loop; only feeds `extract_team_state`; discards `messages` on return |
| `guard.py:318` | `reload_self_daemon` startup | scan-only, one-shot | runs once at guard startup; not a leak source; leave as full-read for simplicity |
| `guard.py:645` | `guard_prune_cycle` | **mutation** | paired with `snapshot_session()` at :637 and `save_messages(…, snapshot=snap)` at :684 — MUST stay full-read |
| `cli.py:220, 238, 254, 270, 365, 466` | cmd_status / cmd_diagnose / cmd_treat / cmd_strategy / cmd_reload | one-shot CLI | process-exits after one call; not a leak source |
| `cli.py:1000, 1012` | cmd_digest update / flush | one-shot CLI | same |
| `doctor.py:695` | fix_orphaned_tool_results | mutation | paired with `save_messages` at :699 |

★ **Only one caller is both hot AND scan-only: `checkpoint_team` at guard.py:128.** That is the full scope of FIX-L1 at the call-site level. The brief mentioned `overflow.on_file_growth` as a second consumer — re-verified at `overflow.py:148`: it uses `quick_token_estimate` (tail read) + a tail-read `detect_overflow()`. It does **not** call `load_messages`. **Ignore the brief's overflow claim — one call site only.**

## 2. Cache architecture

**Module-global dict** in `session.py`, keyed by resolved `Path`, protected by `threading.Lock` (guard is single-threaded today, but the lock is cheap and future-proof against a hypothetical tail-reader thread).

```python
@dataclass
class _CacheEntry:
    messages: list[Message]      # parsed list (same shape load_messages returns)
    offset: int                  # byte offset after the last fully-parsed newline
    mtime_ns: int                # os.stat mtime at last read
    size: int                    # os.stat size at last read
    inode: int                   # os.stat st_ino — catches os.replace rewrites

_INCR_CACHE: dict[Path, _CacheEntry] = {}
_INCR_LOCK = threading.Lock()
MAX_CACHED_MESSAGES = 5000       # drop oldest when exceeded, per session
```

**Eviction strategy** — bounded per-session, not global. When `len(entry.messages) > MAX_CACHED_MESSAGES`, truncate to the newest `MAX_CACHED_MESSAGES`. `extract_team_state` walks the whole list but team detection does not depend on having every historic message — recent messages dominate. This caps worst-case allocation at ≈ `MAX_CACHED_MESSAGES × avg_msg_bytes` per live session (~5–20 MB typical; 50 MB worst case on pathological sessions). **Crucially, eviction does not change byte-offset invariants**: we keep tracking `offset` against the real file, we just don't keep every parsed message in memory.

**Rewrite detection** — compare `(inode, size, mtime_ns)` triple against the cache entry. Trigger full re-read (clear entry, restart) when ANY of:
- `inode != cached.inode` → `os.replace()` happened (prune cycle rewrote file)
- `size < cached.size` → file shrank (truncation, prune without inode change, rare)
- `mtime_ns < cached.mtime_ns` → clock skew / filesystem weirdness; conservative full re-read
- `size == cached.size and mtime_ns == cached.mtime_ns` → no work; return cached list as-is
- `size > cached.size` → seek to `cached.offset`, read forward, parse, append to cache; update offset/mtime/size

## 3. FIX-L1 spec — `load_messages_incremental`

New function in `session.py`:

```python
def load_messages_incremental(path: Path) -> list[Message]:
    """Return the full parsed message list using a byte-offset cache.

    Semantically equivalent to load_messages(path) for the happy-path: same
    list shape, same ordering, same error handling. Differs only in that it
    avoids re-reading bytes already seen. Falls back to a full load_messages()
    on any cache-miss signal (inode change, size shrink, mtime regression,
    or first read).
    """
```

Contract:
- **Output equivalence**: for a file that has only grown since last call, `load_messages_incremental(p) == load_messages(p)` element-by-element (including `(line_index, msg, bytesize)` tuples).
- **Cache miss**: inode/size-shrink/mtime-regression → clear entry, full re-read, repopulate.
- **Partial-line tail safety**: if the file ends mid-line (Claude is writing), read up to the last `\n` only; `offset` advances to that position; next cycle picks up the rest.
- **MAX_LINE_BYTES enforcement**: same 10 MB cap as `load_messages`.
- **Exception parity**: `json.JSONDecodeError` is swallowed the same way (stored as `{"_raw": …, "_parse_error": True}`) to preserve save-roundtrip compatibility.
- **Thread safety**: whole function body under `_INCR_LOCK`.

Wiring: `guard.py:128` changes from `load_messages(session_path)` to `load_messages_incremental(session_path)`. **No other call sites change.**

## 4. FIX-L2 spec — `_is_claude_process` mtime fallback

Current `_is_claude_process` at `guard.py:1493` uses `ps -p <pid> -o args=`. On macOS this can drift: if the parent Claude process forks a subshell whose args no longer contain the `claude-code` marker (observed on live PID 58060), `_is_claude_process` returns `False` even though the JSONL is actively being written.

**Fallback logic** (append to existing `_is_claude_process` after the current `ps` branch returns `False`):

```python
# ps said not-claude — check JSONL recency as liveness corroboration
try:
    mtime_age = time.time() - session_path.stat().st_mtime
    if mtime_age < 60:  # JSONL written within last minute → Claude is alive
        return True
except OSError:
    pass
return False
```

Contract:
- Only fires when `ps` detection has ALREADY returned False.
- Requires `session_path` in scope — needs either a closure or explicit `session_path: Path | None = None` parameter with default behavior unchanged when unset.
- Signature extension: add optional `session_path: Path | None = None` — all 7 call sites in `guard.py` pass `session_path` (in scope at every call site).
- Fallback window: 60 s is conservative. Below Claude's typical idle-write interval (~30 s) + one full cycle margin.

## 5. Test contracts (6 RED tests for Phase 2, `tests/test_guard_leak.py`)

1. **`test_incremental_equivalence_on_append`** — write 10 lines, call `load_messages_incremental`; append 5 more lines, call again; assert result equals `load_messages(path)` exactly (list-of-tuples equality).
2. **`test_incremental_rewrite_detection_via_inode`** — populate cache; `os.replace()` a smaller file at the same path; next call must full-re-read and return the new content.
3. **`test_incremental_size_shrink_detection`** — populate cache, then truncate file to half its size (same inode, via `open('r+').truncate()`); next call must full-re-read.
4. **`test_incremental_partial_line_tail_safe`** — write 5 complete lines + "half-written{\"partial\":" (no trailing newline); call; assert exactly 5 messages and offset stops at last `\n`; append the rest; call again; assert 6 messages.
5. **`test_incremental_bounded_cache_eviction`** — append 6000 messages (exceeds MAX_CACHED_MESSAGES=5000); assert `len(_INCR_CACHE[path].messages) == 5000` and that the newest 5000 are retained; full equivalence with `load_messages` is NOT asserted here (eviction is the whole point).
6. **`test_is_claude_process_mtime_fallback`** — mock `subprocess.run` to return non-claude args; create a JSONL with fresh mtime; assert `_is_claude_process(pid, session_path=p)` returns True. Then age the mtime (> 60 s via `os.utime`); assert returns False.

## 6. Risk analysis

| Risk | Likelihood | Mitigation |
|---|---|---|
| Stale cache mid-prune | LOW | prune cycle uses `load_messages` (mutation path), not incremental; cache on next `checkpoint_team` detects inode change from `os.replace` and full-re-reads |
| Unbounded growth across many sessions | LOW | `MAX_CACHED_MESSAGES` per-session cap; only one session active per guard daemon; multi-session guard does not exist today |
| Claude rewrites file in-place without rename | VERY LOW | size-shrink check catches truncation; mtime-regression check catches backdated writes |
| Partial trailing line re-parsed repeatedly | MITIGATED | offset advances only to last `\n`; re-reading the same partial bytes is O(bytes) not O(file) |
| Concurrency: guard thread + hook child process both call incremental | MITIGATED | module-global lock serializes cache access; worst case is serialization, not corruption |
| FIX-L2 false-positive: dead Claude, fresh mtime from some other writer | VERY LOW | JSONL path is Claude-exclusive; no third-party writers; 60 s window short enough to self-heal |

## Verification

- **Confidence**: 92% — audit is mechanical grep + read; the remaining 8% covers edge cases the RED tests are designed to surface (cache corner cases, FIX-L2 Windows-path behavior).
- **Signals**: (1) `grep -rn "load_messages" src/cozempic/` enumerated all 18 call sites; (2) read `session.py:471-487` (load_messages), `session.py:34-77` (_FileSnapshot pattern for mtime/size/inode design precedent), `guard.py:108-153` (checkpoint_team), `guard.py:634-695` (guard_prune_cycle mutation path), `guard.py:1493-1531` (_is_claude_process), `overflow.py:100-172` (confirmed no load_messages call); (3) empirical repro from brief: `/tmp/leak_repro_v2.py` +587 MB/120 cycles, `/tmp/fix_l1_repro.py` +100 MB/50 cycles; (4) existing test pattern at `tests/test_session.py:45-67` confirms fixture style.
- **Cross-checked**: brief claim "overflow.on_file_growth uses load_messages" is FALSE — `overflow.py:148` uses tail reads only. Report corrected. Claim "120 cycles → +587 MB" matches team-brief data point. `_FileSnapshot` (session.py:34) already uses (inode, size) — FIX-L1 cache design reuses the same invariants + adds mtime_ns for early-exit.
- **Not verified**: Windows behavior of `_is_claude_process` mtime fallback — the `_is_claude_process_windows` branch returns `True` as liveness fallback already, so adding mtime corroboration on the POSIX path only is the safe minimum. RED test #6 is POSIX-only (skip on Windows).
