# Session board

Personal local control plane for coding-agent sessions. Not a SaaS. Not a launcher.

## Locked decisions

- **Who:** me, running Grok + Claude + Codex at the same time, mostly through Orca
- **Job:** in 3 seconds see what each agent is trying to finish, how far, whether it is live, and whether it needs me
- **Goal owner:** this board. The saved goal is the only goal that is stored. The page also shows an inferred goal, labelled by source (`task`, `title`, `prompt`), when no board goal is saved. The board goal wins.
- **Progress:** todo count the board owns. Vendor plans, a poteto run, and finished child agents are shown only as a labelled hint
- **Sources:** Orca live panes (`worktree ps`, `terminal list`, and the hook file for the session join) plus sessions on disk
- **Out:** spawn, worktree, merge queue, team, phone, cloud

## v1

One local page. One row = one agent (a vendor session, joined to its live Orca pane when the hook file says so, or a live pane that has no session id yet).

`status · vendor · goal (source) · progress (source) · what it is doing · presence · time`

Sessions are read from disk:

- Grok `~/.grok/sessions/*/summary.json`
- Claude `~/.claude/projects/*/<sessionId>.jsonl`
- Codex `~/.codex/sessions/**/rollout-*.jsonl` and each Orca Codex account home

Goals live in `data/goals.json`, keyed `<vendor>:<sessionId>`. Vendors do not have to know. A saved goal still shows when its session is not on disk.

## Runs

Each `/pstack:poteto-mode` prompt in a Claude session is a run on that session's row. The row shows the latest run, the playbook it followed, how that was known, and how far it has got. Open the row for every run, newest first. Each run shows the request, follow-ups, the playbook trail and its evidence, every subagent and Orca worker with state and result, pull requests, and the result.

Subagent sessions stay off the board. An Orca worker that belongs to a run is listed on that run.

`python3 board.py --print-rule` prints the checklist format. A run that posts it gets exact step marks. Older runs still show up from the transcript alone.

## Run

```
python3 board.py
python3 board.py --json
python3 board.py --port 8766
python3 board.py --print-rule
```

Open http://127.0.0.1:8765
