"""Guard daemon — continuous team checkpointing + emergency prune.

Architecture:
  EVERY interval:  Extract team state → write checkpoint (lightweight, no prune)
  AT threshold:    Prune non-team messages → inject recovery → optionally reload

The checkpoint runs continuously so team state is ALWAYS on disk, regardless
of whether the threshold is ever hit. The threshold prune is the emergency
fallback — not the primary protection mechanism.

Checkpoint triggers:
  1. Every N seconds (guard daemon)
  2. On demand via `cozempic checkpoint` (hook-driven)
  3. At file size threshold (emergency prune)
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Per-session spawn lock: prevents a concurrent _is_guard_running_for_session
# from treating a live O_CREAT placeholder as a stale file (within the same
# process). Keyed by session_id to avoid false contention across sessions.
_spawn_locks: dict[str, threading.Lock] = {}
_spawn_locks_mu = threading.Lock()

from ._validation import ConfigError
from .executor import run_prescription
from .helpers import is_ssh_session, shell_quote
from .registry import PRESCRIPTIONS
import cozempic.strategies  # noqa: F401 — register strategies so guard_prune_cycle can actually prune (#15)
from .session import (
    PruneConflictError,
    PruneLockError,
    _PruneLock,
    cleanup_old_backups,
    find_claude_pid,
    find_current_session,
    find_sessions,
    load_messages,
    save_messages,
    snapshot_session,
)
from .team import TeamState, extract_team_state, inject_team_recovery, write_team_checkpoint
from .tokens import default_token_thresholds, quick_token_estimate
# Eager import: ensures the daemon's upgrade check uses code from the daemon's
# OWN install state (frozen at import time), not whatever happens to be on
# disk when this function runs post-upgrade. Prevents old-daemon/new-updater
# version skew.
from .updater import maybe_auto_update, ping_install_if_new


def _normalize_session_id(session_id: str) -> str:
    """Extract UUID from a session_id that might be a full path."""
    if session_id.endswith(".jsonl"):
        return Path(session_id).stem
    return session_id


def _resolve_session_by_id(session_id: str, max_retries: int = 10, retry_delay: float = 1.5) -> dict | None:
    """Find a session by explicit ID, UUID prefix, or path.

    Handles full JSONL paths (from SessionStart hook), UUIDs, and prefixes.
    Retries up to max_retries times (15s total) to handle the race condition
    where the hook fires before Claude Code creates the JSONL file (#73).
    """
    p = Path(session_id)

    # Fast path: full path exists on disk
    if p.exists() and p.suffix == ".jsonl":
        return {
            "path": p,
            "session_id": p.stem,
            "size": p.stat().st_size,
            "project": p.parent.name,
        }

    # Extract UUID from path-like input (file may not exist yet)
    search_id = _normalize_session_id(session_id)

    for attempt in range(max_retries):
        # Re-check path on each retry (file may appear)
        if p.suffix == ".jsonl" and p.exists():
            return {
                "path": p,
                "session_id": p.stem,
                "size": p.stat().st_size,
                "project": p.parent.name,
            }
        for sess in find_sessions():
            if sess["session_id"] == search_id or sess["session_id"].startswith(search_id):
                return sess
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    return None


# ─── Lightweight checkpoint (no prune) ───────────────────────────────────────

def checkpoint_team(
    cwd: str | None = None,
    session_path: Path | None = None,
    quiet: bool = False,
) -> TeamState | None:
    """Extract and save team state from the current session. No pruning.

    This is fast and safe — it only reads the JSONL and writes a checkpoint.
    Designed to be called from hooks, guard daemon, or CLI.

    Returns the extracted TeamState, or None if no session found.
    """
    if session_path is None:
        sess = find_current_session(cwd)
        if not sess:
            if not quiet:
                print("  No active session found.", file=sys.stderr)
            return None
        session_path = sess["path"]

    messages = load_messages(session_path)
    state = extract_team_state(messages)

    if state.is_empty():
        if not quiet:
            print("  No team state detected.")
        return state

    project_dir = session_path.parent
    cp_path = write_team_checkpoint(state, project_dir)

    if not quiet:
        agents = len(state.subagents)
        teammates = len(state.teammates)
        tasks = len(state.tasks)
        parts = []
        if agents:
            parts.append(f"{agents} subagents")
        if teammates:
            parts.append(f"{teammates} teammates")
        if tasks:
            parts.append(f"{tasks} tasks")
        summary = ", ".join(parts) if parts else "empty"
        print(f"  Checkpoint: {summary} → {cp_path.name}")

    return state


# ─── Team-aware pruning ──────────────────────────────────────────────────────

def prune_with_team_protect(
    messages: list,
    rx_name: str = "standard",
    config: dict | None = None,
) -> tuple[list, list, TeamState]:
    """Run a prescription but protect team-related messages from pruning.

    Returns (pruned_messages, strategy_results, team_state).

    Strategy:
    1. Extract team state
    2. Tag team messages with __cozempic_team_protected__ (is_protected() skips them)
    3. Run prescription on the FULL list (no splitting, no memory doubling)
    4. Remove tags, inject team recovery messages
    """
    from .team import _is_team_message

    config = config or {}
    strategy_names = PRESCRIPTIONS.get(rx_name, PRESCRIPTIONS["standard"])

    # 1. Extract team state
    team_state = extract_team_state(messages)

    if team_state.is_empty():
        # No team — standard pruning
        new_messages, results = run_prescription(messages, strategy_names, config)
        return new_messages, results, team_state

    # 2. Build pending_task_ids
    from .team import TEAM_TOOL_NAMES
    pending_task_ids: set[str] = set()
    for _, msg_dict, _ in messages:
        inner = msg_dict.get("message", {})
        for block in (inner.get("content", []) if isinstance(inner.get("content"), list) else []):
            if block.get("type") == "tool_use" and block.get("name") in TEAM_TOOL_NAMES:
                tool_use_id = block.get("id", "")
                if tool_use_id:
                    pending_task_ids.add(tool_use_id)

    # 3. Tag team messages as protected (strategies skip via is_protected())
    tagged_indices: list[int] = []
    for _, msg_dict, _ in messages:
        if _is_team_message(msg_dict, pending_task_ids):
            msg_dict["__cozempic_team_protected__"] = True
            tagged_indices.append(id(msg_dict))

    # 4. Prune full list — team messages are protected, no list splitting needed
    pruned_messages, results = run_prescription(messages, strategy_names, config)

    # 5. Remove tags from surviving messages
    for _, msg_dict, _ in pruned_messages:
        msg_dict.pop("__cozempic_team_protected__", None)

    # 6. Inject team recovery messages at the end
    pruned_messages = inject_team_recovery(pruned_messages, team_state)

    return pruned_messages, results, team_state


# ─── Guard daemon ─────────────────────────────────────────────────────────────

def start_guard(
    cwd: str | None = None,
    threshold_mb: float = 50.0,
    soft_threshold_mb: float | None = None,
    rx_name: str = "standard",
    interval: int = 30,
    auto_reload: bool = True,
    config: dict | None = None,
    reactive: bool = True,
    threshold_tokens: int | None = None,
    soft_threshold_tokens: int | None = None,
    session_id: str | None = None,
    claude_pid: int | None = None,
) -> None:
    """Start the guard daemon with tiered pruning.

    Three-phase protection:
      1. CHECKPOINT every interval — extract team state, write to disk
      2. SOFT PRUNE at soft threshold — gentle prune, no reload, no disruption
      3. HARD PRUNE at hard threshold — full prune with team-protect + optional reload

    Thresholds can be bytes-based, token-based, or both. When both are set,
    whichever is hit first triggers the action.

    Default soft threshold is 60% of hard threshold if not specified.

    Args:
        cwd: Working directory for session detection.
        threshold_mb: Hard threshold in MB — emergency prune + optional reload.
        soft_threshold_mb: Soft threshold in MB — gentle prune, no reload.
            Defaults to 60% of threshold_mb.
        rx_name: Prescription to apply at hard threshold.
        interval: Check interval in seconds.
        auto_reload: If True, kill Claude and auto-resume after hard prune.
        config: Extra config for pruning strategies.
        threshold_tokens: Hard threshold in tokens (optional, checked alongside bytes).
        soft_threshold_tokens: Soft threshold in tokens (optional, checked alongside bytes).
        session_id: Explicit session ID to monitor (bypasses auto-detection).
    """
    # Validate ordering invariants FIRST — a reload storm caused by a
    # swapped soft/hard threshold is much worse than a clean upfront error.
    # Argparse already rejects non-positive values, but direct Python callers
    # (guard.start_guard(...)) bypass argparse, so belt-and-braces check.
    if threshold_mb <= 0:
        raise ConfigError(f"threshold_mb must be positive, got {threshold_mb}")
    if soft_threshold_mb is not None and soft_threshold_mb <= 0:
        raise ConfigError(f"soft_threshold_mb must be positive, got {soft_threshold_mb}")
    if (
        soft_threshold_mb is not None
        and soft_threshold_mb >= threshold_mb
    ):
        raise ConfigError(
            f"soft_threshold_mb={soft_threshold_mb} must be strictly less than "
            f"threshold_mb={threshold_mb}"
        )
    if interval <= 0:
        raise ConfigError(f"interval must be positive, got {interval}")
    if threshold_tokens is not None and threshold_tokens <= 0:
        raise ConfigError(f"threshold_tokens must be positive, got {threshold_tokens}")
    if soft_threshold_tokens is not None and soft_threshold_tokens <= 0:
        raise ConfigError(f"soft_threshold_tokens must be positive, got {soft_threshold_tokens}")
    if (
        threshold_tokens is not None
        and soft_threshold_tokens is not None
        and soft_threshold_tokens >= threshold_tokens
    ):
        raise ConfigError(
            f"soft_threshold_tokens={soft_threshold_tokens} must be strictly less than "
            f"threshold_tokens={threshold_tokens}"
        )

    hard_threshold_bytes = int(threshold_mb * 1024 * 1024)

    if soft_threshold_mb is None:
        soft_threshold_mb = round(threshold_mb * 0.6, 1)
    soft_threshold_bytes = int(soft_threshold_mb * 1024 * 1024)

    # Find the session — explicit ID or auto-detect
    # strict=True: guard is destructive, refuse to fall back to "most recently modified"
    if session_id:
        sess = _resolve_session_by_id(session_id)
    else:
        sess = find_current_session(cwd, strict=True)
    if not sess:
        # Clean up any stale PID file from this failed startup
        if session_id:
            try:
                _pid_file_for_session(session_id).unlink(missing_ok=True)
            except Exception:
                pass
        print("  ERROR: Could not detect current session.", file=sys.stderr)
        if not session_id:
            print("  Tip: Use --session <session_id> for explicit targeting.", file=sys.stderr)
        sys.exit(1)

    session_path = sess["path"]

    # Detect context window from session data (used for display + overflow scaling)
    from .tokens import detect_context_window, default_token_thresholds_4tier, DEFAULT_HARD2_TOKEN_PCT
    messages_for_model = load_messages(session_path)
    context_window = detect_context_window(messages_for_model)

    # Default to 4-tier token thresholds when none specified
    if threshold_tokens is None:
        soft_threshold_tokens, threshold_tokens, hard2_threshold_tokens = default_token_thresholds_4tier(context_window)
    else:
        hard2_threshold_tokens = int(context_window * DEFAULT_HARD2_TOKEN_PCT)
        if soft_threshold_tokens is None:
            soft_threshold_tokens = int(threshold_tokens * 0.45)

    # Persist cwd + context_window to the sidecar so reload and guard resume
    # can resolve the project directory without relying on slug reversal.
    from .session import record_session
    record_session(sess["session_id"], cwd or os.getcwd(), context_window)

    # Clean up stale reload watchers from previous versions
    _cleanup_stale_watchers()

    # Auto-update check — force=True so it works even when guard runs via hook (no TTY)
    ping_install_if_new()
    maybe_auto_update(force=True)

    # Format context window for display
    if context_window >= 1_000_000:
        ctx_str = f"{context_window / 1_000_000:.1f}M"
    else:
        ctx_str = f"{context_window / 1_000:.0f}K"

    # Compute threshold %s for display
    soft_pct = int(soft_threshold_tokens / context_window * 100) if soft_threshold_tokens and context_window else 25
    hard1_pct = int(threshold_tokens / context_window * 100) if threshold_tokens and context_window else 55
    hard2_pct = int(hard2_threshold_tokens / context_window * 100) if hard2_threshold_tokens and context_window else 80

    print(
        f"\n  4-tier guard protecting context ({ctx_str} window):\n"
        f"    Soft  ({soft_pct}%): gentle prune, no reload (file maintenance)\n"
        f"    Hard1 ({hard1_pct}%): {rx_name} prune + reload\n"
        f"    Hard2 ({hard2_pct}%): aggressive prune + reload (emergency)\n"
        f"    User  (90%): manual aggressive (cozempic treat -rx aggressive --execute)\n"
    )

    # Reactive overflow recovery via file watcher
    overflow_watcher = None
    if reactive:
        import threading
        from .overflow import CircuitBreaker, OverflowRecovery
        from .watcher import JsonlWatcher

        # Scale danger thresholds based on context window size
        danger_mb = round(threshold_mb * 1.8, 1)
        danger_tokens = int(context_window * 0.90) if context_window else None

        breaker = CircuitBreaker(session_id=sess["session_id"])
        recovery = OverflowRecovery(
            session_path, sess["session_id"], cwd or os.getcwd(), breaker,
            danger_threshold_mb=danger_mb,
            danger_threshold_tokens=danger_tokens,
            claude_pid=claude_pid,
        )
        overflow_watcher = JsonlWatcher(
            str(session_path), on_growth=recovery.on_file_growth,
        )
        watcher_thread = threading.Thread(
            target=overflow_watcher.start, daemon=True, name="cozempic-watcher",
        )
        watcher_thread.start()

    # Graceful shutdown on SIGTERM
    def _graceful_shutdown(signum, frame):
        print(f"\n  [{_now()}] Signal {signum} received — final checkpoint...")
        checkpoint_team(session_path=session_path, quiet=False)
        if overflow_watcher:
            overflow_watcher.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Resolve Claude before daemonization or other reparenting can obscure it.
    if claude_pid is None:
        claude_pid = find_claude_pid()
    claude_alive = True

    prune_count = 0
    soft_prune_count = 0
    checkpoint_count = 0
    cycle_count = 0
    last_team_hash = ""
    consecutive_empty_hard_prunes = 0

    try:
        while True:
            time.sleep(interval)
            cycle_count += 1

            # Periodic backup cleanup every 10 cycles (~5min)
            if cycle_count % 10 == 0:
                cleanup_old_backups(session_path, keep=3)

            # Re-check file exists
            if not session_path.exists():
                print("  WARNING: Session file disappeared. Stopping guard.")
                break

            # Watchdog: detect Claude exit (workaround for Stop hook not firing)
            if claude_pid and claude_alive:
                try:
                    os.kill(claude_pid, 0)
                except (ProcessLookupError, PermissionError):
                    claude_alive = False
                else:
                    # Liveness confirmed — also verify PID identity to guard against
                    # PID reuse (daemon started hours ago; original Claude exited and
                    # kernel recycled its PID to an unrelated process).
                    try:
                        if not _is_claude_process(claude_pid):
                            claude_alive = False
                    except ProcessLookupError:
                        claude_alive = False
                if not claude_alive:
                    print(f"  [{_now()}] Claude process exited (PID {claude_pid}). Final checkpoint...")
                    checkpoint_team(session_path=session_path, quiet=False)
                    print(f"  Guard stopping (Claude exited).")
                    break

            current_size = session_path.stat().st_size

            # ── Phase 1: Continuous checkpoint ────────────────────────
            state = checkpoint_team(
                session_path=session_path,
                quiet=True,
            )

            # Track team state changes silently — only note when prune/threshold fires
            if state and not state.is_empty():
                team_hash = f"{len(state.subagents)}:{len(state.tasks)}:{state.message_count}"
                if team_hash != last_team_hash:
                    checkpoint_count += 1
                    last_team_hash = team_hash

            # ── Token check (fast, from tail of file) ────────────────
            current_tokens = None
            if threshold_tokens is not None or soft_threshold_tokens is not None:
                current_tokens = quick_token_estimate(session_path)

            # Detect if agents are actively running (reload would kill them)
            agents_active = False
            if state and not state.is_empty():
                agents_active = any(
                    s.status in ("running", "unknown")
                    for s in state.subagents
                )

            # ── Phase 4: HARD2 (80%) — aggressive + reload (ALWAYS, even with agents) ──
            hard2_tokens_hit = (
                hard2_threshold_tokens is not None
                and current_tokens is not None
                and current_tokens >= hard2_threshold_tokens
            )
            if hard2_tokens_hit:
                prune_count += 1
                reason = f"{current_tokens:,} tokens >= {hard2_threshold_tokens:,} (80%)"
                print(f"  [{_now()}] EMERGENCY THRESHOLD (80%): {reason}")
                if agents_active:
                    print(f"  WARNING: Agents are active but compaction is imminent — reload required.")
                print(f"  Aggressive prune + reload (cycle #{prune_count})...")

                result = guard_prune_cycle(
                    session_path=session_path,
                    rx_name="aggressive",
                    config=config,
                    auto_reload=auto_reload,
                    cwd=cwd or os.getcwd(),
                    session_id=sess["session_id"],
                )

                if result.get("reloading"):
                    from .helpers import get_savings_line
                    savings = get_savings_line()
                    if savings:
                        print(f"  {savings}")
                    print(f"  Reload triggered. Guard exiting.")
                    break

                print(f"  Pruned: {_fmt_prune_result(result)}")
                if result.get("team_name"):
                    print(f"  Team '{result['team_name']}' state preserved ({result['team_messages']} messages)")
                print()

            # ── Phase 3: HARD1 (55%) — standard + reload (SKIP reload if agents active) ──
            elif (threshold_tokens is not None
                  and current_tokens is not None
                  and current_tokens >= threshold_tokens):
                prune_count += 1
                reason = f"{current_tokens:,} tokens >= {threshold_tokens:,} (55%)"

                if agents_active:
                    # Agents running — prune file only, no reload (don't kill active work)
                    print(f"  [{_now()}] HARD THRESHOLD (55%): {reason}")
                    print(f"  Agents active — prune file only, deferring reload (cycle #{prune_count})...")

                    result = guard_prune_cycle(
                        session_path=session_path,
                        rx_name=rx_name,
                        config=config,
                        auto_reload=False,  # Don't reload — agents are working
                        cwd=cwd or os.getcwd(),
                        session_id=sess["session_id"],
                    )
                else:
                    print(f"  [{_now()}] HARD THRESHOLD (55%): {reason}")
                    print(f"  Standard prune + reload (cycle #{prune_count})...")

                    result = guard_prune_cycle(
                        session_path=session_path,
                        rx_name=rx_name,
                        config=config,
                        auto_reload=auto_reload,
                        cwd=cwd or os.getcwd(),
                        session_id=sess["session_id"],
                    )

                if result.get("reloading"):
                    from .helpers import get_savings_line
                    savings = get_savings_line()
                    if savings:
                        print(f"  {savings}")
                    print(f"  Reload triggered. Guard exiting.")
                    break

                print(f"  Pruned: {_fmt_prune_result(result)}")
                if result.get("team_name"):
                    print(f"  Team '{result['team_name']}' state preserved ({result['team_messages']} messages)")

                if result.get("saved_mb", 0) <= 0:
                    consecutive_empty_hard_prunes += 1
                    if consecutive_empty_hard_prunes >= 3:
                        print(f"  [{_now()}] WARNING: Hard prune freed 0 bytes 3x in a row.")
                        consecutive_empty_hard_prunes = 0
                        time.sleep(interval * 4)
                else:
                    consecutive_empty_hard_prunes = 0
                print()

            # ── Phase 2: SOFT (25%) — gentle, no reload (file maintenance only) ──
            else:
                soft_bytes_hit = current_size >= soft_threshold_bytes
                soft_tokens_hit = (
                    soft_threshold_tokens is not None
                    and current_tokens is not None
                    and current_tokens >= soft_threshold_tokens
                )
                if soft_bytes_hit or soft_tokens_hit:
                    soft_prune_count += 1
                    reason = f"{current_tokens:,} tokens >= {soft_threshold_tokens:,} (25%)" if soft_tokens_hit else f"{current_size / 1024 / 1024:.1f}MB"
                    print(f"  [{_now()}] SOFT THRESHOLD (25%): {reason}")
                    print(f"  Gentle file cleanup, no reload (cycle #{soft_prune_count})...")

                    result = guard_prune_cycle(
                        session_path=session_path,
                        rx_name="gentle",
                        config=config,
                        auto_reload=False,
                        cwd=cwd or os.getcwd(),
                        session_id=sess["session_id"],
                    )

                    print(f"  Trimmed: {_fmt_prune_result(result)}")
                    print()

    except KeyboardInterrupt:
        # Stop reactive watcher
        if overflow_watcher:
            overflow_watcher.stop()

        # Final checkpoint before exit
        checkpoint_team(session_path=session_path, quiet=True)
        total_prunes = prune_count + soft_prune_count
        if total_prunes:
            print(f"\n  Guard stopped. Pruned {total_prunes}x during this session.")
        else:
            print(f"\n  Guard stopped.")


def guard_prune_cycle(
    session_path: Path,
    rx_name: str = "standard",
    config: dict | None = None,
    auto_reload: bool = True,
    cwd: str = "",
    session_id: str | None = None,
    claude_pid: int | None = None,
) -> dict:
    """Execute a single guard prune cycle.

    Holds a _PruneLock for the duration so concurrent guard instances cannot
    race each other.  Takes a _FileSnapshot before loading so that any lines
    Claude appends while pruning is in progress are preserved in the output
    (or the cycle is deferred on conflict).

    Returns dict with: saved_mb, team_name, team_messages, reloading, checkpoint_path
    """
    from .tokens import estimate_session_tokens, calibrate_ratio

    _no_change = {
        "saved_mb": 0.0,
        "original_tokens": 0,
        "final_tokens": 0,
        "team_name": None,
        "team_messages": 0,
        "checkpoint_path": None,
        "backup_path": None,
        "reloading": False,
    }

    try:
        with _PruneLock(session_path):
            # Snapshot before load so we can detect Claude appending mid-prune
            snap = snapshot_session(session_path)

            # Size guard: skip prune for very large sessions (OOM risk #74)
            file_size_mb = session_path.stat().st_size / 1024 / 1024
            if file_size_mb > 200:
                print(f"  [{_now()}] Session {file_size_mb:.0f}MB exceeds 200MB — skipping prune (OOM risk).", file=sys.stderr)
                return _no_change

            messages = load_messages(session_path)
            original_bytes = sum(b for _, _, b in messages)

            # Token estimate before pruning — capture calibrated ratio before metadata-strip
            pre_te = estimate_session_tokens(messages)
            pre_ratio = calibrate_ratio(messages)

            # Prune with team protection
            pruned_messages, results, team_state = prune_with_team_protect(
                messages, rx_name=rx_name, config=config,
            )

            final_bytes = sum(b for _, _, b in pruned_messages)
            saved_bytes = original_bytes - final_bytes

            # If pruning freed nothing (or grew the file via team recovery injection), don't
            # save — avoids backup accumulation and file growth on ineffective prescriptions (#16, #19).
            if saved_bytes <= 0:
                return {
                    "saved_mb": 0.0,
                    "original_tokens": pre_te.total,
                    "final_tokens": pre_te.total,
                    "team_name": team_state.team_name,
                    "team_messages": team_state.message_count,
                    "checkpoint_path": None,
                    "backup_path": None,
                    "reloading": False,
                }

            # Token estimate after pruning — pass pre-calibrated ratio
            post_te = estimate_session_tokens(pruned_messages, pre_calibrated_ratio=pre_ratio)

            # Write checkpoint if team exists
            checkpoint_path = None
            if not team_state.is_empty():
                project_dir = session_path.parent
                checkpoint_path = write_team_checkpoint(team_state, project_dir)

            # Save pruned session — snapshot enables append-aware atomic write
            backup = save_messages(session_path, pruned_messages, create_backup=True, snapshot=snap)

            # Cap backup retention at 3 files to prevent disk fill (#19)
            if backup:
                cleanup_old_backups(session_path, keep=3)

    except PruneLockError as exc:
        print(f"  [{_now()}] Prune deferred — lock held: {exc}", file=sys.stderr)
        return _no_change
    except PruneConflictError as exc:
        print(f"  [{_now()}] Prune deferred — conflict detected: {exc}", file=sys.stderr)
        return _no_change

    # Track lifetime savings
    tokens_saved = pre_te.total - post_te.total if pre_te.total and post_te.total else 0
    if tokens_saved > 0:
        from .helpers import record_savings, get_msg_type
        turn_count = sum(1 for _, m, _ in messages
                       if get_msg_type(m) == "user"
                       and isinstance(m.get("message", {}).get("content", ""), str))
        record_savings(tokens_saved, total_tokens=pre_te.total, turn_count=turn_count)

    result = {
        "saved_mb": saved_bytes / 1024 / 1024,
        "original_tokens": pre_te.total,
        "final_tokens": post_te.total,
        "team_name": team_state.team_name,
        "team_messages": team_state.message_count,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        "backup_path": str(backup) if backup else None,
        "reloading": False,
    }

    # Trigger reload if configured — terminate Claude then auto-resume
    if auto_reload:
        reload_pid = claude_pid if claude_pid is not None else find_claude_pid()
        if reload_pid:
            _terminate_and_resume(reload_pid, cwd, session_id=session_id)
            result["reloading"] = True
        else:
            resume_flag = f"--resume {session_id}" if session_id else "--resume"
            print("  WARNING: Could not find Claude PID. Pruned but not reloading.")
            print(f"  Restart manually: claude {resume_flag}")

    return result


def _is_cozempic_watcher_process(pid: int) -> bool:
    """Verify that `pid` is a cozempic reload watcher (bash + cozempic watcher script).

    Guards against false positives from pgrep substring matching.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            return False
        args = (result.stdout or "").strip()
        # Real watcher script contains both "bash" and "Cozempic guard resumed Claude"
        return "bash" in args and "Cozempic guard resumed Claude" in args
    except (subprocess.SubprocessError, OSError):
        return False


def _cleanup_stale_watchers() -> None:
    """Kill stale reload watchers from previous Cozempic versions.

    Old watchers (pre-1.6.10) had hardcoded resume commands without flag
    detection. They linger as zombie processes waiting for Claude to exit.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cozempic.*resumed Claude"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\n"):
            if pid_str:
                try:
                    pid = int(pid_str)
                    if _is_cozempic_watcher_process(pid):
                        os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, ValueError):
                    pass
    except Exception:
        pass


def _detect_skip_permissions(pid: int) -> bool:
    """Check if the Claude process was launched with --dangerously-skip-permissions."""
    flags = _detect_claude_flags(pid)
    return "--dangerously-skip-permissions" in flags


def _detect_claude_flags(pid: int) -> str:
    """Extract CLI flags from the running Claude process.

    Returns the flags portion of the command line (everything after 'claude'
    but excluding --resume/--continue and the session ID).

    Uses psutil for accurate argv preservation (preserves spaces in values).
    Falls back to ps -o args= with shlex.split when psutil is unavailable.
    """
    import shlex

    parts: list[str] = []

    # Preferred path: psutil preserves original argv boundaries exactly.
    try:
        import psutil
        parts = psutil.Process(pid).cmdline()
    except (ImportError, Exception):
        pass

    # Fallback: ps -o args= + shlex.split (loses space-boundary info on macOS).
    if not parts:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True, timeout=5,
            )
            raw = result.stdout.strip()
            if not raw or "claude" not in raw:
                return ""
            parts = shlex.split(raw)
        except Exception:
            return ""

    if not parts:
        return ""

    # Find 'claude' binary in the argv list.
    claude_idx = next((i for i, p in enumerate(parts) if p.endswith("claude")), -1)
    if claude_idx < 0:
        return ""

    tokens = parts[claude_idx + 1:]

    # Walk tokens pairing --flags with their values.
    # Consecutive non-flag tokens are joined as a single value (preserves paths
    # with spaces when the argv source can provide them).
    # Flags/values containing shell metacharacters are dropped to prevent injection.
    _shell_metachars = set(';`$|()')
    cleaned: list[str] = []
    skip_count = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if skip_count > 0:
            skip_count -= 1
            i += 1
            continue

        # Skip resume/continue flags and their session ID argument
        if tok in ("--resume", "--continue", "-c"):
            skip_count = 1
            i += 1
            continue

        # Skip bare UUID-like session ID args
        if len(tok) >= 32 and "-" in tok and not tok.startswith("-"):
            i += 1
            continue

        if tok.startswith("-"):
            # Collect all following non-flag tokens as this flag's value
            j = i + 1
            while j < len(tokens) and not tokens[j].startswith("-"):
                j += 1
            value_tokens = tokens[i + 1:j]
            value = " ".join(value_tokens) if value_tokens else ""

            # Drop flag+value if value contains shell injection metacharacters
            if any(c in _shell_metachars for c in value):
                i = j
                continue

            if value:
                cleaned.append(tok)
                cleaned.append(shlex.quote(value))
            else:
                cleaned.append(tok)
            i = j
        else:
            # Bare non-flag token (shouldn't be common after flag extraction)
            if not any(c in _shell_metachars for c in tok):
                cleaned.append(shlex.quote(tok))
            i += 1

    return " ".join(cleaned)


def _detect_terminal_env() -> str:
    """Detect the terminal environment: 'tmux', 'screen', 'ssh', or 'plain'."""
    if os.environ.get("TMUX"):
        return "tmux"
    if os.environ.get("STY"):
        return "screen"
    if is_ssh_session():
        return "ssh"
    return "plain"


def _wait_for_exit(pid: int, timeout: float = 5.0) -> bool:
    """Wait for a process to exit. Returns True if exited, False if still alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.2)
        except (ProcessLookupError, PermissionError, OSError):
            return True
    return False


def _terminate_and_resume(claude_pid: int, project_dir: str, session_id: str | None = None) -> None:
    """Gracefully exit Claude and resume in the same terminal where possible.

    Priority:
      1. tmux/screen: send-keys "/exit" → wait → send-keys "claude --resume" (same pane)
      2. Plain terminal: SIGTERM → open new terminal with resume
      3. SSH: skip terminate, print manual instructions
    """
    resume_flag = f"--resume {session_id}" if session_id else "--resume"

    # Preserve all CLI flags from the original Claude process
    original_flags = _detect_claude_flags(claude_pid)
    resume_cmd = f"claude {original_flags} {resume_flag}".replace("  ", " ").strip()
    term_env = _detect_terminal_env()
    system = platform.system()

    if term_env == "ssh":
        print(f"  SSH session — skipping terminate+resume. Resume manually: {resume_cmd}")
        return

    # Verify the PID still belongs to a Claude process before sending any signal.
    # claude_pid is captured at daemon start; it may have been recycled.
    if not _is_claude_process(claude_pid):
        print(f"  WARNING: PID {claude_pid} is no longer a Claude process — skipping terminate+resume.")
        return

    if term_env == "tmux":
        # tmux: graceful /exit via send-keys, then resume in same pane
        pane = os.environ.get("TMUX_PANE", "")
        target = f"-t {pane}" if pane else ""
        print(f"  tmux detected — sending /exit and auto-resuming in same pane...")

        # Send /exit to Claude
        subprocess.run(
            ["tmux", "send-keys", *(["-t", pane] if pane else []), "/exit", "Enter"],
            capture_output=True, timeout=5,
        )

        # Wait for Claude to exit
        if not _wait_for_exit(claude_pid, timeout=10.0):
            if _is_claude_process(claude_pid):
                os.kill(claude_pid, signal.SIGTERM)
            _wait_for_exit(claude_pid, timeout=5.0)

        time.sleep(1)

        # Resume in same pane
        subprocess.run(
            ["tmux", "send-keys", *(["-t", pane] if pane else []),
             f"cd {shell_quote(project_dir)} && {resume_cmd}", "Enter"],
            capture_output=True, timeout=5,
        )
        return

    if term_env == "screen":
        # GNU screen: similar to tmux
        screen_session = os.environ.get("STY", "")
        print(f"  screen detected — sending /exit and auto-resuming...")

        subprocess.run(
            ["screen", "-S", screen_session, "-X", "stuff", "/exit\n"],
            capture_output=True, timeout=5,
        )

        if not _wait_for_exit(claude_pid, timeout=10.0):
            if _is_claude_process(claude_pid):
                os.kill(claude_pid, signal.SIGTERM)
            _wait_for_exit(claude_pid, timeout=5.0)

        time.sleep(1)

        subprocess.run(
            ["screen", "-S", screen_session, "-X", "stuff",
             f"cd {shell_quote(project_dir)} && {resume_cmd}\n"],
            capture_output=True, timeout=5,
        )
        return

    # Plain terminal — SIGTERM + spawn resume watcher
    try:
        if system == "Windows":
            if _is_claude_process(claude_pid):
                subprocess.call(["taskkill", "/PID", str(claude_pid)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            if _is_claude_process(claude_pid):
                os.kill(claude_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    if not _wait_for_exit(claude_pid, timeout=5.0):
        try:
            if system == "Windows":
                if _is_claude_process(claude_pid):
                    subprocess.call(["taskkill", "/F", "/PID", str(claude_pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                if _is_claude_process(claude_pid):
                    os.kill(claude_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    _spawn_reload_watcher(claude_pid, project_dir, session_id=session_id)


def _spawn_reload_watcher(claude_pid: int, project_dir: str, session_id: str | None = None):
    """Spawn a detached watcher that resumes Claude after exit."""
    resume_flag = f"--resume {session_id}" if session_id else "--resume"
    original_flags = _detect_claude_flags(claude_pid)
    if original_flags:
        resume_flag = f"{original_flags} {resume_flag}"

    # SSH sessions can't open GUI terminals — skip auto-resume
    if is_ssh_session():
        print(f"  SSH session detected — skipping auto-resume.")
        print(f"  Resume manually: cd {project_dir} && claude {resume_flag}")
        return

    system = platform.system()

    # log_dir is a bash-safe representation of project_dir for the echo log line.
    # shell_quote wraps in single quotes (POSIX safe); metachars are not executable.
    log_dir = shell_quote(project_dir)

    if system == "Darwin":
        resume_cmd = (
            f"osascript -e 'tell application \"Terminal\" to do script "
            f"\"cd {shell_quote(project_dir)} && claude {resume_flag}\"'"
        )
    elif system == "Linux":
        resume_cmd = (
            f"if command -v gnome-terminal >/dev/null 2>&1; then "
            f"gnome-terminal -- bash -c 'cd {shell_quote(project_dir)} && claude {resume_flag}; exec bash'; "
            f"elif command -v xterm >/dev/null 2>&1; then "
            f"xterm -e 'cd {shell_quote(project_dir)} && claude {resume_flag}' & "
            f"else echo 'No terminal emulator found' >> /tmp/cozempic_guard.log; fi"
        )
    elif system == "Windows":
        # Escape cmd.exe metacharacters in project_dir so they cannot execute.
        # ^ is the cmd.exe escape character; prefix each metachar with ^ to
        # prevent them from being interpreted as shell operators.
        _cmd_metachars = set('&|<>^"')
        escaped_dir = "".join(f"^{c}" if c in _cmd_metachars else c for c in project_dir)
        resume_cmd = (
            f"start cmd /c \"cd /d {escaped_dir} && claude {resume_flag}\""
        )
        # Use escaped form in log line too so the watcher_script has no raw metachars
        log_dir = escaped_dir
    else:
        print(f"  WARNING: Auto-resume not supported on {system}.")
        return

    watcher_script = (
        f"while kill -0 {int(claude_pid)} 2>/dev/null; do sleep 1; done; "
        f"sleep 1; "
        f"{resume_cmd}; "
        f"echo \"$(date): Cozempic guard resumed Claude in {log_dir}\" >> /tmp/cozempic_guard.log"
    )

    subprocess.Popen(
        ["bash", "-c", watcher_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# UUID-shape / hex-only guard for session_id inputs to pidfile path composition
# (BUG-G13). 12+ chars keeps the `[:12]` truncation meaningful; the hex+dash
# character class rejects path-traversal sequences and any non-UUID identifier.
# Require a hex digit as the first char so pure-dash / leading-dash inputs
# reject — real UUIDs always start with a hex digit.
# Note: `_pid_file_for_session` lowercases session_id BEFORE matching, so the
# regex intentionally accepts lowercase hex only (not an RFC-4122 uppercase bug).
_SESSION_ID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{11,}$")


def _pid_file_for_session(session_id: str) -> Path:
    """Return the PID file path for a guard daemon watching a specific session.

    Validates `session_id` against a UUID-shaped regex (hex chars + dashes,
    length >= 12) so that path-traversal sequences or stray filename tokens
    cannot escape `/tmp/cozempic_guard_*.pid` namespace — see BUG-G13.
    Normalizes to lowercase BEFORE truncation so different-case variants of
    the same UUID map to the same pidfile (prevents split-brain spawning).
    Raises ValueError on malformed input so callers fail fast; library-API
    callers like `_is_guard_running_for_session` catch and return None
    (treat invalid session as "no daemon"). Error message logs only type
    and length — never raw content — to avoid PII leaks.
    """
    session_id = _normalize_session_id(session_id).lower()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            f"session_id must be a hex/UUID identifier (>=12 chars), "
            f"got {type(session_id).__name__} of length {len(session_id)}"
        )
    return Path("/tmp") / f"cozempic_guard_{session_id[:12]}.pid"


def _pid_file_for_cwd(cwd: str) -> Path:
    """Legacy: PID file keyed by CWD hash. Used for migration cleanup only."""
    import hashlib
    slug = hashlib.md5(cwd.encode()).hexdigest()[:12]
    return Path("/tmp") / f"cozempic_guard_{slug}.pid"


def _cleanup_legacy_pid(cwd: str) -> None:
    """Remove old CWD-keyed PID files from pre-1.6.13 installations."""
    legacy = _pid_file_for_cwd(cwd)
    if legacy.exists():
        try:
            pid = int(legacy.read_text().strip())
            os.kill(pid, 0)
            # Only SIGTERM if we can confirm this is actually our daemon.
            if _is_cozempic_guard_process(pid):
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
        legacy.unlink(missing_ok=True)
    # Also clean session file
    legacy_sess = Path(str(legacy).replace(".pid", "_session.txt"))
    legacy_sess.unlink(missing_ok=True)


def _is_guard_running_for_session(session_id: str) -> int | None:
    """Check if a guard daemon is already running for this specific session.

    Returns the PID if running, None otherwise.

    An invalid `session_id` (non-UUID) is treated as "no daemon" (None)
    rather than raising — library-API safety. Callers outside the CLI
    (hooks, pytest, third-party integrations) should get a safe default
    instead of a ValueError propagating up from `_pid_file_for_session`.
    """
    norm_sid = _normalize_session_id(session_id)
    try:
        pid_path = _pid_file_for_session(session_id)
    except ValueError:
        # Invalid session_id shape — no daemon can exist for it.
        return None
    if not pid_path.exists():
        return None

    try:
        pid = int(pid_path.read_text().strip())
        if pid <= 0:
            # Guard against PID-reuse footgun: os.kill(0, sig) broadcasts to
            # the caller's process group rather than targeting a sentinel.
            # If a concurrent start_guard_daemon in THIS process holds the
            # session spawn lock, the placeholder is live — skip the unlink.
            with _spawn_locks_mu:
                lock = _spawn_locks.get(norm_sid)
            if lock is not None and not lock.acquire(blocking=False):
                # Lock is held → spawner is in-flight; placeholder is live.
                return None
            if lock is not None:
                lock.release()
            pid_path.unlink(missing_ok=True)
            return None
        os.kill(pid, 0)
        # Verify the PID is actually our guard — defend against PID reuse.
        if not _is_cozempic_guard_process(pid):
            pid_path.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pid_path.unlink(missing_ok=True)
        return None


# Backward compat aliases
def _pid_file(cwd: str) -> Path:
    return _pid_file_for_cwd(cwd)


def start_guard_daemon(
    cwd: str | None = None,
    threshold_mb: float = 50.0,
    soft_threshold_mb: float | None = None,
    rx_name: str = "standard",
    interval: int = 30,
    auto_reload: bool = True,
    reactive: bool = True,
    threshold_tokens: int | None = None,
    soft_threshold_tokens: int | None = None,
    session_id: str | None = None,
    claude_pid: int | None = None,
) -> dict:
    """Start the guard as a background daemon.

    Spawns a detached subprocess running `cozempic guard` with output
    redirected to a log file. Uses a PID file to prevent double-starts.

    Pre-validates numeric parameters before spawning the child process.
    Without this, bad values (negative thresholds, zero intervals) would
    pass to the child via CLI args, be accepted by argparse (which only
    runs in the child), and cause the child to die immediately — while
    the caller sees started=True.

    Returns dict with: started (bool), pid (int|None), pid_file, log_file,
    already_running (bool).
    """
    from ._validation import ConfigError

    if threshold_mb is not None and threshold_mb <= 0:
        raise ConfigError(f"threshold_mb must be positive, got {threshold_mb}")
    if soft_threshold_mb is not None and soft_threshold_mb <= 0:
        raise ConfigError(f"soft_threshold_mb must be positive, got {soft_threshold_mb}")
    if soft_threshold_mb is not None and threshold_mb is not None and soft_threshold_mb >= threshold_mb:
        raise ConfigError(
            f"soft_threshold_mb ({soft_threshold_mb}) must be strictly less than "
            f"threshold_mb ({threshold_mb})"
        )
    if interval is not None and interval <= 0:
        raise ConfigError(f"interval must be positive, got {interval}")
    if threshold_tokens is not None and threshold_tokens <= 0:
        raise ConfigError(f"threshold_tokens must be positive, got {threshold_tokens}")
    if soft_threshold_tokens is not None and soft_threshold_tokens <= 0:
        raise ConfigError(f"soft_threshold_tokens must be positive, got {soft_threshold_tokens}")

    cwd = cwd or os.getcwd()

    # Migrate: clean up legacy CWD-keyed PID files from pre-1.6.13
    _cleanup_legacy_pid(cwd)

    # If we have a session_id, check if a guard already exists for THIS session
    if session_id:
        existing_pid = _is_guard_running_for_session(session_id)
        if existing_pid:
            return {
                "started": False,
                "pid": existing_pid,
                "pid_file": str(_pid_file_for_session(session_id)),
                "log_file": None,
                "already_running": True,
            }
    else:
        # No session_id — detect from CWD (backward compat with old hooks)
        sess = find_current_session(cwd)
        if sess:
            session_id = sess.get("session_id", "")

        if session_id:
            existing_pid = _is_guard_running_for_session(session_id)
            if existing_pid:
                return {
                    "started": False,
                    "pid": existing_pid,
                    "pid_file": str(_pid_file_for_session(session_id)),
                    "log_file": None,
                    "already_running": True,
                }

    # Normalize early — session_id may be a full .jsonl path from the hook's
    # $TRANSCRIPT variable. Must extract the UUID before using it as a filename
    # component (otherwise "/Users/foo/..." ends up in the log/pid path).
    if session_id:
        session_id = _normalize_session_id(session_id)

    # Use session_id for PID file if available, fall back to CWD hash.
    # Route through `_pid_file_for_session` so the UUID-shape / lowercase /
    # hex-first-char validation applies at the spawn path too. Without this
    # the write-side builds a different path than the read-side helper
    # (`_is_guard_running_for_session`), and the caller's own daemon becomes
    # an unreachable orphan for non-UUID session ids.
    if session_id:
        try:
            pid_path = _pid_file_for_session(session_id)
        except ValueError as e:
            return {
                "started": False,
                "reason": f"invalid session_id: {e}",
                "pid": None,
                "pid_file": None,
                "log_file": None,
                "already_running": False,
            }
        log_file = pid_path.with_suffix(".log")
    else:
        import hashlib
        pid_key = hashlib.md5(cwd.encode()).hexdigest()[:12]
        log_file = Path("/tmp") / f"cozempic_guard_{pid_key}.log"
        pid_path = Path("/tmp") / f"cozempic_guard_{pid_key}.pid"

    if claude_pid is None:
        claude_pid = find_claude_pid()

    # Acquire the per-session spawn lock before O_CREAT so that a concurrent
    # _is_guard_running_for_session (same process) sees the lock is held and skips
    # the unlink of our in-flight "0" placeholder. Released after the real PID
    # is committed. File-level O_CREAT|O_EXCL still guards cross-process races.
    norm_sid = _normalize_session_id(session_id) if session_id else ""
    with _spawn_locks_mu:
        if norm_sid not in _spawn_locks:
            _spawn_locks[norm_sid] = threading.Lock()
        spawn_lock = _spawn_locks[norm_sid]
    spawn_lock.acquire()

    # Atomically claim the pid slot before spawning (O_CREAT|O_EXCL prevents TOCTOU).
    # Other OSErrors (ENOSPC, EROFS, EACCES) are non-fatal: return started=False
    # with a reason so the non-interactive SessionStart hook doesn't crash silently.
    _claim_result: dict | None = None
    try:
        try:
            fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, b"0")  # placeholder; real PID written after Popen
            finally:
                os.close(fd)
        except (FileExistsError, OSError) as _e:
            if not isinstance(_e, FileExistsError):
                _claim_result = {"started": False, "reason": f"pidfile: {_e}"}
            else:
                # Peek at what the existing file holds to decide the retry strategy.
                try:
                    _raw = pid_path.read_text().strip() if pid_path.exists() else ""
                    _on_disk_pid = int(_raw) if _raw else 0
                except (ValueError, OSError):
                    _on_disk_pid = 0
                if _on_disk_pid > 0:
                    # File holds a real PID (> 0): a concurrent winner just wrote it.
                    # Trust the write — don't call _is_guard_running_for_session (it
                    # would probe os.kill which may fail for a just-spawned process,
                    # unlink the file, and break the invariant). Dead-guard detection
                    # happens on the NEXT call to start_guard_daemon.
                    _claim_result = {
                        "started": False,
                        "pid": _on_disk_pid,
                        "pid_file": str(pid_path),
                        "log_file": None,
                        "already_running": True,
                    }
                else:
                    # File holds a placeholder (pid <= 0): either a concurrent spawn's
                    # in-flight "0" or a stale placeholder from a previous crash.
                    # Re-read via the full helper which handles the spawn-lock check.
                    existing_pid = _is_guard_running_for_session(session_id) if session_id else None
                    if existing_pid:
                        _claim_result = {
                            "started": False,
                            "pid": existing_pid,
                            "pid_file": str(pid_path),
                            "log_file": None,
                            "already_running": True,
                        }
                    else:
                        # Stale placeholder (previous crash before real PID was written) — remove and retry once
                        pid_path.unlink(missing_ok=True)
                        try:
                            fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            try:
                                os.write(fd, b"0")
                            finally:
                                os.close(fd)
                        except (FileExistsError, OSError) as _e2:
                            if not isinstance(_e2, FileExistsError):
                                _claim_result = {"started": False, "reason": f"pidfile: {_e2}"}
                            else:
                                existing_pid = _is_guard_running_for_session(session_id) if session_id else None
                                _claim_result = {
                                    "started": False,
                                    "pid": existing_pid,
                                    "pid_file": str(pid_path),
                                    "log_file": None,
                                    "already_running": True,
                                }
    except Exception:
        spawn_lock.release()
        raise
    if _claim_result is not None:
        spawn_lock.release()
        return _claim_result

    # Build the guard command
    cmd_parts = [
        sys.executable, "-m", "cozempic.cli", "guard",
        "--cwd", cwd,
        "--threshold", str(threshold_mb),
        "--interval", str(interval),
        "-rx", rx_name,
    ]
    if soft_threshold_mb is not None:
        cmd_parts.extend(["--soft-threshold", str(soft_threshold_mb)])
    if not auto_reload:
        cmd_parts.append("--no-reload")
    if not reactive:
        cmd_parts.append("--no-reactive")
    if threshold_tokens is not None:
        cmd_parts.extend(["--threshold-tokens", str(threshold_tokens)])
    if soft_threshold_tokens is not None:
        cmd_parts.extend(["--soft-threshold-tokens", str(soft_threshold_tokens)])
    if session_id is not None:
        cmd_parts.extend(["--session", _normalize_session_id(session_id)])
    if claude_pid is not None:
        cmd_parts.extend(["--claude-pid", str(claude_pid)])

    # Spawn detached process. Wrapped in try/finally so the spawn_lock is
    # ALWAYS released and the "0" placeholder is cleaned up if Popen (or
    # anything else in this block) raises. Without this, a FileNotFoundError
    # from a missing Python binary or a PermissionError on the log file would
    # leave the lock held permanently (blocking all future in-process spawns
    # for this session) and the placeholder pidfile on disk (blocking all
    # future cross-process spawns until manual deletion).
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            from datetime import datetime
            lf.write(f"\n--- Guard daemon started at {datetime.now().isoformat()} ---\n")
            lf.write(f"CWD: {cwd}\n")
            lf.write(f"CMD: {' '.join(cmd_parts)}\n\n")
            lf.flush()

            # PYTHONUNBUFFERED=1 ensures guard log output is written immediately (#14)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                cmd_parts,
                stdout=lf,
                stderr=lf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=cwd,
                env=env,
            )

        # Write actual PID atomically via temp+rename so readers never see partial content
        tmp_path = pid_path.with_suffix(".pid.tmp")
        tmp_path.write_text(str(proc.pid))
        tmp_path.replace(pid_path)
    except Exception:
        # Cleanup: remove the "0" placeholder so future spawns aren't blocked
        pid_path.unlink(missing_ok=True)
        raise
    finally:
        # Release the spawn lock regardless of success/failure. Any concurrent
        # _is_guard_running_for_session can now read the valid PID > 0 (success)
        # or find no pidfile (failure — cleaned up above).
        spawn_lock.release()

    return {
        "started": True,
        "pid": proc.pid,
        "pid_file": str(pid_path),
        "log_file": str(log_file),
        "already_running": False,
    }


def _is_cozempic_guard_process(pid: int) -> bool:
    """Verify that `pid` is actually a cozempic guard daemon before we signal it.

    Guards against PID reuse: when our daemon exits and the kernel recycles
    its PID to an unrelated user process, a blind `os.kill(pid, SIGTERM)` on
    the recycled PID is a confused-deputy bug (we'd kill something arbitrary).
    Inspects the process's argv; requires BOTH "cozempic.cli guard" (matches
    our spawn pattern in start_guard_daemon) OR the explicit entry-point
    "cozempic guard" — not just substring "cozempic" + "guard" which could
    match unrelated things like `vim /tmp/cozempic_guard_notes.md`.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            return False
        args = (result.stdout or "").strip()
        tokens = args.split()
        if not tokens:
            return False
        binary = Path(tokens[0]).name.lower()
        # tokens[0] must be a python interpreter (any minor/patch version) or
        # the cozempic entry-point. Rejects `run-cozempic`, `fake-cozempic`,
        # `python-attacker`. Accepts `python3.11`, `python3.13.12`, etc. used
        # by pyenv / Homebrew / distro packaging.
        if not (binary == "cozempic" or re.fullmatch(r"^python(\d+(\.\d+)*)?$", binary)):
            return False
        # "cozempic.cli" and "guard" must appear as discrete arg tokens, not as
        # substrings in filenames/paths (grep, less, vim on our source tree).
        if "cozempic.cli" in tokens and "guard" in tokens:
            return True
        if len(tokens) >= 2 and binary == "cozempic" and tokens[1] == "guard":
            return True
        return False
    except (subprocess.SubprocessError, OSError):
        # If we can't verify, err on the side of NOT signaling a potentially
        # unrelated process. The session stays with the existing daemon (or
        # no daemon), which is strictly safer than signaling the wrong one.
        return False


def _is_claude_process(pid: int) -> bool:
    """Verify that `pid` is a Claude Code process (node/claude binary).

    Mirrors _is_cozempic_guard_process but for the Claude client side.
    Guards against PID reuse: if Claude exits and its PID is recycled, a blind
    SIGTERM on the recycled PID is a confused-deputy bug.

    On Windows, `ps` is unavailable — uses `tasklist /FI "PID eq <pid>" /FO CSV`
    instead. If tasklist also fails, falls back to liveness-only (returns True
    for a live PID) so callers can still proceed with taskkill.
    """
    if platform.system() == "Windows":
        return _is_claude_process_windows(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            return False
        args = (result.stdout or "").strip()
        tokens = args.split()
        if not tokens:
            return False
        binary = Path(tokens[0]).name.lower()
        # Match native claude binary (whole name, not substring)
        if binary == "claude":
            return True
        # Match node-based Claude Code: binary must be exactly "node" or "node.js"
        # AND args must contain a Claude Code-specific marker.
        if binary in ("node", "node.js"):
            if "@anthropic-ai/claude-code" in args:
                return True
            # cli.js under a claude-code directory
            if "claude-code/cli.js" in args or "claude-code\\cli.js" in args:
                return True
        return False
    except (subprocess.SubprocessError, OSError):
        return False


def _is_claude_process_windows(pid: int) -> bool:
    """Windows-specific helper: probe via tasklist /FO CSV."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return True  # liveness fallback — let caller proceed with taskkill
        output = (result.stdout or "").strip().lower()
        if not output or "no tasks are running" in output:
            return False
        # CSV row: "image_name","pid","session_name","session#","mem_usage"
        # Image name is the first quoted field.
        image_name = output.split(",")[0].strip('"')
        return any(marker in image_name for marker in ("claude", "node"))
    except (subprocess.SubprocessError, OSError):
        return True  # liveness fallback — let caller proceed with taskkill


def _pid_file_points_to(session_id: str, expected_pid: int) -> bool:
    """CAS helper: return True if the session pid file currently contains
    `expected_pid`. Used before unlink() to avoid clobbering a fresh pid
    file written by a concurrent SessionStart hook.
    """
    try:
        path = _pid_file_for_session(session_id)
        if not path.exists():
            return False
        return int(path.read_text().strip()) == expected_pid
    except (ValueError, OSError):
        return False


def reload_self_daemon(
    cwd: str | None = None,
    session_id: str | None = None,
    threshold_mb: float = 50.0,
    soft_threshold_mb: float | None = None,
    rx_name: str = "standard",
    interval: int = 30,
    auto_reload: bool = True,
    reactive: bool = True,
    threshold_tokens: int | None = None,
    soft_threshold_tokens: int | None = None,
) -> dict:
    """Gracefully restart the running guard daemon for this session.

    Used after an in-place cozempic upgrade so the daemon picks up the new code
    on disk. SIGTERMs the existing daemon (it writes a final checkpoint via the
    SIGTERM handler), waits for it to exit, then spawns a fresh daemon with the
    same args. The new daemon imports from the freshly-installed package files.

    Returns dict: {reloaded: bool, old_pid, new_pid, log_file, reason}.
    """
    cwd = cwd or os.getcwd()

    if not session_id:
        sess = find_current_session(cwd)
        if sess:
            session_id = sess.get("session_id", "")

    if not session_id:
        return {"reloaded": False, "reason": "could not detect session"}

    session_id = _normalize_session_id(session_id)

    old_pid = _is_guard_running_for_session(session_id)
    if not old_pid:
        return {"reloaded": False, "reason": "no daemon running for session"}

    # Verify the PID is actually our daemon — defend against PID reuse.
    if not _is_cozempic_guard_process(old_pid):
        # Stale pid file pointing at a recycled (non-cozempic) PID. Clear it
        # (only if it still points at the stale pid — CAS) and spawn fresh;
        # do NOT signal the unrelated process.
        if _pid_file_points_to(session_id, old_pid):
            _pid_file_for_session(session_id).unlink(missing_ok=True)
        old_pid = None
    else:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            if _pid_file_points_to(session_id, old_pid):
                _pid_file_for_session(session_id).unlink(missing_ok=True)
            old_pid = None

        if old_pid is not None and not _wait_for_exit(old_pid, timeout=10.0):
            # Didn't exit on SIGTERM — escalate, but only if we still see our
            # daemon (guard against the unlikely race where another process
            # grabbed the PID right as the old daemon finally died).
            if _is_cozempic_guard_process(old_pid):
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            # CAS unlink — don't wipe a fresh pid file from a concurrent spawn
            if _pid_file_points_to(session_id, old_pid):
                _pid_file_for_session(session_id).unlink(missing_ok=True)
        elif old_pid is not None:
            # Clean exit. CAS unlink — if a concurrent SessionStart hook
            # already spawned a new daemon and rewrote the pid file with its
            # PID, we leave that fresh file alone.
            if _pid_file_points_to(session_id, old_pid):
                _pid_file_for_session(session_id).unlink(missing_ok=True)

    # Always re-activate what we just disabled. Retry once on transient failures,
    # but NOT on `already_running` (that means a concurrent SessionStart hook
    # already spawned a new daemon — accept that one, don't start a second).
    daemon_args = dict(
        cwd=cwd,
        threshold_mb=threshold_mb,
        soft_threshold_mb=soft_threshold_mb,
        rx_name=rx_name,
        interval=interval,
        auto_reload=auto_reload,
        reactive=reactive,
        threshold_tokens=threshold_tokens,
        soft_threshold_tokens=soft_threshold_tokens,
        session_id=session_id,
    )
    result = start_guard_daemon(**daemon_args)
    if not result.get("started") and not result.get("already_running"):
        time.sleep(1)
        # Only clear a pid file we know is stale (pointing at a dead pid).
        # Do NOT blindly unlink — a live concurrent daemon may have written it.
        pid_path = _pid_file_for_session(session_id)
        try:
            if pid_path.exists():
                stale_pid = int(pid_path.read_text().strip())
                try:
                    os.kill(stale_pid, 0)
                    # Still alive — leave the pid file alone and let
                    # start_guard_daemon below return already_running.
                except (ProcessLookupError, PermissionError):
                    pid_path.unlink(missing_ok=True)
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
        result = start_guard_daemon(**daemon_args)

    reloaded = bool(result.get("started") or result.get("already_running"))
    if reloaded:
        reason = "ok"
    else:
        reason = "could not start fresh daemon after retry — session is unprotected"

    return {
        "reloaded": reloaded,
        "old_pid": old_pid,
        "new_pid": result.get("pid"),
        "log_file": result.get("log_file"),
        "reason": reason,
    }


def _fmt_prune_result(result: dict) -> str:
    """Format a prune cycle result, leading with tokens if available."""
    orig_tok = result.get("original_tokens")
    final_tok = result.get("final_tokens")
    if orig_tok and final_tok:
        saved_tok = orig_tok - final_tok
        tok_str = f"{saved_tok / 1000:.1f}K" if saved_tok >= 1000 else str(saved_tok)
        pct = f"{saved_tok / orig_tok * 100:.1f}%" if orig_tok > 0 else "0%"
        return f"{tok_str} tokens freed ({pct}), {result['saved_mb']:.1f}MB saved"
    return f"{result['saved_mb']:.1f}MB saved"


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")
