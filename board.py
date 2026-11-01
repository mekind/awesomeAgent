#!/usr/bin/env python3
"""Agent back office: one row per agent, live from Orca or on disk, triaged by what needs you."""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, NewType, get_args
from urllib.parse import urlparse

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
    try:
        st = path.stat()
    except OSError:
        return parse(path)
    key = (str(path), getattr(parse, "__qualname__", ""), extra, int(st.st_mtime_ns), st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    value = parse(path)
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
    raw: dict[str, Any | None] = {}
    problems: list[str] = []
    procs: dict[str, subprocess.Popen[str]] = {}
    launch_errors: dict[str, str] = {}
    for name, args in ORCA_CALLS.items():
        label = f"{args[0]} {args[1]}"
        try:
            procs[name] = subprocess.Popen(
                [ORCA_BIN, *args, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            launch_errors[name] = f"{label}: {exc}"
    for name, args in ORCA_CALLS.items():
        label = f"{args[0]} {args[1]}"
        if name in launch_errors:
            raw[name] = None
            problems.append(launch_errors[name])
            continue
        proc = procs[name]
        try:
            out, _err = proc.communicate(timeout=ORCA_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raw[name] = None
            problems.append(f"{label}: timeout after {ORCA_TIMEOUT_S:.1f} s")
            continue
        if proc.returncode != 0:
            raw[name] = None
            problems.append(f"{label}: exit {proc.returncode}")
            continue
        try:
            raw[name] = json.loads(out)
        except json.JSONDecodeError:
            raw[name] = None
            problems.append(f"{label}: bad json")
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
ProgressSource = Literal["board", "plan", "workers"]
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
class Evidence:
    pane: Pane | None
    head: SessionHead | None
    unread: bool
    now: datetime

    @property
    def age(self) -> timedelta | None:
        if self.head is None:
            return None
        return self.now - self.head.touched_at


def rule_pane_blocked(evidence: Evidence) -> str | None:
    return evidence.pane.blocked_on if evidence.pane else None


def rule_screen_ask(evidence: Evidence) -> str | None:
    return evidence.pane.screen_ask if evidence.pane else None


def rule_disk_blocked(evidence: Evidence) -> str | None:
    if evidence.head is None or not evidence.head.blocked_on:
        return None
    alive = evidence.pane is not None or (evidence.age is not None and evidence.age <= DONE_WINDOW)
    if not alive:
        return None
    return evidence.head.blocked_on


def rule_pane_working(evidence: Evidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.WORKING:
        return None
    if pane.tool:
        return pane.tool
    if pane.working_mode == "monitoring":
        return "monitoring"
    return "working"


def rule_screen_output(evidence: Evidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.NO_HOOKS or pane.last_output_at is None:
        return None
    age = evidence.now - pane.last_output_at
    if age > WORKING_WINDOW:
        return None
    return f"output {max(0, int(age.total_seconds()))}s ago (no hooks)"


def rule_file_working(evidence: Evidence) -> str | None:
    if evidence.pane is not None or evidence.head is None or evidence.age is None:
        return None
    if evidence.age > WORKING_WINDOW:
        return None
    return "writing (no Orca pane)"


def rule_pane_done(evidence: Evidence) -> str | None:
    pane = evidence.pane
    if pane is None or pane.state is not PaneState.DONE:
        return None
    recent = pane.state_since is not None and (evidence.now - pane.state_since) <= DONE_WINDOW
    if not (evidence.unread or recent):
        return None
    if pane.reply:
        return one_line(pane.reply.text, 160)
    return "finished"


def rule_file_done(evidence: Evidence) -> str | None:
    if evidence.pane is not None or evidence.head is None or evidence.head.origin is not Origin.YOU:
        return None
    if evidence.age is None or evidence.age > DONE_WINDOW:
        return None
    mins = max(0, int(evidence.age.total_seconds() // 60))
    return f"finished {mins}m ago (no Orca pane)"


def rule_idle(_evidence: Evidence) -> str | None:
    return "idle"


STATUS_RULES: tuple[tuple[Status, StatusSource, Callable[[Evidence], str | None]], ...] = (
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


def status_of(evidence: Evidence) -> tuple[Status, StatusSource, str]:
    for status, source, rule in STATUS_RULES:
        reason = rule(evidence)
        if reason:
            return status, source, reason
    return Status.IDLE, "none", "idle"


def presence_of(evidence: Evidence) -> Presence:
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
        evidence = Evidence(pane, head, bool(pane and pane.workspace.unread), now)
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


def progress_of(agent: Agent, detail: SessionDetail | None, children: list[Agent]) -> Progress | None:
    if agent.board_goal is not None and agent.board_goal.todos:
        done = sum(1 for item in agent.board_goal.todos if item.done)
        return Progress(done, len(agent.board_goal.todos), "board")
    if detail is not None and detail.plan:
        done = sum(1 for step in detail.plan if step.done)
        return Progress(done, len(detail.plan), "plan")
    if children:
        done = sum(1 for child in children if child.status in {Status.DONE, Status.IDLE})
        return Progress(done, len(children), "workers")
    return None


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


def _agent_wire(board: Board, key: AgentKey, details: Mapping[AgentKey, SessionDetail], *, nest: bool) -> dict[str, Any]:
    agent = board.agents[key]
    detail = details.get(key)
    child_keys = board.children.get(key, ()) if nest else ()
    child_agents = [board.agents[child] for child in child_keys]
    goal = goal_of(agent, detail)
    progress = progress_of(agent, detail, child_agents)
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
    return {
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
        "children": [_agent_wire(board, child, details, nest=True) for child in child_keys],
    }


def to_wire(board: Board, details: Mapping[AgentKey, SessionDetail], now: datetime, took_ms: int) -> dict[str, Any]:
    counts = {"needs_you": 0, "working": 0, "done": 0, "idle": 0, "history": len(board.history), "hidden_spawned": board.hidden_spawned}
    for agent in board.main_view():
        counts[agent.status.wire] += 1
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
                "agents": [_agent_wire(board, root, details, nest=True) for root in group.roots],
            }
            for group in board.groups
        ],
        "history": [_agent_wire(board, key, {}, nest=False) for key in board.history],
    }


def _safe_detail(head: SessionHead) -> SessionDetail:
    try:
        return VENDORS[head.key.vendor].detail(head)
    except Exception:
        return SessionDetail(None, None, ())


def snapshot(now: datetime | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    moment = now or datetime.now(timezone.utc)
    goals = load_goals()
    orca = observe_orca()
    heads = read_heads()
    board = build_board(heads, orca, goals, moment)
    details = {agent.key: _safe_detail(agent.head) for agent in board.main_view() if agent.head is not None}
    took_ms = int((time.perf_counter() - started) * 1000)
    return to_wire(board, details, moment, took_ms)


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
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    if args.json:
        json.dump(snapshot(), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Agent back office on http://{HOST}:{args.port}", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
