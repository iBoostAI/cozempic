"""Behavioral digest — extract correction signals from sessions, persist as structured rules.

Research basis: Reflexion (NeurIPS 2023), ExpeL (AAAI 2023), A-MAC (2603.04549),
Lost in the Middle, IFScale. See docs/behavioral-digest-design.md.

Phase 1: heuristic extraction + A-MAC admission gate + JSON persistence.
No injection yet (Phase 2). No LLM calls (heuristic only for Phase 1).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .helpers import get_content_blocks, get_msg_type, text_of
from .types import Message

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTECTION_TAG = "__cozempic_behavioral_digest__"
DIGEST_DIR = Path.home() / ".cozempic"
DIGEST_FILE = DIGEST_DIR / "behavioral-digest.json"
DIGEST_MD_FILE = DIGEST_DIR / "behavioral-digest.md"

MAX_ACTIVE_RULES = 20  # IFScale: >30 irrelevant rules degrades ALL adherence
ADMISSION_THRESHOLD = 0.55  # A-MAC composite score gate
PRUNE_THRESHOLD = 0.30  # Below this → prune
PROMOTION_COUNT = 2  # Occurrences needed to promote pending → active (was 3, too high for real usage)
DECAY_DAYS = 30  # Universal decay period (MemoryArena 2602.16313)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DigestRule:
    """A single behavioral rule extracted from user corrections."""

    id: str  # R001, R002, etc.
    rule: str  # "Do not [X]" — prohibition framing
    priority: Literal["hard", "soft"] = "soft"
    scope: str = "general"  # git, file-ops, testing, communication, general
    trigger: str = ""  # When this rule applies

    # Decision attribution (Trajectory-Informed Memory 2603.10600)
    decision_step: str = ""  # Which step in agent's reasoning caused failure
    before: str = ""  # What agent did wrong
    after: str = ""  # What user wants instead

    # Evidence — stored verbatim, never paraphrased (2603.23013)
    signal: str = ""  # Why agent made the error
    evidence: str = ""  # Direct quote from conversation

    # Scoring (A-MAC 2603.04549)
    importance: int = 1  # ExpeL voting count
    source_reliability: float = 1.0  # 1.0 explicit, 0.6 implicit, 0.3 inferred
    type_prior: float = 0.8  # correction=0.8, preference=0.9, one-off=0.1

    # Lifecycle
    status: Literal["pending", "active", "conflicted"] = "pending"
    occurrence_count: int = 1
    first_seen: str = ""
    last_reinforced: str = ""
    last_injection: str | None = None


@dataclass
class DigestStore:
    """Persistent store for behavioral rules."""

    strategy_rules: list[DigestRule] = field(default_factory=list)
    version: str = "1"
    project: str = ""
    updated: str = ""
    session_id: str = ""

    def is_empty(self) -> bool:
        return not self.strategy_rules

    def active_rules(self) -> list[DigestRule]:
        return [r for r in self.strategy_rules if r.status == "active"]

    def all_rules(self) -> list[DigestRule]:
        return self.strategy_rules

    def next_id(self) -> str:
        existing = {r.id for r in self.all_rules()}
        for i in range(1, 1000):
            rid = f"R{i:03d}"
            if rid not in existing:
                return rid
        return f"R{len(self.all_rules()) + 1:03d}"


# ---------------------------------------------------------------------------
# Classification — FELT taxonomy (heuristic, no LLM)
# ---------------------------------------------------------------------------

# Correction signal patterns
_EXPLICIT_PATTERNS = [
    re.compile(r"^no[,.\s]", re.IGNORECASE),
    re.compile(r"\bdon'?t\b", re.IGNORECASE),
    re.compile(r"\bdo not\b", re.IGNORECASE),
    re.compile(r"\bstop\s+\w+ing\b", re.IGNORECASE),  # "stop summarizing", "stop adding", etc.
    re.compile(r"\bnever\b", re.IGNORECASE),
    re.compile(r"\bplease\s+(don'?t|remove|stop|undo)", re.IGNORECASE),
    re.compile(r"\bremove\s+that\b", re.IGNORECASE),
    re.compile(r"\bundo\s+(that|this|the)\b", re.IGNORECASE),
]

# Synthetic-noise markers — user turns containing any of these are Claude Code
# framework emissions (hooks, slash commands, tool blocks), not real corrections.
_SYSTEM_NOISE_MARKERS = (
    "<local-command-",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<teammate-message",
    "<system-reminder",
    "<function_calls>",
    "<function_results>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook",
    "Please analyze this codebase",  # /init prompt
)


def _is_system_noise(text: str) -> bool:
    """Return True if `text` is a Claude Code synthetic/framework turn.

    Rejects: empty text, tag-wrapped blocks, slash-command lines, and known
    framework prompt markers. Used to gate `extract_corrections` upstream of
    `classify_turn` so synthetic turns never become behavioral rules.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # Tag-like: any line starting with '<' is either synthetic or XML.
    if stripped.startswith("<"):
        return True
    # Slash command: '/' + lowercase letter (distinguish from file paths like /Users)
    if len(stripped) >= 2 and stripped[0] == "/" and stripped[1].islower():
        return True
    # Known framework markers (substring match).
    for marker in _SYSTEM_NOISE_MARKERS:
        if marker in stripped:
            return True
    return False

_IMPLICIT_PATTERNS = [
    re.compile(r"\bactually[,\s]", re.IGNORECASE),
    re.compile(r"\binstead[,\s]", re.IGNORECASE),
    re.compile(r"\brather\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+(not|wrong)", re.IGNORECASE),
    re.compile(r"\bnot\s+what\s+I", re.IGNORECASE),
    re.compile(r"\buse\s+\w+\s+not\s+\w+", re.IGNORECASE),  # "use Edit not Write"
    re.compile(r"\bnot\s+\w+[,;]\s*(use|try)", re.IGNORECASE),  # "not Write, use Edit"
]

_PREFERENCE_PATTERNS = [
    re.compile(r"\bI\s+prefer\b", re.IGNORECASE),
    re.compile(r"\balways\s+(use|do|add|include|run|check)", re.IGNORECASE),
    re.compile(r"\bfrom\s+now\s+on\b", re.IGNORECASE),
    re.compile(r"\bremember\s+(to|that)\b", re.IGNORECASE),
    re.compile(r"\bmake\s+sure\s+(to|you)\b", re.IGNORECASE),
]

_APOLOGY_PATTERNS = [
    re.compile(r"\bsorry\b", re.IGNORECASE),
    re.compile(r"\bI\s+apologize\b", re.IGNORECASE),
    re.compile(r"\bmy\s+(mistake|bad|error)\b", re.IGNORECASE),
]

TurnClass = Literal[
    "EXPLICIT_CORRECTION",
    "IMPLICIT_CORRECTION",
    "PREFERENCE",
    "APOLOGY_FOLLOW_UP",
    "ONE_OFF",
    "NONE",
]


def classify_turn(user_text: str, prev_assistant_text: str = "") -> TurnClass:
    """Classify a user turn by correction signal type.

    Content type prior IS the dominant factor (A-MAC ablation).
    """
    if not user_text or len(user_text.strip()) < 3:
        return "NONE"

    # Check if previous assistant apologized → this turn is a follow-up correction
    if prev_assistant_text:
        for pat in _APOLOGY_PATTERNS:
            if pat.search(prev_assistant_text):
                # User message after apology is likely a correction
                if len(user_text.strip()) > 10:
                    return "APOLOGY_FOLLOW_UP"

    # Explicit correction: strongest signal
    for pat in _EXPLICIT_PATTERNS:
        if pat.search(user_text):
            return "EXPLICIT_CORRECTION"

    # Preference: persistent behavioral instruction
    for pat in _PREFERENCE_PATTERNS:
        if pat.search(user_text):
            return "PREFERENCE"

    # Implicit correction: softer signal
    for pat in _IMPLICIT_PATTERNS:
        if pat.search(user_text):
            return "IMPLICIT_CORRECTION"

    return "NONE"


# ---------------------------------------------------------------------------
# Extraction — heuristic rule extraction from classified turns
# ---------------------------------------------------------------------------


def _get_user_text(msg: dict) -> str:
    """Extract user text from a message."""
    inner = msg.get("message", {})
    content = inner.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _get_assistant_text(msg: dict) -> str:
    """Extract assistant text from a message."""
    blocks = get_content_blocks(msg)
    parts = []
    for block in blocks:
        t = text_of(block)
        if t and block.get("type") in ("text", None, ""):
            parts.append(t)
    return " ".join(parts)


def _infer_scope(text: str) -> str:
    """Infer the scope of a correction from its content."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ("git", "commit", "push", "branch", "merge", "co-authored")):
        return "git"
    if any(kw in text_lower for kw in ("file", "edit", "write", "read", "path", "directory")):
        return "file-ops"
    if any(kw in text_lower for kw in ("test", "pytest", "unittest", "mock", "assert")):
        return "testing"
    if any(kw in text_lower for kw in ("message", "comment", "pr ", "issue", "slack")):
        return "communication"
    return "general"


def _to_prohibition(text: str) -> str:
    """Convert a user correction into prohibition framing.

    "Don't add X" → "Do not add X"
    "Stop doing X" → "Do not do X"
    "No, use Y instead" → "Do not use the previous approach; use Y instead"
    """
    text = text.strip()
    # Already in prohibition form
    if text.lower().startswith("do not ") or text.lower().startswith("don't "):
        return text[0].upper() + text[1:]

    # "Stop doing X" → "Do not X"
    m = re.match(r"(?i)stop\s+(doing\s+|adding\s+|using\s+|creating\s+)?(.*)", text)
    if m:
        action = m.group(2).strip()
        return f"Do not {action}" if action else text

    # "Never X" → "Do not ever X"
    m = re.match(r"(?i)never\s+(.*)", text)
    if m:
        return f"Do not ever {m.group(1).strip()}"

    # "No, ..." → extract the instruction
    m = re.match(r"(?i)^no[,.\s]+\s*(.*)", text)
    if m:
        rest = m.group(1).strip()
        if rest:
            return rest[0].upper() + rest[1:]

    # Default: prefix with "Do not"
    if len(text) > 5:
        return f"Do not {text[0].lower()}{text[1:]}"
    return text


def extract_corrections(
    messages: list[Message],
    since_turn: int = 0,
) -> list[DigestRule]:
    """Extract behavioral corrections from a message window.

    Scans user turns for correction signals, builds DigestRule for each.
    Stores verbatim evidence — never paraphrased (arXiv:2603.23013).
    """
    now = datetime.now(timezone.utc).isoformat()
    rules: list[DigestRule] = []

    prev_assistant_text = ""
    for pos, (idx, msg, _) in enumerate(messages):
        if pos < since_turn:
            # Track assistant text even before our window
            if get_msg_type(msg) == "assistant":
                prev_assistant_text = _get_assistant_text(msg)
            continue

        mtype = get_msg_type(msg)

        if mtype == "assistant":
            prev_assistant_text = _get_assistant_text(msg)
            continue

        if mtype != "user":
            continue

        user_text = _get_user_text(msg)
        if not user_text:
            continue

        # Skip Claude Code synthetic/framework turns — they are not corrections.
        if _is_system_noise(user_text):
            prev_assistant_text = ""
            continue

        turn_class = classify_turn(user_text, prev_assistant_text)
        if turn_class == "NONE":
            prev_assistant_text = ""
            continue

        # Map classification to scoring
        reliability_map = {
            "EXPLICIT_CORRECTION": 1.0,
            "IMPLICIT_CORRECTION": 0.6,
            "PREFERENCE": 0.9,
            "APOLOGY_FOLLOW_UP": 0.8,
            "ONE_OFF": 0.3,
        }
        type_prior_map = {
            "EXPLICIT_CORRECTION": 0.8,
            "IMPLICIT_CORRECTION": 0.6,
            "PREFERENCE": 0.9,
            "APOLOGY_FOLLOW_UP": 0.7,
            "ONE_OFF": 0.1,
        }

        rule_text = _to_prohibition(user_text)
        scope = _infer_scope(user_text)

        rule = DigestRule(
            id="",  # Assigned on admission
            rule=rule_text[:500],  # Cap rule length
            priority="hard" if turn_class == "EXPLICIT_CORRECTION" else "soft",
            scope=scope,
            trigger="",
            before=prev_assistant_text[:200] if prev_assistant_text else "",
            after=user_text[:200],
            signal=turn_class,
            evidence=user_text[:500],  # Verbatim, never paraphrased
            importance=1,
            source_reliability=reliability_map.get(turn_class, 0.5),
            type_prior=type_prior_map.get(turn_class, 0.5),
            # Explicit corrections are high-confidence — active immediately
            status="active" if turn_class == "EXPLICIT_CORRECTION" else "pending",
            occurrence_count=1,
            first_seen=now,
            last_reinforced=now,
        )
        rules.append(rule)

        prev_assistant_text = ""

    return rules


# ---------------------------------------------------------------------------
# Admission gate — A-MAC composite scoring (arXiv:2603.04549)
# ---------------------------------------------------------------------------


def score_rule(rule: DigestRule, days_since_last: float = 0.0) -> float:
    """Compute A-MAC composite score for a rule.

    composite = w1*(count/3) + w2*source_reliability + w3*recency + w4*type_prior
    Threshold: 0.55 for admission, 0.30 for pruning.
    """
    evidence_score = min(rule.occurrence_count / PROMOTION_COUNT, 1.0)
    recency_decay = math.exp(-0.05 * days_since_last)  # λ=0.05 → halves in ~14 days
    composite = (
        0.25 * evidence_score
        + 0.30 * rule.source_reliability
        + 0.20 * recency_decay
        + 0.25 * rule.type_prior
    )
    return round(composite, 4)


def _normalize_for_match(text: str) -> set[str]:
    """Normalize text for duplicate matching — strip stop words, lowercase."""
    _STOP = {"do", "not", "don't", "dont", "the", "a", "an", "to", "is", "it", "of", "in", "for"}
    words = set(text.lower().split())
    return words - _STOP


def _find_duplicate(new_rule: DigestRule, store: DigestStore) -> DigestRule | None:
    """Find a semantically similar existing rule (normalized word overlap for Phase 1)."""
    new_words = _normalize_for_match(new_rule.rule)
    if not new_words:
        return None
    # Also check evidence for stronger matching
    new_evidence_words = _normalize_for_match(new_rule.evidence) if new_rule.evidence else set()

    for existing in store.strategy_rules:
        existing_words = _normalize_for_match(existing.rule)
        if not existing_words:
            continue
        # Match against rule text
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap > 0.5:
            return existing
        # Also match new evidence against existing rule (user may phrase differently)
        if new_evidence_words:
            ev_overlap = len(new_evidence_words & existing_words) / max(len(new_evidence_words), len(existing_words))
            if ev_overlap > 0.5:
                return existing
    return None


def admit_rule(rule: DigestRule, store: DigestStore) -> str:
    """A-MAC admission gate. Returns action taken: 'added', 'upvoted', 'rejected'.

    Quality gate BEFORE any rule enters store (arXiv:2505.16067).
    """
    # Check for duplicate/similar existing rule
    existing = _find_duplicate(rule, store)
    if existing:
        # ExpeL UPVOTE: reinforce existing rule
        existing.occurrence_count += 1
        existing.importance += 1
        existing.last_reinforced = rule.last_reinforced or datetime.now(timezone.utc).isoformat()
        # Promote if threshold reached
        if existing.status == "pending" and existing.occurrence_count >= PROMOTION_COUNT:
            existing.status = "active"
        return "upvoted"

    # Score the new rule
    score = score_rule(rule)
    if score < ADMISSION_THRESHOLD:
        return "rejected"

    # Assign ID and add
    rule.id = store.next_id()
    store.strategy_rules.append(rule)

    # Cap enforcement
    active = store.active_rules()
    if len(active) > MAX_ACTIVE_RULES:
        # Demote lowest-scored active rule
        scored = [(score_rule(r), r) for r in active]
        scored.sort(key=lambda x: x[0])
        scored[0][1].status = "pending"

    return "added"


# ---------------------------------------------------------------------------
# Persistence — JSON on disk
# ---------------------------------------------------------------------------


def load_digest_store(project_dir: str = "") -> DigestStore:
    """Load the digest store from disk."""
    if not DIGEST_FILE.exists():
        return DigestStore(project=project_dir)
    try:
        data = json.loads(DIGEST_FILE.read_text(encoding="utf-8"))
        store = DigestStore(
            version=data.get("version", "1"),
            project=data.get("project", project_dir),
            updated=data.get("updated", ""),
            session_id=data.get("session_id", ""),
        )
        for rd in data.get("strategy_rules", []):
            store.strategy_rules.append(DigestRule(**rd))
        return store
    except (json.JSONDecodeError, TypeError, KeyError):
        return DigestStore(project=project_dir)


def save_digest_store(store: DigestStore) -> None:
    """Save the digest store to disk (JSON + human-readable markdown mirror)."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    store.updated = datetime.now(timezone.utc).isoformat()

    data = {
        "version": store.version,
        "project": store.project,
        "updated": store.updated,
        "session_id": store.session_id,
        "strategy_rules": [asdict(r) for r in store.strategy_rules],
        "failure_patterns": [],  # Reserved for future use
    }
    DIGEST_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Write human-readable markdown mirror
    _write_digest_md(store)


def _write_digest_md(store: DigestStore) -> None:
    """Write a human-readable markdown version of the digest."""
    lines = [
        "# Behavioral Digest",
        f"Updated: {store.updated}",
        f"Project: {store.project}",
        "",
    ]

    active = [r for r in store.strategy_rules if r.status == "active"]
    pending = [r for r in store.strategy_rules if r.status == "pending"]

    if active:
        lines.append(f"## Active Rules ({len(active)})")
        lines.append("")
        for r in active:
            lines.append(f"- **[{r.id}|{r.scope}|{r.priority}]** {r.rule}")
            if r.trigger:
                lines.append(f"  - When: {r.trigger}")
            if r.evidence:
                lines.append(f"  - Evidence: \"{r.evidence[:100]}\"")
            lines.append(f"  - Score: {score_rule(r):.2f} | Seen: {r.occurrence_count}x")
        lines.append("")

    if pending:
        lines.append(f"## Pending Rules ({len(pending)})")
        lines.append("")
        for r in pending:
            lines.append(f"- **[{r.id}|{r.scope}]** {r.rule} (seen {r.occurrence_count}x)")
        lines.append("")

    DIGEST_MD_FILE.write_text("\n".join(lines), encoding="utf-8")


def clear_digest_store() -> None:
    """Remove all digest files."""
    for f in (DIGEST_FILE, DIGEST_MD_FILE):
        if f.exists():
            f.unlink()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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
    store.session_id = session_id

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

    if added > 0 or upvoted > 0:
        save_digest_store(store)

    return added, upvoted, rejected


# ---------------------------------------------------------------------------
# Injection — Phase 2: inject rules at session tail
# ---------------------------------------------------------------------------


def _format_rule_4field(rule: DigestRule) -> str:
    """Format a rule in 4-field compressed format (arXiv:2603.13017 — 11x compression)."""
    line = f"[{rule.id}|{rule.scope}|{rule.priority}] {rule.rule}"
    if rule.trigger:
        line += f"\n  When: {rule.trigger}"
    if rule.signal:
        line += f"\n  Signal: {rule.signal}"
    if rule.evidence:
        line += f"\n  Evidence: \"{rule.evidence[:120]}\""
    return line


def build_injection_text(store: DigestStore) -> str | None:
    """Build the injection text block from active rules.

    Returns None if no active rules exist.
    4-field structured format for injection (full 8-field stored on disk).
    Prefix: "Focus solely on these behavioral rules when applicable" (arXiv:2505.02709).
    Hard cap: 20 active rules (IFScale).
    """
    active = store.active_rules()
    if not active:
        return None

    # Hard rules first, then soft, capped at MAX_ACTIVE_RULES
    hard = [r for r in active if r.priority == "hard"]
    soft = [r for r in active if r.priority == "soft"]
    rules = (hard + soft)[:MAX_ACTIVE_RULES]

    lines = [
        "BEHAVIORAL CONTRACT — Focus solely on these rules when applicable.",
        "",
    ]

    if hard:
        lines.append("PROHIBITIONS:")
        for r in hard:
            lines.append(_format_rule_4field(r))
        lines.append("")

    soft_rules = [r for r in rules if r.priority == "soft"]
    if soft_rules:
        lines.append("PREFERENCES:")
        for r in soft_rules:
            lines.append(_format_rule_4field(r))
        lines.append("")

    return "\n".join(lines)


def _get_memdir(cwd: str = "") -> Path | None:
    """Find the Claude Code memory directory for the given project.

    Honours `CLAUDE_CONFIG_DIR` env var (set by the `claudes` profile launcher)
    before falling back to `~/.claude`. This prevents cross-profile leaks
    where work-session digests land in the personal memdir.
    """
    import os
    if not cwd:
        cwd = os.getcwd()
    # CC stores memories at $CLAUDE_CONFIG_DIR/projects/<slug>/memory (or
    # ~/.claude/projects/<slug>/memory if the env var is not set).
    config_dir_env = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir_env:
        config_dir = Path(config_dir_env).expanduser()
    else:
        config_dir = Path.home() / ".claude"
    claude_dir = config_dir / "projects"
    if not claude_dir.exists():
        return None
    # Sanitize cwd the same way CC does: replace / with -
    slug = cwd.lstrip("/").replace("/", "-")
    project_dir = claude_dir / f"-{slug}"
    if not project_dir.exists():
        # Try finding by prefix match (CC may use different sanitization)
        for d in claude_dir.iterdir():
            if d.is_dir() and slug in d.name:
                project_dir = d
                break
        else:
            return None
    mem_dir = project_dir / "memory"
    return mem_dir if mem_dir.exists() else None


def sync_to_memdir(store: DigestStore, cwd: str = "") -> int:
    """Write active rules as Claude Code feedback memories.

    Claude reads these natively via getMemoryFiles() → injected as <system-reminder>.
    They survive compaction (files on disk, re-read after every compact).
    No custom JSONL injection needed.

    Returns number of rules synced.
    """
    mem_dir = _get_memdir(cwd)
    if mem_dir is None:
        return 0

    active = store.active_rules()
    if not active:
        # Remove existing digest memory if no active rules
        digest_mem = mem_dir / "cozempic_digest.md"
        if digest_mem.exists():
            digest_mem.unlink()
        return 0

    # Build the memory file content
    text = build_injection_text(store)
    if not text:
        return 0

    content = f"""---
name: Cozempic Behavioral Digest
description: Behavioral rules extracted from user corrections — follow these when applicable
type: feedback
---

{text}
"""

    digest_mem = mem_dir / "cozempic_digest.md"
    digest_mem.write_text(content, encoding="utf-8")

    # Update MEMORY.md index if needed
    _update_memory_index(mem_dir)

    # Update last_injection timestamp
    now = datetime.now(timezone.utc).isoformat()
    for r in active:
        r.last_injection = now

    return len(active)


def _update_memory_index(mem_dir: Path) -> None:
    """Ensure cozempic_digest.md is referenced in MEMORY.md index."""
    index_path = mem_dir / "MEMORY.md"
    marker = "[Cozempic Behavioral Digest](cozempic_digest.md)"
    entry = f"- {marker} — behavioral rules from user corrections"

    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        if "cozempic_digest.md" in content:
            return  # Already referenced
        content = content.rstrip() + f"\n{entry}\n"
        index_path.write_text(content, encoding="utf-8")
    else:
        # MEMORY.md doesn't exist yet — don't create it, Claude Code manages this file
        pass


# ---------------------------------------------------------------------------
# Flush / Recover — extraction + memdir sync
# ---------------------------------------------------------------------------


def flush_digest(
    messages: list[Message],
    project_dir: str = "",
    session_id: str = "",
) -> tuple[int, int, int]:
    """Extract corrections from full session, save to disk, sync to memdir.

    Called by PreCompact and Stop hooks to capture corrections before loss.
    Returns (added, upvoted, rejected).
    """
    added, upvoted, rejected = update_digest(
        messages, since_turn=0, project_dir=project_dir, session_id=session_id,
    )
    # Sync active rules to Claude Code's memory system
    store = load_digest_store(project_dir)
    synced = sync_to_memdir(store, cwd=project_dir)
    if synced > 0:
        save_digest_store(store)  # Update last_injection timestamps
    return added, upvoted, rejected


def recover_digest(
    project_dir: str = "",
) -> int:
    """Re-sync digest to memdir after compaction.

    Called by PostCompact hook. Memdir files survive compaction natively,
    but re-sync ensures any newly promoted rules are included.
    Returns number of rules synced.
    """
    store = load_digest_store(project_dir)
    if store.is_empty():
        return 0
    synced = sync_to_memdir(store, cwd=project_dir)
    if synced > 0:
        save_digest_store(store)
    return synced


def show_digest() -> str:
    """Return a formatted string of the current digest."""
    store = load_digest_store()
    if store.is_empty():
        return "No behavioral rules stored."

    lines = []
    active = store.active_rules()
    pending = [r for r in store.strategy_rules if r.status == "pending"]

    if active:
        lines.append(f"Active rules ({len(active)}):")
        for r in active:
            lines.append(f"  [{r.id}|{r.scope}|{r.priority}] {r.rule}")
            lines.append(f"    Score: {score_rule(r):.2f} | Seen: {r.occurrence_count}x")

    if pending:
        lines.append(f"\nPending rules ({len(pending)}):")
        for r in pending:
            lines.append(f"  [{r.id}|{r.scope}] {r.rule} (seen {r.occurrence_count}x)")

    return "\n".join(lines)
