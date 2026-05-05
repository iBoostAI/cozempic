"""Tests for behavioral digest — Phase 1: extraction, scoring, persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic.digest import (
    ADMISSION_THRESHOLD,
    DIGEST_DIR,
    DIGEST_FILE,
    MAX_ACTIVE_RULES,
    PROMOTION_COUNT,
    PROTECTION_TAG,
    DigestRule,
    DigestStore,
    _find_duplicate,
    _get_memdir,
    _to_prohibition,
    admit_rule,
    build_injection_text,
    classify_turn,
    clear_digest_store,
    extract_corrections,
    load_digest_store,
    save_digest_store,
    score_rule,
    show_digest,
    sync_to_memdir,
    update_digest,
)
from cozempic.helpers import is_protected, msg_bytes

import cozempic.strategies  # noqa: F401


def _import_system_noise():
    """Import `_is_system_noise` lazily so the test file still loads when
    the helper does not yet exist (Phase 2b RED phase). Tests that require
    it call this and fail with a clear message instead of crashing the
    entire module."""
    from cozempic import digest as _d
    if not hasattr(_d, "_is_system_noise"):
        raise AssertionError(
            "digest._is_system_noise is not defined — noise filter not implemented yet"
        )
    return _d._is_system_noise


def make_message(line_idx: int, msg: dict) -> tuple[int, dict, int]:
    return (line_idx, msg, msg_bytes(msg))


def make_user(line_idx: int, text: str) -> tuple[int, dict, int]:
    return make_message(line_idx, {
        "type": "user",
        "message": {"role": "user", "content": text},
    })


def make_assistant(line_idx: int, text: str) -> tuple[int, dict, int]:
    return make_message(line_idx, {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


# ---------------------------------------------------------------------------
# classify_turn
# ---------------------------------------------------------------------------

class TestClassifyTurn(unittest.TestCase):

    def test_explicit_no(self):
        self.assertEqual(classify_turn("No, don't do that"), "EXPLICIT_CORRECTION")

    def test_explicit_dont(self):
        self.assertEqual(classify_turn("don't add Co-Authored-By"), "EXPLICIT_CORRECTION")

    def test_explicit_do_not(self):
        self.assertEqual(classify_turn("do not use Write on existing files"), "EXPLICIT_CORRECTION")

    def test_explicit_stop(self):
        self.assertEqual(classify_turn("stop adding comments to every function"), "EXPLICIT_CORRECTION")

    def test_explicit_never(self):
        self.assertEqual(classify_turn("never push to main without asking"), "EXPLICIT_CORRECTION")

    def test_explicit_please_dont(self):
        self.assertEqual(classify_turn("please don't summarize after each change"), "EXPLICIT_CORRECTION")

    def test_implicit_actually(self):
        self.assertEqual(classify_turn("actually, use the other approach"), "IMPLICIT_CORRECTION")

    def test_implicit_instead(self):
        self.assertEqual(classify_turn("instead, use Edit not Write"), "IMPLICIT_CORRECTION")

    def test_implicit_thats_not(self):
        self.assertEqual(classify_turn("that's not what I meant"), "IMPLICIT_CORRECTION")

    def test_preference_always(self):
        self.assertEqual(classify_turn("always use snake_case for variables"), "PREFERENCE")

    def test_preference_from_now_on(self):
        self.assertEqual(classify_turn("from now on, run tests after each change"), "PREFERENCE")

    def test_preference_remember(self):
        self.assertEqual(classify_turn("remember to check for null values"), "PREFERENCE")

    def test_apology_follow_up(self):
        result = classify_turn("use the correct import path", "sorry about that mistake")
        self.assertEqual(result, "APOLOGY_FOLLOW_UP")

    def test_none_normal(self):
        self.assertEqual(classify_turn("can you read that file?"), "NONE")

    def test_none_short(self):
        self.assertEqual(classify_turn("ok"), "NONE")

    def test_none_empty(self):
        self.assertEqual(classify_turn(""), "NONE")


# ---------------------------------------------------------------------------
# _to_prohibition
# ---------------------------------------------------------------------------

class TestToProhibition(unittest.TestCase):

    def test_already_prohibition(self):
        self.assertEqual(_to_prohibition("Don't add X"), "Don't add X")

    def test_do_not(self):
        self.assertEqual(_to_prohibition("do not mock the database"), "Do not mock the database")

    def test_stop_doing(self):
        result = _to_prohibition("stop adding comments")
        self.assertEqual(result, "Do not comments")

    def test_never(self):
        result = _to_prohibition("never push to main")
        self.assertEqual(result, "Do not ever push to main")

    def test_no_prefix(self):
        result = _to_prohibition("No, use Edit instead")
        self.assertEqual(result, "Use Edit instead")


# ---------------------------------------------------------------------------
# extract_corrections
# ---------------------------------------------------------------------------

class TestExtractCorrections(unittest.TestCase):

    def test_extracts_explicit_correction(self):
        messages = [
            make_assistant(0, "I'll add Co-Authored-By"),
            make_user(1, "don't add Co-Authored-By to commits"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].priority, "hard")
        self.assertIn("Co-Authored-By", rules[0].evidence)

    def test_extracts_preference(self):
        messages = [
            make_user(0, "always use snake_case for function names"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source_reliability, 0.9)

    def test_skips_normal_messages(self):
        messages = [
            make_user(0, "can you read the config file?"),
            make_assistant(1, "sure, let me read it"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 0)

    def test_respects_since_turn(self):
        messages = [
            make_user(0, "don't do that"),  # Before window
            make_assistant(1, "ok"),
            make_user(2, "stop using mocks"),  # In window
        ]
        rules = extract_corrections(messages, since_turn=2)
        self.assertEqual(len(rules), 1)
        self.assertIn("mock", rules[0].evidence.lower())

    def test_infers_git_scope(self):
        messages = [make_user(0, "don't push to main branch")]
        rules = extract_corrections(messages)
        self.assertEqual(rules[0].scope, "git")

    def test_infers_file_scope(self):
        messages = [make_user(0, "don't use Write on existing files")]
        rules = extract_corrections(messages)
        self.assertEqual(rules[0].scope, "file-ops")

    def test_rejects_text_over_200_chars(self):
        """Long inputs are rejected (BUG-2 hardening) — extract_corrections skips them.

        Previously this test asserted the rule was capped at 500 chars ("don't " + 1000 x chars),
        but that behavior allowed 500 chars of raw noise to be "Do not "-prefixed and truncated
        mid-tag — the root cause of the R001 = `Do not <local-command-caveat>...` pollution.
        After BUG-2 fix, inputs > 200 chars return "" from _to_prohibition and extract_corrections
        skips the turn.
        """
        long_text = "don't " + "x" * 1000
        messages = [make_user(0, long_text)]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 0)


# ---------------------------------------------------------------------------
# score_rule
# ---------------------------------------------------------------------------

class TestScoreRule(unittest.TestCase):

    def test_new_explicit_correction(self):
        rule = DigestRule(id="R001", rule="test", occurrence_count=1,
                          source_reliability=1.0, type_prior=0.8)
        score = score_rule(rule, days_since_last=0)
        # 0.25*(1/2) + 0.30*1.0 + 0.20*1.0 + 0.25*0.8 = 0.125 + 0.30 + 0.20 + 0.20 = 0.825
        self.assertAlmostEqual(score, 0.825, places=3)

    def test_above_admission(self):
        rule = DigestRule(id="R001", rule="test", occurrence_count=1,
                          source_reliability=1.0, type_prior=0.8)
        self.assertGreater(score_rule(rule), ADMISSION_THRESHOLD)

    def test_low_reliability_rejected(self):
        rule = DigestRule(id="R001", rule="test", occurrence_count=1,
                          source_reliability=0.3, type_prior=0.1)
        score = score_rule(rule, days_since_last=0)
        self.assertLess(score, ADMISSION_THRESHOLD)

    def test_decay_reduces_score(self):
        rule = DigestRule(id="R001", rule="test", occurrence_count=1,
                          source_reliability=1.0, type_prior=0.8)
        fresh = score_rule(rule, days_since_last=0)
        old = score_rule(rule, days_since_last=30)
        self.assertGreater(fresh, old)

    def test_high_occurrence_helps(self):
        low = DigestRule(id="R001", rule="test", occurrence_count=1,
                         source_reliability=0.5, type_prior=0.5)
        high = DigestRule(id="R002", rule="test", occurrence_count=5,
                          source_reliability=0.5, type_prior=0.5)
        self.assertGreater(score_rule(high), score_rule(low))


# ---------------------------------------------------------------------------
# admit_rule
# ---------------------------------------------------------------------------

class TestAdmitRule(unittest.TestCase):

    def test_admits_strong_rule(self):
        store = DigestStore()
        rule = DigestRule(id="", rule="Do not add Co-Authored-By",
                          source_reliability=1.0, type_prior=0.8,
                          first_seen="2026-04-01", last_reinforced="2026-04-01")
        result = admit_rule(rule, store)
        self.assertEqual(result, "added")
        self.assertEqual(len(store.strategy_rules), 1)
        self.assertEqual(store.strategy_rules[0].id, "R001")

    def test_rejects_weak_rule(self):
        store = DigestStore()
        rule = DigestRule(id="", rule="maybe do something",
                          source_reliability=0.3, type_prior=0.1)
        result = admit_rule(rule, store)
        self.assertEqual(result, "rejected")
        self.assertEqual(len(store.strategy_rules), 0)

    def test_upvotes_duplicate(self):
        store = DigestStore()
        rule1 = DigestRule(id="R001", rule="Do not add Co-Authored-By",
                           source_reliability=1.0, type_prior=0.8,
                           occurrence_count=1, status="pending")
        store.strategy_rules.append(rule1)

        rule2 = DigestRule(id="", rule="Do not add Co-Authored-By to commits",
                           evidence="don't add Co-Authored-By",
                           source_reliability=1.0, type_prior=0.8)
        result = admit_rule(rule2, store)
        self.assertEqual(result, "upvoted")
        self.assertEqual(store.strategy_rules[0].occurrence_count, 2)

    def test_promotes_after_threshold(self):
        """Pending rule gets promoted to active after PROMOTION_COUNT upvotes."""
        store = DigestStore()
        # Start as pending (implicit correction, not auto-promoted)
        rule = DigestRule(id="R001", rule="Use snake_case for variables",
                          source_reliability=0.6, type_prior=0.6,
                          occurrence_count=PROMOTION_COUNT - 1, status="pending")
        store.strategy_rules.append(rule)

        dup = DigestRule(id="", rule="Use snake_case for variable names",
                         evidence="use snake_case for variables",
                         source_reliability=0.6, type_prior=0.6)
        admit_rule(dup, store)
        self.assertEqual(store.strategy_rules[0].status, "active")

    def test_caps_active_rules(self):
        store = DigestStore()
        # Fill with MAX_ACTIVE_RULES active rules
        for i in range(MAX_ACTIVE_RULES):
            store.strategy_rules.append(DigestRule(
                id=f"R{i:03d}", rule=f"Rule number {i}",
                source_reliability=0.8, type_prior=0.8,
                occurrence_count=5, status="active",
            ))
        # Add one more
        new_rule = DigestRule(id="", rule="A brand new unique rule about something special",
                              source_reliability=1.0, type_prior=0.9)
        admit_rule(new_rule, store)
        active = store.active_rules()
        self.assertLessEqual(len(active), MAX_ACTIVE_RULES)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_dir = DIGEST_DIR
        self._orig_file = DIGEST_FILE

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        store = DigestStore(project="/test", session_id="sess-1")
        store.strategy_rules.append(DigestRule(
            id="R001", rule="Do not add Co-Authored-By",
            source_reliability=1.0, type_prior=0.8,
            occurrence_count=3, status="active",
            first_seen="2026-04-01", last_reinforced="2026-04-01",
        ))

        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"

        with patch("cozempic.digest.DIGEST_DIR", self.tmpdir), \
             patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md):
            save_digest_store(store)
            self.assertTrue(digest_file.exists())
            self.assertTrue(digest_md.exists())

            loaded = load_digest_store("/test")
            self.assertEqual(len(loaded.strategy_rules), 1)
            self.assertEqual(loaded.strategy_rules[0].id, "R001")
            self.assertEqual(loaded.strategy_rules[0].rule, "Do not add Co-Authored-By")
            self.assertEqual(loaded.strategy_rules[0].status, "active")

    def test_load_missing_file(self):
        with patch("cozempic.digest.DIGEST_FILE", self.tmpdir / "nonexistent.json"):
            store = load_digest_store("/test")
            self.assertTrue(store.is_empty())

    def test_clear(self):
        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"
        digest_file.write_text("{}")
        digest_md.write_text("# test")

        with patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md):
            clear_digest_store()
            self.assertFalse(digest_file.exists())
            self.assertFalse(digest_md.exists())


# ---------------------------------------------------------------------------
# update_digest (integration)
# ---------------------------------------------------------------------------

class TestUpdateDigest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_end_to_end(self):
        messages = [
            make_assistant(0, "I'll add the Co-Authored-By line"),
            make_user(1, "don't add Co-Authored-By to commits"),
            make_assistant(2, "ok, I won't"),
            make_user(3, "always use Edit for existing files"),
        ]

        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"

        with patch("cozempic.digest.DIGEST_DIR", self.tmpdir), \
             patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md):
            added, upvoted, rejected = update_digest(messages, project_dir="/test")
            self.assertGreater(added, 0)

            # Verify persisted
            data = json.loads(digest_file.read_text())
            self.assertGreater(len(data["strategy_rules"]), 0)


# ---------------------------------------------------------------------------
# Protection tag
# ---------------------------------------------------------------------------

class TestProtectionTag(unittest.TestCase):

    def test_digest_tagged_message_is_protected(self):
        msg = {"type": "user", PROTECTION_TAG: True, "message": {"role": "user", "content": "rules"}}
        self.assertTrue(is_protected(msg))

    def test_normal_message_not_protected(self):
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        self.assertFalse(is_protected(msg))


# ---------------------------------------------------------------------------
# DigestStore
# ---------------------------------------------------------------------------

class TestDigestStore(unittest.TestCase):

    def test_is_empty(self):
        self.assertTrue(DigestStore().is_empty())

    def test_not_empty(self):
        store = DigestStore()
        store.strategy_rules.append(DigestRule(id="R001", rule="test"))
        self.assertFalse(store.is_empty())

    def test_next_id_sequential(self):
        store = DigestStore()
        self.assertEqual(store.next_id(), "R001")
        store.strategy_rules.append(DigestRule(id="R001", rule="test"))
        self.assertEqual(store.next_id(), "R002")

    def test_active_rules(self):
        store = DigestStore()
        store.strategy_rules.append(DigestRule(id="R001", rule="active", status="active"))
        store.strategy_rules.append(DigestRule(id="R002", rule="pending", status="pending"))
        self.assertEqual(len(store.active_rules()), 1)


# ===========================================================================
# RED TESTS — Phase 2b — Bugs inventoried in AUDIT_REPORT.md (2026-05-05)
# ===========================================================================
#
# These tests encode buggy current behavior as EXPECTED behavior after the
# fix. They are expected to FAIL against the current digest.py (RED) and
# pass only once Phase 2c implements the corresponding fixes.
#
# Source of truth for bug inventory:
#   .claude/worktrees/fix-digest-noise-filter/AUDIT_REPORT.md
#
# Mapping:
#   BUG-1/3 → TestSystemNoiseFilter  (CRITICAL)
#   BUG-2   → TestToProhibitionHardened (CRITICAL)
#   BUG-4   → TestAdmissionNoAutoActivate (HIGH)
#   BUG-5   → TestLoadRetroactiveSweep (HIGH)
#   BUG-6   → TestCapEnforcement (HIGH)
#   BUG-7   → TestDuplicateMergeStricter (HIGH)
#   BUG-8   → TestMemdirHonorsConfigDir (CRITICAL)
# ===========================================================================


# ---------------------------------------------------------------------------
# BUG-1/3 — noise-filter gate
# ---------------------------------------------------------------------------

class TestSystemNoiseFilter(unittest.TestCase):
    """digest must recognise Claude-Code synthetic noise and skip it.

    Current behavior (RED target): every synthetic user turn containing
    "don't", "never", "no, " etc. becomes a hard active rule.
    Expected behavior (post-fix): `_is_system_noise(text)` returns True
    for synthetic wrappers, and `extract_corrections` skips those turns.
    """

    # ---- _is_system_noise unit tests ----

    def test_local_command_caveat_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("<local-command-caveat>/compact</local-command-caveat>"))

    def test_teammate_message_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise(
            "<teammate-message teammate_id=\"lead\" summary=\"x\">do something</teammate-message>"
        ))

    def test_command_name_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("<command-name>/init</command-name>"))

    def test_command_message_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("<command-message>please analyze</command-message>"))

    def test_system_reminder_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise(
            "<system-reminder>don't forget to commit</system-reminder>"
        ))

    def test_function_calls_block_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise(
            "<function_calls><invoke name=\"Read\"></invoke></function_calls>"
        ))

    def test_bash_stdout_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("<bash-stdout>\nls: /tmp\n</bash-stdout>"))

    def test_bash_stderr_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("<bash-stderr>No such file</bash-stderr>"))

    def test_leading_tag_is_noise(self):
        is_noise = _import_system_noise()
        # A user turn whose content STARTS with an angle-bracket tag is
        # synthetic — never a real correction.
        self.assertTrue(is_noise("<user-prompt-submit-hook>anything</user-prompt-submit-hook>"))

    def test_init_slash_prompt_is_noise(self):
        is_noise = _import_system_noise()
        # /init injects: "Please analyze this codebase and create..."
        self.assertTrue(is_noise(
            "Please analyze this codebase and create a CLAUDE.md describing it."
        ))

    def test_slash_command_line_is_noise(self):
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("/clear"))

    # ---- real corrections must pass ----

    def test_genuine_dont_correction_is_not_noise(self):
        is_noise = _import_system_noise()
        self.assertFalse(is_noise("don't add Co-Authored-By to commits"))

    def test_genuine_never_correction_is_not_noise(self):
        is_noise = _import_system_noise()
        self.assertFalse(is_noise("never push to main without asking"))

    def test_genuine_preference_is_not_noise(self):
        is_noise = _import_system_noise()
        self.assertFalse(is_noise("always use snake_case for variables"))

    def test_genuine_short_correction_is_not_noise(self):
        is_noise = _import_system_noise()
        self.assertFalse(is_noise("stop adding summaries"))

    # ---- extract_corrections must skip noisy turns ----

    def test_extract_skips_local_command_caveat(self):
        messages = [
            make_user(0, "<local-command-caveat>The user ran /compact</local-command-caveat>"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 0,
                         "extract_corrections must skip synthetic <local-command-caveat> turns")

    def test_extract_skips_system_reminder(self):
        messages = [
            make_user(0,
                "<system-reminder>Please don't forget to run tests</system-reminder>"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 0,
                         "extract_corrections must skip <system-reminder> synthetic turns")

    def test_extract_skips_init_prompt(self):
        messages = [
            make_user(0,
                "Please analyze this codebase and create a CLAUDE.md describing it. "
                "Do not include secrets."),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 0,
                         "extract_corrections must skip /init synthetic turns")

    def test_extract_keeps_genuine_correction_after_noisy_turn(self):
        messages = [
            make_user(0, "<local-command-caveat>The user ran /init</local-command-caveat>"),
            make_assistant(1, "ok"),
            make_user(2, "don't add Co-Authored-By to commits"),
        ]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 1,
                         "genuine correction after noise must still be captured")
        self.assertIn("Co-Authored-By", rules[0].evidence)


# ---------------------------------------------------------------------------
# BUG-2 — _to_prohibition hardening
# ---------------------------------------------------------------------------

class TestToProhibitionHardened(unittest.TestCase):
    """`_to_prohibition` must refuse to wrap obviously-structural text.

    Current behavior (RED target): it prefixes "Do not " onto ANY input
    up to 500 chars — producing `Do not <local-command-caveat>...`.
    Expected behavior: return `""` (sentinel for "skip this turn") when
    the input is too long, multi-paragraph, starts with markdown, a code
    fence, or a tag.
    """

    def test_rejects_text_over_200_chars(self):
        long_text = "x" * 250
        self.assertEqual(_to_prohibition(long_text), "",
                         "must reject input > 200 chars")

    def test_rejects_text_with_three_newlines(self):
        text = "line1\nline2\nline3\nline4"
        self.assertEqual(_to_prohibition(text), "",
                         "must reject input with > 2 newlines")

    def test_rejects_markdown_heading(self):
        self.assertEqual(_to_prohibition("# Do not add X"), "",
                         "must reject text starting with markdown heading")

    def test_rejects_markdown_bullet_dash(self):
        self.assertEqual(_to_prohibition("- do not do X"), "",
                         "must reject text starting with markdown bullet (-)")

    def test_rejects_markdown_bullet_star(self):
        self.assertEqual(_to_prohibition("* do not do X"), "",
                         "must reject text starting with markdown bullet (*)")

    def test_rejects_triple_backtick_fence(self):
        self.assertEqual(_to_prohibition("```\ncode\n```"), "",
                         "must reject text starting with code fence")

    def test_rejects_leading_angle_tag(self):
        self.assertEqual(_to_prohibition("<local-command-caveat>foo</local-command-caveat>"), "",
                         "must reject text starting with '<' (tag)")

    def test_accepts_normal_correction(self):
        # Sanity: a clean "don't" still passes through and becomes prohibition.
        result = _to_prohibition("don't add Co-Authored-By")
        self.assertTrue(result.startswith("Don't") or result.startswith("Do not"),
                        f"clean input must still work, got {result!r}")

    def test_accepts_normal_never(self):
        result = _to_prohibition("never push to main")
        self.assertTrue(result.lower().startswith("do not"),
                        f"'never' rewriting must still work, got {result!r}")

    def test_rejects_text_with_newline_and_tag(self):
        # Combined failure mode from the observed R001 pollution.
        text = "<local-command-caveat>foo\nbar</local-command-caveat>"
        self.assertEqual(_to_prohibition(text), "",
                         "combined tag + newlines must be rejected")


# ---------------------------------------------------------------------------
# BUG-4 — EXPLICIT_CORRECTION must not auto-activate
# ---------------------------------------------------------------------------

class TestAdmissionNoAutoActivate(unittest.TestCase):
    """New extracted rules must start `pending` regardless of type.

    Current behavior (RED target): EXPLICIT_CORRECTION rules are created
    with `status="active"` on first sight (digest.py:339).
    Expected behavior: all new rules start `pending`; promotion to
    `active` happens only via `admit_rule` upvote path once
    `occurrence_count >= PROMOTION_COUNT`.
    """

    def test_explicit_correction_starts_pending(self):
        messages = [make_user(0, "don't add Co-Authored-By to commits")]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].status, "pending",
                         "EXPLICIT_CORRECTION must start pending (no auto-activate)")

    def test_preference_starts_pending(self):
        messages = [make_user(0, "always use snake_case for variables")]
        rules = extract_corrections(messages)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].status, "pending")

    def test_explicit_promoted_after_two_occurrences(self):
        """Seeing the same explicit correction twice promotes it to active."""
        store = DigestStore()
        # First admission
        msgs1 = [make_user(0, "don't add Co-Authored-By")]
        for r in extract_corrections(msgs1):
            admit_rule(r, store)
        self.assertEqual(len(store.strategy_rules), 1)
        self.assertEqual(store.strategy_rules[0].status, "pending",
                         "first occurrence must remain pending")

        # Second admission — dedup should upvote and promote
        msgs2 = [make_user(1, "don't add Co-Authored-By to commits")]
        for r in extract_corrections(msgs2):
            admit_rule(r, store)
        self.assertEqual(store.strategy_rules[0].status, "active",
                         "after PROMOTION_COUNT occurrences rule becomes active")
        self.assertGreaterEqual(store.strategy_rules[0].occurrence_count, PROMOTION_COUNT)

    def test_single_explicit_not_in_active_rules(self):
        store = DigestStore()
        msgs = [make_user(0, "stop adding summaries")]
        for r in extract_corrections(msgs):
            admit_rule(r, store)
        self.assertEqual(len(store.active_rules()), 0,
                         "single explicit occurrence must not appear in active_rules()")

    def test_end_to_end_update_digest_keeps_single_occurrence_pending(self):
        """`update_digest` on a single explicit correction must NOT produce an
        active rule — the repetition gate must be honoured."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = tmp_path / "behavioral-digest.json"
            digest_md = tmp_path / "behavioral-digest.md"
            messages = [make_user(0, "don't add Co-Authored-By to commits")]
            with patch("cozempic.digest.DIGEST_DIR", tmp_path), \
                 patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md):
                update_digest(messages, project_dir="/test")
                store = load_digest_store("/test")
                active = store.active_rules()
                self.assertEqual(len(active), 0,
                                 "single-occurrence explicit correction must not auto-activate")


# ---------------------------------------------------------------------------
# BUG-5 — retroactive cap sweep on load
# ---------------------------------------------------------------------------

class TestLoadRetroactiveSweep(unittest.TestCase):
    """load_digest_store must cap active rules at MAX_ACTIVE_RULES.

    Current behavior (RED target): if the JSON contains 447 active rules,
    `load_digest_store` returns all 447 as active. The cap fires only on
    subsequent *admits*, one demotion per admit — so a polluted store is
    never retroactively trimmed.
    Expected behavior: on load, scan the deserialised rules and demote
    the lowest-scored active ones until `<= MAX_ACTIVE_RULES` remain
    active.
    """

    def _write_polluted_store(self, tmp_path: Path, n_active: int) -> Path:
        digest_file = tmp_path / "behavioral-digest.json"
        rules = []
        for i in range(n_active):
            # Vary occurrence_count so score_rule has something to sort on.
            rules.append({
                "id": f"R{i + 1:03d}",
                "rule": f"Do not do thing number {i}",
                "priority": "hard",
                "scope": "general",
                "trigger": "",
                "decision_step": "",
                "before": "",
                "after": "",
                "signal": "EXPLICIT_CORRECTION",
                "evidence": f"evidence {i}",
                "importance": 1,
                "source_reliability": 1.0,
                "type_prior": 0.8,
                "status": "active",
                "occurrence_count": 1 + (i % 5),  # 1..5
                "first_seen": "2026-05-01T00:00:00+00:00",
                "last_reinforced": "2026-05-01T00:00:00+00:00",
                "last_injection": None,
            })
        data = {
            "version": "1",
            "project": "/test",
            "updated": "2026-05-05T00:00:00+00:00",
            "session_id": "sess-x",
            "strategy_rules": rules,
            "failure_patterns": [],
        }
        digest_file.write_text(json.dumps(data), encoding="utf-8")
        return digest_file

    def test_load_caps_fifty_active_to_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = self._write_polluted_store(tmp_path, n_active=50)
            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                self.assertLessEqual(
                    len(store.active_rules()), MAX_ACTIVE_RULES,
                    "load_digest_store must enforce active cap retroactively")
                # Total rules preserved — only status flips to pending.
                self.assertEqual(len(store.strategy_rules), 50)

    def test_load_caps_four_hundred_forty_seven_active(self):
        """Reproduce the actual observed pollution count (447 active)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = self._write_polluted_store(tmp_path, n_active=447)
            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                self.assertLessEqual(
                    len(store.active_rules()), MAX_ACTIVE_RULES,
                    "cap must fire even on extreme pollution")
                self.assertEqual(len(store.strategy_rules), 447,
                                 "rules are demoted to pending, not deleted")

    def test_load_at_cap_no_demotion(self):
        """A store at EXACTLY MAX_ACTIVE_RULES must be left alone."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = self._write_polluted_store(tmp_path, n_active=MAX_ACTIVE_RULES)
            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                self.assertEqual(
                    len(store.active_rules()), MAX_ACTIVE_RULES,
                    "at-cap store must not be over-demoted")


# ---------------------------------------------------------------------------
# BUG-6 — build_injection_text honours the cap for BOTH hard and soft
# ---------------------------------------------------------------------------

class TestCapEnforcement(unittest.TestCase):
    """`build_injection_text` must emit at most MAX_ACTIVE_RULES lines total.

    Current behavior (RED target): the hard loop iterates the full `hard`
    list (digest.py:605) so a store with 30 hard rules emits all 30.
    Expected behavior: total rendered rules <= MAX_ACTIVE_RULES, hard
    rules rendered first within the budget.
    """

    def _make_store_with_rules(self, n_hard: int, n_soft: int) -> DigestStore:
        store = DigestStore()
        idx = 1
        for i in range(n_hard):
            store.strategy_rules.append(DigestRule(
                id=f"R{idx:03d}", rule=f"Do not do hard thing {i}",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=5, status="active",
            ))
            idx += 1
        for i in range(n_soft):
            store.strategy_rules.append(DigestRule(
                id=f"R{idx:03d}", rule=f"Prefer soft thing {i}",
                priority="soft", scope="general",
                source_reliability=0.9, type_prior=0.9,
                occurrence_count=3, status="active",
            ))
            idx += 1
        return store

    def _count_rule_lines(self, text: str) -> int:
        """Count rendered rule entries — each starts with '[Rnnn|'."""
        if not text:
            return 0
        return sum(1 for line in text.splitlines() if line.startswith("[R"))

    def test_thirty_hard_rendered_at_most_twenty(self):
        store = self._make_store_with_rules(n_hard=30, n_soft=0)
        text = build_injection_text(store)
        self.assertIsNotNone(text)
        self.assertLessEqual(
            self._count_rule_lines(text), MAX_ACTIVE_RULES,
            "30 hard rules must be capped to MAX_ACTIVE_RULES in injection text")

    def test_thirty_hard_plus_ten_soft_rendered_at_most_twenty(self):
        store = self._make_store_with_rules(n_hard=30, n_soft=10)
        text = build_injection_text(store)
        self.assertIsNotNone(text)
        self.assertLessEqual(
            self._count_rule_lines(text), MAX_ACTIVE_RULES,
            "hard+soft combined must be capped at MAX_ACTIVE_RULES")

    def test_priority_respected_hard_rendered_first(self):
        """When capped, hard rules must fill the budget before any soft."""
        store = self._make_store_with_rules(n_hard=30, n_soft=10)
        text = build_injection_text(store)
        self.assertIsNotNone(text)
        # PROHIBITIONS section must appear before PREFERENCES section
        prohib_idx = text.find("PROHIBITIONS:")
        pref_idx = text.find("PREFERENCES:")
        if pref_idx != -1:
            self.assertLess(prohib_idx, pref_idx,
                            "PROHIBITIONS section must come before PREFERENCES")
        # No soft line should be rendered when hard alone already fills the cap
        # (30 hard > MAX_ACTIVE_RULES=20 so all budget goes to hard)
        rendered_soft = sum(1 for line in text.splitlines()
                            if line.startswith("[R") and "|soft]" in line)
        self.assertEqual(rendered_soft, 0,
                         "when hard rules alone exceed cap, no soft rules should be rendered")

    def test_five_hard_plus_five_soft_renders_all(self):
        """Below cap — all rules should render."""
        store = self._make_store_with_rules(n_hard=5, n_soft=5)
        text = build_injection_text(store)
        self.assertIsNotNone(text)
        self.assertEqual(self._count_rule_lines(text), 10,
                         "below-cap store must render all rules")

    def test_build_returns_none_for_empty_store(self):
        store = DigestStore()
        text = build_injection_text(store)
        self.assertIsNone(text)

    def test_admit_rule_caps_fifty_new_additions(self):
        """Feeding 50 fresh hard rules through admit_rule must leave at most
        MAX_ACTIVE_RULES active (cap enforcement happens during admission)."""
        store = DigestStore()
        for i in range(50):
            admit_rule(DigestRule(
                id="", rule=f"Do not take action number {i} under any circumstance",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
            ), store)
        self.assertLessEqual(
            len(store.active_rules()), MAX_ACTIVE_RULES,
            "admit_rule cap enforcement must hold across 50 admissions")

    def test_admit_rule_caps_two_hundred_new_additions(self):
        """Stress test — admit_rule must hold the cap under a 200-rule burst."""
        store = DigestStore()
        for i in range(200):
            admit_rule(DigestRule(
                id="", rule=f"Do not touch resource number {i} for any reason",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
            ), store)
        self.assertLessEqual(
            len(store.active_rules()), MAX_ACTIVE_RULES,
            "cap must hold under 200-rule admission burst")


# ---------------------------------------------------------------------------
# BUG-7 — duplicate merge must be stricter
# ---------------------------------------------------------------------------

class TestDuplicateMergeStricter(unittest.TestCase):
    """`_find_duplicate` must not merge semantically different rules.

    Current behavior (RED target): 0.5 word-overlap merges opposites.
    Expected behavior: require higher overlap AND matching scope AND
    matching priority before declaring two rules duplicates.
    """

    def test_opposite_instructions_do_not_merge(self):
        """'use edit not write' vs 'use write not edit' — opposite intents."""
        store = DigestStore()
        r1 = DigestRule(id="R001", rule="Use Edit not Write on existing files",
                        priority="soft", scope="file-ops",
                        source_reliability=0.9, type_prior=0.9,
                        occurrence_count=1, status="pending")
        store.strategy_rules.append(r1)

        r2 = DigestRule(id="", rule="Use Write not Edit on existing files",
                        evidence="use Write not Edit",
                        priority="soft", scope="file-ops",
                        source_reliability=0.9, type_prior=0.9)
        dup = _find_duplicate(r2, store)
        self.assertIsNone(
            dup, "opposite instructions must not be treated as duplicates")

    def test_different_scope_does_not_merge(self):
        """High word overlap but different scope → not a duplicate."""
        store = DigestStore()
        r1 = DigestRule(id="R001", rule="Do not add Co-Authored-By",
                        priority="hard", scope="git",
                        source_reliability=1.0, type_prior=0.8,
                        status="active")
        store.strategy_rules.append(r1)

        r2 = DigestRule(id="", rule="Do not add Co-Authored-By line",
                        evidence="don't add Co-Authored-By",
                        priority="hard", scope="communication",  # different scope
                        source_reliability=1.0, type_prior=0.8)
        dup = _find_duplicate(r2, store)
        self.assertIsNone(
            dup, "duplicate detection must require matching scope")

    def test_different_priority_does_not_merge(self):
        """Same text, different priority → not the same rule."""
        store = DigestStore()
        r1 = DigestRule(id="R001", rule="Do not add tests without asking",
                        priority="hard", scope="testing",
                        source_reliability=1.0, type_prior=0.8,
                        status="active")
        store.strategy_rules.append(r1)

        r2 = DigestRule(id="", rule="Do not add tests without asking",
                        evidence="prefer no tests",
                        priority="soft", scope="testing",
                        source_reliability=0.9, type_prior=0.9)
        dup = _find_duplicate(r2, store)
        self.assertIsNone(
            dup, "duplicate detection must require matching priority")

    def test_true_duplicates_still_merge(self):
        """Same scope+priority+high overlap → still merges (sanity)."""
        store = DigestStore()
        r1 = DigestRule(id="R001", rule="Do not add Co-Authored-By lines to commits",
                        priority="hard", scope="git",
                        source_reliability=1.0, type_prior=0.8,
                        status="active")
        store.strategy_rules.append(r1)

        r2 = DigestRule(id="", rule="Do not add Co-Authored-By lines to commit messages",
                        evidence="don't add Co-Authored-By to commits",
                        priority="hard", scope="git",
                        source_reliability=1.0, type_prior=0.8)
        dup = _find_duplicate(r2, store)
        self.assertIsNotNone(
            dup, "genuine duplicates (same scope+priority, high overlap) must still merge")


# ---------------------------------------------------------------------------
# BUG-8 — _get_memdir must honour CLAUDE_CONFIG_DIR
# ---------------------------------------------------------------------------

class TestMemdirHonorsConfigDir(unittest.TestCase):
    """`_get_memdir` must read `CLAUDE_CONFIG_DIR` env var before falling
    back to `~/.claude`.

    Current behavior (RED target): hardcodes `Path.home() / ".claude"` →
    under the `claudes` profile (CLAUDE_CONFIG_DIR=~/.claudes) the sync
    pipeline silently writes to the wrong profile or no-ops.
    Expected behavior: honour CLAUDE_CONFIG_DIR when set.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmpdir / "fake_claudes"
        self.projects_dir = self.config_dir / "projects"
        # Use the same slug CC uses: slash-to-dash with leading dash
        self.slug_cwd = "/test/slug"
        self.project_slug = f"-{self.slug_cwd.lstrip('/').replace('/', '-')}"
        self.expected_memdir = self.projects_dir / self.project_slug / "memory"
        self.expected_memdir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_honors_claude_config_dir_env(self):
        """When CLAUDE_CONFIG_DIR is set, `_get_memdir` returns the memdir
        under that directory, not ~/.claude."""
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.config_dir)}):
            memdir = _get_memdir(self.slug_cwd)
            self.assertIsNotNone(memdir,
                                 "memdir must be resolved when CLAUDE_CONFIG_DIR is set")
            self.assertEqual(
                Path(memdir).resolve(), self.expected_memdir.resolve(),
                f"expected memdir under CLAUDE_CONFIG_DIR, got {memdir}")

    def test_falls_back_to_home_when_env_unset(self):
        """Without CLAUDE_CONFIG_DIR, legacy behavior (Path.home()/.claude)
        is preserved — don't regress existing flows."""
        # Build a fake ~/.claude under tmpdir and point HOME there.
        fake_home = self.tmpdir / "fake_home"
        (fake_home / ".claude" / "projects" / self.project_slug / "memory").mkdir(
            parents=True, exist_ok=True)
        env = {"HOME": str(fake_home)}
        # Explicitly clear CLAUDE_CONFIG_DIR if it's set in the outer env
        with patch.dict("os.environ", env, clear=False):
            # Remove CLAUDE_CONFIG_DIR if present in parent env
            import os
            old = os.environ.pop("CLAUDE_CONFIG_DIR", None)
            try:
                memdir = _get_memdir(self.slug_cwd)
                # Under the fallback, memdir should resolve under fake_home/.claude
                self.assertIsNotNone(memdir)
                self.assertIn(".claude", str(memdir),
                              "without CLAUDE_CONFIG_DIR, must use ~/.claude fallback")
            finally:
                if old is not None:
                    os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_returns_none_when_config_dir_has_no_projects(self):
        """CLAUDE_CONFIG_DIR set but no projects subdir → return None,
        not a silent fallback to ~/.claude (that would leak cross-profile)."""
        bare_config = self.tmpdir / "bare_claudes"
        bare_config.mkdir(parents=True, exist_ok=True)
        # NOTE: no projects/ under bare_config
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(bare_config)}):
            memdir = _get_memdir(self.slug_cwd)
            self.assertIsNone(
                memdir,
                "CLAUDE_CONFIG_DIR without projects/ must return None, not silently "
                "fall back to ~/.claude (cross-profile leak)")

    def test_sync_to_memdir_writes_under_config_dir(self):
        """End-to-end: with CLAUDE_CONFIG_DIR set, `sync_to_memdir` writes
        `cozempic_digest.md` under that directory, not under ~/.claude."""
        store = DigestStore(project=self.slug_cwd)
        store.strategy_rules.append(DigestRule(
            id="R001", rule="Do not add Co-Authored-By",
            priority="hard", scope="git",
            source_reliability=1.0, type_prior=0.8,
            occurrence_count=5, status="active",
        ))
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.config_dir)}):
            n = sync_to_memdir(store, cwd=self.slug_cwd)
            self.assertGreater(n, 0, "sync_to_memdir should have written 1 rule")
            digest_md = self.expected_memdir / "cozempic_digest.md"
            self.assertTrue(
                digest_md.exists(),
                f"digest should be written under CLAUDE_CONFIG_DIR at {digest_md}")
            content = digest_md.read_text(encoding="utf-8")
            self.assertIn("Do not add Co-Authored-By", content)

    def test_sync_to_memdir_does_not_write_to_home_when_config_dir_set(self):
        """Guard against the cross-profile leak: under CLAUDE_CONFIG_DIR,
        ~/.claude/projects/<slug>/memory must NOT receive a write."""
        # Build a shadow ~/.claude too so we can prove it's left untouched.
        fake_home = self.tmpdir / "shadow_home"
        shadow_memdir = fake_home / ".claude" / "projects" / self.project_slug / "memory"
        shadow_memdir.mkdir(parents=True, exist_ok=True)

        store = DigestStore(project=self.slug_cwd)
        store.strategy_rules.append(DigestRule(
            id="R001", rule="Do not add Co-Authored-By",
            priority="hard", scope="git",
            source_reliability=1.0, type_prior=0.8,
            occurrence_count=5, status="active",
        ))
        with patch.dict("os.environ", {
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "HOME": str(fake_home),
        }):
            sync_to_memdir(store, cwd=self.slug_cwd)
            leaked = shadow_memdir / "cozempic_digest.md"
            self.assertFalse(
                leaked.exists(),
                "under CLAUDE_CONFIG_DIR, ~/.claude must NOT receive a cross-profile write")


if __name__ == "__main__":
    unittest.main()
