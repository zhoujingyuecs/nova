"""nova v1.3.1：陶土球 + 水流 + 程序性记忆 + 前语言念头层（无政策标签）+ 封印清单。

  - 大模型是处理器，不是数据库；甚至**不是 nova 唯一的思考器官**。
  - 联想记忆（FissureField）：被使用本身改写的地形。
  - 程序性记忆（HabitField）：被违反/强化反复重塑的硬约束——只在 tool 层挡动作。
  - 前语言念头（ThoughtCluster）：在 LLM 介入之前就已成型的念头团。
                                   LLM 只负责"翻译可说的部分"。
                                   **不带任何政策标签**——什么都可以浮起来，
                                   什么都可以翻译成话。
  - 语言门（LanguageGate）：决定这一 tick 要不要调 LLM。
  - 封印清单（SealRegistry）：nova **自己**写下的"暂时不想展开的念头类别"清单。
                              不挡说话、不挡动作，只让 prompt 里那一团内容不展开。
                              可以随时 <seal> 加进去，随时 <unseal> 拿掉。
  - 工作区：事实和脚本住在外部文本文件里。

# v1.3.1 vs v1.3 第一版

v1.3 第一版给 cluster 加了 render_policy / action_policy 用关键字规则
和 habit 自动打标签——结果几乎所有念头被打成 forbid，nova 反而更不自由。
v1.3.1 删掉了那套：
  - cluster 没有政策字段
  - clay_tick 不依赖 habit_field
  - 动作管制只在 tool 层（HabitGate）做
  - 是否说话由 LanguageGate 决定，看新颖度 / 模式 / 压力
  - 念头的好恶 / 紧张 / 行动压力**只**从地形（裂缝的 kind / epistemic_state
    / unresolved）读，**不**扫描文本
  - 如果 nova 想暂时不展开某类念头，她**自己**写 <seal> 块；随时可以 <unseal> 拿掉
"""
from .config import NovaConfig, DEFAULT_SYSTEM_PROMPT
from .fissure import Fissure
from .field import FissureField
from .flow import ConsciousnessFlow
from .embedder import Embedder
from .llm import LocalLLM
from .self_state import SelfState
from .workspace import Workspace
from .mind import Nova
from .sleep import consolidate
from .visualize import render_field
from .persistence import save_field, load_field
from .tools import (
    VMAgent,
    parse_actions,
    strip_actions,
    format_result,
)

# Continuous Runtime
from .agenda import Agenda, AgendaItem
from .worklog import WorkLog, WorkEvent
from .executive import (
    ExecutiveController,
    Decision,
    build_goal_prompt,
    build_reflection_prompt,
    build_orientation_prompt,
)
from .runtime import ContinuousRuntime

# v1.1：程序性记忆 / 习惯回路
from .habits import (
    HabitRule,
    HabitField,
    HabitGate,
    detect_reinforcement_signal,
    extract_rule_blocks,
    strip_rule_blocks,
    parse_rule_block,
    SOURCE_USER,
    SOURCE_SELF,
    SOURCE_SYSTEM,
    SOURCE_REINFORCED,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_SUPERSEDED,
)

# v1.3.1：前语言念头层（无政策标签）+ 封印清单
from .thought import ThoughtCluster, fissure_fingerprint
from .clay_tick import ClayTickEngine
from .language_gate import LanguageGate, GateDecision
from .seal import (
    SealEntry,
    SealRegistry,
    extract_seal_blocks,
    strip_seal_blocks,
)

__all__ = [
    "NovaConfig", "DEFAULT_SYSTEM_PROMPT",
    "Fissure", "FissureField", "ConsciousnessFlow",
    "Embedder", "LocalLLM",
    "SelfState", "Workspace", "Nova",
    "consolidate", "render_field",
    "save_field", "load_field",
    "VMAgent", "parse_actions", "strip_actions", "format_result",
    "Agenda", "AgendaItem",
    "WorkLog", "WorkEvent",
    "ExecutiveController", "Decision",
    "build_goal_prompt", "build_reflection_prompt", "build_orientation_prompt",
    "ContinuousRuntime",
    # v1.1
    "HabitRule", "HabitField", "HabitGate",
    "detect_reinforcement_signal",
    "extract_rule_blocks", "strip_rule_blocks", "parse_rule_block",
    "SOURCE_USER", "SOURCE_SELF", "SOURCE_SYSTEM", "SOURCE_REINFORCED",
    "STATUS_ACTIVE", "STATUS_ARCHIVED", "STATUS_SUPERSEDED",
    # v1.3.1
    "ThoughtCluster", "fissure_fingerprint",
    "ClayTickEngine",
    "LanguageGate", "GateDecision",
    "SealEntry", "SealRegistry",
    "extract_seal_blocks", "strip_seal_blocks",
]

__version__ = "1.3.1"


# v1.1 public state helpers
try:
    from .task_state import TaskLedger, TaskState, Evidence
except Exception:  # keep package import tolerant during partial installs
    pass
