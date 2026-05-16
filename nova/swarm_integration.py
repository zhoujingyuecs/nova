"""swarm_integration.py —— Nova 与 SwarmLink 的胶水层（v1.4 新增）

把"跨节点协议"和"单个 nova 的脑子"这两件互不相干的事用最小接触面拼起来：

  - 解析 nova 输出里的 <share-memory> / <share-agenda> / <recall-swarm>
    / <propose> / <vote> 块，转成对应的 SwarmLink 调用。
  - 把 SwarmLink inbox 里的事件消化成 nova 脑子里的东西：
        * memory echo  → 新建一条 source=peer:xxx, kind=echo 的裂缝
        * shared agenda → 镜像进本地 agenda（scope=shared）
        * agenda progress → 更新对应共享 agenda 副本的状态
        * recall query  → 从本地陶土球抽几条相关裂缝寄回去
        * recall response → 收到的回声塞进陶土球（同 memory echo 路径）
        * proposal      → nova 应当被通知（写一条 worklog；nova 可写 <vote> 回应）
        * proposal resolved → 写一条 worklog；APPROVED 的话可以在原发起 node
                              转成一条任务

mind.py 的入侵面只有两处：
  - perceive / think 末尾调用 adapter.absorb_response(response_text)
  - runtime tick 开头调用 adapter.drain_inbox()

  这两个都是空操作时的安全路径，swarm_enabled=False 时整个 adapter 不创建。
"""
from __future__ import annotations

import collections
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .swarm import (
    EVT_ACTION_PROPOSED, EVT_ACTION_RESOLVED,
    EVT_AGENDA_SYNC,
    EVT_MEMORY_ECHO,
    EVT_MESSAGE_IN,
    EVT_PEER_JOINED, EVT_PEER_LEFT,
    EVT_RECALL_QUERY_IN, EVT_RECALL_RESPONSE_IN,
    EVT_WELCOME,
    ActionProposal,
    MemoryEcho,
    NodeProfile, RecallQuery, SharedAgendaItem,
    VOTE_ACK, VOTE_VETO, VOTE_ABSTAIN,
    SCOPE_SHARED,
    default_should_share_fissure,
)


# ============================================================
# 解析 nova 输出里的 swarm 标签
# ============================================================
_SHARE_MEM_RE   = re.compile(r"<share-memory\b[^>]*>(.*?)</share-memory>",
                             re.DOTALL | re.IGNORECASE)
_SHARE_AG_RE    = re.compile(r"<share-agenda\b([^>]*)>(.*?)</share-agenda>",
                             re.DOTALL | re.IGNORECASE)
_RECALL_RE      = re.compile(r"<recall-swarm\b[^>]*>(.*?)</recall-swarm>",
                             re.DOTALL | re.IGNORECASE)
_PROPOSE_RE     = re.compile(r"<propose\b([^>]*)>(.*?)</propose>",
                             re.DOTALL | re.IGNORECASE)
_VOTE_RE        = re.compile(r"<vote\b([^>]*)>(.*?)</vote>",
                             re.DOTALL | re.IGNORECASE)

# 所有 swarm 标签一起剥（给 mind 拼"对外回应"用）
_ALL_SWARM_TAGS_RE = re.compile(
    r"<(?:share-memory|share-agenda|recall-swarm|propose|vote)\b[^>]*>.*?"
    r"</(?:share-memory|share-agenda|recall-swarm|propose|vote)>",
    re.DOTALL | re.IGNORECASE,
)

_ATTR_RE = re.compile(r'(\w+)\s*=\s*"?([^"\s>]+)"?')


def strip_swarm_tags(text: str) -> str:
    """剥掉 nova 输出里所有 swarm 标签（同 <rule>/<seal> 的处理一样）。"""
    if not text:
        return text
    return _ALL_SWARM_TAGS_RE.sub("", text).strip()


def _parse_attrs(attrs: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(attrs or ""):
        out[m.group(1).lower()] = m.group(2)
    return out


def _parse_kv_body(body: str) -> dict[str, str]:
    """解析 `key: value` 形式的多行内容。'payload:' / 'reason:' 等。"""
    out = {}
    cur_key = None
    cur_lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if m and not line.startswith(" "):
            if cur_key is not None:
                out[cur_key] = "\n".join(cur_lines).strip()
            cur_key = m.group(1).lower()
            cur_lines = [m.group(2)]
        else:
            cur_lines.append(line.strip())
    if cur_key is not None:
        out[cur_key] = "\n".join(cur_lines).strip()
    return out


@dataclass
class ParsedSwarmDirectives:
    """从一段 nova 回应里解析出的所有 swarm 指令。"""
    share_memories: list[str]
    share_agendas: list[dict[str, Any]]    # {title, description, next_action, priority, tags}
    recall_queries: list[str]
    proposals: list[dict[str, Any]]
    votes: list[dict[str, Any]]

    @property
    def is_empty(self) -> bool:
        return not (self.share_memories or self.share_agendas
                    or self.recall_queries or self.proposals or self.votes)


def parse_swarm_directives(text: str) -> ParsedSwarmDirectives:
    if not text:
        return ParsedSwarmDirectives([], [], [], [], [])

    share_memories = [m.group(1).strip()
                      for m in _SHARE_MEM_RE.finditer(text)
                      if m.group(1).strip()]

    share_agendas: list[dict[str, Any]] = []
    for m in _SHARE_AG_RE.finditer(text):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2).strip()
        kv = _parse_kv_body(body)
        title = kv.get("title") or body.splitlines()[0].strip()
        title = (title or "").strip()
        if not title:
            continue
        item = {
            "title": title[:160],
            "description": kv.get("description", "")[:600],
            "next_action": kv.get("next", "") or kv.get("next_action", ""),
            "priority": _safe_float(attrs.get("priority", "0.7"), 0.7),
            "tags": _split_csv(attrs.get("tags", "")),
        }
        share_agendas.append(item)

    recall_queries = [m.group(1).strip()
                      for m in _RECALL_RE.finditer(text)
                      if m.group(1).strip()]

    proposals: list[dict[str, Any]] = []
    for m in _PROPOSE_RE.finditer(text):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2).strip()
        kv = _parse_kv_body(body)
        title = kv.get("title") or body.splitlines()[0].strip()
        if not title:
            continue
        # payload 可以是 `payload: key=value, key=value` 或多行
        payload: dict[str, Any] = {}
        raw_payload = kv.get("payload", "")
        if raw_payload:
            for piece in re.split(r"[,\n;]+", raw_payload):
                if "=" in piece:
                    k, v = piece.split("=", 1)
                    payload[k.strip()] = v.strip()
        proposals.append({
            "title": title[:120],
            "description": kv.get("description", "") or kv.get("reason", ""),
            "payload": payload,
            "impact": attrs.get("impact", "medium").lower(),
            "ttl_seconds": _safe_float(attrs.get("ttl", "30"), 30.0),
            "required_acks": int(_safe_float(attrs.get("acks", "0"), 0.0)),
            "reason": kv.get("reason", ""),
        })

    votes: list[dict[str, Any]] = []
    for m in _VOTE_RE.finditer(text):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2).strip()
        pid = attrs.get("proposal") or attrs.get("id") or ""
        if not pid:
            continue
        # 投票内容形如 "veto: 理由"，也可以是 "ack"
        vote = VOTE_ABSTAIN
        reason = body
        low = body.lower()
        if low.startswith("veto"):
            vote = VOTE_VETO
            reason = body.split(":", 1)[1].strip() if ":" in body else ""
        elif low.startswith("ack") or low.startswith("agree"):
            vote = VOTE_ACK
            reason = body.split(":", 1)[1].strip() if ":" in body else ""
        elif low.startswith("abstain"):
            vote = VOTE_ABSTAIN
            reason = body.split(":", 1)[1].strip() if ":" in body else ""
        votes.append({
            "proposal_id": pid,
            "vote": vote,
            "reason": reason,
        })

    return ParsedSwarmDirectives(
        share_memories, share_agendas, recall_queries, proposals, votes,
    )


def _safe_float(s: str, default: float) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _split_csv(s: str) -> list[str]:
    if not s:
        return []
    return [piece.strip() for piece in s.split(",") if piece.strip()]


# ============================================================
# 接收端：把 echo 落进陶土球
# ============================================================
def absorb_memory_echo(nova: Any, echo_dict: dict[str, Any], *,
                       source_prefix: str = "peer") -> Optional[str]:
    """把一条 swarm 收到的 echo 写进本地陶土球，返回新建的 fissure id。

    跳过条件：
      - 内容为空
      - origin 是自己（兜底）
      - 已经吸收过同 echo_id 的（轻量去重）
    """
    content = (echo_dict or {}).get("content", "").strip()
    if not content:
        return None
    origin_id = echo_dict.get("origin_node_id", "")
    origin_name = echo_dict.get("origin_node_name", "") or "unknown"

    # 去重缓存（绑定到 nova 实例上）
    cache = getattr(nova, "_swarm_echo_seen", None)
    if cache is None:
        cache = collections.deque(maxlen=512)
        try:
            setattr(nova, "_swarm_echo_seen", cache)
        except Exception:
            pass
    echo_id = echo_dict.get("echo_id", "")
    if echo_id and echo_id in cache:
        return None
    if echo_id:
        cache.append(echo_id)

    # 嵌入
    shape = None
    embed_dim = getattr(getattr(nova, "embedder", None), "dim", 0)
    raw_shape = echo_dict.get("shape")
    if (raw_shape
            and isinstance(raw_shape, list)
            and len(raw_shape) == embed_dim
            and echo_dict.get("embedding_model") == getattr(
                nova.cfg, "embedding_model", "")):
        try:
            shape = np.array(raw_shape, dtype=np.float32)
        except Exception:
            shape = None
    if shape is None:
        try:
            shape = nova.embedder.embed(content)
        except Exception:
            return None

    source_label = f"{source_prefix}:{(origin_id or 'unknown')[:8]}"
    speaker_label = f"回声·{origin_name}"

    try:
        fis = nova.field.add(
            content=content,
            shape=shape,
            speaker=speaker_label,
            episode_id="",
            turn_index=0,
            source=source_label,
            modality="memory",
            kind="echo",
            epistemic_state="remembered",
            unresolved=False,
        )
    except Exception as e:
        print(f"⚠️ swarm: 落 echo 失败 {e}")
        return None

    # 把它与最近本地裂缝弱连一下，让水流以后能从这边走过
    try:
        recent = nova.field.nearest(shape, k=2, exclude={fis.id})
        for other, sim in recent:
            if sim > 0.55:
                nova.field.link(fis.id, other.id, strength_delta=0.3)
                nova.field.link(other.id, fis.id, strength_delta=0.2)
    except Exception:
        pass

    return fis.id


def collect_recall_response(nova: Any, query_text: str, *,
                            top_k: int = 4) -> list[MemoryEcho]:
    """本地节点回应一次 recall_query：从陶土球查最相关的几条，包成 MemoryEcho 列表。"""
    out: list[MemoryEcho] = []
    try:
        shape = nova.embedder.embed(query_text)
    except Exception:
        return out
    try:
        results = nova.field.nearest(shape, k=top_k)
    except Exception:
        return out
    cfg = getattr(nova, "cfg", None)
    emb_model = getattr(cfg, "embedding_model", "") if cfg else ""
    emb_dim = int(getattr(nova.embedder, "dim", 0))
    node_id = ""
    node_name = ""
    swarm = getattr(nova, "swarm", None)
    if swarm is not None and getattr(swarm, "profile", None) is not None:
        node_id = swarm.profile.node_id
        node_name = swarm.profile.node_name
    for fis, sim in results:
        # 只回 nova 自己的话或对话证据；过滤系统自动生成的回声以免成环
        kind = getattr(fis, "kind", "")
        if kind == "echo":
            continue
        content = (getattr(fis, "content", "") or "").strip()
        if not content:
            continue
        echo = MemoryEcho(
            echo_id=MemoryEcho.new_id(),
            origin_node_id=node_id,
            origin_node_name=node_name,
            content=content[:280],
            shape=None,                # 给跨节点的 echo 不带 shape（带了也用不到）
            speaker=getattr(fis, "speaker", ""),
            kind="echo",
            modality="memory",
            epistemic_state=getattr(fis, "epistemic_state", "remembered"),
            source_label=getattr(fis, "source", ""),
            origin_ts=getattr(fis, "creation_time", time.time()),
            embedding_model=emb_model,
            embedding_dim=emb_dim,
            note=f"相似度 {sim:.2f}",
        )
        out.append(echo)
    return out


# ============================================================
# 把本地 agenda 同步到/接收自 swarm
# ============================================================
def mirror_shared_agenda_into_local(nova: Any, shared: SharedAgendaItem) -> None:
    """收到 swarm 推过来的 shared agenda，镜像进本地 agenda。"""
    runtime = getattr(nova, "_runtime_ref", None)
    if runtime is None or not hasattr(runtime, "agenda"):
        return
    agenda = runtime.agenda
    existing = agenda.by_external_id(shared.agenda_id)
    if existing is None:
        item = agenda.add(
            title=shared.title,
            description=shared.description,
            source="swarm",
            priority=shared.priority,
            drive=shared.drive,
            next_action=shared.next_action,
            tags=shared.tags + ["swarm_shared"],
            scope=SCOPE_SHARED,
            external_id=shared.agenda_id,
            origin_node_id=shared.proposer_node_id,
            origin_node_name=shared.proposer_node_name,
        )
        runtime.worklog.append(
            "swarm",
            f"swarm 共享主线进入本地：{item.title}（来自 {shared.proposer_node_name}）",
            agenda_id=item.id,
        )
    else:
        # 状态同步（远端结案了，本地也结案）
        if shared.status in {"done", "abandoned"} and existing.status != shared.status:
            agenda.update(existing.id, status=shared.status,
                          evidence=shared.last_progress)
            runtime.worklog.append(
                "swarm",
                f"swarm 主线 {existing.title} 状态变更：{shared.status}（来自 {shared.last_progress_by}）",
                agenda_id=existing.id,
            )
        else:
            # progress 同步：把别人推过的 summary 作为我们的 evidence
            if (shared.last_progress
                    and shared.last_progress not in existing.evidence
                    and shared.last_progress_by):
                existing.add_evidence(
                    f"[来自 {shared.last_progress_by}] {shared.last_progress}"
                )
                existing.next_action = (
                    shared.next_action or existing.next_action
                )
                agenda.save()


# ============================================================
# Adapter：粘合 Nova / SwarmLink / Runtime
# ============================================================
class SwarmAdapter:
    """生命周期：local.py 在构造 ContinuousRuntime 后实例化它，挂在 nova.swarm 上。

    适配器持有：
      - nova（脑子）
      - link（链路，可能是 None；nova.swarm_enabled=False 时整体跳过）
      - runtime_ref（弱依赖；通过 nova._runtime_ref 注入）

    使用：
      - perceive / think 末尾调 absorb_response(response)
      - runtime tick 开头调 drain_inbox()
      - 周期性调 send_heartbeat()
    """

    def __init__(self, nova: Any, link: Any):
        self.nova = nova
        self.link = link
        # 未交付的 recall 查询：query_id → text（用来过滤 response）
        self._open_recall_queries: dict[str, str] = {}
        # 已发起的提案：proposal_id → ActionProposal 副本
        self._open_proposals: dict[str, ActionProposal] = {}
        # 已知 share-memory 内容的近期指纹，避免在 absorb 里反复广播
        self._recent_shared_fingerprints: collections.deque = collections.deque(
            maxlen=128
        )
        self._last_heartbeat_at = 0.0
        # 给 Nova 注册 echo 去重缓存（也可 lazy 创建，但放这更可见）
        if not hasattr(nova, "_swarm_echo_seen"):
            try:
                setattr(nova, "_swarm_echo_seen",
                        collections.deque(maxlen=512))
            except Exception:
                pass

    # ----------------- 出站：解析 nova 输出 -----------------
    def absorb_response(self, response_text: str, *,
                        worklog: Any = None) -> str:
        """从 nova 回应里挑出 swarm 指令并执行；返回剥掉标签的文本。

        worklog 是可选的 WorkLog，传入时会记录每个出站事件。
        """
        if not response_text or self.link is None:
            return response_text or ""
        directives = parse_swarm_directives(response_text)
        if directives.is_empty:
            return response_text

        # share-memory
        for content in directives.share_memories:
            self._publish_share_memory(content, worklog=worklog)

        # share-agenda
        for ag_data in directives.share_agendas:
            self._publish_share_agenda(ag_data, worklog=worklog)

        # recall-swarm
        for query_text in directives.recall_queries:
            self._publish_recall_query(query_text, worklog=worklog)

        # propose
        for prop_data in directives.proposals:
            self._publish_proposal(prop_data, worklog=worklog)

        # vote
        for vote_data in directives.votes:
            self._publish_vote(vote_data, worklog=worklog)

        return strip_swarm_tags(response_text)

    def auto_share_if_enabled(self, fis: Any, *, worklog: Any = None) -> None:
        """v1.4：如果 swarm_auto_share_speech 打开，且 fissure 看起来是
        nova 自己说出口的、值得让别人听到的内容，自动广播一份。

        默认是关闭的——nova 主动 <share-memory> 才广播。
        """
        if self.link is None:
            return
        cfg = self.nova.cfg
        if not getattr(cfg, "swarm_auto_share_speech", False):
            return
        if not default_should_share_fissure(fis):
            return
        content = (getattr(fis, "content", "") or "").strip()
        if not content:
            return
        self._publish_share_memory(content, worklog=worklog,
                                   speaker=getattr(fis, "speaker", "我"),
                                   note="(auto)")

    def _publish_share_memory(self, content: str, *,
                              worklog: Any = None,
                              speaker: str = "我",
                              note: str = "") -> None:
        fp = _fingerprint(content)
        if fp in self._recent_shared_fingerprints:
            return
        self._recent_shared_fingerprints.append(fp)
        echo = self._build_echo(content, speaker=speaker, note=note)
        ok = self.link.share_memory(echo)
        if worklog is not None:
            worklog.append(
                "swarm_out",
                f"向 swarm 广播一句话：{content[:60]}",
                detail=content[:400],
                meta={"event": "share_memory", "ok": ok,
                      "echo_id": echo.echo_id},
            )

    def _publish_share_agenda(self, ag_data: dict[str, Any], *,
                              worklog: Any = None) -> None:
        runtime = getattr(self.nova, "_runtime_ref", None)
        if runtime is None:
            return
        title = ag_data.get("title", "").strip()
        if not title:
            return
        # 先在本地把它升格成 shared，再广播
        external_id = SharedAgendaItem.new_id()
        item = runtime.agenda.add_if_absent(
            title,
            ag_data.get("description", ""),
            source="self",
            priority=float(ag_data.get("priority", 0.7)),
            drive="continuity",
            next_action=ag_data.get("next_action", ""),
            tags=list(ag_data.get("tags") or []) + ["swarm_shared"],
            scope=SCOPE_SHARED,
            external_id=external_id,
            origin_node_id=self.link.profile.node_id,
            origin_node_name=self.link.profile.node_name,
        )
        shared = SharedAgendaItem(
            agenda_id=item.external_id or external_id,
            title=item.title,
            description=item.description,
            proposer_node_id=self.link.profile.node_id,
            proposer_node_name=self.link.profile.node_name,
            priority=item.priority,
            drive=item.drive,
            next_action=item.next_action,
            status=item.status,
            tags=list(item.tags),
        )
        ok = self.link.share_agenda(shared)
        if worklog is not None:
            worklog.append(
                "swarm_out",
                f"把主线提交给 swarm：{shared.title}",
                detail=shared.description,
                agenda_id=item.id,
                meta={"event": "share_agenda", "ok": ok,
                      "external_id": shared.agenda_id},
            )

    def _publish_recall_query(self, text: str, *,
                              worklog: Any = None) -> None:
        cfg = self.nova.cfg
        try:
            shape = self.nova.embedder.embed(text)
            shape_list = [float(x) for x in shape.tolist()]
        except Exception:
            shape_list = None
        query = RecallQuery(
            query_id=RecallQuery.new_id(),
            origin_node_id=self.link.profile.node_id,
            origin_node_name=self.link.profile.node_name,
            text=text,
            shape=shape_list,
            top_k=int(getattr(cfg, "swarm_recall_top_k", 4)),
            embedding_model=getattr(cfg, "embedding_model", ""),
            embedding_dim=int(getattr(self.nova.embedder, "dim", 0)),
        )
        self._open_recall_queries[query.query_id] = text
        ok = self.link.issue_recall_query(query)
        if worklog is not None:
            worklog.append(
                "swarm_out",
                f"向 swarm 求一段回忆：{text[:60]}",
                meta={"event": "recall_query", "ok": ok,
                      "query_id": query.query_id},
            )

    def _publish_proposal(self, prop_data: dict[str, Any], *,
                          worklog: Any = None) -> None:
        cfg = self.nova.cfg
        ttl = float(prop_data.get("ttl_seconds")
                    or getattr(cfg, "swarm_proposal_default_ttl", 30.0))
        prop = ActionProposal(
            proposal_id=ActionProposal.new_id(),
            proposer_node_id=self.link.profile.node_id,
            proposer_node_name=self.link.profile.node_name,
            title=prop_data.get("title", "")[:120],
            description=prop_data.get("description", "")[:600],
            payload=dict(prop_data.get("payload") or {}),
            impact=prop_data.get("impact", "medium"),
            ttl_seconds=ttl,
            required_acks=int(prop_data.get("required_acks", 0)),
        )
        self._open_proposals[prop.proposal_id] = prop
        ok = self.link.propose_action(prop)
        if worklog is not None:
            worklog.append(
                "swarm_out",
                f"发起跨集群仲裁：{prop.title}（TTL={prop.ttl_seconds:.0f}s, impact={prop.impact}）",
                detail=prop.description,
                meta={"event": "propose", "ok": ok,
                      "proposal_id": prop.proposal_id},
            )

    def _publish_vote(self, vote_data: dict[str, Any], *,
                      worklog: Any = None) -> None:
        pid = vote_data.get("proposal_id", "")
        if not pid:
            return
        ok = self.link.vote(pid, vote_data.get("vote", VOTE_ABSTAIN),
                            reason=vote_data.get("reason", ""))
        if worklog is not None:
            worklog.append(
                "swarm_out",
                f"对提案 {pid} 投票：{vote_data.get('vote')}",
                detail=vote_data.get("reason", ""),
                meta={"event": "vote", "ok": ok,
                      "proposal_id": pid},
            )

    # ----------------- 入站：消化 swarm 事件 -----------------
    def drain_inbox(self, *, worklog: Any = None,
                    max_events: Optional[int] = None) -> int:
        """处理 swarm 入站事件；返回处理条数。"""
        if self.link is None:
            return 0
        cfg = self.nova.cfg
        if max_events is None:
            max_events = int(getattr(cfg, "swarm_max_inbox_per_tick", 8))

        n = 0
        while n < max_events:
            ev = self.link.poll(timeout=0)
            if ev is None:
                break
            try:
                self._handle_event(ev, worklog=worklog)
            except Exception as e:
                print(f"⚠️ swarm: 处理事件 {ev.kind} 失败 {e}")
            n += 1
        return n

    def _handle_event(self, ev: Any, *, worklog: Any = None) -> None:
        kind = ev.kind
        data = ev.payload or {}

        if kind == EVT_WELCOME:
            peer_count = len(data.get("peers") or [])
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm 接纳：{self.link.profile.node_name}；当前 peer {peer_count}",
                    meta={"event": "welcome"},
                )
            # 把已有 shared agenda 镜像到本地
            for raw in (data.get("shared_agendas") or []):
                try:
                    item = SharedAgendaItem.from_dict(raw)
                    mirror_shared_agenda_into_local(self.nova, item)
                except Exception:
                    continue
            return

        if kind == EVT_PEER_JOINED:
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm: 节点 {data.get('node_name')} 加入",
                    meta={"event": "peer_joined",
                          "node_id": data.get("node_id")},
                )
            return

        if kind == EVT_PEER_LEFT:
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm: 节点 {data.get('node_name')} 离开",
                    meta={"event": "peer_left",
                          "node_id": data.get("node_id")},
                )
            return

        if kind == EVT_MEMORY_ECHO:
            fid = absorb_memory_echo(
                self.nova, data,
                source_prefix=getattr(self.nova.cfg,
                                      "swarm_echo_source_prefix", "peer"),
            )
            if fid and worklog is not None:
                worklog.append(
                    "swarm",
                    f"听见 {data.get('origin_node_name')} 说过的一句：{data.get('content', '')[:60]}",
                    detail=data.get("content", "")[:300],
                    meta={"event": "echo_in",
                          "from": data.get("origin_node_id", ""),
                          "fissure_id": fid},
                )
            return

        if kind == EVT_AGENDA_SYNC:
            item_raw = data.get("item")
            if item_raw:
                try:
                    shared = SharedAgendaItem.from_dict(item_raw)
                    mirror_shared_agenda_into_local(self.nova, shared)
                except Exception:
                    pass
            return

        if kind == EVT_RECALL_QUERY_IN:
            try:
                qry = RecallQuery.from_dict(data)
            except Exception:
                return
            echoes = collect_recall_response(
                self.nova, qry.text,
                top_k=int(qry.top_k or 4),
            )
            if echoes:
                self.link.reply_recall(qry.query_id, echoes)
                if worklog is not None:
                    worklog.append(
                        "swarm",
                        f"{qry.origin_node_name} 问起'{qry.text[:40]}'，我从陶土球里翻了 {len(echoes)} 条寄回去",
                        meta={"event": "recall_reply",
                              "query_id": qry.query_id,
                              "to": qry.origin_node_id,
                              "count": len(echoes)},
                    )
            return

        if kind == EVT_RECALL_RESPONSE_IN:
            qid = data.get("query_id", "")
            # 只接收自己发出去的 query 的回应
            local_text = self._open_recall_queries.get(qid)
            if not local_text:
                return
            echoes = data.get("echoes") or []
            for echo_raw in echoes:
                fid = absorb_memory_echo(
                    self.nova, echo_raw,
                    source_prefix=getattr(self.nova.cfg,
                                          "swarm_echo_source_prefix", "peer"),
                )
            responder = data.get("responder_node_name", "unknown")
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm 回声：{responder} 回我的 recall '{local_text[:40]}'，捎来 {len(echoes)} 条",
                    meta={"event": "recall_response_in",
                          "query_id": qid,
                          "from": data.get("responder_node_id", ""),
                          "count": len(echoes)},
                )
            return

        if kind == EVT_ACTION_PROPOSED:
            try:
                prop = ActionProposal.from_dict(data)
            except Exception:
                return
            self._open_proposals[prop.proposal_id] = prop
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm 收到提案：{prop.title}（来自 {prop.proposer_node_name}, TTL={prop.ttl_seconds:.0f}s）",
                    detail=prop.description,
                    meta={"event": "proposal_in",
                          "proposal_id": prop.proposal_id,
                          "from": prop.proposer_node_id,
                          "impact": prop.impact,
                          "is_own": prop.proposer_node_id == self.link.profile.node_id},
                )
            return

        if kind == EVT_ACTION_RESOLVED:
            try:
                prop = ActionProposal.from_dict(data)
            except Exception:
                return
            self._open_proposals.pop(prop.proposal_id, None)
            is_own = prop.proposer_node_id == self.link.profile.node_id
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm 裁决：{prop.title} → {prop.resolution}",
                    detail=("否决理由：\n"
                            + "\n".join(f"- {n}: {r}"
                                        for n, r in prop.veto_reasons.items())
                            if prop.veto_reasons else ""),
                    meta={"event": "proposal_resolved",
                          "proposal_id": prop.proposal_id,
                          "resolution": prop.resolution,
                          "is_own": is_own},
                )
            return

        if kind == EVT_MESSAGE_IN:
            text = data.get("text", "")
            sender = data.get("from_node_name", "unknown")
            if not text:
                return
            # 把节点间消息也作为一种"打断"投给 nova
            runtime = getattr(self.nova, "_runtime_ref", None)
            if runtime is not None and hasattr(runtime, "submit_interrupt"):
                try:
                    runtime.submit_interrupt(
                        f"[swarm·{sender}] {text}",
                        source=f"swarm:{data.get('from_node_id', '')[:8]}",
                        wait=False,
                    )
                except Exception:
                    pass
            if worklog is not None:
                worklog.append(
                    "swarm",
                    f"swarm 节点 {sender} 给我留言：{text[:80]}",
                    detail=text[:500],
                    meta={"event": "message_in"},
                )
            return

    # ----------------- 心跳 -----------------
    def send_heartbeat(self, *, force: bool = False) -> bool:
        if self.link is None:
            return False
        cfg = self.nova.cfg
        gap = getattr(cfg, "swarm_heartbeat_seconds", 20.0)
        now = time.time()
        if not force and (now - self._last_heartbeat_at) < gap:
            return False
        from .swarm import HeartbeatPayload
        runtime = getattr(self.nova, "_runtime_ref", None)
        mode = "idle"
        current_agenda = ""
        agenda_active = 0
        last_thought = ""
        if runtime is not None:
            try:
                st = runtime._state  # noqa: SLF001 — runtime 是私有，但是同包
                mode = st.mode
                current_agenda = st.current_agenda_title or ""
            except Exception:
                pass
            try:
                agenda_active = len(runtime.agenda.active())
            except Exception:
                pass
            try:
                events = runtime.worklog.recent(limit=1)
                if events:
                    last_thought = (events[-1].summary or "")[:240]
            except Exception:
                pass
        payload = HeartbeatPayload(
            node_id=self.link.profile.node_id,
            ts=now,
            mode=mode,
            current_agenda=current_agenda,
            fissure_count=int(getattr(self.nova, "field", None)
                              and len(self.nova.field) or 0),
            agenda_active=agenda_active,
            last_thought=last_thought,
        )
        ok = self.link.heartbeat(payload)
        if ok:
            self._last_heartbeat_at = now
        return ok

    # ----------------- 报告 -----------------
    def report_progress_for(self, item: Any, *,
                            summary: str,
                            next_action: str = "",
                            status: str = "") -> bool:
        if self.link is None or item is None:
            return False
        if not getattr(item, "external_id", ""):
            return False
        return self.link.report_progress(
            item.external_id,
            summary=summary,
            next_action=next_action,
            status=status or item.status,
        )

    # ----------------- 工具 -----------------
    def build_swarm_block(self, *, max_chars: int = 480) -> str:
        """渲染一段给 prompt 顶部用的"我此刻在 swarm 里看到的东西"。"""
        if self.link is None:
            return ""
        peers = self.link.peers()
        runtime = getattr(self.nova, "_runtime_ref", None)
        shared = []
        if runtime is not None and hasattr(runtime, "agenda"):
            shared = [
                i for i in runtime.agenda.active()
                if i.scope == SCOPE_SHARED
            ][:4]
        pending = list(self._open_proposals.values())[:4]
        lines: list[str] = []
        lines.append(f"[我此刻在 swarm 里——我自己是 {self.link.profile.node_name}]")
        if peers:
            peer_names = "、".join(p.node_name for p in peers[:6])
            lines.append(f"还在线的同类：{peer_names}（共 {len(peers)}）")
        else:
            lines.append("此刻 swarm 里没有其它同类在线。")
        if shared:
            lines.append("正在跨集群推进的主线：")
            for i in shared:
                src = i.origin_node_name or "我"
                lines.append(f"  · {i.title}（由 {src} 提出）next={i.next_action[:60]}")
        if pending:
            lines.append("还在仲裁中的提案：")
            for p in pending:
                rem = p.remaining()
                mine = (p.proposer_node_id == self.link.profile.node_id)
                tag = "（我自己发起）" if mine else f"（{p.proposer_node_name}）"
                lines.append(
                    f"  · {p.proposal_id} {p.title} {tag} 还剩 {rem:.0f}s"
                )
        text = "\n".join(lines) + "\n\n"
        if len(text) > max_chars:
            text = text[:max_chars - 1].rstrip() + "…\n\n"
        return text

    def _build_echo(self, content: str, *,
                    speaker: str = "我",
                    kind: str = "echo",
                    note: str = "") -> MemoryEcho:
        cfg = self.nova.cfg
        return MemoryEcho(
            echo_id=MemoryEcho.new_id(),
            origin_node_id=self.link.profile.node_id,
            origin_node_name=self.link.profile.node_name,
            content=content[:600],
            shape=None,
            speaker=speaker,
            kind=kind,
            modality="memory",
            epistemic_state="remembered",
            source_label="self",
            origin_ts=time.time(),
            embedding_model=getattr(cfg, "embedding_model", ""),
            embedding_dim=int(getattr(self.nova.embedder, "dim", 0)),
            note=note,
        )


def _fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
