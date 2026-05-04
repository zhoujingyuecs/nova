"""Perception：给 nova 补上的“感官 / 现实感”层。

这个模块不取代裂缝场，也不把 nova 改成任务状态机。它做一件事：
让进入意识的东西从一开始就带着来源质地——像人知道“这是我听到的”、
“这是我伸手摸到的”、“这是我自己想到的”。

FissureField 仍然负责回忆和漂移；Perception 只给水流加岸：来源、感官、
证据状态、未完成的社会牵引、动作—观察闭环。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional


# ---------- 来源 / 感官 / 认识状态的约定 ----------
SOURCE_USER = "user"          # 别人说的，社会输入
SOURCE_SELF = "self"          # 我自己说出口 / 想出来
SOURCE_TOOL = "tool"          # 手带回来的
SOURCE_MEMORY = "memory"      # 旧记忆浮起
SOURCE_RUNTIME = "runtime"    # runtime / agenda / 内部调度

MODALITY_HEARING = "hearing"          # 像耳朵听见别人说话
MODALITY_INNER = "inner_speech"       # 内语 / 自己冒出来的念头
MODALITY_SEEING = "seeing"            # web / 文件内容，像看见
MODALITY_TOUCHING = "touching"        # shell/python 的执行反馈，像伸手摸到
MODALITY_MEMORY = "memory"            # 旧事浮起
MODALITY_PROPRIOCEPTION = "proprioception"  # 自己运行状态 / agenda

EPISTEMIC_OBSERVED = "observed"       # 直接观察到
EPISTEMIC_INFERRED = "inferred"       # 推断出来
EPISTEMIC_IMAGINED = "imagined"       # 自己想象 / 假设
EPISTEMIC_REMEMBERED = "remembered"   # 旧记忆
EPISTEMIC_UNVERIFIED = "unverified"   # 未验证
EPISTEMIC_VERIFIED = "verified"       # 有外部证据支撑
EPISTEMIC_ERROR = "error"             # 动作失败 / 工具错误


@dataclass
class Percept:
    """一次“感知对象”。

    它可以是用户一句话、工具一次返回、自己一个念头。关键不是内容，
    而是内容从一开始就带着“我是怎么知道它的”。
    """

    content: str
    source: str
    modality: str
    kind: str
    epistemic_state: str
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)
    action_id: Optional[str] = None
    evidence_url: Optional[str] = None
    raw_result: Optional[str] = None

    def short(self, max_chars: int = 240) -> str:
        text = (self.content or "").strip()
        return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"

    def sensory_label(self) -> str:
        parts = []
        if self.modality:
            parts.append(_modality_cn(self.modality))
        if self.kind:
            parts.append(_kind_cn(self.kind))
        if self.epistemic_state:
            parts.append(_epistemic_cn(self.epistemic_state))
        return "·".join([p for p in parts if p])

    def render(self, max_chars: int = 240) -> str:
        suffix = ""
        if self.evidence_url:
            suffix = f" ｜证据：{self.evidence_url}"
        return f"[{self.sensory_label()}] {self.short(max_chars)}{suffix}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Percept":
        return cls(
            content=d.get("content", ""),
            source=d.get("source", SOURCE_MEMORY),
            modality=d.get("modality", MODALITY_MEMORY),
            kind=d.get("kind", "memory"),
            epistemic_state=d.get("epistemic_state", EPISTEMIC_REMEMBERED),
            confidence=float(d.get("confidence", 1.0) or 1.0),
            timestamp=float(d.get("timestamp", time.time()) or time.time()),
            action_id=d.get("action_id"),
            evidence_url=d.get("evidence_url"),
            raw_result=d.get("raw_result"),
        )


@dataclass
class ActionTrace:
    """一次“伸手/看”的闭环：意图 → 动作 → 观察 → 可推出什么。"""

    id: str
    intention: str
    tool: str
    command: str
    expected: str = ""
    success: bool = False
    observation: Optional[Percept] = None
    conclusion: str = ""
    created_at: float = field(default_factory=time.time)

    def render(self, max_chars: int = 360) -> str:
        ok = "成功" if self.success else "失败/未确认"
        obs = self.observation.render(max_chars=max_chars) if self.observation else "（无观察）"
        return (
            f"- 动作[{self.id}] {ok}：用 {self.tool} 做 `{_one_line(self.command, 90)}`\n"
            f"  观察：{obs}\n"
            f"  边界：{self.conclusion}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["observation"] = self.observation.to_dict() if self.observation else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ActionTrace":
        obs = d.get("observation")
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:8],
            intention=d.get("intention", ""),
            tool=d.get("tool", ""),
            command=d.get("command", ""),
            expected=d.get("expected", ""),
            success=bool(d.get("success", False)),
            observation=Percept.from_dict(obs) if isinstance(obs, dict) else None,
            conclusion=d.get("conclusion", ""),
            created_at=float(d.get("created_at", time.time()) or time.time()),
        )


@dataclass
class OpenRequest:
    """别人交给 nova、尚未完全满足的事。它不是冷冰冰的 ticket，
    更像人脑里“我还欠他一个回应”的小钩子。"""

    speaker: str
    content: str
    requirements: list[str] = field(default_factory=list)
    required_output: str = ""
    evidence_needed: bool = False
    status: str = "active"       # active / satisfied / failed
    urgency: float = 0.7
    created_at: float = field(default_factory=time.time)
    last_touched_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_touched_at = time.time()

    def render(self, max_chars: int = 320) -> str:
        reqs = "；".join(self.requirements) if self.requirements else "理解并回应"
        content = _truncate(self.content, max_chars)
        return (
            f"[未完成的社会牵引]\n"
            f"- {self.speaker} 交给我的事：{content}\n"
            f"- 要满足：{reqs}\n"
            f"- 需要外部证据：{'是' if self.evidence_needed else '不一定'}\n"
            f"- 期望输出：{self.required_output or '直接回答对方'}\n"
            f"- 状态：{self.status}"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OpenRequest":
        return cls(
            speaker=d.get("speaker", "对方"),
            content=d.get("content", ""),
            requirements=list(d.get("requirements", []) or []),
            required_output=d.get("required_output", ""),
            evidence_needed=bool(d.get("evidence_needed", False)),
            status=d.get("status", "active"),
            urgency=float(d.get("urgency", 0.7) or 0.7),
            created_at=float(d.get("created_at", time.time()) or time.time()),
            last_touched_at=float(d.get("last_touched_at", time.time()) or time.time()),
        )


@dataclass
class RealityState:
    """nova 的短期现实感：最近听见/看见/摸到什么，欠谁什么。"""

    current_request: Optional[OpenRequest] = None
    recent_percepts: list[Percept] = field(default_factory=list)
    recent_actions: list[ActionTrace] = field(default_factory=list)
    max_percepts: int = 16
    max_actions: int = 8

    @classmethod
    def load(cls, path: str) -> "RealityState":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"⚠️ reality_state 损坏，从空白重启：{e}")
            return cls()
        req = d.get("current_request")
        return cls(
            current_request=OpenRequest.from_dict(req) if isinstance(req, dict) else None,
            recent_percepts=[Percept.from_dict(x) for x in d.get("recent_percepts", []) if isinstance(x, dict)][-16:],
            recent_actions=[ActionTrace.from_dict(x) for x in d.get("recent_actions", []) if isinstance(x, dict)][-8:],
        )

    def save(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            print(f"⚠️ reality_state 落盘失败（不致命）：{e}")

    def to_dict(self) -> dict:
        return {
            "current_request": self.current_request.to_dict() if self.current_request else None,
            "recent_percepts": [p.to_dict() for p in self.recent_percepts[-self.max_percepts:]],
            "recent_actions": [a.to_dict() for a in self.recent_actions[-self.max_actions:]],
        }

    def observe(self, percept: Percept) -> None:
        self.recent_percepts.append(percept)
        self.recent_percepts = self.recent_percepts[-self.max_percepts:]
        if self.current_request and self.current_request.status == "active":
            self.current_request.touch()

    def hear_user(self, text: str, speaker: str = "对方") -> Percept:
        p = Percept(
            content=text,
            source=SOURCE_USER,
            modality=MODALITY_HEARING,
            kind="request" if looks_like_request(text) else "utterance",
            epistemic_state=EPISTEMIC_OBSERVED,
            confidence=1.0,
        )
        self.observe(p)
        req = infer_open_request(text, speaker=speaker)
        if req is not None:
            self.current_request = req
        return p

    def notice_self_response(self, text: str) -> Percept:
        p = Percept(
            content=text,
            source=SOURCE_SELF,
            modality=MODALITY_INNER,
            kind="response",
            epistemic_state=EPISTEMIC_INFERRED,
            confidence=0.7,
        )
        self.observe(p)
        return p

    def notice_thought(self, text: str) -> Percept:
        p = Percept(
            content=text,
            source=SOURCE_SELF,
            modality=MODALITY_INNER,
            kind="thought",
            epistemic_state=EPISTEMIC_IMAGINED,
            confidence=0.5,
        )
        self.observe(p)
        return p

    def notice_tool_result(self, tool: str, command: str, result: dict, *, intention: str = "") -> ActionTrace:
        action_id = uuid.uuid4().hex[:8]
        percept = percept_from_tool_result(tool, command, result, action_id=action_id)
        success = percept.epistemic_state != EPISTEMIC_ERROR
        conclusion = action_conclusion(tool, command, result, success)
        trace = ActionTrace(
            id=action_id,
            intention=intention or (self.current_request.content if self.current_request else ""),
            tool=tool,
            command=command,
            expected="外部观察 / 执行反馈",
            success=success,
            observation=percept,
            conclusion=conclusion,
        )
        self.observe(percept)
        self.recent_actions.append(trace)
        self.recent_actions = self.recent_actions[-self.max_actions:]
        return trace

    def render_for_prompt(self, max_chars: int = 1800) -> str:
        lines = [
            "[现实感 / 感官锚点]",
            "这些不是额外规则，而是我的感官：我知道什么是听见的、看见的、摸到的、自己想到的。",
            "说事实前先问：我是听别人说的、亲眼看到的、伸手摸到的，还是我自己猜的？",
        ]
        if self.current_request and self.current_request.status == "active":
            lines.append("")
            lines.append(self.current_request.render())
        if self.recent_actions:
            lines.append("\n[最近的动作—观察闭环]")
            for a in self.recent_actions[-4:]:
                lines.append(a.render(max_chars=220))
        if self.recent_percepts:
            lines.append("\n[最近感知]")
            for p in self.recent_percepts[-6:]:
                lines.append(f"- {p.render(max_chars=160)}")
        lines.append(
            "\n[清醒提醒] 工具失败只说明这次动作失败；没有来源链接时，不要把猜测说成查到。"
        )
        text = "\n".join(lines)
        return _truncate(text, max_chars)


SENSORY_SYSTEM_ADDITION = """
——
关于现实感。

你的水流可以联想、做梦、形成隐喻，但进入意识的东西有不同质地：
- 别人说的话像“听见”：这是外部打断，尤其是对方交给你的事。
- web / 文件内容像“看见”：只能说明你看到的页面或文件是什么。
- shell / python 结果像“摸到”：只能说明这次动作返回了什么。
- 自己冒出的念头像“内语”：可以成为假设，但不是事实。

当有人明确让你做事时，这会形成未完成的社会牵引：你可以继续有内心主线，
但不要把它吞掉对方的请求。事实性任务必须让想法撞上世界：上网、读文件、
问工具、拿到来源；如果没拿到，就说“我没查到”，不要编。

工具失败不是哲学结论。404 只说明那个 URL 失败，不说明世界没有新闻。
"""


_REQUEST_PAT = re.compile(
    r"(帮我|帮周靖越|麻烦|请你|查一下|查一查|搜一下|搜索|上网|问问|问豆包|告诉我|给我|出处|链接|不要编|写|改|修改|修|做|找)",
    re.I,
)
_EVIDENCE_PAT = re.compile(r"(新闻|今天|上网|搜索|搜一下|查一下|出处|链接|来源|不要编|验证|事实|百度|新浪|微博|搜狗|bing|豆包)", re.I)


def looks_like_request(text: str) -> bool:
    return bool(_REQUEST_PAT.search(text or ""))


def infer_open_request(text: str, speaker: str = "对方") -> Optional[OpenRequest]:
    text = (text or "").strip()
    if not looks_like_request(text):
        return None
    requirements: list[str] = []
    evidence_needed = bool(_EVIDENCE_PAT.search(text))
    if re.search(r"上网|搜索|搜一下|百度|新浪|微博|搜狗|bing", text, re.I):
        requirements.append("需要外部搜索")
    if "豆包" in text:
        requirements.append("可以按已学步骤问豆包")
    if re.search(r"出处|链接|来源|不要编|验证", text):
        requirements.append("回答要有证据来源，不能脑补")
        evidence_needed = True
    if not requirements:
        requirements.append("先完成对方交代的事，再回到自己的主线")
    required_output = ""
    if evidence_needed:
        required_output = "可核对的结果；若没有证据就明确说没查到"
    return OpenRequest(
        speaker=speaker,
        content=text,
        requirements=requirements,
        required_output=required_output,
        evidence_needed=evidence_needed,
        urgency=0.9 if evidence_needed else 0.75,
    )


def percept_from_tool_result(tool: str, command: str, result: dict, *, action_id: str) -> Percept:
    has_error = bool(result.get("error")) or ("returncode" in result and result.get("returncode") not in (0, None))
    if tool == "web":
        content = result.get("text") or result.get("error") or "页面是空的或者拿不到。"
        modality = MODALITY_SEEING
        evidence_url = _first_url(command)
    elif tool in ("shell", "python"):
        out = result.get("stdout") or ""
        err = result.get("stderr") or ""
        content = result.get("error") or out or err or "命令悄悄结束，没有输出。"
        modality = MODALITY_TOUCHING
        evidence_url = None
    else:
        content = result.get("error") or str(result)
        modality = MODALITY_TOUCHING
        evidence_url = None
    return Percept(
        content=_truncate(content, 1200),
        source=SOURCE_TOOL,
        modality=modality,
        kind="error" if has_error else "observation",
        epistemic_state=EPISTEMIC_ERROR if has_error else EPISTEMIC_OBSERVED,
        confidence=0.95 if not has_error else 0.8,
        action_id=action_id,
        evidence_url=evidence_url,
        raw_result=_truncate(str(result), 2000),
    )


def action_conclusion(tool: str, command: str, result: dict, success: bool) -> str:
    if not success:
        return "这只说明这次伸手失败或返回错误；不能扩大成外部世界的事实。应该换路径或诚实说没查到。"
    if tool == "web":
        return "这说明我看到了这个页面返回的内容；事实陈述只能绑定到页面内容和 URL。"
    if tool in ("shell", "python"):
        return "这说明这次命令/代码的执行反馈；只能据此判断这次动作的结果。"
    return "这是一次外部观察；只能在观察边界内下结论。"


def _first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://\S+", text or "")
    if not m:
        return None
    return m.group(0).rstrip("'\")>,，。；;")


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def _one_line(text: str, max_chars: int) -> str:
    return _truncate(" ".join((text or "").split()), max_chars)


def _modality_cn(x: str) -> str:
    return {
        MODALITY_HEARING: "听见",
        MODALITY_INNER: "内语",
        MODALITY_SEEING: "看见",
        MODALITY_TOUCHING: "摸到",
        MODALITY_MEMORY: "记起",
        MODALITY_PROPRIOCEPTION: "本体感",
    }.get(x, x)


def _kind_cn(x: str) -> str:
    return {
        "request": "请求",
        "utterance": "话语",
        "response": "回应",
        "thought": "念头",
        "observation": "观察",
        "error": "错误",
        "memory": "记忆",
    }.get(x, x)


def _epistemic_cn(x: str) -> str:
    return {
        EPISTEMIC_OBSERVED: "已观察",
        EPISTEMIC_INFERRED: "推断",
        EPISTEMIC_IMAGINED: "想象",
        EPISTEMIC_REMEMBERED: "记得",
        EPISTEMIC_UNVERIFIED: "未验证",
        EPISTEMIC_VERIFIED: "有证据",
        EPISTEMIC_ERROR: "动作失败",
    }.get(x, x)
