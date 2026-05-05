import os
import asyncio
import logging
import feedparser
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# ========================
# 🔑 КОНФІГУРАЦІЯ
# ========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")

logging.basicConfig(level=logging.INFO)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

news_storage = []

RSS_FEEDS = [
    "https://mmr.ua/rss",
    "https://sostav.ua/rss/news.xml",
    "https://ain.ua/feed/",
    "https://mc.today/feed/",
    "https://news.google.com/rss/search?q=маркетинг+SMM+Instagram+TikTok&hl=uk&gl=UA&ceid=UA:uk",
]

KEYWORDS = [
    "маркетинг", "реклама", "smm", "таргет", "контент",
    "instagram", "tiktok", "facebook", "youtube", "linkedin",
    "блогер", "інфлюенсер", "social media", "ads", "campaign"
]

# ========================
# 🤖 AI ЛОГІКА
# ========================
def ai_request(prompt):
    try:
        res = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"AI error: {e}")
        return None

async def ai_async(prompt):
    return await asyncio.to_thread(ai_request, prompt)

def prompt_template(title):
    return f"""Перепиши новину простою українською мовою.

Формат:
ЗАГОЛОВОК: Одне коротке речення, що починається з ключового слова (наприклад: Аналіз, Оцінка, Запуск, Оновлення)
ОПИС: 1 коротке речення про суть новини людською мовою.

Новина:
{title}
"""

# ========================
# 🔍 ФІЛЬТРАЦІЯ ТА RSS
# ========================
def is_relevant(title):
    return any(k in title.lower() for k in KEYWORDS)

def fetch_news():
    global news_storage
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    new_items = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                if not is_relevant(entry.title):
                    continue
                if any(n["link"] == entry.link for n in news_storage):
                    continue

                item = {
                    "title": entry.title.strip(),
                    "link": entry.link,
                    "date": today,
                    "title_ua": None,
                    "summary_ua": None,
                }
                new_items.append(item)
                news_storage.append(item)
        except Exception as e:
            logging.error(f"RSS error: {e}")

    news_storage = [n for n in news_storage if n["date"] >= yesterday]
    return new_items

# ========================
# ⚙️ ОБРОБКА ТЕКСТУ
# ========================
sem = asyncio.Semaphore(5)

async def process_one(item):
    async with sem:
        result = await ai_async(prompt_template(item["title"]))

    if not result:
        item["title_ua"] = item["title"]
        item["summary_ua"] = ""
        return

    try:
        for line in result.splitlines():
            if line.startswith("ЗАГОЛОВОК:"):
                item["title_ua"] = line.split(":", 1)[1].strip()
            elif line.startswith("ОПИС:"):
                item["summary_ua"] = line.split(":", 1)[1].strip()
    except:
        item["title_ua"] = item["title"]
        item["summary_ua"] = ""

async def process_news(items):
    await asyncio.gather(*(process_one(i) for i in items[:30]))

# ========================
# 📝 ФОРМАТУВАННЯ (ЗМІНЕНО)
# ========================
def format_list(items):
    if not items:
        return "📭 Нема новин"

    text = ""
    for i, n in enumerate(items[:15], 1):
        title = n.get("title_ua") or n["title"]
        summary = n.get("summary_ua") or ""
        link = n["link"]

        # Розділяємо заголовок на перше слово та все інше
        parts = title.split(" ", 1)
        
        if len(parts) > 1:
            first_word = parts[0]
            rest_of_title = parts[1]
            # Формат: 1. [Слово](посилання) решта — опис
            text += f"{i}. [{first_word}]({link}) {rest_of_title} — {summary}\n\n"
        else:
            # На випадок, якщо заголовок складається лише з одного слова
            text += f"{i}. [{title}]({link}) — {summary}\n\n"

    return text.strip()

# ========================
# 📱 ТЕЛЕГРАМ ІНТЕРФЕЙС
# ========================
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Сьогодні", callback_data="today")],
        [InlineKeyboardButton("📁 Вчора", callback_data="yesterday")],
        [InlineKeyboardButton("🤖 Дайджест", callback_data="digest")],
        [InlineKeyboardButton("🔄 Оновити", callback_data="refresh")]
    ])

async def send_digest(chat_id=None):
    chat_id = chat_id or CHAT_ID
    if not chat_id:
        logging.error("CHAT_ID is not defined")
        return

    new = fetch_news()
    await process_news(new)

    today = datetime.now().date()
    items = [n for n in news_storage if n["date"] == today]

    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id,
        format_list(items),
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=menu()
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    if q.data == "today":
        items = [n for n in news_storage if n["date"] == today]
        text = format_list(items)
    elif q.data == "yesterday":
        items = [n for n in news_storage if n["date"] == yesterday]
        text = format_list(items)
    elif q.data == "digest":
        items = [n for n in news_storage if n["date"] == today]
        text = format_list(items)
    elif q.data == "refresh":
        new = fetch_news()
        await process_news(new)
        text = f"✅ Оновлено ({len(new)} новин)"
    else:
        text = "❓ Помилка"

    await q.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=menu()
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Новини маркетингу та соцмереж",
        reply_markup=menu()
    )

async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Генерую...")
    await send_digest(update.message.chat_id)

# ========================
# ⏰ ПЛАНУВАЛЬНИК ТА ЗАПУСК
# ========================
scheduler = BackgroundScheduler()

def job():
    asyncio.run(send_digest())

def main():
    logging.info("🚀 Bot started")

    # Первинний збір
    fetch_news()

    scheduler.add_job(job, "cron", hour=9, minute=0)
    scheduler.start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()

if __name__ == "__main__":
    main()