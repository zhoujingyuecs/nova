"""nova：用陶土球与水流模型组织记忆的本地意识体。

==================================================================
                v0.7 —— "她现在能学会东西"
==================================================================

v0.6 给了 nova 一根清醒主线（主意识）。但她有一个老毛病没解决：
**学不会东西**。哪怕你把同一套步骤教她 5 遍，第 6 次她仍然记不住——
因为缝隙场是"按相似度浮起的回忆"，不是"她确实知道的事"。

文艺地讲：
  • 缝隙场 ≈ 回忆——模糊的、漂移的、按经验贴近度浮起来的片段
  • 主意识 ≈ 当下的状态——她现在是谁、在干什么、心情怎么样
  • 但人脑里还有第三块——"我知道的事"：稳定的、明确的、可以"一二
    三"列出来的事。比如怎么用某个工具、用户的名字、被纠正过的误解、
    长期偏好。

v0.7 给 nova 加上这第三块——**笔记本（NotesBook）**。

  1. ★ NotesBook：she-knows
     一份明确的、稳定的、跨 episode 跨重启都保留的清单。
     每条笔记是一句"我知道..."，永远在 prompt 里出现，不靠水流碰巧
     刷过。

  2. ★ 每次 perceive 之后做一次"消化沉淀"
     用一次额外的 LLM 调用，看刚才那段对话有没有要记进笔记本的：
       - 学到的步骤 / 工具用法
       - 重要事实
       - 被纠正过的误解
       - 长期偏好
     LLM 输出 ADD / UPDATE / REMOVE 三种动作，nova 按动作维护笔记本。

  3. ★ Prompt 多一栏 [你已经学会的事]
     位置在主意识下面、回忆上面——也就是 nova 思考时的第二顺位：
       1) 我现在是谁、在干什么（主意识）
       2) 我已经知道、确认过的事（笔记本）
       3) 浮起来的相关回忆（素材）
       4) 这场对话刚刚的几句（场景）
       5) 他刚说的这句话（输入）

  4. ★ 系统提示词更新
     明确告诉 nova："学到的东西真的会被记下来，下次能直接调用。"

==================================================================
v0.6 的主意识、v0.5 的对话链与场景元数据、v0.4 的暗道与意象拆解、
v0.3 之前的水流与缝隙——全都还在。v0.7 是在它们之上加的"笔记本"，
不是替换。
==================================================================
"""

from .config import NovaConfig, DEFAULT_SYSTEM_PROMPT
from .fissure import Fissure
from .field import FissureField
from .flow import ConsciousnessFlow
from .embedder import Embedder
from .llm import LocalLLM
from .notes import Note, NotesBook
from .mind import Nova
from .dreamer import Daydreamer
from .sleep import consolidate
from .visualize import render_field
from .persistence import save_field, load_field
from .tools import (
	VMAgent, parse_actions, strip_actions, format_result,
	CAPABILITY_MEMORIES,
)

__all__ = [
	"NovaConfig",
	"DEFAULT_SYSTEM_PROMPT",
	"Fissure",
	"FissureField",
	"ConsciousnessFlow",
	"Embedder",
	"LocalLLM",
	"Note",
	"NotesBook",
	"Nova",
	"Daydreamer",
	"consolidate",
	"render_field",
	"save_field",
	"load_field",
	"VMAgent",
	"parse_actions",
	"strip_actions",
	"format_result",
	"CAPABILITY_MEMORIES",
]

__version__ = "0.7.0"
