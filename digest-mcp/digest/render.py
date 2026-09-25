"""Текст дайджеста: шаблон с вариативностью, без LLM.

Вариант фразы выбирается детерминированно — по дате и слоту, а не случайно.
Так один и тот же слот всегда рендерится одинаково (digest_preview показывает
ровно то, что уйдёт в рассылку), но день ко дню текст меняется и не выглядит
штампованным.
"""
import html
import re
from typing import Any, Dict, List, Optional

from .models import Task

DESC_LIMIT = 200
COMMENT_LIMIT = 120
CHUNK = 4000          # лимит одного сообщения Telegram — как в Progress.final бота

GREETINGS: Dict[str, List[str]] = {
    "morning": ["Доброе утро! ☀️", "С утром! ☀️", "Утро доброе! 🌤"],
    "day": ["Привет! 🙂", "Добрый день!", "День в разгаре 🙂"],
    "evening": ["Добрый вечер! 🌙", "Вечер добрый! 🌙", "Привет, вечер! ✨"],
}

CLOSINGS: Dict[str, List[str]] = {
    "morning": [
        "Хорошего дня! Если что-то застряло — скажи, разберёмся.",
        "Продуктивного дня! Нужна помощь по задаче — пиши.",
        "Хорошего дня. Захочешь — возьмусь за любую из них.",
    ],
    "day": [
        "Если по какой-то задаче нужна помощь — пиши.",
        "Скажи, если что-то подтолкнуть.",
        "На связи, если понадоблюсь.",
    ],
    "evening": [
        "Хорошего вечера! Завтра продолжим.",
        "Отдыхай, вечер хороший. Задачи никуда не убегут.",
        "Хорошего вечера! Если что-то доделать — скажи, успеем.",
    ],
}

EMPTY: Dict[str, List[str]] = {
    "morning": [
        "Доброе утро! ☀️ Активных задач нет — всё разобрано. Хорошего дня!",
        "С утром! ☀️ В работе сейчас пусто. Чистая доска — хорошее начало дня.",
    ],
    "day": [
        "Привет! Активных задач нет — всё закрыто.",
        "Сейчас в работе ничего. Тишина по проекту 🙂",
    ],
    "evening": [
        "Добрый вечер! 🌙 Активных задач нет — всё разобрано. Хорошего вечера!",
        "Вечер добрый! В работе пусто, можно выдохнуть. Хорошего вечера!",
    ],
}


def _pick(variants: List[str], seed: int) -> str:
    return variants[seed % len(variants)] if variants else ""


def _seed(date_key: str, slot: str) -> int:
    """Устойчивый seed: сумма кодов даты и слота. Не random — чтобы preview и
    рассылка в одном слоте давали идентичный текст."""
    return sum(ord(c) for c in f"{date_key}:{slot}")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _trim(text: Optional[str], limit: int) -> str:
    """Описания в трекере — Markdown в несколько абзацев; в дайджест нужна одна
    строка, поэтому переводы строк сворачиваем."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _comments_note(t: Task) -> str:
    n = t.comments.total
    if not n:
        return "обсуждений пока нет"
    word = _plural(n, "комментарий", "комментария", "комментариев")
    return f"💬 {n} {word}"


def render(tasks: List[Task], kind: str, slot: str, date_key: str,
           totals: Optional[Dict[str, Any]] = None) -> str:
    """HTML для parse_mode='HTML'. Все значения из трекера — через html.escape:
    в заголовках и описаниях регулярно встречаются &, < и кавычки."""
    seed = _seed(date_key, slot)
    kind = kind if kind in GREETINGS else "day"

    if not tasks:
        return _pick(EMPTY[kind], seed)

    n = len(tasks)
    word = _plural(n, "задача", "задачи", "задач")
    head = f"{_pick(GREETINGS[kind], seed)} В работе {n} {word}."

    delta = (totals or {}).get("deltaSincePrev") or {}
    marks = []
    if delta.get("appeared"):
        marks.append(f"новых: {delta['appeared']}")
    if delta.get("statusChanged"):
        marks.append(f"сменили статус: {delta['statusChanged']}")
    if delta.get("closed"):
        marks.append(f"ушло из работы: {delta['closed']}")
    if marks:
        head += " С прошлого раза — " + ", ".join(marks) + "."

    # группировка по очередям: порядок очередей и задач внутри — стабильный,
    # иначе одинаковый набор задач каждый раз выглядел бы по-новому
    by_queue: Dict[str, List[Task]] = {}
    names: Dict[str, str] = {}
    for t in tasks:
        by_queue.setdefault(t.queue, []).append(t)
        if t.queue_name:
            names[t.queue] = t.queue_name

    parts: List[str] = [head, ""]
    for queue in sorted(by_queue):
        title = names.get(queue)
        label = f"🗂 <b>{html.escape(queue)}</b>"
        if title:
            label += f" — «{html.escape(title)}»"
        parts.append(label)
        for t in sorted(by_queue[queue], key=lambda x: x.key):
            parts.append(f"  • <b>{html.escape(t.key)}</b> — {html.escape(t.title)}")
            meta = [
                f"исполнитель: {html.escape(t.assignee)}" if t.assignee else "исполнитель: не назначен",
                html.escape(t.status_name or t.status),
            ]
            if t.priority is not None:
                meta.append(f"приоритет {t.priority}")
            meta.append(_comments_note(t))
            parts.append("    <i>" + " · ".join(meta) + "</i>")
            desc = _trim(t.description, DESC_LIMIT)
            if desc:
                parts.append("    " + html.escape(desc))
            if t.comments.last_body:
                who = f"{html.escape(t.comments.last_author)}: " if t.comments.last_author else ""
                parts.append(f"    Последний — {who}«{html.escape(_trim(t.comments.last_body, COMMENT_LIMIT))}»")
        parts.append("")

    parts.append(_pick(CLOSINGS[kind], seed))
    return "\n".join(parts).strip()


def to_plain(text_html: str) -> str:
    """Тот же текст без разметки — для MCP-клиентов, которым HTML не нужен."""
    return html.unescape(re.sub(r"<[^>]+>", "", text_html))


def split(text: str, limit: int = CHUNK) -> List[str]:
    """Разбивка длинного сообщения по границам строк: рвать HTML-тег посередине
    нельзя — Telegram отклонит разметку."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.split("\n"):
        # одна строка длиннее лимита — режем жёстко, иначе зациклимся
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
