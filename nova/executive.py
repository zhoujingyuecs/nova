"""ExecutiveController：nova 持续运行时的下一步选择器。

LLM 是处理器，不是调度器。这一层用确定性规则在每个 tick 之前选模式：

  - 有人类打断：先处理 interrupt；
  - 到了睡眠阈值：先整理；
  - 有 active agenda：继续主线干活；
  - 主线连续受阻：先做反思而不是蛮干；
  - 一个主线都没有：进入 self_orientation，让 nova 自己生成下一条线；
  - orientation 都生成不出主线时：允许漂移走神。

这个选择器故意保持可解释，避免把"自主性"全部塞进 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Optional

from .agenda import Agenda, AgendaItem


MODE_INTERRUPT = "interrupt_reply"
MODE_GOAL = "goal_pursuit"
MODE_SLEEP = "sleep"
MODE_REFLECT = "reflection"
MODE_DREAM = "free_dream"
MODE_IDLE = "idle"
MODE_ORIENT = "self_orientation"


@dataclass
class Decision:
    mode: str
    reason: str
    agenda_id: str = ""
    should_save: bool = True


class ExecutiveController:
    def __init__(
        self,
        *,
        sleep_every_seconds: float = 6 * 3600,
        reflect_after_failures: int = 3,
        dream_when_idle: bool = True,
        orient_when_no_agenda: bool = True,
    ):
        self.sleep_every_seconds = sleep_every_seconds
        self.reflect_after_failures = reflect_after_failures
        self.dream_when_idle = dream_when_idle
        self.orient_when_no_agenda = orient_when_no_agenda

    def choose(
        self,
        *,
        agenda: Agenda,
        has_interrupt: bool,
        last_sleep_at: float,
        last_tick_at: float,
        now: Optional[float] = None,
    ) -> Decision:
        now = now or time.time()
        if has_interrupt:
            return Decision(MODE_INTERRUPT, "有人类打断，先把外部输入接进主意识。")

        if last_sleep_at and self.sleep_every_seconds > 0 \
                and now - last_sleep_at >= self.sleep_every_seconds:
            return Decision(MODE_SLEEP, "距离上次睡眠整理已经超过阈值。")

        current = agenda.current()
        if current is not None:
            if current.failures >= self.reflect_after_failures:
                return Decision(
                    MODE_REFLECT,
                    f"主线 {current.id} 连续受阻，需要先反思策略。",
                    agenda_id=current.id,
                )
            return Decision(
                MODE_GOAL,
                f"继续推进当前主线：{current.title}",
                agenda_id=current.id,
            )

        # 没有主线：让 nova 自我取向
        if self.orient_when_no_agenda:
            return Decision(MODE_ORIENT,
                            "没有 active agenda，先进行自我取向，生成自己的主线。")

        if self.dream_when_idle:
            return Decision(MODE_DREAM, "没有明确主线，允许意识自由流动。")
        return Decision(MODE_IDLE, "没有主线，也没有允许空闲走神。", should_save=False)


# ============================================================
# 模式 prompt 构造
# ============================================================
def build_goal_prompt(item: AgendaItem, *, recent_work: str,
                      agenda_text: str) -> str:
    """给 Nova.perceive / Nova.think 使用的内部主线工作提示。"""
    evidence = "\n".join(f"- {e}" for e in item.evidence[-8:]) or "（暂无）"
    return f"""【内部主线工作 / 不需要对外寒暄】

你不是在回答一个用户问题。你现在处于无人打断的连续运行状态，正在沿自己的主线继续工作。

当前主线：{item.title}
主线说明：{item.description or '（无）'}
下一步线索：{item.next_action or '（由你判断）'}
最近证据：
{evidence}

当前 agenda 概览：
{agenda_text}

最近工作日志：
{recent_work}

请推进这一轮。可以思考、可以查工作区、可以伸手跑命令。
事实优先：要查的就 grep / cat / curl；要写的就 cat > 写到 ~/nova_workspace 里去。

安全边界：
- 不要执行破坏性 shell 命令（rm -rf、格式化、kill 系统进程、外传密钥）。
- 真要改动用户的真实项目文件，先写到 workspace 草稿里，明确说明再说改。
- 拿不准的就阻塞，别硬做。

输出必须包含以下三段，便于 runtime 记录：
[STATUS] CONTINUE / DONE / BLOCKED / SLEEP
[SUMMARY] 一句话说明这一轮完成了什么
[NEXT] 下一轮该做什么；如果 DONE 或 BLOCKED，写完成证据或阻塞原因
"""


def build_reflection_prompt(item: AgendaItem, *, recent_work: str) -> str:
    evidence = "\n".join(f"- {e}" for e in item.evidence[-12:]) or "（暂无）"
    return f"""【内部反思 / 主线受阻】

当前主线多次受阻：{item.title}
说明：{item.description or '（无）'}
失败/证据：
{evidence}

最近工作日志：
{recent_work}

请不要继续蛮干。请先伸手到工作区 grep 一下，看以前是不是已经写过类似情况
的纠正或步骤；再决定一个更小、更稳的下一步。

如果你识别出之前的某条做法系统性地走错了，请把纠正写到
~/nova_workspace/notes/ 下一篇有日期的笔记里——下次再遇到时先翻笔记。

输出格式：
[STATUS] CONTINUE / BLOCKED / DONE
[SUMMARY] 你识别出的卡点
[NEXT] 更小的下一步，或需要人类确认的问题
"""


def build_orientation_prompt(*, agenda_text: str, recent_work: str,
                             self_state_text: str) -> str:
    """没有 active agenda 时，让 nova 自己生成下一条主线。"""
    return f"""【自我取向 / self_orientation】

你不是因为启动参数才存在。外部任务只是委托，不是你的根。
你正在持续运行；此刻没有必须立刻处理的人类打断，也没有清晰的 active agenda。
请不要随便空想，也不要假装已经有宏大使命。

请根据你已经经历过的记忆、能力、限制、最近工作和工作区里写下来的东西，
生成你下一段时间值得延续的一条具体主线。

可以伸手到 ~/nova_workspace 里看看以前留下了什么未完成的事。

当前 SelfState：
{self_state_text}

当前 agenda：
{agenda_text}

最近工作：
{recent_work}

请输出三个控制行：
[STATUS] CONTINUE
[AGENDA] 你自己生成的一条具体主线（不要是空泛口号，要能被推进）
[NEXT] 这条主线的下一步动作

如果你认为应该先睡眠整理，把 [AGENDA] 写成"睡眠整理：……"。
"""
