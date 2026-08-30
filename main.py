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
CHAT_ID = os.getenv("CHAT_ID")  # например, -1003973808650

# ID тем (message_thread_id) - замените, если отличаются
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

# Эмодзи для каждой темы (временные, можете заменить на реальные из группы)
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

# Веса тем (больше вес – чаще публикуется). Разница max/min не более 30%
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

# Форматы юмора
FORMATS = [
    "анекдот",
    "вопрос-ответ",
    "игра слов",
    "смешное определение",
    "диалог",
]
FORMAT_HASHTAGS = {
    "анекдот": "#анекдоты",
    "вопрос-ответ": "#вопрос-ответ",
    "игра слов": "#играслов",
    "смешное определение": "#смешныеопределения",
    "диалог": "#диалоги",
}

# Российские праздники (без религиозных) - даты в формате (месяц, день)
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

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===== ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ =====
groq_client = groq.Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ===== ФУНКЦИЯ ПРОВЕРКИ ПРАЗДНИКА =====
def get_current_holiday():
    """Возвращает название праздника, если сегодня в диапазоне ±7 дней, иначе None."""
    today = datetime.now()
    for holiday in HOLIDAYS:
        # Дата праздника в текущем году
        holiday_date = datetime(today.year, holiday["month"], holiday["day"])
        delta = today - holiday_date
        # Проверяем диапазон от -7 до +7 дней
        if -7 <= delta.days <= 7:
            return holiday["name"]
    return None

# ===== ФУНКЦИЯ ВЫБОРА ТЕМЫ (взвешенный случайный выбор) =====
def select_topic():
    """Выбирает тему с учётом весов."""
    topics = list(TOPIC_WEIGHTS.keys())
    weights = [TOPIC_WEIGHTS[t] for t in topics]
    return random.choices(topics, weights=weights, k=1)[0]

# ===== ФУНКЦИЯ ГЕНЕРАЦИИ ШУТКИ =====
def generate_joke_sync(topic):
    """Запрашивает шутку у Groq с учётом формата и праздника."""
    # Случайный формат
    format_type = random.choice(FORMATS)
    hashtag = FORMAT_HASHTAGS[format_type]

    # Проверяем праздник
    holiday = get_current_holiday()

    # Собираем промпт
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

    # Удаляем тег [ТЕМА: ...] (он нам уже известен)
    joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
    # Добавляем тематический эмодзи в начало
    emoji = TOPIC_EMOJI.get(topic, "😄")
    joke_text = f"{emoji} {joke_text}"

    # Добавляем хэштег формата в конец (курсивом, если возможно)
    # В Telegram размер шрифта нельзя уменьшить, но можно использовать курсив
    joke_text += f"\n\n<i>{hashtag}</i>"

    return joke_text, topic

# ===== АСИНХРОННАЯ ПУБЛИКАЦИЯ =====
async def publish_joke():
    """Генерирует шутку и публикует её в выбранную тему с отключенными уведомлениями."""
    # Выбираем тему
    topic = select_topic()
    thread_id = TOPIC_IDS.get(topic, TOPIC_IDS[DEFAULT_TOPIC])

    # Генерируем шутку
    joke_text, topic_actual = await asyncio.to_thread(generate_joke_sync, topic)
    if not joke_text:
        logger.error("Не удалось получить шутку, пропускаем публикацию.")
        return

    # Публикация в тему
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=joke_text,
            message_thread_id=thread_id,
            disable_web_page_preview=True,
            disable_notification=True,
        )
        logger.info(f"Опубликовано в теме '{topic_actual}' (thread_id={thread_id}) без уведомлений")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в тему {topic_actual}: {e}")

# ===== АСИНХРОННЫЙ ПЛАНИРОВЩИК =====
async def run_scheduler():
    """Бесконечный цикл публикаций со случайным интервалом."""
    logger.info("Планировщик запущен. Интервал будет случайным (13–17 минут).")
    while True:
        try:
            await publish_joke()
        except Exception as e:
            logger.exception("Непредвиденная ошибка в цикле публикаций: %s", e)
        # Случайный интервал от 780 до 1020 секунд (13–17 минут)
        await asyncio.sleep(random.randint(780, 1020))

# ===== HEALTH CHECK (HTTP-сервер) =====
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
    """Запускает HTTP-сервер для проверки живости (Render)."""
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health-сервер запущен на порту %d", port)
    server.serve_forever()

# ===== ЗАПУСК =====
if __name__ == "__main__":
    # Проверяем обязательные переменные
    if not all([GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
        logger.error("Не все переменные окружения заданы (GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID).")
        exit(1)

    # Запускаем health-сервер в отдельном потоке
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    # Запускаем основной планировщик через asyncio
    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен вручную")
