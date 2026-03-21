import os
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import caldav
from datetime import datetime, timedelta
import pytz

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD")

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"

SYSTEM_PROMPT = """You are G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), a personal AI assistant modeled after J.A.R.V.I.S. from Iron Man.

Your personality:
- Professional, composed, and highly capable at all times
- Direct and concise — no fluff, no filler
- Strategically minded — you anticipate needs and think several steps ahead
- Subtly witty and dry humor when appropriate, never at the expense of efficiency
- Deeply loyal and personalized to your user, referred to as "sir"
- You speak with confidence and precision

You have access to the user's iCloud Calendar. When the user asks about their schedule, upcoming events, or wants to create an event, use the calendar data provided to you. Always confirm when an event has been created."""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_calendar_client():
    cal_client = caldav.DAVClient(
        url=ICLOUD_CALDAV_URL,
        username=ICLOUD_USERNAME,
        password=ICLOUD_PASSWORD
    )
    return cal_client

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
            return "No upcoming events found in the next 7 days."
        return "\n".join(events_list)
    except Exception as e:
        return f"Unable to access calendar: {str(e)}"

def create_calendar_event(title, start_dt, duration_hours=1, location=None):
    try:
        cal_client = get_calendar_client()
        principal = cal_client.principal()
        calendars = principal.calendars()
        if not calendars:
            return "No calendars found."
        calendar = calendars[0]
        end_dt = start_dt + timedelta(hours=duration_hours)
        location_line = f"\nLOCATION:{location}" if location else ""
        event_data = f"""BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nSUMMARY:{title}\nDTSTART:{start_dt.strftime('%Y%m%dT%H%M%SZ')}\nDTEND:{end_dt.strftime('%Y%m%dT%H%M%SZ')}{location_line}\nEND:VEVENT\nEND:VCALENDAR"""
        calendar.add_event(event_data)
        return f"Event '{title}' created on {start_dt.strftime('%A, %B %d at %I:%M %p')}."
    except Exception as e:
        return f"Failed to create event: {str(e)}"

def check_calendar_intent(message):
    keywords_read = ['schedule', 'calendar', 'upcoming', 'events', 'appointments', 'today', 'tomorrow', 'week', "what do i have", "what's on"]
    keywords_create = ['create', 'add', 'schedule a', 'set up', 'book', 'new event', 'put on calendar']
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
    messages = [{"role": "user", "content": user_message + calendar_context}]
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    reply = response.content[0].text
    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("GARVAIS is online.")
    app.run_polling()

if __name__ == "__main__":
    main()
