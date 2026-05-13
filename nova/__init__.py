"""nova v1.1：精简内核版 + 程序性记忆。

陶土球 + 水流 + 外部工作区 + 习惯回路。

  - 大模型是处理器，不是数据库。
  - 记忆是会被使用本身改写的地形（联想记忆 / FissureField）。
  - 规则是会被违反 / 强化反复重塑的硬约束（程序性记忆 / HabitField）。
  - 事实和脚本住在外部工作区里，脑子里只放形状和当下意识。
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
]

__version__ = "1.1.0"


# v1.1 public state helpers
try:
    from .task_state import TaskLedger, TaskState, Evidence
except Exception:  # keep package import tolerant during partial installs
    pass
