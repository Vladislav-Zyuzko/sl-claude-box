"""
SLNexus Bot — Telegram → Claude Code (оркестратор Nexus) в claude-worker по SSH.

Этап 2: бот-менеджер задач.
- запускает Claude headless из чекаута sweet_limit (контекст Nexus + сабагенты);
- стримит прогресс по шагам в Telegram (парсит stream-json);
- PR в develop — только по явной команде /pr;
- одна задача за раз, /cancel прерывает.
"""

import os
import re
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

# Каталог в воркере, куда складываем присланные фото/скрины.
# Живут до явной /clearphotos — Nexus их НЕ удаляет.
PHOTOS_DIR = os.environ.get("NEXUS_PHOTOS_DIR", "/tmp/nexus-uploads").rstrip("/")

# В правилах permissions Claude Code одиночный ведущий "/" означает путь ОТ ИСТОЧНИКА
# НАСТРОЕК, а не от корня ФС; абсолютный путь пишется с "//". Отсюда лишний слеш.
PHOTOS_RULE = "/" + PHOTOS_DIR if PHOTOS_DIR.startswith("/") else PHOTOS_DIR


def _ssh():
    """Единое SSH-подключение к воркеру (одни параметры для всех операций)."""
    return asyncssh.connect(
        SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        client_keys=[SSH_KEY_PATH],
        known_hosts=None,
        keepalive_interval=30,  # держим соединение живым на длинных задачах
    )

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
async def stream_task(prompt: str, allow_gh: bool, new_photos: Optional[List[str]] = None,
                      resume_session: Optional[str] = None):
    """
    Асинхронный генератор. Подключается к воркеру по SSH, дёргает setup-repo.sh,
    затем запускает Claude headless со stream-json. Промпт отдаём в stdin,
    чтобы не возиться с экранированием. Yields кортежи:
      ("event", dict)  — распарсенное событие stream-json
      ("log",   str)   — строка не-JSON (например, вывод setup-repo.sh)
      ("end",   dict)  — {"exit": код, "stderr": текст}

    Фото из PHOTOS_DIR НЕ удаляются (живут до /clearphotos): к промпту лишь
    подмешивается список приложенных/накопленных скринов.
    """
    # Базовый allowlist: git + тулчейн проекта (чтобы Nexus/сабагенты могли
    # верифицировать работу — analyze/format/test/кодоген). gh — только под PR/fix.
    # Read папки с фото — всегда, чтобы Nexus мог открыть присланные скрины.
    allowed = [
        "Bash(git *)",
        "Bash(fvm *)",      # проект работает через fvm-обёртки (fvm flutter / fvm dart)
        "Bash(flutter *)",
        "Bash(dart *)",
        "Bash(make *)",     # у проекта есть Makefile с хелперами (напр. make pg)
        "mcp__dart",        # все инструменты Dart MCP-сервера, если он поднят в воркере
        f"Read({PHOTOS_RULE}/**)",
    ]
    if allow_gh:
        allowed.append("Bash(gh *)")
    tools = "--allowedTools " + " ".join(f'"{t}"' for t in allowed)

    # --resume <id> продолжает прежний диалог Nexus (память между сообщениями).
    # Сессии Claude Code лежат в ~/.claude воркера, привязаны к cwd (PROJECT_DIR).
    resume_flag = f"--resume {resume_session} " if resume_session else ""

    inner = (
        f"cd {PROJECT_DIR} && "
        # --add-dir падает на несуществующем пути: гарантируем каталог до запуска
        f"mkdir -p {PHOTOS_DIR} && "
        "setup-repo.sh 1>&2 && "
        "claude -p --output-format stream-json --verbose "
        f"{resume_flag}"
        # PHOTOS_DIR лежит вне cwd, а Claude читает только рабочий каталог и то,
        # что явно добавлено --add-dir: без этого allowlist-правило не сработает.
        f"--add-dir {PHOTOS_DIR} "
        "--mcp-config /usr/local/etc/dart-mcp.json "
        f"--permission-mode acceptEdits {tools}"
    )
    # login-shell, чтобы подхватился /etc/profile.d/worker-env.sh с токенами
    remote_cmd = f"bash -lc '{inner}'"

    logger.info("SSH задача (gh=%s, photos=%s, resume=%s): %s",
                allow_gh, bool(new_photos), bool(resume_session), inner)

    async with _ssh() as conn:
        # подмешиваем в промпт сведения о приложенных/накопленных фото
        note = await _photos_prompt_note(conn, new_photos)
        if note:
            prompt = prompt + note

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


# ===== ФОТО/СКРИНЫ: хранение в воркере и подмешивание в промпт =====
def _safe_name(name: str) -> str:
    """Безопасное имя файла: убираем путь, пробелы→'_', выкидываем спецсимволы."""
    name = os.path.basename((name or "").strip()) or "photo.jpg"
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^\w.\-]", "", name)
    return name or "photo.jpg"


def _photo_index(name: str) -> int:
    m = re.match(r"(\d+)-", name)
    return int(m.group(1)) if m else 10 ** 9


def _next_start_index(existing: List[str]) -> int:
    """Максимальный номер среди уже лежащих 'N-...' файлов — для сквозной нумерации."""
    mx = 0
    for n in existing:
        m = re.match(r"(\d+)-", n)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


async def _list_photos(conn) -> List[str]:
    """Имена фото в PHOTOS_DIR воркера, отсортированы по номеру."""
    try:
        async with conn.start_sftp_client() as sftp:
            names = [n for n in await sftp.listdir(PHOTOS_DIR) if not n.startswith(".")]
    except Exception:
        return []
    return sorted(names, key=_photo_index)


async def _photos_prompt_note(conn, new_photos: Optional[List[str]]) -> str:
    """Приписка к промпту: какие фото приложены к ЭТОЙ задаче / накоплены ранее."""
    if new_photos:
        body = "\n".join(f"- {p}" for p in new_photos)
        return (
            "\n\nК этой задаче приложены фото — открой их инструментом Read "
            f"и учитывай при выполнении:\n{body}"
        )
    names = await _list_photos(conn)
    if names:
        body = "\n".join(f"- {PHOTOS_DIR}/{n}" for n in names)
        return (
            "\n\nРанее присланные пользователем фото (номер — в начале имени). "
            "Если задача ссылается на фото по номеру — открой нужный инструментом Read, "
            f"иначе не обращай на них внимания:\n{body}"
        )
    return ""


async def _store_photos(items: List[tuple]) -> List[str]:
    """Заливает локальные файлы в PHOTOS_DIR воркера со сквозной нумерацией 'N-<имя>'.
    items: [(local_path, original_name)]. Возвращает удалённые пути; локальные копии чистит."""
    stored: List[str] = []
    async with _ssh() as conn:
        async with conn.start_sftp_client() as sftp:
            try:
                await sftp.makedirs(PHOTOS_DIR, exist_ok=True)
            except Exception:
                pass
            try:
                existing = [n for n in await sftp.listdir(PHOTOS_DIR) if not n.startswith(".")]
            except Exception:
                existing = []
            idx = _next_start_index(existing)
            for local, base in items:
                idx += 1
                remote = f"{PHOTOS_DIR}/{idx}-{_safe_name(base)}"
                await sftp.put(local, remote)
                stored.append(remote)
                try:
                    os.remove(local)
                except OSError:
                    pass
    return stored


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

# Память диалога Nexus: chat_id -> session_id Claude Code (для --resume).
# Сбрасывается командой /reset. Хранится в памяти бота — переживает задачи,
# но не рестарт самого бота (сам транскрипт сессии живёт в ~/.claude воркера).
_sessions: dict = {}


def _looks_like_stale_session(stderr: str) -> bool:
    s = (stderr or "").lower()
    return ("no conversation" in s or "session" in s and "found" in s
            or "--resume" in s or "no such session" in s)


async def _execute(bot, chat_id: int, prompt: str, allow_gh: bool, new_photos: Optional[List[str]] = None):
    prog = Progress(bot, chat_id)
    await prog.start()
    result: Optional[dict] = None
    stderr_tail = ""
    resume = _sessions.get(chat_id)   # продолжаем прежний диалог Nexus, если он есть
    session_id: Optional[str] = None
    try:
        async for kind, payload in stream_task(prompt, allow_gh, new_photos, resume_session=resume):
            if kind == "event":
                sid = payload.get("session_id")
                if sid:
                    session_id = sid  # запоминаем актуальный id для следующего хода
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
            # сохраняем сессию на чат только при успехе — чтобы не тянуть сломанный контекст
            if ok and session_id:
                _sessions[chat_id] = session_id
            await prog.add("— финал —", force=True)
            await prog.final(f"{'✅ Готово' if ok else '❌ Ошибка'}\n\n{res_text or '(пустой ответ)'}")
        else:
            # запуск не дошёл до result. Если резюмировали и воркер не нашёл сессию
            # (например, был пересобран) — сбрасываем, чтобы следующее сообщение начало новый диалог.
            stale = resume and _looks_like_stale_session(stderr_tail)
            if stale:
                _sessions.pop(chat_id, None)
            tail = f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""
            hint = ("\n\n♻️ Похоже, прежний контекст устарел — сбросил его. "
                    "Повтори сообщение, оно начнёт новый диалог.") if stale else ""
            await prog.final("⚠️ Задача завершилась без финального result-сообщения." + hint + tail)

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
                    # уникальное имя для Telegram: app-release-a3f9k2.apk. Имя+размер
                    # одинаковых сборок совпадали → телефон/Telegram считали файл тем же
                    # и не ставили заново. 6-значный ключ делает каждую отправку уникальной.
                    stem, ext = os.path.splitext(name)
                    send_name = f"{stem}-{os.urandom(3).hex()}{ext}"
                    local = os.path.join(tempfile.gettempdir(), name)
                    await sftp.get(remote, local)
                    try:
                        size = os.path.getsize(local)
                        if size > 49 * 1024 * 1024:  # лимит бота Telegram ~50 МБ
                            skipped += 1
                            await bot.send_message(
                                chat_id,
                                f"⚠️ {send_name} = {size // 1024 // 1024} МБ — больше лимита Telegram (50 МБ).\n"
                                f"Артефакт на сервере: {remote}",
                            )
                        else:
                            sent += 1
                            with open(local, "rb") as fh:
                                await bot.send_document(chat_id, document=fh, filename=send_name)
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


def _is_busy() -> bool:
    t = _running.get("task")
    return bool(t and not t.done())


def _start_task(bot, chat_id: int, prompt: str, allow_gh: bool, new_photos: Optional[List[str]] = None):
    _running["task"] = asyncio.create_task(_execute(bot, chat_id, prompt, allow_gh, new_photos))


async def run_job(update: Update, prompt: str, allow_gh: bool, new_photos: Optional[List[str]] = None):
    if not prompt.strip():
        await update.message.reply_text("Пустая задача. Напиши, что нужно сделать.")
        return

    if _is_busy():
        await update.message.reply_text(
            "⚠️ Я ещё занят предыдущей задачей. Дождись её завершения или /cancel."
        )
        return

    _start_task(update.get_bot(), update.effective_chat.id, prompt, allow_gh, new_photos)


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
        "закоммитит и запушит ветку. Контекст диалога он помнит между сообщениями "
        "(не нужно повторять вводные каждый раз).\n\n"
        "• обычное сообщение — задача без PR\n"
        "• 🎙️ голосовое — распознаю и выполню как задачу\n"
        "• 📷 фото (можно несколько/альбомом) — с подписью уйдут в задачу, без подписи просто сохранятся; ссылайся на них по номеру (`2-...`)\n"
        "• 🖼️ скрин можно слать и файлом (без сжатия) — качество выше\n"
        "• `/clearphotos` — очистить сохранённые фото\n"
        "• `/pr <задача>` — задача + открыть PR в develop\n"
        "• `/fix [уточнение]` — прочитать замечания к открытому PR и выкатить правки в ту же ветку\n"
        "• `/build` — собрать APK текущего состояния и прислать сюда\n"
        "• `/reset` — забыть контекст диалога и начать с чистого листа\n"
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


# Буфер для альбомов: Telegram шлёт фото media-группы отдельными апдейтами
# (подпись обычно только у первого) — собираем их вместе с дебаунсом.
_media_buffers: dict = {}  # media_group_id -> {items, caption, chat_id, timer}


async def _process_photo_batch(bot, chat_id: int, items: List[tuple], caption: str):
    """Сохраняет пачку фото в воркер; с подписью — запускает задачу, без подписи — просто складывает."""
    try:
        stored = await _store_photos(items)
    except Exception as e:
        logger.exception("Не смог сохранить фото")
        for local, _ in items:
            try:
                os.remove(local)
            except OSError:
                pass
        await bot.send_message(chat_id, f"💥 Не смог сохранить фото: {str(e)[:300]}")
        return

    listing = "\n".join(f"• {os.path.basename(p)}" for p in stored)
    if not caption:
        await bot.send_message(
            chat_id,
            f"📥 Сохранил {len(stored)} фото:\n{listing}\n\n"
            "Сошлись на них по номеру в задаче. Очистить — /clearphotos.",
        )
        return

    if _is_busy():
        await bot.send_message(
            chat_id,
            f"📥 Сохранил {len(stored)} фото:\n{listing}\n\n"
            "⏳ Сейчас занят другой задачей — пришли задачу текстом, когда освобожусь "
            "(фото уже на месте, ссылайся по номеру).",
        )
        return

    await bot.send_message(chat_id, f"📥 Принял {len(stored)} фото, отдаю Nexus’у.")
    _start_task(bot, chat_id, caption, allow_gh=False, new_photos=stored)


async def _flush_media_group(mgid: str, bot):
    try:
        await asyncio.sleep(1.5)  # ждём, пока приедут все фото альбома
    except asyncio.CancelledError:
        return
    buf = _media_buffers.pop(mgid, None)
    if buf:
        await _process_photo_batch(bot, buf["chat_id"], buf["items"], buf["caption"])


async def _ingest_photo(update: Update, local_path: str, base_name: str):
    """Маршрутизация: одиночное фото — сразу, альбом — через буфер с дебаунсом."""
    msg = update.message
    caption = (msg.caption or "").strip()
    mgid = msg.media_group_id
    if not mgid:
        await _process_photo_batch(update.get_bot(), msg.chat_id, [(local_path, base_name)], caption)
        return
    buf = _media_buffers.get(mgid)
    if buf is None:
        buf = {"items": [], "caption": "", "chat_id": msg.chat_id, "timer": None}
        _media_buffers[mgid] = buf
    buf["items"].append((local_path, base_name))
    if caption:
        buf["caption"] = caption
    if buf["timer"]:
        buf["timer"].cancel()
    buf["timer"] = asyncio.create_task(_flush_media_group(mgid, update.get_bot()))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    # качаем самый крупный вариант фото во временный файл бота
    path = None
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        await tg_file.download_to_drive(path)
    except Exception as e:
        logger.exception("Не смог скачать фото")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        await update.message.reply_text(f"💥 Не смог принять фото: {str(e)[:300]}")
        return
    await _ingest_photo(update, path, "photo.jpg")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрин, присланный файлом (без сжатия) — сохраняем под исходным именем."""
    if not is_allowed(update.effective_user.id):
        return
    doc = update.message.document
    path = None
    try:
        tg_file = await doc.get_file()
        suffix = os.path.splitext(doc.file_name or "")[1] or ".png"
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        await tg_file.download_to_drive(path)
    except Exception as e:
        logger.exception("Не смог скачать документ-картинку")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        await update.message.reply_text(f"💥 Не смог принять файл: {str(e)[:300]}")
        return
    await _ingest_photo(update, path, doc.file_name or "image.png")


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


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    had = _sessions.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "🧹 Контекст диалога сброшен — следующее сообщение начнёт новый."
        if had else "Контекст и так пуст — диалог уже новый."
    )


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


async def cmd_clearphotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    try:
        async with _ssh() as conn:
            async with conn.start_sftp_client() as sftp:
                try:
                    names = [n for n in await sftp.listdir(PHOTOS_DIR) if not n.startswith(".")]
                except Exception:
                    names = []
                for n in names:
                    try:
                        await sftp.remove(f"{PHOTOS_DIR}/{n}")
                    except Exception:
                        pass
    except Exception as e:
        logger.exception("clearphotos failed")
        await update.message.reply_text(f"💥 Не смог почистить: {str(e)[:300]}")
        return
    await update.message.reply_text(
        f"🧹 Удалил {len(names)} фото." if names else "📭 Сохранённых фото нет."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update вызвал ошибку: %s", context.error)


# ===== ЗАПУСК =====
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pr", cmd_pr))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("clearphotos", cmd_clearphotos))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("SLNexus запущен. Доступ: %s", ALLOWED_USER_IDS)
    logger.info("Воркер: %s@%s:%s, проект: %s", SSH_USER, SSH_HOST, SSH_PORT, PROJECT_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
