#!/usr/bin/env python3
"""Behavior of the agent back office through build_board / to_wire and a failed Orca CLI."""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import board

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
NOW = datetime(2026, 11, 1, 7, 20, tzinfo=timezone.utc)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def flatten_groups(wire: dict) -> list[dict]:
    found: list[dict] = []

    def walk(node: dict) -> None:
        found.append(node)
        for child in node.get("children") or []:
            walk(child)

    for group in wire["groups"]:
        for agent in group["agents"]:
            walk(agent)
    return found


def connected_keys(worktrees_payload: dict, terminals_payload: dict, hooks: dict) -> set[str]:
    worktrees = worktrees_payload["result"]["worktrees"]
    terminals = terminals_payload["result"]["terminals"]
    agents: dict[str, dict] = {}
    for workspace in worktrees:
        for agent in workspace.get("agents") or []:
            if agent.get("paneKey"):
                agents[agent["paneKey"]] = agent
    terms: dict[str, dict] = {}
    for terminal in terminals:
        if terminal.get("agentIdentity") in board.VENDORS and terminal.get("tabId") and terminal.get("leafId"):
            terms[f"{terminal['tabId']}:{terminal['leafId']}"] = terminal
    expected: set[str] = set()
    entries = hooks.get("entries") or {}
    for key in set(agents) | set(terms):
        agent = agents.get(key)
        terminal = terms.get(key)
        if terminal is not None:
            connected = bool(terminal.get("connected")) and not bool(terminal.get("orphaned"))
        else:
            connected = bool(agent and agent.get("hasLivePty"))
        if not connected:
            continue
        entry = entries.get(key) or {}
        provider = entry.get("providerSession") or {}
        presence = entry.get("agentPresence") or {}
        sid = provider.get("id")
        vendor = presence.get("agent")
        if vendor in board.VENDORS and sid:
            expected.add(f"{vendor}:{sid}")
        else:
            expected.add(f"orca:{key}")
    return expected


class BoardBehaviorTest(unittest.TestCase):
    def test_real_fixtures_join_every_connected_pane(self) -> None:
        worktrees = load("worktree_ps.json")
        terminals = load("terminal_list.json")
        hooks = load("last-status.json")
        orca = board.parse_orca({"worktrees": worktrees, "terminals": terminals}, hooks)
        self.assertTrue(orca.up)
        self.assertFalse(any("unsupported" in item for item in orca.problems))
        wire = board.to_wire(board.build_board({}, orca, {}, NOW), {}, NOW, 1)
        seen = {agent["key"] for agent in flatten_groups(wire)}
        expected = connected_keys(worktrees, terminals, hooks)
        self.assertTrue(expected)
        self.assertEqual(seen, expected)
        self.assertTrue(all(":" in key and not key.startswith("orca:") for key in expected))

    def test_waiting_agent_needs_you(self) -> None:
        raw = load("synthetic-waiting.json")
        orca = board.parse_orca({"worktrees": raw["worktrees"], "terminals": raw["terminals"]}, {"version": 2, "entries": {}})
        wire = board.to_wire(board.build_board({}, orca, {}, NOW), {}, NOW, 1)
        agents = flatten_groups(wire)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["status"], "needs_you")
        self.assertEqual(agents[0]["status_source"], "orca")
        self.assertIn("Trust this folder?", agents[0]["reason"])

    def test_hookless_trust_prompt_is_pane_only_needs_you(self) -> None:
        raw = load("synthetic-trust.json")
        orca = board.parse_orca(
            {"worktrees": raw["worktrees"], "terminals": raw["terminals"]},
            {"version": 2, "entries": {}},
        )
        wire = board.to_wire(board.build_board({}, orca, {}, NOW), {}, NOW, 1)
        agents = flatten_groups(wire)
        self.assertEqual(len(agents), 1)
        agent = agents[0]
        self.assertEqual(agent["key"], "orca:tab-trust:leaf-trust")
        self.assertFalse(agent["editable"])
        self.assertEqual(agent["vendor"], "codex")
        self.assertEqual(agent["status"], "needs_you")
        self.assertEqual(agent["status_source"], "orca-screen")
        self.assertIn("Trust all and continue", agent["reason"])

    def test_bad_hook_version_does_not_join(self) -> None:
        raw = load("synthetic-waiting.json")
        hooks = load("synthetic-hook-v3.json")
        orca = board.parse_orca({"worktrees": raw["worktrees"], "terminals": raw["terminals"]}, hooks)
        self.assertTrue(any("v3 unsupported" in item for item in orca.problems))
        wire = board.to_wire(board.build_board({}, orca, {}, NOW), {}, NOW, 1)
        keys = {agent["key"] for agent in flatten_groups(wire)}
        self.assertEqual(keys, {"orca:tab-wait:leaf-wait"})
        self.assertNotIn("grok:should-not-join", keys)

    def test_saved_goal_without_session_stays_visible(self) -> None:
        key = board.SessionKey.parse("grok:not-on-disk")
        self.assertIsNotNone(key)
        assert key is not None
        finished = board.BoardGoal("finished work", (board.Todo("1", "ship", True),))
        built = board.build_board({}, board.Orca(True, {}, (), ()), {key: finished}, NOW)
        wire = board.to_wire(built, {}, NOW, 1)
        rows = [agent for agent in flatten_groups(wire) if agent["key"] == "grok:not-on-disk"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["missing"])
        self.assertEqual(rows[0]["goal"]["source"], "board")
        self.assertFalse(any(agent["key"] == "grok:not-on-disk" for agent in wire["history"]))

    def test_orca_cli_failure_keeps_disk_sessions(self) -> None:
        disk = FIXTURES / "disk" / "sessions"
        moment = datetime(2026, 11, 1, 0, 10, tzinfo=timezone.utc)
        with (
            patch("board.subprocess.Popen", side_effect=OSError("orca closed")),
            patch("board.claude_dirs", return_value=[]),
            patch("board.codex_homes", return_value=[]),
            patch("board.GROK_SESSIONS", disk),
            patch("board.HOOK_FILE", FIXTURES / "missing-hook.json"),
        ):
            wire = board.snapshot(moment)
        self.assertFalse(wire["orca"]["up"])
        rows = flatten_groups(wire) + wire["history"]
        match = [agent for agent in rows if agent["key"] == "grok:sid-disk-only"]
        self.assertEqual(len(match), 1)
        self.assertFalse(match[0]["missing"])
        self.assertEqual(match[0]["goal"]["text"], "disk session survives orca down")


class _OrcaProc:
    def __init__(self, out: str) -> None:
        self._out = out
        self.returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self._out, ""

    def kill(self) -> None:
        return None


class RunBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        board._ORCA_SEEN.clear()

    def test_user_lines_playbook_ladder_and_span(self) -> None:
        parsed = board.parse_session(FIXTURES / "claude-runs.jsonl")
        runs = parsed.runs
        self.assertEqual([run.request.text for run in runs], [
            "build a synthetic widget",
            "explain the blue paint",
            "read the bug playbook",
            "rename the header",
        ])
        self.assertEqual([ask.text for run in runs for ask in run.asks], ["Use blue paint."])
        self.assertEqual(
            [(run.playbook.playbook if run.playbook else None, run.playbook.evidence if run.playbook else None) for run in runs],
            [
                ("feature", board.Evidence.DECLARED),
                ("investigation", board.Evidence.COPIED),
                ("bug-fix", board.Evidence.READ),
                (None, None),
            ],
        )
        self.assertNotIn("opening-a-pr", [hit.playbook for run in runs for hit in run.playbooks])
        self.assertNotIn("refactoring", [hit.playbook for run in runs for hit in run.playbooks])
        self.assertEqual(len(runs[0].subagents), 1)
        self.assertEqual(runs[0].subagents[0].tool_use_id, "toolu_agent")
        self.assertEqual(runs[0].subagents[0].title, "Review the widget")
        self.assertGreater(runs[0].subagents[0].launched_at, runs[0].asks[0].at)
        self.assertLess(runs[0].subagents[0].launched_at, runs[1].started_at)
        self.assertEqual(runs[1].subagents, ())
        self.assertEqual(runs[0].until, runs[1].started_at)
        self.assertEqual([str(rid) for rid in runs[0].orca_runs], ["run_a1b2c3d4e5f6"])
        self.assertEqual(runs[0].prs[0].url, "https://example.com/me/widget/pull/1")
        views = board.view_runs(parsed, {}, None, NOW)
        delegate = views[0].delegates[0]
        self.assertEqual([item.kind for item in views[0].delegates], ["subagent"])
        self.assertEqual(delegate.state, board.DelegateState.DONE)
        self.assertEqual(delegate.source, "notification")
        self.assertEqual(delegate.result, "Widget review finished cleanly.")
        self.assertNotIn("bash finished", delegate.result)
        unknown = board.run_wire(views[3])
        self.assertEqual(unknown["roster"], [])
        self.assertIsNone(unknown["playbook"])
        self.assertEqual(unknown["result"]["headline"], "result not observed")
        self.assertIsNone(unknown["result"]["source"])

    def test_checklist_rule_round_trips(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            board.main(["--print-rule"])
        self.assertEqual(buf.getvalue(), board.CHECKLIST_RULE)
        found = board.parse_checklist(buf.getvalue(), board.load_registry(None), None)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.playbook, "feature")
        self.assertEqual(
            [mark for mark, _text in found.steps],
            [board.StepMark.DONE, board.StepMark.SKIPPED, board.StepMark.OPEN],
        )

    def test_failed_orca_roster_stays_unavailable(self) -> None:
        parsed = board.parse_session(FIXTURES / "claude-runs.jsonl")
        head = board.SessionHead(
            parsed.session,
            "/Users/me/widget",
            "Widget",
            NOW,
            board.Origin.YOU,
            None,
            None,
            FIXTURES / "claude-runs.jsonl",
        )
        with (
            patch("board.read_heads", return_value={head.key: head}),
            patch("board.subprocess.Popen", side_effect=OSError("orca closed")),
        ):
            payload = board.runs_payload(head.key, NOW)
        self.assertEqual([item["n"] for item in payload["runs"]], [4, 3, 2, 1])
        run = next(item for item in payload["runs"] if item["n"] == 1)
        self.assertEqual(run["roster"][0]["status"], "unavailable")
        self.assertTrue(run["roster"][0]["reason"])
        self.assertEqual(run["playbook"]["evidence"], "declared")
        self.assertEqual(run["playbook"]["slug"], "feature")
        self.assertEqual([item["kind"] for item in run["delegates"]], ["subagent"])
        self.assertEqual(run["delegates"][0]["result"]["text"], "Widget review finished cleanly.")
        self.assertEqual(run["delegates"][0]["result"]["source"], "notification")
        self.assertNotIn("bash finished", run["delegates"][0]["result"]["text"])
        self.assertTrue(run["state"])
        self.assertIn("Shipped the blue widget", run["result"]["headline"])
        self.assertEqual(run["result"]["source"], "reply")
        self.assertEqual(payload["runs"][0]["roster"], [])

    def test_empty_orca_answer_is_known_and_a_later_failure_is_memo(self) -> None:
        parsed = board.parse_session(FIXTURES / "claude-runs.jsonl")
        rid = parsed.orca_runs[0]
        calls: list[list[str]] = []

        def popen(cmd: list[str], **_kwargs: object) -> _OrcaProc:
            calls.append(cmd)
            if len(calls) > 2:
                raise OSError("orca closed")
            body = {"result": {"tasks": []}} if "task-list" in cmd else {"result": {"workers": []}}
            return _OrcaProc(json.dumps(body))

        with patch("board.subprocess.Popen", side_effect=popen):
            first, _problems = board.observe_orca_runs([rid], NOW)
            second, _problems = board.observe_orca_runs([rid], NOW)
        self.assertEqual(first[rid].status, board.RosterStatus.KNOWN)
        self.assertEqual(first[rid].view.tasks if first[rid].view else None, ())
        self.assertEqual(second[rid].status, board.RosterStatus.MEMO)
        self.assertIsNotNone(second[rid].observed_at)
        wire = board.run_wire(board.view_runs(parsed, second, None, NOW)[0])
        self.assertEqual(wire["roster"][0]["status"], "memo")
        self.assertEqual(wire["roster"][0]["tasks"], 0)
        self.assertIsNotNone(wire["roster"][0]["observed_at"])
        self.assertEqual([item["kind"] for item in wire["delegates"]], ["subagent"])


if __name__ == "__main__":
    unittest.main()
