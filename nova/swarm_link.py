"""swarm_link.py —— 节点端的 swarm 链路层（v1.4 新增）

# 职责

  - 维护与 page.py（swarm hub）的 socketio 通道
  - 自身订阅 swarm 相关事件，把它们投到 Nova 的注意力
  - 提供发送原语：广播记忆 / 共享 agenda / 发起回忆查询 / 发起行动提案 / 投票

# 设计要点

  ☆ 链路与协议分离：协议（`swarm.py`）定义"什么消息"；链路（这个文件）
    负责"怎么送、怎么收、收到后塞到哪儿"。
  ☆ 链路与 runtime 之间用线程安全的队列对接。runtime tick 在合适时机
    （perceive / 主线推进的间隙）消化队列里的事件。这样不打断主意识。
  ☆ 节点 ID 在 NodeProfile 里持久化（落到 field_path/node_id.txt），重启
    不换身份。
  ☆ 兼容 v1.3.1：v1.3.1 的 page.py 协议仍走原来的 new_chat_task /
    chat_result。SwarmLink 只新增 swarm_* 事件，不修改旧协议。

# 重要的事

  - SwarmLink 不会自己改 nova 的脑子。它只把外面的事件丢给一个
    `inbox` 队列，由 nova 在合适时机自己读。
  - 反向：SwarmLink 暴露 publish_*() 方法，nova（或 runtime）需要广播
    时调用即可——它就只是包装一下 socketio.emit。
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable, Optional

from .swarm import (
    EVT_ACTION_PROPOSE, EVT_ACTION_PROPOSED, EVT_ACTION_RESOLVED,
    EVT_ACTION_VOTE,
    EVT_AGENDA_SYNC, EVT_HEARTBEAT, EVT_HELLO,
    EVT_MEMORY_ECHO, EVT_MESSAGE, EVT_MESSAGE_IN,
    EVT_PEER_JOINED, EVT_PEER_LEFT,
    EVT_PROGRESS,
    EVT_RECALL_QUERY, EVT_RECALL_QUERY_IN,
    EVT_RECALL_RESPONSE, EVT_RECALL_RESPONSE_IN,
    EVT_SHARE_AGENDA, EVT_SHARE_MEMORY, EVT_WELCOME,
    NodeProfile, HeartbeatPayload,
    MemoryEcho, SharedAgendaItem, RecallQuery, ActionProposal,
)


# 收到的 swarm 事件统一打包成这种结构丢进 inbox
class SwarmEvent:
    __slots__ = ("kind", "payload", "ts")

    def __init__(self, kind: str, payload: dict[str, Any]):
        self.kind = kind
        self.payload = payload or {}
        self.ts = time.time()

    def __repr__(self) -> str:
        return f"<SwarmEvent kind={self.kind} ts={self.ts:.0f}>"


class SwarmLink:
    """一个节点与 page.py 总线之间的所有 swarm 协议交互。

    用法：
        link = SwarmLink(sio, profile)
        link.bind()                 # 在传入的 socketio Client 上挂事件
        link.hello()                # 注册自己
        ...
        ev = link.poll(timeout=0)   # 取下一条 swarm 事件
    """

    def __init__(
        self,
        sio: Any,                       # socketio.Client
        profile: NodeProfile,
        *,
        inbox_size: int = 256,
        peers_seen_limit: int = 64,
    ):
        self.sio = sio
        self.profile = profile
        self._inbox: collections.deque = collections.deque(maxlen=inbox_size)
        self._inbox_cv = threading.Condition()
        self._peers: dict[str, NodeProfile] = {}
        self._peers_limit = peers_seen_limit
        self._lock = threading.RLock()
        self._bound = False
        self._welcomed = False
        self._on_event_hook: Optional[Callable[[SwarmEvent], None]] = None

    # ==========================================================
    # 绑定 / 注册
    # ==========================================================
    def bind(self) -> None:
        """把所有 swarm_* 事件注册到 socketio 客户端上。"""
        if self._bound:
            return

        sio = self.sio

        @sio.on(EVT_WELCOME)
        def _on_welcome(data):
            self._on_welcome(data)

        @sio.on(EVT_PEER_JOINED)
        def _on_peer_joined(data):
            self._record_peer(data or {})
            self._push(EVT_PEER_JOINED, data or {})

        @sio.on(EVT_PEER_LEFT)
        def _on_peer_left(data):
            self._forget_peer((data or {}).get("node_id", ""))
            self._push(EVT_PEER_LEFT, data or {})

        @sio.on(EVT_MEMORY_ECHO)
        def _on_memory_echo(data):
            # 不要回声自己广播的——hub 会过滤一遍，但兜底再筛一次
            if (data or {}).get("origin_node_id") == self.profile.node_id:
                return
            self._push(EVT_MEMORY_ECHO, data or {})

        @sio.on(EVT_AGENDA_SYNC)
        def _on_agenda_sync(data):
            self._push(EVT_AGENDA_SYNC, data or {})

        @sio.on(EVT_RECALL_QUERY_IN)
        def _on_recall_query_in(data):
            if (data or {}).get("origin_node_id") == self.profile.node_id:
                return
            self._push(EVT_RECALL_QUERY_IN, data or {})

        @sio.on(EVT_RECALL_RESPONSE_IN)
        def _on_recall_response_in(data):
            self._push(EVT_RECALL_RESPONSE_IN, data or {})

        @sio.on(EVT_ACTION_PROPOSED)
        def _on_action_proposed(data):
            if (data or {}).get("proposer_node_id") == self.profile.node_id:
                # 自己的提案也回执给自己（这样 nova 看得到自己提案的回声）
                self._push(EVT_ACTION_PROPOSED, data or {})
                return
            self._push(EVT_ACTION_PROPOSED, data or {})

        @sio.on(EVT_ACTION_RESOLVED)
        def _on_action_resolved(data):
            self._push(EVT_ACTION_RESOLVED, data or {})

        @sio.on(EVT_MESSAGE_IN)
        def _on_message_in(data):
            self._push(EVT_MESSAGE_IN, data or {})

        self._bound = True

    def set_event_hook(self, hook: Optional[Callable[[SwarmEvent], None]]) -> None:
        """注册一个回调，每次有 swarm 事件进 inbox 时被调用（非阻塞）。"""
        self._on_event_hook = hook

    # ==========================================================
    # 出站（节点 → swarm）
    # ==========================================================
    def hello(self) -> bool:
        """注册自己。"""
        return self._emit_safe(EVT_HELLO, self.profile.to_dict())

    def heartbeat(self, payload: HeartbeatPayload) -> bool:
        return self._emit_safe(EVT_HEARTBEAT, payload.to_dict())

    def share_memory(self, echo: MemoryEcho) -> bool:
        return self._emit_safe(EVT_SHARE_MEMORY, echo.to_dict())

    def share_agenda(self, agenda: SharedAgendaItem) -> bool:
        return self._emit_safe(EVT_SHARE_AGENDA, agenda.to_dict())

    def report_progress(self, agenda_id: str, *, summary: str,
                        next_action: str = "", status: str = "active",
                        evidence: str = "") -> bool:
        return self._emit_safe(EVT_PROGRESS, {
            "agenda_id": agenda_id,
            "node_id": self.profile.node_id,
            "node_name": self.profile.node_name,
            "summary": summary,
            "next_action": next_action,
            "status": status,
            "evidence": evidence,
            "ts": time.time(),
        })

    def issue_recall_query(self, query: RecallQuery) -> bool:
        return self._emit_safe(EVT_RECALL_QUERY, query.to_dict())

    def reply_recall(self, query_id: str, echoes: list[MemoryEcho]) -> bool:
        return self._emit_safe(EVT_RECALL_RESPONSE, {
            "query_id": query_id,
            "responder_node_id": self.profile.node_id,
            "responder_node_name": self.profile.node_name,
            "echoes": [e.to_dict() for e in echoes],
            "ts": time.time(),
        })

    def propose_action(self, proposal: ActionProposal) -> bool:
        return self._emit_safe(EVT_ACTION_PROPOSE, proposal.to_dict())

    def vote(self, proposal_id: str, vote: str, reason: str = "") -> bool:
        return self._emit_safe(EVT_ACTION_VOTE, {
            "proposal_id": proposal_id,
            "voter_node_id": self.profile.node_id,
            "voter_node_name": self.profile.node_name,
            "vote": vote,
            "reason": reason,
            "ts": time.time(),
        })

    def send_message(self, to_node_id: str, text: str,
                     in_reply_to: str = "") -> bool:
        return self._emit_safe(EVT_MESSAGE, {
            "from_node_id": self.profile.node_id,
            "from_node_name": self.profile.node_name,
            "to_node_id": to_node_id,
            "text": text,
            "in_reply_to": in_reply_to,
            "ts": time.time(),
        })

    # ==========================================================
    # 入站：inbox 队列
    # ==========================================================
    def poll(self, timeout: float = 0.0) -> Optional[SwarmEvent]:
        with self._inbox_cv:
            if self._inbox:
                return self._inbox.popleft()
            if timeout <= 0:
                return None
            self._inbox_cv.wait(timeout)
            if self._inbox:
                return self._inbox.popleft()
            return None

    def poll_all(self) -> list[SwarmEvent]:
        with self._inbox_cv:
            events = list(self._inbox)
            self._inbox.clear()
            return events

    def pending(self) -> int:
        return len(self._inbox)

    # ==========================================================
    # 状态查询
    # ==========================================================
    def is_welcomed(self) -> bool:
        return self._welcomed

    def peers(self) -> list[NodeProfile]:
        with self._lock:
            return list(self._peers.values())

    def peer(self, node_id: str) -> Optional[NodeProfile]:
        with self._lock:
            return self._peers.get(node_id)

    # ==========================================================
    # 内部
    # ==========================================================
    def _emit_safe(self, event: str, payload: dict[str, Any]) -> bool:
        try:
            if not getattr(self.sio, "connected", True):
                return False
            self.sio.emit(event, payload)
            return True
        except Exception as e:
            # 发送失败不抛——让上层 runtime 把"未投递"也算作正常情况
            print(f"⚠️ swarm 发送 {event} 失败：{e}")
            return False

    def _on_welcome(self, data: dict[str, Any]) -> None:
        self._welcomed = True
        peers = (data or {}).get("peers") or []
        with self._lock:
            self._peers.clear()
            for p in peers:
                try:
                    nid = (p or {}).get("node_id", "")
                    if not nid or nid == self.profile.node_id:
                        continue
                    self._peers[nid] = NodeProfile.from_dict(p)
                except Exception:
                    continue
        self._push(EVT_WELCOME, data or {})

    def _record_peer(self, data: dict[str, Any]) -> None:
        nid = data.get("node_id") or ""
        if not nid or nid == self.profile.node_id:
            return
        with self._lock:
            try:
                self._peers[nid] = NodeProfile.from_dict(data)
            except Exception:
                pass
            # 防止 peer 表无限增长
            if len(self._peers) > self._peers_limit:
                # 把最老的 hello 时间踢掉
                oldest = sorted(
                    self._peers.values(),
                    key=lambda p: getattr(p, "started_at", 0)
                )[0]
                self._peers.pop(oldest.node_id, None)

    def _forget_peer(self, node_id: str) -> None:
        if not node_id:
            return
        with self._lock:
            self._peers.pop(node_id, None)

    def _push(self, kind: str, payload: dict[str, Any]) -> None:
        ev = SwarmEvent(kind, payload)
        with self._inbox_cv:
            self._inbox.append(ev)
            self._inbox_cv.notify()
        hook = self._on_event_hook
        if hook is not None:
            try:
                hook(ev)
            except Exception:
                pass
