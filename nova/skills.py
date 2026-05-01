"""Self Loop: 技能沉淀。

笔记本记录“我知道的事实/步骤”，SkillBook 记录“我反复怎样做会更好”。
技能来自成功/失败，不靠人类手动调 prompt。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Skill:
    name: str
    trigger: str
    procedure: list[str]
    id: str = field(default_factory=lambda: "sk_" + uuid.uuid4().hex[:10])
    confidence: float = 0.45
    success_count: int = 0
    failure_count: int = 0
    last_used_time: float = field(default_factory=time.time)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger,
            "procedure": list(self.procedure),
            "confidence": self.confidence,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used_time": self.last_used_time,
            "examples": list(self.examples),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            id=d.get("id") or "sk_" + uuid.uuid4().hex[:10],
            name=d.get("name", "skill"),
            trigger=d.get("trigger", ""),
            procedure=list(d.get("procedure", [])),
            confidence=float(d.get("confidence", 0.45)),
            success_count=int(d.get("success_count", 0)),
            failure_count=int(d.get("failure_count", 0)),
            last_used_time=float(d.get("last_used_time", time.time())),
            examples=list(d.get("examples", [])),
        )


class SkillBook:
    def __init__(self, path: str, *, max_skills: int = 80):
        self.path = path
        self.max_skills = max_skills
        self.skills: dict[str, Skill] = {}
        self.name_index: dict[str, str] = {}

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        self.skills.clear()
        self.name_index.clear()
        for item in raw.get("skills", []):
            sk = Skill.from_dict(item)
            self.skills[sk.id] = sk
            self.name_index[sk.name] = sk.id

    def save(self) -> None:
        data = {"version": 1, "skills": [s.to_dict() for s in self.skills.values()]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def upsert(self, name: str, trigger: str, procedure: list[str], *, example: str = "", confidence: float = 0.55) -> Skill:
        if name in self.name_index:
            sk = self.skills[self.name_index[name]]
            sk.trigger = trigger or sk.trigger
            # 保留旧步骤，补入新步骤，避免一次事件覆盖整个技能。
            merged = list(sk.procedure)
            for step in procedure:
                if step and step not in merged:
                    merged.append(step)
            sk.procedure = merged[:8]
            sk.confidence = max(sk.confidence, confidence)
            if example:
                sk.examples = (sk.examples + [example])[-6:]
            sk.last_used_time = time.time()
            return sk
        if len(self.skills) >= self.max_skills:
            self._prune_one()
        sk = Skill(name=name, trigger=trigger, procedure=procedure[:8], confidence=confidence)
        if example:
            sk.examples.append(example)
        self.skills[sk.id] = sk
        self.name_index[sk.name] = sk.id
        return sk

    def apply_action(self, action) -> None:
        if getattr(action, "action_type", "") != "create_skill":
            return
        name = getattr(action, "target", "") or "learned_skill"
        content = getattr(action, "content", "") or ""
        reason = getattr(action, "reason", "") or ""
        if not content:
            return
        # 简单把一句技能拆成触发和流程；以后可以让本地 LLM 细化。
        self.upsert(
            name=name,
            trigger=reason or content[:80],
            procedure=[content],
            example=reason,
            confidence=float(getattr(action, "confidence", 0.55) or 0.55),
        )

    def observe_event(self, *, stimulus: str = "", response: str = "") -> None:
        text = f"{stimulus}\n{response}"
        if any(k in text for k in ("改代码", "项目", "patch", "github", "文件")):
            self.upsert(
                "project_change_loop",
                "用户让我修改项目或代码",
                [
                    "先定位相关文件和调用链。",
                    "优先做最小改动，不重写无关部分。",
                    "新增模块时保持向后兼容。",
                    "改完至少做语法检查或最小验证。",
                    "最后说明哪些文件变了、还没验证什么。",
                ],
                example="项目修改任务",
                confidence=0.58,
            )

    def render_prompt_block(self, *, max_chars: int = 1200, limit: int = 8) -> str:
        if not self.skills:
            return ""
        items = sorted(self.skills.values(), key=lambda s: (-s.confidence, -s.last_used_time))[:limit]
        lines = ["[我沉淀出的技能 / SkillBook]", "这些是我从成功和失败中长出的工作习惯；触发时应主动调用。"]
        for sk in items:
            steps = "；".join(sk.procedure[:4])
            lines.append(f"- {sk.name}（置信 {sk.confidence:.2f}）：触发：{sk.trigger}；做法：{steps}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars] + "…"

    def _prune_one(self) -> None:
        if not self.skills:
            return
        victim = min(self.skills.values(), key=lambda s: s.confidence + s.success_count * 0.04 - s.failure_count * 0.05)
        self.skills.pop(victim.id, None)
        self.name_index.pop(victim.name, None)
