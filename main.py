import os
import random
import time
import logging
import threading
import re
import asyncio
import json
from datetime import datetime, timedelta, date
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

import groq
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# ===== НАСТРОЙКИ =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TOPIC_IDS = {
    "быт": 2, "работа": 5, "отношения": 12, "деньги": 15, "еда": 17,
    "спорт": 19, "гаджеты": 21, "учёба": 23, "транспорт": 25, "абсурд": 27,
}

TOPIC_EMOJI = {
    "быт": "🏠", "работа": "💼", "отношения": "❤️", "деньги": "💰", "еда": "🍔",
    "спорт": "🏁", "гаджеты": "💻", "учёба": "📚", "транспорт": "✈️", "абсурд": "🎭",
}

TOPIC_WEIGHTS = {
    "быт": 1.2, "работа": 1.1, "отношения": 1.3, "деньги": 1.0, "еда": 1.0,
    "спорт": 0.9, "гаджеты": 1.2, "учёба": 0.85, "транспорт": 0.9, "абсурд": 1.4,
}

DEFAULT_TOPIC = "быт"

FORMATS = []
FORMAT_HASHTAGS = {}
FORMAT_MAX_TOKENS = {}

MAX_INVITES_PER_DAY = 10
MAX_PERSONAL_JOKES_PER_DAY = 10

HOLIDAYS = [
    {"name": "Новый год", "month": 1, "day": 1},
    {"name": "День защитника Отечества", "month": 2, "day": 23},
    {"name": "Международный женский день", "month": 3, "day": 8},
    {"name": "День смеха", "month": 4, "day": 1},
    {"name": "День Победы", "month": 5, "day": 9},
    {"name": "День России", "month": 6, "day": 12},
    {"name": "День знаний", "month": 9, "day": 1},
    {"name": "День народного единства", "month": 11, "day": 4},
]

DATA_FILE = "data.json"
CONFIG_FILE = "config.json"
PERSONAL_TOPIC_ID = None

def load_config():
    global FORMATS, FORMAT_HASHTAGS, FORMAT_MAX_TOKENS, HOLIDAYS
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            FORMATS = config.get("formats", ["анекдот", "вопрос-ответ", "игра слов", "смешное определение", "диалог"])
            FORMAT_HASHTAGS = config.get("hashtags", {})
            FORMAT_MAX_TOKENS = config.get("max_tokens", {})
            HOLIDAYS = config.get("holidays", HOLIDAYS)
    else:
        FORMATS = ["анекдот", "вопрос-ответ", "игра слов", "смешное определение", "диалог"]
        FORMAT_HASHTAGS = {
            "анекдот": "#анекдоты",
            "вопрос-ответ": "#вопрос_ответ",
            "игра слов": "#игра_слов",
            "смешное определение": "#смешные_определения",
            "диалог": "#диалоги",
        }
        FORMAT_MAX_TOKENS = {
            "анекдот": 250,
            "вопрос-ответ": 200,
            "игра слов": 180,
            "смешное определение": 200,
            "диалог": 220,
        }

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"invites": {}, "daily_limits": {}, "user_settings": {}, "personal_jokes_given": {}, "personal_topic_description_sent": False}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_config()
data = load_data()
groq_client = groq.Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# ===== ФУНКЦИИ =====
def get_thread_id(topic):
    return TOPIC_IDS.get(topic)

def get_current_holiday():
    today = datetime.now()
    for holiday in HOLIDAYS:
        holiday_date = datetime(today.year, holiday["month"], holiday["day"])
        delta = today - holiday_date
        if -7 <= delta.days <= 7:
            return holiday["name"]
    return None

def get_holiday_bias():
    today = datetime.now()
    closest_holiday = None
    min_delta = 100
    for holiday in HOLIDAYS:
        holiday_date = datetime(today.year, holiday["month"], holiday["day"])
        delta = (today - holiday_date).days
        if abs(delta) < min_delta:
            min_delta = abs(delta)
            closest_holiday = holiday
    if closest_holiday is None:
        return 0.0
    delta = (today - datetime(today.year, closest_holiday["month"], closest_holiday["day"])).days
    if delta < -7 or delta > 7:
        return 0.0
    if delta <= 0:
        prob = 0.7 + 0.1 * delta
    else:
        prob = 0.7 - 0.1 * delta
    prob = max(0.05, min(0.7, prob))
    return prob

def select_topic():
    topics = list(TOPIC_WEIGHTS.keys())
    weights = [TOPIC_WEIGHTS[t] for t in topics]
    return random.choices(topics, weights=weights, k=1)[0]

def generate_joke_sync(topic, format_type=None, holiday=None, user_settings=None):
    if format_type is None:
        format_type = random.choice(FORMATS)
    hashtag = FORMAT_HASHTAGS.get(format_type, "")
    max_tokens = FORMAT_MAX_TOKENS.get(format_type, 250)

    prompt = f"Сгенерируй {format_type} на тему «{topic}»."
    if holiday:
        prompt += f" Приурочь его к празднику: {holiday}."
    if user_settings:
        if user_settings.get("name"):
            prompt += f" Используй имя {user_settings['name']}."
        if user_settings.get("gender"):
            prompt += f" Учитывай пол: {user_settings['gender']}."
        if user_settings.get("age"):
            prompt += f" Возраст: {user_settings['age']}."
    prompt += (
        " Пиши кратко и смешно. Это должен быть законченный текст, без обрыва. "
        "Не используй Markdown или HTML-теги. Только чистый текст с переносами строк. "
        "В конце добавь тег [ТЕМА: {topic}] и хэштег " + hashtag + "."
    )

    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": "Ты - генератор юмора. Пиши смешно и законченно."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=max_tokens,
        )
        raw_text = response.choices[0].message.content.strip()
        logger.info(f"Groq ответ: {raw_text}")
    except Exception as e:
        logger.error(f"Ошибка вызова Groq: {e}")
        return None

    joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
    joke_text = re.sub(r'#\S+', '', joke_text).strip()
    joke_text = re.sub(r'\*\*|__|\*|_', '', joke_text).strip()
    joke_text = re.sub(r'\n{3,}', '\n\n', joke_text)

    emoji = TOPIC_EMOJI.get(topic, "😄")
    joke_text = f"{emoji} {joke_text}"
    joke_text += f"\n\n<i>{hashtag}</i>"

    return joke_text

async def publish_joke():
    topic = select_topic()
    thread_id = get_thread_id(topic)
    if thread_id is None:
        logger.error(f"Не найдена тема для '{topic}', пропускаем.")
        return

    holiday_prob = get_holiday_bias()
    if random.random() < holiday_prob:
        holiday = get_current_holiday()
    else:
        holiday = None

    joke_text = await asyncio.to_thread(generate_joke_sync, topic, holiday=holiday)
    if not joke_text:
        return

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=joke_text,
            message_thread_id=thread_id,
            disable_web_page_preview=True,
            disable_notification=True,
            parse_mode='HTML',
        )
        logger.info(f"Опубликовано в теме '{topic}' (thread_id={thread_id})")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в тему {topic}: {e}")

async def scheduler():
    while True:
        try:
            await publish_joke()
        except Exception as e:
            logger.exception("Ошибка в планировщике: %s", e)
        await asyncio.sleep(random.randint(780, 1020))

# ===== РЕФЕРАЛЬНАЯ СИСТЕМА =====
def get_user_data(user_id):
    return data.get(str(user_id), {})

def save_user_data(user_id, user_data):
    data[str(user_id)] = user_data
    save_data(data)

def check_daily_invite_limit(user_id):
    today = date.today().isoformat()
    user_data = get_user_data(user_id)
    if user_data.get("last_invite_date") != today:
        user_data["invites_today"] = 0
        user_data["last_invite_date"] = today
    return user_data.get("invites_today", 0) < MAX_INVITES_PER_DAY

def check_daily_joke_limit(user_id):
    today = date.today().isoformat()
    user_data = get_user_data(user_id)
    if user_data.get("last_joke_date") != today:
        user_data["jokes_today"] = 0
        user_data["last_joke_date"] = today
    return user_data.get("jokes_today", 0) < MAX_PERSONAL_JOKES_PER_DAY

def increment_invite_count(user_id):
    user_data = get_user_data(user_id)
    user_data["invites_today"] = user_data.get("invites_today", 0) + 1
    user_data["total_invites"] = user_data.get("total_invites", 0) + 1
    save_user_data(user_id, user_data)

def increment_joke_count(user_id):
    user_data = get_user_data(user_id)
    user_data["jokes_today"] = user_data.get("jokes_today", 0) + 1
    save_user_data(user_id, user_data)

def get_user_settings(user_id):
    return get_user_data(user_id).get("settings", {})

# ===== ОБРАБОТЧИКИ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if args and args[0] == "personal":
        await personal_joke(update, context)
        return
    if args and args[0].startswith("ref_"):
        ref_user_id = int(args[0].split("_")[1])
        if ref_user_id != user.id:
            invite_data = data.setdefault("invites", {})
            invite_data[str(user.id)] = {"ref_by": ref_user_id, "date": datetime.now().isoformat()}
            save_data(data)
            if check_daily_invite_limit(ref_user_id):
                increment_invite_count(ref_user_id)
                try:
                    await bot.send_message(chat_id=ref_user_id, text="🎉 Вы пригласили нового друга! Получите персональный юмор!")
                except Exception:
                    pass
    if update.effective_chat.type == "private":
        await update.message.reply_text("Привет! Используй команды:\n/referral - получить реферальную ссылку\n/personal - настроить персональный юмор")

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != "private":
        await update.message.reply_text("Эта команда работает только в личных сообщениях.")
        return
    if not check_daily_invite_limit(user.id):
        await update.message.reply_text("Вы сегодня уже пригласили 10 друзей, возвращайтесь завтра!")
        return
    ref_link = f"https://t.me/ai_umor_24?start=ref_{user.id}"
    keyboard = [[InlineKeyboardButton("Пригласить контакт", switch_inline_query="")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Ваша реферальная ссылка:\n{ref_link}\n\nОтправьте её другу или используйте кнопку ниже.",
        reply_markup=reply_markup
    )

async def personal_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != "private":
        await update.message.reply_text("Эта команда работает только в личных сообщениях.")
        return
    keyboard = [
        [InlineKeyboardButton("👤 Имя", callback_data="set_name")],
        [InlineKeyboardButton("📂 Тематика", callback_data="set_topic")],
        [InlineKeyboardButton("🎭 Формат", callback_data="set_format")],
        [InlineKeyboardButton("✅ Получить шутку", callback_data="get_joke")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Настройте параметры персонального юмора:", reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_data = get_user_data(user.id)
    settings = user_data.get("settings", {})

    if query.data == "set_name":
        await query.message.reply_text("Введите ваше имя (или отправьте 'пропустить'):")
        context.user_data["awaiting"] = "name"
    elif query.data == "set_topic":
        topics = list(TOPIC_WEIGHTS.keys())
        buttons = [[InlineKeyboardButton(f"{TOPIC_EMOJI.get(t,'')} {t}", callback_data=f"topic_{t}")] for t in topics]
        buttons.append([InlineKeyboardButton("Пропустить", callback_data="skip_topic")])
        await query.message.reply_text("Выберите тематику:", reply_markup=InlineKeyboardMarkup(buttons))
    elif query.data.startswith("topic_"):
        topic = query.data.split("_", 1)[1]
        settings["topic"] = topic
        user_data["settings"] = settings
        save_user_data(user.id, user_data)
        await query.message.reply_text(f"Тематика: {topic}. Сохранено.")
    elif query.data == "skip_topic":
        await query.message.reply_text("Тематика не выбрана.")
    elif query.data == "set_format":
        buttons = [[InlineKeyboardButton(f, callback_data=f"format_{f}")] for f in FORMATS]
        buttons.append([InlineKeyboardButton("Пропустить", callback_data="skip_format")])
        await query.message.reply_text("Выберите формат:", reply_markup=InlineKeyboardMarkup(buttons))
    elif query.data.startswith("format_"):
        format_type = query.data.split("_", 1)[1]
        settings["format"] = format_type
        user_data["settings"] = settings
        save_user_data(user.id, user_data)
        await query.message.reply_text(f"Формат: {format_type}. Сохранено.")
    elif query.data == "skip_format":
        await query.message.reply_text("Формат не выбран.")
    elif query.data == "get_joke":
        if not check_daily_joke_limit(user.id):
            await query.message.reply_text("Вы сегодня уже получили 10 персональных шуток. Возвращайтесь завтра!")
            return
        topic = settings.get("topic", select_topic())
        format_type = settings.get("format")
        joke = await asyncio.to_thread(generate_joke_sync, topic, format_type=format_type, user_settings=settings)
        if joke:
            increment_joke_count(user.id)
            await query.message.reply_text(joke, parse_mode="HTML")
        else:
            await query.message.reply_text("Не удалось сгенерировать шутку, попробуйте позже.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting") == "name":
        user = update.effective_user
        user_data = get_user_data(user.id)
        text = update.message.text.strip()
        if text.lower() != "пропустить":
            user_data["settings"]["name"] = text
            save_user_data(user.id, user_data)
            await update.message.reply_text(f"Имя сохранено: {text}")
        else:
            await update.message.reply_text("Имя не сохранено.")
        context.user_data.pop("awaiting", None)
        await personal_joke(update, context)

# ===== НОВЫЕ ФУНКЦИИ ДЛЯ ТЕМЫ «ПЕРСОНАЛЬНЫЙ ЮМОР» =====
async def find_personal_topic():
    global PERSONAL_TOPIC_ID
    try:
        topics = await bot.get_forum_topics(chat_id=CHAT_ID)
        for topic in topics.topics:
            if "персональный юмор" in topic.name.lower():
                PERSONAL_TOPIC_ID = topic.message_thread_id
                logger.info(f"Найдена тема 'Персональный юмор': {PERSONAL_TOPIC_ID}")
                return
        logger.warning("Тема 'Персональный юмор' не найдена")
    except Exception as e:
        logger.error(f"Ошибка при поиске темы: {e}")

async def send_personal_topic_description():
    if not PERSONAL_TOPIC_ID:
        return
    if data.get("personal_topic_description_sent"):
        return
    text = ("Здесь вы можете получить персональный юмор!\n\n"
            "Нажмите кнопку ниже, чтобы настроить и получить уникальную шутку.")
    keyboard = [[InlineKeyboardButton("🎁 Получить персональный юмор", url=f"https://t.me/{bot.username}?start=personal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, message_thread_id=PERSONAL_TOPIC_ID, reply_markup=reply_markup)
        data["personal_topic_description_sent"] = True
        save_data(data)
        logger.info("Описание темы 'Персональный юмор' отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки описания: {e}")

# ===== HEALTH CHECK =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    def do_HEAD(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        logger.debug("Health check: %s", format % args)

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health-сервер запущен на порту %d", port)
    server.serve_forever()

# ===== ЗАПУСК =====
async def main():
    try:
        threading.Thread(target=run_health_server, daemon=True).start()
        
        # Находим тему «Персональный юмор» и отправляем описание
        await find_personal_topic()
        await send_personal_topic_description()
        
        # Запускаем планировщик как фоновую задачу
        asyncio.create_task(scheduler())
        
        # Регистрируем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("referral", referral))
        application.add_handler(CommandHandler("personal", personal_joke))
        application.add_handler(CallbackQueryHandler(callback_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        
        # Запускаем бота с polling (это устраняет конфликт)
        await application.run_polling()
    except Exception as e:
        logger.exception("Критическая ошибка в main: %s", e)
        raise

if __name__ == "__main__":
    if not all([GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
        logger.error("Не все переменные окружения заданы.")
        exit(1)
    asyncio.run(main())
