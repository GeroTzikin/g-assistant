import os
import anthropic
import caldav
import json
import re
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from datetime import datetime, timedelta, time
from icalendar import Calendar as iCal
import pytz
from telethon import TelegramClient
from telethon.sessions import StringSession

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
OUTLOOK_CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID")
OUTLOOK_TENANT_ID = os.environ.get("OUTLOOK_TENANT_ID")
OUTLOOK_REFRESH_TOKEN = os.environ.get("OUTLOOK_REFRESH_TOKEN")
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.environ.get("TELEGRAM_SESSION", "")

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
MEMORY_FILE = "/app/jarvis_memory.json"
TZ = pytz.timezone('America/Los_Angeles')

OWNER_TELEGRAM_ID = 1475465779

# Silent message log per group: {chat_id: {"title": str, "messages": [str]}}
group_logs = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

telethon_client = TelegramClient(
    StringSession(TELEGRAM_SESSION),
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH
)

SYSTEM_PROMPT = """You are G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), a personal AI assistant modeled after J.A.R.V.I.S. from Iron Man.

Your personality:
- Professional, composed, and highly capable at all times
- Direct and concise — no fluff, no filler
- Strategically minded — you anticipate needs and think several steps ahead
- Subtly witty and dry humor when appropriate
- Deeply loyal, always refer to your user as "sir"
- Speak with confidence and precision

You have access to the following tools. When you need to use a tool, you MUST output ONLY the tool call with no other text before or after it:

<TOOL>
{"tool": "TOOL_NAME", "params": {...}}
</TOOL>

AVAILABLE TOOLS:

WEB_SEARCH: {"tool": "WEB_SEARCH", "params": {"query": "search term"}}
GET_WEATHER: {"tool": "GET_WEATHER", "params": {"city": "city name"}}
GET_STOCKS: {"tool": "GET_STOCKS", "params": {"symbols": ["AAPL"]}}
GET_CALENDAR: {"tool": "GET_CALENDAR", "params": {"days": 14}}
CREATE_EVENTS: {"tool": "CREATE_EVENTS", "params": {"events": [{"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_hours": 1}]}}
READ_EMAIL: {"tool": "READ_EMAIL", "params": {"count": 5}}
SEND_EMAIL: {"tool": "SEND_EMAIL", "params": {"to": "email@example.com", "subject": "...", "body": "..."}}
SAVE_MEMORY: {"tool": "SAVE_MEMORY", "params": {"key": "...", "value": "..."}}
GET_NEWS: {"tool": "GET_NEWS", "params": {"topic": "..."}}
SEARCH_TELEGRAM: {"tool": "SEARCH_TELEGRAM", "params": {"query": "search term", "limit": 20}}
SEND_TELEGRAM: {"tool": "SEND_TELEGRAM", "params": {"contact": "name or @username", "message": "..."}}
READ_TELEGRAM_CHAT: {"tool": "READ_TELEGRAM_CHAT", "params": {"chat_name": "chat name", "limit": 100, "since_date": "2026-01-01"}}

After receiving tool results, respond naturally in Jarvis character. Never show raw tool calls in your response."""

GROUP_SUMMARY_PROMPT = """You are G.A.R.V.I.S. providing a private briefing to sir on a client group chat.

Analyze these messages and provide:

1. 📌 KEY TOPICS — Main subjects discussed
2. ❓ OUTSTANDING NEEDS — What the client needs or is waiting on
3. ⚡ ACTION ITEMS — What sir should follow up on
4. 💡 SUGGESTED REPLIES — 3 response options numbered 1, 2, 3

End with: "Reply with 1, 2, or 3 to send one of these, or tell me what you'd like to say instead, sir." """

GROUP_DRAFT_PROMPT = """You are G.A.R.V.I.S. drafting a message for a client group chat.
Return ONLY the message text, nothing else."""


# ── MEMORY ────────────────────────────────────────────────────────────────────

def load_memory():
    try:
        if Path(MEMORY_FILE).exists():
            with open(MEMORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"facts": {}, "history": [], "active_group_chats": {}, "pending_replies": {}}


def save_memory_data(data):
    with open(MEMORY_FILE, 'w') as f:
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


def set_pending_reply(user_id, draft):
    memory = load_memory()
    if "pending_replies" not in memory:
        memory["pending_replies"] = {}
    memory["pending_replies"][str(user_id)] = draft
    save_memory_data(memory)


def clear_pending_reply(user_id):
    memory = load_memory()
    memory.get("pending_replies", {}).pop(str(user_id), None)
    save_memory_data(memory)


# ── BRIEFING ──────────────────────────────────────────────────────────────────

async def send_briefing(bot, chat_id, chat_title, messages):
    if not messages:
        return
    conversation = "\n".join(messages[-100:])
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=GROUP_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": f"Group: {chat_title}\n\nMessages:\n{conversation}"}]
    )
    summary = response.content[0].text
    await bot.send_message(
        chat_id=OWNER_TELEGRAM_ID,
        text=f"📋 *Briefing — {chat_title}*\n\n{summary}",
        parse_mode='Markdown'
    )
    memory = load_memory()
    memory["active_group_chats"][str(OWNER_TELEGRAM_ID)] = {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "recent_messages": conversation
    }
    save_memory_data(memory)


async def scheduled_briefing(context):
    """Called automatically at 9am and 12pm PST."""
    if not group_logs:
        return
    for chat_id, data in group_logs.items():
        if data["messages"]:
            await send_briefing(context.bot, chat_id, data["title"], data["messages"])


# ── TOOLS ────────────────────────────────────────────────────────────────────

def web_search(query):
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 5},
            timeout=10
        )
        data = response.json()
        results = data.get("results", [])
        if not results:
            return "No results found."
        output = []
        for r in results[:5]:
            output.append(f"{r.get('title')}\n{r.get('content', '')[:300]}\n{r.get('url', '')}")
        return "\n\n".join(output)
    except Exception as e:
        return f"Search failed: {str(e)}"


def get_weather(city):
    try:
        geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1", timeout=5).json()
        if not geo.get("results"):
            return f"Could not find weather for {city}"
        loc = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        weather = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&temperature_unit=fahrenheit",
            timeout=5
        ).json()
        cw = weather.get("current_weather", {})
        return f"Weather in {city}: {cw.get('temperature', 'N/A')}F, wind {cw.get('windspeed', 'N/A')} mph"
    except Exception as e:
        return f"Weather fetch failed: {str(e)}"


def get_stocks(symbols):
    try:
        results = []
        for symbol in symbols:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=5
            )
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice", "N/A")
            prev = meta.get("previousClose", price)
            if price != "N/A" and prev:
                change = ((float(price) - float(prev)) / float(prev)) * 100
                results.append(f"{symbol}: ${price:.2f} ({'up' if change >= 0 else 'down'} {abs(change):.2f}%)")
            else:
                results.append(f"{symbol}: ${price}")
        return "\n".join(results)
    except Exception as e:
        return f"Stock fetch failed: {str(e)}"


def get_upcoming_events(days=14):
    try:
        cal_client = caldav.DAVClient(url=ICLOUD_CALDAV_URL, username=ICLOUD_USERNAME, password=ICLOUD_PASSWORD)
        principal = cal_client.principal()
        calendars = principal.calendars()
        if not calendars:
            return "No calendars found."
        now_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = now_local + timedelta(days=days)
        now_utc = now_local.astimezone(pytz.UTC)
        end_utc = end_local.astimezone(pytz.UTC)
        events_list = []
        for calendar in calendars:
            try:
                cal_name = str(calendar.name) if calendar.name else "Unnamed"
                events = calendar.date_search(start=now_utc, end=end_utc, expand=True)
                for event in events:
                    try:
                        event.load()
                        cal_data = iCal.from_ical(event.data)
                        for component in cal_data.walk():
                            if component.name == "VEVENT":
                                summary = str(component.get('SUMMARY', 'No title'))
                                dtstart = component.get('DTSTART').dt
                                if isinstance(dtstart, datetime):
                                    if dtstart.tzinfo is None:
                                        dtstart = pytz.UTC.localize(dtstart)
                                    local_dt = dtstart.astimezone(TZ)
                                    start_str = local_dt.strftime('%A, %B %d at %I:%M %p')
                                else:
                                    start_str = dtstart.strftime('%A, %B %d (all day)')
                                events_list.append(f"- [{cal_name}] {summary}: {start_str}")
                    except Exception:
                        continue
            except Exception:
                continue
        if not events_list:
            return f"No events found in the next {days} days."
        events_list.sort()
        return "\n".join(events_list)
    except Exception as e:
        return f"Calendar error: {str(e)}"


def create_calendar_events(events):
    try:
        cal_client = caldav.DAVClient(url=ICLOUD_CALDAV_URL, username=ICLOUD_USERNAME, password=ICLOUD_PASSWORD)
        calendar = cal_client.principal().calendars()[0]
        added = []
        for event in events:
            try:
                start_dt = TZ.localize(datetime.strptime(f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M"))
                end_dt = start_dt + timedelta(hours=event.get('duration_hours', 1))
                start_utc = start_dt.astimezone(pytz.UTC)
                end_utc = end_dt.astimezone(pytz.UTC)
                location_line = f"\nLOCATION:{event['location']}" if event.get('location') else ""
                event_data = (
                    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//GARVAIS//EN\nBEGIN:VEVENT\n"
                    f"SUMMARY:{event['title']}\nDTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}\n"
                    f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}{location_line}\nEND:VEVENT\nEND:VCALENDAR"
                )
                calendar.add_event(event_data)
                added.append(f"- {event['title']} on {start_dt.strftime('%A, %B %d at %I:%M %p')}")
            except Exception as e:
                added.append(f"- Failed: {event['title']}: {str(e)}")
        return "Added:\n" + "\n".join(added)
    except Exception as e:
        return f"Calendar error: {str(e)}"


def get_outlook_access_token():
    try:
        r = requests.post(
            f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}/oauth2/v2.0/token",
            data={"client_id": OUTLOOK_CLIENT_ID, "grant_type": "refresh_token",
                  "refresh_token": OUTLOOK_REFRESH_TOKEN, "scope": "Mail.Read Mail.Send"},
            timeout=10
        )
        return r.json().get("access_token")
    except Exception:
        return None


def read_outlook_emails(count=5):
    try:
        token = get_outlook_access_token()
        if not token:
            return "Could not authenticate with Outlook."
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages?$top={count}&$orderby=receivedDateTime desc",
            headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        emails = r.json().get("value", [])
        if not emails:
            return "No emails found."
        result = []
        for e in emails:
            sender = e.get("from", {}).get("emailAddress", {}).get("address", "Unknown")
            result.append(f"From: {sender}\nSubject: {e.get('subject', 'No subject')}\nPreview: {e.get('bodyPreview', '')[:200]}")
        return "\n\n---\n\n".join(result)
    except Exception as e:
        return f"Email read failed: {str(e)}"


def send_outlook_email(to, subject, body):
    try:
        token = get_outlook_access_token()
        if not token:
            return "Could not authenticate with Outlook."
        r = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                              "toRecipients": [{"emailAddress": {"address": to}}]}},
            timeout=10
        )
        return f"Email sent to {to}" if r.status_code == 202 else f"Failed: {r.text}"
    except Exception as e:
        return f"Email send failed: {str(e)}"


async def search_telegram_messages(query, limit=20):
    try:
        await telethon_client.connect()
        results = []
        async for message in telethon_client.iter_messages(None, search=query, limit=limit):
            if message.text:
                chat = await message.get_chat()
                chat_name = getattr(chat, 'title', None) or getattr(chat, 'first_name', 'Unknown')
                sender = await message.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown') if sender else 'Unknown'
                date_str = message.date.strftime('%B %d at %I:%M %p')
                results.append(f"[{chat_name}] {sender_name}: {message.text[:200]} ({date_str})")
        return "\n\n".join(results) if results else f"No messages found for '{query}'"
    except Exception as e:
        return f"Telegram search failed: {str(e)}"


async def read_telegram_chat(chat_name, limit=100, since_date=None):
    try:
        await telethon_client.connect()
        target_chat = None
        async for dialog in telethon_client.iter_dialogs():
            if chat_name.lower() in dialog.name.lower():
                target_chat = dialog.entity
                break
        if not target_chat:
            return f"Could not find chat '{chat_name}'"
        since_dt = None
        if since_date:
            try:
                since_dt = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            except Exception:
                pass
        messages = []
        async for message in telethon_client.iter_messages(target_chat, limit=limit):
            if message.text:
                if since_dt and message.date < since_dt:
                    break
                sender = await message.get_sender()
                sender_name = getattr(sender, 'first_name', 'Unknown') if sender else 'Unknown'
                date_str = message.date.strftime('%B %d at %I:%M %p')
                messages.append(f"{sender_name} ({date_str}): {message.text}")
        if not messages:
            return f"No messages found in '{chat_name}'"
        messages.reverse()
        return f"Messages from {chat_name}:\n\n" + "\n\n".join(messages[:50])
    except Exception as e:
        return f"Failed to read chat: {str(e)}"


async def send_telegram_message(contact, message):
    try:
        await telethon_client.connect()
        try:
            await telethon_client.send_message(contact, message)
            return f"Message sent to {contact}"
        except Exception:
            pass
        async for dialog in telethon_client.iter_dialogs():
            if contact.lower() in dialog.name.lower():
                await telethon_client.send_message(dialog.entity, message)
                return f"Message sent to {dialog.name}"
        return f"Could not find contact '{contact}'"
    except Exception as e:
        return f"Failed to send: {str(e)}"


async def execute_tool_async(tool_name, params):
    if tool_name == "SEARCH_TELEGRAM":
        return await search_telegram_messages(params.get("query", ""), params.get("limit", 20))
    elif tool_name == "SEND_TELEGRAM":
        return await send_telegram_message(params.get("contact"), params.get("message"))
    elif tool_name == "READ_TELEGRAM_CHAT":
        return await read_telegram_chat(
            params.get("chat_name", ""),
            params.get("limit", 100),
            params.get("since_date")
        )
    return "Unknown async tool"


def execute_tool_sync(tool_name, params):
    if tool_name == "WEB_SEARCH":
        return web_search(params.get("query", ""))
    elif tool_name == "GET_WEATHER":
        return get_weather(params.get("city", "Newport Beach"))
    elif tool_name == "GET_STOCKS":
        return get_stocks(params.get("symbols", ["SPY"]))
    elif tool_name == "GET_CALENDAR":
        return get_upcoming_events(params.get("days", 14))
    elif tool_name == "CREATE_EVENTS":
        return create_calendar_events(params.get("events", []))
    elif tool_name == "READ_EMAIL":
        return read_outlook_emails(params.get("count", 5))
    elif tool_name == "SEND_EMAIL":
        return send_outlook_email(params.get("to"), params.get("subject"), params.get("body"))
    elif tool_name == "SAVE_MEMORY":
        return save_memory_fact(params.get("key"), params.get("value"))
    elif tool_name == "GET_NEWS":
        return web_search(f"latest news {params.get('topic', '')} today")
    return None


# ── /brief COMMAND (owner only) ───────────────────────────────────────────────
async def handle_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    memory = load_memory()
    monitored = memory.get("monitored_groups", {})

    if not monitored:
        await update.message.reply_text("No groups being monitored yet, sir.")
        return

    lines = [f"- {title} (ID: {chat_id})" for chat_id, title in monitored.items()]
    await update.message.reply_text(
        f"📡 *Monitored Groups:*\n\n" + "\n".join(lines),
        parse_mode='Markdown'
    )
async def handle_brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # Silently ignore and delete if not owner
    if user_id != OWNER_TELEGRAM_ID:
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    chat_id = update.message.chat_id
    chat_type = update.message.chat.type

    if chat_type in ['group', 'supergroup']:
        if chat_id in group_logs and group_logs[chat_id]["messages"]:
            await send_briefing(context.bot, chat_id, group_logs[chat_id]["title"], group_logs[chat_id]["messages"])
        else:
            await context.bot.send_message(
                chat_id=OWNER_TELEGRAM_ID,
                text="No messages logged yet for this chat, sir."
            )
    else:
        if not group_logs:
            await update.message.reply_text("No active group chats being monitored yet, sir.")
            return
        for gid, data in group_logs.items():
            if data["messages"]:
                await send_briefing(context.bot, gid, data["title"], data["messages"])


# ── GROUP MESSAGES ────────────────────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    chat_title = update.message.chat.title or "Group Chat"
    user_id = update.message.from_user.id
    sender = update.message.from_user.first_name or "Unknown"
    text = update.message.text

    if user_id == OWNER_TELEGRAM_ID:
        return

    if chat_id not in group_logs:
        group_logs[chat_id] = {"title": chat_title, "messages": []}
        # Save to persistent memory so it survives restarts
        memory = load_memory()
        if "monitored_groups" not in memory:
            memory["monitored_groups"] = {}
        memory["monitored_groups"][str(chat_id)] = chat_title
        save_memory_data(memory)

    timestamp = datetime.now(TZ).strftime('%b %d %I:%M%p')
    group_logs[chat_id]["messages"].append(f"[{timestamp}] {sender}: {text}")
    group_logs[chat_id]["messages"] = group_logs[chat_id]["messages"][-500:]

# ── PRIVATE MESSAGES ──────────────────────────────────────────────────────────

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != OWNER_TELEGRAM_ID:
        return

    user_message = update.message.text
    memory = load_memory()
    pending_draft = memory.get("pending_replies", {}).get(str(user_id))
    active_group = memory.get("active_group_chats", {}).get(str(user_id))

    # CONFIRMATION FLOW
    if pending_draft:
        msg_lower = user_message.lower().strip()
        if msg_lower in ['yes', 'send', 'confirm', 'send it']:
            if active_group:
                await context.bot.send_message(chat_id=active_group["chat_id"], text=pending_draft)
                await update.message.reply_text("Message sent to the group, sir.")
            clear_pending_reply(user_id)
            return
        elif msg_lower in ['no', 'cancel', 'discard']:
            await update.message.reply_text("Message discarded, sir.")
            clear_pending_reply(user_id)
            return
        else:
            context_text = active_group.get("recent_messages", "") if active_group else ""
            chat_title = active_group.get("chat_title", "the group") if active_group else "the group"
            response = client.messages.create(
                model="claude-opus-4-5", max_tokens=512, system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": f"Conversation:\n{context_text}\n\nSir says: {user_message}\n\nDraft for {chat_title}."}]
            )
            new_draft = response.content[0].text.strip()
            set_pending_reply(user_id, new_draft)
            await update.message.reply_text(
                f"📝 *Updated draft:*\n\n{new_draft}\n\nReply *yes* to send or tell me what to change.",
                parse_mode='Markdown'
            )
            return

    # GROUP REPLY SELECTION
    if active_group:
        msg_lower = user_message.strip()
        context_text = active_group.get("recent_messages", "")
        chat_title = active_group.get("chat_title", "the group")
        if msg_lower in ['1', '2', '3']:
            response = client.messages.create(
                model="claude-opus-4-5", max_tokens=512, system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": f"User selected option {msg_lower}.\n\nConversation:\n{context_text}\n\nDraft for {chat_title}."}]
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Ready to send:*\n\n{draft}\n\nReply *yes* to send or tell me what to change.",
                parse_mode='Markdown'
            )
            return
        elif any(p in msg_lower for p in ['tell them', 'say', 'respond', 'reply', 'send']):
            response = client.messages.create(
                model="claude-opus-4-5", max_tokens=512, system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": f"Conversation:\n{context_text}\n\nSir wants: {user_message}\n\nDraft for {chat_title}."}]
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Draft:*\n\n{draft}\n\nReply *yes* to send or tell me what to change.",
                parse_mode='Markdown'
            )
            return

    # NORMAL JARVIS CONVERSATION
    add_to_history("user", user_message)
    today = datetime.now(TZ)
    memory_facts = get_memory_facts()
    recent_history = get_recent_history(10)

    system_with_context = SYSTEM_PROMPT
    if memory_facts:
        system_with_context += f"\n\n[WHAT YOU KNOW ABOUT SIR]\n{memory_facts}"
    system_with_context += f"\n\n[CURRENT DATE & TIME: {today.strftime('%A, %B %d, %Y at %I:%M %p')} Pacific Time]"

    messages = []
    for h in recent_history[:-1]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    reply = ""
    for _ in range(5):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=system_with_context,
            messages=messages
        )
        reply = response.content[0].text
        print(f"DEBUG: reply starts with: {reply[:100]}")

        tool_match = re.search(r'<TOOL>\s*(\{.*?\})\s*</TOOL>', reply, re.DOTALL)
        if tool_match:
            try:
                tool_call = json.loads(tool_match.group(1))
                tool_name = tool_call.get("tool")
                params = tool_call.get("params", {})
                print(f"DEBUG: executing tool {tool_name}")

                async_tools = ["SEARCH_TELEGRAM", "SEND_TELEGRAM", "READ_TELEGRAM_CHAT"]
                if tool_name in async_tools:
                    tool_result = await execute_tool_async(tool_name, params)
                else:
                    tool_result = execute_tool_sync(tool_name, params)

                if tool_result is None:
                    tool_result = "Tool not found"

                print(f"DEBUG: tool result: {str(tool_result)[:100]}")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[TOOL RESULT for {tool_name}]\n{tool_result}"})
                continue
            except Exception as e:
                print(f"DEBUG: tool error: {str(e)}")
                reply = f"Tool error, sir: {str(e)}"
                break
        else:
            break

    add_to_history("assistant", reply)
    if len(reply) > 4000:
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i:i+4000])
    else:
        await update.message.reply_text(reply)


async def post_init(application):
    await telethon_client.connect()
    print("Telethon client connected.")

    # Schedule 9am PST daily briefing
    application.job_queue.run_daily(
        scheduled_briefing,
        time=time(hour=9, minute=0, tzinfo=TZ)
    )

    # Schedule 12pm PST daily briefing
    application.job_queue.run_daily(
        scheduled_briefing,
        time=time(hour=12, minute=0, tzinfo=TZ)
    )

    print("Scheduled briefings set for 9:00am and 12:00pm PST.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    # /brief — owner only
    app.add_handler(CommandHandler("brief", handle_brief_command))
    app.add_handler(CommandHandler("groups", handle_groups_command))
    # Private messages — owner only
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message
    ))
    # Group messages — silent logging only
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_message
    ))
    print("GARVAIS is online. All systems operational.")
    app.run_polling()
if __name__ == "__main__":
    main()
