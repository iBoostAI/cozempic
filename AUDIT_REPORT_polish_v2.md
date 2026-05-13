# polish-v2 Audit Report — PR-A

**Scope**: 7 parked bugs against v1.8.9 (commit f67aa72). Branch `feat/polish-v2`.
**Targets**: `src/cozempic/digest.py` (983 LOC) and `src/cozempic/guard.py` (1656 LOC).
**Methodology**: full symbol re-read at each hint line, cross-reference of callers, lexical/semantic repro of each failure mode in a throwaway REPL, and whole-worktree grep for dead-code / caller-count claims.
**NOTE**: This file previously held the v1.8.7 guard audit (de38d56). It is overwritten here to avoid two stale reports coexisting in the same worktree — the prior audit's findings all landed in v1.8.8/1.8.9 and need no further action.

---

## Verdict summary

| Bug | File:line | Verdict | Severity | Risk flag |
|---|---|---|---|---|
| BUG-9 | digest.py:755,772-773 | REAL | LOW | — |
| A12 | digest.py:175 | REAL | MED | — |
| A2 | digest.py:322 | REAL | LOW | — |
| BUG-13 | digest.py:95-101 | REAL | MED | ID sequence change — verify no test pins 4-digit format |
| BUG-12 | digest.py:349-350 | REAL | MED | BREAKING for users with digit-prefixed rule text |
| BUG-G13 | guard.py:1067-1070 | REAL | HIGH | Path-traversal possible with adversarial session_id |
| BUG-G16 | guard.py:1141-1143 | REAL | LOW | Zero callers in src/ AND tests/ — safe to delete |

All 7 bugs are REAL and in scope. One carries a scope-change risk (BUG-12), two carry minor invariants to preserve (BUG-13 ID format, BUG-G13 normal-path backward compat). Details below.

---

## BUG-9 — `update_digest` does not persist session_id / migration on rejected-only runs

**Location**: `src/cozempic/digest.py:755` (mutation) and `:772-773` (persist gate).
**Verdict**: REAL

### Current code
```python
def update_digest(
    messages: list[Message],
    since_turn: int = 0,
    project_dir: str = "",
    session_id: str = "",
) -> tuple[int, int, int]:
    """Extract corrections from messages and update the digest store.

    Returns (new_rules, upvoted, rejected).
    """
    store = load_digest_store(project_dir)
    store.session_id = session_id                       # ← line 755: mutation

    candidates = extract_corrections(messages, since_turn=since_turn)

    added = 0
    upvoted = 0
    rejected = 0

    for rule in candidates:
        result = admit_rule(rule, store)
        if result == "added":
            added += 1
        elif result == "upvoted":
            upvoted += 1
        else:
            rejected += 1

    if added > 0 or upvoted > 0:                        # ← line 772: persist gate
        save_digest_store(store)                        # ← line 773
    return added, upvoted, rejected
```

### Why it's a bug
`store.session_id = session_id` at line 755 mutates the in-memory store BEFORE the admission loop, so the session_id always changes whenever `update_digest` is called. The `if added > 0 or upvoted > 0` gate at line 772 only persists when NEW material lands — but `store.session_id` is already mutated, and silently lost if every candidate is rejected. The `updated` timestamp (set inside `save_digest_store` line 682) is also never bumped on rejected-only runs, so downstream consumers cannot distinguish "stale file from a prior run" from "hook ran today and found nothing to admit".

Symptoms users can hit:
1. `cozempic` hook fires on a quiet session → all rule candidates low-score → `rejected_only=N`, but the `behavioral-digest.json` file still shows last week's `session_id`, misleading anyone inspecting the store via `cozempic doctor` / show.
2. Any `load_digest_store → save_digest_store` migration in between (line 649-653 handles this correctly within `load_digest_store`), so BUG-9 is specifically the `update_digest` path where the caller's session_id is dropped.

### Proposed fix (spec)
Persist unconditionally when any store field changed — simplest and safest: always save when candidates were processed (even if all rejected), since session_id was already mutated.

```python
if added > 0 or upvoted > 0 or store.session_id != prior_session_id:
    save_digest_store(store)
```

Or simpler/idempotent:

```python
# Persist unconditionally — save is atomic and cheap relative to JSONL scan.
# Rejected-only runs still need to bump `updated` and retain the new session_id.
save_digest_store(store)
```

The simpler form is preferred — `save_digest_store` already runs concurrent-merge at line 671-680 and is write-idempotent.

### Test contracts (minimum 2)
1. `test_rejected_only_persists_session_id` — Pre-seed a `behavioral-digest.json` with `session_id="old"`. Call `update_digest(messages=[...all-noise-rejected...], session_id="new")`. Load store from disk. Assert `store.session_id == "new"`.
2. `test_rejected_only_bumps_updated_timestamp` — Pre-seed a file with `updated="2020-01-01T00:00:00+00:00"`. Call `update_digest` with rejected-only candidates. Reload store. Assert `store.updated > "2020-01-01"`.
3. `test_zero_candidates_no_op` (regression) — Call `update_digest(messages=[])` with empty input. No exceptions, the file is either unchanged or re-written idempotently with the same content.

---

## A12 — slash-command noise filter is case-sensitive (`/Compact` leaks)

**Location**: `src/cozempic/digest.py:174-176`.
**Verdict**: REAL

### Current code
```python
# Slash command: '/' + lowercase letter (distinguish from file paths like /Users)
if len(stripped) >= 2 and stripped[0] == "/" and stripped[1].islower():
    return True
```

### Why it's a bug
The filter intends to catch Claude Code slash-command turns like `/compact`, `/clear`, `/init`. The `.islower()` check rejects `/Compact`, `/INIT`, `/Help`, etc. Verified experimentally: `"C".islower()` is False, so `/Compact` passes through as a non-noise user turn and becomes a correction candidate.

Users invoking slash commands capitalised (either deliberately or via auto-capitalise on iPadOS / autocorrect on macOS keyboards) leak "Do not compact" style rules into the digest. Observed by comparing `/compact` flow vs `/Compact` flow on any live session.

The same issue applies to ALL CAPS (`/COMPACT`) which `.islower()` also rejects.

### Proposed fix (spec)
Use `.isalpha()` instead of `.islower()` — any alphabetical char distinguishes a command from `/Users/...` file paths (file paths start with `/U` uppercase which WOULD match `.isalpha()` — must combine with a path-disambiguator).

Correct fix: match any alpha char for slot [1], AND verify the line is not a filesystem path (absolute paths typically have `/` as separator after the name).

```python
# Slash command: '/' + letter, not a file path. Case-insensitive because users
# sometimes type /Compact or /INIT (A12). File paths (/Users, /tmp, /opt) are
# disambiguated by containing another '/' — slash commands are single-token.
if (
    len(stripped) >= 2
    and stripped[0] == "/"
    and stripped[1].isalpha()
    and "/" not in stripped[1:].split()[0]  # no second slash in first token
):
    return True
```

Simpler alternative (matches current intent more directly): pre-compile a regex `^/[A-Za-z][A-Za-z0-9_-]*(\s|$)` and match.

```python
_SLASH_CMD_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)")
...
if _SLASH_CMD_RE.match(stripped):
    return True
```

The regex form avoids the `/Users/...` false-positive cleanly: `/Users/` does not match because after `Users` the next char is `/` which is neither whitespace nor end-of-string.

### Test contracts (minimum 2)
1. `test_compact_uppercase_is_noise` — `_is_system_noise("/Compact")` must return True (currently False).
2. `test_init_allcaps_is_noise` — `_is_system_noise("/INIT")` must return True.
3. `test_file_path_users_is_not_noise` — `_is_system_noise("/Users/alice/foo.py needs a fix")` must return False (regression — file-path protection preserved).
4. `test_compact_lowercase_still_noise` — `_is_system_noise("/compact")` remains True (regression).

---

## A2 — `_to_prohibition` silently rejects long/multi-paragraph input (no debug signal)

**Location**: `src/cozempic/digest.py:319-325`.
**Verdict**: REAL (missing debug path — not a wrong-behavior bug)

### Current code
```python
    text = text.strip()
    # Reject structural / oversize input — cannot be a clean correction.
    if not text or len(text) > 200 or text.count("\n") > 2:
        return ""
    if text[0] in "<-*#`":
        return ""
```

### Why it's a bug
`_to_prohibition` returning `""` is a sentinel for "skip this candidate" — caller in `extract_corrections` drops the rule silently. When users report "my correction was not captured", there is currently no way to diagnose which gate rejected it: noise filter, admission threshold, or `_to_prohibition` length/structure gate. `digest.py` contains zero `print`/`logging`/`stderr` calls (grep confirmed).

Impact: low (cosmetic / diagnosability), but A2 is explicitly in scope for polish-v2.

### Proposed fix (spec)
Emit an opt-in stderr debug line when `COZEMPIC_DEBUG=1` is set. Do NOT print unconditionally — digest runs inside hooks (PreCompact, Stop) where stderr noise would leak to the user. Keep the mechanism minimal: one module-level helper, no `logging` setup (cozempic keeps `dependencies = []`).

```python
import os
import sys

_DEBUG = os.environ.get("COZEMPIC_DEBUG") == "1"

def _debug(msg: str) -> None:
    if _DEBUG:
        print(f"[cozempic.digest] {msg}", file=sys.stderr)

# inside _to_prohibition:
    text = text.strip()
    if not text:
        return ""
    if len(text) > 200:
        _debug(f"_to_prohibition rejected: len={len(text)} > 200 — {text[:60]!r}...")
        return ""
    if text.count("\n") > 2:
        _debug(f"_to_prohibition rejected: multi-paragraph ({text.count(chr(10))} newlines) — {text[:60]!r}")
        return ""
    if text[0] in "<-*#`":
        _debug(f"_to_prohibition rejected: structural-prefix {text[0]!r} — {text[:60]!r}")
        return ""
```

Three debug emits — one per rejection branch. The `_DEBUG` flag is resolved at import time; tests can monkeypatch `digest._DEBUG = True` to exercise.

### Test contracts (minimum 2)
1. `test_debug_flag_off_no_stderr` — `COZEMPIC_DEBUG` unset, call `_to_prohibition("a" * 500)`, capture stderr via `capsys` (pytest), assert empty.
2. `test_debug_flag_on_emits_length_rejection` — monkeypatch `digest._DEBUG = True`, call `_to_prohibition("a" * 500)`, stderr contains `"len=500 > 200"` AND `"_to_prohibition rejected"`.
3. `test_debug_flag_on_emits_multiline_rejection` — monkeypatch `digest._DEBUG = True`, call `_to_prohibition("a\nb\nc\nd")`, stderr contains `"multi-paragraph"`.
4. `test_to_prohibition_return_value_unchanged` (regression) — with `_DEBUG=True`, the return values for oversize/multiline/structural inputs are STILL `""` (behavior parity).

---

## BUG-13 — `next_id` fallback after R999 produces a lexically-misordered ID

**Location**: `src/cozempic/digest.py:95-101`.
**Verdict**: REAL

### Current code
```python
    def next_id(self) -> str:
        existing = {r.id for r in self.all_rules()}
        for i in range(1, 1000):
            rid = f"R{i:03d}"
            if rid not in existing:
                return rid
        return f"R{len(self.all_rules()) + 1:03d}"
```

### Why it's a bug
Two sub-issues:

1. **Lexical sort breaks once 4-digit IDs appear.** Verified:
   ```python
   sorted(['R001', 'R999', 'R1000', 'R100']) → ['R001', 'R100', 'R1000', 'R999']
   'R1000' > 'R999' → False
   ```
   After R999 fills up, the first fallback ID is `f"R{1000:03d}"` = `"R1000"` (4 chars — `:03d` is a MIN width, not a cap). Any caller that relies on ID sort order (e.g., display in `show_digest` line 974, or a future migration script) gets R1000 sorted BEFORE R999.

2. **Collision risk on sparse stores.** If a store has 900 rules with gaps (e.g., R001..R500 and R600..R999), the loop finds R501 as a gap — fine. But if ALL R001..R999 are taken AND the store has a gap previously filled with a 4-digit ID (e.g., R1000 was created, R500 was deleted, now len=999), the fallback returns `f"R{1000:03d}"` = "R1000" which COLLIDES with the existing R1000 that was never deleted. The loop never scanned i>999.

### Proposed fix (spec)
Extend the loop range to cover any numeric ID, and use a stable width that handles 4+ digits without breaking lex sort:

**Option A (simplest — fix the loop ceiling and pad to 4 digits once >R999 is in play)**:
```python
    def next_id(self) -> str:
        existing = {r.id for r in self.all_rules()}
        # Find the smallest unused Rnnnn slot (nnnn >= 001).
        for i in range(1, 10_000):
            rid = f"R{i:04d}" if i >= 1000 else f"R{i:03d}"
            if rid not in existing:
                return rid
        # Practical upper bound — the cap (MAX_ACTIVE_RULES=20) makes
        # >9999 rules a cosmic-ray scenario, but fail loudly if hit.
        raise RuntimeError("digest store exhausted R0001-R9999 id space")
```

**Option B (cleaner — always pad to 4 digits going forward, preserve legacy 3-digit IDs already on disk)**:
```python
    def next_id(self) -> str:
        existing = {r.id for r in self.all_rules()}
        for i in range(1, 10_000):
            # Match legacy 3-digit shape for 1..999, then widen to 4.
            rid = f"R{i:03d}" if i < 1000 else f"R{i:04d}"
            if rid not in existing:
                return rid
        raise RuntimeError("digest store exhausted id space")
```

Option A/B are equivalent; pick A for consistency with the existing `:03d` idiom. Note: lexical sort still mixes R0999 vs R1000 IF we widened all IDs, but since MAX_ACTIVE_RULES=20 caps real usage far below 999, practical sort-order regressions are bounded to synthetic / migrated stores.

### Test contracts (minimum 2)
1. `test_next_id_after_r999_is_r1000_4digit` — populate 999 rules R001..R999 → `store.next_id()` returns `"R1000"` (currently also returns "R1000" — same on current code, but assert 4-digit format to lock in the width choice).
2. `test_next_id_no_collision_on_full_plus_gap` — populate R001..R999 + R1000, remove R500 → `store.next_id()` returns `"R500"` (gap fill, not fallback to R1001) (currently: loop finds R500 → works; so this test is regression).
3. `test_next_id_collision_when_all_3digit_filled_and_1000_exists` — populate R001..R999 AND R1000, delete none → `store.next_id()` returns `"R1001"` (currently returns `"R1000"` which COLLIDES — RED). This is the discriminating test.
4. `test_ids_sort_chronologically_through_boundary` — add rules in sequence R998, R999, R1000, sort IDs → lex-sort must match insertion order (`["R998", "R999", "R1000"]`). Currently fails.

**Risk**: `test_next_id_sequential` at `tests/test_digest.py:451` currently asserts `"R002"` after one rule — that contract holds. No test pins the 4-digit format, so widening is safe.

---

## BUG-12 — `_to_prohibition` default branch produces malformed "Do not <digit><rest>"

**Location**: `src/cozempic/digest.py:348-350`.
**Verdict**: REAL

### Current code
```python
    # Default: prefix with "Do not"
    if len(text) > 5:
        return f"Do not {text[0].lower()}{text[1:]}"
    return text
```

### Why it's a bug
`text[0].lower()` is a no-op for any character that is not an uppercase letter — digits, punctuation, non-ASCII letters are passed through unchanged. Concrete outputs:

```python
_to_prohibition("5xx errors must be retried") → "Do not 5xx errors must be retried"
_to_prohibition("123 lines of config")         → "Do not 123 lines of config"
_to_prohibition("%20 encoding")                → "Do not %20 encoding"
_to_prohibition("émoji prefix")                → "Do not émoji prefix"  # acceptable
```

Outputs like "Do not 5xx errors must be retried" are grammatically malformed — "Do not" requires a verb phrase, not a noun. These rules then get injected into Claude's context as hard prohibitions that no model can usefully apply.

**Bonus latent bug**: on short inputs (`len(text) <= 5`), the current code at line 351 `return text` returns the RAW input — not a prohibition, not the skip sentinel. Callers (`extract_corrections`) treat any non-empty return as a valid prohibition rule text. So `_to_prohibition("hi")` returns `"hi"` which then becomes a rule with text "hi" — not a prohibition. Fix below corrects this too.

### Risk (elevated, flagged per prompt)
**BREAKING** for users whose current rule text starts with a digit or punctuation. Before any fix merges, run a one-shot audit of existing `~/.cozempic/behavioral-digest.json` files:
- If ZERO active rules have digit/punctuation first char → safe to reject in `_to_prohibition`
- If N>0 active rules start with non-letter → the fix must either (a) sanitize (drop first non-letter run and capitalize the next letter), OR (b) reject AND demote existing rules via `load_digest_store` auto-migration path (the hook at line 641 already re-runs `_to_prohibition`, so existing rules that fail the new gate would auto-demote to pending — this is GOOD).

Recommended direction: **reject** in `_to_prohibition` (return `""`), let auto-migration demote existing malformed rules on next load. Sanitization is tempting but introduces a new failure mode: a user rule "3xx redirects" becomes "Do not xx redirects" which changes meaning.

### Proposed fix (spec)
```python
    # Default: prefix with "Do not" — require the first character to be a letter
    # so the grammar is valid. Digit/punctuation-prefixed text is rejected
    # (demoted to pending by the auto-migration path in load_digest_store).
    if len(text) > 5 and text[0].isalpha():
        return f"Do not {text[0].lower()}{text[1:]}"
    return ""   # was `return text` — malformed output is worse than a skip
```

Two changes:
1. Gate on `text[0].isalpha()` — rejects digit, punctuation, whitespace (already stripped), symbol leads.
2. Return `""` (skip sentinel) instead of `text` when the guard fails. Returning `text` was also a latent bug: callers expect prohibition framing and get raw input, which then becomes a non-prohibition rule.

### Test contracts (minimum 2)
1. `test_digit_prefix_returns_empty` — `_to_prohibition("5xx errors must be retried")` returns `""` (currently returns malformed prohibition — RED).
2. `test_punctuation_prefix_returns_empty` — `_to_prohibition("%20 encoding is bad")` returns `""`.
3. `test_letter_prefix_still_works` — `_to_prohibition("add Co-Authored-By")` returns `"Do not add Co-Authored-By"` (regression).
4. `test_short_input_returns_empty_not_raw` — `_to_prohibition("hi")` returns `""` (currently returns `"hi"` — latent bug fix).
5. `test_existing_digit_prefix_rule_migrates_to_pending` — seed store with `DigestRule(id="R001", rule="5xx errors are bad", evidence="5xx errors are bad", status="active")`, call `load_digest_store`, assert `rule.status == "pending"` (auto-migration path).

**Risk block**:
> **Risk**: Users with rules like "3xx redirect handling" or "$VAR substitution" will see their rules demoted from active to pending on next cozempic invocation. Mitigation: the existing auto-migration path in `load_digest_store` (line 636-644) runs `_to_prohibition` on `rule.evidence or rule.rule` and demotes on empty — so this is already handled, just exercised. No user data loss (pending rules are retained, just not injected).

---

## BUG-G13 — `_pid_file_for_session` uses 12-char truncation with no UUID validation (collision + path-traversal)

**Location**: `src/cozempic/guard.py:1067-1070`.
**Verdict**: REAL

### Current code
```python
def _pid_file_for_session(session_id: str) -> Path:
    """Return the PID file path for a guard daemon watching a specific session."""
    session_id = _normalize_session_id(session_id)
    return Path("/tmp") / f"cozempic_guard_{session_id[:12]}.pid"
```

### Why it's a bug

Two failure modes:

1. **Truncation collision**. Session IDs passed in are expected to be UUIDs (8-4-4-4-12 hex, 36 chars). `session_id[:12]` = first 8 hex digits + `-` + 3 hex digits. Two UUIDs sharing the first 8 hex chars (2^32 ≈ 4B namespace) collide on the pidfile path. For a single user on one machine over years of usage, collision probability is negligible for true random UUIDs — but nothing enforces UUIDs. Any caller that passes a non-UUID session identifier (test harness, custom launcher, typo) can collide trivially.

2. **Path traversal (security).** `_normalize_session_id` at line 61-65 only strips a `.jsonl` suffix via `Path.stem`. It does NOT validate that the remaining string is a UUID. A malicious / malformed `session_id = "../../etc/pa"` passes through untouched:
   ```python
   >>> Path("/tmp") / f"cozempic_guard_{'../../etc/pa'[:12]}.pid"
   PosixPath('/tmp/cozempic_guard_../../etc/pa.pid')
   >>> _.resolve()
   PosixPath('/private/tmp/etc/pa.pid')
   ```
   This writes the daemon's PID file OUTSIDE `/tmp/cozempic_guard_*.pid` naming convention, breaking `doctor` cleanup (which globs `/tmp/cozempic_guard_*.pid`) AND potentially overwriting another user's files if `/tmp/etc/` is writable. Realistic attack requires controlling `session_id` input, which comes from CLI args and SessionStart hook — normal usage is UUID, but defense in depth is warranted.

### Proposed fix (spec)
Validate with a UUID regex. Reject non-UUID session_ids with `ValueError` (fail-fast — the CALLER is wrong). For backward compat during the transition, a relaxed regex matches "hex-only, at least 12 chars" which covers both UUIDs and any legitimate hex-derived identifier.

```python
import re

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{12,}$")

def _pid_file_for_session(session_id: str) -> Path:
    """Return the PID file path for a guard daemon watching a specific session.

    Validates `session_id` against a UUID-shaped regex to prevent path-traversal
    and filename collisions via non-hex input (BUG-G13).
    """
    session_id = _normalize_session_id(session_id)
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            f"session_id must be a hex/UUID identifier, got {session_id!r}"
        )
    return Path("/tmp") / f"cozempic_guard_{session_id[:12]}.pid"
```

Alternative (softer — sanitize instead of raise): replace non-hex chars with `_` before truncation. Rejected in favor of fail-fast because silent sanitization hides caller bugs.

### Test contracts (minimum 2)
1. `test_pid_file_uuid_valid` — `_pid_file_for_session("e6c3a4b2-1234-5678-9abc-def012345678")` returns `Path("/tmp/cozempic_guard_e6c3a4b2-12.pid")` (regression — current behavior preserved for real UUIDs).
2. `test_pid_file_path_traversal_rejected` — `_pid_file_for_session("../../etc/pa")` raises `ValueError`.
3. `test_pid_file_short_id_rejected` — `_pid_file_for_session("abc")` raises `ValueError`.
4. `test_pid_file_jsonl_suffix_stripped` — `_pid_file_for_session("/path/e6c3a4b2-1234-5678-9abc-def012345678.jsonl")` returns pid path for the UUID (regression — `_normalize_session_id` still runs first).
5. `test_pid_file_non_hex_chars_rejected` — `_pid_file_for_session("zzzzzzzzzzzzzz")` raises `ValueError`.

---

## BUG-G16 — dead `_is_guard_running` shim returns None always, zero callers

**Location**: `src/cozempic/guard.py:1141-1143`.
**Verdict**: REAL — safe to delete.

### Current code
```python
def _is_guard_running(cwd: str) -> int | None:
    """Legacy check — scans for any guard PID file matching this CWD."""
    return _is_guard_running_for_session(cwd)  # Won't match, but keeps signature
```

### Why it's a bug
The function delegates to `_is_guard_running_for_session(cwd)` which expects a session_id (UUID), not a CWD. Internally, `_is_guard_running_for_session` calls `_normalize_session_id(cwd)` which does NOT coerce a CWD to a UUID — it just strips `.jsonl` suffix. The pidfile path then becomes `/tmp/cozempic_guard_{cwd[:12]}.pid` which never exists (daemons write to session-UUID pidfiles). Result: **always returns None**, regardless of daemon state.

The function comment itself (`# Won't match, but keeps signature`) is a confession that this is a broken stub.

### Caller count (verified across entire worktree, not just src/)
```
$ grep -rnE "_is_guard_running\b" src/ tests/ .claude-plugin/ plugin/
src/cozempic/guard.py:1141: def _is_guard_running(cwd: str) -> int | None:
```
One hit — the definition itself. Zero callers in src/, zero in tests/, zero in plugin/ or .claude-plugin/. (A handful of text mentions in the stale `AUDIT_REPORT.md` and prose in `tests/test_guard_hardening.py` docstring — not callers.)

### Proposed fix (spec)
Delete the function. No compat shim needed — private (leading-underscore) symbol, no external API.

```python
# Lines 1141-1143 deleted.
# Keep `_pid_file` alias at line 1137-1138 unchanged (it's tested by test_guard_robustness).
```

### Test contracts (minimum 2)
1. `test_is_guard_running_removed` — `from cozempic import guard; assert not hasattr(guard, "_is_guard_running")`. (RED-aligned: passes only after deletion; currently fails because attr still exists.)
2. `test_pid_file_alias_retained` — `from cozempic.guard import _pid_file; ...` still importable and returns `Path("/tmp/cozempic_guard_*.pid")` (regression — the OTHER legacy alias at line 1137 must NOT be deleted).
3. (Optional) `test_no_dead_shim_for_session_variant` — assert `_is_guard_running_for_session` IS still exported (it's the live API).

---

## Cross-cutting notes

- **Scope risk**: BUG-12 is the only bug in the batch with a real data-migration footprint (existing malformed rules will auto-demote). All other fixes are contained: BUG-9 persists one more field, A12 widens a regex, A2 adds opt-in stderr, BUG-13 widens id format forward-only, BUG-G13 validates input, BUG-G16 deletes dead code.
- **Zero-dependency preserved**: A2 uses `os` + `sys` + `print`, BUG-G13 uses stdlib `re`. No new deps.
- **Backward-compat**: existing 608-test suite expected to stay green. The `tests/test_digest.py::test_next_id_sequential` is the only test touching `next_id` and it operates below the R999 boundary.
- **Commit atomicity**: 7 separate GREEN commits recommended. Each bug is independent except BUG-9 and A2 both touch imports (`os`/`sys`) — still independent at the function level.
- **Test file placement**: Write tests as `TestPolishV2<BugName>` classes inside existing `tests/test_digest.py` (5 bugs) and `tests/test_guard_hardening.py` (2 bugs). DO NOT create a new `tests/test_polish_v2.py` — project convention groups tests by module under test, not by PR.

---

## Verification
- Confidence: 95% (all 7 bugs reproduced against the current worktree source; fix specs grounded in the verified current behavior, not in the original prompt's hint text).
- Signals (≥3 orthogonal):
  - Source reads: `src/cozempic/digest.py` (lines 95-101, 172-176, 174, 309-351, 744-775) and `src/cozempic/guard.py` (lines 61-65, 1067-1070, 1099-1133, 1141-1143) via Read.
  - Whole-worktree grep for `_is_guard_running\b` (zero callers verified).
  - Python REPL reproduction of each failure mode (BUG-12 malformed output, BUG-13 lex sort, A12 `.islower()`, BUG-G13 path traversal via `Path.resolve`).
  - Cross-check of `tests/test_digest.py` to confirm no existing test pins the buggy behavior (so fixes will not cascade into regressions).
- Cross-checked: pidfile traversal resolution (`Path.resolve()` output captured), caller count of `_is_guard_running` via grep, mutation point of `store.session_id` at line 755 (BUG-9 root cause).
- Not verified: live daemon behavior under the proposed fixes (that's task #4's job — validator). Whether any user on the fork has existing digit-prefixed rules (BUG-12 migration impact) — empirical check deferred to validator phase.
