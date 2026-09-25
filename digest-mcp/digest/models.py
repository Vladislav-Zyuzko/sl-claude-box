"""Нормализованные модели. Всё, что приходит из трекера, приводится сюда —
рендер, агрегат и хранение работают только с этими типами и не знают про MCP.
Так смена контракта sl-tracker-mcp задевает ровно один модуль (tracker.py)."""
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


@dataclass
class Comments:
    total: int = 0
    last_author: Optional[str] = None
    last_body: Optional[str] = None


@dataclass
class Task:
    key: str                       # MOBILE-1
    title: str
    queue: str                     # MOBILE
    queue_name: str = ""
    status: str = ""               # in_progress | review | testing
    status_name: str = ""
    assignee: Optional[str] = None  # displayName, не UUID
    priority: Optional[int] = None
    description: Optional[str] = None
    comments: Comments = field(default_factory=Comments)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["comments"] = asdict(self.comments)
        return d

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Task":
        c = d.get("comments") or {}
        return Task(
            key=d.get("key", ""),
            title=d.get("title", ""),
            queue=d.get("queue", ""),
            queue_name=d.get("queue_name", ""),
            status=d.get("status", ""),
            status_name=d.get("status_name", ""),
            assignee=d.get("assignee"),
            priority=d.get("priority"),
            description=d.get("description"),
            comments=Comments(
                total=int(c.get("total") or 0),
                last_author=c.get("last_author"),
                last_body=c.get("last_body"),
            ),
        )


@dataclass
class Snapshot:
    """Один прогон сбора. Пишется в snapshots.jsonl, из него считаются дельты."""
    generated_at: str              # ISO-8601 UTC
    slot: str                      # '08:00' | '20:00' | 'manual'
    kind: str                      # morning | day | evening
    timezone: str
    tasks: List[Task] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None    # прогон мог упасть — это тоже история

    def to_json(self) -> Dict[str, Any]:
        return {
            "generatedAt": self.generated_at,
            "slot": self.slot,
            "kind": self.kind,
            "timezone": self.timezone,
            "totals": self.totals,
            "tasks": [t.to_json() for t in self.tasks],
            "error": self.error,
        }

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Snapshot":
        return Snapshot(
            generated_at=d.get("generatedAt", ""),
            slot=d.get("slot", ""),
            kind=d.get("kind", ""),
            timezone=d.get("timezone", ""),
            tasks=[Task.from_json(t) for t in d.get("tasks") or []],
            totals=d.get("totals") or {},
            error=d.get("error"),
        )
