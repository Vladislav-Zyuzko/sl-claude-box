"""Источник данных — sl-tracker-mcp (чужой репозиторий, правки только через автора).

Свой REST-клиент к трекеру здесь сознательно НЕ пишется: доступ к задачам идёт
только через инструменты этого MCP-сервера.

Важное про контракт: инструменты sl-tracker-mcp отдают **человекочитаемый текст**,
а не JSON — сервер написан под LLM и structuredContent не заполняет. Поэтому ниже
парсеры прозы, а не разбор объектов. Форматы сняты с живого сервера 25.09.2026 и
закреплены тестами на реальных образцах (tests/test_tracker.py): если автор сервера
поменяет формулировки, тесты покажут это сразу, а не дайджест в 08:00.

Инструменты (проверено через tools/list):
    list_queues(project?)               — очереди проекта и их статусы
    list_issues(queue, status?, limit?)  — задачи очереди, без описания
    get_task(key, includeComments?)      — описание + комментарии ОДНИМ вызовом
Имена переопределяются переменной SL_TRACKER_TOOLS (JSON), если контракт поменяется.
`list_comments` не используем: includeComments у get_task отдаёт то же самое и
экономит по вызову на каждую задачу.

Наружу модуль отдаёт только list[Task] — остальной дайджест про MCP не знает.
"""
import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .models import Comments, Task

logger = logging.getLogger(__name__)

TOOLS: Dict[str, str] = {
    "queues": "list_queues",
    "issues": "list_issues",
    "issue": "get_task",
}

NOT_ASSIGNED = "не назначен"

# «  MOBILE («Flutter задачи»): open=«Открыт», in_progress=«В работе», ...»
_QUEUE_RE = re.compile(r"^\s+([A-Za-z0-9_-]+)\s*\(«(.*?)»\)\s*:\s*(.*)$")
# «- MOBILE-1: «Заголовок» · статус in_progress («В работе») · исполнитель X · приоритет 50»
_ISSUE_RE = re.compile(r"^-\s*([A-Za-z0-9]+-\d+)\s*:\s*(.*)$")
_TITLE_RE = re.compile(r"^«(.*)»$")
_STATUS_SEG_RE = re.compile(r"^статус\s+(\S+)(?:\s*\(«(.*?)»\))?")
_PRIORITY_SEG_RE = re.compile(r"^приоритет\s+(-?\d+)")
_ASSIGNEE_SEG_RE = re.compile(r"^исполнитель\s+(.+)$")
# «комментарии (3):» и «- автор (2026-09-24T06:31:23.117Z): текст»
_COMMENTS_HEAD_RE = re.compile(r"^комментарии\s*\((\d+)\)\s*:")
_COMMENT_RE = re.compile(r"^-\s*(.*?)\s*\((\d{4}-\d{2}-\d{2}T[^)]*)\)\s*:\s*(.*)$")


def _tools() -> Dict[str, str]:
    raw = os.environ.get("SL_TRACKER_TOOLS", "").strip()
    if not raw:
        return dict(TOOLS)
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("ожидался объект")
    except Exception:
        logger.exception("SL_TRACKER_TOOLS не разобран — беру имена по умолчанию")
        return dict(TOOLS)
    merged = dict(TOOLS)
    merged.update({k: str(v) for k, v in override.items() if k in TOOLS})
    return merged


# ───────────────────────── парсеры ответов ─────────────────────────


def parse_queues(text: str) -> List[Tuple[str, str]]:
    """Очереди из ответа list_queues: [(key, name)].

    Строка проекта идёт без отступа («sweet-limit: Sweet Limit»), очереди — с
    отступом, поэтому различаем по нему, а не по содержимому.
    """
    out: List[Tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = _QUEUE_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def parse_issues(text: str) -> List[Dict[str, Any]]:
    """Задачи из ответа list_issues.

    Хвост строки делим по « · » и опознаём сегменты по префиксу, а не по позиции:
    состав полей у сервера меняется от задачи к задаче («исполнитель не назначен»,
    отсутствующий приоритет).
    """
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _ISSUE_RE.match(line.strip())
        if not m:
            continue
        key, tail = m.group(1), m.group(2)
        row: Dict[str, Any] = {"key": key, "title": "", "status": "", "status_name": "",
                               "assignee": None, "priority": None}
        for i, seg in enumerate(s.strip() for s in tail.split("·")):
            if i == 0:
                t = _TITLE_RE.match(seg)
                row["title"] = t.group(1) if t else seg
                continue
            ms = _STATUS_SEG_RE.match(seg)
            if ms:
                row["status"] = ms.group(1)
                row["status_name"] = ms.group(2) or ""
                continue
            mp = _PRIORITY_SEG_RE.match(seg)
            if mp:
                row["priority"] = int(mp.group(1))
                continue
            ma = _ASSIGNEE_SEG_RE.match(seg)
            if ma:
                name = ma.group(1).strip()
                row["assignee"] = None if name == NOT_ASSIGNED else name
                continue
        if not row["title"]:
            logger.warning("не разобрал строку задачи: %s", line[:120])
        out.append(row)
    return out


def parse_task(text: str) -> Dict[str, Any]:
    """Описание и комментарии из ответа get_task(includeComments=True).

    Описание многострочное, поэтому забираем всё между «описание:» и либо
    «комментарии (N):», либо концом текста.
    """
    desc: List[str] = []
    total = 0
    last_author: Optional[str] = None
    last_body: Optional[str] = None

    mode = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("описание:"):
            mode = "desc"
            rest = stripped[len("описание:"):].strip()
            if rest:
                desc.append(rest)
            continue
        mh = _COMMENTS_HEAD_RE.match(stripped)
        if mh:
            total = int(mh.group(1))
            mode = "comments"
            continue
        if mode == "desc":
            desc.append(line.rstrip())
        elif mode == "comments":
            mc = _COMMENT_RE.match(stripped)
            if mc:
                # последний в списке и есть самый свежий
                last_author = mc.group(1) or None
                last_body = mc.group(3) or None

    return {
        "description": "\n".join(desc).strip() or None,
        "comments": Comments(total=total, last_author=last_author, last_body=last_body),
    }


def _text_of(result: Any) -> str:
    """Склейка текстовых блоков ответа инструмента."""
    out = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            out.append(text)
    return "\n".join(out)


# ───────────────────────── клиент ─────────────────────────


class Tracker:
    """Клиент sl-tracker-mcp. На каждый прогон — одна сессия."""

    def __init__(self, url: str, token: str = "", timeout: float = 20.0,
                 concurrency: int = 5):
        self.url = url
        self._token = token
        self._timeout = timeout
        self._concurrency = max(1, concurrency)
        self.tools = _tools()

    def _transport(self):
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # streamable_http_client — асинхронный контекст-менеджер, отдающий
        # TransportStreams, то есть готовый Transport для Client.
        return streamable_http_client(
            self.url,
            http_client=httpx2.AsyncClient(headers=headers, timeout=self._timeout),
        )

    async def _text(self, client: Client, tool: str, args: Dict[str, Any]) -> str:
        result = await client.call_tool(tool, args)
        if getattr(result, "isError", False):
            raise RuntimeError(f"{tool}: сервер вернул ошибку: {_text_of(result)[:300]}")
        return _text_of(result)

    async def probe(self) -> Dict[str, Any]:
        """Что сервер реально умеет. Зовётся из MCP-инструмента status: если имена
        не совпали, это видно сразу и с конкретикой."""
        async with Client(self._transport()) as client:
            listed = await client.list_tools()
            available = sorted(t.name for t in listed.tools)
        missing = {slot: name for slot, name in self.tools.items() if name not in available}
        return {
            "url": self.url,
            "available": available,
            "expected": self.tools,
            "missing": missing,
            "ok": not missing,
        }

    # ───────────────────────── сбор ─────────────────────────

    async def fetch_active(self, project: str, statuses: List[str],
                           queues: Optional[List[str]] = None,
                           max_tasks: int = 30) -> List[Task]:
        """Активные задачи со всеми полями для дайджеста.

        Очереди -> задачи с фильтром по статусам -> описание и комментарии.
        Последний шаг параллельный с ограничением: последовательно 30 задач
        растянули бы прогон на минуты.
        """
        async with Client(self._transport()) as client:
            names: Dict[str, str] = {}
            keys: List[str] = list(queues or [])
            if not keys:
                text = await self._text(client, self.tools["queues"], {"project": project})
                for key, name in parse_queues(text):
                    keys.append(key)
                    names[key] = name
                if not keys:
                    logger.warning("list_queues не дал ни одной очереди: %s", text[:200])

            tasks: List[Task] = []
            for queue in keys:
                text = await self._text(client, self.tools["issues"], {
                    "queue": queue,
                    "status": ",".join(statuses),
                    "limit": max_tasks,
                })
                for row in parse_issues(text):
                    # фильтр сервер применяет, но проверяем сами: у review/testing
                    # категория тоже in_progress, надёжен только key статуса
                    if statuses and row["status"] and row["status"] not in statuses:
                        continue
                    tasks.append(Task(
                        key=row["key"],
                        title=row["title"],
                        queue=queue,
                        queue_name=names.get(queue, ""),
                        status=row["status"],
                        status_name=row["status_name"],
                        assignee=row["assignee"],
                        priority=row["priority"],
                    ))

            tasks = sorted(tasks, key=lambda t: t.key)[:max_tasks]
            await self._enrich(client, tasks)
            return tasks

    async def _enrich(self, client: Client, tasks: List[Task]) -> None:
        """Описание и комментарии — одним get_task на задачу. Ошибка по одной
        задаче не роняет дайджест, а лишь обедняет её строку."""
        sem = asyncio.Semaphore(self._concurrency)

        async def one(task: Task) -> None:
            async with sem:
                try:
                    text = await self._text(client, self.tools["issue"],
                                            {"key": task.key, "includeComments": True})
                    parsed = parse_task(text)
                    task.description = parsed["description"]
                    task.comments = parsed["comments"]
                except Exception as e:
                    logger.warning("детали %s не получены: %s", task.key, str(e)[:160])

        await asyncio.gather(*(one(t) for t in tasks))
