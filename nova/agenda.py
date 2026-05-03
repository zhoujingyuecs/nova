"""Agenda：nova 的主线任务栈。

agenda 是 nova 当下意识主线的外骨骼：
  - 用户交代的长期目标会进入 agenda；
  - nova 自己发现的问题也可以进入 agenda；
  - ContinuousRuntime 在无人打断时会围绕 active agenda 继续工作；
  - WorkLog 记录每一次推进，让人回来时能问"你刚才干得怎么样"。

这个模块故意不依赖 LLM，也不依赖缝隙场。它是稳定骨架。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional


STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_SLEEPING = "sleeping"
STATUS_ABANDONED = "abandoned"

VALID_STATUSES = {
    STATUS_ACTIVE, STATUS_BLOCKED, STATUS_DONE,
    STATUS_SLEEPING, STATUS_ABANDONED,
}


@dataclass
class AgendaItem:
    """一条主线。

    source: user / self / commission / system / memory
    drive:  creation / competence / curiosity / continuity / relation / caution / coherence
    evidence: 工具结果、文件路径、工作摘要、失败信息等证据。短句。
    """
    title: str
    description: str = ""
    source: str = "self"
    status: str = STATUS_ACTIVE
    priority: float = 0.5
    drive: str = "continuity"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_worked_at: float = 0.0
    evidence: list[str] = field(default_factory=list)
    next_action: str = ""
    attempts: int = 0
    failures: int = 0
    tags: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = time.time()

    def worked(self) -> None:
        now = time.time()
        self.last_worked_at = now
        self.updated_at = now
        self.attempts += 1

    def add_evidence(self, text: str, *, max_items: int = 40,
                     max_chars: int = 500) -> None:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        self.evidence.append(text)
        if len(self.evidence) > max_items:
            self.evidence = self.evidence[-max_items:]
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgendaItem":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in allowed}
        item = cls(**kwargs)
        if item.status not in VALID_STATUSES:
            item.status = STATUS_ACTIVE
        return item


class Agenda:
    """持久化的主线任务栈。"""

    def __init__(self, path: str, *, max_items: int = 200):
        self.path = path
        self.max_items = max_items
        self._items: dict[str, AgendaItem] = {}
        self._lock = threading.RLock()

    # ---------- persistence ----------
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(f"⚠️ agenda 损坏，从空开始：{e}")
                self._items = {}
                return
            items = raw.get("items", raw if isinstance(raw, list) else [])
            self._items = {}
            for obj in items:
                try:
                    item = AgendaItem.from_dict(obj)
                    self._items[item.id] = item
                except Exception:
                    continue

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            data = {
                "version": 1,
                "saved_at": time.time(),
                "items": [item.to_dict() for item in self._sorted_items()],
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- queries ----------
    def all(self) -> list[AgendaItem]:
        with self._lock:
            return list(self._sorted_items())

    def active(self) -> list[AgendaItem]:
        with self._lock:
            return [i for i in self._sorted_items() if i.status == STATUS_ACTIVE]

    def blocked(self) -> list[AgendaItem]:
        with self._lock:
            return [i for i in self._sorted_items() if i.status == STATUS_BLOCKED]

    def done(self) -> list[AgendaItem]:
        with self._lock:
            return [i for i in self._sorted_items() if i.status == STATUS_DONE]

    def get(self, item_id: str) -> Optional[AgendaItem]:
        with self._lock:
            return self._items.get(item_id)

    def current(self) -> Optional[AgendaItem]:
        """取下一条最值得继续的 active agenda。

        排序故意兼顾优先级与"最近没碰过"，避免一个主线永远霸占水流。"""
        with self._lock:
            active = self.active()
            if not active:
                return None
            now = time.time()

            def score(item: AgendaItem) -> tuple[float, float]:
                age_bonus = min((now - (item.last_worked_at or item.created_at)) / 3600.0, 6.0) * 0.02
                failure_penalty = min(item.failures, 5) * 0.05
                return (item.priority + age_bonus - failure_penalty, -item.created_at)

            return max(active, key=score)

    # ---------- mutations ----------
    def add(
        self,
        title: str,
        description: str = "",
        *,
        source: str = "self",
        priority: float = 0.5,
        drive: str = "continuity",
        next_action: str = "",
        tags: Optional[Iterable[str]] = None,
        save: bool = True,
    ) -> AgendaItem:
        with self._lock:
            title = (title or "").strip()
            if not title:
                raise ValueError("Agenda title cannot be empty")
            item = AgendaItem(
                title=title,
                description=(description or "").strip(),
                source=source,
                priority=max(0.0, min(float(priority), 1.0)),
                drive=drive or "continuity",
                next_action=(next_action or "").strip(),
                tags=list(tags or []),
            )
            self._items[item.id] = item
            self._trim_if_needed()
            if save:
                self.save()
            return item

    def add_if_absent(
        self,
        title: str,
        description: str = "",
        *,
        source: str = "self",
        priority: float = 0.5,
        drive: str = "continuity",
        next_action: str = "",
        tags: Optional[Iterable[str]] = None,
        similarity_key: Optional[str] = None,
    ) -> AgendaItem:
        key = _norm_key(similarity_key or title)
        with self._lock:
            for item in self._items.values():
                if item.status in {STATUS_ACTIVE, STATUS_BLOCKED} and _norm_key(item.title) == key:
                    if description and description not in item.description:
                        item.description = (item.description + "\n" + description).strip()
                    item.priority = max(item.priority, priority)
                    item.touch()
                    self.save()
                    return item
        return self.add(title, description, source=source, priority=priority,
                        drive=drive, next_action=next_action, tags=tags)

    def update(
        self,
        item_id: str,
        *,
        status: Optional[str] = None,
        next_action: Optional[str] = None,
        priority: Optional[float] = None,
        evidence: Optional[str] = None,
        failure: bool = False,
        save: bool = True,
    ) -> Optional[AgendaItem]:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if status is not None:
                if status not in VALID_STATUSES:
                    raise ValueError(f"Unknown agenda status: {status}")
                item.status = status
            if next_action is not None:
                item.next_action = next_action.strip()
            if priority is not None:
                item.priority = max(0.0, min(float(priority), 1.0))
            if evidence:
                item.add_evidence(evidence)
            if failure:
                item.failures += 1
            item.touch()
            if save:
                self.save()
            return item

    def mark_done(self, item_id: str, evidence: str = "") -> Optional[AgendaItem]:
        return self.update(item_id, status=STATUS_DONE, evidence=evidence)

    def mark_blocked(self, item_id: str, reason: str = "") -> Optional[AgendaItem]:
        return self.update(item_id, status=STATUS_BLOCKED, evidence=reason, failure=True)

    def record_progress(self, item_id: str, summary: str, *,
                        next_action: str = "") -> Optional[AgendaItem]:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            item.worked()
            if summary:
                item.add_evidence(summary)
            if next_action:
                item.next_action = next_action.strip()
            self.save()
            return item

    def summary_text(self, *, limit: int = 8) -> str:
        with self._lock:
            rows = []
            for item in self._sorted_items()[:limit]:
                rows.append(
                    f"- [{item.status}] {item.title} "
                    f"(id={item.id}, priority={item.priority:.2f}, drive={item.drive})"
                )
                if item.next_action:
                    rows.append(f"  next: {item.next_action}")
            return "\n".join(rows) if rows else "（agenda 为空。）"

    # ---------- internals ----------
    def _sorted_items(self) -> list[AgendaItem]:
        return sorted(
            self._items.values(),
            key=lambda i: (i.status != STATUS_ACTIVE, -i.priority, -(i.updated_at or 0.0)),
        )

    def _trim_if_needed(self) -> None:
        if len(self._items) <= self.max_items:
            return
        removable = sorted(
            [i for i in self._items.values() if i.status in {STATUS_DONE, STATUS_ABANDONED}],
            key=lambda i: i.updated_at,
        )
        while len(self._items) > self.max_items and removable:
            victim = removable.pop(0)
            self._items.pop(victim.id, None)


def _norm_key(text: str) -> str:
    return "".join(ch.lower() for ch in (text or "") if not ch.isspace()).strip("。.!！?？")
