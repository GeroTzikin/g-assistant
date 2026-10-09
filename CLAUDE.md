# G.A.R.V.I.S. (Jarvis) : Connected Project Guide

This file connects any Claude account or project to the Jarvis codebase. Load it as project knowledge (or keep it at the repo root, where Claude Code reads it automatically) before doing any work on Jarvis.

## 1. What Jarvis is

* G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), also called Jarvis.
* A Telegram bot (`@Tzikin_Assistant_Bot`) owned and operated by G (Geronimo).
* Two jobs:
  * Personal assistant for G: monitors group chats, drafts messages, manages reminders and tasks, sends daily briefings.
  * Business ops tool for Xeebi Mobile: invoice request routing, `/sent` and `/pending` invoice notifications, keyword watch rules, automated notifications.

## 2. Stack

* Python, single entry point: `bot.py`
* `python-telegram-bot` (with job queue) for the bot
* `Telethon` (user API session) for reading group chats and sending as G where bots cannot
* Anthropic API (Claude) for all reasoning
* Persistent state: `jarvis_memory.json` (path set by the `JARVIS_MEMORY_FILE` env var)
* Secrets live only in the server env file. Never commit tokens, session strings, API keys, IPs, or chat IDs to this repo. **This repository is public.**

## 3. Where it runs

* Production runs 24/7 on G's own Linux server as a systemd service (`jarvis.service`).
* Railway is retired for Jarvis. Never trigger, enable, or push a deploy to Railway; it would fight the server for the bot token.
* Deploying is done by G only. A connected Claude never deploys, restarts, or SSHes into the server.

## 4. Branch rules (MANDATORY)

| Branch | Purpose | Who can change it |
|---|---|---|
| `main` | Source of truth, what production runs | G only |
| `claude_collab` | Working branch for connected Claude projects | You (push directly) |

Workflow for every change:

1. Pull the latest `claude_collab`.
2. Make the change. Keep each commit focused on one thing with a clear message (G gets each commit message on Telegram).
3. Run `python -m py_compile bot.py` and confirm it passes before pushing.
4. Commit and push to `claude_collab`.
5. Never push to `main`, never force push, never delete `claude_collab`. G moves work from `claude_collab` into `main` himself when he deploys.

Every push or pull request on `claude_collab` sends G a Telegram notification through `.github/workflows/notify-claude-collab.yml`. Do not edit or disable that workflow.

## 5. Pull Request template

Optional. If you open a PR into `claude_collab` for a bigger change, use this body:

```
## What
One or two sentences on the change.

## Why
The problem or request it solves.

## Files touched
bot.py (functions: ...)

## Risk
Low / Medium / High, plus what could break.

## How I checked it
py_compile passed; any manual reasoning or tests.

## Needs from G
Anything G must do on deploy (new env var, restart, etc). Write "Nothing" if none.
```

## 6. Coding rules learned the hard way

* G wants complete, ready to deploy `bot.py` changes, never partial snippets.
* Escape Markdown in every Telegram message that includes client names, amounts, or user text. Unescaped characters cause parse errors and silent send failures.
* Guard every read of `jarvis_memory.json`. Handle missing keys, empty strings, and malformed records (for example never call `int("")`). A partial memory file must never crash the bot.
* Scheduling intent is checked first (`is_schedule_intent()`) before the redraft path, so scheduling requests are not misrouted.
* Some group chats cannot be resolved by name through Telethon; those use hardcoded IDs that already exist in `bot.py`. Reuse them, do not invent new ones.
* Bots cannot message other bots. Anything sent to another bot goes through the Telethon user session.
* `BLOCKED_CHAT_PATTERNS` near the top of `bot.py` cuts Jarvis off from specific chats entirely. Never remove an entry without G's approval.
* Owner only commands must stay owner only. Do not widen permissions on any command.
* Do not touch other services that share the server.
* When Jarvis goes silent, the first suspect is depleted Anthropic API credits, not the code.

## 7. Style for anything Jarvis writes

* Friendly but professional, direct.
* Concise.
* No dashes or em dashes in user facing text written for G.

## 8. What a connected Claude may and may not do

**May:** read the repo, commit and push to `claude_collab`, open PRs into `claude_collab`, write docs, review code, answer questions about how Jarvis works.

**May not:** push or merge into `main`, deploy, restart the service, edit secrets, change repo settings or the notification workflow, or add collaborators.

## 9. Connecting a new Claude account

1. In the other Claude account, connect GitHub and give it access to `GeroTzikin/g-assistant`.
2. Add this file to that Claude project's knowledge (or point it at this repo, where `CLAUDE.md` loads automatically).
3. First prompt to send it: "Read CLAUDE.md in GeroTzikin/g-assistant on the claude_collab branch and confirm the branch rules before doing anything."
