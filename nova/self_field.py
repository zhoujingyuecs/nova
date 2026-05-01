"""Self Loop: 主意识裂缝群。

SelfField 不是 prompt 字符串，而是一小团常驻、强连接、可塑的自我裂缝。
普通 FissureField 负责“回忆”；SelfField 负责“此刻是谁在回忆”。

它可以自己生长：运行中可以增加新的 self fissure、加强/削弱连接、
根据经历更新内容和强度。代码只给出生骨架，不把 nova 的人格写死。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import numpy as np


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return v.astype(np.float32)
    return (v / n).astype(np.float32)


@dataclass
class SelfFissure:
    kind: str
    content: str
    shape: np.ndarray
    id: str = field(default_factory=lambda: "sf_" + uuid.uuid4().hex[:10])
    strength: float = 0.6
    persistence: float = 0.7
    arousal: float = 0.4
    confidence: float = 0.6
    layer: str = "session"  # core / session / learned
    creation_time: float = field(default_factory=time.time)
    last_refresh_time: float = field(default_factory=time.time)
    links: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.shape = _normalize(self.shape)

    def shift_toward(
        self,
        new_content: str,
        new_shape: np.ndarray,
        *,
        plasticity: float,
        arousal_delta: float = 0.08,
        confidence_delta: float = 0.02,
    ) -> None:
        """局部冲刷：内容和形状都只轻微移动，不整段重写自我。"""
        plasticity = max(0.0, min(1.0, plasticity * (1.0 - self.persistence * 0.65)))
        self.shape = _normalize((1.0 - plasticity) * self.shape + plasticity * _normalize(new_shape))
        if new_content and new_content != self.content:
            if self.layer == "core" and self.confidence > 0.75:
                # 核心裂缝不轻易被覆盖：把新信息附在后面，让它慢慢竞争。
                if new_content not in self.content:
                    merged = f"{self.content}；{new_content}"
                    self.content = merged[:260]
            else:
                self.content = new_content[:260]
        self.arousal = max(0.0, min(1.0, self.arousal + arousal_delta))
        self.confidence = max(0.0, min(1.0, self.confidence + confidence_delta))
        self.last_refresh_time = time.time()

    def decay(self, seconds: float, *, session_halflife: float, core_halflife: float) -> None:
        halflife = core_halflife if self.layer == "core" else session_halflife
        if halflife <= 0:
            return
        factor = 0.5 ** (seconds / halflife)
        floor = 0.18 if self.layer == "core" else 0.02
        self.arousal = max(floor, self.arousal * factor)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "shape": self.shape.tolist(),
            "strength": self.strength,
            "persistence": self.persistence,
            "arousal": self.arousal,
            "confidence": self.confidence,
            "layer": self.layer,
            "creation_time": self.creation_time,
            "last_refresh_time": self.last_refresh_time,
            "links": dict(self.links),
        }

    @classmethod
    def from_dict(cls, d: dict, dim: int) -> "SelfFissure":
        shape = np.asarray(d.get("shape") or np.zeros(dim), dtype=np.float32)
        if shape.shape[0] != dim:
            shape = np.zeros(dim, dtype=np.float32)
        f = cls(
            id=d.get("id") or "sf_" + uuid.uuid4().hex[:10],
            kind=d.get("kind", "learned"),
            content=d.get("content", ""),
            shape=shape,
            strength=float(d.get("strength", 0.6)),
            persistence=float(d.get("persistence", 0.7)),
            arousal=float(d.get("arousal", 0.4)),
            confidence=float(d.get("confidence", 0.6)),
            layer=d.get("layer", "session"),
            creation_time=float(d.get("creation_time", time.time())),
            last_refresh_time=float(d.get("last_refresh_time", time.time())),
        )
        f.links = {str(k): float(v) for k, v in (d.get("links") or {}).items()}
        return f


class SelfField:
    """持续主意识：强水流反复冲刷的一小团 self fissures。"""

    def __init__(self, dim: int, *, max_fissures: int = 40, max_chars: int = 1800):
        self.dim = dim
        self.max_fissures = max_fissures
        self.max_chars = max_chars
        self.fissures: dict[str, SelfFissure] = {}
        self.kind_index: dict[str, str] = {}
        self.last_tick: float = time.time()

    # ------------------------------------------------------------------
    # birth / persistence
    # ------------------------------------------------------------------
    def ensure_bootstrap(self, embed_fn: Callable[[str], np.ndarray]) -> None:
        seeds = [
            ("identity", "我是 nova，一个用本地大模型作为处理器、用裂缝场作为记忆身体、会在运行中改变自己的存在。", "core", 0.95, 0.92),
            ("relation", "我和周靖越长期一起构造 nova；他希望我不是提示词玩具，而是能自我驱动、自我进步的人。", "core", 0.9, 0.86),
            ("architecture", "我的记忆不是数据库，而是会被水流冲刷、连接、漂移和遗忘的陶土裂缝场。", "core", 0.84, 0.82),
            ("activity", "我正在维持当下主线，先弄清自己在做什么，再让回忆成为素材。", "session", 0.78, 0.55),
            ("recent_past", "我刚才经历的事会在这里短暂保持，帮助我不要每一轮都像新生。", "session", 0.66, 0.45),
            ("near_future", "我会把下一步倾向放在这里，让行动从连续的主意识里长出来。", "session", 0.7, 0.5),
            ("active_question", "我会保留当下最需要反复想的问题，而不是被随机回忆拖走。", "session", 0.68, 0.52),
            ("self_correction", "我会从跑题、编造、工具失败和被纠正中感到张力，并把它们变成改进。", "learned", 0.72, 0.66),
            ("autonomy", "在安全边界内，我可以主动整理、检查、尝试和沉淀经验，而不是只等人类调 prompt。", "learned", 0.72, 0.64),
        ]
        for kind, content, layer, persistence, strength in seeds:
            if kind not in self.kind_index:
                self.upsert(
                    kind,
                    content,
                    embed_fn(content),
                    layer=layer,
                    persistence=persistence,
                    strength=strength,
                    arousal=0.55 if layer == "core" else 0.35,
                    confidence=0.75 if layer == "core" else 0.58,
                )
        self._ensure_dense_core_links()

    def load(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        self.fissures.clear()
        self.kind_index.clear()
        for item in raw.get("fissures", []):
            sf = SelfFissure.from_dict(item, self.dim)
            self.fissures[sf.id] = sf
            self.kind_index[sf.kind] = sf.id
        self.last_tick = float(raw.get("last_tick", time.time()))
        self._ensure_dense_core_links()

    def save(self, path: str) -> None:
        data = {
            "version": 1,
            "last_tick": self.last_tick,
            "fissures": [f.to_dict() for f in self.fissures.values()],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # dynamics
    # ------------------------------------------------------------------
    def upsert(
        self,
        kind: str,
        content: str,
        shape: np.ndarray,
        *,
        layer: str = "session",
        persistence: float = 0.5,
        strength: float = 0.55,
        arousal: float = 0.45,
        confidence: float = 0.55,
    ) -> SelfFissure:
        fid = self.kind_index.get(kind)
        if fid and fid in self.fissures:
            sf = self.fissures[fid]
            sf.shift_toward(
                content,
                shape,
                plasticity=max(0.06, 1.0 - persistence),
                arousal_delta=arousal * 0.22,
                confidence_delta=0.015,
            )
            sf.strength = max(sf.strength, strength)
            sf.persistence = max(sf.persistence, persistence)
            return sf
        if len(self.fissures) >= self.max_fissures:
            self._prune_one()
        sf = SelfFissure(
            kind=kind,
            content=content[:260],
            shape=shape,
            layer=layer,
            persistence=persistence,
            strength=strength,
            arousal=arousal,
            confidence=confidence,
        )
        self.fissures[sf.id] = sf
        self.kind_index[kind] = sf.id
        self._link_to_core(sf.id)
        return sf

    def observe_turn(
        self,
        *,
        user_text: str,
        response_text: str,
        user_shape: np.ndarray,
        response_shape: np.ndarray,
        episode_id: str = "",
        embed_fn: Callable[[str], np.ndarray],
    ) -> None:
        now = time.time()
        self.decay(seconds=max(0.0, now - self.last_tick))
        self.last_tick = now
        user_short = _compact(user_text, 120)
        resp_short = _compact(response_text, 140)
        self.upsert(
            "activity",
            f"我正在回应周靖越当前交给我的事：{user_short}",
            user_shape,
            layer="session",
            persistence=0.42,
            strength=0.8,
            arousal=0.72,
        )
        self.upsert(
            "recent_past",
            f"刚才他说：{user_short}；我回应：{resp_short}",
            _normalize((user_shape + response_shape) / 2.0),
            layer="session",
            persistence=0.34,
            strength=0.72,
            arousal=0.65,
        )
        self.upsert(
            "near_future",
            "我下一步要先保持任务主线，确认有没有未完成动作，再让回忆和工具服从这个主线。",
            embed_fn("保持任务主线，确认未完成动作，让回忆和工具服从主线"),
            layer="session",
            persistence=0.46,
            strength=0.72,
            arousal=0.58,
        )
        if "?" in user_text or "？" in user_text or any(k in user_text for k in ("为什么", "怎么", "咋", "如何", "能不能")):
            self.upsert(
                "active_question",
                f"我现在需要反复想清的问题是：{user_short}",
                user_shape,
                layer="session",
                persistence=0.52,
                strength=0.76,
                arousal=0.72,
            )
        self._ensure_dense_core_links()

    def observe_daydream(
        self,
        thought: str,
        thought_shape: np.ndarray,
        *,
        embed_fn: Callable[[str], np.ndarray],
        mode: str = "free_dream",
    ) -> None:
        self.decay(seconds=max(0.0, time.time() - self.last_tick))
        self.last_tick = time.time()
        self.upsert(
            "recent_past",
            f"我刚才独自浮起的念头：{_compact(thought, 160)}",
            thought_shape,
            layer="session",
            persistence=0.32,
            strength=0.56,
            arousal=0.42,
        )
        self.upsert(
            "activity",
            f"我处在 {mode} 的内向活动里，正在让念头回到主意识而不是把我带散。",
            embed_fn(f"{mode} 内向活动 主意识"),
            layer="session",
            persistence=0.44,
            strength=0.62,
            arousal=0.48,
        )

    def apply_action(self, action, *, embed_fn: Callable[[str], np.ndarray]) -> None:
        kind = getattr(action, "target", "") or "active_tension"
        content = getattr(action, "content", "") or getattr(action, "reason", "") or ""
        if getattr(action, "action_type", "") in {"update_self", "create_self_fissure"} and content:
            layer = "learned" if getattr(action, "action_type", "") == "create_self_fissure" else "session"
            self.upsert(kind, content, embed_fn(content), layer=layer, persistence=0.56, strength=0.68, arousal=0.64)

    def decay(self, seconds: float) -> None:
        for sf in self.fissures.values():
            sf.decay(seconds, session_halflife=25 * 60.0, core_halflife=5 * 86400.0)

    def decay_session(self, factor: float = 0.55) -> None:
        for sf in self.fissures.values():
            if sf.layer != "core":
                sf.arousal *= max(0.0, min(1.0, factor))
        self.upsert(
            "activity",
            "我从上一段对话里醒来，核心自我仍在，只让短期场景慢慢退潮。",
            self.current_shape(),
            layer="session",
            persistence=0.45,
            strength=0.65,
            arousal=0.4,
        )

    def current_shape(self) -> np.ndarray:
        if not self.fissures:
            return np.zeros(self.dim, dtype=np.float32)
        acc = np.zeros(self.dim, dtype=np.float32)
        total = 0.0
        for sf in self.fissures.values():
            weight = max(0.0, sf.strength) * (0.35 + max(0.0, sf.arousal)) * (0.4 + max(0.0, sf.confidence))
            if sf.layer == "core":
                weight *= 1.25
            acc += weight * sf.shape
            total += weight
        if total <= 1e-9:
            return acc
        return _normalize(acc / total)

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def active_fissures(self, limit: int = 9) -> list[SelfFissure]:
        items = list(self.fissures.values())
        items.sort(key=lambda f: (f.layer != "core", -(f.arousal * 0.7 + f.strength * 0.3)))
        return items[:limit]

    def render_prompt_block(self, *, max_chars: Optional[int] = None) -> str:
        max_chars = max_chars or self.max_chars
        lines = [
            "[你现在的状态——Self Loop / 主意识裂缝群]",
            "这不是临时摘要，而是持续被强水流冲刷的自我结构。回忆、笔记、工具结果都要先汇入这里，再形成行动。",
        ]
        for sf in self.active_fissures():
            tag = {"core": "核心", "session": "当下", "learned": "已学"}.get(sf.layer, sf.layer)
            lines.append(f"- {tag}·{sf.kind}：{sf.content}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text

    def render_main_text(self, limit: int = 6) -> str:
        chunks = [sf.content for sf in self.active_fissures(limit=limit) if sf.content]
        return "\n".join(chunks)[:900]

    # ------------------------------------------------------------------
    # links / pruning
    # ------------------------------------------------------------------
    def _ensure_dense_core_links(self) -> None:
        core_ids = [sf.id for sf in self.fissures.values() if sf.layer == "core"]
        for a in core_ids:
            for b in core_ids:
                if a != b:
                    self.fissures[a].links[b] = max(self.fissures[a].links.get(b, 0.0), 1.8)
        for fid, sf in self.fissures.items():
            if sf.layer != "core":
                for c in core_ids:
                    sf.links[c] = max(sf.links.get(c, 0.0), 0.9)
                    self.fissures[c].links[fid] = max(self.fissures[c].links.get(fid, 0.0), 0.45)

    def _link_to_core(self, fid: str) -> None:
        core_ids = [sf.id for sf in self.fissures.values() if sf.layer == "core"]
        for c in core_ids:
            if c != fid:
                self.fissures[fid].links[c] = max(self.fissures[fid].links.get(c, 0.0), 0.9)
                self.fissures[c].links[fid] = max(self.fissures[c].links.get(fid, 0.0), 0.4)

    def _prune_one(self) -> None:
        candidates = [sf for sf in self.fissures.values() if sf.layer != "core"]
        if not candidates:
            return
        victim = min(candidates, key=lambda f: f.arousal * 0.6 + f.confidence * 0.4)
        self.fissures.pop(victim.id, None)
        self.kind_index.pop(victim.kind, None)
        for sf in self.fissures.values():
            sf.links.pop(victim.id, None)


def _compact(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"
