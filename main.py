import os
import logging
import json
import asyncio
import random
import re
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import groq
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler
from telegram.error import TelegramError

# ===== НАСТРОЙКИ =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # ID супергруппы
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

TOPIC_IDS = {
    "быт": 2, "работа": 5, "отношения": 12, "деньги": 15, "еда": 17,
    "спорт": 19, "гаджеты": 21, "учёба": 23, "транспорт": 25, "абсурд": 27,
}
TOPIC_EMOJI = {
    "быт": "🏠", "работа": "💼", "отношения": "❤️", "деньги": "💰", "еда": "🍔",
    "спорт": "🏁", "гаджеты": "💻", "учёба": "📚", "транспорт": "✈️", "абсурд": "🎭",
}
FORMATS = ["анекдот", "вопрос-ответ", "игра слов", "смешное определение", "диалог"]

DATA_FILE = "data.json"
CONFIG_FILE = "config.json"

def load_config():
    global FORMATS, TOPIC_IDS, TOPIC_EMOJI
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            FORMATS = config.get("formats", FORMATS)
            TOPIC_IDS = config.get("topic_ids", TOPIC_IDS)
            TOPIC_EMOJI = config.get("topic_emoji", TOPIC_EMOJI)
    else:
        pass

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"invites": {}, "user_settings": {}, "has_invited": {}}

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

def check_is_member(user_id):
    """Проверяет, является ли пользователь участником группы."""
    try:
        member = bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Ошибка проверки членства: {e}")
        return False

def generate_joke_sync(topic, format_type=None, user_settings=None):
    if format_type is None:
        format_type = random.choice(FORMATS)
    prompt = f"Сгенерируй {format_type} на тему «{topic}»."
    if user_settings:
        if user_settings.get("name"):
            prompt += f" Используй имя {user_settings['name']}."
    prompt += " Пиши кратко и смешно, законченный текст. Без Markdown и HTML. В конце добавь тег [ТЕМА: " + topic + "]."
    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": "Ты - генератор юмора. Пиши смешно и законченно."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=200,
        )
        raw = response.choices[0].message.content.strip()
        joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw, flags=re.IGNORECASE).strip()
        joke_text = re.sub(r'#\S+', '', joke_text).strip()
        joke_text = re.sub(r'\*\*|__|\*|_', '', joke_text).strip()
        joke_text = joke_text.strip()
        emoji = TOPIC_EMOJI.get(topic, "😄")
        return f"{emoji} {joke_text}"
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return None

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
                "и пригласить в эту группу нового пользователя.")
        keyboard = [[InlineKeyboardButton("Пригласить контакт", callback_data="invite_contact")]]
        # Если у пользователя уже есть право (has_invited), добавим кнопку получения юмора
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
        if not check_is_member(user.id):
            await query.edit_message_text("Для начала нужно быть участником этой группы. Вступите в группу, а затем повторите попытку.")
            return
        ref_link = f"https://t.me/{bot.username}?start=ref_{user.id}"
        keyboard = [[InlineKeyboardButton("📤 Отправить другу", url=f"https://t.me/share/url?url={ref_link}&text=Присоединяйся к группе юмора!")]]
        await query.edit_message_text(
            "Отправьте приглашение другу. После того как он вступит в группу, вы сможете получить персональный юмор.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
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
        if not user_data.get("has_invited"):
            await query.edit_message_text("Вы ещё не пригласили друга. Сначала пригласите и дождитесь вступления, чтобы получить персональный юмор.")
            return
        topic = settings.get("topic", random.choice(list(TOPIC_IDS.keys())))
        format_type = settings.get("format")
        joke = await asyncio.to_thread(generate_joke_sync, topic, format_type, settings)
        if joke:
            await query.message.reply_text(joke)
            await personal_joke_from_callback(update, context)
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
    await query.edit_message_text(
        f"Настройте параметры персонального юмора:\n\n{current}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def personal_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Команда /personal
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
        if text.lower() != "пропустить":
            user_data = get_user_data(user.id)
            user_data["settings"]["name"] = text
            save_user_data(user.id, user_data)
            await update.message.reply_text(f"Имя сохранено: {text}")
        else:
            await update.message.reply_text("Имя не сохранено.")
        context.user_data.pop("awaiting", None)
        original_message_id = context.user_data.pop("original_message_id", None)
        if original_message_id:
            settings = get_user_data(user.id).get("settings", {})
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
            await bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=original_message_id,
                text=f"Настройте параметры персонального юмора:\n\n{current}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await personal_joke(update, context)

async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_member = update.chat_member.new_chat_member
    old_member = update.chat_member.old_chat_member
    user = new_member.user
    if new_member.status in ("member", "administrator", "creator") and old_member.status not in ("member", "administrator", "creator"):
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
                except Exception as e:
                    logger.error(f"Ошибка уведомления пригласившего: {e}")

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
    threading.Thread(target=run_health_server, daemon=True).start()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("personal", personal_joke))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))
    await application.initialize()
    await application.start()
    # Ждём 5 секунд, чтобы старый процесс завершился
    await asyncio.sleep(5)
    await application.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member"])
    await asyncio.Event().wait()

if __name__ == "__main__":
    if not all([GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
        logger.error("Не все переменные окружения заданы.")
        exit(1)
    asyncio.run(main())
