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
from telethon import TelegramClient
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
MEMORY_FILE          = "/app/jarvis_memory.json"

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

⚠️ CRITICAL TOOL RULE: Tool results prefixed with [ERROR] mean the operation FAILED.
You MUST relay the exact error to sir word-for-word. NEVER say an action succeeded when
the tool result contains [ERROR]. If a calendar event fails, say it failed and why.

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
    memory["history"] = memory["history"][-50:]
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

    return f"Unknown tool: {tool_name}"

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
            async with telethon_client:
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
        entity = pending_meta.get("entity", "")
        if "xeebi noc" in entity.lower():
            await context.bot.send_message(chat_id=XEEBI_NOC_CHAT_ID, text=draft_text)
        else:
            async with telethon_client:
                await telethon_client.send_message(entity, draft_text)
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
    await update.message.reply_text(
        f"Hi {user_first_name}! 👋 How much would you like to invoice for?"
    )
    return ASKING_AMOUNT

async def handle_invoice_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_text = update.message.text.strip()
    chat_title  = context.user_data.get("invoice_chat_title", "the group")
    await update.message.reply_text("Got it! I'll request your invoice right away. 🙏")
    invoice_message = (
        f"Hello team! 👋 Can we please invoice *{chat_title}* "
        f"for the amount of *{amount_text}*? Thank you! 🙏"
    )
    await context.bot.send_message(
        chat_id=XEEBI_SALES_GROUP_ID,
        message_thread_id=INVOICING_THREAD_ID,
        text=invoice_message,
        parse_mode="Markdown",
    )
    if "global telecom" in chat_title.lower():
        try:
            async with telethon_client:
                async for dialog in telethon_client.iter_dialogs():
                    if UPM_NEWPORT_CHAT.lower() in dialog.name.lower():
                        await telethon_client.send_message(
                            dialog.entity,
                            invoice_message.replace("*", ""),
                        )
                        break
        except Exception as e:
            print(f"UPM NEWPORT send failed: {e}")
    return ConversationHandler.END

async def handle_invoice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Invoice cancelled.")
    return ConversationHandler.END

# ── GROUP MESSAGES ────────────────────────────────────────────────────────────
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id    = update.message.chat_id
    chat_title = update.message.chat.title or "Group Chat"
    user_id    = update.message.from_user.id
    sender     = update.message.from_user.first_name or "Unknown"
    text       = update.message.text
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

    # ── PENDING DRAFT FLOW ────────────────────────────────────────────────────
    if pending_draft:
        msg_lower = user_message.lower().strip()

        if msg_lower in ("yes", "send", "confirm", "send it", "yes send it", "yes, send it"):
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
    history  = get_recent_history(10)
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

        # ── Detect draft with destination metadata ──
        dest_match = re.search(r"<DEST>(.*?)</DEST>", reply, re.DOTALL)
        if dest_match:
            try:
                dest_data  = json.loads(dest_match.group(1).strip())
                display    = reply[: reply.index("<DEST>")].strip()
                draft_text = re.sub(r"^📝\s*\*Draft:\*\s*\n+", "", display, flags=re.IGNORECASE).strip()
                set_pending_reply(user_id, draft_text, meta=dest_data)
                await update.message.reply_text(
                    display + "\n\nReply *yes* to send immediately, "
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
                tool_call   = json.loads(tool_match.group(1).strip())
                tool_name   = tool_call.get("tool")
                params      = tool_call.get("params", {})
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
    print("G.A.R.V.I.S. online. Briefings set for 9:00 AM and 12:00 PM PST.")

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
    app.add_handler(CommandHandler("brief",       handle_brief_command))
    app.add_handler(CommandHandler("groups",      handle_groups_command))
    app.add_handler(CommandHandler("watch",       handle_watch_command))
    app.add_handler(CommandHandler("watches",     handle_watches_command))
    app.add_handler(CommandHandler("deletewatch", handle_deletewatch_command))
    app.add_handler(CommandHandler("scheduled",   handle_scheduled_command))
    app.add_handler(CommandHandler("testcal",     handle_testcal_command))
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
