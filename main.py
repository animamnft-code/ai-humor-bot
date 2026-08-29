import os
import random
import time
import logging
import threading
import re
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

# Интервал публикации (сек)
PUBLISH_INTERVAL = 900  # 15 минут

# Промпты для генерации (случайный выбор для разнообразия)
PROMPTS = [
    "Сгенерируй короткую смешную шутку (1-2 предложения). В конце добавь тег [ТЕМА: ...], где ... - одна из категорий: быт, работа, отношения, деньги, еда, спорт, гаджеты, учёба, транспорт, абсурд.",
    "Придумай остроумную шутку на злобу дня. Обязательно закончи тегом [ТЕМА: ...] с указанием категории из списка: быт, работа, отношения, деньги, еда, спорт, гаджеты, учёба, транспорт, абсурд.",
    "Расскажи анекдот в одно предложение. В конце напиши [ТЕМА: ...], выбрав одну из категорий: быт, работа, отношения, деньги, еда, спорт, гаджеты, учёба, транспорт, абсурд.",
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

# ===== ФУНКЦИЯ ГЕНЕРАЦИИ ШУТКИ =====
def generate_joke():
    """Запрашивает шутку у Groq, возвращает (текст_шутки, тема)."""
    prompt = random.choice(PROMPTS)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",  # ЗАМЕНЁННАЯ МОДЕЛЬ
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

    # Парсим тег [ТЕМА: ...]
    match = re.search(r"\[ТЕМА:\s*([^\]]+)\]", raw_text, re.IGNORECASE)
    if match:
        topic_raw = match.group(1).strip().lower()
        # Сопоставляем с ключами словаря
        for key in TOPIC_IDS.keys():
            if topic_raw.startswith(key):
                topic = key
                break
        else:
            topic = DEFAULT_TOPIC
        # Удаляем тег из текста
        joke_text = re.sub(r"\[ТЕМА:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
    else:
        topic = DEFAULT_TOPIC
        joke_text = raw_text

    return joke_text, topic

# ===== ФУНКЦИЯ ПУБЛИКАЦИИ =====
def publish_joke():
    """Генерирует шутку и публикует её в соответствующую тему и в главную."""
    joke_text, topic = generate_joke()
    if not joke_text:
        logger.error("Не удалось получить шутку, пропускаем публикацию.")
        return

    # Определяем ID темы
    thread_id = TOPIC_IDS.get(topic, TOPIC_IDS[DEFAULT_TOPIC])

    # Публикация в тематическую ветку
    try:
        bot.send_message(
            chat_id=CHAT_ID,
            text=joke_text,
            message_thread_id=thread_id,
            disable_web_page_preview=True,
        )
        logger.info(f"Опубликовано в теме '{topic}' (thread_id={thread_id})")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в тему {topic}: {e}")
        return

    # Небольшая пауза, чтобы избежать троттлинга
    time.sleep(2)

    # Публикация в главную тему (message_thread_id=None)
    try:
        bot.send_message(
            chat_id=CHAT_ID,
            text=joke_text,
            message_thread_id=None,  # главная тема
            disable_web_page_preview=True,
        )
        logger.info("Опубликовано в главную тему 'Все здесь'")
    except TelegramError as e:
        logger.error(f"Ошибка публикации в главную тему: {e}")

# ===== ЗАПУСК ЦИКЛА =====
def run_scheduler():
    """Бесконечный цикл публикаций."""
    logger.info("Планировщик запущен. Интервал: %d сек.", PUBLISH_INTERVAL)
    while True:
        try:
            publish_joke()
        except Exception as e:
            logger.exception("Непредвиденная ошибка в цикле публикаций: %s", e)
        time.sleep(PUBLISH_INTERVAL)

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

    # Основной планировщик
    run_scheduler()
