"""
SLNexus Bot — Telegram → Claude Code (оркестратор Nexus) в claude-worker по SSH.

Этап 2: бот-менеджер задач.
- запускает Claude headless из чекаута sweet_limit (контекст Nexus + сабагенты);
- стримит прогресс по шагам в Telegram (парсит stream-json);
- PR в develop — только по явной команде /pr;
- одна задача за раз, /cancel прерывает.
"""

import os
import html
import json
import time
import asyncio
import logging
import tempfile
from typing import List, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import asyncssh

# ===== НАСТРОЙКИ =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в окружении")

ALLOWED_USER_IDS: List[int] = [
    int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
]
if not ALLOWED_USER_IDS:
    raise ValueError("ALLOWED_USER_IDS не задан или пуст")

SSH_HOST = os.environ.get("SSH_HOST", "claude-worker")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "claude-ssh")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/ssh-key")

PROJECT_DIR = os.environ.get("SWEET_LIMIT_DIR", "/workspace/sweet_limit")

# Модель распознавания речи (faster-whisper, локально). small — баланс точность/скорость на CPU.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


# ===== ЗАПУСК ЗАДАЧИ В ВОРКЕРЕ (SSH + stream-json) =====
async def stream_task(prompt: str, allow_gh: bool):
    """
    Асинхронный генератор. Подключается к воркеру по SSH, дёргает setup-repo.sh,
    затем запускает Claude headless со stream-json. Промпт отдаём в stdin,
    чтобы не возиться с экранированием. Yields кортежи:
      ("event", dict)  — распарсенное событие stream-json
      ("log",   str)   — строка не-JSON (например, вывод setup-repo.sh)
      ("end",   dict)  — {"exit": код, "stderr": текст}
    """
    # allowlist git всегда; gh — только когда нужен PR / чтение PR (жёсткий гейт)
    tools = '--allowedTools "Bash(git *)"'
    if allow_gh:
        tools += ' "Bash(gh *)"'

    inner = (
        f"cd {PROJECT_DIR} && "
        "setup-repo.sh 1>&2 && "
        "claude -p --output-format stream-json --verbose "
        f"--permission-mode acceptEdits {tools}"
    )
    # login-shell, чтобы подхватился /etc/profile.d/worker-env.sh с токенами
    remote_cmd = f"bash -lc '{inner}'"

    logger.info("SSH задача (gh=%s): %s", allow_gh, inner)

    async with asyncssh.connect(
        SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        client_keys=[SSH_KEY_PATH],
        known_hosts=None,
        keepalive_interval=30,  # держим соединение живым на длинных задачах
    ) as conn:
        proc = await conn.create_process(remote_cmd)

        # промпт задачи -> stdin
        proc.stdin.write(prompt)
        proc.stdin.write_eof()

        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield ("event", json.loads(line))
            except json.JSONDecodeError:
                yield ("log", line)

        stderr_text = await proc.stderr.read()
        await proc.wait()
        yield ("end", {"exit": proc.exit_status, "stderr": stderr_text})


# ===== ПЕРЕВОД СОБЫТИЙ stream-json В ЧЕЛОВЕЧЕСКИЕ ШАГИ =====
def _tool_step(block: dict) -> Optional[str]:
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        return f"🔧 bash: {cmd[:70]}"
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"✏️ {name.lower()}: {inp.get('file_path', '')}"
    if name == "Read":
        return f"👀 read: {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"🔍 {name.lower()}: {inp.get('pattern', '')}"
    if name in ("Task", "Agent"):
        sub = inp.get("subagent_type") or inp.get("description", "")
        return f"🤝 делегирует: {sub}"
    if name == "TodoWrite":
        return "🗒️ план обновлён"
    if name.startswith("mcp__dart__"):
        return f"🎯 dart: {name.split('__')[-1]}"
    if name.startswith("Skill") or name == "Skill":
        return f"🧩 скилл: {inp.get('skill', '')}"
    return f"🔧 {name}"


def summarize(event: dict) -> Optional[str]:
    """Короткая строка-шаг для апдейта прогресса, либо None если событие неинтересно."""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        return "🚀 Nexus запущен, читаю проект…"
    if etype == "assistant":
        steps = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                s = _tool_step(block)
                if s:
                    steps.append(s)
        return "\n".join(steps) if steps else None
    return None


# ===== ПРОГРЕСС В TELEGRAM (одно сообщение, троттлинг правок) =====
class Progress:
    EDIT_INTERVAL = 1.3  # сек между правками, чтобы не упереться в лимиты Telegram

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: Optional[int] = None
        self.steps: List[str] = []
        self.last_edit = 0.0

    async def start(self, text: str = "🟢 Принято. Nexus взялся за задачу…"):
        msg = await self.bot.send_message(self.chat_id, text)
        self.message_id = msg.message_id

    async def add(self, text: str, force: bool = False):
        for line in text.split("\n"):
            if line.strip():
                self.steps.append(line)
        self.steps = self.steps[-12:]  # показываем последние шаги

        now = time.monotonic()
        if not force and now - self.last_edit < self.EDIT_INTERVAL:
            return
        self.last_edit = now

        body = "⏳ <b>Работаю…</b>\n\n" + "\n".join(html.escape(s) for s in self.steps)
        try:
            await self.bot.edit_message_text(
                body[:4000], self.chat_id, self.message_id, parse_mode="HTML"
            )
        except Exception:
            pass  # «message is not modified» и т.п. — не критично

    async def final(self, text: str):
        for i in range(0, len(text), 4000):
            await self.bot.send_message(self.chat_id, text[i:i + 4000])


# ===== JOB-МОДЕЛЬ (одна задача за раз) =====
_running = {"task": None}  # type: ignore


async def _execute(bot, chat_id: int, prompt: str, allow_gh: bool):
    prog = Progress(bot, chat_id)
    await prog.start()
    result: Optional[dict] = None
    stderr_tail = ""
    try:
        async for kind, payload in stream_task(prompt, allow_gh):
            if kind == "event":
                if payload.get("type") == "result":
                    result = payload
                else:
                    step = summarize(payload)
                    if step:
                        await prog.add(step)
            elif kind == "end":
                stderr_tail = (payload.get("stderr") or "")[-500:]
                if payload.get("exit"):
                    logger.warning("claude exit=%s stderr=%s", payload.get("exit"), stderr_tail)

        if result is not None:
            ok = not result.get("is_error")
            res_text = (result.get("result") or "").strip()
            await prog.add("— финал —", force=True)
            await prog.final(f"{'✅ Готово' if ok else '❌ Ошибка'}\n\n{res_text or '(пустой ответ)'}")
        else:
            tail = f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""
            await prog.final("⚠️ Задача завершилась без финального result-сообщения." + tail)

    except asyncio.CancelledError:
        await prog.final("🛑 Задача отменена.")
        raise
    except asyncssh.Error as e:
        logger.exception("SSH ошибка")
        await prog.final(f"💥 SSH-сбой: {str(e)[:400]}")
    except Exception as e:
        logger.exception("Сбой задачи")
        await prog.final(f"💥 Непредвиденная ошибка: {str(e)[:400]}")


async def _execute_build(bot, chat_id: int):
    """Детерминированная сборка APK (без LLM): прогон build-скрипта по SSH + доставка артефакта."""
    prog = Progress(bot, chat_id)
    await prog.start("🔨 Собираю APK… (первый раз — несколько минут)")
    artifacts: List[str] = []
    try:
        remote_cmd = "bash -lc 'setup-toolchain.sh 1>&2 && build-apk.sh'"
        async with asyncssh.connect(
            SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,
            keepalive_interval=30,
        ) as conn:
            proc = await conn.create_process(remote_cmd)
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("ARTIFACT:"):
                    artifacts.append(line[len("ARTIFACT:"):])
                else:
                    await prog.add(f"🔨 {line[:80]}")

            stderr_text = await proc.stderr.read()
            await proc.wait()

            if proc.exit_status:
                await prog.final(f"❌ Сборка упала (exit {proc.exit_status}).\n\n{(stderr_text or '')[-1200:]}")
                return
            if not artifacts:
                await prog.final("⚠️ Сборка прошла, но APK не найден.")
                return

            # выкачиваем артефакт(ы) по SFTP тем же SSH и шлём в Telegram
            await prog.add("📦 Забираю APK…", force=True)
            sent = 0
            skipped = 0
            async with conn.start_sftp_client() as sftp:
                for remote in artifacts:
                    name = os.path.basename(remote)
                    local = os.path.join(tempfile.gettempdir(), name)
                    await sftp.get(remote, local)
                    try:
                        size = os.path.getsize(local)
                        if size > 49 * 1024 * 1024:  # лимит бота Telegram ~50 МБ
                            skipped += 1
                            await bot.send_message(
                                chat_id,
                                f"⚠️ {name} = {size // 1024 // 1024} МБ — больше лимита Telegram (50 МБ).\n"
                                f"Артефакт на сервере: {remote}",
                            )
                        else:
                            sent += 1
                            with open(local, "rb") as fh:
                                await bot.send_document(chat_id, document=fh, filename=name)
                            # успешно отправили — убираем артефакт с сервера, чтобы не копился
                            try:
                                await sftp.remove(remote)
                            except Exception:
                                pass
                    finally:
                        try:
                            os.remove(local)
                        except OSError:
                            pass

            if sent and not skipped:
                await prog.final("✅ Сборка готова, APK отправлен.")
            elif sent and skipped:
                await prog.final(f"✅ Сборка готова. Отправлено: {sent}, пропущено по размеру: {skipped} (путь выше).")
            else:
                await prog.final("✅ Сборка готова, но APK не влез в лимит Telegram — путь на сервере выше.")

    except asyncio.CancelledError:
        await prog.final("🛑 Сборка отменена.")
        raise
    except asyncssh.Error as e:
        logger.exception("SSH ошибка сборки")
        await prog.final(f"💥 SSH-сбой при сборке: {str(e)[:400]}")
    except Exception as e:
        logger.exception("Сбой сборки")
        await prog.final(f"💥 Ошибка сборки: {str(e)[:400]}")


async def run_job(update: Update, prompt: str, allow_gh: bool):
    if not prompt.strip():
        await update.message.reply_text("Пустая задача. Напиши, что нужно сделать.")
        return

    t = _running.get("task")
    if t and not t.done():
        await update.message.reply_text(
            "⚠️ Я ещё занят предыдущей задачей. Дождись её завершения или /cancel."
        )
        return

    chat_id = update.effective_chat.id
    bot = update.get_bot()
    _running["task"] = asyncio.create_task(_execute(bot, chat_id, prompt, allow_gh))


# ===== РАСПОЗНАВАНИЕ ГОЛОСА (faster-whisper, локально на сервере) =====
_whisper = {"model": None}
_whisper_lock = asyncio.Lock()


async def _ensure_whisper():
    # ленивая инициализация с double-checked locking: грузим модель один раз
    if _whisper["model"] is None:
        async with _whisper_lock:
            if _whisper["model"] is None:
                from faster_whisper import WhisperModel
                logger.info("Загружаю Whisper-модель '%s'…", WHISPER_MODEL)
                _whisper["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper["model"]


async def transcribe_voice(path: str) -> str:
    model = await _ensure_whisper()

    def _run() -> str:
        segments, _info = model.transcribe(path, language="ru")
        return "".join(seg.text for seg in segments).strip()

    # транскрипция CPU-bound — уводим в executor, чтобы не блокировать event loop
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


# ===== ХЕНДЛЕРЫ =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("❌ Доступ запрещён. Обратитесь к @zyuzko2002")
        logger.warning("Неавторизованный доступ от user_id=%s", update.effective_user.id)
        return
    await update.message.reply_text(
        "*SLNexus* на связи.\n\n"
        "Просто напиши задачу — Nexus реализует её в проекте sweet_limit, "
        "закоммитит и запушит ветку.\n\n"
        "• обычное сообщение — задача без PR\n"
        "• 🎙️ голосовое — распознаю и выполню как задачу\n"
        "• `/pr <задача>` — задача + открыть PR в develop\n"
        "• `/fix [уточнение]` — прочитать замечания к открытому PR и выкатить правки в ту же ветку\n"
        "• `/build` — собрать APK текущего состояния и прислать сюда\n"
        "• `/cancel` — прервать текущую задачу",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await run_job(update, update.message.text, allow_gh=False)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    status = await update.message.reply_text("🎙️ Распознаю голос…")
    path = None
    try:
        tg_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            path = tmp.name
        await tg_file.download_to_drive(path)
        text = await transcribe_voice(path)
    except Exception as e:
        logger.exception("Ошибка распознавания голоса")
        await status.edit_text(f"💥 Не смог распознать голос: {str(e)[:300]}")
        return
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    if not text:
        await status.edit_text("🤷 Ничего не разобрал в записи.")
        return

    # показываем расшифровку и запускаем как обычную задачу (голос = без PR)
    await status.edit_text(f"🎙️ Распознал:\n«{text}»")
    await run_job(update, text, allow_gh=False)


async def cmd_pr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    task = (update.message.text or "").partition(" ")[2].strip()
    if not task:
        await update.message.reply_text("Формат: `/pr <что сделать>`", parse_mode="Markdown")
        return
    prompt = task + "\n\nКогда всё готово и закоммичено — открой PR в develop."
    await run_job(update, prompt, allow_gh=True)


# Инструкция Nexus для итерации по уже открытому PR (режим /fix)
ITERATION_PROMPT = (
    "Это итерация по уже открытому pull request — пользователь оставил замечания в комментариях.\n"
    "Действуй так:\n"
    "1. Найди относящийся открытый PR: `gh pr list --state open` (если неоднозначно — самый свежий твой).\n"
    "2. Прочитай ВСЕ замечания — и обсуждение PR (`gh pr view <n> --comments`), и инлайн-комментарии "
    "ревью к строкам кода (`gh api repos/Tsezia/sweet_limit/pulls/<n>/comments`).\n"
    "3. Переключись на ветку PR из origin: `git fetch origin && git checkout -B <branch> origin/<branch>`.\n"
    "4. Внеси правки по замечаниям (делегируя Ванделю/Лине в их зонах), закоммить и запушь в ту же ветку — "
    "PR обновится сам, новый создавать НЕ нужно.\n"
    "5. В конце кратко отчитайся, что именно поправил по каждому замечанию."
)


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    extra = (update.message.text or "").partition(" ")[2].strip()
    prompt = ITERATION_PROMPT
    if extra:
        prompt += f"\n\nДополнительное уточнение от пользователя: {extra}"
    await run_job(update, prompt, allow_gh=True)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    t = _running.get("task")
    if t and not t.done():
        t.cancel()
        await update.message.reply_text("🛑 Отменяю текущую задачу…")
    else:
        await update.message.reply_text("Сейчас нечего отменять.")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    t = _running.get("task")
    if t and not t.done():
        await update.message.reply_text("⚠️ Я ещё занят предыдущей задачей. Дождись её или /cancel.")
        return
    chat_id = update.effective_chat.id
    bot = update.get_bot()
    _running["task"] = asyncio.create_task(_execute_build(bot, chat_id))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update вызвал ошибку: %s", context.error)


# ===== ЗАПУСК =====
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pr", cmd_pr))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("SLNexus запущен. Доступ: %s", ALLOWED_USER_IDS)
    logger.info("Воркер: %s@%s:%s, проект: %s", SSH_USER, SSH_HOST, SSH_PORT, PROJECT_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
