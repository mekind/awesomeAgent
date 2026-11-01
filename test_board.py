#!/usr/bin/env python3
"""Behavior of the agent back office through build_board / to_wire and a failed Orca CLI."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
