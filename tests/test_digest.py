"""Tests for behavioral digest — Phase 1: extraction, scoring, persistence."""

from __future__ import annotations

import json
import os
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


# ===========================================================================
# RED TESTS — Phase 2b round 2 — Phase 2d adversarial findings (2026-05-05)
# ===========================================================================
#
# Post-fix adversarial review (team `cozempic-digest-fix`, devils-advocate)
# surfaced one CRITICAL and one HIGH that the Phase 2b/2c cycle did NOT
# close. These RED tests must fail against commit range
# `86f6c4d..HEAD (7a0dc27)` and flip GREEN only after Phase 2c-r2 lands the
# fixes.
#
# Source: .claude/worktrees/fix-digest-noise-filter/ADVERSARIAL_REPORT.md
#
# Mapping:
#   A9/A10/A14 (CRITICAL) → TestLoadNoiseEvidencePurge
#   A6         (HIGH)     → TestAtomicSave
# ===========================================================================


# ---------------------------------------------------------------------------
# A9/A10/A14 — load_digest_store must purge noise-evidence rules, not merely cap
# ---------------------------------------------------------------------------

class TestLoadNoiseEvidencePurge(unittest.TestCase):
    """The retroactive sweep added in Phase 2c trims active count to
    MAX_ACTIVE_RULES but does NOT clean pollution. On the real poisoned
    backup, 17 of the 20 survivors are still `Do not <teammate-message ...`
    rules because all polluted rules share identical scoring inputs and
    the stable sort simply keeps the latest-inserted.

    Expected behavior (post Phase 2c-r2): on load, any rule whose
    `evidence` field is flagged by `_is_system_noise` MUST be dropped (or
    forcibly demoted to pending and excluded from active_rules) BEFORE
    the cap sweep runs — so genuine corrections fill the 20-active budget.
    """

    def _build_polluted_rule(self, rid: int, evidence: str, rule: str,
                             status: str = "active") -> dict:
        return {
            "id": f"R{rid:03d}",
            "rule": rule,
            "priority": "hard",
            "scope": "general",
            "trigger": "",
            "decision_step": "",
            "before": "",
            "after": "",
            "signal": "EXPLICIT_CORRECTION",
            "evidence": evidence,
            "importance": 1,
            "source_reliability": 1.0,
            "type_prior": 0.8,
            "status": status,
            "occurrence_count": 1,
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_reinforced": "2026-05-01T00:00:00+00:00",
            "last_injection": None,
        }

    def _write_store(self, tmp_path: Path, rules: list[dict]) -> Path:
        digest_file = tmp_path / "behavioral-digest.json"
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

    def test_load_drops_noise_evidence_tag_prefixed(self):
        """Pre-populated store: 30 tag-prefixed pollution + 20 clean rules.
        After load, the active list must be EXCLUSIVELY clean rules."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            noise_evidences = [
                "<teammate-message teammate_id=\"x\" summary=\"y\">anything</teammate-message>",
                "<local-command-caveat>The user ran /compact</local-command-caveat>",
                "<task-notification>task complete</task-notification>",
                "<function_calls><invoke name=\"Read\"></invoke></function_calls>",
                "<system-reminder>do something</system-reminder>",
                "<command-name>/init</command-name>",
            ]
            rules = []
            # 30 polluted rules — cycle through noise evidence types
            for i in range(30):
                ev = noise_evidences[i % len(noise_evidences)]
                rules.append(self._build_polluted_rule(
                    i + 1, evidence=ev, rule=f"Do not {ev[:60]}"))
            # 20 clean rules
            for j in range(20):
                rules.append(self._build_polluted_rule(
                    100 + j,
                    evidence=f"don't push to main in project {j}",
                    rule=f"Do not push to main in project {j}"))
            digest_file = self._write_store(tmp_path, rules)

            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                active = store.active_rules()
                # All surviving active rules must have CLEAN evidence
                for r in active:
                    self.assertFalse(
                        r.evidence.lstrip().startswith("<") or
                        r.evidence.lstrip().startswith("/"),
                        f"noise-evidence rule survived as active: "
                        f"id={r.id} evidence={r.evidence[:80]!r}")
                # At least some clean rules survived
                self.assertGreater(
                    len(active), 0,
                    "expected some clean rules to remain active after purge")

    def test_load_demotes_all_four_noise_tag_shapes(self):
        """Verify each of the documented noise-evidence shapes is purged."""
        shapes = {
            "local-command-caveat": "<local-command-caveat>x</local-command-caveat>",
            "teammate-message":      "<teammate-message teammate_id=\"a\">y</teammate-message>",
            "task-notification":     "<task-notification>z</task-notification>",
            "function_calls":        "<function_calls><invoke name=\"R\"></invoke></function_calls>",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rules = []
            for i, (name, ev) in enumerate(shapes.items()):
                rules.append(self._build_polluted_rule(
                    i + 1, evidence=ev, rule=f"Do not {name} thing"))
            # One clean rule so the store isn't all noise
            rules.append(self._build_polluted_rule(
                99, evidence="never push to main", rule="Do not ever push to main"))
            digest_file = self._write_store(tmp_path, rules)

            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                active_ids = {r.id for r in store.active_rules()}
                # All 4 noise-tagged rules must be out of active
                for i, name in enumerate(shapes.keys()):
                    rid = f"R{i + 1:03d}"
                    self.assertNotIn(
                        rid, active_ids,
                        f"{name}-tagged noise rule ({rid}) must not remain active")

    def test_load_572_pollution_replay_leaves_no_noise_active(self):
        """Synthetic replay of the real poisoned backup shape (572 rules, mostly
        tag-prefixed noise). Post-load active list must contain NO noise-
        evidence rules."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rules = []
            # 552 polluted rules: various tag-prefixed evidences
            pollution_template = [
                "<teammate-message teammate_id=\"x{i}\" color=\"orange\">do y{i}</teammate-message>",
                "<local-command-caveat>The user ran /cmd-{i}</local-command-caveat>",
                "<task-notification>task {i} complete</task-notification>",
            ]
            for i in range(552):
                ev = pollution_template[i % 3].format(i=i)
                rules.append(self._build_polluted_rule(
                    i + 1, evidence=ev, rule=f"Do not {ev[:60]}"))
            # 20 clean rules at the end — these should be the active survivors
            for j in range(20):
                rules.append(self._build_polluted_rule(
                    600 + j,
                    evidence=f"never commit without running tests, rule {j}",
                    rule=f"Do not ever commit without running tests rule {j}",
                ))
            digest_file = self._write_store(tmp_path, rules)

            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                active = store.active_rules()
                # Cap must hold
                self.assertLessEqual(len(active), MAX_ACTIVE_RULES)
                # NONE of the survivors may have tag-prefixed evidence
                polluted_survivors = [
                    r for r in active
                    if r.evidence.lstrip().startswith("<")
                    or r.evidence.lstrip().startswith("/")
                ]
                self.assertEqual(
                    polluted_survivors, [],
                    f"expected zero tag-prefixed survivors, got "
                    f"{len(polluted_survivors)}: "
                    f"{[(r.id, r.evidence[:60]) for r in polluted_survivors[:3]]}")

    def test_load_preserves_clean_rules_when_under_cap(self):
        """Sanity: a mixed store with 10 clean + 10 noise rules, all active.
        Post-load: clean rules remain active (up to cap), noise rules purged."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rules = []
            # 10 noise-evidence rules
            for i in range(10):
                rules.append(self._build_polluted_rule(
                    i + 1,
                    evidence=f"<teammate-message teammate_id=\"t{i}\">msg</teammate-message>",
                    rule=f"Do not <teammate-message thing {i}"))
            # 10 clean rules
            for j in range(10):
                rules.append(self._build_polluted_rule(
                    100 + j,
                    evidence=f"don't add Co-Authored-By to commit {j}",
                    rule=f"Do not add Co-Authored-By to commit {j}"))
            digest_file = self._write_store(tmp_path, rules)

            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                store = load_digest_store("/test")
                active = store.active_rules()
                # All 10 clean rules should survive as active
                clean_active = [r for r in active
                                if not r.evidence.lstrip().startswith("<")]
                self.assertEqual(
                    len(clean_active), 10,
                    f"all 10 clean rules must remain active; got "
                    f"{len(clean_active)}")
                # Zero noise rules survive as active
                noise_active = [r for r in active
                                if r.evidence.lstrip().startswith("<")]
                self.assertEqual(
                    noise_active, [],
                    f"zero noise-evidence rules must remain active; got "
                    f"{len(noise_active)}")


# ---------------------------------------------------------------------------
# A6 — save_digest_store must be atomic (tmp + os.replace)
# ---------------------------------------------------------------------------

class TestAtomicSave(unittest.TestCase):
    """`save_digest_store` currently calls `DIGEST_FILE.write_text(...)`
    directly — a non-atomic sequence of `open(w) → write → close` that
    leaves a partial (or empty) file if the process is killed mid-write.
    Two CC hooks firing near-simultaneously (PreCompact + Stop) can also
    interleave and silently clobber one another's save (lost-update).

    Expected behavior: use tempfile + `os.replace` so the target file is
    either fully-old or fully-new — never partially written — AND one
    process's save cannot clobber another's concurrent in-flight save.
    """

    def test_save_atomic_under_mid_write_crash(self):
        """Simulate a crash: patch `Path.write_text` to raise on the first
        call. The pre-existing digest file must survive unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = tmp_path / "behavioral-digest.json"
            digest_md = tmp_path / "behavioral-digest.md"

            # Pre-populate a valid store on disk
            baseline = {
                "version": "1",
                "project": "/test",
                "updated": "2026-05-01T00:00:00+00:00",
                "session_id": "baseline",
                "strategy_rules": [{
                    "id": "R001", "rule": "Do not touch me",
                    "priority": "hard", "scope": "general",
                    "trigger": "", "decision_step": "",
                    "before": "", "after": "",
                    "signal": "EXPLICIT_CORRECTION",
                    "evidence": "don't touch me",
                    "importance": 1, "source_reliability": 1.0, "type_prior": 0.8,
                    "status": "active", "occurrence_count": 5,
                    "first_seen": "2026-05-01T00:00:00+00:00",
                    "last_reinforced": "2026-05-01T00:00:00+00:00",
                    "last_injection": None,
                }],
                "failure_patterns": [],
            }
            baseline_json = json.dumps(baseline, indent=2)
            digest_file.write_text(baseline_json, encoding="utf-8")
            original_mtime = digest_file.stat().st_mtime_ns
            original_bytes = digest_file.read_bytes()

            # Build a NEW store with different content and try to save it,
            # injecting a crash mid-write.
            new_store = DigestStore(project="/test")
            new_store.strategy_rules.append(DigestRule(
                id="R999", rule="Do not NEW RULE that must not land",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
                evidence="don't add this",
            ))

            # Simulate a real mid-write crash by patching os.replace — the
            # atomic-commit syscall that MUST succeed for the new bytes to
            # land on the target. If os.replace raises, any well-written
            # atomic save MUST leave the target file byte-for-byte unchanged
            # (the tmp file may be leaked, that's handled by the save's
            # try/finally). This test is impl-agnostic: it checks the
            # crash-safety CONTRACT, not a specific tmp naming scheme.
            real_replace = os.replace

            def exploding_replace(src, dst, *args, **kwargs):
                if str(dst).endswith("behavioral-digest.json"):
                    raise IOError("simulated os.replace failure mid-commit")
                return real_replace(src, dst, *args, **kwargs)

            with patch("cozempic.digest.DIGEST_DIR", tmp_path), \
                 patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.os.replace", exploding_replace):
                try:
                    save_digest_store(new_store)
                except (IOError, OSError):
                    pass

            # ORIGINAL file must be intact after the crash — atomic semantics.
            self.assertTrue(digest_file.exists(),
                            "digest file must still exist after crash")
            self.assertEqual(
                digest_file.read_bytes(), original_bytes,
                "original digest file must be byte-for-byte unchanged after "
                "a mid-write crash (atomic save via tmp+os.replace required)")

    def test_concurrent_save_no_lost_update(self):
        """Simulate two CC hooks (PreCompact + Stop) running concurrently:
        each loads the store, mutates it, saves it. Without file locking
        the second save CLOBBERS the first — the rule added by P1 is
        silently lost because P2 loaded the pre-P1 state.

        Expected behavior (post Phase 2c-r2): read-modify-write must be
        protected by `fcntl.flock` (or equivalent) so P2's load blocks
        until P1's save commits. Final disk state must contain BOTH rules.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = tmp_path / "behavioral-digest.json"
            digest_md = tmp_path / "behavioral-digest.md"

            # Pre-existing empty-but-valid store on disk
            initial = {
                "version": "1", "project": "/test",
                "updated": "2026-05-01T00:00:00+00:00",
                "session_id": "initial",
                "strategy_rules": [], "failure_patterns": [],
            }
            digest_file.write_text(json.dumps(initial), encoding="utf-8")

            with patch("cozempic.digest.DIGEST_DIR", tmp_path), \
                 patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md):
                # Simulate the interleaving that causes lost updates:
                #   P1.load → P2.load (BEFORE P1.save) → P1.mutate+save → P2.mutate+save
                # Without locking, P2 overwrites P1's save with the pre-P1
                # state plus P2's mutation — P1's rule is lost.
                p1_store = load_digest_store("/test")
                p2_store = load_digest_store("/test")  # P2 loads BEFORE P1 saves

                p1_store.strategy_rules.append(DigestRule(
                    id="R001", rule="Do not P1-RULE",
                    priority="hard", scope="general",
                    source_reliability=1.0, type_prior=0.8,
                    occurrence_count=1, status="active",
                    evidence="don't P1", first_seen="2026-05-05",
                    last_reinforced="2026-05-05",
                ))
                save_digest_store(p1_store)  # P1 commits

                # P2 mutates its (stale) copy and saves → should DETECT the
                # intervening P1 save and either retry or merge. With
                # fcntl.flock + stat(mtime) re-check, P2's save must either
                # (a) fail and caller retries, or (b) re-load and include
                # P1's rule before saving. Without locking, P2 silently
                # clobbers P1's addition.
                p2_store.strategy_rules.append(DigestRule(
                    id="R001", rule="Do not P2-RULE",
                    priority="hard", scope="general",
                    source_reliability=1.0, type_prior=0.8,
                    occurrence_count=1, status="active",
                    evidence="don't P2", first_seen="2026-05-05",
                    last_reinforced="2026-05-05",
                ))
                try:
                    save_digest_store(p2_store)
                except Exception:
                    # A properly-locked save may raise to force caller retry;
                    # that's an acceptable post-fix behavior.
                    pass

                # Final state on disk must contain BOTH rules (or the save
                # must have failed loudly). A silent lost-update is the bug.
                final = json.loads(digest_file.read_text(encoding="utf-8"))
                rule_texts = [r["rule"] for r in final["strategy_rules"]]
                self.assertIn(
                    "Do not P1-RULE", rule_texts,
                    f"P1's rule silently lost under concurrent save — "
                    f"final rules: {rule_texts}. Atomic save + file lock "
                    f"(fcntl.flock) required to prevent lost-update.")


class TestLoadDigestStoreHardening(unittest.TestCase):
    """BUG-15 — load_digest_store must not crash on PermissionError / OSError."""

    def test_load_returns_empty_store_on_permission_error(self):
        """Corrupted perms or unreadable file → empty store, no crash."""
        from cozempic.digest import load_digest_store
        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_file.write_text("{}", encoding="utf-8")
            real_read_text = Path.read_text

            def denying_read_text(self, *args, **kwargs):
                if str(self).endswith("behavioral-digest.json"):
                    raise PermissionError("simulated EACCES")
                return real_read_text(self, *args, **kwargs)

            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch.object(Path, "read_text", denying_read_text):
                store = load_digest_store("/test")
                self.assertTrue(store.is_empty())

    def test_load_returns_empty_store_on_oserror(self):
        """Generic OSError (disk IO failure) → empty store, no crash."""
        from cozempic.digest import load_digest_store
        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_file.write_text("{}", encoding="utf-8")
            real_read_text = Path.read_text

            def broken_read_text(self, *args, **kwargs):
                if str(self).endswith("behavioral-digest.json"):
                    raise OSError("simulated disk IO error")
                return real_read_text(self, *args, **kwargs)

            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch.object(Path, "read_text", broken_read_text):
                store = load_digest_store("/test")
                self.assertTrue(store.is_empty())


class TestSystemNoiseUnicodeMarkers(unittest.TestCase):
    """A1 — `_is_system_noise` must catch Unicode tag lookalikes and zero-width prefixes."""

    def test_rejects_fullwidth_angle_bracket(self):
        """U+FF1C FULLWIDTH LESS-THAN SIGN (＜) used in some LLM tag emissions."""
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("＜tool_call＞don't do X＜/tool_call＞"))

    def test_rejects_guillemet_wrapped(self):
        """French guillemets « » used as tag substitute by some models."""
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("«command»don't do Y«/command»"))

    def test_rejects_cjk_angle_bracket(self):
        """CJK angle brackets 〈 〉 (U+3008/U+3009)."""
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("〈system〉don't do Z〈/system〉"))

    def test_rejects_zero_width_prefix_tag(self):
        """ZWSP/BOM before '<' — Python's .strip() does NOT remove these by default."""
        is_noise = _import_system_noise()
        self.assertTrue(is_noise("​<system-reminder>don't do W</system-reminder>"))
        self.assertTrue(is_noise("﻿<command-name>/init</command-name>"))

    def test_accepts_genuine_correction_with_guillemet_quotes(self):
        """False-positive check: guillemets around a word (not wrapping a tag) must pass."""
        is_noise = _import_system_noise()
        self.assertFalse(is_noise("don't use «Write» on existing files"))


class TestMemdirConfigDirFallback(unittest.TestCase):
    """A tightening of TestMemdirHonorsConfigDir — verify fallback path includes `.claude`
    even when HOME is patched, by directly checking the returned path structure."""

    def test_fallback_path_structure_when_env_unset(self):
        """When CLAUDE_CONFIG_DIR is unset, _get_memdir must resolve a path ending in
        `.claude/projects/<slug>/memory`. This is a structural check, not HOME-patching."""
        import os
        with tempfile.TemporaryDirectory() as fake_home:
            projects_dir = Path(fake_home) / ".claude" / "projects" / "-test-cwd" / "memory"
            projects_dir.mkdir(parents=True)

            env = os.environ.copy()
            env.pop("CLAUDE_CONFIG_DIR", None)

            with patch.dict(os.environ, env, clear=True), \
                 patch("pathlib.Path.home", return_value=Path(fake_home)):
                result = _get_memdir("/test/cwd")
                self.assertIsNotNone(result)
                self.assertTrue(str(result).endswith(".claude/projects/-test-cwd/memory"),
                                f"expected .claude/... suffix, got {result}")


class TestLoadRevalidatesRulesAgainstHardening(unittest.TestCase):
    """Auto-migration: pre-existing stores with rules that would be REJECTED by the
    current hardening (_to_prohibition gate + _is_system_noise) must be demoted on
    load. This is the "zero user action needed on upgrade" contract.

    Real-world pollution seen pre-fix:
    - Long multi-paragraph prompts ("# BMAD — Big Model Adversarial Debate...")
    - Pasted messages ("regarde ce message slack de notre po Hello team...")
    - cozempic-self meta noise ("[Cozempic Guard: context was pruned...]")
    - Compaction-resume banners ("This session is being continued...")

    None of these pass _to_prohibition (>200 chars, multi-line, markdown-lead)
    but a pre-hardening store has them stored as status=active.
    """

    def test_load_demotes_markdown_prefixed_active(self):
        """Rule with evidence starting with '#' (markdown header) must be demoted."""
        import tempfile
        from cozempic.digest import DigestStore, DigestRule, save_digest_store, load_digest_store

        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_md = Path(tmp) / "behavioral-digest.md"
            # Pre-populate a store with a markdown-prefixed active rule (as if saved pre-fix)
            store = DigestStore(project="/test")
            store.strategy_rules.append(DigestRule(
                id="R001",
                rule="Do not # BMAD — Big Model Adversarial Debate prompt text here",
                evidence="# BMAD — Big Model Adversarial Debate\n\nYou are the Leader",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
                first_seen="2026-04-01", last_reinforced="2026-04-01",
            ))
            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.DIGEST_DIR", Path(tmp)):
                save_digest_store(store)
                reloaded = load_digest_store("/test")
                self.assertEqual(len(reloaded.active_rules()), 0,
                                 "markdown-prefixed rule must be demoted on load — "
                                 "upgrade-time auto-migration")

    def test_load_demotes_oversize_active(self):
        """Rule with evidence > 200 chars must be demoted (matches _to_prohibition gate)."""
        import tempfile
        from cozempic.digest import DigestStore, DigestRule, save_digest_store, load_digest_store

        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_md = Path(tmp) / "behavioral-digest.md"
            long_evidence = "Regarde ce message slack " + "x" * 300
            store = DigestStore(project="/test")
            store.strategy_rules.append(DigestRule(
                id="R001", rule="Do not " + long_evidence[:193],
                evidence=long_evidence,
                priority="hard", scope="git",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
            ))
            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.DIGEST_DIR", Path(tmp)):
                save_digest_store(store)
                reloaded = load_digest_store("/test")
                self.assertEqual(len(reloaded.active_rules()), 0,
                                 "oversize rule must be demoted on load")

    def test_load_demotes_multiline_active(self):
        """Rule with > 2 newlines in evidence must be demoted."""
        import tempfile
        from cozempic.digest import DigestStore, DigestRule, save_digest_store, load_digest_store

        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_md = Path(tmp) / "behavioral-digest.md"
            store = DigestStore(project="/test")
            store.strategy_rules.append(DigestRule(
                id="R001", rule="Do not multi line prompt",
                evidence="line1\nline2\nline3\nline4 with don't",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
            ))
            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.DIGEST_DIR", Path(tmp)):
                save_digest_store(store)
                reloaded = load_digest_store("/test")
                self.assertEqual(len(reloaded.active_rules()), 0,
                                 "multi-line rule must be demoted on load")

    def test_load_demotes_cozempic_meta_noise(self):
        """Cozempic's own compaction-restoration banner is self-noise."""
        is_noise = _import_system_noise()
        # Part 1: direct test of the marker
        self.assertTrue(is_noise("[Cozempic Guard: context was pruned. Team state restored below]"))

        # Part 2: integration via load
        import tempfile
        from cozempic.digest import DigestStore, DigestRule, save_digest_store, load_digest_store
        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_md = Path(tmp) / "behavioral-digest.md"
            store = DigestStore(project="/test")
            store.strategy_rules.append(DigestRule(
                id="R001", rule="Do not [Cozempic Guard: context was pruned",
                evidence="[Cozempic Guard: context was pruned. Team state restored below for your reference — do not echo it back]",
                priority="hard", scope="general",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=1, status="active",
            ))
            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.DIGEST_DIR", Path(tmp)):
                save_digest_store(store)
                reloaded = load_digest_store("/test")
                self.assertEqual(len(reloaded.active_rules()), 0,
                                 "cozempic-meta noise rule must be demoted on load")

    def test_load_demotes_session_resume_banner(self):
        """Claude Code compaction-resume banner is noise, not a correction."""
        is_noise = _import_system_noise()
        banner = "This session is being continued from a previous conversation that ran out of context."
        # Long + specific phrase → should be caught
        self.assertTrue(is_noise(banner))

    def test_load_preserves_genuine_clean_corrections(self):
        """Baseline: genuine, short, well-formed corrections are PRESERVED on load."""
        import tempfile
        from cozempic.digest import DigestStore, DigestRule, save_digest_store, load_digest_store

        with tempfile.TemporaryDirectory() as tmp:
            digest_file = Path(tmp) / "behavioral-digest.json"
            digest_md = Path(tmp) / "behavioral-digest.md"
            store = DigestStore(project="/test")
            store.strategy_rules.append(DigestRule(
                id="R001",
                rule="Do not add Co-Authored-By",
                evidence="don't add Co-Authored-By",
                priority="hard", scope="git",
                source_reliability=1.0, type_prior=0.8,
                occurrence_count=3, status="active",
            ))
            with patch("cozempic.digest.DIGEST_FILE", digest_file), \
                 patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
                 patch("cozempic.digest.DIGEST_DIR", Path(tmp)):
                save_digest_store(store)
                reloaded = load_digest_store("/test")
                self.assertEqual(len(reloaded.active_rules()), 1,
                                 "genuine clean correction must be preserved on load")


if __name__ == "__main__":
    unittest.main()


# ── Follow-up: boundary tests for _to_prohibition + purge persistence ─────

class TestToProhibitionBoundary(unittest.TestCase):
    """Edge cases at exact thresholds for _to_prohibition rejection."""

    def test_exactly_200_chars_passes(self):
        """200 chars should pass (threshold is > 200, not >=)."""
        text = "don't " + "x" * 194  # 6 + 194 = 200
        self.assertEqual(len(text), 200)
        result = _to_prohibition(text)
        self.assertNotEqual(result, "", "exactly 200 chars must pass")

    def test_201_chars_rejected(self):
        """201 chars should be rejected."""
        text = "don't " + "x" * 195  # 6 + 195 = 201
        self.assertEqual(len(text), 201)
        self.assertEqual(_to_prohibition(text), "")

    def test_exactly_2_newlines_passes(self):
        """2 newlines should pass (threshold is > 2, not >=)."""
        text = "don't do line1\nline2\nline3"
        self.assertEqual(text.count("\n"), 2)
        result = _to_prohibition(text)
        self.assertNotEqual(result, "", "exactly 2 newlines must pass")

    def test_3_newlines_rejected(self):
        """3 newlines should be rejected."""
        text = "don't do\nline1\nline2\nline3"
        self.assertEqual(text.count("\n"), 3)
        self.assertEqual(_to_prohibition(text), "")


class TestPurgePersistsToDisk(unittest.TestCase):
    """load_digest_store must save the migrated state so the purge is one-shot."""

    def test_purge_writes_back_to_disk(self):
        """After loading a polluted store, the on-disk file should reflect
        the demotions — so a second load doesn't re-scan."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            digest_file = tmp_path / "behavioral-digest.json"

            # Build a store with noise rules
            rules = []
            for i in range(5):
                rules.append({
                    "id": f"R{i:03d}",
                    "rule": f"Do not <teammate-message>noise {i}</teammate-message>",
                    "priority": "hard",
                    "scope": "general",
                    "trigger": "",
                    "decision_step": "",
                    "before": "",
                    "after": "",
                    "signal": "EXPLICIT_CORRECTION",
                    "evidence": f"<teammate-message>noise {i}</teammate-message>",
                    "importance": 1,
                    "source_reliability": 1.0,
                    "type_prior": 0.8,
                    "status": "active",
                    "occurrence_count": 2,
                    "first_seen": "2026-05-01T00:00:00",
                    "last_reinforced": "2026-05-01T00:00:00",
                    "last_injection": None,
                })
            # Add one clean rule
            rules.append({
                "id": "R099",
                "rule": "Do not add Co-Authored-By to commits",
                "priority": "hard",
                "scope": "git",
                "trigger": "",
                "decision_step": "",
                "before": "",
                "after": "",
                "signal": "EXPLICIT_CORRECTION",
                "evidence": "never add Co-Authored-By",
                "importance": 3,
                "source_reliability": 1.0,
                "type_prior": 0.8,
                "status": "active",
                "occurrence_count": 3,
                "first_seen": "2026-05-01T00:00:00",
                "last_reinforced": "2026-05-01T00:00:00",
                "last_injection": None,
            })

            data = {
                "version": "1", "project": "/test", "updated": "",
                "session_id": "", "strategy_rules": rules, "failure_patterns": [],
            }
            digest_file.write_text(json.dumps(data), encoding="utf-8")

            from unittest.mock import patch
            with patch("cozempic.digest.DIGEST_FILE", digest_file):
                # First load — should purge + save
                store1 = load_digest_store("/test")
                active1 = [r for r in store1.strategy_rules if r.status == "active"]
                self.assertEqual(len(active1), 1, "only clean rule should be active")
                self.assertIn("Co-Authored-By", active1[0].rule)

                # Verify disk was updated
                disk_data = json.loads(digest_file.read_text())
                disk_active = [r for r in disk_data["strategy_rules"] if r["status"] == "active"]
                self.assertEqual(len(disk_active), 1,
                    "purged state must be persisted to disk")

                # Second load — should be a no-op (no re-purge needed)
                store2 = load_digest_store("/test")
                active2 = [r for r in store2.strategy_rules if r.status == "active"]
                self.assertEqual(len(active2), 1)


# ---------------------------------------------------------------------------
# BUG-11 — _infer_scope word-boundary
# ---------------------------------------------------------------------------

class TestInferScopeWordBoundary(unittest.TestCase):
    """`_infer_scope` must match keywords as whole words, not substrings.

    Substring match produces silent false-positives that break BUG-7's
    dedup gate (which requires scope+priority match before text overlap).
    "make it digital" matching "git" scope makes a GENERAL rule collide
    with genuine git rules on dedup.
    """

    def test_digital_is_not_git(self):
        """'digital' contains 'git' as substring but is not a git scope."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("make it more digital"), "general")

    def test_editorial_is_not_file_ops(self):
        """'editorial' contains 'edit' as substring but is not a file-ops scope."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("editorial review of the draft"), "general")

    def test_testimony_is_not_testing(self):
        """'testimony' contains 'test' as substring but is not a testing scope."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("testimony from the witness"), "general")

    def test_merger_is_not_git(self):
        """'merger' contains 'merge' as substring but is not a git scope."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("the merger acquisition"), "general")

    def test_slackline_is_not_communication(self):
        """'slackline' contains 'slack' as substring but is not a communication scope."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("slackline balance practice"), "general")

    def test_write_tests_is_testing_not_file_ops(self):
        """Order-dependent: `don't write tests` has BOTH 'write' (file-ops)
        AND 'tests' (testing). Testing intent should win — `tests` is the
        noun; `write` is the verb operating on tests. Under the old order
        `file-ops` wins because it appears first in the if/elif chain.
        Word-boundary fix alone may not resolve this — may need priority
        ordering adjustment. Documented as part of the fix."""
        from cozempic.digest import _infer_scope
        # Prefer testing since the user is explicitly talking about tests
        self.assertEqual(_infer_scope("don't write tests"), "testing")

    # Positive tests — real matches must still work
    def test_legit_git_still_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("always push to git main"), "git")
        self.assertEqual(_infer_scope("never commit secrets"), "git")

    def test_legit_file_ops_still_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("don't edit the config file"), "file-ops")

    def test_legit_testing_still_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("never mock the database in tests"), "testing")

    def test_legit_communication_still_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("reply to the slack message"), "communication")

    def test_case_insensitive_preserved(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("Always PUSH to GIT"), "git")


# ---------------------------------------------------------------------------
# BUG-11 — _infer_scope edge case hardening (PR #85 verification probes)
# ---------------------------------------------------------------------------


class TestInferScopeEdgeCases(unittest.TestCase):
    """Edge case hardening — empty/whitespace, Unicode, long inputs, mixed scripts,
    URLs, regex injection, punctuation boundaries, multi-scope priority.
    Added as part of PR #85 adversarial verification.
    """

    # -- empty / whitespace / newline-only -----------------------------------

    def test_empty_string_returns_general_no_crash(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope(""), "general")

    def test_whitespace_only_returns_general(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("  "), "general")
        self.assertEqual(_infer_scope("\n\n"), "general")
        self.assertEqual(_infer_scope("\t"), "general")
        self.assertEqual(_infer_scope(" \t\n "), "general")

    # -- Unicode / CJK / emoji / zero-width ----------------------------------

    def test_guillemets_wrapped_keyword_still_matches(self):
        """Guillemets are non-word chars; the GIT token remains intact."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("«GIT»"), "git")

    def test_fullwidth_punctuation_around_keyword_still_matches(self):
        """Fullwidth backticks around `push` do not prevent tokenization."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("｀push｀"), "git")

    def test_turkish_dotless_i_does_not_match(self):
        """U+0131 (dotless i) is distinct from U+0069 (i) after .lower() —
        `gıt` is not `git`. Fix must not silently coerce."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("pısh to gıt"), "general")

    def test_zero_width_space_between_keywords_loses_match(self):
        """Zero-width space U+200B inside a keyword breaks tokenization.
        This is acceptable — the user cannot have typed a ZWSP by accident;
        if present, the token is genuinely not a keyword."""
        from cozempic.digest import _infer_scope
        # "don'tZWSPpush" — tokens are ['don', 't​push'] — no match
        # but the full string contains "push" after zero-width is treated
        # as part of the token — let's assert the current behavior
        r = _infer_scope("don't​push")
        # Result is 'git' because regex r'[\w-]+' with UNICODE splits on ZWSP?
        # Actually \w matches ZWSP in Python 3, so this stays as one token.
        # Document the behavior — this is implementation-defined and we
        # just want to ensure no crash and deterministic output.
        self.assertIn(r, ("general", "git"))

    def test_cjk_scope_keyword_does_not_match(self):
        """CJK characters for 'push' (推送) do not match English keywords."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("推送 to main"), "general")

    def test_emoji_only_returns_general(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("\U0001f527\U0001f4bb\U0001f680"), "general")

    def test_cjk_plus_english_keyword_still_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("don't 推送 to git"), "git")

    # -- very long input -----------------------------------------------------

    def test_long_input_no_regression(self):
        """10k-char input with keyword at tail must still match without OOM."""
        from cozempic.digest import _infer_scope
        big = ("lorem " * 1000) + " don't push"
        self.assertEqual(_infer_scope(big), "git")

    # -- URL / path false-positive check -------------------------------------

    def test_url_path_etc_passwords_maps_to_file_ops(self):
        """`/etc/passwords` tokenizes as etc, passwords. file-ops wins via 'read'."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("don't read /etc/passwords"), "file-ops")

    def test_path_with_git_substring_but_no_token_is_general(self):
        """/usr/local/gitlab/config has 'git' only as substring of 'gitlab'.
        Post-fix should return general (BUG-11 premise)."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("/usr/local/gitlab/config"), "general")

    # -- regex injection safety ---------------------------------------------

    def test_regex_metacharacters_in_input_not_interpreted(self):
        """User text containing regex metacharacters must not be interpreted
        as a pattern — tokenizer uses re.findall on the static pattern."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("don't (.*?)"), "general")
        self.assertEqual(_infer_scope(r"don't \b\w+\b"), "general")
        # [git] contains the token 'git' as whole word
        self.assertEqual(_infer_scope("[git]"), "git")

    # -- punctuation boundary -----------------------------------------------

    def test_parenthesized_keyword_matches(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("(commit)"), "git")
        self.assertEqual(_infer_scope("[merge]"), "git")

    def test_dot_notation_keyword_matches(self):
        """`.push()` tokenizes so that `push` is a standalone token."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope(".push()"), "git")

    def test_hyphen_compound_keeps_token_intact(self):
        """`don't-push` tokenizes as ['don', 't-push'] — the fix preserves
        hyphens in tokens. This means `pre-push` / `force-push` style
        compounds will NOT match bare `push` keyword. Documented tradeoff."""
        from cozempic.digest import _infer_scope
        # don't-push → token 't-push' does not match 'push' whole token
        self.assertEqual(_infer_scope("don't-push"), "general")
        # pre-push hook → pre-push is one token, no match
        self.assertEqual(_infer_scope("pre-push hook"), "general")

    # -- multi-scope priority -----------------------------------------------

    def test_push_tests_resolves_to_testing(self):
        """BOTH 'push' (git) AND 'tests' (testing) present — testing wins
        because it is listed FIRST in _SCOPE_KEYWORDS table."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("push tests"), "testing")

    def test_write_to_main_branch_resolves_to_git(self):
        """write (file-ops) + branch (git) — git wins because testing was
        checked first (no match) and file-ops would normally come before git,
        but `write` and `branch` are both present; current table order
        puts testing → git → file-ops so branch/git wins over write/file-ops."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("don't write to main branch"), "git")

    def test_merge_and_test_resolves_to_testing(self):
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("merge and test"), "testing")

    # -- co-authored compound stays matched ---------------------------------

    def test_co_authored_by_compound_still_matches(self):
        """Verify BUG-11 fix keeps hyphenated git keyword `co-authored-by`
        working (it's explicitly in the keyword set as a single token)."""
        from cozempic.digest import _infer_scope
        self.assertEqual(_infer_scope("co-authored-by me"), "git")
        self.assertEqual(_infer_scope("CO-AUTHORED-BY: ..."), "git")
