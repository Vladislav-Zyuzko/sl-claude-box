"""MCP-сервер дайджеста: инструменты + собственный планировщик.

Расписание, хранение и агрегация живут ЗДЕСЬ, внутри MCP-сервера, а не во внешнем
кроне — по заданию (M3/M4) «выполняется по расписанию» и «возвращает агрегированный
результат» должны быть свойствами самого инструмента.

Транспорт: Streamable HTTP (порт наружу из compose не публикуется) и stdio
(NEXUS_DIGEST_TRANSPORT=stdio — для локального запуска агентом).

Замечание по авторизации: RFC §5 просит bearer-токен на HTTP. Готового
shared-token гейта в mcp 2.x нет (там OAuth-провайдер), поэтому границей служит
неопубликованный порт внутри compose-сети. NEXUS_DIGEST_MCP_TOKEN зарезервирован
и пока не проверяется — это единственное расхождение с RFC, см. README.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from . import aggregate, render, scheduler
from .broadcast import Telegram
from .config import Config, load
from .models import Snapshot, Task
from .store import Store
from .tracker import Tracker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("NEXUS_DIGEST_LOG_LEVEL", "INFO"),
)
# httpx печатает URL запроса на INFO, а в URL Telegram Bot API лежит токен бота —
# ровно та утечка, что нашлась в логах бота. Не повторяем её здесь.
for _noisy in ("httpx", "httpx2", "httpcore", "mcp.server"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("digest")

mcp = MCPServer(
    name="nexus-digest",
    instructions=(
        "Дайджест активных задач sweet_limit. Данные берутся из sl-tracker-mcp, "
        "снимки сохраняются на диск, рассылка идёт в Telegram по расписанию."
    ),
)


class Service:
    """Состояние сервиса. Собирается один раз на старте."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.data_dir, cfg.snapshot_keep_days)
        self.tracker = Tracker(cfg.tracker_mcp_url, cfg.tracker_mcp_token,
                               cfg.http_timeout, cfg.fetch_concurrency)
        self.telegram = Telegram(cfg.telegram_token, cfg.http_timeout)
        self.last_error: Optional[str] = None
        self.last_run: Optional[str] = None

    # ───────────────────────── расписание из state ─────────────────────────

    def schedule(self) -> Dict[str, Any]:
        """Окружение задаёт дефолт, schedule_set — переопределение на диске."""
        state = self.store.load_state()
        override = state.get("schedule") or {}
        return {
            "times": override.get("times") or self.cfg.times,
            "tz": override.get("tz") or str(self.cfg.tz),
            "enabled": override.get("enabled", True),
        }

    def _tz(self, name: str):
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("таймзона %s не найдена, беру %s", name, self.cfg.tz)
            return self.cfg.tz

    # ───────────────────────── сбор ─────────────────────────

    async def collect(self, slot: str, kind: str) -> Snapshot:
        """Один прогон сбора: задачи -> агрегат -> снимок на диск.

        Упавший прогон тоже пишется в историю (с error): иначе в `status` не
        видно, что трекер был недоступен, а дельта следующего прогона посчиталась
        бы от старого снимка молча.
        """
        now = datetime.now(timezone.utc)
        prev = self.store.last_snapshot()
        snap = Snapshot(
            generated_at=now.isoformat().replace("+00:00", "Z"),
            slot=slot, kind=kind, timezone=str(self.cfg.tz),
        )
        try:
            tasks: List[Task] = await self.tracker.fetch_active(
                self.cfg.project, self.cfg.statuses, self.cfg.queues, self.cfg.max_tasks
            )
            snap.tasks = tasks
            snap.totals = aggregate.totals(tasks, prev)
            self.last_error = None
        except Exception as e:
            snap.error = f"{type(e).__name__}: {e}"[:400]
            self.last_error = snap.error
            logger.exception("сбор дайджеста не удался")

        self.last_run = snap.generated_at
        self.store.append_snapshot(snap)
        try:
            dropped = self.store.rotate(now)
            if dropped:
                logger.info("ротация истории: удалено %s снимков", dropped)
        except Exception:
            logger.exception("ротация истории не удалась")
        return snap

    def text_of(self, snap: Snapshot) -> str:
        date_key = snap.generated_at[:10]
        return render.render(snap.tasks, snap.kind, snap.slot, date_key, snap.totals)

    def result_of(self, snap: Snapshot) -> Dict[str, Any]:
        """Форма ответа инструментов — зафиксирована RFC §5, чтобы агент мог
        на неё опираться."""
        text_html = self.text_of(snap)
        return {
            "generatedAt": snap.generated_at,
            "slot": snap.slot,
            "kind": snap.kind,
            "timezone": snap.timezone,
            "totals": snap.totals,
            "tasks": [t.to_json() for t in snap.tasks],
            "textHtml": text_html,
            "textPlain": render.to_plain(text_html),
            "error": snap.error,
        }

    # ───────────────────────── рассылка ─────────────────────────

    async def send(self, snap: Snapshot) -> Dict[str, int]:
        if snap.error:
            # Молчание лучше, чем «дайджест сломался» дважды в день всем в личку:
            # ошибка уходит в лог и в status, а не в чат.
            logger.warning("не рассылаю дайджест: прогон с ошибкой (%s)", snap.error)
            return {"sent": 0, "failed": 0, "skipped": len(self.cfg.recipients)}
        return await self.telegram.broadcast(self.cfg.recipients, self.text_of(snap))

    # ───────────────────────── планировщик ─────────────────────────

    async def loop(self) -> None:
        """Ждём слот, шлём, отмечаем. Отметка ставится ДО рассылки по слоту, но
        после успешного сбора — двойная отправка хуже пропуска."""
        logger.info("планировщик: слоты %s (%s), адресатов %s",
                    ",".join(self.cfg.times), self.cfg.tz, len(self.cfg.recipients))
        while True:
            try:
                sched = self.schedule()
                tz = self._tz(sched["tz"])
                times = sched["times"]
                if sched["enabled"]:
                    now = datetime.now(timezone.utc)
                    state = self.store.load_state()
                    due = scheduler.due_slot(now, times, tz, state.get("sent") or {},
                                             self.cfg.late_limit_min)
                    if due:
                        slot_dt, slot = due
                        kind = scheduler.slot_kind(slot)
                        logger.info("слот %s (%s) — собираю дайджест", slot, kind)
                        snap = await self.collect(slot, kind)
                        if not snap.error:
                            state = self.store.load_state()
                            state.setdefault("sent", {})[scheduler.sent_key(slot_dt, slot)] = \
                                datetime.now(timezone.utc).isoformat()
                            self.store.save_state(state)
                            stats = await self.send(snap)
                            logger.info("дайджест отправлен: %s", stats)
                        else:
                            # слот не отмечаем — повторим в пределах порога опоздания
                            logger.warning("слот %s пропущен из-за ошибки сбора", slot)
                    for key in scheduler.stale_slots(now, times, tz,
                                                     (self.store.load_state().get("sent") or {}),
                                                     self.cfg.late_limit_min):
                        logger.info("слот %s уже не догнать — пропускаю", key)
                delay = scheduler.sleep_seconds(datetime.now(timezone.utc), times, tz)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Планировщик обязан выжить: упавший прогон не должен убивать цикл.
                logger.exception("итерация планировщика упала")
                delay = 60.0
            await asyncio.sleep(delay)


SERVICE: Optional[Service] = None


def service() -> Service:
    if SERVICE is None:
        raise RuntimeError("сервис не инициализирован")
    return SERVICE


# ───────────────────────── MCP-инструменты ─────────────────────────


@mcp.tool()
async def digest_preview() -> Dict[str, Any]:
    """Собрать дайджест сейчас и вернуть агрегат с готовым текстом, НЕ отправляя."""
    svc = service()
    slot, kind = _manual_slot(svc)
    snap = await svc.collect(slot, kind)
    return svc.result_of(snap)


@mcp.tool()
async def digest_run(send: bool = False) -> Dict[str, Any]:
    """Собрать снимок, сохранить и при send=true разослать адресатам.

    Ручной прогон не влияет на отметки расписания: плановый слот всё равно уйдёт.
    """
    svc = service()
    slot, kind = _manual_slot(svc)
    snap = await svc.collect(slot, kind)
    out = svc.result_of(snap)
    out["delivery"] = await svc.send(snap) if send else {"sent": 0, "skipped": len(svc.cfg.recipients)}
    return out


@mcp.tool()
async def digest_history(limit: int = 10) -> Dict[str, Any]:
    """Последние снимки: счётчики и дельты между прогонами, без полных списков задач."""
    svc = service()
    snaps = svc.store.read_snapshots(limit=max(1, min(limit, 200)))
    return {"count": len(snaps), "items": aggregate.history(snaps)}


@mcp.tool()
async def schedule_list() -> Dict[str, Any]:
    """Расписание, таймзона, последний и следующий запуск."""
    svc = service()
    sched = svc.schedule()
    tz = svc._tz(sched["tz"])
    nxt = scheduler.next_slot(datetime.now(timezone.utc), sched["times"], tz)
    state = svc.store.load_state()
    return {
        "times": sched["times"],
        "timezone": sched["tz"],
        "enabled": sched["enabled"],
        "nextRun": nxt[0].isoformat() if nxt else None,
        "nextSlot": nxt[1] if nxt else None,
        "lastRun": svc.last_run,
        "sent": state.get("sent") or {},
    }


@mcp.tool()
async def schedule_set(times: Optional[List[str]] = None, tz: Optional[str] = None,
                       enabled: Optional[bool] = None) -> Dict[str, Any]:
    """Поменять расписание и сохранить на диск (переживает перезапуск)."""
    svc = service()
    state = svc.store.load_state()
    sched = dict(state.get("schedule") or {})
    if times is not None:
        bad = [t for t in times if not _valid_time(t)]
        if bad:
            raise ValueError(f"не время в формате HH:MM: {', '.join(bad)}")
        sched["times"] = times
    if tz is not None:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)          # проверка до записи: битую таймзону не сохраняем
        sched["tz"] = tz
    if enabled is not None:
        sched["enabled"] = bool(enabled)
    state["schedule"] = sched
    svc.store.save_state(state)
    return await schedule_list()


@mcp.tool()
async def status() -> Dict[str, Any]:
    """Здоровье: последний прогон, ошибки, доступность sl-tracker-mcp, схема данных."""
    svc = service()
    probe: Dict[str, Any]
    try:
        probe = await svc.tracker.probe()
    except Exception as e:
        probe = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "url": svc.tracker.url}
    from .store import SCHEMA_VERSION
    return {
        "lastRun": svc.last_run,
        "lastError": svc.last_error,
        "recipients": len(svc.cfg.recipients),
        "project": svc.cfg.project,
        "queues": svc.cfg.queues or "все очереди проекта",
        "statuses": svc.cfg.statuses,
        "schemaVersion": SCHEMA_VERSION,
        "snapshots": len(svc.store.read_snapshots()),
        "tracker": probe,
        "schedule": await schedule_list(),
    }


def _manual_slot(svc: Service) -> tuple:
    """Для ручного прогона слот берём ближайший по времени суток — чтобы текст
    был уместным (утром утренний), но отметку расписания не трогаем."""
    now = datetime.now(svc.cfg.tz)
    hour = now.hour
    kind = "morning" if hour < 12 else ("day" if hour < 17 else "evening")
    return "manual", kind


def _valid_time(t: str) -> bool:
    hh, _, mm = str(t).partition(":")
    return hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60


# ───────────────────────── служебные HTTP-маршруты ─────────────────────────
# MCP-протокол — это /mcp; ниже два обычных маршрута для тех, кто MCP не говорит:
# health для healthcheck в compose и /run, которым бот дёргает ручной прогон
# (тащить MCP-клиент в образ бота ради одной кнопки не стоит).


@mcp.custom_route("/health", ["GET"])
async def http_health(request):
    from starlette.responses import JSONResponse
    svc = service()
    return JSONResponse({
        "status": "ok" if not svc.last_error else "degraded",
        "lastRun": svc.last_run,
        "lastError": svc.last_error,
    })


@mcp.custom_route("/run", ["POST"])
async def http_run(request):
    """Ручной прогон с рассылкой. custom_route по документации SDK идёт БЕЗ
    авторизации, а этот маршрут пишет людям в личку — поэтому bearer проверяем
    сами. Пустой NEXUS_DIGEST_MCP_TOKEN закрывает маршрут совсем."""
    from starlette.responses import JSONResponse
    svc = service()
    expected = svc.cfg.mcp_token
    if not expected:
        return JSONResponse({"error": "NEXUS_DIGEST_MCP_TOKEN не задан — маршрут закрыт"}, status_code=503)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or header[7:] != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    send = True
    try:
        body = await request.json()
        send = bool(body.get("send", True))
    except Exception:
        pass       # тело необязательно: по умолчанию собрать и разослать

    slot, kind = _manual_slot(svc)
    snap = await svc.collect(slot, kind)
    delivery = await svc.send(snap) if send else {"sent": 0}
    return JSONResponse({
        "generatedAt": snap.generated_at,
        "active": snap.totals.get("active", len(snap.tasks)),
        "error": snap.error,
        "delivery": delivery,
        "textPlain": render.to_plain(svc.text_of(snap)),
    })


# ───────────────────────── запуск ─────────────────────────


async def _serve(cfg: Config) -> None:
    transport = os.environ.get("NEXUS_DIGEST_TRANSPORT", "streamable-http")
    svc = service()
    tasks = [asyncio.create_task(svc.loop(), name="scheduler")]
    if transport == "stdio":
        tasks.append(asyncio.create_task(mcp.run_stdio_async(), name="mcp-stdio"))
    else:
        host = os.environ.get("NEXUS_DIGEST_HOST", "0.0.0.0")
        port = int(os.environ.get("NEXUS_DIGEST_PORT", "8080"))
        path = os.environ.get("NEXUS_DIGEST_PATH", "/mcp")
        logger.info("MCP: streamable-http на %s:%s%s", host, port, path)
        tasks.append(asyncio.create_task(
            mcp.run_streamable_http_async(host=host, port=port, streamable_http_path=path),
            name="mcp-http",
        ))
    # падение любой половины должно валить контейнер — restart:unless-stopped
    # поднимет его, а тихо работающий наполовину сервис хуже перезапуска
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def main() -> None:
    global SERVICE
    cfg = load()
    SERVICE = Service(cfg)
    logger.info("nexus-digest: проект %s, очереди %s, статусы %s",
                cfg.project, cfg.queues or "все", ",".join(cfg.statuses))
    asyncio.run(_serve(cfg))


if __name__ == "__main__":
    main()
