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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
OUTLOOK_CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID")
OUTLOOK_TENANT_ID = os.environ.get("OUTLOOK_TENANT_ID")
OUTLOOK_CLIENT_SECRET = os.environ.get("OUTLOOK_CLIENT_SECRET")

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
MEMORY_FILE = "/app/jarvis_memory.json"
TZ = pytz.timezone('America/Los_Angeles')

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

To use a tool, output ONLY the tool call in this format with no other text:
<TOOL>
{
  "tool": "TOOL_NAME",
  "params": { ... }
}
</TOOL>

After receiving tool results, respond naturally in Jarvis character.

For CREATE_EVENTS, params format:
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

Always use tools when they would provide better answers. Chain multiple tools if needed."""


def load_memory():
    try:
        if Path(MEMORY_FILE).exists():
            with open(MEMORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"facts": {}, "history": []}


def save_memory_fact(key, value):
    memory = load_memory()
    memory["facts"][key] = value
    with open(MEMORY_FILE, 'w') as f:
        json.dump(memory, f)
    return f"Memory saved: {key} = {value}"


def add_to_history(role, content):
    memory = load_memory()
    memory["history"].append({"role": role, "content": content, "time": datetime.now().isoformat()})
    memory["history"] = memory["history"][-50:]
    with open(MEMORY_FILE, 'w') as f:
        json.dump(memory, f)


def get_recent_history(n=10):
    memory = load_memory()
    return memory["history"][-n:]


def get_memory_facts():
    memory = load_memory()
    facts = memory.get("facts", {})
    if not facts:
        return ""
    return "\n".join([f"- {k}: {v}" for k, v in facts.items()])


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


def get_outlook_token():
    url = f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": OUTLOOK_CLIENT_ID,
        "client_secret": OUTLOOK_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }
    r = requests.post(url, data=data, timeout=10)
    return r.json().get("access_token")


def read_outlook_emails(count=5):
    try:
        token = get_outlook_token()
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
        token = get_outlook_token()
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
    return "Unknown tool"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
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
                tool_result = execute_tool(tool_name, params)
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


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("GARVAIS is online. All systems operational.")
    app.run_polling()


if __name__ == "__main__":
    main()
