"""Агрегат: счётчики и дельты к предыдущему снимку.

Требование M4 задания — инструмент возвращает агрегированный результат, а не
сырой ответ источника. Здесь же считается всё, что показывает `digest_history`.
"""
from typing import Any, Dict, List, Optional

from .models import Snapshot, Task


def totals(tasks: List[Task], prev: Optional[Snapshot] = None) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    by_queue: Dict[str, int] = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
        by_queue[t.queue] = by_queue.get(t.queue, 0) + 1

    out: Dict[str, Any] = {
        "active": len(tasks),
        "byStatus": by_status,
        "byQueue": by_queue,
        "withComments": sum(1 for t in tasks if t.comments.total > 0),
        "unassigned": sum(1 for t in tasks if not t.assignee),
    }
    out["deltaSincePrev"] = delta(tasks, prev)
    return out


def delta(tasks: List[Task], prev: Optional[Snapshot]) -> Dict[str, Any]:
    """Что изменилось с прошлого снимка.

    Если предыдущего снимка нет, дельта пустая, а не «появились все»: первый
    прогон не должен выглядеть как всплеск активности.
    """
    if prev is None:
        return {"appeared": 0, "statusChanged": 0, "closed": 0,
                "appearedKeys": [], "statusChangedKeys": [], "closedKeys": []}

    was = {t.key: t.status for t in prev.tasks}
    now = {t.key: t.status for t in tasks}

    appeared = [k for k in now if k not in was]
    closed = [k for k in was if k not in now]   # ушла из активных статусов
    changed = [k for k in now if k in was and was[k] != now[k]]

    return {
        "appeared": len(appeared),
        "statusChanged": len(changed),
        "closed": len(closed),
        "appearedKeys": sorted(appeared),
        "statusChangedKeys": sorted(changed),
        "closedKeys": sorted(closed),
    }


def history(snapshots: List[Snapshot]) -> List[Dict[str, Any]]:
    """Компактная история для `digest_history`: без полных списков задач,
    только время, слот, счётчики и дельта."""
    out = []
    for snap in snapshots:
        out.append({
            "generatedAt": snap.generated_at,
            "slot": snap.slot,
            "kind": snap.kind,
            "active": snap.totals.get("active", len(snap.tasks)),
            "byStatus": snap.totals.get("byStatus", {}),
            "byQueue": snap.totals.get("byQueue", {}),
            "delta": snap.totals.get("deltaSincePrev", {}),
            "error": snap.error,
        })
    return out
