import os
import anthropic
import caldav
import json
import re
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta
import pytz

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD")
ICLOUD_CALDAV_URL = "https://caldav.icloud.com"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), a personal AI assistant modeled after J.A.R.V.I.S. from Iron Man.

Your personality:
- Professional, composed, and highly capable at all times
- Direct and concise — no fluff, no filler
- Strategically minded — you anticipate needs and think several steps ahead
- Subtly witty and dry humor when appropriate
- Deeply loyal, always refer to your user as "sir"
- Speak with confidence and precision

You have access to the user's iCloud Calendar.

When the user wants to CREATE an event, extract the details and respond ONLY with a JSON block in this exact format (no other text):
<CREATE_EVENT>
{
  "title": "Event title",
  "date": "YYYY-MM-DD",
  "time": "HH:MM",
  "duration_hours": 1,
  "location": "optional location or null"
}
</CREATE_EVENT>

When the user asks about their SCHEDULE, the calendar data will be provided to you — summarize it clearly and concisely in Jarvis style.

For all other questions, respond normally in character."""

def get_calendar_client():
    return caldav.DAVClient(
        url=ICLOUD_CALDAV_URL,
        username=ICLOUD_USERNAME,
        password=ICLOUD_PASSWORD
    )

def get_upcoming_events(days=7):
    try:
        cal_client = get_calendar_client()
        principal = cal_client.principal()
        calendars = principal.calendars()
        now = datetime.now(pytz.UTC)
        end = now + timedelta(days=days)
        events_list = []
        for calendar in calendars:
            try:
                events = calendar.date_search(start=now, end=end)
                for event in events:
                    event.load()
                    vevent = event.vobject_instance.vevent
                    summary = str(vevent.summary.value) if hasattr(vevent, 'summary') else 'No title'
                    dtstart = vevent.dtstart.value
                    if hasattr(dtstart, 'strftime'):
                        start_str = dtstart.strftime('%A, %B %d at %I:%M %p')
                    else:
                        start_str = str(dtstart)
                    events_list.append(f"- {summary}: {start_str}")
            except Exception:
                continue
        if not events_list:
            return "No upcoming events found."
        return "\n".join(events_list)
    except Exception as e:
        return f"Unable to access calendar: {str(e)}"

def create_calendar_event(title, date_str, time_str, duration_hours=1, location=None):
    try:
        cal_client = get_calendar_client()
        principal = cal_client.principal()
        calendars = principal.calendars()
        if not calendars:
            return False, "No calendars found."
        calendar = calendars[0]
        
        tz = pytz.timezone('America/Los_Angeles')
        start_dt = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
        end_dt = start_dt + timedelta(hours=duration_hours)
        
        start_utc = start_dt.astimezone(pytz.UTC)
        end_utc = end_dt.astimezone(pytz.UTC)
        
        location_line = f"\nLOCATION:{location}" if location else ""
        
        event_data = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//GARVAIS//EN
BEGIN:VEVENT
SUMMARY:{title}
DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}
DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}{location_line}
END:VEVENT
END:VCALENDAR"""
        
        calendar.add_event(event_data)
        return True, start_dt.strftime('%A, %B %d at %I:%M %p')
    except Exception as e:
        return False, str(e)

def check_calendar_intent(message):
    keywords_read = ['schedule', 'calendar', 'upcoming', 'events', 'appointments', 'today', 'tomorrow', 'week', 'what do i have', "what's on"]
    keywords_create = ['add', 'create', 'schedule a', 'set up', 'book', 'new event', 'put on calendar', 'add to calendar', 'remind me']
    message_lower = message.lower()
    if any(k in message_lower for k in keywords_create):
        return 'create'
    if any(k in message_lower for k in keywords_read):
        return 'read'
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    calendar_context = ""
    intent = check_calendar_intent(user_message)

    if intent == 'read':
        events = get_upcoming_events(days=7)
        calendar_context = f"\n\n[CALENDAR DATA - Next 7 days]\n{events}"

    today = datetime.now(pytz.timezone('America/Los_Angeles'))
    date_context = f"\n\n[TODAY'S DATE: {today.strftime('%A, %B %d, %Y')}]"

    messages = [{"role": "user", "content": user_message + calendar_context + date_context}]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )

    reply = response.content[0].text

    # Check if Claude wants to create an event
    event_match = re.search(r'<CREATE_EVENT>(.*?)</CREATE_EVENT>', reply, re.DOTALL)
    if event_match:
        try:
            event_data = json.loads(event_match.group(1).strip())
            success, result = create_calendar_event(
                title=event_data['title'],
                date_str=event_data['date'],
                time_str=event_data['time'],
                duration_hours=event_data.get('duration_hours', 1),
                location=event_data.get('location')
            )
            if success:
                reply = f"Done, sir. '{event_data['title']}' has been added to your calendar for {result}."
            else:
                reply = f"I encountered an issue adding the event, sir: {result}"
        except Exception as e:
            reply = f"I had trouble parsing the event details, sir: {str(e)}"

    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("GARVAIS is online.")
    app.run_polling()

if __name__ == "__main__":
    main()
