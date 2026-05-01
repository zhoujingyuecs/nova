"""Self Loop: 自我调整日志。

nova 可以提出小范围 self patch，并根据结果固化或回滚。第一版先记录
“为什么要调整”和“调整对象”，避免直接无保护地改源码或危险权限。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SelfPatch:
    target: str
    change_type: str
    before: dict
    after: dict
    reason: str
    expected_effect: str = ""
    confidence: float = 0.45
    risk: float = 0.25
    id: str = field(default_factory=lambda: "sp_" + uuid.uuid4().hex[:10])
    created_at: float = field(default_factory=time.time)
    trial_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    status: str = "proposed"  # proposed / trial / accepted / rejected

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "SelfPatch":
        return cls(
            id=d.get("id") or "sp_" + uuid.uuid4().hex[:10],
            target=d.get("target", ""),
            change_type=d.get("change_type", "adjust"),
            before=dict(d.get("before", {})),
            after=dict(d.get("after", {})),
            reason=d.get("reason", ""),
            expected_effect=d.get("expected_effect", ""),
            confidence=float(d.get("confidence", 0.45)),
            risk=float(d.get("risk", 0.25)),
            created_at=float(d.get("created_at", time.time())),
            trial_count=int(d.get("trial_count", 0)),
            success_count=int(d.get("success_count", 0)),
            failure_count=int(d.get("failure_count", 0)),
            status=d.get("status", "proposed"),
        )


class SelfModificationLog:
    def __init__(self, path: str, *, max_patches: int = 120):
        self.path = path
        self.max_patches = max_patches
        self.patches: dict[str, SelfPatch] = {}

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        self.patches = {p.id: p for p in (SelfPatch.from_dict(x) for x in raw.get("patches", []))}

    def save(self) -> None:
        data = {"version": 1, "patches": [p.to_dict() for p in self.patches.values()]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def observe_actions(self, actions, drive_system=None, skillbook=None) -> None:
        # 当同类纠错/失败重复出现，提出自我调整候选。先只记日志，不直接改源码。
        for a in actions:
            typ = getattr(a, "action_type", "")
            target = getattr(a, "target", "")
            reason = getattr(a, "reason", "") or getattr(a, "content", "") or ""
            if typ == "raise_drive" and target in {"competence", "coherence", "caution"}:
                self.propose(
                    target=f"drives.{target}.action_threshold",
                    change_type="threshold_review",
                    before={"reason": "repeated tension"},
                    after={"proposal": "lower threshold slightly during similar contexts"},
                    reason=reason,
                    expected_effect="让 nova 在同类失败后更快进入自我修正/目标推进，而不是继续自由漂流。",
                    risk=0.18,
                    confidence=float(getattr(a, "confidence", 0.5) or 0.5),
                )

    def propose(self, **kwargs) -> SelfPatch:
        if len(self.patches) >= self.max_patches:
            victim = min(self.patches.values(), key=lambda p: (p.status != "accepted", p.confidence, p.created_at))
            self.patches.pop(victim.id, None)
        p = SelfPatch(**kwargs)
        self.patches[p.id] = p
        return p

    def render_prompt_block(self, limit: int = 4) -> str:
        items = sorted(self.patches.values(), key=lambda p: -p.created_at)[:limit]
        if not items:
            return ""
        lines = ["[我正在试验的自我调整 / SelfModification]", "这些不是源码级改造，而是小范围行为/权重/技能策略候选。"]
        for p in items:
            lines.append(f"- {p.target}: {p.change_type}｜{p.reason}｜状态 {p.status}")
        return "\n".join(lines)
