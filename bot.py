import os
import anthropic
import json
import re
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ConversationHandler,
    filters, ContextTypes
)
from datetime import datetime, timedelta, time as dt_time
import pytz
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# ── ENV ───────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION   = os.environ.get("TELEGRAM_SESSION", "")
TAVILY_API_KEY     = os.environ.get("TAVILY_API_KEY", "")

OWNER_TELEGRAM_ID    = 1475465779
XEEBI_SALES_GROUP_ID = -1003894146193
INVOICING_THREAD_ID  = 379
XEEBI_NOC_CHAT_ID    = -5236682220
UPM_NEWPORT_CHAT     = "UPM NEWPORT"
MEMORY_FILE          = "/app/jarvis_memory.json"

TZ         = pytz.timezone("America/Los_Angeles")
MOSCOW_TZ  = pytz.timezone("Europe/Moscow")

ASKING_AMOUNT = 1

group_logs        = {}
watch_setup_state = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
1. WEB_SEARCH   — search the web for real-time info
   <TOOL>{"tool": "WEB_SEARCH", "params": {"query": "..."}}</TOOL>

2. GET_WEATHER  — get weather for a location
   <TOOL>{"tool": "GET_WEATHER", "params": {"location": "..."}}</TOOL>

3. SAVE_MEMORY  — remember a fact permanently
   <TOOL>{"tool": "SAVE_MEMORY", "params": {"key": "...", "value": "..."}}</TOOL>

4. READ_TELEGRAM_CHAT — read recent messages from a monitored group
   <TOOL>{"tool": "READ_TELEGRAM_CHAT", "params": {"chat_name": "..."}}</TOOL>

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
    "moscow": MOSCOW_TZ,
    "msk":    MOSCOW_TZ,
    "russia": MOSCOW_TZ,
    "pst":    TZ,
    "pacific": TZ,
    "la":     TZ,
    "utc":    pytz.UTC,
    "gmt":    pytz.UTC,
}

def parse_schedule_time(text):
    """
    Extract a scheduled UTC datetime and source timezone from text.
    E.g. '9am Moscow time', '9:00 MSK', 'at 9 pst'
    Returns (datetime_utc, source_tz) or (None, None).
    """
    text_lower = text.lower()

    # Detect timezone — iterate longest keyword first to avoid partial matches
    tz = MOSCOW_TZ  # default
    for keyword, timezone in TIMEZONE_MAP.items():
        if keyword in text_lower:
            tz = timezone
            break

    # Match time: "9am", "9:00am", "9:00 am", "9 am", "9:00", plain "9"
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
        # No am/pm context: 1–6 almost certainly means afternoon/evening
        hour += 12

    now       = datetime.now(tz)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If the time already passed today, push to tomorrow
    if scheduled <= now:
        scheduled += timedelta(days=1)

    return scheduled.astimezone(pytz.UTC), tz


def is_schedule_intent(text):
    """Return True if the user wants to schedule rather than send immediately."""
    patterns = [
        r"do\s+it\s+at",
        r"send\s+at",
        r"schedule",
        r"not\s+now",
        r"don'?t\s+send\s+now",
        r"do\s+not\s+send.*?now",
        r"send.*?later",
        r"send.*?at\s+\d",
        r"at\s+\d+\s*(am|pm)",
        r"\d+\s*(am|pm).*time",
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


# ── TOOL EXECUTION ────────────────────────────────────────────────────────────
def execute_tool(tool_name, params):
    if tool_name == "WEB_SEARCH":
        query = params.get("query", "")
        try:
            resp = requests.post(
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

    return f"Unknown tool: {tool_name}"


# ── SCHEDULED MESSAGE JOB ─────────────────────────────────────────────────────
async def send_scheduled_message(context):
    """Job callback: fires a previously scheduled outgoing message."""
    data     = context.job.data
    job_id   = data["job_id"]
    owner_id = data["owner_id"]

    memory = load_memory()
    jobs   = memory.get("scheduled_jobs", [])
    job    = next((j for j in jobs if j["id"] == job_id), None)

    if not job:
        return

    message     = job["message"]
    method      = job.get("method", "telethon")
    destination = job.get("destination", "destination")

    try:
        entity_name = (job.get("telethon_entity") or job.get("destination") or "").lower()
        if "xeebi noc" in entity_name:
            # Bot is already a member — use bot API directly, no Telethon needed
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
    """Immediately deliver a pending draft to its recorded destination."""
    if pending_meta and pending_meta.get("type") == "telethon":
        entity = pending_meta.get("entity", "")
        if "xeebi noc" in entity.lower():
            # Bot is already a member — use bot API directly, no Telethon needed
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
    """Store job metadata in memory and create the job_queue entry."""
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
        "chat_id":        chat_id,
        "chat_title":     chat_title,
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

    memory   = load_memory()
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
    chat_title     = update.message.chat.title or "this group"
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

    # Always post to XEEBI Invoicing thread
    await context.bot.send_message(
        chat_id=XEEBI_SALES_GROUP_ID,
        message_thread_id=INVOICING_THREAD_ID,
        text=invoice_message,
        parse_mode="Markdown",
    )

    # Global Telecom also gets a copy to UPM NEWPORT via Telethon
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

    # Check watch rules
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
    # This block handles any draft that is awaiting confirmation/scheduling.
    # It is checked FIRST so context never bleeds into the Claude conversation path.
    if pending_draft:
        msg_lower = user_message.lower().strip()

        # ➤ Send immediately
        if msg_lower in ("yes", "send", "confirm", "send it", "yes send it", "yes, send it"):
            try:
                await _send_pending_draft(context, pending_draft, pending_meta, active_group)
                await update.message.reply_text("Message sent, sir. ✅")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Could not send: {e}")
            clear_pending_reply(user_id)
            return

        # ➤ Discard
        if msg_lower in ("no", "cancel", "discard", "stop"):
            await update.message.reply_text("Message discarded, sir.")
            clear_pending_reply(user_id)
            return

        # ➤ Schedule intent — MUST be checked before the redraft path
        #   Catches: "yes but at 9am Moscow", "do it at 9 MSK", "schedule for 9am", etc.
        if is_schedule_intent(user_message):
            scheduled_utc, source_tz = parse_schedule_time(user_message)
            if scheduled_utc:
                delay       = max((scheduled_utc - datetime.now(pytz.UTC)).total_seconds(), 1)
                label       = tz_label(source_tz)
                display_time = scheduled_utc.astimezone(source_tz).strftime("%I:%M %p")

                # Determine destination label for confirmation message
                if pending_meta and pending_meta.get("entity"):
                    destination = pending_meta["entity"]
                elif active_group:
                    destination = active_group.get("chat_title", "the group")
                else:
                    destination = "the destination"

                # Build the job record
                job_id   = f"job_{int(datetime.now().timestamp())}"
                job_data = {
                    "id":             job_id,
                    "message":        pending_draft,
                    "destination":    destination,
                    "scheduled_utc":  scheduled_utc.isoformat(),
                    "method":         "telethon" if (pending_meta and pending_meta.get("type") == "telethon") else "bot",
                }
                if pending_meta and pending_meta.get("type") == "telethon":
                    job_data["telethon_entity"] = pending_meta.get("entity")
                elif active_group:
                    job_data["chat_id"]  = active_group.get("chat_id")
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

        # ➤ Redraft — user gave revision instructions
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
                # Strip everything from <DEST> onward for the display text
                display    = reply[: reply.index("<DEST>")].strip()
                # Extract just the draft text (after "📝 *Draft:*\n\n" if present)
                draft_text = re.sub(r"^📝\s*\*Draft:\*\s*\n+", "", display, flags=re.IGNORECASE).strip()
                set_pending_reply(user_id, draft_text, meta=dest_data)
                await update.message.reply_text(
                    display + "\n\nReply *yes* to send immediately, "
                    "*schedule [time] [timezone]* to schedule, or tell me what to change.",
                    parse_mode="Markdown",
                )
            except Exception:
                # If parsing fails, just show the response without DEST block
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
    print("Scheduled briefings set for 9:00 AM and 12:00 PM PST.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Invoice conversation flow
    invoice_conv = ConversationHandler(
        entry_points=[CommandHandler("invoice", handle_invoice_command)],
        states={ASKING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invoice_amount)]},
        fallbacks=[CommandHandler("cancel", handle_invoice_cancel)],
        per_chat=False,
        per_user=True,
    )
    app.add_handler(invoice_conv)

    # Owner commands
    app.add_handler(CommandHandler("brief",       handle_brief_command))
    app.add_handler(CommandHandler("groups",      handle_groups_command))
    app.add_handler(CommandHandler("watch",       handle_watch_command))
    app.add_handler(CommandHandler("watches",     handle_watches_command))
    app.add_handler(CommandHandler("deletewatch", handle_deletewatch_command))
    app.add_handler(CommandHandler("scheduled",   handle_scheduled_command))

    # Message handlers
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
