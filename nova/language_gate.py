"""LanguageGate：决定这一 tick 要不要调 LLM。

—— v1.3.1。

# 起因

人脑里没有一直开着一个 LLM。多数时候人在转念头、走神、回忆、感觉——
不必把每一个念头都翻译成语言。语言皮层是按需唤醒的。

v1.2 之前 nova 每次 think / perceive 都会调一次（甚至多次）LLM。这让
nova 变成"一个一直开着的本地模型"，而不是"一个偶尔使用语言模型的持续
主体"。

LanguageGate 给 ClayTickEngine 配一个判断器：

  - 用户在等回答：调 LLM
  - 任务需要长解释 / 工具调用：调
  - 念头团激活度高 + 新颖度高：调
  - dream / idle / sleep：默认不调，clay 自转就够了

# 设计取舍

这一层故意做成**确定性的、可解释的打分器**，不依赖另一个 LLM 判断
"要不要说"。理由：

  1. 如果用 LLM 来决定要不要用 LLM，就形成自激；
  2. 这种判断的特征很可枚举，rule-based 已经够；
  3. 用户可以一眼看出"nova 现在为什么沉默 / 为什么开口"。

**这层不管该不该说什么内容**——cluster 没有政策标签，nova 想说什么
都可以。这层只回答"现在这一刻值不值得唤醒语言皮层"。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GateDecision:
    call_llm: bool
    score: float
    reasons: list[str]

    def explain(self) -> str:
        verdict = "调 LLM" if self.call_llm else "保持沉默"
        return f"{verdict}（score={self.score:.2f}）：{'; '.join(self.reasons)}"


class LanguageGate:
    """判断要不要把当前念头翻译成语言。

    打分逻辑：

      + user_waiting          0.65   有人在等回应
      + interrupt_mode        0.20   是被打断模式
      + goal_mode             0.18   主线推进
      + reflect_mode          0.05   反思可以是沉默的
      + orient_mode           0.10   定向需要给出明确选择
      + high_novelty          0.20   有未被说过的高新颖度念头
      + high_agency_pressure  0.15   有强烈的行动冲动
      + high_activation       0.10   有特别强的念头团
      + long_silence          0.05   已经沉默很久了

      - sleep_mode            0.40
      - idle_mode             0.30
      - dream_mode            0.25
      - all_low_novelty       0.15   没什么新东西
    """

    DEFAULT_THRESHOLD = 0.60

    def __init__(self, threshold: float = DEFAULT_THRESHOLD):
        self.threshold = threshold

    def decide(
        self,
        *,
        clusters,                       # list[ThoughtCluster]
        mode: str = "idle",             # interrupt / goal / reflect / orient / dream / idle / sleep
        user_waiting: bool = False,
        seconds_since_last_speech: float = 0.0,
        force_call: bool = False,
        force_silent: bool = False,
    ) -> GateDecision:
        if force_call:
            return GateDecision(True, 1.0, ["force_call"])
        if force_silent:
            return GateDecision(False, 0.0, ["force_silent"])

        score = 0.0
        reasons: list[str] = []

        # --- positive ---
        if user_waiting:
            score += 0.65
            reasons.append("+0.65 user_waiting")

        m = (mode or "").lower()
        if m in {"interrupt", "interrupt_reply"}:
            score += 0.20
            reasons.append("+0.20 interrupt_mode")
        elif m in {"goal", "goal_pursuit"}:
            score += 0.18
            reasons.append("+0.18 goal_mode")
        elif m in {"reflect", "reflection"}:
            score += 0.05
            reasons.append("+0.05 reflect_mode (light)")
        elif m in {"orient", "self_orientation"}:
            score += 0.10
            reasons.append("+0.10 orient_mode")

        active_clusters = [c for c in (clusters or []) if c.activation > 0.05]

        if active_clusters:
            max_novelty = max(c.novelty for c in active_clusters)
            if max_novelty > 0.7:
                score += 0.20
                reasons.append(f"+0.20 high_novelty({max_novelty:.2f})")
            elif max_novelty < 0.25:
                score -= 0.15
                reasons.append(f"-0.15 all_low_novelty({max_novelty:.2f})")

            max_agency = max(c.agency_pressure for c in active_clusters)
            if max_agency > 0.7:
                score += 0.15
                reasons.append(f"+0.15 high_agency_pressure({max_agency:.2f})")

            max_act = max(c.activation for c in active_clusters)
            if max_act > 0.85:
                score += 0.10
                reasons.append(f"+0.10 high_activation({max_act:.2f})")

        if seconds_since_last_speech > 600 and active_clusters:
            score += 0.05
            reasons.append("+0.05 long_silence")

        # --- negative ---
        if m == "sleep":
            score -= 0.40
            reasons.append("-0.40 sleep_mode")
        elif m == "idle":
            score -= 0.30
            reasons.append("-0.30 idle_mode")
        elif m in {"dream", "free_dream"}:
            score -= 0.25
            reasons.append("-0.25 dream_mode")

        score = max(0.0, min(1.0, score))
        call = score >= self.threshold
        return GateDecision(call, score, reasons)


__all__ = ["LanguageGate", "GateDecision"]
