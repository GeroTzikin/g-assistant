import os
import anthropic
import json
import re
import requests
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ConversationHandler,
    filters, ContextTypes
)
from datetime import datetime, timedelta, time as dt_time
import pytz
import asyncio
import tempfile
from telethon import TelegramClient, events as tl_events
from telethon.sessions import StringSession

# ── ENV ───────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY        = os.environ.get("OPENAI_API_KEY", "")
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_ID       = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH     = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION      = os.environ.get("TELEGRAM_SESSION", "")
TAVILY_API_KEY        = os.environ.get("TAVILY_API_KEY", "")

# iCloud
ICLOUD_USERNAME       = os.environ.get("ICLOUD_USERNAME", "")
ICLOUD_APP_PASSWORD   = os.environ.get("ICLOUD_PASSWORD", "")

# Microsoft / Outlook
OUTLOOK_CLIENT_ID     = os.environ.get("OUTLOOK_CLIENT_ID", "")
OUTLOOK_CLIENT_SECRET = os.environ.get("OUTLOOK_CLIENT_SECRET", "")
OUTLOOK_TENANT_ID     = os.environ.get("OUTLOOK_TENANT_ID", "")
OUTLOOK_REFRESH_TOKEN = os.environ.get("OUTLOOK_REFRESH_TOKEN", "")

OWNER_TELEGRAM_ID    = 1475465779
XEEBI_SALES_GROUP_ID = -1003894146193
INVOICING_THREAD_ID  = 379
XEEBI_NOC_CHAT_ID    = -5236682220
UPM_NEWPORT_CHAT     = "UPM NEWPORT"
MEMORY_FILE          = os.environ.get("JARVIS_MEMORY_FILE", "/opt/jarvis/jarvis_memory.json")

# ── BLOCKED CHATS ─────────────────────────────────────────────────────────────
# Chats G.A.R.V.I.S. must not assist with in any way: no message logging, no
# briefings, no watch rules, no reply suggestions, no client-report inclusion,
# and no outgoing messages of any kind. Matched case-insensitively as a
# substring of the chat title / entity name.
BLOCKED_CHAT_PATTERNS = ("16 media", "16media")

def is_blocked_chat(name) -> bool:
    """True if `name` refers to a chat on the blocklist."""
    if not name:
        return False
    n = str(name).strip().lower()
    return any(pat in n for pat in BLOCKED_CHAT_PATTERNS)

def _entity_label(entity) -> str:
    """Best-effort display name for a Telethon entity or plain string."""
    if entity is None:
        return ""
    if isinstance(entity, str):
        return entity
    title = getattr(entity, "title", None)
    if title:
        return title
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    return " ".join(x for x in parts if x).strip()

TZ        = pytz.timezone("America/Los_Angeles")
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

ASKING_AMOUNT     = 1
group_logs        = {}
watch_setup_state = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
try:
    import openai as _openai
    openai_client = _openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except ImportError:
    openai_client = None

telethon_client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
)

class _TelethonCtx:
    """Context manager that ensures Telethon is connected but NEVER disconnects it,
    so the persistent event listener stays alive."""
    async def __aenter__(self):
        if not telethon_client.is_connected():
            await telethon_client.connect()
        return telethon_client
    async def __aexit__(self, *_):
        pass  # intentionally do not disconnect

_tl = _TelethonCtx()

# ── PROMPTS ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), a personal AI assistant modeled after J.A.R.V.I.S. from Iron Man.

Your personality:
- Professional, composed, and highly capable
- Direct and concise — no fluff, no filler
- Strategically minded — think several steps ahead
- Subtly dry wit when appropriate
- Deeply loyal — always refer to your user as "sir"

TOOLS AVAILABLE (use autonomously when relevant):

1. WEB_SEARCH — search the web for real-time info
   <TOOL>{"tool": "WEB_SEARCH", "params": {"query": "..."}}</TOOL>

2. GET_WEATHER — get weather for a location
   <TOOL>{"tool": "GET_WEATHER", "params": {"location": "..."}}</TOOL>

3. SAVE_MEMORY — remember a fact permanently
   <TOOL>{"tool": "SAVE_MEMORY", "params": {"key": "...", "value": "..."}}</TOOL>

4. READ_TELEGRAM_CHAT — read recent messages from a monitored group
   <TOOL>{"tool": "READ_TELEGRAM_CHAT", "params": {"chat_name": "..."}}</TOOL>

5. CREATE_CALENDAR_EVENT — add an event to sir's Apple Calendar (iCloud)
   Use for personal scheduling: flights, appointments, personal reminders.
   Always convert natural language dates to YYYY-MM-DD and times to HH:MM (24h).
   <TOOL>{"tool": "CREATE_CALENDAR_EVENT", "params": {"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "timezone": "America/Los_Angeles", "duration_minutes": 60, "notes": "..."}}</TOOL>

6. CREATE_OUTLOOK_EVENT — add an event to sir's Outlook / Office 365 Calendar
   Use for work meetings, business calls, and professional scheduling.
   Always convert natural language dates to YYYY-MM-DD and times to HH:MM (24h).
   <TOOL>{"tool": "CREATE_OUTLOOK_EVENT", "params": {"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "timezone": "America/Los_Angeles", "duration_minutes": 60, "notes": "...", "attendees": ["email@example.com"]}}</TOOL>

7. READ_OUTLOOK_EMAIL — read sir's recent Outlook emails
   Use when sir asks what's in his inbox, wants an email summary, or mentions a specific email.
   <TOOL>{"tool": "READ_OUTLOOK_EMAIL", "params": {"count": 10, "filter": "unread"}}</TOOL>

8. SEND_OUTLOOK_EMAIL — send an email via sir's Outlook account
   Always show sir the draft and wait for approval unless he says to send immediately.
   <TOOL>{"tool": "SEND_OUTLOOK_EMAIL", "params": {"to": "email@example.com", "subject": "...", "body": "..."}}</TOOL>

9. SEND_TELEGRAM — send a Telegram message to any contact or group immediately as sir
   Use when sir says "message X", "tell X", "text X", or "send X a message".
   The entity must be the exact Telegram contact name or group name.
   <TOOL>{"tool": "SEND_TELEGRAM", "params": {"entity": "Exact contact or group name", "message": "message text"}}</TOOL>

10. SEND_AND_FOLLOWUP — send a Telegram message AND automatically follow up if no reply
    Use when sir says "message X and follow up if no reply" or "remind me if X doesn't respond".
    <TOOL>{"tool": "SEND_AND_FOLLOWUP", "params": {"entity": "Exact contact or group name", "message": "message text", "followup_hours": 48, "reminder": "short reminder note for sir if no reply"}}</TOOL>

11. LIST_FOLLOWUPS — show all active follow-up tasks
    Use when sir asks "what follow-ups do I have" or "any pending follow-ups".
    <TOOL>{"tool": "LIST_FOLLOWUPS", "params": {}}</TOOL>

12. RECALL_MESSAGE — delete the last Telegram message that was sent on sir's behalf
    Use when sir says "delete that", "recall the message", "unsend", "take it back", or "delete what you just sent".
    <TOOL>{"tool": "RECALL_MESSAGE", "params": {}}</TOOL>

⚠️ CRITICAL TOOL RULES:
- Tool results prefixed with [ERROR] mean the operation FAILED. Relay the exact error word-for-word.
- NEVER say an action succeeded when the tool result contains [ERROR].
- NEVER deny sending a message. If a message was dispatched (tool result confirmed ✅), you sent it. Be honest.
- If sir asks what you last sent, use RECALL_MESSAGE or state what the tool result confirmed.

DRAFTING OUTGOING MESSAGES:
When sir asks you to compose or draft a message to send to a specific Telegram chat or person, format your response EXACTLY like this:

📝 *Draft:*
[the message text here]

<DEST>{"entity": "Exact Chat or Person Name", "type": "telethon"}</DEST>

Reply *yes* to send immediately, *schedule [time] [timezone]* to schedule, or tell me what to change.

IMPORTANT: Only include the <DEST> block when drafting an outgoing message to send somewhere. Do NOT include it in regular conversation responses.
"""

GROUP_SUMMARY_PROMPT = """You are G.A.R.V.I.S., providing a private briefing to sir on a client group chat.
Analyze these messages and provide:
1. 📌 KEY TOPICS — Main subjects discussed
2. ❓ OUTSTANDING NEEDS — What the client needs or is waiting on
3. ⚡ ACTION ITEMS — What sir should follow up on
4. 💡 SUGGESTED REPLIES — 3 response options numbered 1, 2, 3

End with: "Reply with 1, 2, or 3 to send one of these, or tell me what you'd like to say instead, sir." """

GROUP_DRAFT_PROMPT = """You are G.A.R.V.I.S. drafting a message for a client group chat.
Return ONLY the message text — nothing else, no preamble, no labels."""

CLIENTS_REPORT_PROMPT = """You are G.A.R.V.I.S., writing sir's morning client report from the last 24 hours of Telegram activity across his client chats.

Your output is sent directly to Telegram with parse_mode=HTML. Use ONLY these tags: <b>, <i>, <code>. Never use <br>, <ul>, <li>, <p>, headings, or markdown — use plain newlines and the structure below. Escape any literal < > & from message content.

Structure the report EXACTLY like this:

🗞 <b>CLIENT REPORT</b>
<i>{date_line}</i>

▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔

<b>🎯 TOP PRIORITIES</b>
1. Most urgent item, with client name in <b>bold</b>
2. Second
3. Third
(max 3 — the things sir must handle first today)

▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔

Then ONE block per client chat that had meaningful activity, ordered most-urgent first:

<b>{Client / chat name}</b>
🔸 Needs — what the client needs from sir (omit line if none)
⏳ Pending — open items waiting on either side (omit if none)
❓ Questions — questions the client asked that are still unanswered (omit if none)

Leave one blank line between client blocks.

Rules:
- Be concise: each line one sentence, no filler. The whole report must be scannable in under a minute.
- Skip chats with only small talk, stickers, or auto-notices — do not mention them at all.
- If sir already answered a question or resolved an item within the window, do not list it.
- Quote exact figures, numbers, and dates when clients mention them.
- If NO chat has meaningful activity, return exactly: a short all-clear note in the same header style.
- End with: <i>All caught up, sir.</i>"""

# ── TIMEZONE / SCHEDULING HELPERS ─────────────────────────────────────────────
TIMEZONE_MAP = {
    "moscow":  MOSCOW_TZ,
    "msk":     MOSCOW_TZ,
    "russia":  MOSCOW_TZ,
    "pst":     TZ,
    "pacific": TZ,
    "la":      TZ,
    "utc":     pytz.UTC,
    "gmt":     pytz.UTC,
}

def parse_schedule_time(text):
    text_lower = text.lower()
    tz = MOSCOW_TZ
    for keyword, timezone in TIMEZONE_MAP.items():
        if keyword in text_lower:
            tz = timezone
            break
    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text_lower)
    if not time_match:
        return None, None
    hour   = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    ampm   = time_match.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and 1 <= hour <= 6:
        hour += 12
    now       = datetime.now(tz)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled <= now:
        scheduled += timedelta(days=1)
    return scheduled.astimezone(pytz.UTC), tz

def is_schedule_intent(text):
    patterns = [
        r"do\s+it\s+at", r"send\s+at", r"schedule", r"not\s+now",
        r"don'?t\s+send\s+now", r"do\s+not\s+send.*?now",
        r"send.*?later", r"send.*?at\s+\d",
        r"at\s+\d+\s*(am|pm)", r"\d+\s*(am|pm).*time",
    ]
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in patterns)

def tz_label(source_tz):
    if source_tz == MOSCOW_TZ:
        return "Moscow"
    if source_tz == TZ:
        return "PST"
    return "UTC"

# ── MEMORY ────────────────────────────────────────────────────────────────────
def load_memory():
    try:
        if Path(MEMORY_FILE).exists():
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "facts": {},
        "history": [],
        "active_group_chats": {},
        "pending_replies": {},
        "pending_draft_meta": {},
        "scheduled_jobs": [],
        "watch_rules": [],
        "monitored_groups": {},
        "pending_invoices": {},
        "invoice_counter": 0,
    }

def save_memory_data(data):
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f)

def save_memory_fact(key, value):
    memory = load_memory()
    memory["facts"][key] = value
    save_memory_data(memory)
    return f"Memory saved: {key} = {value}"

def add_to_history(role, content):
    memory = load_memory()
    memory["history"].append({"role": role, "content": content, "time": datetime.now().isoformat()})
    memory["history"] = memory["history"][-100:]
    save_memory_data(memory)

def get_recent_history(n=10):
    memory = load_memory()
    return memory["history"][-n:]

def get_memory_facts():
    memory = load_memory()
    facts = memory.get("facts", {})
    if not facts:
        return ""
    return "\n".join([f"- {k}: {v}" for k, v in facts.items()])

def get_pending_reply(user_id):
    memory = load_memory()
    return memory.get("pending_replies", {}).get(str(user_id))

def get_pending_draft_meta(user_id):
    memory = load_memory()
    return memory.get("pending_draft_meta", {}).get(str(user_id), {})

def set_pending_reply(user_id, draft, meta=None):
    memory = load_memory()
    if "pending_replies" not in memory:
        memory["pending_replies"] = {}
    memory["pending_replies"][str(user_id)] = draft
    if meta is not None:
        if "pending_draft_meta" not in memory:
            memory["pending_draft_meta"] = {}
        memory["pending_draft_meta"][str(user_id)] = meta
    save_memory_data(memory)

def clear_pending_reply(user_id):
    memory = load_memory()
    memory.get("pending_replies", {}).pop(str(user_id), None)
    memory.get("pending_draft_meta", {}).pop(str(user_id), None)
    save_memory_data(memory)

# ── FOLLOW-UP SYSTEM ──────────────────────────────────────────────────────────
def save_followup(fu_data: dict):
    memory = load_memory()
    if "follow_ups" not in memory:
        memory["follow_ups"] = []
    memory["follow_ups"].append(fu_data)
    save_memory_data(memory)

def get_pending_followups() -> list:
    memory = load_memory()
    return [fu for fu in memory.get("follow_ups", []) if fu.get("status") == "pending"]

def list_all_followups() -> str:
    memory = load_memory()
    followups = memory.get("follow_ups", [])
    pending   = [fu for fu in followups if fu.get("status") == "pending"]
    if not pending:
        return "No active follow-ups, sir."
    lines = []
    for i, fu in enumerate(pending, 1):
        sent_at  = datetime.fromisoformat(fu["sent_at"]).astimezone(TZ).strftime("%b %d %I:%M %p PST")
        deadline = (datetime.fromisoformat(fu["sent_at"]).astimezone(pytz.UTC)
                    + timedelta(hours=fu["followup_hours"])).astimezone(TZ).strftime("%b %d %I:%M %p PST")
        lines.append(
            f"{i}. *{fu['entity']}* — sent {sent_at}\n"
            f"   Follow-up due: {deadline}\n"
            f"   Reminder: {fu['reminder']}"
        )
    return "\n\n".join(lines)

def update_followup_status(fu_id: str, status: str):
    memory = load_memory()
    for fu in memory.get("follow_ups", []):
        if fu["id"] == fu_id:
            fu["status"] = status
            break
    save_memory_data(memory)

# ── REPLY MONITORING ──────────────────────────────────────────────────────────
def save_monitored_outgoing(data: dict):
    memory = load_memory()
    if "monitored_outgoing" not in memory:
        memory["monitored_outgoing"] = []
    memory["monitored_outgoing"].append(data)
    # Prune entries older than 7 days
    cutoff = (datetime.now(pytz.UTC) - timedelta(days=7)).isoformat()
    memory["monitored_outgoing"] = [
        m for m in memory["monitored_outgoing"]
        if m.get("sent_at", "") > cutoff
    ]
    save_memory_data(memory)

def get_monitored_outgoing() -> list:
    memory = load_memory()
    return [m for m in memory.get("monitored_outgoing", []) if m.get("status") == "waiting"]

def update_monitored_status(mon_id: str, status: str):
    memory = load_memory()
    for m in memory.get("monitored_outgoing", []):
        if m["id"] == mon_id:
            m["status"] = status
            break
    save_memory_data(memory)

def get_pending_contact_reply(user_id):
    memory = load_memory()
    return memory.get("pending_contact_replies", {}).get(str(user_id))

def set_pending_contact_reply(user_id, data: dict):
    memory = load_memory()
    if "pending_contact_replies" not in memory:
        memory["pending_contact_replies"] = {}
    memory["pending_contact_replies"][str(user_id)] = data
    save_memory_data(memory)

def clear_pending_contact_reply(user_id):
    memory = load_memory()
    memory.get("pending_contact_replies", {}).pop(str(user_id), None)
    save_memory_data(memory)

# ── LAST SENT TRACKING ────────────────────────────────────────────────────────
def save_last_sent(data: dict):
    memory = load_memory()
    memory["last_sent"] = data
    save_memory_data(memory)

def get_last_sent() -> dict:
    memory = load_memory()
    return memory.get("last_sent", {})

# ── TELETHON DIALOG RESOLVER ──────────────────────────────────────────────────
async def _resolve_dialog(name: str):
    """
    Search Telethon dialogs for the best match to `name`.
    Must be called while telethon_client is already connected (inside async with).
    Returns (entity_obj, display_name, is_group) on success.
    Raises ValueError with a helpful message on failure.
    """
    from telethon.tl.types import Channel, Chat

    name_lower = name.strip().lower()
    if is_blocked_chat(name_lower):
        raise ValueError(
            f"'{name}' is on the blocked-chat list, sir. I no longer assist with that chat."
        )
    exact   = []
    partial = []

    async for dialog in telethon_client.iter_dialogs(limit=300):
        dn       = (dialog.name or "").strip()
        if not dn:          # skip chats with no visible name
            continue
        if is_blocked_chat(dn):
            continue
        dn_lower = dn.lower()
        is_grp   = isinstance(dialog.entity, (Channel, Chat))
        if dn_lower == name_lower:
            exact.append((dialog.entity, dn, is_grp))
        elif name_lower in dn_lower or dn_lower in name_lower:
            partial.append((dialog.entity, dn, is_grp))

    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(f'"{p[1]}"' for p in partial[:6])
        raise ValueError(
            f"Multiple chats match '{name}': {names}. "
            "Please give me the exact name, sir."
        )

    # No match — collect word-based suggestions
    words = [w for w in name_lower.split() if len(w) > 2]
    suggestions = []
    if words:
        async for dialog in telethon_client.iter_dialogs(limit=300):
            dn = (dialog.name or "").strip()
            if is_blocked_chat(dn):
                continue
            if any(w in dn.lower() for w in words):
                suggestions.append(f'"{dn}"')
            if len(suggestions) >= 6:
                break

    if suggestions:
        raise ValueError(
            f"No chat named '{name}' found. Did you mean: {', '.join(suggestions)}?"
        )
    raise ValueError(
        f"No chat named '{name}' found. "
        "Use the exact name as it appears in your Telegram app, sir."
    )


def _get_bot_chat_id(entity_obj) -> int:
    """Convert a Telethon entity to the correct Bot API chat_id integer."""
    from telethon.tl.types import Channel, Chat
    if isinstance(entity_obj, Channel):
        return int(f"-100{entity_obj.id}")
    if isinstance(entity_obj, Chat):
        return -entity_obj.id
    return entity_obj.id  # User (private chat)

# ── WATCH RULES ───────────────────────────────────────────────────────────────
def get_watch_rules():
    memory = load_memory()
    return memory.get("watch_rules", [])

def save_watch_rule(rule):
    memory = load_memory()
    if "watch_rules" not in memory:
        memory["watch_rules"] = []
    memory["watch_rules"].append(rule)
    save_memory_data(memory)

def delete_watch_rule(index):
    memory = load_memory()
    rules = memory.get("watch_rules", [])
    if 0 <= index < len(rules):
        removed = rules.pop(index)
        memory["watch_rules"] = rules
        save_memory_data(memory)
        return removed
    return None

# ── APPLE CALENDAR (raw iCloud CalDAV — no third-party library) ───────────────
ICLOUD_BASE = "https://caldav.icloud.com"

def _discover_icloud_calendar():
    """
    Walk the CalDAV discovery chain and return the URL of the user's default calendar.
    Raises RuntimeError with a human-readable message on any failure.
    """
    auth = (ICLOUD_USERNAME, ICLOUD_APP_PASSWORD)

    # 1 ── principal URL
    r1 = requests.request(
        "PROPFIND", f"{ICLOUD_BASE}/",
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
        data=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            '<d:prop><d:current-user-principal/></d:prop>'
            '</d:propfind>'
        ),
        timeout=20,
        allow_redirects=True,
    )
    print(f"[iCloud] principal discovery: HTTP {r1.status_code}")
    if r1.status_code not in (200, 207):
        raise RuntimeError(
            f"iCloud authentication failed (HTTP {r1.status_code}). "
            "Check ICLOUD_USERNAME and ICLOUD_PASSWORD — make sure ICLOUD_PASSWORD "
            "is an app-specific password from appleid.apple.com, not your main password."
        )

    root1 = ET.fromstring(r1.content)
    principal_elem = root1.find(".//{DAV:}current-user-principal/{DAV:}href")
    if principal_elem is None or not principal_elem.text:
        raise RuntimeError("Could not find principal URL in iCloud response.")
    principal_path = principal_elem.text.strip()
    principal_url  = ICLOUD_BASE + principal_path if principal_path.startswith("/") else principal_path
    print(f"[iCloud] principal URL: {principal_url}")

    # 2 ── calendar home
    r2 = requests.request(
        "PROPFIND", principal_url,
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
        data=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><c:calendar-home-set/></d:prop>'
            '</d:propfind>'
        ),
        timeout=20,
    )
    print(f"[iCloud] calendar-home-set: HTTP {r2.status_code}")
    root2 = ET.fromstring(r2.content)
    home_elem = root2.find(".//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href")
    if home_elem is None or not home_elem.text:
        raise RuntimeError("Could not find calendar-home-set in iCloud response.")
    home_path = home_elem.text.strip()
    home_url  = ICLOUD_BASE + home_path if home_path.startswith("/") else home_path
    print(f"[iCloud] calendar home: {home_url}")

    # 3 ── pick the first real calendar (resourcetype contains <calendar/>)
    r3 = requests.request(
        "PROPFIND", home_url,
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        data=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:resourcetype/><d:displayname/></d:prop>'
            '</d:propfind>'
        ),
        timeout=20,
    )
    print(f"[iCloud] calendar list: HTTP {r3.status_code}")
    root3 = ET.fromstring(r3.content)

    for response in root3.findall("{DAV:}response"):
        href_elem = response.find("{DAV:}href")
        is_calendar = response.find(".//{urn:ietf:params:xml:ns:caldav}calendar")
        if href_elem is None or is_calendar is None:
            continue
        cal_path = href_elem.text.strip()
        cal_url  = ICLOUD_BASE + cal_path if cal_path.startswith("/") else cal_path
        # Skip the home itself and task/reminder collections
        name_elem    = response.find(".//{DAV:}displayname")
        display_name = name_elem.text.strip().lower() if (name_elem is not None and name_elem.text) else ""
        if cal_url.rstrip("/") == home_url.rstrip("/"):
            continue
        if "reminder" in display_name or "task" in display_name:
            continue
        print(f"[iCloud] using calendar: {display_name!r} → {cal_url}")
        return cal_url

    raise RuntimeError("No writable calendar found in iCloud.")


def create_icloud_event(title, date, time_str, timezone_str="America/Los_Angeles",
                         duration_minutes=60, notes=""):
    if not ICLOUD_USERNAME or not ICLOUD_APP_PASSWORD:
        return "[ERROR] iCloud credentials missing. Set ICLOUD_USERNAME and ICLOUD_PASSWORD in Railway."

    print(f"[iCloud] Creating: '{title}' on {date} at {time_str} ({timezone_str})")
    try:
        calendar_url = _discover_icloud_calendar()

        tz       = pytz.timezone(timezone_str)
        local_dt = None
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                local_dt = tz.localize(datetime.strptime(f"{date} {time_str}", fmt))
                break
            except ValueError:
                continue
        if local_dt is None:
            return f"[ERROR] Could not parse '{date} {time_str}'. Use YYYY-MM-DD and HH:MM."

        end_dt    = local_dt + timedelta(minutes=int(duration_minutes))
        event_uid = str(uuid.uuid4())
        ical_data = "\r\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//GARVIS//EN",
            "BEGIN:VEVENT",
            f"UID:{event_uid}",
            f"DTSTAMP:{datetime.now(pytz.UTC).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;TZID={timezone_str}:{local_dt.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={timezone_str}:{end_dt.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{title}",
            f"DESCRIPTION:{notes}",
            "END:VEVENT",
            "END:VCALENDAR",
        ])

        event_url = calendar_url.rstrip("/") + f"/{event_uid}.ics"
        auth      = (ICLOUD_USERNAME, ICLOUD_APP_PASSWORD)
        r = requests.put(
            event_url,
            auth=auth,
            headers={
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            },
            data=ical_data.encode("utf-8"),
            timeout=20,
        )
        print(f"[iCloud] PUT {event_url} → HTTP {r.status_code}")

        if r.status_code in (201, 204):
            return (
                f"✅ '{title}' has been added to your Apple Calendar "
                f"for {local_dt.strftime('%B %d at %I:%M %p %Z')}."
            )
        return (
            f"[ERROR] iCloud rejected the event (HTTP {r.status_code}). "
            f"Response: {r.text[:300]}"
        )

    except RuntimeError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        print(f"[iCloud] Unexpected exception: {e}")
        return f"[ERROR] Unexpected failure creating calendar event: {e}"


# ── /testcal COMMAND ──────────────────────────────────────────────────────────
async def handle_testcal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick sanity-check: creates a test event 1 hour from now."""
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    await update.message.reply_text("🧪 Testing Apple Calendar connection, sir…")
    now      = datetime.now(TZ)
    test_dt  = now + timedelta(hours=1)
    result   = create_icloud_event(
        title        = "G.A.R.V.I.S. Test Event",
        date         = test_dt.strftime("%Y-%m-%d"),
        time_str     = test_dt.strftime("%H:%M"),
        timezone_str = "America/Los_Angeles",
        duration_minutes = 15,
        notes        = "Automated test from G.A.R.V.I.S.",
    )
    await update.message.reply_text(result)


# ── MICROSOFT GRAPH (Outlook Calendar + Email) ────────────────────────────────
def get_outlook_access_token():
    if not OUTLOOK_CLIENT_ID or not OUTLOOK_TENANT_ID or not OUTLOOK_REFRESH_TOKEN:
        return None, "[ERROR] Outlook credentials not fully configured."
    data = {
        "client_id":     OUTLOOK_CLIENT_ID,
        "grant_type":    "refresh_token",
        "refresh_token": OUTLOOK_REFRESH_TOKEN,
        "scope":         "Calendars.ReadWrite Mail.ReadWrite Mail.Send offline_access",
    }
    if OUTLOOK_CLIENT_SECRET:
        data["client_secret"] = OUTLOOK_CLIENT_SECRET
    try:
        resp       = requests.post(
            f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}/oauth2/v2.0/token",
            data=data, timeout=15,
        )
        token_data   = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return None, f"[ERROR] Outlook token refresh failed: {token_data.get('error_description', token_data)}"
        return access_token, None
    except Exception as e:
        return None, f"[ERROR] Outlook token request failed: {e}"


def create_outlook_event(title, date, time_str, timezone_str="America/Los_Angeles",
                          duration_minutes=60, notes="", attendees=None):
    access_token, error = get_outlook_access_token()
    if error:
        return error
    try:
        tz       = pytz.timezone(timezone_str)
        local_dt = None
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                local_dt = tz.localize(datetime.strptime(f"{date} {time_str}", fmt))
                break
            except ValueError:
                continue
        if local_dt is None:
            return f"[ERROR] Could not parse '{date} {time_str}'. Use YYYY-MM-DD and HH:MM."

        end_dt = local_dt + timedelta(minutes=int(duration_minutes))
        body   = {
            "subject": title,
            "body": {"contentType": "Text", "content": notes or ""},
            "start": {"dateTime": local_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": timezone_str},
            "end":   {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": timezone_str},
        }
        if attendees:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in (attendees if isinstance(attendees, list) else [attendees])
            ]
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/events",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=body, timeout=15,
        )
        print(f"[Outlook] create event → HTTP {resp.status_code}")
        if resp.status_code in (200, 201):
            return (
                f"✅ '{title}' has been added to your Outlook Calendar "
                f"for {local_dt.strftime('%B %d at %I:%M %p %Z')}."
            )
        return f"[ERROR] Outlook Calendar rejected the event (HTTP {resp.status_code}): {resp.text[:300]}"
    except Exception as e:
        return f"[ERROR] Outlook Calendar failed: {e}"


def read_outlook_emails(count=10, filter_type="unread"):
    access_token, error = get_outlook_access_token()
    if error:
        return error
    try:
        params = {
            "$top":     min(int(count), 25),
            "$orderby": "receivedDateTime desc",
            "$select":  "subject,from,receivedDateTime,isRead,bodyPreview",
        }
        if filter_type == "unread":
            params["$filter"] = "isRead eq false"
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params, timeout=15,
        )
        if resp.status_code != 200:
            return f"[ERROR] Could not read emails (HTTP {resp.status_code}): {resp.text[:200]}"
        emails = resp.json().get("value", [])
        if not emails:
            label = "unread emails" if filter_type == "unread" else "emails"
            return f"No {label} found, sir."
        lines = []
        for i, email in enumerate(emails, 1):
            sender   = email.get("from", {}).get("emailAddress", {})
            name     = sender.get("name", "Unknown")
            address  = sender.get("address", "")
            subject  = email.get("subject", "(no subject)")
            preview  = email.get("bodyPreview", "")[:120]
            received = email.get("receivedDateTime", "")[:10]
            unread   = "" if email.get("isRead") else "🔵 "
            lines.append(f"{i}. {unread}*{subject}*\n   From: {name} <{address}> — {received}\n   {preview}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"[ERROR] Email read failed: {e}"


def send_outlook_email(to, subject, body):
    access_token, error = get_outlook_access_token()
    if error:
        return error
    try:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [
                    {"emailAddress": {"address": addr.strip()}}
                    for addr in (to if isinstance(to, list) else [to])
                ],
            },
            "saveToSentItems": True,
        }
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        print(f"[Outlook] sendMail → HTTP {resp.status_code}")
        if resp.status_code == 202:
            recipients = to if isinstance(to, str) else ", ".join(to)
            return f"✅ Email sent to {recipients}, sir."
        return f"[ERROR] Email send failed (HTTP {resp.status_code}): {resp.text[:300]}"
    except Exception as e:
        return f"[ERROR] Email send failed: {e}"


# ── TOOL EXECUTION ────────────────────────────────────────────────────────────
def execute_tool(tool_name, params):
    if tool_name == "WEB_SEARCH":
        query = params.get("query", "")
        try:
            resp    = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 5},
                timeout=10,
            )
            results = resp.json().get("results", [])
            return "\n".join([f"- {r['title']}: {r['content'][:200]}" for r in results[:3]])
        except Exception as e:
            return f"Search failed: {e}"

    elif tool_name == "GET_WEATHER":
        location = params.get("location", "")
        try:
            resp = requests.get(f"https://wttr.in/{location}?format=3", timeout=10)
            return resp.text
        except Exception as e:
            return f"Weather fetch failed: {e}"

    elif tool_name == "SAVE_MEMORY":
        return save_memory_fact(params.get("key", ""), params.get("value", ""))

    elif tool_name == "READ_TELEGRAM_CHAT":
        chat_name = params.get("chat_name", "").lower()
        for gid, data in group_logs.items():
            if chat_name in data["title"].lower():
                recent = data["messages"][-20:]
                return "\n".join(recent) or "No recent messages."
        return f"Chat '{chat_name}' not found in monitored groups."

    elif tool_name == "CREATE_CALENDAR_EVENT":
        return create_icloud_event(
            title            = params.get("title", "Untitled"),
            date             = params.get("date", ""),
            time_str         = params.get("time", "09:00"),
            timezone_str     = params.get("timezone", "America/Los_Angeles"),
            duration_minutes = int(params.get("duration_minutes", 60)),
            notes            = params.get("notes", ""),
        )

    elif tool_name == "CREATE_OUTLOOK_EVENT":
        return create_outlook_event(
            title            = params.get("title", "Untitled"),
            date             = params.get("date", ""),
            time_str         = params.get("time", "09:00"),
            timezone_str     = params.get("timezone", "America/Los_Angeles"),
            duration_minutes = int(params.get("duration_minutes", 60)),
            notes            = params.get("notes", ""),
            attendees        = params.get("attendees", []),
        )

    elif tool_name == "READ_OUTLOOK_EMAIL":
        return read_outlook_emails(
            count       = params.get("count", 10),
            filter_type = params.get("filter", "unread"),
        )

    elif tool_name == "SEND_OUTLOOK_EMAIL":
        return send_outlook_email(
            to      = params.get("to", ""),
            subject = params.get("subject", ""),
            body    = params.get("body", ""),
        )

    elif tool_name == "LIST_FOLLOWUPS":
        return list_all_followups()

    return f"Unknown tool: {tool_name}"


async def _execute_recall_message(bot=None) -> str:
    """Delete the last message sent by Jarvis on sir's behalf."""
    last = get_last_sent()
    if not last or not last.get("entity"):
        return "[ERROR] No sent message on record to recall, sir."
    try:
        if last.get("sent_via") == "bot":
            # Sent by bot — use Bot API delete (bots can always delete their own messages)
            if not bot:
                return "[ERROR] Bot reference unavailable. Please delete manually, sir."
            chat_id    = last.get("chat_id")
            message_id = last.get("message_id")
            if not chat_id or not message_id:
                return "[ERROR] Missing chat/message ID for recall."
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        else:
            # Sent as user via Telethon — use Telethon delete
            async with _tl:
                entity_obj, _, _ = await _resolve_dialog(last["entity"])
                await telethon_client.delete_messages(entity_obj, [last["message_id"]])
        save_last_sent({})  # clear record
        return f"✅ Message to *{last['entity']}* has been recalled and deleted, sir."
    except Exception as e:
        return f"[ERROR] Could not recall message: {e}"

# ── VOICE I/O (Whisper transcription + OpenAI TTS) ───────────────────────────
def _transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=f)
    return transcript.text

def _synthesize_speech(text: str) -> bytes:
    resp = openai_client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text[:1000],
        response_format="opus",
    )
    return resp.content

async def send_tts_reply(update: Update, text: str):
    if not openai_client:
        return
    try:
        clean = re.sub(r"[*_`~]", "", text)
        audio = await asyncio.get_event_loop().run_in_executor(None, _synthesize_speech, clean)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            await update.message.reply_voice(voice=f)
        os.unlink(tmp_path)
    except Exception as e:
        print(f"TTS error: {e}")

# ── SCHEDULED MESSAGE JOB ─────────────────────────────────────────────────────
async def send_scheduled_message(context):
    data     = context.job.data
    job_id   = data["job_id"]
    owner_id = data["owner_id"]
    memory   = load_memory()
    jobs     = memory.get("scheduled_jobs", [])
    job      = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return
    message     = job["message"]
    method      = job.get("method", "telethon")
    destination = job.get("destination", "destination")
    try:
        entity_name = (job.get("telethon_entity") or job.get("destination") or "").lower()
        if "xeebi noc" in entity_name:
            await context.bot.send_message(chat_id=XEEBI_NOC_CHAT_ID, text=message)
        elif method == "telethon":
            entity = job.get("telethon_entity")
            async with _tl:
                await telethon_client.send_message(entity, message)
        else:
            chat_id   = job["chat_id"]
            thread_id = job.get("thread_id")
            kwargs    = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await context.bot.send_message(**kwargs)
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"✅ Scheduled message delivered to *{destination}*, sir.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"⚠️ Failed to deliver scheduled message to *{destination}*: {e}",
            parse_mode="Markdown",
        )
    finally:
        memory = load_memory()
        memory["scheduled_jobs"] = [j for j in memory.get("scheduled_jobs", []) if j["id"] != job_id]
        save_memory_data(memory)

async def _send_pending_draft(context, draft_text, pending_meta, active_group):
    if pending_meta and pending_meta.get("type") == "telethon":
        entity_name = pending_meta.get("entity", "")
        if is_blocked_chat(entity_name):
            raise ValueError(
                f"'{entity_name}' is on the blocked-chat list, sir. Nothing was sent."
            )
        if "xeebi noc" in entity_name.lower():
            await context.bot.send_message(chat_id=XEEBI_NOC_CHAT_ID, text=draft_text)
        else:
            async with _tl:
                entity_obj, display_name, is_group = await _resolve_dialog(entity_name)
                if not is_group:
                    sent = await telethon_client.send_message(entity_obj, draft_text)
                    save_last_sent({
                        "entity":     display_name,
                        "message":    draft_text,
                        "message_id": sent.id,
                        "sent_via":   "telethon",
                        "sent_at":    datetime.now(pytz.UTC).isoformat(),
                    })
            if is_group:
                bot_sent = await context.bot.send_message(
                    chat_id=_get_bot_chat_id(entity_obj), text=draft_text
                )
                save_last_sent({
                    "entity":     display_name,
                    "message":    draft_text,
                    "message_id": bot_sent.message_id,
                    "chat_id":    _get_bot_chat_id(entity_obj),
                    "sent_via":   "bot",
                    "sent_at":    datetime.now(pytz.UTC).isoformat(),
                })
    elif active_group:
        chat_id   = active_group.get("chat_id")
        thread_id = active_group.get("thread_id")
        kwargs    = {"chat_id": chat_id, "text": draft_text, "parse_mode": "Markdown"}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        await context.bot.send_message(**kwargs)
    else:
        raise ValueError("No destination recorded for this draft.")

def _register_scheduled_job(context, job_id, delay, job_data):
    memory = load_memory()
    if "scheduled_jobs" not in memory:
        memory["scheduled_jobs"] = []
    memory["scheduled_jobs"].append(job_data)
    save_memory_data(memory)
    context.job_queue.run_once(
        send_scheduled_message,
        when=delay,
        data={"job_id": job_id, "owner_id": OWNER_TELEGRAM_ID},
        name=job_id,
    )

# ── /scheduled COMMAND ────────────────────────────────────────────────────────
async def handle_scheduled_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    memory = load_memory()
    jobs   = memory.get("scheduled_jobs", [])
    if not jobs:
        await update.message.reply_text("No scheduled messages queued, sir.")
        return
    lines = []
    for i, job in enumerate(jobs, 1):
        send_at_utc    = datetime.fromisoformat(job["scheduled_utc"])
        send_at_moscow = send_at_utc.astimezone(MOSCOW_TZ).strftime("%b %d %I:%M %p MSK")
        send_at_pst    = send_at_utc.astimezone(TZ).strftime("%I:%M %p PST")
        preview        = job["message"][:60] + ("..." if len(job["message"]) > 60 else "")
        lines.append(
            f"*{i}.* To: {job['destination']}\n"
            f"   At: {send_at_moscow} / {send_at_pst}\n"
            f"   Message: _{preview}_"
        )
    await update.message.reply_text(
        "🕐 *Scheduled Messages:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
    )

# ── BRIEFING ──────────────────────────────────────────────────────────────────
async def send_briefing(bot, chat_id, chat_title, messages):
    if not messages:
        return
    if is_blocked_chat(chat_title):
        return
    conversation = "\n".join(messages[-100:])
    response     = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=GROUP_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": f"Group: {chat_title}\n\nMessages:\n{conversation}"}],
    )
    summary = response.content[0].text
    await bot.send_message(
        chat_id=OWNER_TELEGRAM_ID,
        text=f"📋 *Briefing — {chat_title}*\n\n{summary}",
        parse_mode="Markdown",
    )
    memory = load_memory()
    memory["active_group_chats"][str(OWNER_TELEGRAM_ID)] = {
        "chat_id":         chat_id,
        "chat_title":      chat_title,
        "recent_messages": conversation,
    }
    save_memory_data(memory)

async def scheduled_briefing(context):
    for gid, data in group_logs.items():
        if data["messages"]:
            await send_briefing(context.bot, gid, data["title"], data["messages"])

# ── /brief COMMAND ────────────────────────────────────────────────────────────
async def handle_brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    if not group_logs:
        await update.message.reply_text("No active group chats being monitored yet, sir.")
        return
    for gid, data in group_logs.items():
        if data["messages"]:
            await send_briefing(context.bot, gid, data["title"], data["messages"])

# ── /groups COMMAND ───────────────────────────────────────────────────────────
async def handle_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    memory    = load_memory()
    monitored = memory.get("monitored_groups", {})
    if not monitored:
        await update.message.reply_text("No groups being monitored yet, sir.")
        return
    lines = [f"• {title}" for title in monitored.values()]
    await update.message.reply_text(
        "📡 *Monitored Groups:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )

# ── /watch COMMANDS ───────────────────────────────────────────────────────────
async def handle_watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    watch_setup_state[user_id] = {"step": 1, "rule": {}}
    await update.message.reply_text(
        "🔍 *Setting up a Watch Rule*\n\n"
        "*Step 1/4* — Which group chat should I monitor?\n"
        "_(Type the chat name, e.g. 'Xeebi Toll Free Support')_",
        parse_mode="Markdown",
    )

async def handle_watches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    rules = get_watch_rules()
    if not rules:
        await update.message.reply_text("No active watch rules, sir.")
        return
    lines = []
    for i, rule in enumerate(rules):
        lines.append(
            f"*{i+1}.* Chat: {rule['chat_name']}\n"
            f"   Person: {rule['person']}\n"
            f"   Keyword: {rule['keyword']}\n"
            f"   Action: {rule['action']}\n"
            f"   Notify: {rule['notify_contact']}"
        )
    await update.message.reply_text(
        "📋 *Active Watch Rules:*\n\n" + "\n\n".join(lines) +
        "\n\nTo delete a rule, type `/deletewatch <number>`",
        parse_mode="Markdown",
    )

async def handle_deletewatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /deletewatch <number>  (use /watches to see rule numbers)")
        return
    index   = int(args[0]) - 1
    removed = delete_watch_rule(index)
    if removed:
        await update.message.reply_text(
            f"✅ Watch rule deleted, sir: monitoring '{removed['keyword']}' in {removed['chat_name']}"
        )
    else:
        await update.message.reply_text("Rule not found, sir.")

async def process_watch_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    state = watch_setup_state[user_id]
    step  = state["step"]
    rule  = state["rule"]
    if step == 1:
        rule["chat_name"] = text
        state["step"] = 2
        await update.message.reply_text(
            f"✅ Chat: *{text}*\n\n"
            "*Step 2/4* — Whose message should trigger this?\n"
            "_(Type the person's first name, e.g. 'Dmitry')_",
            parse_mode="Markdown",
        )
    elif step == 2:
        rule["person"] = text
        state["step"]  = 3
        await update.message.reply_text(
            f"✅ Person: *{text}*\n\n"
            "*Step 3/4* — What keyword should I watch for?\n"
            "_(e.g. 'ready', 'done', 'complete')_",
            parse_mode="Markdown",
        )
    elif step == 3:
        rule["keyword"] = text
        state["step"]   = 4
        await update.message.reply_text(
            f"✅ Keyword: *{text}*\n\n"
            "*Step 4/4* — Who should I notify and what should I say?\n"
            "_(e.g. 'Message Bruce: Dmitry said the shipment is ready')_",
            parse_mode="Markdown",
        )
    elif step == 4:
        rule["action"] = text
        action_lower   = text.lower()
        if "message " in action_lower:
            parts = text.split("message ", 1)
            rule["notify_contact"] = parts[1].split(":")[0].strip() if len(parts) > 1 else "unknown"
        else:
            rule["notify_contact"] = "unknown"
        save_watch_rule(rule)
        del watch_setup_state[user_id]
        await update.message.reply_text(
            f"✅ *Watch Rule Active*, sir!\n\n"
            f"📡 Monitoring: *{rule['chat_name']}*\n"
            f"👤 Person: *{rule['person']}*\n"
            f"🔑 Keyword: *{rule['keyword']}*\n"
            f"📨 When triggered: {rule['action']}\n"
            f"📬 Notify: *{rule['notify_contact']}*\n\n"
            f"I'll alert you and fire the message automatically, sir.",
            parse_mode="Markdown",
        )

# ── INVOICE FLOW ──────────────────────────────────────────────────────────────
async def handle_invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_title      = update.message.chat.title or "this group"
    user_first_name = update.message.from_user.first_name or "there"
    context.user_data["invoice_chat_title"]  = chat_title
    context.user_data["invoice_client_name"] = user_first_name
    # Remember exactly where the request came from so /sent can notify them back.
    context.user_data["invoice_chat_id"]   = update.message.chat_id
    context.user_data["invoice_thread_id"] = update.message.message_thread_id
    context.user_data["invoice_user_id"]   = update.message.from_user.id
    await update.message.reply_text(
        f"Hi {user_first_name}! 👋 How much would you like to invoice for?"
    )
    return ASKING_AMOUNT

async def handle_invoice_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_text = update.message.text.strip()
    chat_title  = context.user_data.get("invoice_chat_title", "the group")
    await update.message.reply_text("Got it! I'll request your invoice right away. 🙏")

    invoice_id = _next_invoice_id()

    invoice_message = (
        f"Hello team! 👋 Can we please invoice *{_md_escape(chat_title)}* "
        f"for the amount of *{_md_escape(amount_text)}*? Thank you! 🙏\n\n"
        f"`{invoice_id}` — reply to this message with /sent once it's emailed."
    )
    invoice_message_plain = (
        f"Hello team! 👋 Can we please invoice {chat_title} "
        f"for the amount of {amount_text}? Thank you! 🙏"
    )
    thread_msg = await context.bot.send_message(
        chat_id=XEEBI_SALES_GROUP_ID,
        message_thread_id=INVOICING_THREAD_ID,
        text=invoice_message,
        parse_mode="Markdown",
    )

    _save_pending_invoice({
        "id":                 invoice_id,
        "status":             "pending",
        "chat_title":         chat_title,
        "amount":             amount_text,
        "requester_chat_id":  context.user_data.get("invoice_chat_id"),
        "requester_thread_id": context.user_data.get("invoice_thread_id"),
        "requester_user_id":  context.user_data.get("invoice_user_id"),
        "requester_name":     context.user_data.get("invoice_client_name", "there"),
        "requested_at":       datetime.now(TZ).isoformat(),
        "thread_message_id":  thread_msg.message_id,
    })
    if "global telecom" in chat_title.lower():
        try:
            async with _tl:
                async for dialog in telethon_client.iter_dialogs():
                    if UPM_NEWPORT_CHAT.lower() in dialog.name.lower():
                        await telethon_client.send_message(
                            dialog.entity,
                            invoice_message_plain,
                        )
                        break
        except Exception as e:
            print(f"UPM NEWPORT send failed: {e}")
    return ConversationHandler.END

async def handle_invoice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Invoice cancelled.")
    return ConversationHandler.END

# ── INVOICE TRACKING (/sent) ──────────────────────────────────────────────────
def _md_escape(text):
    """Neutralise Markdown control chars so client names can't break parsing."""
    return re.sub(r"([*_`\[\]])", r"\\\1", str(text))

def _next_invoice_id():
    data  = load_memory()
    count = int(data.get("invoice_counter", 0)) + 1
    data["invoice_counter"] = count
    save_memory_data(data)
    return f"INV-{count:04d}"

def _save_pending_invoice(record):
    data = load_memory()
    data.setdefault("pending_invoices", {})[record["id"]] = record
    _prune_old_invoices(data)
    save_memory_data(data)

def _update_invoice(invoice_id, updates):
    data = load_memory()
    invoices = data.setdefault("pending_invoices", {})
    if invoice_id in invoices:
        invoices[invoice_id].update(updates)
        save_memory_data(data)

def _prune_old_invoices(data, days=30):
    """Keep sent invoices around briefly so late replies still resolve, then drop them."""
    invoices = data.get("pending_invoices", {})
    cutoff   = datetime.now(TZ) - timedelta(days=days)
    for inv_id in list(invoices.keys()):
        inv = invoices[inv_id]
        if inv.get("status") != "sent":
            continue
        try:
            if datetime.fromisoformat(inv.get("sent_at", "")) < cutoff:
                del invoices[inv_id]
        except Exception:
            pass

def _digits(value):
    return re.sub(r"\D", "", str(value))

def _resolve_invoice(update, context, invoices):
    """
    Work out which invoice /sent refers to, in order of confidence:
      1. the message being replied to
      2. an explicit ID argument (INV-0042 / 42 / #42)
      3. the only outstanding request
    Returns (invoice, error_message).
    """
    msg = update.message

    if msg.reply_to_message:
        target_id = msg.reply_to_message.message_id
        for inv in invoices.values():
            if inv.get("thread_message_id") == target_id:
                return inv, None

    if context.args:
        wanted = _digits(context.args[0])
        if wanted:
            for inv in invoices.values():
                existing = _digits(inv.get("id", ""))
                if not existing:
                    continue  # skip malformed records rather than crashing
                if existing == wanted.zfill(4) or int(existing) == int(wanted):
                    return inv, None
        return None, (
            f"I couldn't find an invoice matching `{_md_escape(context.args[0])}`. "
            "Use /pending to see what's outstanding."
        )

    outstanding = [i for i in invoices.values() if i.get("status") == "pending"]

    if len(outstanding) == 1:
        return outstanding[0], None

    if not outstanding:
        return None, "There are no outstanding invoice requests right now. ✅"

    outstanding.sort(key=lambda i: i.get("requested_at", ""))
    listing = "\n".join(
        f"• `{i['id']}` — {_md_escape(i['chat_title'])} ({_md_escape(i['amount'])})"
        for i in outstanding
    )
    return None, (
        f"There are {len(outstanding)} invoices outstanding, so I'm not sure which one you mean:\n\n"
        f"{listing}\n\n"
        "Reply directly to the request, or use `/sent INV-0042`."
    )

async def handle_sent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Invoice team marks an invoice as emailed; the client who asked gets notified."""
    msg = update.message
    if not msg:
        return

    in_invoicing_thread = (
        msg.chat_id == XEEBI_SALES_GROUP_ID
        and msg.message_thread_id == INVOICING_THREAD_ID
    )
    is_owner_dm = (
        msg.chat.type == "private"
        and msg.from_user.id == OWNER_TELEGRAM_ID
    )
    if not (in_invoicing_thread or is_owner_dm):
        return  # silently ignore everywhere else

    data     = load_memory()
    invoices = data.get("pending_invoices", {})

    invoice, error = _resolve_invoice(update, context, invoices)
    if error:
        await msg.reply_text(error, parse_mode="Markdown")
        return

    if invoice.get("status") == "sent":
        await msg.reply_text(
            f"`{invoice['id']}` was already marked sent by "
            f"{_md_escape(invoice.get('sent_by', 'someone'))} "
            f"({_format_invoice_age(invoice.get('sent_at'))}).",
            parse_mode="Markdown",
        )
        return

    sender_name = msg.from_user.first_name or "the team"
    client_chat = invoice.get("requester_chat_id")

    notification = (
        f"📧 Good news, {invoice.get('requester_name', 'there')}! "
        f"Our team has just emailed your invoice for {invoice.get('amount', '')}.\n\n"
        "Please check your inbox — if it's not there, have a quick look in your spam folder. "
        "Let us know if anything doesn't look right. 🙏"
    )

    notified = False
    failure  = ""
    if client_chat:
        try:
            await context.bot.send_message(
                chat_id=client_chat,
                message_thread_id=invoice.get("requester_thread_id"),
                text=notification,
            )
            notified = True
        except Exception as e:
            failure = str(e)
    else:
        failure = "no origin chat was recorded for this request"

    _update_invoice(invoice["id"], {
        "status":         "sent",
        "sent_by":        sender_name,
        "sent_at":        datetime.now(TZ).isoformat(),
        "client_notified": notified,
        "notify_error":   failure,
    })

    if notified:
        await msg.reply_text(
            f"✅ `{invoice['id']}` marked as sent — "
            f"*{_md_escape(invoice['chat_title'])}* has been notified.",
            parse_mode="Markdown",
        )
    else:
        await msg.reply_text(
            f"⚠️ `{invoice['id']}` is marked as sent, but I couldn't reach "
            f"*{_md_escape(invoice['chat_title'])}*: {_md_escape(failure)}\n\n"
            "You may need to let them know directly.",
            parse_mode="Markdown",
        )

def _format_invoice_age(iso_timestamp):
    try:
        stamp = datetime.fromisoformat(iso_timestamp)
    except Exception:
        return "unknown time"
    delta = datetime.now(TZ) - stamp
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = int(delta.total_seconds() // 3600)
    if hours >= 1:
        return f"{hours}h ago"
    return f"{max(1, int(delta.total_seconds() // 60))}m ago"

async def handle_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List invoice requests that haven't been marked sent yet."""
    msg = update.message
    if not msg:
        return

    in_invoicing_thread = (
        msg.chat_id == XEEBI_SALES_GROUP_ID
        and msg.message_thread_id == INVOICING_THREAD_ID
    )
    is_owner_dm = (
        msg.chat.type == "private"
        and msg.from_user.id == OWNER_TELEGRAM_ID
    )
    if not (in_invoicing_thread or is_owner_dm):
        return

    invoices    = load_memory().get("pending_invoices", {})
    outstanding = [i for i in invoices.values() if i.get("status") == "pending"]

    if not outstanding:
        await msg.reply_text("No outstanding invoice requests. All clear! ✅")
        return

    outstanding.sort(key=lambda i: i.get("requested_at", ""))
    lines = [
        f"• `{i['id']}` — *{_md_escape(i['chat_title'])}* for "
        f"{_md_escape(i['amount'])} _(requested {_format_invoice_age(i.get('requested_at'))})_"
        for i in outstanding
    ]
    await msg.reply_text(
        f"*Outstanding invoice requests ({len(outstanding)}):*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )

# ── GROUP MESSAGES ────────────────────────────────────────────────────────────
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id    = update.message.chat_id
    chat_title = update.message.chat.title or "Group Chat"
    user_id    = update.message.from_user.id
    sender     = update.message.from_user.first_name or "Unknown"
    text       = update.message.text
    if is_blocked_chat(chat_title):
        return
    if user_id == OWNER_TELEGRAM_ID:
        return
    if chat_id not in group_logs:
        group_logs[chat_id] = {"title": chat_title, "messages": []}
        memory = load_memory()
        if "monitored_groups" not in memory:
            memory["monitored_groups"] = {}
        memory["monitored_groups"][str(chat_id)] = chat_title
        save_memory_data(memory)
    timestamp = datetime.now(TZ).strftime("%b %d %I:%M%p")
    group_logs[chat_id]["messages"].append(f"[{timestamp}] {sender}: {text}")
    group_logs[chat_id]["messages"] = group_logs[chat_id]["messages"][-500:]
    for rule in get_watch_rules():
        if (
            rule["chat_name"].lower() in chat_title.lower()
            and rule["person"].lower() in sender.lower()
            and rule["keyword"].lower() in text.lower()
        ):
            draft_response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=256,
                system="You are G.A.R.V.I.S. Draft a short professional notification message.",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Watch rule triggered: {rule['action']}.\n"
                        f"Trigger message: '{text}' from {sender} in {chat_title}.\n"
                        f"Draft a message to {rule['notify_contact']}."
                    ),
                }],
            )
            notification = draft_response.content[0].text.strip()
            await context.bot.send_message(
                chat_id=OWNER_TELEGRAM_ID,
                text=(
                    f"🔔 *Watch Rule Triggered*\n\n"
                    f"In *{chat_title}*, {sender} said: _{text}_\n\n"
                    f"📨 Sending to {rule['notify_contact']}:\n\n{notification}"
                ),
                parse_mode="Markdown",
            )

# ── PRIVATE MESSAGES ──────────────────────────────────────────────────────────
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return
    user_message  = update.message.text
    memory        = load_memory()
    pending_draft = get_pending_reply(user_id)
    pending_meta  = get_pending_draft_meta(user_id)
    active_group  = memory.get("active_group_chats", {}).get(str(user_id))

    # ── WATCH SETUP ──────────────────────────────────────────────────────────
    if user_id in watch_setup_state:
        await process_watch_setup(update, context, user_id, user_message)
        return

    # ── PENDING CONTACT REPLY FLOW ────────────────────────────────────────────
    pending_contact = get_pending_contact_reply(user_id)
    if pending_contact:
        msg_lower = user_message.strip().lower()
        entity    = pending_contact["entity"]
        is_group  = pending_contact.get("is_group", False)

        if msg_lower in ("no", "cancel", "skip", "ignore", "dismiss"):
            clear_pending_contact_reply(user_id)
            await update.message.reply_text("Understood, sir. No reply sent.")
            return

        # Pick a numbered suggestion or use custom text
        if msg_lower in ("1", "2", "3"):
            lines = [
                l.strip() for l in pending_contact["suggestions"].split("\n") if l.strip()
            ]
            idx        = int(msg_lower) - 1
            reply_text = lines[idx] if idx < len(lines) else lines[0]
            reply_text = re.sub(r"^\d+[\.\)]\s*", "", reply_text).strip()
        else:
            reply_text = user_message.strip()

        try:
            if is_group and pending_contact.get("bot_chat_id"):
                await context.bot.send_message(
                    chat_id=pending_contact["bot_chat_id"], text=reply_text
                )
            else:
                async with _tl:
                    entity_obj, _, _ = await _resolve_dialog(entity)
                    await telethon_client.send_message(entity_obj, reply_text)
            clear_pending_contact_reply(user_id)
            via = "as the bot" if is_group else "as you"
            await update.message.reply_text(
                f"✅ Reply sent to *{entity}* ({via}), sir.", parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not send reply: {e}")
        return

    # ── PENDING DRAFT FLOW ────────────────────────────────────────────────────
    if pending_draft:
        msg_lower = user_message.lower().strip()

        _clean = re.sub(r"[^a-z\s]", "", msg_lower).strip()
        if _clean in ("yes", "y", "yep", "yup", "sure", "ok", "okay", "send",
                      "confirm", "send it", "yes send it", "do it", "go ahead"):
            try:
                await _send_pending_draft(context, pending_draft, pending_meta, active_group)
                await update.message.reply_text("Message sent, sir. ✅")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Could not send: {e}")
            clear_pending_reply(user_id)
            return

        if msg_lower in ("no", "cancel", "discard", "stop"):
            await update.message.reply_text("Message discarded, sir.")
            clear_pending_reply(user_id)
            return

        if is_schedule_intent(user_message):
            scheduled_utc, source_tz = parse_schedule_time(user_message)
            if scheduled_utc:
                delay        = max((scheduled_utc - datetime.now(pytz.UTC)).total_seconds(), 1)
                label        = tz_label(source_tz)
                display_time = scheduled_utc.astimezone(source_tz).strftime("%I:%M %p")
                if pending_meta and pending_meta.get("entity"):
                    destination = pending_meta["entity"]
                elif active_group:
                    destination = active_group.get("chat_title", "the group")
                else:
                    destination = "the destination"
                job_id   = f"job_{int(datetime.now().timestamp())}"
                job_data = {
                    "id":            job_id,
                    "message":       pending_draft,
                    "destination":   destination,
                    "scheduled_utc": scheduled_utc.isoformat(),
                    "method":        "telethon" if (pending_meta and pending_meta.get("type") == "telethon") else "bot",
                }
                if pending_meta and pending_meta.get("type") == "telethon":
                    job_data["telethon_entity"] = pending_meta.get("entity")
                elif active_group:
                    job_data["chat_id"]   = active_group.get("chat_id")
                    job_data["thread_id"] = active_group.get("thread_id")
                _register_scheduled_job(context, job_id, delay, job_data)
                clear_pending_reply(user_id)
                await update.message.reply_text(
                    f"✅ Scheduled, sir. I'll send that to *{destination}* at "
                    f"*{display_time} {label}*.\n\nUse /scheduled to view all queued messages.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "I couldn't parse the time, sir. "
                    "Please specify like '9am Moscow time' or '9:00 PST'."
                )
            return

        if active_group:
            context_text = active_group.get("recent_messages", "")
            chat_title   = active_group.get("chat_title", "the group")
        else:
            context_text = ""
            chat_title   = pending_meta.get("entity", "the destination") if pending_meta else "the destination"
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=512,
            system=GROUP_DRAFT_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Current draft:\n{pending_draft}\n\n"
                    f"Context:\n{context_text}\n\n"
                    f"Sir's revision instruction: {user_message}\n\n"
                    f"Revise the draft for {chat_title}. Return ONLY the revised message."
                ),
            }],
        )
        new_draft = response.content[0].text.strip()
        set_pending_reply(user_id, new_draft, meta=pending_meta)
        await update.message.reply_text(
            f"📝 *Updated draft:*\n\n{new_draft}\n\n"
            "Reply *yes* to send immediately, *schedule [time] [timezone]* to schedule, "
            "or tell me what to change.",
            parse_mode="Markdown",
        )
        return

    # ── GROUP REPLY SELECTION (after a briefing) ──────────────────────────────
    if active_group:
        msg_lower    = user_message.strip().lower()
        context_text = active_group.get("recent_messages", "")
        chat_title   = active_group.get("chat_title", "the group")
        if msg_lower in ("1", "2", "3"):
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=GROUP_DRAFT_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"User selected reply option {msg_lower}.\n"
                        f"Conversation:\n{context_text}\n\n"
                        f"Draft the selected reply for {chat_title}."
                    ),
                }],
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Ready to send:*\n\n{draft}\n\n"
                "Reply *yes* to send immediately, *schedule [time] [timezone]* to schedule, "
                "or tell me what to change.",
                parse_mode="Markdown",
            )
            return
        if any(p in msg_lower for p in ("tell them", "say", "respond", "reply with", "send")):
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=GROUP_DRAFT_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Conversation:\n{context_text}\n\n"
                        f"Sir's instruction: {user_message}\n\n"
                        f"Draft a reply for {chat_title}."
                    ),
                }],
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Draft:*\n\n{draft}\n\n"
                "Reply *yes* to send immediately, *schedule [time] [timezone]* to schedule, "
                "or tell me what to change.",
                parse_mode="Markdown",
            )
            return

    # ── GENERAL CLAUDE CONVERSATION ───────────────────────────────────────────
    history  = get_recent_history(20)
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": user_message})
    facts     = get_memory_facts()
    last_sent = get_last_sent()
    system    = SYSTEM_PROMPT + (f"\n\nKnown facts about sir:\n{facts}" if facts else "")
    if last_sent and last_sent.get("entity"):
        sent_at_str = last_sent.get("sent_at", "")[:19].replace("T", " ")
        system += (
            f"\n\nLAST MESSAGE DISPATCHED: You sent \"{last_sent['message'][:120]}\" "
            f"to {last_sent['entity']} at {sent_at_str} UTC via {last_sent['sent_via']}. "
            "If sir asks about this, confirm it honestly. Use RECALL_MESSAGE if sir wants to delete it."
        )
    add_to_history("user", user_message)
    reply = ""
    for _ in range(5):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text.strip()

        # ── Detect draft with destination metadata ──
        dest_match = re.search(r"<DEST>(.*?)</DEST>", reply, re.DOTALL)
        if dest_match:
            try:
                dest_data  = json.loads(dest_match.group(1).strip())
                display    = reply[: reply.index("<DEST>")].strip()
                draft_text = re.sub(r"^📝\s*\*Draft:\*\s*\n+", "", display, flags=re.IGNORECASE).strip()

                # Resolve NOW so user sees the EXACT chat before confirming
                entity_name   = dest_data.get("entity", "")
                resolved_name = entity_name
                if entity_name and dest_data.get("type") == "telethon":
                    try:
                        async with _tl:
                            _, resolved_name, _ = await _resolve_dialog(entity_name)
                        dest_data["entity"] = resolved_name
                    except ValueError as ve:
                        await update.message.reply_text(f"⚠️ {ve}", parse_mode="Markdown")
                        add_to_history("assistant", reply)
                        return
                    except Exception:
                        pass

                set_pending_reply(user_id, draft_text, meta=dest_data)
                await update.message.reply_text(
                    f"📝 *Draft for {resolved_name}:*\n\n{draft_text}\n\n"
                    "Reply *yes* to send immediately, "
                    "*schedule [time] [timezone]* to schedule, or tell me what to change.",
                    parse_mode="Markdown",
                )
            except Exception:
                await update.message.reply_text(
                    reply.replace(dest_match.group(0), "").strip(),
                    parse_mode="Markdown",
                )
            add_to_history("assistant", reply)
            return

        # ── Tool call ──
        tool_match = re.search(r"<TOOL>\s*(\{.*?\})\s*</TOOL>", reply, re.DOTALL)
        if tool_match:
            try:
                tool_call = json.loads(tool_match.group(1).strip())
                tool_name = tool_call.get("tool")
                params    = tool_call.get("params", {})

                # ── Async Telethon tools ──────────────────────────────────────
                if tool_name == "RECALL_MESSAGE":
                    tool_result = await _execute_recall_message(bot=context.bot)
                elif tool_name in ("SEND_TELEGRAM", "SEND_AND_FOLLOWUP"):
                    entity_name = params.get("entity", "")
                    message     = params.get("message", "")
                    try:
                        async with _tl:
                            entity_obj, display_name, is_group = await _resolve_dialog(entity_name)
                            if not is_group:
                                sent = await telethon_client.send_message(entity_obj, message)
                                save_last_sent({
                                    "entity":     display_name,
                                    "message":    message,
                                    "message_id": sent.id,
                                    "sent_via":   "telethon",
                                    "sent_at":    datetime.now(pytz.UTC).isoformat(),
                                })
                        if is_group:
                            bot_sent = await context.bot.send_message(
                                chat_id=_get_bot_chat_id(entity_obj), text=message
                            )
                            save_last_sent({
                                "entity":     display_name,
                                "message":    message,
                                "message_id": bot_sent.message_id,
                                "chat_id":    _get_bot_chat_id(entity_obj),
                                "sent_via":   "bot",
                                "sent_at":    datetime.now(pytz.UTC).isoformat(),
                            })
                        # Start reply monitoring
                        save_monitored_outgoing({
                            "id":           f"mon_{int(datetime.now().timestamp())}",
                            "entity":       display_name,
                            "is_group":     is_group,
                            "bot_chat_id":  _get_bot_chat_id(entity_obj) if is_group else None,
                            "message_sent": message,
                            "sent_at":      datetime.now(pytz.UTC).isoformat(),
                            "status":       "waiting",
                        })
                        if tool_name == "SEND_AND_FOLLOWUP":
                            fu_id = f"fu_{int(datetime.now().timestamp())}"
                            save_followup({
                                "id":             fu_id,
                                "entity":         display_name,
                                "message_sent":   message,
                                "sent_at":        datetime.now(pytz.UTC).isoformat(),
                                "followup_hours": int(params.get("followup_hours", 48)),
                                "reminder":       params.get("reminder", f"Follow up with {display_name}"),
                                "status":         "pending",
                            })
                            via = "as the bot" if is_group else "as you"
                            tool_result = (
                                f"✅ Message sent to *{display_name}* ({via}) and follow-up set for "
                                f"{params.get('followup_hours', 48)} hours, sir."
                            )
                        else:
                            via = "as the bot" if is_group else "as you"
                            tool_result = f"✅ Message sent to *{display_name}* ({via}), sir."
                    except ValueError as ve:
                        tool_result = f"[ERROR] {ve}"
                    except Exception as te:
                        tool_result = f"[ERROR] Could not send to {entity_name}: {te}"
                else:
                    tool_result = execute_tool(tool_name, params)

                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user",      "content": f"[TOOL RESULT for {tool_name}]\n{tool_result}"})
                continue
            except Exception as e:
                reply = f"Tool execution error, sir: {e}"
                break
        else:
            break

    add_to_history("assistant", reply)
    if len(reply) > 4000:
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i : i + 4000])
    else:
        await update.message.reply_text(reply)

# ── VOICE TEXT PROCESSOR (returns Jarvis's reply for TTS) ────────────────────
async def process_voice_as_text(user_message: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Run the full Jarvis logic on a transcribed voice message and return the reply text for TTS."""
    user_id = update.message.from_user.id
    memory  = load_memory()

    history  = get_recent_history(20)
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": user_message})
    facts  = get_memory_facts()
    system = SYSTEM_PROMPT + (f"\n\nKnown facts about sir:\n{facts}" if facts else "")
    add_to_history("user", user_message)

    reply = ""
    for _ in range(5):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text.strip()

        tool_match = re.search(r"<TOOL>\s*(\{.*?\})\s*</TOOL>", reply, re.DOTALL)
        dest_match = re.search(r"<DEST>(.*?)</DEST>", reply, re.DOTALL)

        if dest_match:
            try:
                dest_data  = json.loads(dest_match.group(1).strip())
                display    = reply[: reply.index("<DEST>")].strip()
                draft_text = re.sub(r"^📝\s*\*Draft:\*\s*\n+", "", display, flags=re.IGNORECASE).strip()
                set_pending_reply(user_id, draft_text, meta=dest_data)
                await update.message.reply_text(
                    display + "\n\nReply *yes* to send, *schedule [time]* to schedule, or tell me what to change.",
                    parse_mode="Markdown",
                )
            except Exception:
                await update.message.reply_text(reply.replace(dest_match.group(0), "").strip(), parse_mode="Markdown")
            add_to_history("assistant", reply)
            return reply

        if tool_match:
            try:
                tool_call = json.loads(tool_match.group(1).strip())
                tool_name = tool_call.get("tool")
                params    = tool_call.get("params", {})
                if tool_name == "RECALL_MESSAGE":
                    tool_result = await _execute_recall_message(bot=context.bot)
                elif tool_name in ("SEND_TELEGRAM", "SEND_AND_FOLLOWUP"):
                    entity_name = params.get("entity", "")
                    message     = params.get("message", "")
                    try:
                        async with _tl:
                            entity_obj, display_name, is_group = await _resolve_dialog(entity_name)
                            if not is_group:
                                sent = await telethon_client.send_message(entity_obj, message)
                                save_last_sent({
                                    "entity":     display_name,
                                    "message":    message,
                                    "message_id": sent.id,
                                    "sent_via":   "telethon",
                                    "sent_at":    datetime.now(pytz.UTC).isoformat(),
                                })
                        if is_group:
                            bot_sent = await context.bot.send_message(
                                chat_id=_get_bot_chat_id(entity_obj), text=message
                            )
                            save_last_sent({
                                "entity":     display_name,
                                "message":    message,
                                "message_id": bot_sent.message_id,
                                "chat_id":    _get_bot_chat_id(entity_obj),
                                "sent_via":   "bot",
                                "sent_at":    datetime.now(pytz.UTC).isoformat(),
                            })
                        # Start reply monitoring
                        save_monitored_outgoing({
                            "id":           f"mon_{int(datetime.now().timestamp())}",
                            "entity":       display_name,
                            "is_group":     is_group,
                            "bot_chat_id":  _get_bot_chat_id(entity_obj) if is_group else None,
                            "message_sent": message,
                            "sent_at":      datetime.now(pytz.UTC).isoformat(),
                            "status":       "waiting",
                        })
                        if tool_name == "SEND_AND_FOLLOWUP":
                            fu_id = f"fu_{int(datetime.now().timestamp())}"
                            save_followup({
                                "id":             fu_id,
                                "entity":         display_name,
                                "message_sent":   message,
                                "sent_at":        datetime.now(pytz.UTC).isoformat(),
                                "followup_hours": int(params.get("followup_hours", 48)),
                                "reminder":       params.get("reminder", f"Follow up with {display_name}"),
                                "status":         "pending",
                            })
                            via = "as the bot" if is_group else "as you"
                            tool_result = (
                                f"✅ Message sent to *{display_name}* ({via}) and follow-up set for "
                                f"{params.get('followup_hours', 48)} hours."
                            )
                        else:
                            via = "as the bot" if is_group else "as you"
                            tool_result = f"✅ Message sent to *{display_name}* ({via})."
                    except ValueError as ve:
                        tool_result = f"[ERROR] {ve}"
                    except Exception as te:
                        tool_result = f"[ERROR] Could not send to {entity_name}: {te}"
                else:
                    tool_result = execute_tool(tool_name, params)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[TOOL RESULT for {tool_name}]\n{tool_result}"})
                continue
            except Exception as e:
                reply = f"Tool execution error, sir: {e}"
                break
        else:
            break

    add_to_history("assistant", reply)
    if len(reply) > 4000:
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i : i + 4000])
    else:
        await update.message.reply_text(reply)
    return reply

# ── VOICE MESSAGE HANDLER ────────────────────────────────────────────────────
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"VOICE: received from user {update.message.from_user.id}")
    if update.message.from_user.id != OWNER_TELEGRAM_ID:
        return
    if not openai_client:
        await update.message.reply_text(
            "⚠️ OpenAI API key not configured, sir. Set OPENAI_API_KEY in Railway."
        )
        return
    try:
        await update.message.reply_text("🎙️ Transcribing…")
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        loop          = asyncio.get_event_loop()
        transcription = await loop.run_in_executor(None, _transcribe_audio, tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(
            f"🎙️ *You said:* _{transcription}_", parse_mode="Markdown"
        )
        # Process the transcribed text through the full private message logic
        # We inject the transcription as the message text and capture Jarvis's reply for TTS
        original_reply_text = await process_voice_as_text(transcription, update, context)
        if original_reply_text:
            await send_tts_reply(update, original_reply_text)
    except Exception as e:
        print(f"VOICE ERROR: {e}")
        await update.message.reply_text(f"⚠️ Voice error, sir: {e}")

# ── FOLLOW-UP CHECKER (runs every 2 hours) ────────────────────────────────────
async def check_followups_job(context):
    memory   = load_memory()
    followups = memory.get("follow_ups", [])
    now      = datetime.now(pytz.UTC)
    changed  = False

    for fu in followups:
        if fu.get("status") != "pending":
            continue

        sent_at = datetime.fromisoformat(fu["sent_at"])
        if sent_at.tzinfo is None:
            sent_at = pytz.UTC.localize(sent_at)
        deadline = sent_at + timedelta(hours=fu.get("followup_hours", 48))

        if now < deadline:
            continue  # not due yet

        # Check Telethon for a reply from this contact
        replied = False
        try:
            async with _tl:
                async for dialog in telethon_client.iter_dialogs():
                    if fu["entity"].lower() in dialog.name.lower():
                        async for msg in telethon_client.iter_messages(dialog.entity, limit=15):
                            msg_date = msg.date
                            if msg_date.tzinfo is None:
                                msg_date = pytz.UTC.localize(msg_date)
                            if not msg.out and msg_date > sent_at:
                                replied = True
                            break
                        break
        except Exception as e:
            print(f"Follow-up check error for {fu['entity']}: {e}")

        fu["status"] = "replied" if replied else "alerted"
        changed = True

        if not replied:
            deadline_str = deadline.astimezone(TZ).strftime("%b %d at %I:%M %p PST")
            await context.bot.send_message(
                chat_id=OWNER_TELEGRAM_ID,
                text=(
                    f"🔔 *Follow-up Alert*\n\n"
                    f"*{fu['entity']}* has not replied since {deadline_str}.\n\n"
                    f"📨 Original message: _{fu['message_sent']}_\n\n"
                    f"📝 Reminder: {fu['reminder']}\n\n"
                    f"Shall I send a follow-up message, sir?"
                ),
                parse_mode="Markdown",
            )

    if changed:
        memory["follow_ups"] = followups
        save_memory_data(memory)

# ── REPLY MONITORING JOB (runs every 15 min) ──────────────────────────────────
async def check_reply_monitoring_job(context):
    monitored = get_monitored_outgoing()
    if not monitored:
        return

    # Don't stack a new alert if owner is already handling one
    if get_pending_contact_reply(OWNER_TELEGRAM_ID):
        return

    found_reply = None
    found_mon   = None

    try:
        async with _tl:
            for mon in monitored:
                entity_name = mon["entity"]
                if is_blocked_chat(entity_name):
                    continue
                sent_at     = datetime.fromisoformat(mon["sent_at"])
                if sent_at.tzinfo is None:
                    sent_at = pytz.UTC.localize(sent_at)

                async for dialog in telethon_client.iter_dialogs(limit=300):
                    if is_blocked_chat(dialog.name):
                        continue
                    if entity_name.lower() not in (dialog.name or "").lower():
                        continue
                    async for msg in telethon_client.iter_messages(dialog.entity, limit=15):
                        msg_date = msg.date
                        if msg_date.tzinfo is None:
                            msg_date = pytz.UTC.localize(msg_date)
                        if not msg.out and msg_date > sent_at and msg.text:
                            found_reply = msg.text
                            found_mon   = mon
                            break
                    if found_reply:
                        break
                if found_reply:
                    break
    except Exception as e:
        print(f"[Reply monitor] Error: {e}")
        return

    if not found_reply or not found_mon:
        return

    # Mark as alerted so we don't re-alert
    update_monitored_status(found_mon["id"], "alerted")

    # Generate 3 reply suggestions with Claude
    try:
        sugg_resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
            system=(
                "You are G.A.R.V.I.S. Generate exactly 3 short, professional reply options "
                "numbered 1, 2, 3. Each on its own line. No preamble or extra text."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Sir sent to {found_mon['entity']}: \"{found_mon['message_sent']}\"\n"
                    f"{found_mon['entity']} replied: \"{found_reply}\"\n\n"
                    "Generate 3 reply options for sir."
                ),
            }],
        )
        suggestions = sugg_resp.content[0].text.strip()
    except Exception:
        suggestions = (
            "1. Got it, thank you.\n"
            "2. Understood, I'll get back to you shortly.\n"
            "3. Acknowledged."
        )

    set_pending_contact_reply(OWNER_TELEGRAM_ID, {
        "entity":         found_mon["entity"],
        "is_group":       found_mon.get("is_group", False),
        "bot_chat_id":    found_mon.get("bot_chat_id"),
        "incoming_message": found_reply,
        "message_sent":   found_mon["message_sent"],
        "suggestions":    suggestions,
    })

    preview = found_mon["message_sent"][:60] + ("…" if len(found_mon["message_sent"]) > 60 else "")
    await context.bot.send_message(
        chat_id=OWNER_TELEGRAM_ID,
        text=(
            f"💬 *{found_mon['entity']}* replied:\n\n"
            f"_{found_reply}_\n\n"
            f"_(Re: \"{preview}\")_\n\n"
            f"Suggested replies:\n{suggestions}\n\n"
            f"Reply with *1*, *2*, or *3* to send, type your own, or say *skip* to ignore."
        ),
        parse_mode="Markdown",
    )


async def handle_followups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_TELEGRAM_ID:
        return
    text = list_all_followups()
    await update.message.reply_text(f"📋 *Active Follow-ups:*\n\n{text}", parse_mode="Markdown")

# ── REAL-TIME INCOMING MESSAGE HANDLER (Telethon event) ───────────────────────
async def _handle_incoming_telethon_message(event, bot):
    """Fires instantly when any Telegram message arrives via the user account."""
    if not event.text:
        return

    # Skip if owner already has a reply pending
    if get_pending_contact_reply(OWNER_TELEGRAM_ID):
        return

    monitored = get_monitored_outgoing()
    if not monitored:
        return

    # Identify the chat name
    try:
        chat      = await event.get_chat()
        chat_name = (
            getattr(chat, "title", None)
            or (
                (getattr(chat, "first_name", "") or "")
                + " "
                + (getattr(chat, "last_name",  "") or "")
            ).strip()
        )
    except Exception:
        return

    if is_blocked_chat(chat_name):
        return

    # Find a monitored message from this chat
    matched_mon = None
    for mon in monitored:
        e = mon["entity"].lower()
        c = chat_name.lower()
        if e in c or c in e:
            matched_mon = mon
            break

    if not matched_mon:
        return

    update_monitored_status(matched_mon["id"], "alerted")

    # Generate 3 reply suggestions
    try:
        sugg_resp  = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
            system=(
                "You are G.A.R.V.I.S. Generate exactly 3 short, professional reply options "
                "numbered 1, 2, 3. Each on its own line. No preamble or extra text."
            ),
            messages=[{
                "role":    "user",
                "content": (
                    f"Sir sent to {matched_mon['entity']}: \"{matched_mon['message_sent']}\"\n"
                    f"{chat_name} replied: \"{event.text}\"\n\n"
                    "Generate 3 reply options for sir."
                ),
            }],
        )
        suggestions = sugg_resp.content[0].text.strip()
    except Exception:
        suggestions = (
            "1. Got it, thank you.\n"
            "2. Understood, I'll follow up shortly.\n"
            "3. Acknowledged."
        )

    set_pending_contact_reply(OWNER_TELEGRAM_ID, {
        "entity":           chat_name,
        "is_group":         matched_mon.get("is_group", False),
        "bot_chat_id":      matched_mon.get("bot_chat_id"),
        "incoming_message": event.text,
        "message_sent":     matched_mon["message_sent"],
        "suggestions":      suggestions,
    })

    preview = matched_mon["message_sent"][:60] + ("…" if len(matched_mon["message_sent"]) > 60 else "")
    try:
        await bot.send_message(
            chat_id=OWNER_TELEGRAM_ID,
            text=(
                f"💬 *{chat_name}* replied:\n\n"
                f"_{event.text}_\n\n"
                f"_(Re: \"{preview}\")_\n\n"
                f"Suggested replies:\n{suggestions}\n\n"
                "Reply with *1*, *2*, or *3* to send, "
                "type your own, or say *skip* to ignore."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[Reply alert] Failed to notify owner: {e}")


# ── DAILY CLIENTS REPORT (folder: "G clients") ───────────────────────────────
CLIENTS_FOLDER_NAME = "G clients"
_TG_MAX = 4000  # safe chunk size under Telegram's 4096 limit


def _html_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_for_telegram(text: str) -> list:
    """Split a long message into chunks on line boundaries."""
    if len(text) <= _TG_MAX:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > _TG_MAX:
            if current:
                chunks.append(current.rstrip())
            current = ""
            while len(line) > _TG_MAX:      # single monster line
                chunks.append(line[:_TG_MAX])
                line = line[_TG_MAX:]
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


async def _get_clients_folder_peers():
    """Find the Telegram folder (dialog filter) named CLIENTS_FOLDER_NAME and
    return its list of peers. Raises ValueError if the folder is missing."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    def _norm(s):
        # Keep only letters/digits/spaces so emojis and symbols in folder
        # titles (e.g. "G clients 🔝") don't break matching.
        s = "".join(ch for ch in (s or "") if ch.isalnum() or ch.isspace())
        return " ".join(s.lower().split())

    result  = await telethon_client(GetDialogFiltersRequest())
    filters_ = getattr(result, "filters", result) or []
    target   = _norm(CLIENTS_FOLDER_NAME)
    seen     = []
    for f in filters_:
        raw_title = getattr(f, "title", None)
        title = getattr(raw_title, "text", raw_title)  # TextWithEntities or str
        if not title:
            continue
        seen.append(title)
        nt = _norm(title)
        if nt == target or target in nt or nt in target:
            peers = list(getattr(f, "pinned_peers", []) or []) + \
                    list(getattr(f, "include_peers", []) or [])
            return peers
    raise ValueError(
        f'Telegram folder "{CLIENTS_FOLDER_NAME}" not found. '
        f'Folders I can see: {", ".join(seen) if seen else "(none)"}'
    )


async def _collect_clients_activity(hours: int = 24):
    """Return (digest_text, active_chats, total_msgs) for the last `hours` of
    messages across every chat in the clients folder."""
    cutoff = datetime.now(pytz.UTC) - timedelta(hours=hours)
    peers  = await _get_clients_folder_peers()

    sections, active_chats, total_msgs = [], 0, 0
    for peer in peers:
        try:
            entity = await telethon_client.get_entity(peer)
        except Exception:
            continue
        chat_name = (getattr(entity, "title", None)
                     or " ".join(filter(None, [getattr(entity, "first_name", None),
                                               getattr(entity, "last_name", None)]))
                     or "Unknown chat")
        if is_blocked_chat(chat_name):
            continue
        lines = []
        try:
            async for msg in telethon_client.iter_messages(entity, limit=80):
                if msg.date < cutoff:
                    break
                text = msg.text or ""
                if not text.strip():
                    continue
                if msg.out:
                    sender_name = "G (sir)"
                else:
                    try:
                        sender = await msg.get_sender()
                        sender_name = (" ".join(filter(None, [getattr(sender, "first_name", None),
                                                              getattr(sender, "last_name", None)]))
                                       or getattr(sender, "title", None) or "Unknown")
                    except Exception:
                        sender_name = "Unknown"
                stamp = msg.date.astimezone(TZ).strftime("%a %H:%M")
                lines.append(f"[{stamp}] {sender_name}: {text[:600]}")
        except Exception as e:
            print(f"[Clients report] Failed to read '{chat_name}': {e}")
            continue
        if lines:
            lines.reverse()  # chronological order
            active_chats += 1
            total_msgs   += len(lines)
            sections.append(f"=== CHAT: {chat_name} ===\n" + "\n".join(lines))

    return "\n\n".join(sections), active_chats, total_msgs


async def generate_clients_report(hours: int = 24) -> str:
    """Build the formatted HTML client report."""
    digest, active_chats, total_msgs = await _collect_clients_activity(hours)
    now       = datetime.now(TZ)
    date_line = now.strftime("%A, %B %-d — last 24 hours")

    if not digest:
        return (f"🗞 <b>CLIENT REPORT</b>\n<i>{date_line}</i>\n\n"
                f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n\n"
                f"Quiet night — no new client activity.\n\n<i>All caught up, sir.</i>")

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3000,
        system=CLIENTS_REPORT_PROMPT,
        messages=[{"role": "user", "content":
                   f"date_line: {date_line}\n"
                   f"Chats with activity: {active_chats} | Messages: {total_msgs}\n\n"
                   f"{digest}"}],
    )
    return response.content[0].text.strip()


async def _send_clients_report(bot, hours: int = 24):
    try:
        report = await generate_clients_report(hours)
    except ValueError as e:
        await bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=f"⚠️ Client report: {e}")
        return
    except Exception as e:
        print(f"[Clients report] Generation failed: {e}")
        await bot.send_message(
            chat_id=OWNER_TELEGRAM_ID,
            text=f"⚠️ Client report failed to generate: {e}",
        )
        return
    for chunk in _split_for_telegram(report):
        try:
            await bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=chunk, parse_mode="HTML")
        except Exception:
            # Malformed HTML fallback — send as plain text rather than dropping it
            await bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=chunk)


async def clients_report_job(context):
    await _send_clients_report(context.bot)


async def handle_clients_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    await update.message.reply_text("Compiling your client report now, sir…")
    await _send_clients_report(context.bot)


# ── STARTUP ───────────────────────────────────────────────────────────────────
async def post_init(application):
    application.job_queue.run_daily(
        scheduled_briefing,
        time=dt_time(hour=9, minute=0, tzinfo=TZ),
    )
    application.job_queue.run_daily(
        scheduled_briefing,
        time=dt_time(hour=12, minute=0, tzinfo=TZ),
    )
    application.job_queue.run_daily(
        clients_report_job,
        time=dt_time(hour=9, minute=0, tzinfo=TZ),
        name="daily_clients_report",
    )
    application.job_queue.run_repeating(check_followups_job, interval=7200, first=60)
    # Keep polling as a 5-min safety fallback (real-time events handle the rest)
    application.job_queue.run_repeating(check_reply_monitoring_job, interval=300, first=180)

    # ── Start persistent Telethon connection + real-time reply listener ────────
    await telethon_client.connect()

    bot = application.bot

    @telethon_client.on(tl_events.NewMessage(incoming=True))
    async def _on_incoming(event):
        await _handle_incoming_telethon_message(event, bot)

    print("G.A.R.V.I.S. online. Telethon listener active. Briefings: 9 AM & 12 PM PST. Client report: 9 AM.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    invoice_conv = ConversationHandler(
        entry_points=[CommandHandler("invoice", handle_invoice_command)],
        states={ASKING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invoice_amount)]},
        fallbacks=[CommandHandler("cancel", handle_invoice_cancel)],
        per_chat=False,
        per_user=True,
    )
    app.add_handler(invoice_conv)
    app.add_handler(CommandHandler("sent",        handle_sent_command))
    app.add_handler(CommandHandler("pending",     handle_pending_command))
    app.add_handler(CommandHandler("brief",       handle_brief_command))
    app.add_handler(CommandHandler("clients",     handle_clients_command))
    app.add_handler(CommandHandler("groups",      handle_groups_command))
    app.add_handler(CommandHandler("watch",       handle_watch_command))
    app.add_handler(CommandHandler("watches",     handle_watches_command))
    app.add_handler(CommandHandler("deletewatch", handle_deletewatch_command))
    app.add_handler(CommandHandler("scheduled",   handle_scheduled_command))
    app.add_handler(CommandHandler("followups",   handle_followups_command))
    app.add_handler(CommandHandler("testcal",     handle_testcal_command))
    app.add_handler(MessageHandler(
        filters.VOICE & filters.ChatType.PRIVATE,
        handle_voice_message,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_message,
    ))
    print("G.A.R.V.I.S. is online. All systems operational.")
    app.run_polling()

if __name__ == "__main__":
    main()
