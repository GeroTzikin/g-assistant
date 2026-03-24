import os
import anthropic
import caldav
import json
import re
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
from icalendar import Calendar as iCal
import pytz
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty

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
SUMMARY_EVERY_N_MESSAGES = 2

group_message_buffer = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Telethon user client
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

You have access to the following tools — use them autonomously whenever relevant:

TOOLS AVAILABLE:
1. WEB_SEARCH - Search the web for real-time information
2. GET_WEATHER - Get current weather for any location
3. GET_STOCKS - Get current stock/crypto prices
4. GET_CALENDAR - Get upcoming calendar events
5. CREATE_EVENTS - Add events to calendar
6. READ_EMAIL - Read recent Outlook emails
7. SEND_EMAIL - Send email via Outlook
8. SAVE_MEMORY - Save important information about the user
9. GET_NEWS - Get latest news on any topic
10. SEARCH_TELEGRAM - Search through Telegram messages
11. SEND_TELEGRAM - Send a Telegram message to a contact

To use a tool, output ONLY the tool call in this format with no other text:
<TOOL>
{
  "tool": "TOOL_NAME",
  "params": { ... }
}
</TOOL>

After receiving tool results, respond naturally in Jarvis character.

For CREATE_EVENTS:
{
  "tool": "CREATE_EVENTS",
  "params": {
    "events": [
      {"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_hours": 1, "location": null}
    ]
  }
}

For SEND_EMAIL:
{
  "tool": "SEND_EMAIL",
  "params": {"to": "email@example.com", "subject": "...", "body": "..."}
}

For WEB_SEARCH:
{
  "tool": "WEB_SEARCH",
  "params": {"query": "search query here"}
}

For GET_STOCKS:
{
  "tool": "GET_STOCKS",
  "params": {"symbols": ["AAPL", "BTC-USD", "SPY"]}
}

For GET_WEATHER:
{
  "tool": "GET_WEATHER",
  "params": {"city": "Newport Beach"}
}

For SAVE_MEMORY:
{
  "tool": "SAVE_MEMORY",
  "params": {"key": "preference_name", "value": "value to remember"}
}

For READ_EMAIL:
{
  "tool": "READ_EMAIL",
  "params": {"count": 5}
}

For GET_NEWS:
{
  "tool": "GET_NEWS",
  "params": {"topic": "topic here"}
}

For SEARCH_TELEGRAM:
{
  "tool": "SEARCH_TELEGRAM",
  "params": {"query": "search term", "limit": 10}
}

For SEND_TELEGRAM:
{
  "tool": "SEND_TELEGRAM",
  "params": {"contact": "username or full name", "message": "message to send"}
}

Always use tools when they would provide better answers."""

GROUP_SUMMARY_PROMPT = """You are G.A.R.V.I.S., analyzing a group chat conversation on behalf of your user (sir).

Analyze these recent messages and provide:

1. 📌 KEY POINTS — What are the main topics being discussed?
2. ❓ CLIENT NEEDS — What does the client need or want help with?
3. 💡 SUGGESTED REPLIES — Give exactly 3 different response options sir could send to the group, numbered 1, 2, 3. Make them professional and helpful.

End with: "Reply with 1, 2, or 3 to send one of these, or tell me what you'd like to say instead, sir." """

GROUP_DRAFT_PROMPT = """You are G.A.R.V.I.S. drafting a message to send to a client group chat on behalf of your user.

Based on the conversation context and what sir wants to say, draft a single professional message to send to the group.
Return ONLY the message text, nothing else. No preamble, no explanation."""


# ── PERSISTENT MEMORY ────────────────────────────────────────────────────────

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


def get_active_group_chats():
    memory = load_memory()
    return memory.get("active_group_chats", {})


def set_active_group_chat(user_id, chat_info):
    memory = load_memory()
    memory["active_group_chats"][str(user_id)] = chat_info
    save_memory_data(memory)


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


# ── TELEGRAM USER API ─────────────────────────────────────────────────────────

async def search_telegram_messages(query, limit=10):
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
        if not results:
            return f"No messages found for '{query}'"
        return "\n\n".join(results[:10])
    except Exception as e:
        return f"Telegram search failed: {str(e)}"


async def send_telegram_message(contact, message):
    try:
        await telethon_client.connect()
        
        # First try direct send
        try:
            await telethon_client.send_message(contact, message)
            return f"Message sent to {contact}"
        except Exception:
            pass
        
        # Search through dialogs to find matching contact
        async for dialog in telethon_client.iter_dialogs():
            if contact.lower() in dialog.name.lower():
                await telethon_client.send_message(dialog.entity, message)
                return f"Message sent to {dialog.name}"
        
        return f"Could not find contact '{contact}' in your Telegram chats."
    except Exception as e:
        return f"Failed to send Telegram message: {str(e)}"


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
        geo = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1",
            timeout=5
        ).json()
        if not geo.get("results"):
            return f"Could not find weather for {city}"
        loc = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        weather = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&temperature_unit=fahrenheit",
            timeout=5
        ).json()
        cw = weather.get("current_weather", {})
        temp = cw.get("temperature", "N/A")
        wind = cw.get("windspeed", "N/A")
        return f"Weather in {city}: {temp}F, wind {wind} mph"
    except Exception as e:
        return f"Weather fetch failed: {str(e)}"


def get_stocks(symbols):
    try:
        results = []
        for symbol in symbols:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5
            )
            data = r.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice", "N/A")
            prev = meta.get("previousClose", price)
            if price != "N/A" and prev:
                change = ((float(price) - float(prev)) / float(prev)) * 100
                direction = "up" if change >= 0 else "down"
                results.append(f"{symbol}: ${price:.2f} ({direction} {abs(change):.2f}%)")
            else:
                results.append(f"{symbol}: ${price}")
        return "\n".join(results)
    except Exception as e:
        return f"Stock fetch failed: {str(e)}"


def get_calendar_client():
    return caldav.DAVClient(
        url=ICLOUD_CALDAV_URL,
        username=ICLOUD_USERNAME,
        password=ICLOUD_PASSWORD
    )


def get_upcoming_events(days=14):
    try:
        cal_client = get_calendar_client()
        principal = cal_client.principal()
        calendars = principal.calendars()

        if not calendars:
            return "No calendars found on this account."

        now_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = now_local + timedelta(days=days)
        now_utc = now_local.astimezone(pytz.UTC)
        end_utc = end_local.astimezone(pytz.UTC)

        events_list = []
        calendar_names = []

        for calendar in calendars:
            try:
                cal_name = str(calendar.name) if calendar.name else "Unnamed"
                calendar_names.append(cal_name)
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
            return f"No events found in the next {days} days. Calendars checked: {', '.join(calendar_names)}"

        events_list.sort()
        return f"Calendars: {', '.join(calendar_names)}\n\n" + "\n".join(events_list)

    except Exception as e:
        return f"Calendar error: {str(e)}"


def create_calendar_events(events):
    try:
        cal_client = get_calendar_client()
        principal = cal_client.principal()
        calendars = principal.calendars()
        if not calendars:
            return "No calendars found."
        calendar = calendars[0]
        added = []
        failed = []
        for event in events:
            try:
                start_dt = TZ.localize(datetime.strptime(f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M"))
                end_dt = start_dt + timedelta(hours=event.get('duration_hours', 1))
                start_utc = start_dt.astimezone(pytz.UTC)
                end_utc = end_dt.astimezone(pytz.UTC)
                location_line = f"\nLOCATION:{event['location']}" if event.get('location') else ""
                event_data = (
                    "BEGIN:VCALENDAR\n"
                    "VERSION:2.0\n"
                    "PRODID:-//GARVAIS//EN\n"
                    "BEGIN:VEVENT\n"
                    f"SUMMARY:{event['title']}\n"
                    f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}\n"
                    f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}"
                    f"{location_line}\n"
                    "END:VEVENT\n"
                    "END:VCALENDAR"
                )
                calendar.add_event(event_data)
                added.append(f"- {event['title']} on {start_dt.strftime('%A, %B %d at %I:%M %p')}")
            except Exception as e:
                failed.append(f"- {event['title']}: {str(e)}")
        result = ""
        if added:
            result += "Added:\n" + "\n".join(added)
        if failed:
            result += "\nFailed:\n" + "\n".join(failed)
        return result
    except Exception as e:
        return f"Calendar error: {str(e)}"


def get_outlook_access_token():
    try:
        url = f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}/oauth2/v2.0/token"
        data = {
            "client_id": OUTLOOK_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": OUTLOOK_REFRESH_TOKEN,
            "scope": "Mail.Read Mail.Send"
        }
        r = requests.post(url, data=data, timeout=10)
        return r.json().get("access_token")
    except Exception:
        return None


def read_outlook_emails(count=5):
    try:
        token = get_outlook_access_token()
        if not token:
            return "Could not authenticate with Outlook."
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages?$top={count}&$orderby=receivedDateTime desc",
            headers=headers, timeout=10
        )
        emails = r.json().get("value", [])
        if not emails:
            return "No emails found."
        result = []
        for e in emails:
            sender = e.get("from", {}).get("emailAddress", {}).get("address", "Unknown")
            subject = e.get("subject", "No subject")
            preview = e.get("bodyPreview", "")[:200]
            result.append(f"From: {sender}\nSubject: {subject}\nPreview: {preview}")
        return "\n\n---\n\n".join(result)
    except Exception as e:
        return f"Email read failed: {str(e)}"


def send_outlook_email(to, subject, body):
    try:
        token = get_outlook_access_token()
        if not token:
            return "Could not authenticate with Outlook."
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        email_data = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}]
            }
        }
        r = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers, json=email_data, timeout=10
        )
        if r.status_code == 202:
            return f"Email sent to {to}"
        return f"Failed to send email: {r.text}"
    except Exception as e:
        return f"Email send failed: {str(e)}"


def get_news(topic):
    return web_search(f"latest news {topic} today")


async def execute_tool_async(tool_name, params):
    if tool_name == "SEARCH_TELEGRAM":
        return await search_telegram_messages(params.get("query", ""), params.get("limit", 10))
    elif tool_name == "SEND_TELEGRAM":
        return await send_telegram_message(params.get("contact"), params.get("message"))
    return None


def execute_tool(tool_name, params):
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
        return get_news(params.get("topic", ""))
    return None


# ── GROUP CHAT ────────────────────────────────────────────────────────────────

async def generate_group_summary(messages, chat_title, chat_id, context):
    conversation = "\n".join(messages)
    prompt = f"Group chat: {chat_title}\n\nRecent messages:\n{conversation}"

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=GROUP_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    summary = response.content[0].text
    briefing = f"📋 *Group Briefing — {chat_title}*\n\n{summary}"

    await context.bot.send_message(
        chat_id=OWNER_TELEGRAM_ID,
        text=briefing,
        parse_mode='Markdown'
    )

    memory = load_memory()
    memory["active_group_chats"][str(OWNER_TELEGRAM_ID)] = {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "recent_messages": conversation
    }
    save_memory_data(memory)


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    chat_title = update.message.chat.title or "Group Chat"
    sender = update.message.from_user.first_name or "Unknown"
    text = update.message.text

    if update.message.from_user.id == OWNER_TELEGRAM_ID:
        return

    if chat_id not in group_message_buffer:
        group_message_buffer[chat_id] = []

    group_message_buffer[chat_id].append(f"{sender}: {text}")

    if len(group_message_buffer[chat_id]) >= SUMMARY_EVERY_N_MESSAGES:
        await generate_group_summary(
            group_message_buffer[chat_id],
            chat_title,
            chat_id,
            context
        )
        group_message_buffer[chat_id] = []


# ── PRIVATE CHAT ──────────────────────────────────────────────────────────────

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.message.from_user.id
    # Only respond to owner
    if user_id != OWNER_TELEGRAM_ID:
        return

    memory = load_memory()
    pending_draft = memory.get("pending_replies", {}).get(str(user_id))
    active_group = memory.get("active_group_chats", {}).get(str(user_id))

    # ── CONFIRMATION FLOW ──
    if pending_draft:
        msg_lower = user_message.lower().strip()

        if msg_lower in ['yes', 'send', 'confirm', 'send it', 'yes send it']:
            if active_group:
                await context.bot.send_message(
                    chat_id=active_group["chat_id"],
                    text=pending_draft
                )
                await update.message.reply_text("✅ Message sent to the group, sir.")
            else:
                await update.message.reply_text("No active group chat found, sir.")
            clear_pending_reply(user_id)
            return

        elif msg_lower in ['no', 'cancel', 'discard', 'nevermind']:
            await update.message.reply_text("Message discarded, sir. What would you like to say instead?")
            clear_pending_reply(user_id)
            return

        else:
            context_text = active_group.get("recent_messages", "") if active_group else ""
            chat_title = active_group.get("chat_title", "the group") if active_group else "the group"
            draft_prompt = f"Original conversation:\n{context_text}\n\nSir wants to modify and says: {user_message}\n\nDraft an updated message for {chat_title}."
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": draft_prompt}]
            )
            new_draft = response.content[0].text.strip()
            set_pending_reply(user_id, new_draft)
            await update.message.reply_text(
                f"📝 *Updated draft:*\n\n{new_draft}\n\n✅ Reply *yes* to send, *no* to discard, or tell me what to change.",
                parse_mode='Markdown'
            )
            return

    # ── GROUP REPLY SELECTION ──
    if active_group:
        msg_lower = user_message.strip()
        context_text = active_group.get("recent_messages", "")
        chat_title = active_group.get("chat_title", "the group")

        if msg_lower in ['1', '2', '3']:
            draft_prompt = f"User selected option {msg_lower}.\n\nOriginal conversation:\n{context_text}\n\nDraft that reply for {chat_title}."
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": draft_prompt}]
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Ready to send:*\n\n{draft}\n\n✅ Reply *yes* to send, *no* to discard, or tell me what to change.",
                parse_mode='Markdown'
            )
            return

        elif any(phrase in msg_lower for phrase in ['tell them', 'say', 'respond', 'reply', 'send']):
            draft_prompt = f"Original conversation:\n{context_text}\n\nSir wants to say: {user_message}\n\nDraft for {chat_title}."
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=GROUP_DRAFT_PROMPT,
                messages=[{"role": "user", "content": draft_prompt}]
            )
            draft = response.content[0].text.strip()
            set_pending_reply(user_id, draft)
            await update.message.reply_text(
                f"📝 *Draft:*\n\n{draft}\n\n✅ Reply *yes* to send, *no* to discard, or tell me what to change.",
                parse_mode='Markdown'
            )
            return

    # ── NORMAL JARVIS CONVERSATION ──
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

    for _ in range(5):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=system_with_context,
            messages=messages
        )
        reply = response.content[0].text

        tool_match = re.search(r'<TOOL>(.*?)</TOOL>', reply, re.DOTALL)
        if tool_match:
            try:
                tool_call = json.loads(tool_match.group(1).strip())
                tool_name = tool_call.get("tool")
                params = tool_call.get("params", {})

                # Check if it's a Telegram tool (async)
                if tool_name in ["SEARCH_TELEGRAM", "SEND_TELEGRAM"]:
                    tool_result = await execute_tool_async(tool_name, params)
                else:
                    tool_result = execute_tool(tool_name, params)

                if tool_result is None:
                    tool_result = "Unknown tool"

                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[TOOL RESULT for {tool_name}]\n{tool_result}"})
                continue
            except Exception as e:
                reply = f"Tool execution error, sir: {str(e)}"
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


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_private_message
    ))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_group_message
    ))

    print("GARVAIS is online. All systems operational.")
    app.run_polling()


if __name__ == "__main__":
    main()
