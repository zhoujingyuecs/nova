"""swarm_hub.py —— page.py 端的 swarm 总线（v1.4 新增）

# 在哪运行

它跑在 page.py 进程里（云服务器上，有公网 IP）。
作为 socketio 的"邮局"：node 们连进来，hub 中继它们之间的消息。

# 它做什么

  - 维护"当前在线的 node 列表"（NodeProfile）
  - 中继 share_memory / recall_query / 提案 / 节点间消息
  - 维护共享 agenda 池的权威副本（落盘 + 广播 sync）
  - 裁决行动提案（收 veto / 收 ack；TTL 到期自动 expired→approved）
  - 把 swarm 状态以 JSON 暴露给前端 UI 展示集群拓扑

# 它不做什么

  - 不思考、不调用 LLM
  - 不修改任何 node 的内部状态——node 把广播当作"外部信号"，
    自己决定怎么消化
  - 不当法官：仲裁规则只是"任一 veto 即否决，否则 TTL 后通过"，
    集群行动的判断权仍在 node 自己手里

# 兼容 v1.3.1 的 page.py

  page.py 原有的 dispatch_task(访客对话) 协议**不变**。这个 hub 用一组
  EVT_SWARM_* 事件名挂在同一个 socketio 上，互不干扰。
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .swarm import (
    EVT_ACTION_PROPOSE, EVT_ACTION_PROPOSED, EVT_ACTION_RESOLVED,
    EVT_ACTION_VOTE,
    EVT_AGENDA_SYNC, EVT_HEARTBEAT, EVT_HELLO,
    EVT_MEMORY_ECHO, EVT_MESSAGE, EVT_MESSAGE_IN,
    EVT_PEER_JOINED, EVT_PEER_LEFT,
    EVT_PROGRESS,
    EVT_RECALL_QUERY, EVT_RECALL_QUERY_IN,
    EVT_RECALL_RESPONSE, EVT_RECALL_RESPONSE_IN,
    EVT_SHARE_AGENDA, EVT_SHARE_MEMORY,
    EVT_WELCOME,
    NodeProfile, SharedAgendaItem, ActionProposal,
    RESOLUTION_APPROVED, RESOLUTION_REJECTED, RESOLUTION_EXPIRED,
    VOTE_ACK, VOTE_VETO, VOTE_ABSTAIN,
)


# ============================================================
# 数据存储
# ============================================================
@dataclass
class _OnlineNode:
    sid: str                           # socketio 客户端 id
    profile: NodeProfile
    connected_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    last_status: dict[str, Any] = field(default_factory=dict)
    # 心跳来的运行时摘要
    mode: str = "idle"
    current_agenda: str = ""
    fissure_count: int = 0
    agenda_active: int = 0
    last_thought: str = ""


class SwarmHub:
    """page.py 进程里的 swarm 总线状态机。"""

    def __init__(
        self,
        socketio: Any,
        *,
        data_dir: str = "./swarm_data",
        broadcast_history: int = 200,
        proposal_history: int = 100,
        # 多久没收到心跳就当离线
        stale_after_seconds: float = 90.0,
    ):
        self.sio = socketio
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.broadcast_history = broadcast_history
        self.proposal_history = proposal_history
        self.stale_after_seconds = stale_after_seconds

        self._lock = threading.RLock()

        # sid → _OnlineNode
        self._nodes_by_sid: dict[str, _OnlineNode] = {}
        # node_id → sid（一个 node_id 同时只允许一个 sid 在线；新的踢旧的）
        self._sid_by_node: dict[str, str] = {}

        # 共享 agenda 池：agenda_id → SharedAgendaItem
        self._shared_agendas: dict[str, SharedAgendaItem] = {}
        self._agenda_path = os.path.join(data_dir, "shared_agendas.json")
        self._load_agendas()

        # 进行中的提案：proposal_id → ActionProposal
        self._proposals: dict[str, ActionProposal] = {}
        # 历史（已裁决）
        self._proposal_history_log: collections.deque = collections.deque(
            maxlen=proposal_history
        )

        # 最近的广播事件（给前端展示用）
        self._recent_events: collections.deque = collections.deque(
            maxlen=broadcast_history
        )

        # 后台线程：定期清理过期提案 / 标记离线节点
        self._stop_event = threading.Event()
        self._janitor = threading.Thread(
            target=self._janitor_loop,
            daemon=True,
            name="swarm-hub-janitor",
        )
        self._janitor.start()

    # ==========================================================
    # 注册到 socketio
    # ==========================================================
    def bind(self) -> None:
        """把所有 swarm_* 事件绑到 socketio。"""
        sio = self.sio

        @sio.on(EVT_HELLO)
        def _on_hello(data):
            self._on_hello(data)

        @sio.on(EVT_HEARTBEAT)
        def _on_heartbeat(data):
            self._on_heartbeat(data)

        @sio.on(EVT_SHARE_MEMORY)
        def _on_share_memory(data):
            self._on_share_memory(data)

        @sio.on(EVT_SHARE_AGENDA)
        def _on_share_agenda(data):
            self._on_share_agenda(data)

        @sio.on(EVT_PROGRESS)
        def _on_progress(data):
            self._on_progress(data)

        @sio.on(EVT_RECALL_QUERY)
        def _on_recall_query(data):
            self._on_recall_query(data)

        @sio.on(EVT_RECALL_RESPONSE)
        def _on_recall_response(data):
            self._on_recall_response(data)

        @sio.on(EVT_ACTION_PROPOSE)
        def _on_action_propose(data):
            self._on_action_propose(data)

        @sio.on(EVT_ACTION_VOTE)
        def _on_action_vote(data):
            self._on_action_vote(data)

        @sio.on(EVT_MESSAGE)
        def _on_message(data):
            self._on_message(data)

    # 在 socketio 的 disconnect 里调
    def on_disconnect(self, sid: str) -> None:
        with self._lock:
            node = self._nodes_by_sid.pop(sid, None)
            if node is None:
                return
            if self._sid_by_node.get(node.profile.node_id) == sid:
                self._sid_by_node.pop(node.profile.node_id, None)
        self.sio.emit(EVT_PEER_LEFT, {
            "node_id": node.profile.node_id,
            "node_name": node.profile.node_name,
            "ts": time.time(),
        })
        self._record_event("peer_left", {
            "node_id": node.profile.node_id,
            "node_name": node.profile.node_name,
        })

    # ==========================================================
    # 事件 handlers
    # ==========================================================
    def _on_hello(self, data: dict[str, Any]) -> None:
        from flask import request   # 仅在被 page.py 调用时上下文里有
        sid = request.sid
        if not data:
            return
        try:
            profile = NodeProfile.from_dict(data)
        except Exception as e:
            print(f"⚠️ swarm: 拒绝畸形的 hello: {e}")
            return

        with self._lock:
            # 如果同一 node_id 已经在另一个 sid 上，踢掉旧的
            old_sid = self._sid_by_node.get(profile.node_id)
            if old_sid and old_sid != sid:
                old = self._nodes_by_sid.pop(old_sid, None)
                if old is not None:
                    try:
                        self.sio.emit(EVT_PEER_LEFT, {
                            "node_id": old.profile.node_id,
                            "node_name": old.profile.node_name,
                            "reason": "replaced",
                            "ts": time.time(),
                        })
                    except Exception:
                        pass
            self._sid_by_node[profile.node_id] = sid
            self._nodes_by_sid[sid] = _OnlineNode(sid=sid, profile=profile)
            # 给新人快照：peer 列表 + 共享 agenda + 进行中提案
            peer_list = [
                n.profile.to_dict()
                for n in self._nodes_by_sid.values()
                if n.sid != sid
            ]
            agendas_snapshot = [
                a.to_dict() for a in self._shared_agendas.values()
            ]
            proposals_snapshot = [
                p.to_dict() for p in self._proposals.values()
                if not p.expired()
            ]

        # 回新人欢迎包
        self.sio.emit(EVT_WELCOME, {
            "your_node_id": profile.node_id,
            "your_node_name": profile.node_name,
            "swarm_id": profile.swarm_id,
            "peers": peer_list,
            "shared_agendas": agendas_snapshot,
            "pending_proposals": proposals_snapshot,
            "ts": time.time(),
        }, room=sid)
        # 通告 swarm
        self.sio.emit(EVT_PEER_JOINED, profile.to_dict())
        self._record_event("peer_joined", {
            "node_id": profile.node_id,
            "node_name": profile.node_name,
        })
        print(f"🌌 swarm: 节点 {profile.node_name} ({profile.node_id[:10]}…) 上线，"
              f"当前共 {len(self._nodes_by_sid)} 节点")

    def _on_heartbeat(self, data: dict[str, Any]) -> None:
        nid = (data or {}).get("node_id", "")
        with self._lock:
            sid = self._sid_by_node.get(nid, "")
            node = self._nodes_by_sid.get(sid)
            if node is None:
                return
            node.last_heartbeat_at = time.time()
            node.mode = str(data.get("mode") or "idle")
            node.current_agenda = str(data.get("current_agenda") or "")
            node.fissure_count = int(data.get("fissure_count") or 0)
            node.agenda_active = int(data.get("agenda_active") or 0)
            node.last_thought = str(data.get("last_thought") or "")[:240]
            node.last_status = dict(data)

    def _on_share_memory(self, data: dict[str, Any]) -> None:
        # 转发给除原发者之外的所有 node
        origin = (data or {}).get("origin_node_id", "")
        self._broadcast_except(origin, EVT_MEMORY_ECHO, data)
        self._record_event("memory_echo", {
            "origin": (data or {}).get("origin_node_name", ""),
            "content_preview": (data or {}).get("content", "")[:80],
        })

    def _on_share_agenda(self, data: dict[str, Any]) -> None:
        try:
            item = SharedAgendaItem.from_dict(data)
        except Exception as e:
            print(f"⚠️ swarm: 畸形的 shared agenda: {e}")
            return
        with self._lock:
            existing = self._shared_agendas.get(item.agenda_id)
            if existing is None:
                self._shared_agendas[item.agenda_id] = item
                action = "added"
            else:
                # 字段级合并：保留较新的 progress_log；其余按提交者意愿覆盖
                merged_log = existing.progress_log + [
                    e for e in item.progress_log
                    if e not in existing.progress_log
                ]
                existing.title = item.title or existing.title
                existing.description = item.description or existing.description
                existing.priority = max(existing.priority, item.priority)
                existing.next_action = item.next_action or existing.next_action
                existing.status = item.status or existing.status
                existing.tags = list(set(existing.tags + item.tags))
                existing.progress_log = merged_log[-30:]
                existing.updated_at = time.time()
                action = "updated"
            self._save_agendas()
            snapshot = [a.to_dict() for a in self._shared_agendas.values()]
        self.sio.emit(EVT_AGENDA_SYNC, {
            "kind": action,
            "item": item.to_dict(),
            "snapshot": snapshot,
            "ts": time.time(),
        })
        self._record_event("agenda_" + action, {
            "agenda_id": item.agenda_id,
            "title": item.title,
            "from": item.proposer_node_name,
        })

    def _on_progress(self, data: dict[str, Any]) -> None:
        aid = (data or {}).get("agenda_id", "")
        with self._lock:
            item = self._shared_agendas.get(aid)
            if item is None:
                return
            item.last_progress = str(data.get("summary") or "")[:300]
            item.last_progress_by = str(data.get("node_name") or "")
            item.next_action = str(data.get("next_action") or item.next_action)
            status = str(data.get("status") or "").lower()
            if status in {"active", "blocked", "done", "abandoned"}:
                item.status = status
            item.updated_at = time.time()
            entry = {
                "ts": time.time(),
                "node_id": str(data.get("node_id") or ""),
                "node_name": str(data.get("node_name") or ""),
                "summary": item.last_progress,
                "status": item.status,
                "evidence": str(data.get("evidence") or "")[:300],
            }
            item.progress_log.append(entry)
            item.progress_log = item.progress_log[-30:]
            self._save_agendas()
            snapshot = [a.to_dict() for a in self._shared_agendas.values()]
        self.sio.emit(EVT_AGENDA_SYNC, {
            "kind": "progress",
            "item": item.to_dict(),
            "snapshot": snapshot,
            "ts": time.time(),
        })
        self._record_event("agenda_progress", {
            "agenda_id": aid,
            "title": item.title,
            "from": item.last_progress_by,
            "summary": item.last_progress,
        })

    def _on_recall_query(self, data: dict[str, Any]) -> None:
        origin = (data or {}).get("origin_node_id", "")
        self._broadcast_except(origin, EVT_RECALL_QUERY_IN, data)
        self._record_event("recall_query", {
            "from": (data or {}).get("origin_node_name", ""),
            "text": (data or {}).get("text", "")[:80],
        })

    def _on_recall_response(self, data: dict[str, Any]) -> None:
        # 只送给原发起者
        qid = (data or {}).get("query_id", "")
        # 我们不存 qid→origin_node_id 的映射（轻量起见），所以单播给
        # 所有在线 node，让发起方在客户端通过 query_id 过滤。
        # 为了节省带宽，可以以后改为索引；当前 swarm 规模都很小。
        self.sio.emit(EVT_RECALL_RESPONSE_IN, data)
        self._record_event("recall_response", {
            "from": (data or {}).get("responder_node_name", ""),
            "echoes": len((data or {}).get("echoes") or []),
        })

    def _on_action_propose(self, data: dict[str, Any]) -> None:
        try:
            prop = ActionProposal.from_dict(data)
        except Exception as e:
            print(f"⚠️ swarm: 畸形的 action proposal: {e}")
            return
        with self._lock:
            self._proposals[prop.proposal_id] = prop
        self.sio.emit(EVT_ACTION_PROPOSED, prop.to_dict())
        self._record_event("action_proposed", {
            "proposal_id": prop.proposal_id,
            "title": prop.title,
            "from": prop.proposer_node_name,
            "ttl": prop.ttl_seconds,
        })

    def _on_action_vote(self, data: dict[str, Any]) -> None:
        pid = (data or {}).get("proposal_id", "")
        voter = (data or {}).get("voter_node_id", "")
        vote = (data or {}).get("vote", VOTE_ABSTAIN)
        reason = (data or {}).get("reason", "")
        if vote not in {VOTE_ACK, VOTE_VETO, VOTE_ABSTAIN}:
            return
        resolved_payload: Optional[dict[str, Any]] = None
        with self._lock:
            prop = self._proposals.get(pid)
            if prop is None:
                return
            prop.votes[voter] = vote
            if vote == VOTE_VETO:
                prop.veto_reasons[voter] = reason
                prop.resolution = RESOLUTION_REJECTED
                prop.resolution_at = time.time()
                resolved_payload = prop.to_dict()
                self._proposals.pop(pid, None)
                self._proposal_history_log.append(prop.to_dict())
            elif prop.required_acks > 0:
                ack_count = sum(1 for v in prop.votes.values() if v == VOTE_ACK)
                if ack_count >= prop.required_acks:
                    prop.resolution = RESOLUTION_APPROVED
                    prop.resolution_at = time.time()
                    resolved_payload = prop.to_dict()
                    self._proposals.pop(pid, None)
                    self._proposal_history_log.append(prop.to_dict())
        if resolved_payload is not None:
            self.sio.emit(EVT_ACTION_RESOLVED, resolved_payload)
            self._record_event("action_resolved", {
                "proposal_id": pid,
                "title": resolved_payload.get("title", ""),
                "resolution": resolved_payload.get("resolution", ""),
            })

    def _on_message(self, data: dict[str, Any]) -> None:
        to = (data or {}).get("to_node_id", "")
        if not to:
            return
        with self._lock:
            sid = self._sid_by_node.get(to)
        if not sid:
            return
        self.sio.emit(EVT_MESSAGE_IN, data, room=sid)
        self._record_event("message", {
            "from": (data or {}).get("from_node_name", ""),
            "to": to,
            "preview": (data or {}).get("text", "")[:80],
        })

    # ==========================================================
    # Janitor：扫提案超时、扫离线
    # ==========================================================
    def _janitor_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            try:
                self._janitor_tick()
            except Exception as e:
                print(f"⚠️ swarm janitor 错：{e}")

    def _janitor_tick(self) -> None:
        now = time.time()
        # 1) 过期提案
        resolved: list[ActionProposal] = []
        with self._lock:
            for pid, prop in list(self._proposals.items()):
                if prop.expired():
                    has_ack = any(v == VOTE_ACK for v in prop.votes.values())
                    if prop.required_acks > 0 and not has_ack:
                        prop.resolution = RESOLUTION_EXPIRED
                    else:
                        # TTL 到 + 没人 veto = 默认通过
                        prop.resolution = RESOLUTION_APPROVED
                    prop.resolution_at = now
                    resolved.append(prop)
                    self._proposals.pop(pid, None)
                    self._proposal_history_log.append(prop.to_dict())
        for prop in resolved:
            try:
                self.sio.emit(EVT_ACTION_RESOLVED, prop.to_dict())
                self._record_event("action_resolved", {
                    "proposal_id": prop.proposal_id,
                    "title": prop.title,
                    "resolution": prop.resolution,
                })
            except Exception:
                pass

        # 2) 心跳 stale → 主动断开
        with self._lock:
            stale = [
                node for node in self._nodes_by_sid.values()
                if now - node.last_heartbeat_at > self.stale_after_seconds
            ]
        # 真正的 disconnect 由 socketio 主导，这里只是记录到 last_status
        # 留给 page.py 的 status 接口判断
        for node in stale:
            node.last_status["stale"] = True

    # ==========================================================
    # 持久化（仅 shared agenda）
    # ==========================================================
    def _load_agendas(self) -> None:
        if not os.path.exists(self._agenda_path):
            return
        try:
            with open(self._agenda_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"⚠️ swarm: 加载 shared_agendas 失败：{e}")
            return
        items = raw if isinstance(raw, list) else raw.get("items", [])
        for obj in items:
            try:
                item = SharedAgendaItem.from_dict(obj)
                self._shared_agendas[item.agenda_id] = item
            except Exception:
                continue

    def _save_agendas(self) -> None:
        try:
            data = [a.to_dict() for a in self._shared_agendas.values()]
            tmp = self._agenda_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._agenda_path)
        except Exception as e:
            print(f"⚠️ swarm: 保存 shared_agendas 失败：{e}")

    # ==========================================================
    # 辅助
    # ==========================================================
    def _broadcast_except(self, exclude_node_id: str, event: str,
                          payload: dict[str, Any]) -> None:
        with self._lock:
            targets = [
                node.sid
                for nid, node in [(nid, self._nodes_by_sid.get(sid))
                                  for nid, sid in self._sid_by_node.items()]
                if node is not None and nid != exclude_node_id
            ]
        # 一一发——socketio 没有"广播除某些 sid 之外"的语法
        for sid in targets:
            try:
                self.sio.emit(event, payload, room=sid)
            except Exception:
                pass

    def _record_event(self, kind: str, data: dict[str, Any]) -> None:
        ev = {
            "id": uuid.uuid4().hex[:10],
            "kind": kind,
            "ts": time.time(),
            "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            **data,
        }
        self._recent_events.append(ev)

    # ==========================================================
    # 给 page 的 HTTP 路由用
    # ==========================================================
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            nodes = []
            for node in self._nodes_by_sid.values():
                gap = now - node.last_heartbeat_at
                nodes.append({
                    "node_id": node.profile.node_id,
                    "node_name": node.profile.node_name,
                    "swarm_id": node.profile.swarm_id,
                    "hostname": node.profile.hostname,
                    "backend": node.profile.backend,
                    "embedding_model": node.profile.embedding_model,
                    "connected_at": node.connected_at,
                    "connected_str": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(node.connected_at)
                    ),
                    "last_heartbeat_at": node.last_heartbeat_at,
                    "last_heartbeat_gap": gap,
                    "mode": node.mode,
                    "current_agenda": node.current_agenda,
                    "fissure_count": node.fissure_count,
                    "agenda_active": node.agenda_active,
                    "last_thought": node.last_thought,
                    "stale": gap > self.stale_after_seconds,
                })
            agendas = [a.to_dict() for a in self._shared_agendas.values()]
            agendas.sort(key=lambda a: -a.get("updated_at", 0))
            proposals = [p.to_dict() for p in self._proposals.values()]
            history = list(self._proposal_history_log)[-20:]
            events = list(self._recent_events)[-50:]
            events.reverse()
        return {
            "nodes": nodes,
            "node_count": len(nodes),
            "online_count": sum(1 for n in nodes if not n["stale"]),
            "shared_agendas": agendas,
            "pending_proposals": proposals,
            "recent_resolved_proposals": history,
            "recent_events": events,
            "ts": now,
        }

    # 给"访客对话"的调度逻辑用：挑一个在线 node 去派发任务
    def pick_node_for_chat(self,
                           preferred_node_id: str = "") -> Optional[str]:
        """返回应当承接访客对话任务的 sid。

        优先策略：
          1) preferred_node_id 在线就用它
          2) 最早连上来的 node（让"主"节点稳定承接）
          3) 否则返回 None
        """
        with self._lock:
            if preferred_node_id:
                sid = self._sid_by_node.get(preferred_node_id)
                if sid:
                    return sid
            if not self._nodes_by_sid:
                return None
            oldest = min(
                self._nodes_by_sid.values(),
                key=lambda n: n.connected_at,
            )
            return oldest.sid

    def stop(self) -> None:
        self._stop_event.set()
