"""nova v1.4：从单节点意识体 → 集群意志。

# 思路演化

  v1.0   陶土球（FissureField）+ 水流（ConsciousnessFlow）：联想记忆。
  v1.1   程序性记忆（HabitField）：硬约束，只在 tool 层挡动作。
  v1.2   云端 LLM 后端 + 工程化（启动器 / 一键脚本）。
  v1.3.1 前语言念头层（ThoughtCluster）+ 封印清单（Seal）。
         念头先有，话后到；语言门决定是否调 LLM。
  v1.4   ★ 集群意志（swarm）★：多 nova 通过 page.py 联结成一个意志：
           - 各自拥有局部意识流
           - 共享一组目标
           - 共享部分记忆
           - 通过仲裁形成行动
           - 通过回忆形成连续性

# 不变的核心

  - 大模型是处理器，不是数据库；甚至**不是 nova 唯一的思考器官**。
  - 联想记忆（FissureField）：被使用本身改写的地形。
  - 程序性记忆（HabitField）：被违反/强化反复重塑的硬约束。
  - 前语言念头（ThoughtCluster）：LLM 介入之前已成型的念头团。
  - 工作区：事实和脚本住在外部文本文件里。

# v1.4 加的不仅是"几台机器"

  集群意志不是"在多个机器上运行同一个 nova"。每个节点保留：
    * 独立的局部意识流
    * 独立的 SelfState（身份、好恶、最近、未完）
    * 独立的硬约束和封印清单
    * 独立的那只手和工作区

  共享的只有四样东西：
    1) 共享目标（shared agenda）
    2) 部分共享记忆（memory echo）
    3) 通过仲裁形成的行动（propose / vote）
    4) 通过回忆形成的连续性（cross-node recall）

  swarm 之间通过 page.py（公网入口）中转——这样跨 NAT、跨地域都能联通。
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

# v1.4：集群意志（swarm）—— 跨物理机的多 nova 联合
from .swarm import (
    NodeProfile, HeartbeatPayload,
    MemoryEcho, SharedAgendaItem, RecallQuery, ActionProposal,
    PROTOCOL_VERSION as SWARM_PROTOCOL_VERSION,
    VOTE_ACK, VOTE_VETO, VOTE_ABSTAIN,
    RESOLUTION_APPROVED, RESOLUTION_REJECTED, RESOLUTION_EXPIRED,
    SCOPE_LOCAL, SCOPE_SHARED,
    derive_default_node_id, derive_default_node_name,
)
from .swarm_link import SwarmLink, SwarmEvent
from .swarm_hub import SwarmHub
from .swarm_integration import (
    SwarmAdapter,
    ParsedSwarmDirectives,
    parse_swarm_directives,
    strip_swarm_tags,
    absorb_memory_echo,
    collect_recall_response,
    mirror_shared_agenda_into_local,
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
    # v1.4 — swarm
    "NodeProfile", "HeartbeatPayload",
    "MemoryEcho", "SharedAgendaItem", "RecallQuery", "ActionProposal",
    "SWARM_PROTOCOL_VERSION",
    "VOTE_ACK", "VOTE_VETO", "VOTE_ABSTAIN",
    "RESOLUTION_APPROVED", "RESOLUTION_REJECTED", "RESOLUTION_EXPIRED",
    "SCOPE_LOCAL", "SCOPE_SHARED",
    "derive_default_node_id", "derive_default_node_name",
    "SwarmLink", "SwarmEvent",
    "SwarmHub",
    "SwarmAdapter", "ParsedSwarmDirectives",
    "parse_swarm_directives", "strip_swarm_tags",
    "absorb_memory_echo", "collect_recall_response",
    "mirror_shared_agenda_into_local",
]

__version__ = "1.4.0"


# v1.1 public state helpers
try:
    from .task_state import TaskLedger, TaskState, Evidence
except Exception:  # keep package import tolerant during partial installs
    pass
