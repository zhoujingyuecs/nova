"""SelfState：合并自我环路。

老版本里同一件事——"我是谁、我刚在做什么、下一步想干嘛"——分散在
五个模块里：self_field（自我裂缝群）、drives（张力）、metacognition
（内省规则）、skills（技能本）、self_modification（自我调整日志）。

每一种都对 prompt 加了一段，每一种都要单独存盘，每一种都要单独维护。
互相之间还会重复——比如"我刚刚被纠正了"会在 SelfField、DriveSystem、
Metacognition、SelfModificationLog 里同时被记录一遍。

v1.0 把它们合并成一个朴素的可读对象 SelfState：
  - identity         —— 我是谁（极少改）
  - current_focus    —— 我此刻在做什么（每轮可改）
  - recent_summary   —— 最近发生了什么（每轮被刷新）
  - open_threads     —— 我想回头继续的几件事

它不带向量、不带级别、不带漂移、不带补丁。它只是个文本字典，
每隔几次 perceive 由一次轻量级 LLM 调用更新一次。具体的事实/步骤
不属于它——那些写在工作区里。

"我是谁"的细微感觉来自缝隙场（哪条裂缝最近常被刷过）；这里只放
一句话能说清的、给 prompt 顶上当主线的状态。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional


_DEFAULT_IDENTITY = (
    "我是 nova。我用本地大模型作为处理器，用陶土球的裂缝场作为记忆地形，"
    "用工作区里的笔记和脚本作为外部记事本。我活在持续运行里，对话只是打断。"
)


@dataclass
class SelfState:
    identity: str = _DEFAULT_IDENTITY
    current_focus: str = "刚醒来。先看一眼自己在哪、最近做过什么，再决定下一步。"
    recent_summary: str = "（还没有最近的事。）"
    open_threads: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    update_count: int = 0

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------
    @classmethod
    def default(cls) -> "SelfState":
        return cls()

    @classmethod
    def load(cls, path: str) -> "SelfState":
        if not os.path.exists(path):
            return cls.default()
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"⚠️ self_state 损坏，从默认重启：{e}")
            return cls.default()
        return cls(
            identity=(d.get("identity") or _DEFAULT_IDENTITY).strip(),
            current_focus=(d.get("current_focus") or "").strip(),
            recent_summary=(d.get("recent_summary") or "").strip(),
            open_threads=[s for s in (d.get("open_threads") or []) if s and s.strip()][:12],
            updated_at=float(d.get("updated_at") or time.time()),
            update_count=int(d.get("update_count") or 0),
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
            print(f"⚠️ self_state 落盘失败（不致命）：{e}")

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "current_focus": self.current_focus,
            "recent_summary": self.recent_summary,
            "open_threads": list(self.open_threads),
            "updated_at": self.updated_at,
            "update_count": self.update_count,
        }

    # ------------------------------------------------------------------
    # rendering for prompt
    # ------------------------------------------------------------------
    def render_for_prompt(self, *, max_chars: int = 1200) -> str:
        lines = ["[你现在的状态——SelfState]"]
        lines.append(f"我是谁：{self.identity}")
        if self.current_focus:
            lines.append(f"我此刻在做：{self.current_focus}")
        if self.recent_summary:
            lines.append(f"刚才发生：{self.recent_summary}")
        if self.open_threads:
            lines.append("我想回头继续的事：")
            for t in self.open_threads[:6]:
                lines.append(f"  - {t}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def apply_update(
        self,
        *,
        current_focus: Optional[str] = None,
        recent_summary: Optional[str] = None,
        add_thread: Optional[str] = None,
        close_thread: Optional[str] = None,
    ) -> bool:
        """局部更新，返回是否真的有变化。"""
        changed = False
        if current_focus is not None:
            new_focus = current_focus.strip()[:300]
            if new_focus and new_focus != self.current_focus:
                self.current_focus = new_focus
                changed = True
        if recent_summary is not None:
            new_summary = recent_summary.strip()[:400]
            if new_summary and new_summary != self.recent_summary:
                self.recent_summary = new_summary
                changed = True
        if add_thread:
            t = add_thread.strip()[:200]
            if t and not any(_norm(x) == _norm(t) for x in self.open_threads):
                self.open_threads.append(t)
                if len(self.open_threads) > 12:
                    self.open_threads = self.open_threads[-12:]
                changed = True
        if close_thread:
            key = _norm(close_thread)
            new_list = [x for x in self.open_threads if _norm(x) != key]
            if len(new_list) != len(self.open_threads):
                self.open_threads = new_list
                changed = True
        if changed:
            self.updated_at = time.time()
            self.update_count += 1
        return changed


# ======================================================================
# 让本地 LLM 帮 nova 更新一次 SelfState（每若干次 perceive 调用一次）
# ======================================================================
SELF_UPDATE_PROMPT = """\
你正在帮 nova 更新她的 SelfState。SelfState 是 nova 持续运行的"主意识快照"，
不是详细日记，不是情绪片段。它只放：
  - current_focus：她此刻在做什么（一句话，不超过 80 字）
  - recent_summary：刚发生的事的事实摘要（一两句，不超过 120 字）
  - 可选 open_thread：一件值得回头继续的事（如果出现，否则不要写）
  - 可选 close_thread：一件已经做完、可以从待办里拿掉的事

约束：
  - 输出必须是事实摘要，不要诗意比喻、情绪铺陈。
  - 多数轮次只需更新 current_focus 和 recent_summary，不要每次都加 thread。
  - 如果这一轮没什么实质变化，就只输出"（无变动。）"。

【当前 SelfState】
{current_state}

【刚刚发生】
{event}

【当前主线/agenda 摘要】
{agenda_text}

请输出 0~4 行控制：
[FOCUS] 我此刻在做什么
[SUMMARY] 刚发生的事
[OPEN] 一件值得回头继续的事
[CLOSE] 一件可以划掉的事

如果没有任何要变的，只输出：
（无变动。）
"""

_FOCUS_RE = re.compile(r"^\s*\[FOCUS\]\s*(.+?)\s*$", re.I | re.M)
_SUMMARY_RE = re.compile(r"^\s*\[SUMMARY\]\s*(.+?)\s*$", re.I | re.M)
_OPEN_RE = re.compile(r"^\s*\[OPEN\]\s*(.+?)\s*$", re.I | re.M)
_CLOSE_RE = re.compile(r"^\s*\[CLOSE\]\s*(.+?)\s*$", re.I | re.M)


def parse_self_update(raw: str) -> dict:
    """解析 LLM 的更新输出。返回可以喂给 apply_update 的 kwargs。"""
    raw = raw or ""
    out: dict = {}
    m = _FOCUS_RE.search(raw)
    if m:
        out["current_focus"] = m.group(1).strip()
    m = _SUMMARY_RE.search(raw)
    if m:
        out["recent_summary"] = m.group(1).strip()
    m = _OPEN_RE.search(raw)
    if m:
        out["add_thread"] = m.group(1).strip()
    m = _CLOSE_RE.search(raw)
    if m:
        out["close_thread"] = m.group(1).strip()
    return out


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "") if not ch.isspace()).strip("。.!！?？")
