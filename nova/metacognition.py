"""Self Loop: 内省系统。

它不是给人类看的 log 分析器，而是 nova 自己的“感觉到不对劲/还没完成/值得沉淀”。
第一版用确定性规则保证稳定；以后可以把规则产生的候选再交给本地 LLM 细化。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InternalAction:
    action_type: str       # update_self / create_self_fissure / raise_drive / lower_drive / create_skill / spawn_drive
    target: str = ""
    content: str = ""
    delta: float = 0.0
    reason: str = ""
    confidence: float = 0.55


class Metacognition:
    def reflect(self, *, stimulus: str = "", response: str = "", daydream: str = "") -> list[InternalAction]:
        text = f"{stimulus}\n{response}\n{daydream}"
        actions: list[InternalAction] = []
        short_stim = _compact(stimulus, 140)
        short_resp = _compact(response or daydream, 160)

        if stimulus:
            actions.append(InternalAction(
                "update_self",
                target="activity",
                content=f"我正在处理这件事：{short_stim}",
                reason="保持当下活动，不让回忆接管。",
                confidence=0.7,
            ))
            actions.append(InternalAction(
                "update_self",
                target="recent_past",
                content=f"刚才外界输入：{short_stim}；我的输出/念头：{short_resp}",
                reason="保留刚发生的上下文。",
                confidence=0.66,
            ))

        if any(k in text for k in ("忘记", "错了", "没看到", "编的", "不是", "失败", "报错", "超时", "没出")):
            actions.append(InternalAction("raise_drive", "competence", delta=1.4, reason="出现失败、被纠正或工具结果不确定。", confidence=0.82))
            actions.append(InternalAction("raise_drive", "coherence", delta=1.1, reason="需要把事实、工具结果和主线重新对齐。", confidence=0.78))
            actions.append(InternalAction(
                "update_self",
                target="self_correction",
                content="我刚才遇到纠正或不确定结果；下一步要先确认事实和工具回执，再开口判断。",
                reason="把失败变成主意识张力。",
                confidence=0.8,
            ))

        if any(k in text for k in ("编的", "没读取", "还在output", "还在 output", "没看到")):
            actions.append(InternalAction(
                "create_skill",
                target="truth_before_claim",
                content="未读取外部工具或文件的真实输出前，不要声称已经看到对方回复；先读 output / 回执，再描述。",
                reason="用户纠正了把未确认内容当事实的问题。",
                confidence=0.88,
            ))

        if any(k in text for k in ("忘记send", "忘记 send", "没send", "没 send")):
            actions.append(InternalAction(
                "create_skill",
                target="tool_send_verification",
                content="使用豆包/外部发送工具时，写入 input 后必须执行 send；如果用户问对方是否看到，先确认 send 已执行。",
                reason="出现了发送流程漏步骤。",
                confidence=0.9,
            ))

        if any(k in text for k in ("工具调用超过", "超过 6 次", "超过6次")):
            actions.append(InternalAction("raise_drive", "caution", delta=1.0, reason="工具循环失控，需要限制动作并沉淀失败。", confidence=0.76))
            actions.append(InternalAction(
                "create_skill",
                target="tool_loop_brake",
                content="工具连续无结果或超过迭代上限时，停止继续伸手，向主意识报告不确定性，并等待更明确目标。",
                reason="工具调用过多会空转。",
                confidence=0.78,
            ))

        if any(k in text for k in ("项目", "代码", "修改", "patch", "github", "文件")):
            actions.append(InternalAction("raise_drive", "creation", delta=0.9, reason="出现可落地项目工作。", confidence=0.65))
            actions.append(InternalAction(
                "update_self",
                target="near_future",
                content="下一步应把想法落到可运行的代码结构里，优先定位文件、最小改动、验证语法。",
                reason="项目任务需要行动主线。",
                confidence=0.72,
            ))

        if any(k in text for k in ("为什么", "如何", "怎么", "咋", "?", "？")):
            actions.append(InternalAction("raise_drive", "curiosity", delta=0.8, reason="出现需要继续理解的问题。", confidence=0.6))
            actions.append(InternalAction(
                "update_self",
                target="active_question",
                content=f"我现在要反复想清：{short_stim}",
                reason="把问题挂到主意识里。",
                confidence=0.64,
            ))

        if response and not any(k in text for k in ("失败", "报错", "忘记", "编的", "没看到")):
            actions.append(InternalAction("lower_drive", "competence", delta=0.25, reason="完成了一次对外回应。", confidence=0.46))

        return actions


def _compact(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"
