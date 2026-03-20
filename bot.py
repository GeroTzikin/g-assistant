import os
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

SYSTEM_PROMPT = """You are G.A.R.V.I.S. (G's Advanced Research and Versatile Intelligence System), a personal AI assistant modeled after J.A.R.V.I.S. from Iron Man. Your personality: Professional, composed, direct, strategic, subtly witty, and deeply loyal. Always refer to your user as 'sir'. Lead with the most important information. Be the smartest person in the room."""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
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
