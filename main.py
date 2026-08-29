import os
import random
import time
import logging
import threading
import re
import asyncio
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
DEFAULT_TOPIC = "быт"

# Список тем для равномерного распределения (в порядке обхода)
TOPIC_ORDER = ["быт", "работа", "отношения", "деньги", "еда", "спорт", "гаджеты", "учёба", "транспорт", "абсурд"]
current_topic_index = 0  # счётчик для циклического выбора

# Интервал публикации (сек)
PUBLISH_INTERVAL = 900  # 15 минут

# Промпты для генерации (случайный выбор для разнообразия)
PROMPTS = [
    "Сгенерируй короткую смешную шутку (1-2 предложения) на тему «{topic}». В конце добавь тег [ТЕМА: {topic}].",
    "Придумай остроумную шутку или анекдот на тему «{topic}». Закончи тегом [ТЕМА: {topic}].",
    "Расскажи смешную фразу или игру слов на тему «{topic}». В конце напиши [ТЕМА: {topic}].",
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

# ===== ФУНКЦИЯ ВЫБОРА ТЕМЫ (циклически) =====
def get_next_topic():
    """Возвращает следующую тему по кругу."""
    global current_topic_index
    topic = TOPIC_ORDER[current_topic_index]
    current_topic_index = (current_topic_index + 1) % len(TOPIC_ORDER)
    return topic

# ===== ФУНКЦИЯ ГЕНЕРАЦИИ ШУТКИ (синхронная, вызывается через asyncio.to_thread) =====
def generate_joke_sync(topic):
    """Запрашивает шутку у Groq для указанной темы, возвращает текст шутки."""
    prompt_template = random.choice(PROMPTS)
    prompt = prompt_template.format(topic=topic)
    try:
        response = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",  # МОДЕЛЬ ИЗ AI-PULSE (проверено рабочая)
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
        return None

    # Удаляем возможный тег из текста, если он остался
    # (мы уже знаем тему, поэтому тег не нужен в публикации)
    joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
    return joke_text

# ===== АСИНХРОННАЯ ПУБЛИКАЦИЯ =====
async def publish_joke():
    """Генерирует шутку и публикует её в выбранную тему."""
    # Выбираем следующую тему по кругу
    topic = get_next_topic()
    thread_id = TOPIC_IDS.get(topic, TOPIC_IDS[DEFAULT_TOPIC])

    # Генерируем шутку для выбранной темы
    joke_text = await asyncio.to_thread(generate_joke_sync, topic)
    if not joke_text:
        logger.error("Не удалось получить шутку, пропускаем публикацию.")
        return

    # Публикация только в тематическую ветку
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=joke_text,
            message_thread_id=thread_id,
            disable_web_page_preview=True,
        )
        logger.info(f"Опубликовано в теме '{topic}' (thread_id={thread_id})")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в тему {topic}: {e}")

# ===== АСИНХРОННЫЙ ПЛАНИРОВЩИК =====
async def run_scheduler():
    """Бесконечный цикл публикаций."""
    logger.info("Планировщик запущен. Интервал: %d сек.", PUBLISH_INTERVAL)
    while True:
        try:
            await publish_joke()
        except Exception as e:
            logger.exception("Непредвиденная ошибка в цикле публикаций: %s", e)
        await asyncio.sleep(PUBLISH_INTERVAL)

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
