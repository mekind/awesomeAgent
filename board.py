#!/usr/bin/env python3
"""Agent back office: one row per agent, live from Orca or on disk, triaged by what needs you.

A Claude row that ran /pstack:poteto-mode also carries one run per prompt.
"""

from __future__ import annotations

import argparse
import enum
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, NewType, get_args
from urllib.parse import parse_qs, urlparse

# Windows the status table reads. Nowhere else.
HOST, PORT = "127.0.0.1", 8765
ROOT = Path(__file__).resolve().parent
GOALS_PATH = ROOT / "data" / "goals.json"
PAGE_PATH = ROOT / "page.html"
HOME = Path.home()
ORCA_SUPPORT = HOME / "Library" / "Application Support" / "orca"
ORCA_BIN = "orca"
ORCA_TIMEOUT_S = 1.0
WORKING_WINDOW = timedelta(seconds=120)
DONE_WINDOW = timedelta(minutes=60)
HISTORY_LIMIT = 60
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 2 * 1024 * 1024

Vendor = Literal["claude", "codex", "grok"]


@dataclass(frozen=True, slots=True)
class SessionKey:
    vendor: Vendor
    sid: str

    def __str__(self) -> str:
        return f"{self.vendor}:{self.sid}"

    @classmethod
    def parse(cls, raw: str) -> SessionKey | None:
        if not isinstance(raw, str) or ":" not in raw:
            return None
        vendor, sid = raw.split(":", 1)
        if vendor not in VENDORS or not sid:
            return None
        return cls(vendor, sid)  # type: ignore[arg-type]


PaneKey = NewType("PaneKey", str)


@dataclass(frozen=True, slots=True)
class PaneOnly:
    pane: PaneKey

    def __str__(self) -> str:
        return f"orca:{self.pane}"


AgentKey = SessionKey | PaneOnly


class Status(enum.IntEnum):
    NEEDS_YOU = 0
    WORKING = 1
    DONE = 2
    IDLE = 3

    @property
    def wire(self) -> str:
        return self.name.lower()


class Presence(enum.StrEnum):
    ORCA = "orca"
    FILE = "file"
    GONE = "gone"


class Origin(enum.StrEnum):
    YOU = "you"
    SPAWNED = "spawned"


@dataclass(frozen=True, slots=True)
class Said:
    text: str
    at: datetime | None


@dataclass(frozen=True, slots=True)
class PlanStep:
    text: str
    done: bool


@dataclass(frozen=True, slots=True)
class SessionHead:
    key: SessionKey
    cwd: str
    title: str | None
    touched_at: datetime
    origin: Origin
    spawned_by: SessionKey | None
    blocked_on: str | None
    transcript: Path


@dataclass(frozen=True, slots=True)
class SessionDetail:
    last_prompt: Said | None
    last_reply: Said | None
    plan: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class VendorSource:
    color: str
    heads: Callable[[], Iterator[SessionHead]]
    detail: Callable[[SessionHead], SessionDetail]


HARNESS_BLOCK = re.compile(
    r"<(system-reminder|environment_context|user_instructions|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>",
    re.S,
)
USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
TEMP_ROOTS = ("/private/tmp/", "/tmp/", "/private/var/folders/")
# "Trust all and continue" is the Codex trust screen itself. The other
# alternatives are the surrounding chrome that screen also prints.
SCREEN_PROMPT = re.compile(
    r"Trust this folder\??|Trust all and continue|Do you trust[^\n]*|enter confirm|\(y/n\)|Allow [^\n]{0,60}\?|Press enter to continue",
    re.I,
)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def one_line(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except TypeError:
            value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def clean_said(text: str, at: datetime | None) -> Said | None:
    if not text:
        return None
    match = USER_QUERY.search(text)
    if match:
        text = match.group(1)
    text = HARNESS_BLOCK.sub("", text).strip()
    if not text or text.startswith("# AGENTS.md") or text.startswith("<permissions"):
        return None
    if len(text) > 4000:
        text = text[:3999] + "…"
    return Said(text, at)


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") in {"tool_result", "tool_use", "toolUse", "thinking"}:
            return ""
        return content_text(content.get("text") or content.get("content"))
    if isinstance(content, list):
        return "\n".join(part for part in (content_text(item) for item in content) if part)
    return ""


def classify_origin(cwd: str, spawned_hint: bool) -> tuple[Origin, None]:
    # Nesting is only Orca's parentPaneKey. A scratchpad path is not a parent link.
    spawned = spawned_hint or cwd.startswith(TEMP_ROOTS)
    return (Origin.SPAWNED if spawned else Origin.YOU), None


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def read_text_window(path: Path, nbytes: int, *, tail: bool) -> str:
    with path.open("rb") as fh:
        if tail:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            raw = fh.read()
            if size > nbytes:
                nl = raw.find(b"\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
        else:
            size = path.stat().st_size
            raw = fh.read(nbytes)
    text = raw.decode("utf-8", errors="replace")
    if size > nbytes:
        lines = text.splitlines()
        text = "\n".join(lines[:-1] if not tail else lines)
    return text


def iter_json_lines(text: str, *, reverse: bool = False) -> Iterator[Any]:
    lines = text.splitlines()
    if reverse:
        lines = list(reversed(lines))
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


_CACHE: dict[tuple[Any, ...], Any] = {}


def cached(path: Path, parse: Callable[[Path], Any], extra: str = "") -> Any:
    # One entry per path. A transcript is re-read every refresh, and keeping every stamp would grow without bound.
    try:
        st = path.stat()
    except OSError:
        return parse(path)
    identity = (str(path), getattr(parse, "__qualname__", ""), extra)
    key = (*identity, int(st.st_mtime_ns), st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    value = parse(path)
    for old in [item for item in _CACHE if item[:3] == identity and item != key]:
        del _CACHE[old]
    _CACHE[key] = value
    return value


def claude_dirs() -> list[Path]:
    dirs = [HOME / ".claude"]
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(Path(env))
    observed = ORCA_SUPPORT / "claude-observed-config-dirs.json"
    try:
        data = json.loads(observed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, list):
        dirs.extend(Path(item) for item in data if isinstance(item, str))
    return dedupe_paths(dirs)


def _claude_title(obj: Mapping[str, Any]) -> tuple[int, str | None]:
    kind = obj.get("type")
    if kind == "custom-title" and obj.get("customTitle"):
        return 3, str(obj["customTitle"]).strip()
    if kind == "ai-title" and obj.get("aiTitle"):
        return 2, str(obj["aiTitle"]).strip()
    if kind == "last-prompt" and obj.get("lastPrompt"):
        return 1, str(obj["lastPrompt"]).strip()
    return 0, None


def _claude_head(path: Path) -> SessionHead | None:
    sid = path.stem
    if not sid:
        return None
    try:
        text = read_text_window(path, HEAD_BYTES, tail=False)
        st = path.stat()
    except OSError:
        return None
    cwd = ""
    best_rank = 0
    title: str | None = None
    for obj in iter_json_lines(text):
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("cwd"), str) and obj["cwd"]:
            cwd = obj["cwd"]
        rank, label = _claude_title(obj)
        if rank and rank >= best_rank and label:
            best_rank = rank
            title = label
    origin, _parent = classify_origin(cwd, False)
    return SessionHead(
        SessionKey("claude", sid),
        cwd,
        title,
        datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        origin,
        None,
        None,
        path,
    )


def claude_heads() -> Iterator[SessionHead]:
    for root in claude_dirs():
        projects = root / "projects"
        if not projects.is_dir():
            continue
        for path in projects.glob("*/*.jsonl"):
            try:
                head = cached(path, _claude_head)
            except OSError:
                continue
            if isinstance(head, SessionHead):
                yield head


def _assistant_said(obj: Mapping[str, Any]) -> Said | None:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    return clean_said(content_text(message.get("content")), as_dt(obj.get("timestamp")))


def claude_detail(head: SessionHead) -> SessionDetail:
    last_prompt: Said | None = None
    last_reply: Said | None = None

    def take(obj: Any) -> None:
        nonlocal last_prompt, last_reply
        if not isinstance(obj, dict) or obj.get("isSidechain"):
            return
        if last_prompt is None and obj.get("type") == "last-prompt" and obj.get("lastPrompt"):
            last_prompt = clean_said(str(obj["lastPrompt"]), as_dt(obj.get("timestamp")))
        if last_reply is None and obj.get("type") == "assistant":
            said = _assistant_said(obj)
            if said:
                last_reply = said

    try:
        tail = read_text_window(head.transcript, TAIL_BYTES, tail=True)
    except OSError:
        tail = ""
    for obj in iter_json_lines(tail, reverse=True):
        take(obj)
        if last_prompt and last_reply:
            break
    if last_prompt is None or last_reply is None:
        try:
            head_text = read_text_window(head.transcript, HEAD_BYTES, tail=False)
        except OSError:
            head_text = ""
        for obj in iter_json_lines(head_text, reverse=True):
            take(obj)
            if last_prompt and last_reply:
                break
    return SessionDetail(last_prompt, last_reply, ())


def codex_homes() -> list[Path]:
    homes = [HOME / ".codex"]
    env = os.environ.get("CODEX_HOME")
    if env:
        homes.append(Path(env))
    accounts = ORCA_SUPPORT / "codex-accounts"
    if accounts.is_dir():
        homes.extend(path / "home" for path in accounts.iterdir() if (path / "home").is_dir())
    return dedupe_paths(homes)


def _codex_head(path: Path) -> SessionHead | None:
    sid = ""
    cwd = ""
    originator = ""
    title: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index >= 80:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if index == 0 and obj.get("type") == "session_meta":
                    sid = str(payload.get("id") or "")
                    cwd = str(payload.get("cwd") or "")
                    originator = str(payload.get("originator") or "")
                if title is None and payload.get("type") == "user_message":
                    said = clean_said(str(payload.get("message") or ""), as_dt(obj.get("timestamp")))
                    if said:
                        title = one_line(said.text, 180)
        st = path.stat()
    except OSError:
        return None
    if not sid:
        return None
    origin, _parent = classify_origin(cwd, originator == "codex_exec")
    return SessionHead(
        SessionKey("codex", sid),
        cwd,
        title,
        datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        origin,
        None,
        None,
        path,
    )


def codex_heads() -> Iterator[SessionHead]:
    for home in codex_homes():
        sessions = home / "sessions"
        if not sessions.is_dir():
            continue
        for path in sessions.rglob("rollout-*.jsonl"):
            try:
                head = cached(path, _codex_head)
            except OSError:
                continue
            if isinstance(head, SessionHead):
                yield head


def _codex_plan(obj: Mapping[str, Any]) -> tuple[PlanStep, ...] | None:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    if payload.get("name") != "update_plan" and obj.get("name") != "update_plan":
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    steps = args.get("plan")
    if not isinstance(steps, list):
        return None
    out: list[PlanStep] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        text = str(step.get("step") or step.get("content") or "").strip()
        if not text:
            continue
        status = str(step.get("status") or "")
        out.append(PlanStep(text, status in {"completed", "done"}))
    return tuple(out) if out else None


def codex_detail(head: SessionHead) -> SessionDetail:
    last_prompt: Said | None = None
    last_reply: Said | None = None
    plan: tuple[PlanStep, ...] = ()
    try:
        text = read_text_window(head.transcript, TAIL_BYTES, tail=True)
    except OSError:
        return SessionDetail(None, None, ())
    for obj in iter_json_lines(text, reverse=True):
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        kind = payload.get("type")
        if last_prompt is None and kind == "user_message":
            last_prompt = clean_said(str(payload.get("message") or ""), as_dt(obj.get("timestamp")))
        if last_reply is None and kind == "agent_message":
            last_reply = clean_said(str(payload.get("message") or ""), as_dt(obj.get("timestamp")))
        if not plan:
            found = _codex_plan(obj)
            if found:
                plan = found
        if last_prompt and last_reply and plan:
            break
    return SessionDetail(last_prompt, last_reply, plan)


GROK_SESSIONS = HOME / ".grok" / "sessions"


def _grok_head(path: Path) -> SessionHead | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    sid = str(info.get("id") or path.parent.name)
    if not sid:
        return None
    cwd = str(info.get("cwd") or "")
    title_raw = data.get("generated_title") or data.get("session_summary") or data.get("last_turn_summary")
    title = str(title_raw).strip() if isinstance(title_raw, str) and str(title_raw).strip() else None
    touched = as_dt(data.get("last_active_at"))
    if touched is None:
        try:
            touched = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
    blocked = None
    plan_path = path.parent / "plan_mode.json"
    try:
        mode = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mode = None
    if isinstance(mode, dict) and mode.get("awaiting_plan_approval"):
        blocked = "plan awaiting approval"
    origin, _parent = classify_origin(cwd, False)
    return SessionHead(
        SessionKey("grok", sid),
        cwd,
        title,
        touched,
        origin,
        None,
        blocked,
        path.parent / "chat_history.jsonl",
    )


def grok_heads() -> Iterator[SessionHead]:
    if not GROK_SESSIONS.is_dir():
        return
    for path in GROK_SESSIONS.rglob("summary.json"):
        plan_path = path.parent / "plan_mode.json"
        try:
            extra = str(plan_path.stat().st_mtime_ns) if plan_path.is_file() else ""
        except OSError:
            extra = ""
        try:
            head = cached(path, _grok_head, extra)
        except OSError:
            continue
        if isinstance(head, SessionHead):
            yield head


def grok_detail(head: SessionHead) -> SessionDetail:
    root = head.transcript.parent
    plan: tuple[PlanStep, ...] = ()
    chunk_prompt: Said | None = None
    updates = root / "updates.jsonl"
    try:
        text = read_text_window(updates, TAIL_BYTES, tail=True)
    except OSError:
        text = ""
    for obj in iter_json_lines(text, reverse=True):
        if not isinstance(obj, dict):
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if chunk_prompt is None and kind == "user_message_chunk":
            chunk_prompt = clean_said(content_text(update.get("content")), as_dt(obj.get("timestamp")))
        if not plan and kind == "plan" and isinstance(update.get("entries"), list):
            steps: list[PlanStep] = []
            for entry in update["entries"]:
                if not isinstance(entry, dict) or not entry.get("content"):
                    continue
                status = str(entry.get("status") or "")
                steps.append(PlanStep(str(entry["content"]), status in {"completed", "done"}))
            if steps:
                plan = tuple(steps)
        if chunk_prompt and plan:
            break
    hist_prompt: Said | None = None
    reply: Said | None = None
    try:
        history = read_text_window(head.transcript, TAIL_BYTES, tail=True)
    except OSError:
        history = ""
    for obj in iter_json_lines(history, reverse=True):
        if not isinstance(obj, dict):
            continue
        if hist_prompt is None and obj.get("type") == "user":
            hist_prompt = clean_said(content_text(obj.get("content")), as_dt(obj.get("timestamp")))
        if reply is None and obj.get("type") == "assistant":
            body = obj.get("content")
            if body is None and isinstance(obj.get("message"), dict):
                body = obj["message"].get("content")
            reply = clean_said(content_text(body), as_dt(obj.get("timestamp")))
        if hist_prompt and reply:
            break
    return SessionDetail(hist_prompt or chunk_prompt, reply, plan)


VENDORS: dict[Vendor, VendorSource] = {
    "claude": VendorSource("#f5b97a", claude_heads, claude_detail),
    "codex": VendorSource("#8ab4ff", codex_heads, codex_detail),
    "grok": VendorSource("#6ee7b7", grok_heads, grok_detail),
}
assert set(VENDORS) == set(get_args(Vendor)), "Vendor literal and VENDORS registry disagree"


def read_heads() -> dict[SessionKey, SessionHead]:
    found: dict[SessionKey, SessionHead] = {}
    for source in VENDORS.values():
        try:
            iterator = source.heads()
        except Exception:
            continue
        while True:
            try:
                head = next(iterator)
            except StopIteration:
                break
            except Exception:
                continue
            if isinstance(head, SessionHead):
                found[head.key] = head
    return found


# Hook file is the pane -> session id join only. Its payload.state stays stale after Orca quits.
ORCA_CALLS: dict[str, tuple[str, ...]] = {
    "worktrees": ("worktree", "ps"),
    "terminals": ("terminal", "list"),
}
HOOK_FILE = ORCA_SUPPORT / "agent-hooks" / "last-status.json"
HOOK_FILE_VERSION = 2


class PaneState(enum.StrEnum):
    WORKING = "working"
    WAITING = "waiting"
    DONE = "done"
    NO_HOOKS = "no-hooks"


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    name: str
    path: str
    status: str | None
    unread: bool


@dataclass(frozen=True, slots=True)
class Pane:
    key: PaneKey
    vendor: Vendor
    workspace: Workspace
    state: PaneState
    state_since: datetime | None
    blocked_on: str | None
    screen_ask: str | None
    task: str | None
    prompt: Said | None
    reply: Said | None
    tool: str | None
    working_mode: str | None
    parent: PaneKey | None
    session: SessionKey | None
    last_output_at: datetime | None
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class Orca:
    up: bool
    panes: Mapping[PaneKey, Pane]
    workspaces: tuple[Workspace, ...]
    problems: tuple[str, ...]

    @classmethod
    def down(cls, why: str) -> Orca:
        return cls(False, {}, (), (why,))


def screen_ask(preview: str) -> str:
    flat = re.sub(r"\s+", " ", preview).strip()
    matches = list(SCREEN_PROMPT.finditer(flat))
    if not matches:
        return flat[-100:]
    end = matches[-1].end()
    return flat[max(0, end - 100) : end].strip()


def unwrap_list(payload: Any, key: str) -> list[Any] | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get(key), list):
        return payload[key]
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get(key), list):
        return result[key]
    return None


def _hook_sessions(hooks: Any) -> dict[str, SessionKey]:
    entries = hooks.get("entries") if isinstance(hooks, dict) else None
    if not isinstance(entries, dict):
        return {}
    sessions: dict[str, SessionKey] = {}
    for raw_key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        presence = entry.get("agentPresence") if isinstance(entry.get("agentPresence"), dict) else {}
        provider = entry.get("providerSession") if isinstance(entry.get("providerSession"), dict) else {}
        agent = presence.get("agent")
        sid = provider.get("id")
        if agent not in VENDORS or not isinstance(sid, str) or not sid:
            continue
        pane_key = str(entry.get("paneKey") or raw_key)
        sessions[pane_key] = SessionKey(agent, sid)  # type: ignore[arg-type]
    return sessions


def parse_orca(
    raw: Mapping[str, Any | None],
    hooks: Any | None,
    extra_problems: tuple[str, ...] | list[str] = (),
) -> Orca:
    problems = list(extra_problems)
    worktrees = unwrap_list(raw.get("worktrees"), "worktrees")
    if worktrees is None:
        why = next((item for item in problems if item.startswith("worktree")), "worktree ps failed")
        return Orca.down(why)
    terminals = unwrap_list(raw.get("terminals"), "terminals")
    if terminals is None:
        if not any(item.startswith("terminal list") for item in problems):
            problems.append("terminal list failed")
        terminals = []
    sessions: dict[str, SessionKey] = {}
    if hooks is None:
        if not any("hook file" in item for item in problems):
            problems.append("hook file missing: live panes not joined to disk sessions")
    elif not isinstance(hooks, dict) or hooks.get("version") != HOOK_FILE_VERSION:
        version = hooks.get("version") if isinstance(hooks, dict) else "?"
        problems.append(f"hook file v{version} unsupported: live panes not joined to disk sessions")
    else:
        sessions = _hook_sessions(hooks)

    workspaces: dict[str, Workspace] = {}
    agents: dict[str, tuple[dict[str, Any], Workspace]] = {}
    for item in worktrees:
        if not isinstance(item, dict) or not item.get("worktreeId"):
            continue
        path = str(item.get("path") or "")
        name = str(item.get("displayName") or Path(path).name or item["worktreeId"])
        status = item.get("workspaceStatus")
        ws = Workspace(
            str(item["worktreeId"]),
            name,
            path,
            str(status) if isinstance(status, str) else None,
            bool(item.get("unread")),
        )
        workspaces[ws.id] = ws
        for agent in item.get("agents") or []:
            if isinstance(agent, dict) and agent.get("paneKey"):
                agents[str(agent["paneKey"])] = (agent, ws)

    terminals_by_key: dict[str, dict[str, Any]] = {}
    for item in terminals:
        if not isinstance(item, dict) or item.get("agentIdentity") not in VENDORS:
            continue
        if not item.get("tabId") or not item.get("leafId"):
            continue
        terminals_by_key[f"{item['tabId']}:{item['leafId']}"] = item

    panes: dict[PaneKey, Pane] = {}
    for key in set(agents) | set(terminals_by_key):
        agent, agent_ws = agents.get(key, (None, None))
        terminal = terminals_by_key.get(key)
        if terminal is not None:
            connected = bool(terminal.get("connected")) and not bool(terminal.get("orphaned"))
        else:
            connected = bool(agent and agent.get("hasLivePty"))
        if not connected or agent is None and terminal is None:
            continue
        vendor: Vendor | None = None
        if agent and agent.get("agentType") in VENDORS:
            vendor = agent["agentType"]
        elif terminal and terminal.get("agentIdentity") in VENDORS:
            vendor = terminal["agentIdentity"]
        if vendor is None:
            continue
        raw_state = agent.get("state") if agent else None
        state = PaneState(raw_state) if raw_state in {"working", "waiting", "done"} else PaneState.NO_HOOKS
        program = agent.get("programStatus") if agent and isinstance(agent.get("programStatus"), dict) else {}
        blocked_on = None
        if agent and (agent.get("state") == "waiting" or program.get("state") == "blocked"):
            message = program.get("message") if isinstance(program.get("message"), str) else ""
            blocked_on = message.strip() or one_line(agent.get("toolInput"), 160) or "waiting for you"
        ask = None
        preview = terminal.get("preview") if terminal and isinstance(terminal.get("preview"), str) else ""
        if state is PaneState.NO_HOOKS and preview and SCREEN_PROMPT.search(preview):
            ask = screen_ask(preview)
        tool = None
        if agent and agent.get("toolName") and state is PaneState.WORKING:
            tool_input = one_line(agent.get("toolInput"), 80)
            tool = f"{agent['toolName']}: {tool_input}" if tool_input else str(agent["toolName"])
        workspace = agent_ws
        if workspace is None and terminal is not None:
            wid = str(terminal.get("worktreeId") or key)
            path = str(terminal.get("worktreePath") or "")
            workspace = workspaces.get(wid) or Workspace(wid, Path(path).name or "orca", path, None, False)
            workspaces[workspace.id] = workspace
        if workspace is None:
            workspace = Workspace(key, "orca", "", None, False)
        parent_raw = agent.get("parentPaneKey") if agent else None
        task = agent.get("taskTitle") if agent and isinstance(agent.get("taskTitle"), str) else None
        task = task.strip() if isinstance(task, str) and task.strip() else None
        prompt = clean_said(str(agent.get("prompt")), as_dt(agent.get("updatedAt"))) if agent and agent.get("prompt") else None
        reply = (
            clean_said(str(agent.get("lastAssistantMessage")), as_dt(agent.get("updatedAt")))
            if agent and agent.get("lastAssistantMessage")
            else None
        )
        last_output = as_dt(terminal.get("lastOutputAt")) if terminal else None
        if last_output is None and agent:
            last_output = as_dt(agent.get("updatedAt"))
        mode = agent.get("workingMode") if agent and isinstance(agent.get("workingMode"), str) else None
        handle = terminal.get("handle") if terminal and isinstance(terminal.get("handle"), str) else None
        panes[PaneKey(key)] = Pane(
            PaneKey(key),
            vendor,
            workspace,
            state,
            as_dt(agent.get("stateStartedAt")) if agent else None,
            blocked_on,
            ask,
            task,
            prompt,
            reply,
            tool,
            mode,
            PaneKey(str(parent_raw)) if parent_raw else None,
            sessions.get(key),
            last_output,
            handle,
        )
    return Orca(True, panes, tuple(workspaces.values()), tuple(problems))


def _read_hook_file() -> tuple[Any | None, str | None]:
    try:
        data = json.loads(HOOK_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "hook file missing: live panes not joined to disk sessions"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"hook file unreadable: {exc}"
    return data, None


def observe_orca() -> Orca:
    raw, problems = orca_json(ORCA_CALLS)
    hooks, hook_problem = _read_hook_file()
    if hook_problem:
        problems.append(hook_problem)
        hooks = None
    return parse_orca(
        {"worktrees": raw.get("worktrees"), "terminals": raw.get("terminals")},
        hooks,
        problems,
    )


@dataclass(frozen=True, slots=True)
class Todo:
    id: str
    text: str
    done: bool


@dataclass(frozen=True, slots=True)
class BoardGoal:
    text: str
    todos: tuple[Todo, ...]

    @property
    def open(self) -> bool:
        return bool(self.text) and not (self.todos and all(item.done for item in self.todos))


GOALS_LOCK = threading.Lock()


def _read_goals_raw() -> dict[str, Any]:
    try:
        data = json.loads(GOALS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"goals.json unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("goals.json is not an object")
    return data


def _todos_from(raw: Any) -> tuple[Todo, ...]:
    if not isinstance(raw, list):
        return ()
    todos: list[Todo] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        todos.append(Todo(str(item.get("id") or uuid.uuid4()), text, bool(item.get("done"))))
    return tuple(todos)


def load_goals() -> dict[SessionKey, BoardGoal]:
    try:
        raw = _read_goals_raw()
    except ValueError:
        return {}
    goals: dict[SessionKey, BoardGoal] = {}
    for key, entry in raw.items():
        parsed = SessionKey.parse(str(key))
        if parsed is None or not isinstance(entry, dict):
            continue
        text = entry.get("text") if isinstance(entry.get("text"), str) else ""
        goals[parsed] = BoardGoal(text, _todos_from(entry.get("todos")))
    return goals


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_goal(
    key: SessionKey,
    *,
    text: str | None = None,
    todos: list[Todo] | None = None,
) -> BoardGoal:
    # ThreadingHTTPServer runs POSTs on separate threads. The lock covers the whole read-modify-save.
    with GOALS_LOCK:
        raw = _read_goals_raw()
        entry = raw.get(str(key))
        if not isinstance(entry, dict):
            entry = {}
        if text is not None:
            entry["text"] = text
        if todos is not None:
            entry["todos"] = [{"id": item.id, "text": item.text, "done": item.done} for item in todos]
        else:
            entry.setdefault("todos", entry.get("todos") if isinstance(entry.get("todos"), list) else [])
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        raw[str(key)] = entry
        _atomic_write(GOALS_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        stored = entry.get("text") if isinstance(entry.get("text"), str) else ""
        return BoardGoal(stored, _todos_from(entry.get("todos")))


GoalSource = Literal["board", "orca-task", "vendor-title", "prompt"]
ProgressSource = Literal["board", "plan", "checklist", "delegates", "workers"]
StatusSource = Literal["orca", "orca-screen", "disk", "file", "none"]


@dataclass(frozen=True, slots=True)
class Goal:
    text: str
    source: GoalSource


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int
    source: ProgressSource


@dataclass(frozen=True, slots=True)
class StatusEvidence:
    pane: Pane | None
    head: SessionHead | None
    unread: bool
    now: datetime

    @property
    def age(self) -> timedelta | None:
        if self.head is None:
            return None
        return self.now - self.head.touched_at


def rule_pane_blocked(evidence: StatusEvidence) -> str | None:
    return evidence.pane.blocked_on if evidence.pane else None


def rule_screen_ask(evidence: StatusEvidence) -> str | None:
    return evidence.pane.screen_ask if evidence.pane else None


def rule_disk_blocked(evidence: StatusEvidence) -> str | None:
    if evidence.head is None or not evidence.head.blocked_on:
        return None
    alive = evidence.pane is not None or (evidence.age is not None and evidence.age <= DONE_WINDOW)
    if not alive:
        return None
    return evidence.head.blocked_on


def rule_pane_working(evidence: StatusEvidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.WORKING:
        return None
    if pane.tool:
        return pane.tool
    if pane.working_mode == "monitoring":
        return "monitoring"
    return "working"


def rule_screen_output(evidence: StatusEvidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.NO_HOOKS or pane.last_output_at is None:
        return None
    age = evidence.now - pane.last_output_at
    if age > WORKING_WINDOW:
        return None
    return f"output {max(0, int(age.total_seconds()))}s ago (no hooks)"


def rule_file_working(evidence: StatusEvidence) -> str | None:
    if evidence.pane is not None or evidence.head is None or evidence.age is None:
        return None
    if evidence.age > WORKING_WINDOW:
        return None
    return "writing (no Orca pane)"


def rule_pane_done(evidence: StatusEvidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.DONE:
        return None
    recent = pane.state_since is not None and (evidence.now - pane.state_since) <= DONE_WINDOW
    if not (evidence.unread or recent):
        return None
    if pane.reply:
        return one_line(pane.reply.text, 160)
    return "finished"


def rule_file_done(evidence: StatusEvidence) -> str | None:
    if evidence.pane is not None or evidence.head is None or evidence.head.origin is not Origin.YOU:
        return None
    if evidence.age is None or evidence.age > DONE_WINDOW:
        return None
    mins = max(0, int(evidence.age.total_seconds() // 60))
    return f"finished {mins}m ago (no Orca pane)"


def rule_idle(_evidence: StatusEvidence) -> str | None:
    return "idle"


STATUS_RULES: tuple[tuple[Status, StatusSource, Callable[[StatusEvidence], str | None]], ...] = (
    (Status.NEEDS_YOU, "orca", rule_pane_blocked),
    (Status.NEEDS_YOU, "orca-screen", rule_screen_ask),
    (Status.NEEDS_YOU, "disk", rule_disk_blocked),
    (Status.WORKING, "orca", rule_pane_working),
    (Status.WORKING, "orca-screen", rule_screen_output),
    (Status.WORKING, "file", rule_file_working),
    (Status.DONE, "orca", rule_pane_done),
    (Status.DONE, "file", rule_file_done),
    (Status.IDLE, "none", rule_idle),
)


def status_of(evidence: StatusEvidence) -> tuple[Status, StatusSource, str]:
    for status, source, rule in STATUS_RULES:
        reason = rule(evidence)
        if reason:
            return status, source, reason
    return Status.IDLE, "none", "idle"


def presence_of(evidence: StatusEvidence) -> Presence:
    if evidence.pane is not None:
        return Presence.ORCA
    if evidence.head is not None and evidence.age is not None and evidence.age <= DONE_WINDOW:
        return Presence.FILE
    return Presence.GONE


@dataclass(frozen=True, slots=True)
class Agent:
    key: AgentKey
    vendor: Vendor
    workspace: str
    workspace_path: str
    orca_status: str | None
    status: Status
    status_source: StatusSource
    reason: str
    since: datetime | None
    presence: Presence
    unread: bool
    last_activity: datetime | None
    pane: Pane | None
    head: SessionHead | None
    board_goal: BoardGoal | None
    missing: bool


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    path: str
    orca_status: str | None
    status: Status
    roots: tuple[AgentKey, ...]


@dataclass(frozen=True, slots=True)
class Board:
    agents: Mapping[AgentKey, Agent]
    children: Mapping[AgentKey, tuple[AgentKey, ...]]
    groups: tuple[Group, ...]
    history: tuple[AgentKey, ...]
    hidden_spawned: int
    orca: Orca

    def main_view(self) -> Iterator[Agent]:
        seen: set[AgentKey] = set()

        def walk(key: AgentKey) -> Iterator[Agent]:
            if key in seen or key not in self.agents:
                return
            seen.add(key)
            yield self.agents[key]
            for child in self.children.get(key, ()):
                yield from walk(child)

        for group in self.groups:
            for root in group.roots:
                yield from walk(root)


def _longest_workspace(workspaces: tuple[Workspace, ...], cwd: str) -> Workspace | None:
    best: Workspace | None = None
    best_len = -1
    target = cwd.rstrip("/")
    if not target:
        return None
    for workspace in workspaces:
        path = workspace.path.rstrip("/")
        if not path:
            continue
        if target == path or target.startswith(path + "/"):
            if len(path) > best_len:
                best = workspace
                best_len = len(path)
    return best


def _place(
    head: SessionHead | None,
    pane: Pane | None,
    orca: Orca,
    missing: bool,
) -> tuple[str, str, str | None]:
    if pane is not None:
        return pane.workspace.name, pane.workspace.path, pane.workspace.status
    if missing or head is None:
        return "saved goals", "", None
    match = _longest_workspace(orca.workspaces, head.cwd)
    if match is not None:
        return match.name, match.path, match.status
    cwd = head.cwd
    if cwd == str(HOME):
        return "~", cwd, None
    name = Path(cwd).name if cwd else "unknown"
    return name or "unknown", cwd or name, None


def _stamp(agent: Agent) -> float:
    if agent.last_activity is None:
        return 0.0
    return agent.last_activity.timestamp()


def build_board(
    heads: Mapping[SessionKey, SessionHead],
    orca: Orca,
    goals: Mapping[SessionKey, BoardGoal],
    now: datetime,
) -> Board:
    pane_by_key: dict[AgentKey, Pane] = {}
    pane_to_agent: dict[PaneKey, AgentKey] = {}
    rank = {PaneState.WAITING: 0, PaneState.WORKING: 1, PaneState.NO_HOOKS: 2, PaneState.DONE: 3}
    for pane in orca.panes.values():
        key: AgentKey = pane.session if pane.session is not None else PaneOnly(pane.key)
        current = pane_by_key.get(key)
        if current is None or rank[pane.state] < rank[current.state]:
            pane_by_key[key] = pane
        pane_to_agent[pane.key] = key

    def make(key: AgentKey, head: SessionHead | None, pane: Pane | None, missing: bool) -> Agent:
        goal = goals.get(key) if isinstance(key, SessionKey) else None
        evidence = StatusEvidence(pane, head, bool(pane and pane.workspace.unread), now)
        status, source, reason = status_of(evidence)
        presence = presence_of(evidence)
        name, path, orca_status = _place(head, pane, orca, missing)
        since = None
        if status is not Status.IDLE:
            if pane is not None and source in {"orca", "orca-screen"}:
                since = pane.state_since or pane.last_output_at
            elif head is not None:
                since = head.touched_at
        times = [
            item
            for item in (
                pane.last_output_at if pane else None,
                pane.state_since if pane else None,
                head.touched_at if head else None,
            )
            if item is not None
        ]
        vendor = key.vendor if isinstance(key, SessionKey) else pane.vendor  # type: ignore[union-attr]
        return Agent(
            key,
            vendor,
            name,
            path,
            orca_status,
            status,
            source,
            reason,
            since,
            presence,
            bool(pane and pane.workspace.unread),
            max(times) if times else None,
            pane,
            head,
            goal,
            missing,
        )

    agents: dict[AgentKey, Agent] = {}
    for key, head in heads.items():
        agents[key] = make(key, head, pane_by_key.get(key), False)
    for key, pane in pane_by_key.items():
        if key not in agents:
            agents[key] = make(key, None, pane, False)
    for key, _goal in goals.items():
        if key not in agents:
            agents[key] = make(key, None, None, True)

    raw_parent: dict[AgentKey, AgentKey] = {}
    for key, agent in agents.items():
        if agent.pane is None or agent.pane.parent is None:
            continue
        parent = pane_to_agent.get(agent.pane.parent)
        if parent is not None and parent != key and parent in agents:
            raw_parent[key] = parent

    def parent_chain_ok(key: AgentKey) -> bool:
        seen: set[AgentKey] = set()
        parent = raw_parent.get(key)
        while parent is not None:
            if parent in seen or parent == key:
                return False
            seen.add(parent)
            parent = raw_parent.get(parent)
        return True

    parent_of = {key: parent for key, parent in raw_parent.items() if parent_chain_ok(key)}
    children_lists: dict[AgentKey, list[AgentKey]] = defaultdict(list)
    for key, parent in parent_of.items():
        children_lists[parent].append(key)
    children: dict[AgentKey, tuple[AgentKey, ...]] = {}
    for key, kids in children_lists.items():
        kids.sort(key=lambda item: (agents[item].status, -_stamp(agents[item])))
        children[key] = tuple(kids)

    child_keys = {key for kids in children.values() for key in kids}
    roots = [key for key in agents if key not in child_keys]

    def tree_nodes(root: AgentKey) -> list[AgentKey]:
        nodes: list[AgentKey] = []
        stack = [root]
        seen: set[AgentKey] = set()
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            nodes.append(key)
            stack.extend(children.get(key, ()))
        return nodes

    def tree_visible(nodes: list[AgentKey]) -> bool:
        for key in nodes:
            agent = agents[key]
            if agent.missing or agent.presence is not Presence.GONE or agent.status < Status.IDLE:
                return True
            if agent.board_goal is not None and agent.board_goal.open:
                return True
        return False

    main_roots: list[AgentKey] = []
    history_nodes: list[AgentKey] = []
    hidden = 0
    for root in roots:
        nodes = tree_nodes(root)
        if tree_visible(nodes):
            main_roots.append(root)
            continue
        root_agent = agents[root]
        spawned = root_agent.head is not None and root_agent.head.origin is Origin.SPAWNED and root_agent.pane is None
        if spawned:
            hidden += 1
            continue
        history_nodes.extend(nodes)

    history_nodes.sort(key=lambda key: _stamp(agents[key]), reverse=True)
    history = tuple(history_nodes[:HISTORY_LIMIT])

    def subtree_status(key: AgentKey) -> Status:
        best = agents[key].status
        for child in children.get(key, ()):
            best = min(best, subtree_status(child))
        return best

    grouped: dict[str, list[AgentKey]] = defaultdict(list)
    for root in main_roots:
        grouped[agents[root].workspace_path].append(root)
    groups: list[Group] = []
    for path, group_roots in grouped.items():
        group_roots.sort(key=lambda key: (subtree_status(key), -_stamp(agents[key])))
        lead = min(group_roots, key=lambda key: (subtree_status(key), -_stamp(agents[key])))
        groups.append(
            Group(
                agents[lead].workspace,
                path,
                agents[lead].orca_status,
                min(subtree_status(key) for key in group_roots),
                tuple(group_roots),
            )
        )
    groups.sort(key=lambda group: (group.status, -max(_stamp(agents[key]) for key in group.roots)))
    return Board(agents, children, tuple(groups), history, hidden, orca)


def goal_of(agent: Agent, detail: SessionDetail | None) -> Goal | None:
    if agent.board_goal is not None and agent.board_goal.text.strip():
        return Goal(agent.board_goal.text.strip(), "board")
    if agent.pane is not None and agent.pane.task:
        return Goal(agent.pane.task, "orca-task")
    if agent.head is not None and agent.head.title:
        return Goal(agent.head.title, "vendor-title")
    said = None
    if detail is not None and detail.last_prompt is not None:
        said = detail.last_prompt
    elif agent.pane is not None and agent.pane.prompt is not None:
        said = agent.pane.prompt
    if said is not None and said.text.strip():
        return Goal(one_line(said.text, 240), "prompt")
    return None


def progress_of(
    agent: Agent,
    detail: SessionDetail | None,
    children: list[Agent],
    run: RunView | None = None,
) -> Progress | None:
    if agent.board_goal is not None and agent.board_goal.todos:
        done = sum(1 for item in agent.board_goal.todos if item.done)
        return Progress(done, len(agent.board_goal.todos), "board")
    if detail is not None and detail.plan:
        done = sum(1 for step in detail.plan if step.done)
        return Progress(done, len(detail.plan), "plan")
    if run is not None and run.progress is not None:
        return run.progress
    if children:
        done = sum(1 for child in children if child.status in {Status.DONE, Status.IDLE})
        return Progress(done, len(children), "workers")
    return None


# One poteto prompt is one Run. Parse is a pure fold of the transcript. Join adds live delegates.

PSTACK_ROOT = HOME / ".claude" / "plugins" / "cache" / "cursor-plugins" / "pstack"
STALE_WINDOW = timedelta(minutes=20)  # Bash can stay silent for 10 min, and `check --wait` is 15.
RESUME_SLACK = timedelta(seconds=30)

RunId = NewType("RunId", str)
OrcaRunId = NewType("OrcaRunId", str)


class UserLine(enum.Enum):
    POTETO = "poteto"
    PROMPT = "prompt"
    NUDGE = "nudge"
    NOTE = "note"
    INTERRUPT = "interrupt"
    NOISE = "noise"


# Orca types its nudge as a human prompt, so text rules cover the shapes the structure flags miss.
STRUCTURAL_NOISE = ("toolUseResult", "isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
USER_LINE_RULES: tuple[tuple[UserLine, re.Pattern[str]], ...] = (
    (UserLine.POTETO, re.compile(r"<command-name>/pstack:poteto-mode</command-name>")),
    (UserLine.NOTE, re.compile(r"\A\s*<task-notification>")),
    (UserLine.NUDGE, re.compile(r"\AYou have \d+ orchestration messages?\.")),
    (UserLine.INTERRUPT, re.compile(r"\A\[Request interrupted by user")),
    (UserLine.NOISE, re.compile(r"\A\s*(\[SYSTEM NOTIFICATION|<system-reminder>|<local-command-)")),
)
ORIGIN_KINDS = {"task-notification": UserLine.NOTE, "peer": UserLine.NOISE}

COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SKILL_BASE = re.compile(r"/pstack/([^/\s]+)/skills/poteto-mode\b")
ORCA_RUN = re.compile(r"\brun_[0-9a-f]{12}\b")
PLAYBOOK_PATH = re.compile(r"poteto-mode/playbooks/([a-z0-9-]+)\.md")
NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
ORCA_WAIT = re.compile(r"orca orchestration check\b[^\n]*--wait")
RECAP_SUFFIX = re.compile(r"\s*\(disable recaps in /config\)\s*$")
WAIT_TOOLS = frozenset({"TaskOutput", "BashOutput", "Monitor"})
LAUNCH_TOOLS = frozenset({"Agent", "Task"})


def classify_user(obj: Mapping[str, Any]) -> tuple[UserLine, str]:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    text = content_text(message.get("content")).strip()
    origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
    by_origin = ORIGIN_KINDS.get(str(origin.get("kind") or ""))
    if by_origin is not None:
        return by_origin, text
    if not text or any(obj.get(flag) not in (None, False) for flag in STRUCTURAL_NOISE):
        return UserLine.NOISE, ""
    for kind, pattern in USER_LINE_RULES:
        if pattern.search(text):
            return kind, text
    return UserLine.PROMPT, text


class Evidence(enum.IntEnum):
    """Lower is stronger. The page shows the name."""

    DECLARED = 0
    COPIED = 1
    READ = 2
    NAMED = 3

    @property
    def wire(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class Playbook:
    slug: str
    title: str
    steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlaybookRegistry:
    version: str
    playbooks: Mapping[str, Playbook]
    signatures: Mapping[str, tuple[str, int]]
    titles: Mapping[str, str]
    named: re.Pattern[str]


def normalize(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text.lower())
    return " ".join(re.sub(r"[^\w:/-]+", " ", text).split())


COPIED_WEIGHT = 2


def _step_signatures(step: str) -> dict[str, int]:
    # Opening words weigh 1. Short step openers are also principle names agents cite in prose.
    # A long literal code span weighs 2. Nobody types that unless they copied the step.
    sigs: dict[str, int] = {}
    words = normalize(re.split(r"(?<=[.!?])\s", step, maxsplit=1)[0]).split()[:6]
    if len(words) >= 3 and len(" ".join(words)) >= 18:
        sigs[" ".join(words)] = 1
    for span in re.findall(r"`([^`]{24,})`", step):
        sigs[normalize(span)] = COPIED_WEIGHT
    return sigs


def installed_versions() -> list[str]:
    def key(name: str) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", name))

    if not PSTACK_ROOT.is_dir():
        return []
    return sorted((p.name for p in PSTACK_ROOT.iterdir() if (p / "skills/poteto-mode/playbooks").is_dir()), key=key)


@lru_cache(maxsize=8)
def load_registry(version: str | None) -> PlaybookRegistry | None:
    versions = installed_versions()
    if not versions:
        return None
    chosen = version if version in versions else versions[-1]
    folder = PSTACK_ROOT / chosen / "skills" / "poteto-mode" / "playbooks"
    playbooks: dict[str, Playbook] = {}
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        title = next((line[4:].strip() for line in text.splitlines() if line.startswith("### ")), path.stem)
        steps = tuple(m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", text, re.M))
        playbooks[path.stem] = Playbook(path.stem, title, steps)
    owners: dict[str, set[str]] = {}
    weights: dict[str, int] = {}
    for book in playbooks.values():
        for step in book.steps:
            for sig, weight in _step_signatures(step).items():
                owners.setdefault(sig, set()).add(book.slug)
                weights[sig] = max(weights.get(sig, 0), weight)
    unique = {sig: (next(iter(slugs)), weights[sig]) for sig, slugs in owners.items() if len(slugs) == 1}
    titles = {book.title.casefold(): book.slug for book in playbooks.values()}
    alternation = "|".join(re.escape(book.title) for book in sorted(playbooks.values(), key=lambda b: -len(b.title)))
    named = re.compile(rf"\b({alternation})\s+playbook\b", re.I) if alternation else re.compile(r"(?!)")
    return PlaybookRegistry(chosen, playbooks, unique, titles, named)


CHECKLIST_RULE = """\
# Playbook checklist for the session board

Applies only while /pstack:poteto-mode is running, on the adapter's path that keeps the playbook
todolist in your reply (no TodoWrite). Write the list in exactly this form. Post it again each time
a step is done or skipped, and post a new one when the run moves to another playbook.

Playbook: Feature
- [x] 1. `how` over the affected subsystem.
- [-] 2. `architect` for parallel design exploration. skip: one-file change
- [ ] 3. Write the throughput checkpoint as four todo items.

The title is the playbook's name as poteto-mode's Playbooks list writes it. Each step is the
playbook's numbered step, first sentence, verbatim. `[x]` done, `[-]` skipped, `[ ]` open.
"""

CHECKLIST_HEAD = re.compile(r"^[ \t]*\**Playbook:\**[ \t]*(?P<title>[^\n*]+?)[ \t]*\**[ \t]*$", re.M)
CHECKLIST_STEP = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX-])\]\s*(?:\d+\.\s*)?(?P<text>\S.*?)\s*$")


class StepMark(enum.StrEnum):
    OPEN = " "
    DONE = "x"
    SKIPPED = "-"


@dataclass(frozen=True, slots=True)
class Checklist:
    playbook: str | None
    title: str
    steps: tuple[tuple[StepMark, str], ...]
    at: datetime | None


def parse_checklist(text: str, registry: PlaybookRegistry | None, at: datetime | None) -> Checklist | None:
    head = CHECKLIST_HEAD.search(text)
    if head is None:
        return None
    steps: list[tuple[StepMark, str]] = []
    for line in text[head.end() :].splitlines()[1:]:
        if not line.strip():
            if steps:
                break
            continue
        match = CHECKLIST_STEP.match(line)
        if match is None:
            break
        raw = match.group("mark").lower()
        body = match.group("text")
        mark = StepMark.SKIPPED if raw == "-" or "skip:" in body else StepMark(raw)
        steps.append((mark, body))
    if not steps:
        return None
    title = head.group("title").strip()
    slug = registry.titles.get(title.casefold()) if registry else None
    return Checklist(slug, title, tuple(steps), at)


class Turn(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"
    API_ERROR = "api-error"


class PendingKind(enum.Enum):
    ASK = "ask"
    WAIT = "wait"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class PendingTool:
    kind: PendingKind
    detail: str
    at: datetime | None


@dataclass(frozen=True, slots=True)
class PlaybookHit:
    playbook: str
    title: str
    evidence: Evidence
    at: datetime | None
    detail: str


@dataclass(frozen=True, slots=True)
class TaskNote:
    task_id: str
    tool_use_id: str | None
    status: str
    result: str
    at: datetime | None


@dataclass(frozen=True, slots=True)
class SubagentFacts:
    tool_use_id: str
    agent_id: str | None
    title: str
    agent_type: str
    model: str | None
    effort: str | None
    launched_at: datetime | None
    note: TaskNote | None
    handback: Said | None


@dataclass(frozen=True, slots=True)
class PrLink:
    number: int
    url: str
    repo: str
    at: datetime | None


@dataclass(frozen=True, slots=True)
class Run:
    """Transcript facts for one poteto prompt. The window runs to the next poteto prompt."""

    id: RunId
    session: SessionKey
    n: int
    request: Said
    started_at: datetime
    until: datetime | None
    asks: tuple[Said, ...]
    playbooks: tuple[PlaybookHit, ...]
    checklist: Checklist | None
    subagents: tuple[SubagentFacts, ...]
    orca_runs: tuple[OrcaRunId, ...]
    prs: tuple[PrLink, ...]
    last_reply: Said | None
    recap: Said | None
    error: Said | None
    turn: Turn
    pending: PendingTool | None
    last_event_at: datetime

    @property
    def playbook(self) -> PlaybookHit | None:
        if not self.playbooks:
            return None
        return min(self.playbooks, key=lambda hit: (hit.evidence, hit.at or self.started_at))


@dataclass(frozen=True, slots=True)
class SessionRuns:
    session: SessionKey
    transcript: Path
    runs: tuple[Run, ...]
    orca_runs: tuple[OrcaRunId, ...]

    @property
    def subagents_dir(self) -> Path:
        return self.transcript.with_suffix("") / "subagents"


@dataclass(slots=True)
class _RunFold:
    n: int
    request: Said
    started_at: datetime
    version: str | None = None
    until: datetime | None = None
    asks: list[Said] = field(default_factory=list)
    reads: list[tuple[datetime | None, str, str]] = field(default_factory=list)
    texts: list[tuple[datetime | None, str]] = field(default_factory=list)
    launches: dict[str, SubagentFacts] = field(default_factory=dict)
    orca_runs: list[OrcaRunId] = field(default_factory=list)
    prs: list[PrLink] = field(default_factory=list)
    recap: Said | None = None
    error: Said | None = None
    turn: Turn = Turn.OPEN
    pending: dict[str, PendingTool] = field(default_factory=dict)
    last_event_at: datetime | None = None

    def touch(self, at: datetime | None) -> None:
        if at is not None:
            self.last_event_at = at

    def wake(self, at: datetime | None) -> None:
        self.pending.clear()
        self.error = None
        self.turn = Turn.OPEN
        self.touch(at)


def _tag(body: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
    return html.unescape(match.group(1).strip()) if match else ""


def parse_notes(text: str, at: datetime | None) -> Iterator[TaskNote]:
    for block in NOTIFICATION.finditer(text):
        body = block.group(1)
        task_id = _tag(body, "task-id")
        if task_id:
            yield TaskNote(task_id, _tag(body, "tool-use-id") or None, _tag(body, "status"), _tag(body, "result"), at)


def _pending_of(name: str, payload: Mapping[str, Any], at: datetime | None) -> PendingTool:
    if name == "AskUserQuestion":
        questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
        first = questions[0].get("question") if questions and isinstance(questions[0], dict) else ""
        return PendingTool(PendingKind.ASK, one_line(first or "question", 160), at)
    command = str(payload.get("command") or "")
    if name in WAIT_TOOLS or (name == "Bash" and ORCA_WAIT.search(command)):
        return PendingTool(PendingKind.WAIT, one_line(command or name, 120), at)
    summary = one_line(payload.get("description") or command or payload.get("file_path") or "", 100)
    return PendingTool(PendingKind.TOOL, f"{name}: {summary}" if summary else name, at)


def _playbook_reads(name: str, payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    if name == "Read":
        source = str(payload.get("file_path") or "")
    elif name == "Bash":
        source = str(payload.get("command") or "")
        if "<<" in source or "worker-start" in source:
            return []
    else:
        return []
    return [(m.group(1), one_line(source, 120)) for m in PLAYBOOK_PATH.finditer(source)]


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                parts.append(json.dumps(part.get("content"), ensure_ascii=False))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def parse_session(path: Path) -> SessionRuns:
    session = SessionKey("claude", path.stem)
    try:
        raw = path.read_bytes()
    except OSError:
        return SessionRuns(session, path, (), ())
    if b"/pstack:poteto-mode</command-name>" not in raw:
        return SessionRuns(session, path, (), ())
    uses: dict[str, tuple[str, Mapping[str, Any]]] = {}
    agent_ids: dict[str, str] = {}
    notes: dict[str, TaskNote] = {}
    handbacks: dict[str, Said] = {}
    seen_prs: set[str] = set()
    seen_orca: dict[OrcaRunId, None] = {}
    folds: list[_RunFold] = []

    def note_orca(fold: _RunFold | None, ids: Iterable[str]) -> None:
        for rid in ids:
            key = OrcaRunId(rid)
            if key not in seen_orca:
                seen_orca[key] = None
                if fold is not None:
                    fold.orca_runs.append(key)

    def take_notes(text: str, at: datetime | None) -> None:
        for note in parse_notes(text, at):
            notes[note.task_id] = note

    for line in raw.splitlines():
        if line.startswith(b'{"type":"api-request'):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain"):
            continue
        at = as_dt(obj.get("timestamp"))
        kind = obj.get("type")
        fold = folds[-1] if folds else None

        if kind == "user":
            result = obj.get("toolUseResult")
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = message.get("content")
            if result is not None:
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict) or part.get("type") != "tool_result":
                        continue
                    use_id = str(part.get("tool_use_id") or "")
                    name, payload = uses.get(use_id, ("", {}))
                    if fold is not None:
                        fold.pending.pop(use_id, None)
                    if name in LAUNCH_TOOLS and isinstance(result, dict) and result.get("agentId"):
                        agent_ids[use_id] = str(result["agentId"])
                    if name == "Bash" and "orchestration run-create" in str(payload.get("command") or ""):
                        note_orca(fold, ORCA_RUN.findall(_result_text(content) + json.dumps(result, ensure_ascii=False)))
                if fold is not None:
                    fold.touch(at)
                continue
            origin = obj.get("origin") if isinstance(obj.get("origin"), dict) else {}
            if origin.get("kind") == "peer" and origin.get("handback") and origin.get("from"):
                report = str(origin.get("handbackReportText") or "").strip()
                if report:
                    handbacks[str(origin["from"])] = Said(report, at)
                continue
            if obj.get("isMeta"):
                version = SKILL_BASE.search(content_text(content))
                if fold is not None and version and fold.version is None:
                    fold.version = version.group(1)
                continue
            line_kind, text = classify_user(obj)
            if line_kind is UserLine.POTETO:
                args = COMMAND_ARGS.search(text)
                request = Said((args.group(1) if args else "").strip(), at)
                if fold is not None:
                    fold.until = at
                folds.append(_RunFold(len(folds) + 1, request, at or datetime.now(timezone.utc)))
                folds[-1].touch(at)
            elif fold is None or line_kind is UserLine.NOISE:
                if line_kind is UserLine.NOTE:
                    take_notes(text, at)
            elif line_kind is UserLine.PROMPT:
                said = clean_said(text, at)
                if said is not None:
                    fold.asks.append(said)
                fold.wake(at)
            elif line_kind is UserLine.NUDGE:
                note_orca(fold, ORCA_RUN.findall(text))
                fold.wake(at)
            elif line_kind is UserLine.NOTE:
                take_notes(text, at)
                fold.wake(at)
            elif line_kind is UserLine.INTERRUPT:
                fold.pending.clear()
                fold.turn = Turn.INTERRUPTED
                fold.touch(at)
            continue

        if kind == "attachment":
            attachment = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
            if attachment.get("type") == "queued_command":
                take_notes(str(attachment.get("prompt") or ""), at)
            continue

        if kind == "assistant":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if obj.get("isApiErrorMessage"):
                if fold is not None:
                    fold.error = Said(
                        one_line(content_text(message.get("content")) or str(obj.get("error") or "API error"), 200),
                        at,
                    )
                    fold.touch(at)
                continue
            for part in message.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    use_id = str(part.get("id") or "")
                    name = str(part.get("name") or "")
                    payload = part.get("input") if isinstance(part.get("input"), dict) else {}
                    uses.setdefault(use_id, (name, payload))
                    if fold is None:
                        continue
                    fold.pending.setdefault(use_id, _pending_of(name, payload, at))
                    for slug, excerpt in _playbook_reads(name, payload):
                        fold.reads.append((at, slug, excerpt))
                    if name in LAUNCH_TOOLS and use_id not in fold.launches:
                        fold.launches[use_id] = SubagentFacts(
                            use_id,
                            None,
                            str(payload.get("description") or "subagent"),
                            str(payload.get("subagent_type") or "general-purpose"),
                            payload.get("model") if isinstance(payload.get("model"), str) else None,
                            payload.get("effort") if isinstance(payload.get("effort"), str) else None,
                            at,
                            None,
                            None,
                        )
                elif part.get("type") == "text" and fold is not None and str(part.get("text") or "").strip():
                    fold.texts.append((at, str(part["text"])))
            if fold is not None:
                fold.error = None
                if fold.turn is not Turn.INTERRUPTED:
                    fold.turn = Turn.OPEN
                fold.touch(at)
            continue

        if fold is None:
            continue
        if kind == "system" and obj.get("subtype") == "turn_duration":
            fold.pending.clear()
            if fold.turn is not Turn.INTERRUPTED:
                fold.turn = Turn.API_ERROR if fold.error is not None else Turn.CLOSED
            fold.touch(at)
        elif kind == "system" and obj.get("subtype") == "away_summary":
            text = RECAP_SUFFIX.sub("", str(obj.get("content") or "")).strip()
            if text:
                fold.recap = Said(text, at)
        elif kind == "pr-link" and obj.get("prUrl") and obj["prUrl"] not in seen_prs:
            seen_prs.add(obj["prUrl"])
            fold.prs.append(PrLink(int(obj.get("prNumber") or 0), str(obj["prUrl"]), str(obj.get("prRepository") or ""), at))

    runs = tuple(_freeze(session, fold, agent_ids, notes, handbacks) for fold in folds)
    return SessionRuns(session, path, runs, tuple(seen_orca))


def detect_playbooks(fold: _RunFold) -> tuple[tuple[PlaybookHit, ...], Checklist | None]:
    registry = load_registry(fold.version)
    if registry is None:
        return (), None
    hits: list[PlaybookHit] = []

    def hit(slug: str, evidence: Evidence, at: datetime | None, detail: str) -> None:
        book = registry.playbooks.get(slug)
        if book is not None:
            hits.append(PlaybookHit(slug, book.title, evidence, at, detail))

    for at, slug, excerpt in fold.reads:
        hit(slug, Evidence.READ, at, excerpt)
    checklist: Checklist | None = None
    for at, text in fold.texts:
        found = parse_checklist(text, registry, at)
        if found is not None:
            checklist = found
            if found.playbook:
                hit(found.playbook, Evidence.DECLARED, at, f"Playbook: {found.title}")
        norm = normalize(text)
        copied: dict[str, list[int]] = {}
        for sig, (slug, weight) in registry.signatures.items():
            if sig in norm:
                copied.setdefault(slug, []).append(weight)
        for slug, weights in copied.items():
            if sum(weights) >= COPIED_WEIGHT:
                hit(slug, Evidence.COPIED, at, f"{len(weights)} of {len(registry.playbooks[slug].steps)} step lines copied")
        for match in registry.named.finditer(text):
            slug = registry.titles.get(match.group(1).casefold())
            if slug:
                start = max(0, match.start() - 40)
                hit(slug, Evidence.NAMED, at, one_line(text[start : match.end() + 40], 120))
    best: dict[str, PlaybookHit] = {}
    first: dict[str, datetime] = {}
    for item in hits:
        when = item.at or fold.started_at
        first[item.playbook] = min(first.get(item.playbook, when), when)
        current = best.get(item.playbook)
        if current is None or item.evidence < current.evidence:
            best[item.playbook] = item
    trail = tuple(sorted(best.values(), key=lambda item: first[item.playbook]))
    return trail, checklist


def _freeze(
    session: SessionKey,
    fold: _RunFold,
    agent_ids: Mapping[str, str],
    notes: Mapping[str, TaskNote],
    handbacks: Mapping[str, Said],
) -> Run:
    trail, checklist = detect_playbooks(fold)
    subagents = []
    for use_id, facts in fold.launches.items():
        agent_id = agent_ids.get(use_id)
        note = notes.get(agent_id) if agent_id else None
        if note is None:
            note = next((item for item in notes.values() if item.tool_use_id == use_id), None)
        handback = handbacks.get(agent_id) if agent_id else None
        subagents.append(
            SubagentFacts(
                use_id,
                agent_id,
                facts.title,
                facts.agent_type,
                facts.model,
                facts.effort,
                facts.launched_at,
                note,
                handback,
            )
        )
    reply = None
    for at, text in reversed(fold.texts):
        reply = clean_said(text, at)
        if reply is not None:
            break
    pending = None
    if fold.turn is Turn.OPEN and fold.pending:
        pending = list(fold.pending.values())[-1]
    return Run(
        RunId(f"{session}#{fold.n}"),
        session,
        fold.n,
        fold.request,
        fold.started_at,
        fold.until,
        tuple(fold.asks),
        trail,
        checklist,
        tuple(subagents),
        tuple(fold.orca_runs),
        tuple(fold.prs),
        reply,
        fold.recap,
        fold.error,
        fold.turn,
        pending,
        fold.last_event_at or fold.started_at,
    )


class RosterStatus(enum.Enum):
    KNOWN = "known"
    UNAVAILABLE = "unavailable"
    MEMO = "memo"


@dataclass(frozen=True, slots=True)
class OrcaAttempt:
    dispatch: str
    provider: str | None
    state: str
    liveness: str | None
    terminal: str | None


ORCA_TASK_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled"})


@dataclass(frozen=True, slots=True)
class OrcaTask:
    id: str
    title: str
    created_at: datetime | None
    completed_at: datetime | None
    status: str
    outcome: str | None
    subject: str | None
    report: str | None
    files: tuple[str, ...]
    attempts: tuple[OrcaAttempt, ...]

    @property
    def terminal(self) -> bool:
        return self.status in ORCA_TASK_TERMINAL


@dataclass(frozen=True, slots=True)
class OrcaRunView:
    id: OrcaRunId
    tasks: tuple[OrcaTask, ...]
    observed_at: datetime

    @property
    def settled(self) -> bool:
        # An empty answer is not settled. all([]) is true, and run-create often returns before any task exists.
        return bool(self.tasks) and all(task.terminal for task in self.tasks)


@dataclass(frozen=True, slots=True)
class OrcaRoster:
    id: OrcaRunId
    status: RosterStatus
    reason: str | None
    observed_at: datetime | None
    view: OrcaRunView | None


ORCA_RUN_CALLS: dict[str, tuple[str, ...]] = {
    "tasks": ("orchestration", "task-list"),
    "workers": ("orchestration", "worker-list"),
}
_ORCA_SEEN: dict[OrcaRunId, OrcaRunView] = {}


def _utc(value: Any) -> datetime | None:
    dt = as_dt(value)
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_orca_run(run_id: OrcaRunId, tasks_payload: Any, workers_payload: Any, observed_at: datetime) -> OrcaRunView | None:
    tasks = ((tasks_payload or {}).get("result") or {}).get("tasks") if isinstance(tasks_payload, dict) else None
    workers = ((workers_payload or {}).get("result") or {}).get("workers") if isinstance(workers_payload, dict) else None
    if not isinstance(tasks, list):
        return None
    attempts: dict[str, list[OrcaAttempt]] = {}
    for worker in workers if isinstance(workers, list) else []:
        if not isinstance(worker, dict):
            continue
        projection = worker.get("projection") if isinstance(worker.get("projection"), dict) else {}
        provider = projection.get("provider") if isinstance(projection.get("provider"), dict) else {}
        liveness = projection.get("liveness") if isinstance(projection.get("liveness"), dict) else {}
        attempts.setdefault(str(worker.get("taskId") or ""), []).append(
            OrcaAttempt(
                str(worker.get("dispatchId") or ""),
                provider.get("id") if isinstance(provider.get("id"), str) else None,
                str(worker.get("workerState") or ""),
                liveness.get("verdict") if isinstance(liveness.get("verdict"), str) else None,
                worker.get("agentTerminalHandle") if isinstance(worker.get("agentTerminalHandle"), str) else None,
            )
        )
    out: list[OrcaTask] = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        result: Any = task.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = None
        result = result if isinstance(result, dict) else {}
        files = result.get("filesModified") if isinstance(result.get("filesModified"), list) else []
        out.append(
            OrcaTask(
                str(task["id"]),
                str(task.get("task_title") or task.get("display_name") or task["id"]),
                _utc(task.get("created_at")),
                _utc(task.get("completed_at")),
                str(task.get("status") or ""),
                result.get("outcome") if isinstance(result.get("outcome"), str) else None,
                result.get("subject") if isinstance(result.get("subject"), str) else None,
                result.get("reportPath") if isinstance(result.get("reportPath"), str) else None,
                tuple(str(item) for item in files),
                tuple(attempts.get(str(task["id"]), ())),
            )
        )
    return OrcaRunView(run_id, tuple(out), observed_at)


def _call_label(name: str, args: tuple[str, ...]) -> str:
    if "|" in name:
        rid, kind = name.split("|", 1)
        return f"{rid} {kind}"
    return " ".join(args[:2])


def orca_json(calls: Mapping[str, tuple[str, ...]]) -> tuple[dict[str, Any | None], list[str]]:
    procs: dict[str, subprocess.Popen[str]] = {}
    out: dict[str, Any | None] = {}
    problems: list[str] = []
    for name, args in calls.items():
        label = _call_label(name, args)
        try:
            procs[name] = subprocess.Popen(
                [ORCA_BIN, *args, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            out[name] = None
            problems.append(f"{label}: {exc}")
    for name, proc in procs.items():
        label = _call_label(name, calls[name])
        try:
            stdout, _stderr = proc.communicate(timeout=ORCA_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            out[name] = None
            problems.append(f"{label}: timeout after {ORCA_TIMEOUT_S:.1f} s")
            continue
        if proc.returncode != 0:
            out[name] = None
            problems.append(f"{label}: exit {proc.returncode}")
            continue
        try:
            out[name] = json.loads(stdout)
        except json.JSONDecodeError:
            out[name] = None
            problems.append(f"{label}: bad json")
    return out, problems


def _roster_reason(rid: str, problems: Iterable[str]) -> str:
    hits = [item for item in problems if item.startswith(f"{rid} ")]
    return "; ".join(hits) if hits else "orca unavailable"


def observe_orca_runs(ids: Iterable[OrcaRunId], now: datetime) -> tuple[dict[OrcaRunId, OrcaRoster], tuple[str, ...]]:
    unique = list(dict.fromkeys(ids))
    wanted = [rid for rid in unique if not (rid in _ORCA_SEEN and _ORCA_SEEN[rid].settled)]
    calls = {f"{rid}|{name}": (*args, "--run", str(rid)) for rid in wanted for name, args in ORCA_RUN_CALLS.items()}
    raw, problems = orca_json(calls) if calls else ({}, [])
    found: dict[OrcaRunId, OrcaRoster] = {}
    for rid in unique:
        cached_view = _ORCA_SEEN.get(rid)
        if cached_view is not None and cached_view.settled:
            found[rid] = OrcaRoster(rid, RosterStatus.KNOWN, None, cached_view.observed_at, cached_view)
            continue
        view = parse_orca_run(rid, raw.get(f"{rid}|tasks"), raw.get(f"{rid}|workers"), now)
        if view is not None:
            _ORCA_SEEN[rid] = view
            found[rid] = OrcaRoster(rid, RosterStatus.KNOWN, None, view.observed_at, view)
            continue
        reason = _roster_reason(str(rid), problems)
        if cached_view is not None:
            found[rid] = OrcaRoster(rid, RosterStatus.MEMO, reason, cached_view.observed_at, cached_view)
        else:
            found[rid] = OrcaRoster(rid, RosterStatus.UNAVAILABLE, reason, None, None)
    return found, tuple(problems)


class DelegateState(enum.IntEnum):
    NEEDS_YOU = 0
    RUNNING = 1
    FAILED = 2
    DONE = 3
    UNKNOWN = 4

    @property
    def wire(self) -> str:
        return self.name.lower()


DelegateSource = Literal["handback", "notification", "meta", "subagent-file", "orca", "orca-memo", "transcript"]


@dataclass(frozen=True, slots=True)
class Delegate:
    kind: Literal["subagent", "orca"]
    id: str
    title: str
    who: str
    state: DelegateState
    source: DelegateSource
    started_at: datetime | None
    last_seen_at: datetime | None
    result: str | None
    report: str | None
    attempts: tuple[str, ...]
    pane: str | None = None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _meta_finished(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("finished") is True


def subagent_delegate(facts: SubagentFacts, folder: Path, now: datetime) -> Delegate:
    who = " · ".join(part for part in (facts.agent_type, " ".join(p for p in (facts.model, facts.effort) if p)) if part)
    seen = _mtime(folder / f"agent-{facts.agent_id}.jsonl") if facts.agent_id else None

    def make(state: DelegateState, source: DelegateSource, result: str | None) -> Delegate:
        return Delegate(
            "subagent",
            facts.agent_id or facts.tool_use_id,
            facts.title,
            who,
            state,
            source,
            facts.launched_at,
            seen,
            result,
            None,
            (),
        )

    note = facts.note
    resumed = note is not None and note.at is not None and seen is not None and seen > note.at + RESUME_SLACK
    if note is not None and not resumed:
        if note.status == "completed" and facts.handback is not None:
            return make(DelegateState.DONE, "handback", one_line(facts.handback.text.split("\n\n", 1)[0], 240))
        if note.status == "completed":
            return make(DelegateState.DONE, "notification", one_line(note.result, 240) or None)
        return make(DelegateState.FAILED, "notification", f"{note.status}: {one_line(note.result, 200)}")
    if facts.agent_id and not resumed and _meta_finished(folder / f"agent-{facts.agent_id}.meta.json"):
        return make(DelegateState.DONE, "meta", None)
    if seen is not None and now - seen <= STALE_WINDOW:
        return make(DelegateState.RUNNING, "subagent-file", None)
    if facts.agent_id is None:
        return make(DelegateState.UNKNOWN, "transcript", "launch result not in transcript")
    quiet = "never wrote" if seen is None else f"quiet for {int((now - seen).total_seconds() // 60)}m"
    return make(DelegateState.UNKNOWN, "subagent-file", quiet)


def orca_delegate(task: OrcaTask, view: OrcaRunView, fresh: bool, panes: Mapping[str, Agent]) -> Delegate:
    pane = next((panes[attempt.terminal] for attempt in reversed(task.attempts) if attempt.terminal in panes), None)
    if task.status == "completed":
        state = DelegateState.DONE if task.outcome in (None, "succeeded") else DelegateState.FAILED
    elif task.terminal:
        state = DelegateState.FAILED
    elif pane is not None and pane.status is Status.NEEDS_YOU:
        state = DelegateState.NEEDS_YOU
    else:
        state = DelegateState.RUNNING if fresh else DelegateState.UNKNOWN
    provider = next((attempt.provider for attempt in task.attempts if attempt.provider), None)
    result = pane.reason if state is DelegateState.NEEDS_YOU and pane is not None else task.subject
    return Delegate(
        "orca",
        task.id,
        task.title,
        provider or "orca",
        state,
        "orca" if fresh else "orca-memo",
        task.created_at,
        task.completed_at or (view.observed_at if not task.terminal else None),
        result,
        task.report,
        tuple(attempt.state for attempt in task.attempts),
        str(pane.key) if pane is not None else None,
    )


class RunState(enum.IntEnum):
    NEEDS_YOU = 0
    WORKING = 1
    WAITING = 2
    YOUR_MOVE = 3
    INTERRUPTED = 4
    ENDED = 5

    @property
    def wire(self) -> str:
        return self.name.lower()


RunStateSource = Literal["transcript", "session", "delegates"]


@dataclass(frozen=True, slots=True)
class RunEvidence:
    run: Run
    delegates: tuple[Delegate, ...]
    session: Agent | None
    latest: bool
    now: datetime

    @property
    def quiet(self) -> timedelta:
        return self.now - self.run.last_event_at

    @property
    def pane_working(self) -> bool:
        return self.session is not None and self.session.status is Status.WORKING and self.session.status_source == "orca"


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%H:%M") if dt else "?"


def rule_asked(evidence: RunEvidence) -> str | None:
    pending = evidence.run.pending
    if evidence.latest and pending is not None and pending.kind is PendingKind.ASK:
        return f"asked: {pending.detail}"
    return None


def rule_delegate_blocked(evidence: RunEvidence) -> str | None:
    blocked = next((item for item in evidence.delegates if item.state is DelegateState.NEEDS_YOU), None)
    if blocked is not None:
        return f"{one_line(blocked.title, 40)}: {blocked.result or 'waiting for you'}"
    return None


def rule_session_blocked(evidence: RunEvidence) -> str | None:
    if evidence.latest and evidence.session is not None and evidence.session.status is Status.NEEDS_YOU:
        return evidence.session.reason
    return None


def rule_interrupted(evidence: RunEvidence) -> str | None:
    if evidence.latest and evidence.run.turn is Turn.INTERRUPTED:
        return f"you interrupted at {_hhmm(evidence.run.last_event_at)}"
    return None


def rule_working(evidence: RunEvidence) -> str | None:
    pending = evidence.run.pending
    if not evidence.latest or evidence.run.turn is not Turn.OPEN or (pending is not None and pending.kind is PendingKind.WAIT):
        return None
    if evidence.quiet > STALE_WINDOW and not evidence.pane_working:
        return None
    return pending.detail if pending is not None else "thinking"


def rule_waiting(evidence: RunEvidence) -> str | None:
    running = [item for item in evidence.delegates if item.state is DelegateState.RUNNING]
    if running:
        names = ", ".join(one_line(item.title, 40) for item in running[:3])
        error = evidence.run.error if evidence.latest and evidence.run.turn is Turn.API_ERROR else None
        tail = f" · last turn hit an API error at {_hhmm(error.at)}" if error is not None else ""
        return f"waiting on {len(running)}: {names}{tail}"
    pending = evidence.run.pending
    if (
        evidence.latest
        and evidence.run.turn is Turn.OPEN
        and pending is not None
        and pending.kind is PendingKind.WAIT
        and evidence.quiet <= STALE_WINDOW
    ):
        return f"waiting: {pending.detail}"
    return None


def rule_api_error(evidence: RunEvidence) -> str | None:
    if evidence.latest and evidence.run.turn is Turn.API_ERROR and evidence.run.error is not None:
        return f"stopped on API error at {_hhmm(evidence.run.error.at)}: {evidence.run.error.text}"
    return None


def rule_stale(evidence: RunEvidence) -> str | None:
    if evidence.latest and evidence.run.turn is Turn.OPEN:
        return f"turn never closed, quiet for {int(evidence.quiet.total_seconds() // 60)}m"
    return None


def rule_ended(evidence: RunEvidence) -> str | None:
    if not evidence.latest:
        return f"run #{evidence.run.n + 1} started {_hhmm(evidence.run.until)}"
    return None


def rule_replied(evidence: RunEvidence) -> str | None:
    return f"replied {_hhmm(evidence.run.last_event_at)}"


RUN_STATE_RULES: tuple[tuple[RunState, RunStateSource, Callable[[RunEvidence], str | None]], ...] = (
    (RunState.NEEDS_YOU, "transcript", rule_asked),
    (RunState.NEEDS_YOU, "delegates", rule_delegate_blocked),
    (RunState.NEEDS_YOU, "session", rule_session_blocked),
    (RunState.INTERRUPTED, "transcript", rule_interrupted),
    (RunState.WORKING, "transcript", rule_working),
    (RunState.WAITING, "delegates", rule_waiting),
    (RunState.INTERRUPTED, "transcript", rule_api_error),
    (RunState.INTERRUPTED, "transcript", rule_stale),
    (RunState.ENDED, "transcript", rule_ended),
    (RunState.YOUR_MOVE, "transcript", rule_replied),
)


def state_of(evidence: RunEvidence) -> tuple[RunState, RunStateSource, str]:
    for state, source, rule in RUN_STATE_RULES:
        reason = rule(evidence)
        if reason:
            return state, source, reason
    return RunState.YOUR_MOVE, "transcript", "replied"


def run_progress(run: Run, delegates: tuple[Delegate, ...]) -> Progress | None:
    if run.checklist is not None:
        done = sum(1 for mark, _text in run.checklist.steps if mark is not StepMark.OPEN)
        return Progress(done, len(run.checklist.steps), "checklist")
    if delegates:
        settled = sum(1 for item in delegates if item.state in (DelegateState.DONE, DelegateState.FAILED))
        return Progress(settled, len(delegates), "delegates")
    return None


@dataclass(frozen=True, slots=True)
class RunResult:
    headline: Said | None
    headline_source: Literal["recap", "reply"] | None
    reply: Said | None
    prs: tuple[PrLink, ...]


def run_result(run: Run) -> RunResult:
    last_ask = run.asks[-1].at if run.asks else run.started_at
    if run.recap is not None and run.recap.at is not None and last_ask is not None and run.recap.at >= last_ask:
        return RunResult(run.recap, "recap", run.last_reply, run.prs)
    if run.last_reply is not None:
        first = run.last_reply.text.split("\n\n", 1)[0]
        return RunResult(Said(one_line(first, 240), run.last_reply.at), "reply", run.last_reply, run.prs)
    return RunResult(None, None, None, run.prs)


@dataclass(frozen=True, slots=True)
class RunView:
    run: Run
    delegates: tuple[Delegate, ...]
    rosters: tuple[OrcaRoster, ...]
    state: RunState
    state_source: RunStateSource
    reason: str
    progress: Progress | None
    result: RunResult


def _roster_for(rid: OrcaRunId, rosters: Mapping[OrcaRunId, OrcaRoster]) -> OrcaRoster:
    found = rosters.get(rid)
    if found is not None:
        return found
    return OrcaRoster(rid, RosterStatus.UNAVAILABLE, "not queried", None, None)


def view_runs(
    runs: SessionRuns,
    rosters: Mapping[OrcaRunId, OrcaRoster],
    session: Agent | None,
    now: datetime,
    panes: Mapping[str, Agent] | None = None,
) -> tuple[RunView, ...]:
    by_run: dict[RunId, list[Delegate]] = {run.id: [] for run in runs.runs}
    owned: dict[RunId, list[OrcaRoster]] = {run.id: [] for run in runs.runs}
    for run in runs.runs:
        for facts in run.subagents:
            by_run[run.id].append(subagent_delegate(facts, runs.subagents_dir, now))
    pane_map = panes or {}
    for rid in runs.orca_runs:
        roster = _roster_for(rid, rosters)
        owner = next((run for run in runs.runs if rid in run.orca_runs), runs.runs[-1] if runs.runs else None)
        if owner is None:
            continue
        owned[owner.id].append(roster)
        view = roster.view
        if view is None:
            continue
        just_seen = roster.observed_at is not None and roster.observed_at >= now - timedelta(seconds=5)
        fresh = roster.status is RosterStatus.KNOWN and (view.settled or just_seen)
        for task in view.tasks:
            target = owner
            for run in runs.runs:
                if task.created_at and run.started_at <= task.created_at and (run.until is None or task.created_at < run.until):
                    target = run
            by_run[target.id].append(orca_delegate(task, view, fresh, pane_map))
    out: list[RunView] = []
    for index, run in enumerate(runs.runs):
        delegates = tuple(sorted(by_run[run.id], key=lambda item: (item.started_at or run.started_at, item.id)))
        latest = index == len(runs.runs) - 1
        state, source, reason = state_of(RunEvidence(run, delegates, session if latest else None, latest, now))
        out.append(
            RunView(
                run,
                delegates,
                tuple(owned[run.id]),
                state,
                source,
                reason,
                run_progress(run, delegates),
                run_result(run),
            )
        )
    return tuple(out)


def _hit(hit: PlaybookHit | None) -> dict[str, Any] | None:
    if hit is None:
        return None
    return {
        "slug": hit.playbook,
        "title": hit.title,
        "evidence": hit.evidence.wire,
        "detail": hit.detail,
        "at": iso(hit.at),
    }


def _observed(text: str | None, source: str | None) -> dict[str, Any]:
    if text:
        return {"text": text, "source": source}
    return {"text": "result not observed", "source": None}


def run_chip_wire(view: RunView, count: int) -> dict[str, Any]:
    tally = {state.wire: 0 for state in DelegateState}
    for delegate in view.delegates:
        tally[delegate.state.wire] += 1
    progress = view.progress
    return {
        "id": view.run.id,
        "n": view.run.n,
        "count": count,
        "request": one_line(view.run.request.text, 160),
        "playbook": _hit(view.run.playbook),
        "trail": [hit.title for hit in view.run.playbooks],
        "state": view.state.wire,
        "state_source": view.state_source,
        "reason": view.reason,
        "delegates": tally,
        "progress": None
        if progress is None
        else {"done": progress.done, "total": progress.total, "source": progress.source},
        "started_at": iso(view.run.started_at),
    }


def _roster_wire(roster: OrcaRoster) -> dict[str, Any]:
    return {
        "id": roster.id,
        "status": roster.status.value,
        "reason": roster.reason,
        "observed_at": iso(roster.observed_at),
        "tasks": None if roster.view is None else len(roster.view.tasks),
    }


def run_wire(view: RunView) -> dict[str, Any]:
    run = view.run
    checklist = run.checklist
    headline = view.result.headline
    observed = _observed(headline.text if headline is not None else None, view.result.headline_source if headline is not None else None)
    return {
        "id": run.id,
        "n": run.n,
        "request": _said_wire(run.request),
        "asks": [_said_wire(ask) for ask in run.asks],
        "started_at": iso(run.started_at),
        "until": iso(run.until),
        "last_event_at": iso(run.last_event_at),
        "playbook": _hit(run.playbook),
        "trail": [_hit(hit) for hit in run.playbooks],
        "checklist": None
        if checklist is None
        else {
            "title": checklist.title,
            "known": checklist.playbook is not None,
            "at": iso(checklist.at),
            "steps": [{"mark": mark.name.lower(), "text": text} for mark, text in checklist.steps],
        },
        "state": view.state.wire,
        "state_source": view.state_source,
        "reason": view.reason,
        "progress": None
        if view.progress is None
        else {"done": view.progress.done, "total": view.progress.total, "source": view.progress.source},
        "delegates": [
            {
                "kind": item.kind,
                "id": item.id,
                "title": item.title,
                "who": item.who,
                "state": item.state.wire,
                "source": item.source,
                "started_at": iso(item.started_at),
                "last_seen_at": iso(item.last_seen_at),
                "result": _observed(item.result, item.source if item.result else None),
                "report": item.report,
                "attempts": list(item.attempts),
                "pane": item.pane,
            }
            for item in view.delegates
        ],
        "orca_runs": list(run.orca_runs),
        "roster": [_roster_wire(item) for item in view.rosters],
        "result": {
            "headline": observed["text"],
            "source": observed["source"],
            "at": iso(headline.at) if headline is not None else None,
            "reply": _said_wire(view.result.reply),
            "prs": [{"number": pr.number, "url": pr.url, "repo": pr.repo} for pr in view.result.prs],
        },
    }


def claude_runs(head: SessionHead) -> SessionRuns:
    empty = SessionRuns(head.key, head.transcript, (), ())
    if head.key.vendor != "claude":
        return empty
    try:
        parsed = cached(head.transcript, parse_session)
    except OSError:
        return empty
    return parsed if isinstance(parsed, SessionRuns) else empty


def _panes_by_handle(agents: Iterable[Agent]) -> dict[str, Agent]:
    found: dict[str, Agent] = {}
    for agent in agents:
        handle = agent.pane.handle if agent.pane is not None else None
        if handle:
            found[handle] = agent
    return found


_LAST_BOARD: Board | None = None


def snapshot_runs(board: Board, now: datetime) -> dict[SessionKey, tuple[RunView, ...]]:
    agents = list(board.main_view())
    sessions: dict[SessionKey, SessionRuns] = {}
    for agent in agents:
        if agent.vendor != "claude" or not isinstance(agent.key, SessionKey) or agent.head is None:
            continue
        parsed = claude_runs(agent.head)
        if parsed.runs:
            sessions[agent.key] = parsed
    ids = [rid for parsed in sessions.values() for rid in parsed.orca_runs]
    rosters, _problems = observe_orca_runs(ids, now)
    panes = _panes_by_handle(agents)
    return {
        key: view_runs(parsed, rosters, board.agents.get(key), now, panes)
        for key, parsed in sessions.items()
    }


def runs_payload(key: SessionKey, now: datetime) -> dict[str, Any]:
    if key.vendor != "claude":
        raise KeyError(str(key))
    head = read_heads().get(key)
    if head is None:
        raise KeyError(str(key))
    parsed = claude_runs(head)
    rosters, problems = observe_orca_runs(parsed.orca_runs, now)
    session = _LAST_BOARD.agents.get(key) if _LAST_BOARD is not None else None
    panes = _panes_by_handle(_LAST_BOARD.agents.values()) if _LAST_BOARD is not None else {}
    views = view_runs(parsed, rosters, session, now, panes)
    return {
        "key": str(key),
        "at": iso(now),
        "orca": {"problems": list(problems)},
        "runs": [run_wire(view) for view in reversed(views)],
    }


def _delegate_index(runs: Mapping[SessionKey, tuple[RunView, ...]] | None) -> dict[str, str]:
    found: dict[str, str] = {}
    if not runs:
        return found
    for views in runs.values():
        for view in views:
            for delegate in view.delegates:
                if delegate.pane:
                    found[delegate.pane] = str(view.run.id)
    return found


def latest_of(
    agent: Agent,
    detail: SessionDetail | None,
) -> tuple[Literal["ask", "tool", "reply", "prompt"], Said] | None:
    if agent.status is Status.NEEDS_YOU:
        return "ask", Said(agent.reason, agent.since)
    if agent.status is Status.WORKING:
        if agent.pane is not None and agent.pane.tool:
            return "tool", Said(agent.pane.tool, agent.pane.last_output_at)
        if detail is not None and detail.last_reply is not None:
            return "reply", detail.last_reply
        if agent.pane is not None and agent.pane.reply is not None:
            return "reply", agent.pane.reply
        return "tool", Said(agent.reason, agent.since)
    if agent.status is Status.DONE:
        if detail is not None and detail.last_reply is not None:
            return "reply", detail.last_reply
        if agent.pane is not None and agent.pane.reply is not None:
            return "reply", agent.pane.reply
        return "reply", Said(agent.reason, agent.since)
    if detail is not None and detail.last_prompt is not None:
        return "prompt", detail.last_prompt
    if agent.pane is not None and agent.pane.prompt is not None:
        return "prompt", agent.pane.prompt
    return None


def _said_wire(said: Said | None) -> dict[str, Any] | None:
    if said is None:
        return None
    return {"text": said.text, "at": iso(said.at)}


def _agent_wire(
    board: Board,
    key: AgentKey,
    details: Mapping[AgentKey, SessionDetail],
    *,
    nest: bool,
    runs: Mapping[SessionKey, tuple[RunView, ...]] | None = None,
    delegate_index: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    agent = board.agents[key]
    detail = details.get(key)
    child_keys = board.children.get(key, ()) if nest else ()
    child_agents = [board.agents[child] for child in child_keys]
    goal = goal_of(agent, detail)
    run_views = runs.get(agent.key) if runs is not None and isinstance(agent.key, SessionKey) else None
    latest_run = run_views[-1] if run_views else None
    progress = progress_of(agent, detail, child_agents, latest_run)
    latest = latest_of(agent, detail)
    detail_body = None
    if nest and agent.head is not None:
        shown = detail or SessionDetail(None, None, ())
        todos = []
        if agent.board_goal is not None:
            todos = [{"id": item.id, "text": item.text, "done": item.done} for item in agent.board_goal.todos]
        detail_body = {
            "cwd": agent.head.cwd,
            "last_prompt": _said_wire(shown.last_prompt),
            "last_reply": _said_wire(shown.last_reply),
            "plan": [{"text": step.text, "done": step.done} for step in shown.plan],
            "todos": todos,
        }
    body: dict[str, Any] = {
        "key": str(agent.key),
        "editable": isinstance(agent.key, SessionKey),
        "missing": agent.missing,
        "vendor": agent.vendor,
        "presence": agent.presence.value,
        "status": agent.status.wire,
        "status_source": agent.status_source,
        "reason": agent.reason,
        "since": iso(agent.since),
        "last_activity": iso(agent.last_activity),
        "unread": agent.unread,
        "workspace": agent.workspace,
        "goal": None if goal is None else {"text": goal.text, "source": goal.source},
        "progress": None if progress is None else {"done": progress.done, "total": progress.total, "source": progress.source},
        "latest": None if latest is None else {"kind": latest[0], "text": latest[1].text, "at": iso(latest[1].at)},
        "detail": detail_body,
        "children": [
            _agent_wire(board, child, details, nest=True, runs=runs, delegate_index=delegate_index)
            for child in child_keys
        ],
    }
    if latest_run is not None:
        body["run"] = run_chip_wire(latest_run, len(run_views or ()))
    owned = (delegate_index or {}).get(str(agent.key))
    if owned:
        body["delegate_of"] = owned
    return body


def to_wire(
    board: Board,
    details: Mapping[AgentKey, SessionDetail],
    now: datetime,
    took_ms: int,
    runs: Mapping[SessionKey, tuple[RunView, ...]] | None = None,
) -> dict[str, Any]:
    counts = {"needs_you": 0, "working": 0, "done": 0, "idle": 0, "history": len(board.history), "hidden_spawned": board.hidden_spawned}
    for agent in board.main_view():
        counts[agent.status.wire] += 1
    index = _delegate_index(runs)
    return {
        "at": iso(now),
        "took_ms": took_ms,
        "orca": {"up": board.orca.up, "problems": list(board.orca.problems)},
        "vendors": {name: source.color for name, source in VENDORS.items()},
        "counts": counts,
        "groups": [
            {
                "name": group.name,
                "path": group.path,
                "orca_status": group.orca_status,
                "status": group.status.wire,
                "agents": [
                    _agent_wire(board, root, details, nest=True, runs=runs, delegate_index=index) for root in group.roots
                ],
            }
            for group in board.groups
        ],
        "history": [_agent_wire(board, key, {}, nest=False, runs=runs, delegate_index=index) for key in board.history],
    }


def _safe_detail(head: SessionHead) -> SessionDetail:
    try:
        return VENDORS[head.key.vendor].detail(head)
    except Exception:
        return SessionDetail(None, None, ())


def snapshot(now: datetime | None = None) -> dict[str, Any]:
    global _LAST_BOARD
    started = time.perf_counter()
    moment = now or datetime.now(timezone.utc)
    goals = load_goals()
    orca = observe_orca()
    heads = read_heads()
    board = build_board(heads, orca, goals, moment)
    _LAST_BOARD = board
    details = {agent.key: _safe_detail(agent.head) for agent in board.main_view() if agent.head is not None}
    runs = snapshot_runs(board, moment)
    took_ms = int((time.perf_counter() - started) * 1000)
    return to_wire(board, details, moment, took_ms, runs)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[board] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("object required")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            try:
                body = PAGE_PATH.read_bytes()
            except OSError:
                self._send(500, b"page.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/board":
            try:
                payload = snapshot()
            except Exception as exc:
                message = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send(500, message, "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/runs":
            query = parse_qs(urlparse(self.path).query)
            key = SessionKey.parse(query.get("key", [""])[0])
            if key is None:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                payload = runs_payload(key, datetime.now(timezone.utc))
            except KeyError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            except Exception as exc:
                message = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send(500, message, "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        key = SessionKey.parse(str(payload.get("key") or ""))
        if key is None:
            self._send(400, b'{"error":"key required"}', "application/json")
            return
        try:
            if path == "/api/goal":
                stored = write_goal(key, text=str(payload.get("text") or "").strip())
            elif path == "/api/todos":
                incoming = payload.get("todos")
                if not isinstance(incoming, list):
                    self._send(400, b'{"error":"todos array required"}', "application/json")
                    return
                todos = [
                    Todo(str(item.get("id") or uuid.uuid4()), str(item.get("text") or "").strip(), bool(item.get("done")))
                    for item in incoming
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                stored = write_goal(key, todos=todos)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
        except ValueError as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"), "application/json")
            return
        body = {
            "text": stored.text,
            "todos": [{"id": item.id, "text": item.text, "done": item.done} for item in stored.todos],
        }
        self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="board.py")
    parser.add_argument("--json", action="store_true", help="print one board snapshot and exit")
    parser.add_argument("--print-rule", action="store_true", help="print the playbook checklist rule")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    if args.print_rule:
        sys.stdout.write(CHECKLIST_RULE)
        return
    if args.json:
        json.dump(snapshot(), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Agent back office on http://{HOST}:{args.port}", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
