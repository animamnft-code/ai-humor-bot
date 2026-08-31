import os
import logging
import json
import asyncio
import random
import re
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import groq
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler
from telegram.error import TelegramError, BadRequest

# ===== НАСТРОЙКИ =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PERSONAL_TOPIC_ID = os.getenv("PERSONAL_TOPIC_ID")
if PERSONAL_TOPIC_ID:
    PERSONAL_TOPIC_ID = int(PERSONAL_TOPIC_ID)
else:
    PERSONAL_TOPIC_ID = None

# Лимиты
MAX_INVITES_PER_DAY = 10
MAX_PERSONAL_JOKES_PER_DAY = 10

# Дефолтные значения (будут перезаписаны из config.json)
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
TOPIC_IDS = {
    "быт": 2, "работа": 5, "отношения": 12, "деньги": 15, "еда": 17,
    "спорт": 19, "гаджеты": 21, "учёба": 23, "транспорт": 25, "абсурд": 27,
}
TOPIC_EMOJI = {
    "быт": "🏠", "работа": "💼", "отношения": "❤️", "деньги": "💰", "еда": "🍔",
    "спорт": "🏁", "гаджеты": "💻", "учёба": "📚", "транспорт": "✈️", "абсурд": "🎭",
}
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
CURRENT_EVENTS = []
EVENT_PROBABILITY = 0.15

DATA_FILE = "data.json"
CONFIG_FILE = "config.json"
BOT_USERNAME = None

def load_config():
    global FORMATS, FORMAT_HASHTAGS, FORMAT_MAX_TOKENS, TOPIC_IDS, TOPIC_EMOJI
    global HOLIDAYS, CURRENT_EVENTS, EVENT_PROBABILITY
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            FORMATS = config.get("formats", FORMATS)
            FORMAT_HASHTAGS = config.get("hashtags", FORMAT_HASHTAGS)
            FORMAT_MAX_TOKENS = config.get("max_tokens", FORMAT_MAX_TOKENS)
            TOPIC_IDS = config.get("topic_ids", TOPIC_IDS)
            TOPIC_EMOJI = config.get("topic_emoji", TOPIC_EMOJI)
            HOLIDAYS = config.get("holidays", HOLIDAYS)
            CURRENT_EVENTS = config.get("current_events", CURRENT_EVENTS)
            EVENT_PROBABILITY = config.get("event_probability", EVENT_PROBABILITY)
    else:
        pass

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"invites": {}, "user_settings": {}, "has_invited": {}, "personal_topic_description_sent": False}

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

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def get_user_data(user_id):
    return data.get(str(user_id), {})

def save_user_data(user_id, user_data):
    data[str(user_id)] = user_data
    save_data(data)

async def check_is_member(user_id):
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Ошибка проверки членства: {e}")
        return False

def check_daily_joke_limit(user_id):
    today = date.today().isoformat()
    user_data = get_user_data(user_id)
    if user_data.get("last_joke_date") != today:
        user_data["jokes_today"] = 0
        user_data["last_joke_date"] = today
        save_user_data(user_id, user_data)
    return user_data.get("jokes_today", 0) < MAX_PERSONAL_JOKES_PER_DAY

def increment_joke_count(user_id):
    today = date.today().isoformat()
    user_data = get_user_data(user_id)
    if user_data.get("last_joke_date") != today:
        user_data["jokes_today"] = 0
        user_data["last_joke_date"] = today
    user_data["jokes_today"] = user_data.get("jokes_today", 0) + 1
    save_user_data(user_id, user_data)

# ===== ФУНКЦИИ ПРАЗДНИКОВ И СОБЫТИЙ =====
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
    prob = 0.65 - (abs(delta) / 7) * 0.55
    return max(0.05, prob)

def get_random_event():
    if CURRENT_EVENTS:
        return random.choice(CURRENT_EVENTS)
    return None

# ===== ГЕНЕРАЦИЯ ШУТКИ =====
def generate_joke_sync(topic, format_type=None, user_settings=None, holiday=None, event=None):
    if format_type is None:
        format_type = random.choice(FORMATS)
    hashtag = FORMAT_HASHTAGS.get(format_type, "")
    max_tokens = FORMAT_MAX_TOKENS.get(format_type, 250)

    prompt = f"Сгенерируй {format_type} на тему «{topic}»."
    if holiday:
        prompt += f" Приурочь шутку к празднику: {holiday}."
    if event:
        prompt += f" Упомяни событие: {event}."
    if user_settings:
        if user_settings.get("name"):
            prompt += f" Используй имя {user_settings['name']}."
    prompt += " Пиши кратко и смешно, законченный текст. Без Markdown и HTML. В конце добавь тег [ТЕМА: " + topic + "] и хэштег " + hashtag + "."

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
        raw = response.choices[0].message.content.strip()
        joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw, flags=re.IGNORECASE).strip()
        joke_text = re.sub(r'#\S+', '', joke_text).strip()
        joke_text = re.sub(r'\*\*|__|\*|_', '', joke_text).strip()
        joke_text = joke_text.strip()
        emoji = TOPIC_EMOJI.get(topic, "😄")
        if hashtag and hashtag not in joke_text:
            joke_text += f"\n\n<i>{hashtag}</i>"
        return f"{emoji} {joke_text}"
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return None

# ===== ОТПРАВКА ОПИСАНИЯ В ТЕМУ «ПЕРСОНАЛЬНЫЙ ЮМОР» =====
async def send_personal_topic_description():
    if not PERSONAL_TOPIC_ID:
        logger.warning("PERSONAL_TOPIC_ID не задан, пропускаем отправку описания.")
        return
    if data.get("personal_topic_description_sent"):
        return

    text = (
        "🎭 Добро пожаловать в тему «Персональный юмор»!\n\n"
        "Здесь вы можете получить уникальную шутку, созданную специально для вас.\n\n"
        "🎁 Как получить персональный юмор?\n"
        "1. Вступите в нашу группу: https://t.me/ai_umor_24\n"
        "2. Пригласите в неё одного друга.\n"
        "3. Нажмите кнопку ниже, чтобы настроить параметры и получить шутку.\n\n"
        "⚠️ Без участия в группе и приглашения друга получить персональный юмор невозможно.\n\n"
        "Присоединяйтесь и смейтесь вместе с нами! 😄"
    )

    if not BOT_USERNAME:
        logger.error("BOT_USERNAME не установлен!")
        return
    keyboard = [[InlineKeyboardButton("🎁 Получить персональный юмор", url=f"https://t.me/{BOT_USERNAME}?start=personal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            message_thread_id=PERSONAL_TOPIC_ID,
            reply_markup=reply_markup
        )
        data["personal_topic_description_sent"] = True
        save_data(data)
        logger.info("Описание темы «Персональный юмор» отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки описания: {e}")

# ===== ОБРАБОТЧИКИ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    logger.info(f"Start from {user.id}, args={args}")
    
    if args and args[0] == "personal":
        await personal_joke(update, context)
        return
    if args and args[0].startswith("ref_"):
        ref_user_id = int(args[0].split("_")[1])
        if ref_user_id != user.id:
            invite_data = data.setdefault("invites", {})
            invite_data[str(user.id)] = {"ref_by": ref_user_id, "date": datetime.now().isoformat()}
            save_data(data)
    
    if update.effective_chat.type == "private":
        text = ("Привет! Чтобы получить 'персональный юмор' нужно быть участником этой группы "
                "https://t.me/ai_umor_24 и пригласить в эту группу нового пользователя.")
        keyboard = [[InlineKeyboardButton("Пригласить контакт", callback_data="invite_contact")]]
        user_data = get_user_data(user.id)
        if user_data.get("has_invited"):
            keyboard.append([InlineKeyboardButton("🎁 Получить персональный юмор", callback_data="get_joke")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_data = get_user_data(user.id)
    settings = user_data.get("settings", {})
    logger.info(f"Callback {query.data} from {user.id}")

    if query.data == "invite_contact":
        logger.info("Обработка invite_contact")
        if not await check_is_member(user.id):
            await query.edit_message_text(
                "Для начала нужно быть участником этой группы: https://t.me/ai_umor_24\n"
                "Вступите в группу, а затем повторите попытку."
            )
            return
        if not BOT_USERNAME:
            logger.error("BOT_USERNAME не установлен!")
            BOT_USERNAME_FALLBACK = os.getenv("BOT_USERNAME", "ai_umor_24")
        else:
            BOT_USERNAME_FALLBACK = BOT_USERNAME
        ref_link = f"https://t.me/{BOT_USERNAME_FALLBACK}?start=ref_{user.id}"
        keyboard = [[InlineKeyboardButton("📤 Отправить другу", url=f"https://t.me/share/url?url={ref_link}&text=Присоединяйся к группе юмора!")]]
        try:
            await query.edit_message_text(
                "Отправьте приглашение другу. После того как он вступит в группу, вы сможете получить персональный юмор.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            logger.info("Кнопка отправки другу показана")
        except Exception as e:
            logger.error(f"Ошибка при показе кнопки: {e}", exc_info=True)
    elif query.data == "get_joke":
        await personal_joke_from_callback(update, context)
    elif query.data == "set_name":
        await query.edit_message_text("Введите ваше имя (или 'пропустить'):")
        context.user_data["awaiting"] = "name"
        context.user_data["original_message_id"] = query.message.message_id
    elif query.data == "set_topic":
        topics = list(TOPIC_IDS.keys())
        buttons = [[InlineKeyboardButton(f"{TOPIC_EMOJI.get(t,'')} {t}", callback_data=f"topic_{t}")] for t in topics]
        buttons.append([InlineKeyboardButton("Пропустить", callback_data="skip_topic")])
        await query.edit_message_text("Выберите тематику:", reply_markup=InlineKeyboardMarkup(buttons))
    elif query.data.startswith("topic_"):
        topic = query.data.split("_", 1)[1]
        settings["topic"] = topic
        user_data["settings"] = settings
        save_user_data(user.id, user_data)
        await personal_joke_from_callback(update, context)
    elif query.data == "skip_topic":
        await query.edit_message_text("Тематика не выбрана.")
        await personal_joke_from_callback(update, context)
    elif query.data == "set_format":
        buttons = [[InlineKeyboardButton(f, callback_data=f"format_{f}")] for f in FORMATS]
        buttons.append([InlineKeyboardButton("Пропустить", callback_data="skip_format")])
        await query.edit_message_text("Выберите формат:", reply_markup=InlineKeyboardMarkup(buttons))
    elif query.data.startswith("format_"):
        format_type = query.data.split("_", 1)[1]
        settings["format"] = format_type
        user_data["settings"] = settings
        save_user_data(user.id, user_data)
        await personal_joke_from_callback(update, context)
    elif query.data == "skip_format":
        await query.edit_message_text("Формат не выбран.")
        await personal_joke_from_callback(update, context)
    elif query.data == "reset_settings":
        user_data["settings"] = {}
        save_user_data(user.id, user_data)
        await personal_joke_from_callback(update, context)
    elif query.data == "confirm_get_joke":
        if not check_daily_joke_limit(user.id):
            await query.edit_message_text("Вы сегодня уже получили 10 персональных шуток. Лимит исчерпан. Возвращайтесь завтра!")
            return
        if not user_data.get("has_invited"):
            await query.edit_message_text("Вы ещё не пригласили друга. Сначала пригласите и дождитесь вступления, чтобы получить персональный юмор.")
            return
        topic = settings.get("topic", random.choice(list(TOPIC_IDS.keys())))
        format_type = settings.get("format")
        joke = await asyncio.to_thread(generate_joke_sync, topic, format_type, settings)
        if joke:
            increment_joke_count(user.id)
            await query.message.reply_text(joke, parse_mode='HTML')
            try:
                await personal_joke_from_callback(update, context)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"Ошибка при возврате меню: {e}")
        else:
            await query.edit_message_text("Не удалось сгенерировать шутку, попробуйте позже.")

async def personal_joke_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_data = get_user_data(user.id)
    settings = user_data.get("settings", {})
    current = (
        f"👤 Имя: {settings.get('name', 'не указано')}\n"
        f"📂 Тематика: {settings.get('topic', 'не указана')}\n"
        f"🎭 Формат: {settings.get('format', 'не указан')}"
    )
    keyboard = [
        [InlineKeyboardButton("👤 Имя", callback_data="set_name")],
        [InlineKeyboardButton("📂 Тематика", callback_data="set_topic")],
        [InlineKeyboardButton("🎭 Формат", callback_data="set_format")],
        [InlineKeyboardButton("✅ Получить юмор", callback_data="confirm_get_joke")],
        [InlineKeyboardButton("🔄 Сбросить настройки", callback_data="reset_settings")],
    ]
    try:
        await query.edit_message_text(
            f"Настройте параметры персонального юмора:\n\n{current}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Ошибка при редактировании меню: {e}")

async def personal_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != "private":
        await update.message.reply_text("Эта команда работает только в личных сообщениях.")
        return
    user_data = get_user_data(user.id)
    if not user_data.get("has_invited"):
        await update.message.reply_text("Вы ещё не пригласили друга. Сначала пригласите и дождитесь вступления, чтобы получить персональный юмор.")
        return
    settings = user_data.get("settings", {})
    current = (
        f"👤 Имя: {settings.get('name', 'не указано')}\n"
        f"📂 Тематика: {settings.get('topic', 'не указана')}\n"
        f"🎭 Формат: {settings.get('format', 'не указан')}"
    )
    keyboard = [
        [InlineKeyboardButton("👤 Имя", callback_data="set_name")],
        [InlineKeyboardButton("📂 Тематика", callback_data="set_topic")],
        [InlineKeyboardButton("🎭 Формат", callback_data="set_format")],
        [InlineKeyboardButton("✅ Получить юмор", callback_data="confirm_get_joke")],
        [InlineKeyboardButton("🔄 Сбросить настройки", callback_data="reset_settings")],
    ]
    await update.message.reply_text(
        f"Настройте параметры персонального юмора:\n\n{current}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting") == "name":
        user = update.effective_user
        text = update.message.text.strip()
        user_data = get_user_data(user.id)
        user_data.setdefault("settings", {})["name"] = text if text.lower() != "пропустить" else ""
        save_user_data(user.id, user_data)
        if text.lower() != "пропустить":
            await update.message.reply_text(f"Имя сохранено: {text}")
        else:
            await update.message.reply_text("Имя не сохранено.")
        context.user_data.pop("awaiting", None)
        original_message_id = context.user_data.pop("original_message_id", None)
        if original_message_id:
            settings = user_data.get("settings", {})
            current = (
                f"👤 Имя: {settings.get('name', 'не указано')}\n"
                f"📂 Тематика: {settings.get('topic', 'не указана')}\n"
                f"🎭 Формат: {settings.get('format', 'не указан')}"
            )
            keyboard = [
                [InlineKeyboardButton("👤 Имя", callback_data="set_name")],
                [InlineKeyboardButton("📂 Тематика", callback_data="set_topic")],
                [InlineKeyboardButton("🎭 Формат", callback_data="set_format")],
                [InlineKeyboardButton("✅ Получить юмор", callback_data="confirm_get_joke")],
                [InlineKeyboardButton("🔄 Сбросить настройки", callback_data="reset_settings")],
            ]
            try:
                await bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=original_message_id,
                    text=f"Настройте параметры персонального юмора:\n\n{current}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"Ошибка при редактировании меню: {e}")
        else:
            await personal_joke(update, context)

async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_chat_member = update.chat_member.new_chat_member
    old_chat_member = update.chat_member.old_chat_member
    user = new_chat_member.user
    if new_chat_member.status in ("member", "administrator", "creator") and old_chat_member.status not in ("member", "administrator", "creator"):
        invite_data = data.get("invites", {}).get(str(user.id))
        if invite_data:
            inviter_id = invite_data.get("ref_by")
            if inviter_id:
                inviter_data = get_user_data(inviter_id)
                inviter_data["has_invited"] = True
                save_user_data(inviter_id, inviter_data)
                try:
                    await bot.send_message(
                        chat_id=inviter_id,
                        text="🎉 Ваш друг вступил в группу! Теперь вы можете получить персональный юмор.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎁 Получить персональный юмор", callback_data="get_joke")]])
                    )
                    logger.info(f"Уведомление отправлено пригласившему {inviter_id}")
                except Exception as e:
                    logger.error(f"Ошибка уведомления пригласившего: {e}")
        try:
            text = "Добро пожаловать в группу! Теперь вы можете приглашать друзей и получать персональный юмор."
            keyboard = [[InlineKeyboardButton("Пригласить контакт", callback_data="invite_contact")]]
            await bot.send_message(chat_id=user.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            logger.info(f"Отправлено приветствие новому участнику {user.id}")
        except Exception as e:
            logger.error(f"Ошибка отправки приветствия: {e}")

# ===== ПЛАНИРОВЩИК ПУБЛИКАЦИЙ =====
async def publish_joke():
    """Генерирует и публикует шутку в группу, учитывая праздники и события."""
    topic = random.choice(list(TOPIC_IDS.keys()))
    thread_id = TOPIC_IDS[topic]
    
    holiday = None
    if random.random() < get_holiday_bias():
        holiday = get_current_holiday()
    
    event = None
    if random.random() < EVENT_PROBABILITY:
        event = get_random_event()
    
    joke_text = await asyncio.to_thread(generate_joke_sync, topic, holiday=holiday, event=event)
    if joke_text:
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=joke_text,
                message_thread_id=thread_id,
                disable_web_page_preview=True,
                disable_notification=True,
                parse_mode='HTML'
            )
            logger.info(f"Опубликовано в теме '{topic}' (thread_id={thread_id})")
        except TelegramError as e:
            logger.error(f"Ошибка публикации: {e}")

async def scheduler():
    logger.info("Планировщик запущен.")
    while True:
        try:
            await publish_joke()
        except Exception as e:
            logger.exception("Ошибка в планировщике: %s", e)
        await asyncio.sleep(random.randint(780, 1020))

# ===== HEALTH CHECK =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
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
    global BOT_USERNAME
    threading.Thread(target=run_health_server, daemon=True).start()
    
    await bot.initialize()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Bot username: {BOT_USERNAME}")
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("personal", personal_joke))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))
    
    await application.initialize()
    await application.start()
    
    await send_personal_topic_description()
    
    await asyncio.sleep(10)
    asyncio.create_task(scheduler())
    await application.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member"])
    await asyncio.Event().wait()

if __name__ == "__main__":
    if not all([GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
        logger.error("Не все переменные окружения заданы.")
        exit(1)
    asyncio.run(main())
