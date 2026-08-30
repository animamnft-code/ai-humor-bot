import os
import random
import time
import logging
import threading
import re
import asyncio
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import groq
from telegram import Bot
from telegram.error import TelegramError

# ===== НАСТРОЙКИ =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TOPIC_IDS = {
    "быт": 2,
    "работа": 5,
    "отношения": 12,
    "деньги": 15,
    "еда": 17,
    "спорт": 19,
    "гаджеты": 21,
    "учёба": 23,
    "транспорт": 25,
    "абсурд": 27,
}

TOPIC_EMOJI = {
    "быт": "🏠",
    "работа": "💼",
    "отношения": "❤️",
    "деньги": "💰",
    "еда": "🍕",
    "спорт": "💪",
    "гаджеты": "📱",
    "учёба": "📚",
    "транспорт": "✈️",
    "абсурд": "🔮",
}

TOPIC_WEIGHTS = {
    "быт": 1.2,
    "работа": 1.1,
    "отношения": 1.3,
    "деньги": 1.0,
    "еда": 1.0,
    "спорт": 0.9,
    "гаджеты": 1.2,
    "учёба": 0.85,
    "транспорт": 0.9,
    "абсурд": 1.4,
}

DEFAULT_TOPIC = "быт"

FORMATS = [
    "анекдот",
    "вопрос-ответ",
    "игра слов",
    "смешное определение",
    "диалог",
]

# ИЗМЕНЁННЫЙ СЛОВАРЬ ХЭШТЕГОВ
FORMAT_HASHTAGS = {
    "анекдот": "#анекдоты",
    "вопрос-ответ": "#вопрос_ответ",
    "игра слов": "#игра_слов",
    "смешное определение": "#смешные_определения",
    "диалог": "#диалоги",
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = groq.Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

def get_current_holiday():
    today = datetime.now()
    for holiday in HOLIDAYS:
        holiday_date = datetime(today.year, holiday["month"], holiday["day"])
        delta = today - holiday_date
        if -7 <= delta.days <= 7:
            return holiday["name"]
    return None

def select_topic():
    topics = list(TOPIC_WEIGHTS.keys())
    weights = [TOPIC_WEIGHTS[t] for t in topics]
    return random.choices(topics, weights=weights, k=1)[0]

def generate_joke_sync(topic):
    format_type = random.choice(FORMATS)
    hashtag = FORMAT_HASHTAGS[format_type]

    holiday = get_current_holiday()

    prompt = f"Сгенерируй {format_type} на тему «{topic}»."
    if holiday:
        prompt += f" Приурочь его к празднику: {holiday}."
    prompt += " В конце добавь тег [ТЕМА: {topic}] и хэштег " + hashtag + "."

    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": "Ты - генератор юмора."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=200,
        )
        raw_text = response.choices[0].message.content.strip()
        logger.info(f"Groq ответ: {raw_text}")
    except Exception as e:
        logger.error(f"Ошибка вызова Groq: {e}")
        return None, None

    joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
    emoji = TOPIC_EMOJI.get(topic, "😄")
    joke_text = f"{emoji} {joke_text}"

    joke_text += f"\n\n<i>{hashtag}</i>"

    return joke_text, topic

async def publish_joke():
    topic = select_topic()
    thread_id = TOPIC_IDS.get(topic, TOPIC_IDS[DEFAULT_TOPIC])

    joke_text, topic_actual = await asyncio.to_thread(generate_joke_sync, topic)
    if not joke_text:
        logger.error("Не удалось получить шутку, пропускаем публикацию.")
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
        logger.info(f"Опубликовано в теме '{topic_actual}' (thread_id={thread_id}) без уведомлений")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в тему {topic_actual}: {e}")

async def run_scheduler():
    logger.info("Планировщик запущен. Интервал будет случайным (13–17 минут).")
    while True:
        try:
            await publish_joke()
        except Exception as e:
            logger.exception("Непредвиденная ошибка в цикле публикаций: %s", e)
        await asyncio.sleep(random.randint(780, 1020))

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

if __name__ == "__main__":
    if not all([GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
        logger.error("Не все переменные окружения заданы (GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID).")
        exit(1)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен вручную")
