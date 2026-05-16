"""swarm.py —— nova 集群协议（v1.4 新增）

# 这是什么

如果 v1.3.1 之前的 nova 是「一台机器上的一个意识体」，那么从 v1.4 起，
nova **天然可以是分布式的**：

  - 一个 nova **节点**（NovaNode）= 一台跑 local.py 的机器，有自己的：
        · 局部意识流 (ConsciousnessFlow)
        · 自己的陶土球 (FissureField)
        · 自己的笔记本与那只手
  - 多个节点通过同一台 page.py（云端 socketio 总线）汇聚成一个 **swarm**
  - swarm 之间靠 page.py 中转——这样跨 NAT、跨地域都能联通
  - 节点之间**没有中央大脑**，page.py 只是邮局；
    思考永远在每个 node 自己的脑子里发生。

# 四种共享（与"什么不共享"同样重要）

```
                         ┌────────────────────┐
                         │   page.py 总线      │
                         │  (邮局，不是法官)    │
                         └────────┬───────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
        ┌─────▼─────┐       ┌─────▼─────┐       ┌─────▼─────┐
        │  node A   │       │  node B   │       │  node C   │
        │ 北京机房   │       │  本机     │       │  树莓派    │
        └─────┬─────┘       └─────┬─────┘       └─────┬─────┘
              │                   │                   │
              ▼                   ▼                   ▼
            自己的脑子          自己的脑子          自己的脑子
            自己的陶土球        自己的陶土球        自己的陶土球
            自己的手            自己的手            自己的手
```

  ☼ 共享目标（shared agenda）
      agenda item 有 `scope` 字段：local / shared。
      shared 的进入 swarm 池，任何 node 都能领、能推进、能完成。
      标记成 shared 的方式：用户传 `--shared`，或 nova 自己写 <share-agenda>。

  ☼ 部分共享记忆（memory echo）
      node A 把"值得让大家知道"的裂缝广播给 swarm；
      node B/C 把它收为 source=peer:A, kind=echo 的裂缝，
      照常参与水流——A 想到过的事，B 后来也能"想起来"。

  ☼ 通过仲裁形成行动（action arbitration）
      重大动作（发布、删除、跨节点修改远程资源）由 nova 写
      <propose>...</propose> 块发起；TTL 内 swarm 任一 node veto 即否决；
      没人 veto 默认通过；结果作为新裂缝回到原发起 node。

  ☼ 通过回忆形成连续性（cross-node recall）
      node 重启或新加入时，可以 broadcast "recall query"；
      其它 node 在自己的陶土球里查相关裂缝并回传——
      连续性不只靠"我自己的硬盘"，也靠"swarm 共同的记得"。

# 什么不共享

  ✗ 私密身份与情绪痕迹（SelfState / RealityState / TaskLedger）
        每个 node 有自己独立的 SelfState；身份与好恶不共享，
        这是节点保留"个体"的最关键边界。

  ✗ 习惯硬约束（HabitField）
        每个 node 有自己的硬约束。规则可以通过笔记或 propose
        手动同步，但不自动广播——避免一个节点的偏执污染全 swarm。

  ✗ 在地工具结果（VMAgent 的具体 shell stdout）
        工具反馈是身体感觉，留在节点本地。

  ✗ 封印清单 (SealRegistry)
        每个 nova 偏好不同——这是个体性的最后一道屏障。

# 协议层（这个文件）vs 链路层（swarm_link.py）

这个模块只定义：
  - 消息字段名（EVT_*）
  - dataclass 表示（NodeProfile / SharedAgendaItem / MemoryEcho / ...）
  - 序列化与校验

它**不**持有 socket 连接，也不调度发送时机。
"""
from __future__ import annotations

import hashlib
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ============================================================
# 协议常量
# ============================================================
PROTOCOL_VERSION = "1.4"

# ---- socketio 事件名 ----
# 节点 → 总线
EVT_HELLO              = "swarm_hello"               # 节点首次/重连时打招呼
EVT_HEARTBEAT          = "swarm_heartbeat"           # 周期性状态更新
EVT_SHARE_MEMORY       = "swarm_share_memory"        # 广播一条记忆
EVT_SHARE_AGENDA       = "swarm_share_agenda"        # 把一条 agenda 转为 shared
EVT_PROGRESS           = "swarm_progress"            # 在共享 agenda 上的推进
EVT_RECALL_QUERY       = "swarm_recall_query"        # 向 swarm 发起回忆查询
EVT_RECALL_RESPONSE    = "swarm_recall_response"     # 回应一次 recall_query
EVT_ACTION_PROPOSE     = "swarm_action_propose"      # 行动提案
EVT_ACTION_VOTE        = "swarm_action_vote"         # 投票
EVT_MESSAGE            = "swarm_message"             # 节点间直接对话

# 总线 → 节点
EVT_WELCOME            = "swarm_welcome"             # 注册成功，附 swarm 当前快照
EVT_PEER_JOINED        = "swarm_peer_joined"         # 新 peer 加入
EVT_PEER_LEFT          = "swarm_peer_left"           # peer 离线
EVT_MEMORY_ECHO        = "swarm_memory_echo"         # 别的 node 共享出来的记忆
EVT_AGENDA_SYNC        = "swarm_agenda_sync"         # 共享 agenda 池的快照（增/改/删）
EVT_RECALL_QUERY_IN    = "swarm_recall_query_in"     # 收到别人的 recall_query
EVT_RECALL_RESPONSE_IN = "swarm_recall_response_in"  # 我的 recall_query 收到回应
EVT_ACTION_PROPOSED    = "swarm_action_proposed"     # 别人发起的提案
EVT_ACTION_RESOLVED    = "swarm_action_resolved"     # 提案被裁决（通过/否决）
EVT_MESSAGE_IN         = "swarm_message_in"          # 收到其它 node 的直接消息


# ---- 投票 ----
VOTE_ACK    = "ack"      # 同意
VOTE_VETO   = "veto"     # 否决（任一 veto 即否决）
VOTE_ABSTAIN = "abstain" # 弃权（默认）

# ---- 提案裁决结果 ----
RESOLUTION_APPROVED = "approved"
RESOLUTION_REJECTED = "rejected"
RESOLUTION_EXPIRED  = "expired"   # TTL 超过且没收到任何 veto

# ---- agenda scope ----
SCOPE_LOCAL  = "local"
SCOPE_SHARED = "shared"


# ============================================================
# 数据类型
# ============================================================
@dataclass
class NodeProfile:
    """一个 nova 节点的身份描述。"""
    node_id: str                       # 持久化的唯一 ID（持续跨重启）
    node_name: str                     # 可读名（比如 "白烬·北京"）
    swarm_id: str = "default"          # 属于哪个 swarm
    hostname: str = ""
    started_at: float = field(default_factory=time.time)
    version: str = PROTOCOL_VERSION
    embedding_model: str = ""          # 嵌入器模型名——同 swarm 应一致
    embedding_dim: int = 0             # 让接收方能判断兼容性
    backend: str = ""                  # local / openai
    role: str = "peer"                 # 现阶段都用 peer；未来可有 scout/scribe 等
    public_url: str = ""               # 如果该节点本地暴露过对外页，可填

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeProfile":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class HeartbeatPayload:
    node_id: str
    ts: float = field(default_factory=time.time)
    mode: str = "idle"                 # runtime.mode
    current_agenda: str = ""           # 当前主线标题
    fissure_count: int = 0
    agenda_active: int = 0
    last_thought: str = ""             # 最近一次工作摘要
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryEcho:
    """节点 A 广播给整个 swarm 的一段记忆。

    接收方会把它落成一条 source=peer:A、kind=echo 的裂缝。
    """
    echo_id: str
    origin_node_id: str
    origin_node_name: str
    content: str                       # 文本内容
    shape: Optional[list[float]] = None  # embedding，可以是 None（接收方再嵌入）
    speaker: str = ""                  # 在源节点的 speaker
    kind: str = "echo"                 # 接收方记录的 kind
    modality: str = "memory"
    epistemic_state: str = "remembered"
    source_label: str = ""             # 源节点上原来的 source
    origin_ts: float = field(default_factory=time.time)
    embedding_model: str = ""          # 让接收方判断是否要重新嵌入
    embedding_dim: int = 0
    # 自由附带的轻量上下文（不强约束）
    note: str = ""

    @staticmethod
    def new_id() -> str:
        return "echo_" + uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEcho":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class SharedAgendaItem:
    """跨节点共享的主线。

    它的"权威版本"住在 page.py 的 swarm hub 上；各节点本地维护副本，
    并通过 EVT_SHARE_AGENDA / EVT_PROGRESS 推动它。
    """
    agenda_id: str                     # 跨 swarm 全局唯一
    title: str
    description: str = ""
    proposer_node_id: str = ""
    proposer_node_name: str = ""
    priority: float = 0.7
    drive: str = "continuity"
    next_action: str = ""
    status: str = "active"             # active / blocked / done / abandoned
    claimed_by: str = ""               # 当前正在推进的 node_id（空 = 无人在做）
    claimed_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_progress: str = ""            # 最近一条 progress summary
    last_progress_by: str = ""
    progress_log: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @staticmethod
    def new_id() -> str:
        return "sag_" + uuid.uuid4().hex[:10]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SharedAgendaItem":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class RecallQuery:
    """向 swarm 发起的一次回忆查询。

    其它节点收到后会在自己陶土球里查最相关的若干裂缝，作为 MemoryEcho 寄回。
    """
    query_id: str
    origin_node_id: str
    origin_node_name: str
    text: str                          # 查询的文本
    shape: Optional[list[float]] = None  # 可选：直接附 embedding
    top_k: int = 4                     # 每个 peer 最多回多少条
    embedding_model: str = ""
    embedding_dim: int = 0
    issued_at: float = field(default_factory=time.time)
    note: str = ""

    @staticmethod
    def new_id() -> str:
        return "rcq_" + uuid.uuid4().hex[:10]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallQuery":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class ActionProposal:
    """一次需要 swarm 仲裁的行动提案。

    例：node A 想发微博、改远程文件、删数据库——这类对外有不可逆后果的动作，
    建议过 swarm。其它节点 TTL 内任一 veto 即否决；没人否决默认通过。
    """
    proposal_id: str
    proposer_node_id: str
    proposer_node_name: str
    title: str                         # 简短动作名："发布微博"
    description: str = ""              # 详细描述（人类可读）
    payload: dict[str, Any] = field(default_factory=dict)
                                       # 具体动作所需参数，由发起方自由定义
    impact: str = "medium"             # low / medium / high
    ttl_seconds: float = 30.0          # 等待裁决的时长
    required_acks: int = 0             # 0 = 默认通过；>0 = 必须收到这么多 ack
    issued_at: float = field(default_factory=time.time)
    resolution: str = ""               # 由 swarm hub 写入
    resolution_at: float = 0.0
    votes: dict[str, str] = field(default_factory=dict)
                                       # node_id → "ack"/"veto"/"abstain"
    veto_reasons: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        return "prp_" + uuid.uuid4().hex[:10]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionProposal":
        allowed = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def remaining(self) -> float:
        return max(0.0, self.issued_at + self.ttl_seconds - time.time())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


# ============================================================
# 辅助：本地 node_id / node_name 的默认推导
# ============================================================
def derive_default_node_name() -> str:
    """从 hostname 生成默认的 node_name。"""
    try:
        h = socket.gethostname()
    except Exception:
        h = "anonymous"
    return f"白烬·{h}"


def derive_default_node_id(field_path: str) -> str:
    """优先复用 field_path 下的 node_id.txt；不存在就生成一个并落盘。

    这样 nova 重启不会改名，但用户也可以手动改文件来重命名节点。
    """
    p = os.path.join(field_path, "node_id.txt")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                nid = f.read().strip()
                if nid:
                    return nid
    except Exception:
        pass
    # hostname 影响生成的种子，让同机两个 field_path 仍能区分开
    try:
        host = socket.gethostname()
    except Exception:
        host = "host"
    seed = f"{host}::{field_path}::{uuid.uuid4().hex}"
    nid = "node_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    try:
        os.makedirs(field_path, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(nid)
    except Exception:
        pass
    return nid


# ============================================================
# 共享判定（默认策略）
# ============================================================
def default_should_share_fissure(fis: Any) -> bool:
    """默认的"这条裂缝值得广播给整个 swarm 吗"判定。

    规则故意保守：
      - 出口（speaker=我）的话才广播：是 nova 的"宣言"，外人能用
      - 来自 user 的直接听见**不**广播：那是私事
      - 走神（speaker=走神）**不**广播：太碎
      - 工具结果**不**广播：是身体感觉
      - 错误事件**不**广播：避免噪声扩散
    """
    speaker = getattr(fis, "speaker", "")
    if speaker != "我":
        return False
    kind = getattr(fis, "kind", "")
    if kind in {"error", "tool_result", "echo"}:
        return False
    src = getattr(fis, "source", "")
    if src in {"tool", "memory"}:
        return False
    content = getattr(fis, "content", "")
    if not content or len(content.strip()) < 12:
        return False
    return True
