import os
import asyncio
import logging
import feedparser
import httpx
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.error import TelegramError
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

def prompt_template(title, description=""):
    extra = f"\nДодатковий контекст із статті: {description}" if description else ""
    return f"""Ти — професійний маркетинговий аналітик. Проаналізуй новину.

ФОРМАТ ВІДПОВІДІ (суворо дотримуйся, без зайвих слів):
ЗАГОЛОВОК: [Влучний заголовок до 10 слів, без лапок]
СУТЬ: [2-3 конкретних речення: що сталося → чому важливо для маркетологів → який практичний вплив або висновок]

Новина: {title}{extra}
"""

# ========================
# 🖼️ ПАРСИНГ ЗОБРАЖЕНЬ З RSS
# ========================
def extract_image_from_entry(entry) -> str | None:
    """Витягує URL зображення з RSS-запису різними способами."""
    # 1. media:content
    if hasattr(entry, "media_content") and entry.media_content:
        for m in entry.media_content:
            url = m.get("url", "")
            if url and any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                return url

    # 2. media:thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url", "")
        if url:
            return url

    # 3. enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            url = enc.get("href", enc.get("url", ""))
            if url and "image" in enc.get("type", "image"):
                return url

    # 4. <img> тег у summary/content
    content = ""
    if hasattr(entry, "summary") and entry.summary:
        content = entry.summary
    elif hasattr(entry, "content") and entry.content:
        content = entry.content[0].get("value", "")

    if content:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
        if match:
            url = match.group(1)
            if url.startswith("http"):
                return url

    return None

async def is_image_accessible(url: str) -> bool:
    """Перевіряє чи доступне зображення за URL."""
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
            r = await c.head(url)
            ct = r.headers.get("content-type", "")
            return r.status_code == 200 and "image" in ct
    except Exception:
        return False

async def find_cover_image(items: list) -> str | None:
    """Шукає перше доступне зображення серед перших 10 новин."""
    for n in items[:10]:
        url = n.get("image_url")
        if url and await is_image_accessible(url):
            return url
    return None

# ========================
# 🔍 RSS ТА ФІЛЬТРАЦІЯ
# ========================
def is_relevant(title):
    return any(k in title.lower() for k in KEYWORDS)

def fetch_news():
    """
    Завантажує нові новини з RSS.
    Зберігає реальну дату публікації з RSS-фіду.
    Видаляє новини старше 2 днів.
    """
    global news_storage
    today = datetime.now().date()
    two_days_ago = today - timedelta(days=2)
    new_items = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                if not is_relevant(entry.title):
                    continue
                if any(n["link"] == entry.link for n in news_storage):
                    continue

                # Визначаємо дату публікації з RSS
                pub_date = today
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:3]).date()
                    except Exception:
                        pass
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    try:
                        pub_date = datetime(*entry.updated_parsed[:3]).date()
                    except Exception:
                        pass

                if pub_date < two_days_ago:
                    continue

                # Короткий опис із RSS для AI-контексту
                description = ""
                if hasattr(entry, "summary") and entry.summary:
                    description = re.sub(r"<[^>]+>", "", entry.summary)[:400].strip()

                item = {
                    "title": entry.title.strip(),
                    "link": entry.link,
                    "date": pub_date,
                    "description": description,
                    "image_url": extract_image_from_entry(entry),
                    "title_ua": None,
                    "summary_ua": None,
                }
                new_items.append(item)
                news_storage.append(item)
        except Exception as e:
            logging.error(f"RSS error [{url}]: {e}")

    news_storage = [n for n in news_storage if n["date"] >= two_days_ago]
    return new_items

# ========================
# ⚙️ ОБРОБКА ТЕКСТУ AI
# ========================
sem = asyncio.Semaphore(5)

async def process_one(item):
    async with sem:
        result = await ai_async(prompt_template(item["title"], item.get("description", "")))

    if not result:
        item["title_ua"] = item["title"]
        item["summary_ua"] = "Деталі доступні у джерелі."
        return

    try:
        lines = result.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("ЗАГОЛОВОК:"):
                item["title_ua"] = stripped.split(":", 1)[1].strip()
            elif stripped.upper().startswith("СУТЬ:"):
                item["summary_ua"] = stripped.split(":", 1)[1].strip()

        if not item.get("title_ua"):
            item["title_ua"] = item["title"]
        if not item.get("summary_ua"):
            item["summary_ua"] = "Деталі доступні у джерелі."

    except Exception:
        item["title_ua"] = item["title"]
        item["summary_ua"] = "Деталі доступні у джерелі."

async def process_news(items):
    if not items:
        return
    await asyncio.gather(*(process_one(i) for i in items[:30]))

# ========================
# 📝 ФОРМАТУВАННЯ
# ========================
def format_list(items: list, label: str = "сьогодні") -> str:
    if not items:
        return f"📭 Новин за {label} поки немає."

    date_str = items[0]["date"].strftime("%d.%m.%Y")
    text = f"📰 *Маркетингові новини — {date_str}*\n\n"

    for i, n in enumerate(items[:10], 1):
        title = n.get("title_ua") or n["title"]
        summary = n.get("summary_ua") or "Деталі доступні у джерелі."
        link = n["link"]
        text += f"*{i}. {title}*\n{summary}\n[Читати далі →]({link})\n\n"

    return text.strip()

# ========================
# 📱 МЕНЮ
# ========================
def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📰 Сьогодні", callback_data="today"),
            InlineKeyboardButton("📁 Вчора", callback_data="yesterday"),
        ],
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh"),
        ],
    ])

# ========================
# 📤 ВІДПРАВКА ДАЙДЖЕСТУ
# ========================
async def send_single_digest(bot: Bot, chat_id, items: list, label: str = "сьогодні"):
    """
    Відправляє дайджест одним повідомленням.
    Якщо знайдено фото — відправляє як photo+caption, інакше текстом.
    """
    text = format_list(items, label)
    cover = await find_cover_image(items)

    # Telegram caption обмежений 1024 символами — обрізаємо якщо треба
    caption = text if len(text) <= 1024 else text[:1020] + "…"

    try:
        if cover:
            await bot.send_photo(
                chat_id=chat_id,
                photo=cover,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=menu()
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=menu()
            )
    except TelegramError as e:
        logging.warning(f"Помилка відправки (fallback до тексту): {e}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=menu()
            )
        except TelegramError as e2:
            logging.error(f"Критична помилка відправки: {e2}")

async def send_digest(chat_id=None):
    """Головна функція — збирає та надсилає свіжий дайджест."""
    chat_id = chat_id or CHAT_ID
    if not chat_id:
        return

    new = fetch_news()
    await process_news(new)

    today = datetime.now().date()
    items = [n for n in news_storage if n["date"] == today]

    bot = Bot(token=TELEGRAM_TOKEN)
    await send_single_digest(bot, chat_id, items, label="сьогодні")

# ========================
# 🎛️ ОБРОБНИКИ КОМАНД ТА КНОПОК
# ========================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    bot = context.bot
    chat_id = q.message.chat_id

    if q.data == "today":
        items = [n for n in news_storage if n["date"] == today]
        if not items:
            await q.message.reply_text("⏳ Завантажую свіжі новини...")
            new = fetch_news()
            await process_news(new)
            items = [n for n in news_storage if n["date"] == today]
        await send_single_digest(bot, chat_id, items, label="сьогодні")

    elif q.data == "yesterday":
        items = [n for n in news_storage if n["date"] == yesterday]
        if not items:
            await q.message.reply_text("⏳ Шукаю вчорашні новини...")
            new = fetch_news()
            await process_news(new)
            items = [n for n in news_storage if n["date"] == yesterday]
        if not items:
            await bot.send_message(
                chat_id=chat_id,
                text="📭 Вчорашніх новин не знайдено.\nMожливо RSS-джерела ще не містять матеріалів за вчора.",
                reply_markup=menu()
            )
            return
        await send_single_digest(bot, chat_id, items, label="вчора")

    elif q.data == "refresh":
        await q.message.reply_text("⏳ Оновлюю стрічку...")
        new = fetch_news()
        await process_news(new)
        today_items = [n for n in news_storage if n["date"] == today]
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ Готово\\! Нових: *{len(new)}*, всього сьогодні: *{len(today_items)}*",
            parse_mode="MarkdownV2",
            reply_markup=menu()
        )

    else:
        await bot.send_message(chat_id=chat_id, text="❓ Невідома команда", reply_markup=menu())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Вітаю\\! Я — агрегатор маркетингових новин\\.*\n\n"
        "Щодня о 9:00 надсилаю підбірку найважливішого з SMM, реклами та digital\\.\n\n"
        "📌 Команди:\n"
        "/digest — свіжий дайджест\n"
        "/menu — показати меню",
        parse_mode="MarkdownV2",
        reply_markup=menu()
    )


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формую підбірку...")
    await send_digest(update.message.chat_id)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /menu — показує меню в будь-який момент."""
    await update.message.reply_text(
        "📋 Обери що тебе цікавить:",
        reply_markup=menu()
    )

# ========================
# ⏰ SCHEDULER
# ========================
scheduler = BackgroundScheduler()

def job():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_digest())

# ========================
# MAIN
# ========================
def main():
    logging.info("🚀 Бот стартував")
    fetch_news()

    scheduler.add_job(job, "cron", hour=9, minute=0)
    scheduler.start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()

if __name__ == "__main__":
    main()