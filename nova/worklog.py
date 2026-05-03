"""WorkLog：nova 持续运行时留下的可验证轨迹。

没有 WorkLog，所谓"自主运行"很容易退化成临场编故事。
WorkLog 把每个 tick 的思考、工具动作、睡眠、阻塞和决定都写成 JSONL。
用户回来问"干得怎么样"时，runtime 从这里汇报，而不是靠模糊回忆现编。

它也是 nova 自我评价的事实底料：runtime 在反思时会把最近一段
worklog 喂给她，让她基于"我刚才做过的事"判断进展，而不是凭感觉。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class WorkEvent:
    kind: str
    summary: str
    detail: str = ""
    agenda_id: str = ""
    artifacts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkEvent":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in allowed}
        return cls(**kwargs)


class WorkLog:
    def __init__(self, path: str, *, max_detail_chars: int = 4000):
        self.path = path
        self.max_detail_chars = max_detail_chars
        self._lock = threading.RLock()

    def append(
        self,
        kind: str,
        summary: str,
        *,
        detail: str = "",
        agenda_id: str = "",
        artifacts: Optional[list[str]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> WorkEvent:
        summary = _clip((summary or "").strip(), 500)
        detail = _clip((detail or "").strip(), self.max_detail_chars)
        ev = WorkEvent(
            kind=kind,
            summary=summary or f"{kind} step",
            detail=detail,
            agenda_id=agenda_id or "",
            artifacts=list(artifacts or []),
            meta=dict(meta or {}),
        )
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        return ev

    def recent(self, *, limit: int = 20,
               since_seconds: Optional[float] = None) -> list[WorkEvent]:
        if not os.path.exists(self.path):
            return []
        cutoff = time.time() - since_seconds if since_seconds else None
        events: list[WorkEvent] = []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = WorkEvent.from_dict(json.loads(line))
                    except Exception:
                        continue
                    if cutoff is None or ev.ts >= cutoff:
                        events.append(ev)
        return events[-limit:]

    def summary_text(self, *, limit: int = 12,
                     since_seconds: Optional[float] = None) -> str:
        events = self.recent(limit=limit, since_seconds=since_seconds)
        if not events:
            return "（还没有工作日志。）"
        rows: list[str] = []
        for ev in events:
            clock = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev.ts))
            agenda = f" agenda={ev.agenda_id}" if ev.agenda_id else ""
            rows.append(f"- {clock} [{ev.kind}{agenda}] {ev.summary}")
        return "\n".join(rows)

    def compact(self, *, keep_last: int = 500) -> int:
        """保留最后 keep_last 条。返回删除数量。"""
        if not os.path.exists(self.path):
            return 0
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            if len(lines) <= keep_last:
                return 0
            removed = len(lines) - keep_last
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_last:])
            os.replace(tmp, self.path)
            return removed


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
