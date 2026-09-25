"""Хранение: state.json (расписание + отметки отправки) и snapshots.jsonl (история).

Правила, заданные RFC: запись атомарная, повреждённый файл не роняет сервис,
ротация по возрасту. Каталог — том, поэтому данные переживают пересборку.
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import Snapshot

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class Store:
    def __init__(self, data_dir: str, keep_days: int = 90):
        self.dir = data_dir
        self.keep_days = keep_days
        self.state_path = os.path.join(data_dir, "state.json")
        self.snapshots_path = os.path.join(data_dir, "snapshots.jsonl")
        os.makedirs(data_dir, exist_ok=True)

    # ───────────────────────── state ─────────────────────────

    def load_state(self) -> Dict[str, Any]:
        """Битый или отсутствующий state не должен ронять сервис: отдаём дефолт
        и пишем в лог — расписание всё равно есть в окружении."""
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                raise ValueError("state.json: ожидался объект")
        except FileNotFoundError:
            state = {}
        except Exception:
            logger.exception("state.json не читается — начинаю с пустого")
            state = {}
        state.setdefault("schema", SCHEMA_VERSION)
        state.setdefault("sent", {})       # "2026-09-25:08:00" -> ISO отправки
        state.setdefault("schedule", {})   # переопределения через schedule_set
        state.setdefault("last_error", None)
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        state["schema"] = SCHEMA_VERSION
        self._atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    # ─────────────────────── snapshots ───────────────────────

    def append_snapshot(self, snap: Snapshot) -> None:
        line = json.dumps(snap.to_json(), ensure_ascii=False)
        # append короткой строки в JSONL атомарен на практике (O_APPEND, одна запись
        # меньше PIPE_BUF), а полный перезапись файла ради одной строки — дороже.
        try:
            with open(self.snapshots_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            logger.exception("не смог записать снимок")

    def read_snapshots(self, limit: Optional[int] = None) -> List[Snapshot]:
        """Последние снимки, новые в конце. Битые строки пропускаем: одна
        обрезанная запись не должна закрывать доступ ко всей истории."""
        try:
            with open(self.snapshots_path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return []
        except Exception:
            logger.exception("snapshots.jsonl не читается")
            return []

        if limit:
            lines = lines[-limit:]
        out: List[Snapshot] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(Snapshot.from_json(json.loads(raw)))
            except Exception:
                logger.warning("пропускаю битую строку истории")
        return out

    def last_snapshot(self, with_tasks_only: bool = True) -> Optional[Snapshot]:
        """Предыдущий успешный снимок — база для дельт. Упавшие прогоны (error)
        не считаем: иначе дельта показала бы «все задачи появились»."""
        for snap in reversed(self.read_snapshots()):
            if with_tasks_only and snap.error:
                continue
            return snap
        return None

    def rotate(self, now: Optional[datetime] = None) -> int:
        """Выкидывает снимки старше keep_days. Возвращает число удалённых."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.keep_days)
        snaps = self.read_snapshots()
        keep = []
        for snap in snaps:
            try:
                ts = datetime.fromisoformat(snap.generated_at.replace("Z", "+00:00"))
            except Exception:
                keep.append(snap)   # без даты — не нам решать, оставляем
                continue
            if ts >= cutoff:
                keep.append(snap)
        dropped = len(snaps) - len(keep)
        if dropped > 0:
            body = "\n".join(json.dumps(s.to_json(), ensure_ascii=False) for s in keep)
            self._atomic_write(self.snapshots_path, body + ("\n" if body else ""))
        return dropped

    # ───────────────────────── низкий уровень ─────────────────────────

    def _atomic_write(self, path: str, body: str) -> None:
        """Временный файл рядом + rename: при падении посреди записи остаётся
        целый старый файл, а не половина нового."""
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
