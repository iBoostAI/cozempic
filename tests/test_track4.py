"""Tests for Track 4: memdir sync, build_injection_text, flush/recover, hooks."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic.digest import (
    PROTECTION_TAG,
    DigestRule,
    DigestStore,
    build_injection_text,
    flush_digest,
    load_digest_store,
    recover_digest,
    save_digest_store,
    sync_to_memdir,
)
from cozempic.helpers import msg_bytes

import cozempic.strategies  # noqa: F401


def make_message(line_idx: int, msg: dict) -> tuple[int, dict, int]:
    return (line_idx, msg, msg_bytes(msg))


def make_user(line_idx: int, text: str = "hi") -> tuple[int, dict, int]:
    return make_message(line_idx, {
        "type": "user",
        "message": {"role": "user", "content": text},
    })


def make_assistant(line_idx: int, text: str = "ok") -> tuple[int, dict, int]:
    return make_message(line_idx, {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _make_store_with_rules() -> DigestStore:
    store = DigestStore(project="/test")
    store.strategy_rules.append(DigestRule(
        id="R001", rule="Do not add Co-Authored-By to commits",
        priority="hard", scope="git", status="active",
        occurrence_count=5, source_reliability=1.0, type_prior=0.8,
        evidence="don't add Co-Authored-By",
        first_seen="2026-04-01", last_reinforced="2026-04-01",
    ))
    store.strategy_rules.append(DigestRule(
        id="R002", rule="Always use Edit for existing files, not Write",
        priority="soft", scope="file-ops", status="active",
        occurrence_count=3, source_reliability=0.9, type_prior=0.9,
        evidence="use Edit instead of Write",
        first_seen="2026-04-01", last_reinforced="2026-04-01",
    ))
    store.strategy_rules.append(DigestRule(
        id="R003", rule="Do not mock the database in tests",
        priority="soft", scope="testing", status="pending",
        occurrence_count=1, source_reliability=0.6, type_prior=0.6,
    ))
    return store


# ---------------------------------------------------------------------------
# build_injection_text
# ---------------------------------------------------------------------------

class TestBuildInjectionText(unittest.TestCase):

    def test_returns_none_for_empty_store(self):
        self.assertIsNone(build_injection_text(DigestStore()))

    def test_returns_none_for_only_pending(self):
        store = DigestStore()
        store.strategy_rules.append(DigestRule(id="R001", rule="test", status="pending"))
        self.assertIsNone(build_injection_text(store))

    def test_formats_hard_and_soft_rules(self):
        store = _make_store_with_rules()
        text = build_injection_text(store)
        self.assertIn("BEHAVIORAL CONTRACT", text)
        self.assertIn("PROHIBITIONS:", text)
        self.assertIn("PREFERENCES:", text)
        self.assertIn("Co-Authored-By", text)

    def test_excludes_pending_rules(self):
        store = _make_store_with_rules()
        text = build_injection_text(store)
        self.assertNotIn("mock the database", text)

    def test_hard_rules_first(self):
        store = _make_store_with_rules()
        text = build_injection_text(store)
        self.assertLess(text.index("PROHIBITIONS:"), text.index("PREFERENCES:"))


# ---------------------------------------------------------------------------
# sync_to_memdir
# ---------------------------------------------------------------------------

class TestSyncToMemdir(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_memory_file(self):
        store = _make_store_with_rules()
        with patch("cozempic.digest._get_memdir", return_value=self.mem_dir):
            synced = sync_to_memdir(store)
        self.assertEqual(synced, 2)  # 2 active rules
        digest_mem = self.mem_dir / "cozempic_digest.md"
        self.assertTrue(digest_mem.exists())
        content = digest_mem.read_text()
        self.assertIn("type: feedback", content)
        self.assertIn("BEHAVIORAL CONTRACT", content)
        self.assertIn("Co-Authored-By", content)

    def test_removes_file_when_no_active_rules(self):
        # Pre-create file
        digest_mem = self.mem_dir / "cozempic_digest.md"
        digest_mem.write_text("old content")
        store = DigestStore()
        with patch("cozempic.digest._get_memdir", return_value=self.mem_dir):
            synced = sync_to_memdir(store)
        self.assertEqual(synced, 0)
        self.assertFalse(digest_mem.exists())

    def test_returns_zero_when_no_memdir(self):
        store = _make_store_with_rules()
        with patch("cozempic.digest._get_memdir", return_value=None):
            synced = sync_to_memdir(store)
        self.assertEqual(synced, 0)

    def test_updates_last_injection(self):
        store = _make_store_with_rules()
        self.assertIsNone(store.strategy_rules[0].last_injection)
        with patch("cozempic.digest._get_memdir", return_value=self.mem_dir):
            sync_to_memdir(store)
        self.assertIsNotNone(store.strategy_rules[0].last_injection)


# ---------------------------------------------------------------------------
# Flush / Recover
# ---------------------------------------------------------------------------

class TestFlushRecover(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_flush_extracts_and_syncs(self):
        """Post-BUG-4 fix: rules start as pending. Two occurrences → promote to active → memdir synced."""
        messages = [
            make_assistant(0, "I'll add the Co-Authored-By"),
            make_user(1, "don't add Co-Authored-By"),
            make_assistant(2, "I'll add Co-Authored-By again"),
            make_user(3, "don't add Co-Authored-By to commits"),
        ]
        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"

        with patch("cozempic.digest.DIGEST_DIR", self.tmpdir), \
             patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
             patch("cozempic.digest._get_memdir", return_value=self.mem_dir):
            added, upvoted, rejected = flush_digest(messages, project_dir="/test")
            self.assertGreater(added + upvoted, 0)
            self.assertTrue(digest_file.exists())
            # Memdir should have been synced too (rule promoted to active via 2nd occurrence)
            digest_mem = self.mem_dir / "cozempic_digest.md"
            self.assertTrue(digest_mem.exists())

    def test_recover_syncs_to_memdir(self):
        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"

        store = _make_store_with_rules()
        with patch("cozempic.digest.DIGEST_DIR", self.tmpdir), \
             patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
             patch("cozempic.digest._get_memdir", return_value=self.mem_dir):
            save_digest_store(store)
            synced = recover_digest(project_dir="/test")
            self.assertGreater(synced, 0)
            content = (self.mem_dir / "cozempic_digest.md").read_text()
            self.assertIn("Co-Authored-By", content)

    def test_full_cycle(self):
        """flush → compaction (memdir survives) → recover re-syncs.

        Post-BUG-4 fix: rules start pending, promoted to active via PROMOTION_COUNT=2.
        Test uses 2 occurrences of the same correction to trigger promotion.
        """
        digest_file = self.tmpdir / "behavioral-digest.json"
        digest_md = self.tmpdir / "behavioral-digest.md"

        with patch("cozempic.digest.DIGEST_DIR", self.tmpdir), \
             patch("cozempic.digest.DIGEST_FILE", digest_file), \
             patch("cozempic.digest.DIGEST_MD_FILE", digest_md), \
             patch("cozempic.digest._get_memdir", return_value=self.mem_dir):

            # Flush: extract corrections (2 occurrences → promote via upvote)
            messages = [
                make_assistant(0, "I'll add Co-Authored-By"),
                make_user(1, "don't add Co-Authored-By"),
                make_assistant(2, "adding Co-Authored-By again"),
                make_user(3, "don't add Co-Authored-By to commits"),
            ]
            flush_digest(messages, project_dir="/test")

            # Verify active after 2nd occurrence (PROMOTION_COUNT=2 threshold met)
            store = load_digest_store("/test")
            self.assertGreater(len(store.active_rules()), 0)

            # Verify memdir synced
            self.assertTrue((self.mem_dir / "cozempic_digest.md").exists())

            # Recover: re-sync (idempotent)
            synced = recover_digest(project_dir="/test")
            self.assertGreater(synced, 0)


# ---------------------------------------------------------------------------
# Hooks.json
# ---------------------------------------------------------------------------

class TestHooksJson(unittest.TestCase):

    def test_hooks_json_is_valid(self):
        hooks_path = Path(__file__).parent.parent / "plugin" / "hooks" / "hooks.json"
        if not hooks_path.exists():
            self.skipTest("hooks.json not found")
        data = json.loads(hooks_path.read_text())
        hooks = data["hooks"]
        self.assertIn("SessionStart", hooks)
        self.assertIn("PreCompact", hooks)
        self.assertIn("Stop", hooks)

    def test_digest_commands_in_hooks(self):
        hooks_path = Path(__file__).parent.parent / "plugin" / "hooks" / "hooks.json"
        if not hooks_path.exists():
            self.skipTest("hooks.json not found")
        raw = hooks_path.read_text()
        self.assertIn("digest inject", raw)
        self.assertIn("digest flush", raw)


if __name__ == "__main__":
    unittest.main()
