"""nova v1.0：精简内核版。

陶土球 + 水流 + 外部工作区。

  - 大模型是处理器，不是数据库。
  - 记忆是会被使用本身改写的地形。
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
]

__version__ = "1.0.0"


# v1.1 public state helpers
try:
    from .task_state import TaskLedger, TaskState, Evidence
except Exception:  # keep package import tolerant during partial installs
    pass
