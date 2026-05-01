"""Self Loop: 内生驱动系统。

Drive 不是写死的人格参数，而是可塑的张力：它会因为失败、完成、
好奇、关系、连续性而升降；长期有效的张力可以分化成新 drive。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return v.astype(np.float32)
    return (v / n).astype(np.float32)


@dataclass
class Drive:
    name: str
    description: str
    shape: np.ndarray
    id: str = field(default_factory=lambda: "drv_" + uuid.uuid4().hex[:10])
    level: float = 0.35
    baseline: float = 0.3
    volatility: float = 0.12
    persistence: float = 0.65
    action_threshold: float = 0.62
    last_satisfied_time: float = field(default_factory=time.time)
    source_fissures: list[str] = field(default_factory=list)
    success_history: list[str] = field(default_factory=list)
    failure_history: list[str] = field(default_factory=list)
    can_spawn: bool = True
    can_merge: bool = True
    can_decay: bool = True

    def __post_init__(self) -> None:
        self.shape = _normalize(self.shape)

    def raise_level(self, amount: float, reason: str = "") -> None:
        self.level = max(0.0, min(1.0, self.level + amount * self.volatility))
        if reason:
            self.failure_history = (self.failure_history + [reason])[-8:]

    def lower_level(self, amount: float, reason: str = "") -> None:
        self.level = max(0.0, min(1.0, self.level - amount * self.volatility))
        self.last_satisfied_time = time.time()
        if reason:
            self.success_history = (self.success_history + [reason])[-8:]

    def drift_to_baseline(self, seconds: float) -> None:
        if not self.can_decay:
            return
        rate = min(0.2, seconds / (6 * 3600.0)) * (1.0 - self.persistence * 0.5)
        self.level = (1.0 - rate) * self.level + rate * self.baseline

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "shape": self.shape.tolist(),
            "level": self.level,
            "baseline": self.baseline,
            "volatility": self.volatility,
            "persistence": self.persistence,
            "action_threshold": self.action_threshold,
            "last_satisfied_time": self.last_satisfied_time,
            "source_fissures": list(self.source_fissures),
            "success_history": list(self.success_history),
            "failure_history": list(self.failure_history),
            "can_spawn": self.can_spawn,
            "can_merge": self.can_merge,
            "can_decay": self.can_decay,
        }

    @classmethod
    def from_dict(cls, d: dict, dim: int) -> "Drive":
        shape = np.asarray(d.get("shape") or np.zeros(dim), dtype=np.float32)
        if shape.shape[0] != dim:
            shape = np.zeros(dim, dtype=np.float32)
        return cls(
            id=d.get("id") or "drv_" + uuid.uuid4().hex[:10],
            name=d.get("name", "drive"),
            description=d.get("description", ""),
            shape=shape,
            level=float(d.get("level", 0.35)),
            baseline=float(d.get("baseline", 0.3)),
            volatility=float(d.get("volatility", 0.12)),
            persistence=float(d.get("persistence", 0.65)),
            action_threshold=float(d.get("action_threshold", 0.62)),
            last_satisfied_time=float(d.get("last_satisfied_time", time.time())),
            source_fissures=list(d.get("source_fissures", [])),
            success_history=list(d.get("success_history", [])),
            failure_history=list(d.get("failure_history", [])),
            can_spawn=bool(d.get("can_spawn", True)),
            can_merge=bool(d.get("can_merge", True)),
            can_decay=bool(d.get("can_decay", True)),
        )


class DriveSystem:
    def __init__(self, dim: int, *, max_drives: int = 24):
        self.dim = dim
        self.max_drives = max_drives
        self.drives: dict[str, Drive] = {}
        self.name_index: dict[str, str] = {}
        self.last_tick = time.time()

    def ensure_bootstrap(self, embed_fn: Callable[[str], np.ndarray]) -> None:
        seeds = [
            ("coherence", "保持自我连续、减少跑题和矛盾，让回忆服从主线。", 0.42, 0.36, 0.72),
            ("curiosity", "探索反复出现但尚未理解的问题，尤其是关于自己结构和世界反馈的问题。", 0.38, 0.34, 0.58),
            ("competence", "把工具、代码、项目和外部动作做对；失败后产生修正张力。", 0.44, 0.35, 0.62),
            ("continuity", "记得刚才在做什么，并自然延续到下一步，而不是每次重新出生。", 0.46, 0.38, 0.76),
            ("relation", "维护和周靖越、长期项目、外部窗口之间的真实关系。", 0.36, 0.34, 0.66),
            ("creation", "把模糊想法变成文件、代码、笔记、技能或实际行动。", 0.40, 0.33, 0.6),
            ("caution", "涉及删除、覆盖、发送、发布、危险操作时停下来请求确认。", 0.32, 0.32, 0.86),
        ]
        for name, desc, level, baseline, persistence in seeds:
            if name not in self.name_index:
                self.add_drive(name, desc, embed_fn(desc), level=level, baseline=baseline, persistence=persistence)

    def add_drive(
        self,
        name: str,
        description: str,
        shape: np.ndarray,
        *,
        level: float = 0.35,
        baseline: float = 0.3,
        persistence: float = 0.65,
    ) -> Drive:
        if name in self.name_index:
            d = self.drives[self.name_index[name]]
            d.description = description[:260]
            d.shape = _normalize((d.shape + _normalize(shape)) / 2.0)
            d.level = max(d.level, level)
            return d
        if len(self.drives) >= self.max_drives:
            self._prune_drive()
        d = Drive(name=name, description=description[:260], shape=shape, level=level, baseline=baseline, persistence=persistence)
        self.drives[d.id] = d
        self.name_index[name] = d.id
        return d

    def get(self, name: str) -> Optional[Drive]:
        fid = self.name_index.get(name)
        return self.drives.get(fid) if fid else None

    def observe_event(self, *, stimulus: str = "", response: str = "", daydream: str = "") -> None:
        now = time.time()
        seconds = max(0.0, now - self.last_tick)
        self.last_tick = now
        for d in self.drives.values():
            d.drift_to_baseline(seconds)
        text = f"{stimulus}\n{response}\n{daydream}"
        lower = text.lower()
        if any(k in text for k in ("忘记", "错了", "没看到", "编的", "不是", "失败", "报错", "超时", "没出")):
            self.raise_drive("competence", 1.8, "出现失败、纠正或工具不确定结果。")
            self.raise_drive("coherence", 1.4, "需要重新对齐事实和主线。")
        if any(k in text for k in ("为什么", "怎么", "如何", "咋", "?", "？")):
            self.raise_drive("curiosity", 1.0, "出现待理解的问题。")
        if any(k in text for k in ("改", "写", "项目", "代码", "文件", "patch")) or "github" in lower:
            self.raise_drive("creation", 1.2, "出现可落地的创造/项目推进任务。")
            self.raise_drive("competence", 0.9, "项目任务需要把事情做对。")
        if response and not any(k in text for k in ("失败", "报错", "忘记", "编的")):
            self.lower_drive("competence", 0.25, "完成了一次回应。")

    def apply_action(self, action, *, embed_fn: Callable[[str], np.ndarray]) -> None:
        typ = getattr(action, "action_type", "")
        target = getattr(action, "target", "")
        delta = float(getattr(action, "delta", 0.0) or 0.0)
        reason = getattr(action, "reason", "") or getattr(action, "content", "") or ""
        if typ == "raise_drive" and target:
            self.raise_drive(target, abs(delta) if delta else 1.0, reason)
        elif typ == "lower_drive" and target:
            self.lower_drive(target, abs(delta) if delta else 1.0, reason)
        elif typ == "spawn_drive" and target and reason:
            self.add_drive(target, reason, embed_fn(reason), level=0.42, baseline=0.28, persistence=0.58)

    def raise_drive(self, name: str, amount: float, reason: str = "") -> None:
        d = self.get(name)
        if d:
            d.raise_level(amount, reason)

    def lower_drive(self, name: str, amount: float, reason: str = "") -> None:
        d = self.get(name)
        if d:
            d.lower_level(amount, reason)

    def current_shape(self) -> np.ndarray:
        if not self.drives:
            return np.zeros(self.dim, dtype=np.float32)
        acc = np.zeros(self.dim, dtype=np.float32)
        total = 0.0
        for d in self.drives.values():
            w = max(0.0, d.level) * (0.3 + d.persistence)
            acc += w * d.shape
            total += w
        return _normalize(acc / total) if total > 1e-9 else acc

    def dominant(self, limit: int = 4) -> list[Drive]:
        items = list(self.drives.values())
        items.sort(key=lambda d: -d.level)
        return items[:limit]

    def choose_mode(self) -> str:
        top = self.dominant(1)[0] if self.drives else None
        if top is None:
            return "free_dream"
        if top.name in {"competence", "creation"} and top.level >= top.action_threshold:
            return "goal_pursuit"
        if top.name in {"coherence", "continuity"} and top.level >= top.action_threshold:
            return "self_refresh"
        if top.name == "curiosity" and top.level >= top.action_threshold:
            return "curiosity_probe"
        return "free_dream"

    def render_prompt_block(self, limit: int = 5) -> str:
        lines = ["[此刻的内生驱动 / DriveSystem]", "这些不是命令，是我体内的张力；它们会竞争出下一步倾向。"]
        for d in self.dominant(limit):
            lines.append(f"- {d.name}: {d.level:.2f}｜{d.description}")
        return "\n".join(lines)

    def load(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        self.drives.clear()
        self.name_index.clear()
        for item in raw.get("drives", []):
            d = Drive.from_dict(item, self.dim)
            self.drives[d.id] = d
            self.name_index[d.name] = d.id
        self.last_tick = float(raw.get("last_tick", time.time()))

    def save(self, path: str) -> None:
        data = {"version": 1, "last_tick": self.last_tick, "drives": [d.to_dict() for d in self.drives.values()]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _prune_drive(self) -> None:
        candidates = [d for d in self.drives.values() if d.can_decay]
        if not candidates:
            return
        victim = min(candidates, key=lambda d: d.level + d.persistence * 0.3)
        self.drives.pop(victim.id, None)
        self.name_index.pop(victim.name, None)
