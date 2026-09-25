"""Конфигурация дайджеста. Всё из окружения, чтобы менять без пересборки образа."""
import os
from dataclasses import dataclass, field
from typing import List, Optional
from zoneinfo import ZoneInfo


def _ints(raw: str) -> List[int]:
    return [int(x.strip()) for x in (raw or "").split(",") if x.strip()]


def _strs(raw: str) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    # ─── адресаты и отправка ───
    telegram_token: str
    recipients: List[int]

    # ─── источник данных: MCP-сервер трекера ───
    # Свой REST-клиент к трекеру не пишем: у трекера есть sl-tracker-mcp (отдельный
    # репозиторий, чужой). Ходим только через него, правки контракта — через автора.
    tracker_mcp_url: str
    tracker_mcp_token: str
    project: str
    # Пусто = все очереди проекта. Сужение нужно редко, но пусть будет.
    queues: List[str]
    statuses: List[str]

    # ─── расписание ───
    times: List[str]
    tz: ZoneInfo
    late_limit_min: int

    # ─── прочее ───
    data_dir: str
    max_tasks: int
    snapshot_keep_days: int
    mcp_token: str
    http_timeout: float
    fetch_concurrency: int

    @property
    def enabled(self) -> bool:
        return bool(self.recipients and self.telegram_token)


def load() -> Config:
    tz_name = os.environ.get("NEXUS_DIGEST_TZ", "Asia/Omsk")
    cfg = Config(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        recipients=_ints(os.environ.get("ALLOWED_USER_IDS", "")),
        tracker_mcp_url=os.environ.get("SL_TRACKER_MCP_URL", "").strip().rstrip("/"),
        tracker_mcp_token=os.environ.get("SL_TRACKER_MCP_TOKEN", "").strip(),
        project=os.environ.get("SL_TRACKER_PROJECT", "sweet-limit").strip(),
        queues=_strs(os.environ.get("NEXUS_DIGEST_QUEUES", "")),
        statuses=_strs(os.environ.get("NEXUS_DIGEST_STATUSES", "in_progress,review,testing")),
        times=_strs(os.environ.get("NEXUS_DIGEST_TIMES", "08:00,20:00")),
        tz=ZoneInfo(tz_name),
        late_limit_min=int(os.environ.get("NEXUS_DIGEST_LATE_LIMIT_MIN", "60")),
        data_dir=os.environ.get("NEXUS_DIGEST_DATA_DIR", "/data"),
        max_tasks=int(os.environ.get("NEXUS_DIGEST_MAX_TASKS", "30")),
        snapshot_keep_days=int(os.environ.get("NEXUS_DIGEST_KEEP_DAYS", "90")),
        mcp_token=os.environ.get("NEXUS_DIGEST_MCP_TOKEN", "").strip(),
        http_timeout=float(os.environ.get("NEXUS_DIGEST_HTTP_TIMEOUT", "20")),
        fetch_concurrency=int(os.environ.get("NEXUS_DIGEST_CONCURRENCY", "5")),
    )
    # Тихо неработающий дайджест хуже падения: имена переменных — в текст ошибки.
    missing = []
    if not cfg.telegram_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg.recipients:
        missing.append("ALLOWED_USER_IDS")
    if not cfg.tracker_mcp_url:
        missing.append("SL_TRACKER_MCP_URL")
    if missing:
        raise ValueError(
            "не заданы обязательные переменные: " + ", ".join(missing)
            + ". SL_TRACKER_MCP_URL — адрес sl-tracker-mcp, токен трекера — SL_TRACKER_MCP_TOKEN."
        )
    for t in cfg.times:
        hh, _, mm = t.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"NEXUS_DIGEST_TIMES: {t!r} — не время в формате HH:MM")
    return cfg
