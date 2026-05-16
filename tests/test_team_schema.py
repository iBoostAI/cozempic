"""Tests for schema-first team message detection and config merge."""

from __future__ import annotations

import json
import unittest

from cozempic.team import (
    TaskInfo,
    TeamState,
    _is_team_message,
    _is_task_tool_result,
    extract_team_state,
    inject_team_recovery,
    merge_config_into_state,
)


def _msg(content, role="assistant"):
    """Build a minimal JSONL message dict."""
    return {"message": {"role": role, "content": content}}


def _tool_use(name, tool_id="tid-001", input=None):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input or {}}


def _tool_result(tool_use_id, text="done"):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}


class TestIsTeamMessage(unittest.TestCase):

    # ── Definitive positives ──────────────────────────────────────────────────

    def test_task_tool_use_is_team(self):
        msg = _msg([_tool_use("Task", "t1")])
        self.assertTrue(_is_team_message(msg))

    def test_task_create_tool_use_is_team(self):
        msg = _msg([_tool_use("TaskCreate", "t2")])
        self.assertTrue(_is_team_message(msg))

    def test_send_message_tool_use_is_team(self):
        msg = _msg([_tool_use("SendMessage", "t3")])
        self.assertTrue(_is_team_message(msg))

    def test_task_notification_xml_is_team(self):
        content = "<task-notification><task-id>x</task-id><status>done</status><summary>ok</summary><result>42</result></task-notification>"
        msg = _msg(content, role="user")
        self.assertTrue(_is_team_message(msg))

    def test_queue_operation_with_task_notification_is_team(self):
        msg = {"type": "queue-operation", "content": "<task-notification>...</task-notification>"}
        self.assertTrue(_is_team_message(msg))

    def test_tool_result_with_matching_task_id_is_team(self):
        pending = {"t-abc"}
        msg = _msg([_tool_result("t-abc", "agent output")])
        self.assertTrue(_is_team_message(msg, pending_task_ids=pending))

    def test_tool_result_for_task_output_is_team(self):
        """tool_result for TaskOutput (polling agent status) must be preserved."""
        pending = {"t-poll"}
        msg = _msg([_tool_result("t-poll", "agent still running")])
        self.assertTrue(_is_team_message(msg, pending_task_ids=pending))

    # ── Definitive negatives ──────────────────────────────────────────────────

    def test_plain_text_agent_id_mention_is_NOT_team(self):
        """Regression: a message mentioning 'agent_id' in text should NOT be team."""
        msg = _msg([{"type": "text", "text": "the agent_id: foo configuration is important"}])
        self.assertFalse(_is_team_message(msg))

    def test_tool_result_content_with_team_keywords_NOT_team_without_ids(self):
        """Without pending_task_ids, tool_result keyword content is NOT classified as team."""
        msg = _msg([_tool_result("unknown-id", "agent_id: foo TeamCreate spawn")])
        self.assertFalse(_is_team_message(msg, pending_task_ids=None))

    def test_tool_result_with_non_matching_id_is_NOT_team(self):
        pending = {"other-id"}
        msg = _msg([_tool_result("t-xyz", "agent output")])
        self.assertFalse(_is_team_message(msg, pending_task_ids=pending))

    def test_regular_tool_use_is_NOT_team(self):
        msg = _msg([_tool_use("Bash", "t9", {"command": "ls"})])
        self.assertFalse(_is_team_message(msg))

    def test_queue_operation_without_task_notification_is_NOT_team(self):
        msg = {"type": "queue-operation", "content": "some other content"}
        self.assertFalse(_is_team_message(msg))

    def test_string_content_without_task_notification_is_NOT_team(self):
        msg = _msg("just a regular user message with agent_id mention", role="user")
        self.assertFalse(_is_team_message(msg))


class TestMergeConfigStrongJoin(unittest.TestCase):

    def _state(self, **kwargs):
        from cozempic.team import SubagentInfo
        s = TeamState()
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def test_merge_on_lead_session_id(self):
        state = self._state(lead_session_id="sess-abc", team_name="")
        configs = [
            {"leadSessionId": "sess-abc", "name": "TeamA", "members": [], "leadAgentId": "a1"},
            {"leadSessionId": "sess-xyz", "name": "TeamB", "members": [], "leadAgentId": "a2"},
        ]
        result = merge_config_into_state(state, configs)
        self.assertEqual(result.team_name, "TeamA")

    def test_merge_on_lead_agent_id(self):
        state = self._state(lead_agent_id="agent-99", team_name="")
        configs = [
            {"leadAgentId": "agent-99", "name": "CorrectTeam", "members": [], "leadSessionId": ""},
        ]
        result = merge_config_into_state(state, configs)
        self.assertEqual(result.team_name, "CorrectTeam")

    def test_no_strong_join_skips_merge(self):
        state = self._state(team_name="", lead_session_id=None, lead_agent_id=None)
        configs = [
            {"leadSessionId": "other", "name": "RandomTeam", "members": []},
        ]
        result = merge_config_into_state(state, configs)
        # team_name must NOT be set to RandomTeam
        self.assertEqual(result.team_name, "")

    def test_name_match_still_works(self):
        state = self._state(team_name="MyTeam")
        configs = [
            {"name": "MyTeam", "members": [], "leadAgentId": "a1"},
        ]
        result = merge_config_into_state(state, configs)
        self.assertEqual(result.lead_agent_id, "a1")

    def test_empty_configs_returns_state_unchanged(self):
        state = self._state(team_name="Existing")
        result = merge_config_into_state(state, [])
        self.assertEqual(result.team_name, "Existing")


class TestTeamRecoveryRendering(unittest.TestCase):

    def _mixed_task_state(self):
        return TeamState(tasks=[
            TaskInfo("1", "Ship current fix", "pending"),
            TaskInfo("2", "Review tests", "in_progress", owner="dev"),
            TaskInfo("3", "Old completed task", "completed"),
            TaskInfo("4", "", "completed"),
        ])

    def test_recovery_text_lists_active_tasks_and_summarizes_omitted(self):
        text = self._mixed_task_state().to_recovery_text()

        self.assertIn("Shared active tasks:", text)
        self.assertIn("[PENDING] Ship current fix", text)
        self.assertIn("[IN_PROGRESS] Review tests (owner: dev)", text)
        self.assertIn("1 completed, 1 blank", text)
        self.assertNotIn("Old completed task", text)

    def test_checkpoint_markdown_lists_active_tasks_and_summarizes_omitted(self):
        text = self._mixed_task_state().to_markdown()

        self.assertIn("## Active Task List", text)
        self.assertIn("- [ ] Ship current fix", text)
        self.assertIn("- [/] Review tests @dev", text)
        self.assertIn("1 completed, 1 blank", text)
        self.assertNotIn("Old completed task", text)

    def test_inject_team_recovery_skips_completed_only_tasks(self):
        messages = [(0, {"uuid": "root", "message": {"role": "assistant", "content": []}}, 1)]
        state = TeamState(tasks=[
            TaskInfo("1", "Already finished", "completed"),
            TaskInfo("2", "", "completed"),
        ])

        self.assertEqual(inject_team_recovery(messages, state), messages)

    def test_inject_team_recovery_deduplicates_same_recovery_block(self):
        messages = [(0, {"uuid": "root", "message": {"role": "assistant", "content": []}}, 1)]
        state = TeamState(tasks=[
            TaskInfo("1", "Ship current fix", "pending"),
            TaskInfo("2", "Already finished", "completed"),
        ])

        once = inject_team_recovery(messages, state)
        twice = inject_team_recovery(once, state)

        self.assertEqual(len(once), 3)
        self.assertEqual(len(twice), 3)


class TestExtractTeamState(unittest.TestCase):

    def test_empty_messages_returns_empty_state(self):
        state = extract_team_state([])
        self.assertTrue(state.is_empty())

    def test_task_spawn_detected(self):
        msgs = [
            (0, _msg([_tool_use("Task", "t1", {"subagent_type": "general-purpose", "prompt": "do x"})]), 100),
        ]
        state = extract_team_state(msgs)
        self.assertEqual(len(state.subagents), 1)
