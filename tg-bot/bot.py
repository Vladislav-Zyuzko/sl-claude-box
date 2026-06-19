"""
SLNexus Bot — Telegram бот для управления Claude Code через SSH
Автор: (Sweet Limit)
"""

import os
import asyncio
import logging
from typing import List

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Новая библиотека для SSH
import asyncssh

# ===== НАСТРОЙКИ =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в окружении")

ALLOWED_IDS_RAW = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: List[int] = []
for x in ALLOWED_IDS_RAW.split(","):
    x = x.strip()
    if x:
        ALLOWED_USER_IDS.append(int(x))

if not ALLOWED_USER_IDS:
    raise ValueError("ALLOWED_USER_IDS не задан или пуст")

# SSH настройки
SSH_HOST = os.environ.get("SSH_HOST", "claude-worker")  # имя сервиса в docker-compose
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "claude-ssh")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/ssh-key")  # путь к приватному ключу внутри контейнера

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА =====
def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

# ===== КОМАНДА /START =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещен. Обратитесь к @zyuzko2002")
        logger.warning(f"Неавторизованный доступ от user_id={user_id}")
        return

    await update.message.reply_text(
        "**SLNexus** готов к работе!\n\n"
        "Просто напиши мне задачу, и я передам её Claude Code.\n"
        "Он будет писать код, а ты — получать результат.\n\n"
        "Примеры:\n"
        "• `напиши функцию для расчета дозы инсулина`\n"
        "• `сделай экран логина на Flutter`\n"
        "• `объясни, что такое BLoC`"
    )
    logger.info(f"Пользователь {user_id} запустил бота")

# ===== ФУНКЦИЯ ВЫЗОВА CLAUDE ЧЕРЕЗ SSH =====
async def call_claude_via_ssh(prompt: str) -> str:
    """
    Подключается к claude-worker по SSH и выполняет claude -p
    """
    try:
        # Экранируем кавычки в промпте
        escaped_prompt = prompt.replace('"', '\\"')
        command = f'echo "{escaped_prompt}" | claude -p'

        logger.info(f"SSH команда: {command[:100]}...")

        # Подключаемся и выполняем команду
        async with asyncssh.connect(
            SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None  # В продакшене можно заменить на known_hosts
        ) as conn:
            result = await conn.run(command, check=True)
            return result.stdout.strip()

    except asyncssh.Error as e:
        logger.error(f"SSH ошибка: {e}")
        raise Exception(f"SSH соединение не удалось: {str(e)[:200]}")
    except Exception as e:
        logger.exception(f"Неизвестная ошибка при вызове Claude через SSH")
        raise

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    user_text = update.message.text
    logger.info(f"Получен запрос от {user_id}: {user_text[:100]}...")

    # Отправляем статус "печатает"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # Вызываем Claude через SSH
        response = await call_claude_via_ssh(user_text)

        if response:
            logger.info(f"Ответ Claude получен, длина: {len(response)} символов")
            # Telegram не принимает больше 4096 символов за раз
            for i in range(0, len(response), 4000):
                await update.message.reply_text(response[i:i+4000])
        else:
            await update.message.reply_text("❌ Claude ничего не ответил. Попробуй переформулировать вопрос.")

    except asyncio.TimeoutError:
        logger.error("Таймаут при вызове Claude")
        await update.message.reply_text("⏰ Claude думает слишком долго. Попробуй позже.")
    except Exception as e:
        logger.exception(f"Неизвестная ошибка: {e}")
        await update.message.reply_text(f"💥 Непредвиденная ошибка:\n```\n{str(e)[:500]}\n```")

# ===== ОБРАБОТЧИК ОШИБОК =====
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} вызвало ошибку {context.error}")

# ===== ЗАПУСК =====
def main():
    # Создаём приложение
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрируем хендлеры
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info(f"Бот SLNexus запущен. Доступ разрешён для: {ALLOWED_USER_IDS}")
    logger.info(f"SSH подключение к {SSH_USER}@{SSH_HOST}:{SSH_PORT}")

    # Запускаем поллинг
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
