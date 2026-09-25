"""Источник данных — sl-tracker-mcp (чужой репозиторий, правки только через автора).

Свой REST-клиент к трекеру здесь сознательно НЕ пишется: доступ к задачам идёт
только через инструменты этого MCP-сервера.

Про контракт. Имена инструментов и форма их ответа принадлежат sl-tracker-mcp, а
не нам, поэтому:
  • имена лежат в TOOLS и переопределяются через SL_TRACKER_TOOLS (JSON) —
    подгонка под реальный сервер не требует правки кода;
  • разбор ответа терпимый: поля ищутся по нескольким вероятным именам
    (items/issues/data, key/id, title/summary), объект-или-строка для assignee;
  • probe() зовёт tools/list и говорит, какие имена сервер реально отдаёт —
    первый запуск сам сообщит о расхождении вместо тихой пустой выдачи.

Наружу модуль отдаёт только list[Task] — остальной дайджест про MCP не знает.
"""
import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .models import Comments, Task

logger = logging.getLogger(__name__)

# Ожидаемые имена инструментов sl-tracker-mcp. Переопределяются переменной
# SL_TRACKER_TOOLS, например: {"issues": "tracker_list_issues"}
TOOLS: Dict[str, str] = {
    "queues": "list_queues",       # очереди проекта
    "issues": "list_issues",       # задачи очереди с фильтром по статусам
    "issue": "get_issue",          # одна задача (нужна из-за отсутствия description в списке)
    "comments": "list_comments",   # комментарии задачи (нужен total и последний)
}


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


def _first(d: Dict[str, Any], *names: str, default: Any = None) -> Any:
    """Первое непустое из вероятных имён поля."""
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """Список записей из ответа инструмента, в какой бы обёртке он ни пришёл."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "issues", "queues", "comments", "results", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        # одиночный объект — тоже валидный ответ (get_issue)
        return [payload]
    return []


def _name_of(value: Any) -> Optional[str]:
    """assignee/author приходит объектом {displayName,...} либо уже строкой."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return _first(value, "displayName", "display_name", "name", "login", "email")
    return None


def _status_pair(value: Any) -> tuple:
    """(key, name) статуса. В review/testing category == in_progress, поэтому
    фильтровать и показывать надо именно key, а не категорию."""
    if isinstance(value, dict):
        return (_first(value, "key", "id", default="") or "",
                _first(value, "name", "title", default="") or "")
    if isinstance(value, str):
        return (value, "")
    return ("", "")


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

    # ───────────────────────── низкий уровень ─────────────────────────

    async def _payload(self, client: Client, tool: str, args: Dict[str, Any]) -> Any:
        """Вызов инструмента + вытаскивание полезной нагрузки.

        Предпочитаем structuredContent (машинная форма), иначе разбираем текстовый
        блок как JSON — MCP-серверы часто отдают JSON строкой.
        """
        result = await client.call_tool(tool, args)
        if getattr(result, "isError", False):
            raise RuntimeError(f"{tool}: сервер вернул ошибку: {_text_of(result)[:300]}")

        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured

        text = _text_of(result)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"{tool}: ответ не JSON: {text[:200]}")

    async def probe(self) -> Dict[str, Any]:
        """Что сервер реально умеет. Зовётся на старте и в MCP-инструменте status:
        если имена не совпали, это видно сразу и с конкретикой."""
        async with Client(self._transport()) as client:
            listed = await client.list_tools()
            available = sorted(t.name for t in listed.tools)
        expected = self.tools
        missing = {slot: name for slot, name in expected.items() if name not in available}
        return {
            "url": self.url,
            "available": available,
            "expected": expected,
            "missing": missing,
            "ok": not missing,
        }

    # ───────────────────────── сбор ─────────────────────────

    async def fetch_active(self, project: str, statuses: List[str],
                           queues: Optional[List[str]] = None,
                           max_tasks: int = 30) -> List[Task]:
        """Активные задачи со всеми полями для дайджеста.

        Порядок: очереди проекта -> задачи с фильтром по статусам -> описание и
        комментарии по каждой задаче. Последние два шага — параллельно с
        ограничением, иначе 30 задач превращаются в 60 последовательных вызовов
        и прогон растягивается на минуты.
        """
        async with Client(self._transport()) as client:
            keys = list(queues or [])
            names: Dict[str, str] = {}
            if not keys:
                payload = await self._payload(client, self.tools["queues"], {"project": project})
                for row in _rows(payload):
                    key = _first(row, "key", "slug", "id")
                    if key:
                        keys.append(str(key))
                        names[str(key)] = _first(row, "name", "title", default="") or ""

            tasks: List[Task] = []
            for queue in keys:
                payload = await self._payload(client, self.tools["issues"], {
                    "queue": queue,
                    "status": ",".join(statuses),
                    "limit": max_tasks,
                })
                for row in _rows(payload):
                    key = _first(row, "key", "id")
                    if not key:
                        continue
                    status_key, status_name = _status_pair(row.get("status"))
                    # сервер мог не применить фильтр — отсекаем сами, по key статуса
                    if statuses and status_key and status_key not in statuses:
                        continue
                    queue_row = row.get("queue") if isinstance(row.get("queue"), dict) else None
                    qkey = str(_first(queue_row or {}, "key", "id", default=queue) or queue)
                    tasks.append(Task(
                        key=str(key),
                        title=str(_first(row, "title", "summary", "name", default="") or ""),
                        queue=qkey,
                        queue_name=names.get(qkey) or (_first(queue_row or {}, "name", default="") or ""),
                        status=status_key,
                        status_name=status_name,
                        assignee=_name_of(row.get("assignee")),
                        priority=_as_int(row.get("priority")),
                        description=_first(row, "description", "body"),
                    ))

            tasks = sorted(tasks, key=lambda t: t.key)[:max_tasks]
            await self._enrich(client, tasks)
            return tasks

    async def _enrich(self, client: Client, tasks: List[Task]) -> None:
        """Описание (в списке его нет) и признак обсуждения. Ошибка по одной
        задаче не должна ронять весь дайджест — она лишь обедняет её строку."""
        sem = asyncio.Semaphore(self._concurrency)

        async def one(task: Task) -> None:
            async with sem:
                if not task.description:
                    try:
                        payload = await self._payload(client, self.tools["issue"], {"key": task.key})
                        rows = _rows(payload)
                        if rows:
                            row = rows[0]
                            task.description = _first(row, "description", "body")
                            if not task.status_name:
                                _, task.status_name = _status_pair(row.get("status"))
                            if not task.assignee:
                                task.assignee = _name_of(row.get("assignee"))
                    except Exception as e:
                        logger.warning("описание %s не получено: %s", task.key, str(e)[:160])
                try:
                    payload = await self._payload(client, self.tools["comments"],
                                                  {"key": task.key, "limit": 1})
                    task.comments = _comments_of(payload)
                except Exception as e:
                    logger.warning("комментарии %s не получены: %s", task.key, str(e)[:160])

        await asyncio.gather(*(one(t) for t in tasks))


def _comments_of(payload: Any) -> Comments:
    total = 0
    if isinstance(payload, dict):
        total = _as_int(payload.get("total")) or 0
    rows = _rows(payload)
    # total может не прийти — тогда считаем по отданным записям
    if not total:
        total = len(rows)
    if not rows:
        return Comments(total=total)
    last = rows[-1]
    return Comments(
        total=total,
        last_author=_name_of(last.get("author")),
        last_body=_first(last, "body", "text", "content"),
    )


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_of(result: Any) -> str:
    """Склейка текстовых блоков ответа инструмента."""
    out = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            out.append(text)
    return "\n".join(out)
