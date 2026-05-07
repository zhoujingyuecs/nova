"""nova v1.0 主循环 —— 精简内核版。

老版本里 perceive 一次会做四次 LLM 调用：主回应、意象拆解、主意识更新、
笔记本更新。每次还要把 SelfField / DriveSystem / SkillBook /
SelfModificationLog / NotesBook 五个块都渲染进 prompt，加起来 prompt
头部经常占掉两千字符还没说一句正经话。

v1.0 把它压成一次主回应 + 偶尔一次轻量级 self_state 更新。

每轮 perceive 做的事：
  1. 嵌入 stimulus
  2. 让水流从 (stimulus + self_state) 这个混合种子出发，沿语义近邻、
     暗道、对话链、冷跳收集激活缝隙
  3. 拼 prompt：SelfState + 工作区索引 + 激活缝隙 + 当前对话链 + 输入
  4. 调一次 LLM；如果回应里有 <tool> 块，让 VM 的手做完，把 result
     填回 prompt，再调一次；最多 max_tool_iterations 次
  5. 把回应嵌入，反向冲刷激活的缝隙；新增 stimulus / response 两条
     缝隙到对话链
  6. 每 cfg.self_update_every 次 perceive，触发一次轻量 self_state 更新
  7. 自动存盘

dream / think 是同样的流程，只是种子来自缝隙场内部 + self_state。
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time
import uuid
from typing import Optional

import numpy as np

from .config import NovaConfig
from .embedder import Embedder
from .field import FissureField
from .fissure import (
    Fissure,
    _normalize,
    SPEAKER_OUTSIDER,
    SPEAKER_SELF,
    SPEAKER_DAYDREAM,
    SPEAKER_NONE,
)
from .flow import ConsciousnessFlow
from .llm import LocalLLM
from .persistence import load_field, save_field
from .perception import (
    EPISTEMIC_IMAGINED,
    EPISTEMIC_INFERRED,
    EPISTEMIC_OBSERVED,
    MODALITY_HEARING,
    MODALITY_INNER,
    RealityState,
    SENSORY_SYSTEM_ADDITION,
    SOURCE_SELF,
    SOURCE_USER,
)
from .self_state import SelfState, SELF_UPDATE_PROMPT, parse_self_update
from .tools import VMAgent, format_result, parse_actions, strip_actions
from .task_state import TaskLedger, TASK_SYSTEM_ADDITION
from .tool_guard import ToolLoopGuard
from .workspace import Workspace


DREAM_PROMPT_BASE = (
    "[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。]\n\n"
    "{state_block}"
    "{workspace_block}"
    "[下面这些片段浮上心头——是素材，不是替代品：]\n\n"
    "{memories}\n\n"
    "[你现在脑子里在想什么？写一两句就好，像在自言自语。"
    "你也可以伸手做点事——查一下工作区、跑一段已有脚本、"
    "或者把刚才领悟到的东西写成一篇笔记。]"
)

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</think>", re.IGNORECASE)


def _strip_think_block(text: str) -> str:
    """剥掉 Qwen / R1 风格的 <think>...</think> 推理块。"""
    if not text:
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    m = _OPEN_THINK_RE.search(text)
    if m:
        text = text[: m.start()]
    m = _CLOSE_THINK_RE.search(text)
    if m:
        text = text[m.end() :]
    return text.strip()


def _format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


class Nova:
    def __init__(self, cfg: Optional[NovaConfig] = None):
        self.cfg = cfg or NovaConfig()
        self.embedder = Embedder(self.cfg)

        try:
            self.field = load_field(self.cfg, self.embedder.dim)
        except FileNotFoundError:
            self.field = FissureField(self.cfg, self.embedder.dim)

        self.flow_engine = ConsciousnessFlow(self.cfg, self.field)
        self.llm = LocalLLM(self.cfg)

        # SelfState: 五合一的轻量自我状态
        self._self_state_path = os.path.join(self.cfg.field_path, "self_state.json")
        self.self_state = SelfState.load(self._self_state_path)

        # RealityState: 短期现实感 / 感官锚点 / 未完成社会牵引
        self._reality_state_path = os.path.join(self.cfg.field_path, "reality_state.json")
        self.reality_state = RealityState.load(self._reality_state_path)

        # v1.1: active_user_task + evidence ledger.
        self._task_state_path = os.path.join(self.cfg.field_path, "task_state.json")
        self.task_ledger = TaskLedger.load(self._task_state_path)

        # VM hand + workspace
        self.vm_agent: Optional[VMAgent] = None
        if self.cfg.vm_agent_url:
            agent = VMAgent(
                self.cfg.vm_agent_url,
                self.cfg.vm_agent_token,
                timeout=self.cfg.vm_request_timeout,
            )
            if agent.is_alive():
                self.vm_agent = agent
                print(f"虚拟机里的手已连：{self.cfg.vm_agent_url}")
            else:
                print(f"⚠️ 虚拟机里的手没回应（{self.cfg.vm_agent_url}），这次没有手。")

        self.workspace = Workspace(
            self.vm_agent,
            root=self.cfg.workspace_root,
            index_ttl=self.cfg.workspace_index_ttl,
            index_max_chars=self.cfg.workspace_index_max_chars,
        )
        if self.workspace.is_available:
            self.workspace.ensure_bootstrap()

        # 运行状态
        self._perceive_count = 0
        self._recent_history = collections.deque(maxlen=self.cfg.recent_history_size)
        self._current_episode_id: str = ""
        self._last_episode_activity: float = 0.0
        self._last_episode_fid: str = ""
        self._current_turn_index: int = 0

        self._restore_episode_state_from_field()

        self._lock = threading.RLock()

        if len(self.field) == 0 and self.cfg.seed_memories_file:
            self._load_seeds(self.cfg.seed_memories_file)

    # ==========================================================
    # 对外接口
    # ==========================================================
    def perceive(self, stimulus: str) -> str:
        """感知一段外界输入，给出一次回应。"""
        with self._lock:
            episode_id = self._get_or_start_episode()

            # 外部输入先变成“听见的东西”，再进入裂缝场。
            # 这样 nova 记得：这是对方说的，不是自己想到的。
            stim_percept = self.reality_state.hear_user(stimulus, speaker="周靖越")
            self.task_ledger.observe_user_message(stimulus)

            stim_shape = self.embedder.embed(stimulus)
            stim_fid = self._open_episode_turn(
                content=stimulus,
                shape=stim_shape,
                speaker=SPEAKER_OUTSIDER,
                episode_id=episode_id,
                source=stim_percept.source,
                modality=stim_percept.modality,
                kind=stim_percept.kind,
                epistemic_state=stim_percept.epistemic_state,
                unresolved=bool(self.reality_state.current_request and self.reality_state.current_request.status == "active"),
            )

            # 水流：种子 = stimulus + self_state 形状
            attention_seed = self._compose_attention_seed(stim_shape)
            episode_anchors = self.field.walk_chain_back(stim_fid, k=self.cfg.episode_recall_size)
            activated = self.flow_engine.flow(
                attention_seed,
                recent_history=set(self._recent_history),
                mandatory_anchors=episode_anchors,
            )

            user_prompt = self._build_prompt(stimulus, stim_fid, activated, episode_id)
            final_response = self._chat_with_tools(user_prompt)
            final_response = _strip_think_block(final_response)
            visible = strip_actions(final_response).strip() or "（沉默。）"
            visible = ToolLoopGuard.compact_visible_text(visible)

            response_percept = self.reality_state.notice_self_response(visible)

            response_shape = self.embedder.embed(visible)
            self._reshape(activated, visible, response_shape)

            response_fid = self._open_episode_turn(
                content=visible,
                shape=response_shape,
                speaker=SPEAKER_SELF,
                episode_id=episode_id,
                source=response_percept.source,
                modality=response_percept.modality,
                kind=response_percept.kind,
                epistemic_state=response_percept.epistemic_state,
            )

            # 共激活弱链接：被一起想起的几条之间长出微弱暗道
            activated_ids = [f.id for f in activated]
            if len(activated_ids) >= 2:
                self.field.link_chain(
                    activated_ids,
                    base_strength=self.cfg.flow_coactivation_link_strength,
                    decay=0.65,
                    max_distance=self.cfg.flow_coactivation_distance,
                    bidirectional=False,
                )
            for fid in activated_ids[-self.cfg.flow_coactivation_distance:]:
                self.field.link(fid, response_fid,
                                strength_delta=self.cfg.flow_coactivation_link_strength)

            self.field.sync_all()
            self._tick_autosave()

            for fid in activated_ids:
                f = self.field.get(fid)
                if f is None:
                    continue
                if f.episode_id and f.episode_id == episode_id:
                    continue
                self._recent_history.append(fid)

            # 工作区索引可能因为这一轮 nova 写过笔记/脚本而过期
            if "<tool" in final_response.lower() and self.workspace.is_available:
                self.workspace.invalidate()

            # 偶尔更新一次 SelfState；现实感每轮都轻量落盘，避免未完成请求丢失。
            self._maybe_update_self_state(stimulus, visible, agenda_text="")
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()

            return visible

    def think(self, *, max_tokens: Optional[int] = None,
              prompt_hint: str = "") -> Optional[str]:
        """没人说话时的一次内向活动（替代 dream_step）。

        prompt_hint：可选的 prompt 前缀，runtime 在 goal_pursuit /
        reflection / orientation 时塞进来当主线指令。"""
        with self._lock:
            if len(self.field) < 1:
                return None
            seed_shape = self._dream_seed()
            attention_seed = self._compose_attention_seed(seed_shape)

            activated = self.flow_engine.flow(
                attention_seed,
                recent_history=set(self._recent_history),
            )
            if not activated:
                return None

            memories = "\n".join(
                f"- {self._format_recall_line(f, in_episode=False)}"
                for f in activated
            )
            state_block = self.self_state.render_for_prompt(
                max_chars=self.cfg.self_update_max_tokens * 4,
            ) + "\n\n"
            workspace_block = self._render_workspace_block()
            base_prompt = DREAM_PROMPT_BASE.format(
                memories=memories,
                state_block=state_block,
                workspace_block=workspace_block,
            )
            if prompt_hint:
                user_prompt = prompt_hint.rstrip() + "\n\n" + base_prompt
            else:
                user_prompt = base_prompt

            thought_raw = self._chat_with_tools(
                user_prompt,
                max_tokens=max_tokens or self.cfg.daydream_max_tokens,
            )
            thought_raw = _strip_think_block(thought_raw)
            thought = strip_actions(thought_raw).strip()
            thought = ToolLoopGuard.compact_visible_text(thought)
            if not thought:
                return None

            activated_ids = [f.id for f in activated]
            if len(activated_ids) >= 2:
                self.field.link_chain(
                    activated_ids,
                    base_strength=self.cfg.flow_coactivation_link_strength * 0.7,
                    decay=0.65,
                    max_distance=self.cfg.flow_coactivation_distance,
                    bidirectional=False,
                )
            thought_percept = self.reality_state.notice_thought(thought)
            thought_shape = self.embedder.embed(thought)
            self._reshape(activated, thought, thought_shape)
            thought_fid = self._maybe_create(
                thought,
                thought_shape,
                speaker=SPEAKER_DAYDREAM,
                source=thought_percept.source,
                modality=thought_percept.modality,
                kind=thought_percept.kind,
                epistemic_state=thought_percept.epistemic_state,
            )
            if thought_fid is not None:
                for fid in activated_ids[-self.cfg.flow_coactivation_distance:]:
                    self.field.link(fid, thought_fid,
                                    strength_delta=self.cfg.flow_coactivation_link_strength)

            self.field.sync_all()
            self._tick_autosave()
            for fid in activated_ids:
                self._recent_history.append(fid)

            if "<tool" in thought_raw.lower() and self.workspace.is_available:
                self.workspace.invalidate()

            self._maybe_update_self_state("（内向活动）", thought, agenda_text=prompt_hint[:200])
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()
            return thought

    # 兼容旧 API ——
    def dream_step(self, max_tokens: Optional[int] = None) -> Optional[str]:
        return self.think(max_tokens=max_tokens)

    def consolidate(self, prune: bool = True, merge: bool = True,
                    decay_links: bool = True) -> dict:
        from .sleep import consolidate as _consolidate
        with self._lock:
            stats = _consolidate(self.field, self.cfg, prune=prune,
                                 merge=merge, decay_links=decay_links)
            save_field(self.field, keep_backup=self.cfg.backup_keep)
            self.self_state.save(self._self_state_path)
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()
            return stats

    def visualize(self, output_path: str, method: str = "pca", **kwargs) -> Optional[str]:
        from .visualize import render_field
        with self._lock:
            return render_field(self.field, output_path, method=method, **kwargs)

    def save(self) -> None:
        with self._lock:
            save_field(self.field, keep_backup=self.cfg.backup_keep)
            self.self_state.save(self._self_state_path)
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()

    # ==========================================================
    # Prompt / LLM / tools
    # ==========================================================
    def _chat_with_tools(self, initial_user: str,
                         max_tokens: Optional[int] = None) -> str:
        system = self.cfg.system_prompt + "\n" + SENSORY_SYSTEM_ADDITION
        if getattr(self.cfg, "task_state_prompt_enabled", True):
            system += "\n" + TASK_SYSTEM_ADDITION
        if self.vm_agent is None:
            return self.llm.chat(system, initial_user, max_tokens=max_tokens)

        current_user = initial_user
        last_response = ""
        guard = ToolLoopGuard(
            max_same_action=getattr(self.cfg, "tool_guard_max_same_action", 2),
            max_same_error=getattr(self.cfg, "tool_guard_max_same_error", 2),
            max_repeated_response=getattr(self.cfg, "tool_guard_max_repeated_response", 2),
        )
        for _ in range(self.cfg.max_tool_iterations):
            response = self.llm.chat(system, current_user, max_tokens=max_tokens)
            ok, reason = guard.check_response(response)
            if not ok:
                if hasattr(self, "task_ledger"):
                    self.task_ledger.set_blocked(reason)
                return strip_actions(last_response).strip() or f"（工具循环已熔断：{reason}）"
            last_response = response
            actions = parse_actions(response)
            if not actions:
                return response
            result_blocks = []
            reality_blocks = []
            blocked_reason = ""
            for action_type, content in actions:
                ok, reason = guard.check_action(action_type, content)
                if not ok:
                    blocked_reason = reason
                    result = {"error": reason}
                else:
                    try:
                        result = self.vm_agent.dispatch(action_type, content)
                    except Exception as e:
                        result = {"error": str(e)}
                guard_ok, guard_reason = guard.observe_result(action_type, result)
                if not guard_ok:
                    blocked_reason = guard_reason
                trace = self.reality_state.notice_tool_result(action_type, content, result)
                if hasattr(self, "task_ledger"):
                    self.task_ledger.record_tool_result(action_type, content, result)
                result_blocks.append(format_result(action_type, content, result))
                reality_blocks.append(trace.render())
            if blocked_reason:
                if hasattr(self, "task_ledger"):
                    self.task_ledger.set_blocked(blocked_reason)
                reality_blocks.append(f"- [工具循环保护] {blocked_reason}。停止重复伸手，改为向用户说明边界或换路径。")
            current_user = (
                current_user
                + "\n\n[你刚才在心里这样转过：]\n"
                + response
                + "\n\n[手回来了，带回这些：]\n"
                + "\n\n".join(result_blocks)
                + "\n\n[现实感校准：这次伸手到底摸到了什么]\n"
                + "\n".join(reality_blocks)
                + "\n\n[继续。可以再伸手，也可以直接对眼前的人说话。若事实还没有证据，就说还没查到，不要补全。]"
            )
        print(f"⚠️ 工具调用超过 {self.cfg.max_tool_iterations} 次，停下了。")
        return last_response

    def _build_prompt(self, stimulus: str, stim_fid: str,
                      activated: list, episode_id: str) -> str:
        in_episode: list[Fissure] = []
        others: list[Fissure] = []
        for f in activated:
            if f.id == stim_fid:
                continue
            if episode_id and f.episode_id == episode_id:
                in_episode.append(f)
            else:
                others.append(f)

        sections: list[str] = []
        sections.append(self.self_state.render_for_prompt())
        sections.append(self.reality_state.render_for_prompt())
        if getattr(self.cfg, "task_state_prompt_enabled", True):
            sections.append(self.task_ledger.render_for_prompt())

        ws_block = self._render_workspace_block().strip()
        if ws_block:
            sections.append(ws_block)

        if others:
            block = [
                "[脑海里浮起的相关片段]",
                "（这些是旧事，是素材，不是当下。让它们融入主意识，不要被它们带跑。）",
            ]
            for f in others:
                block.append(f"- {self._format_recall_line(f, in_episode=False)}")
            sections.append("\n".join(block))

        if in_episode:
            in_episode.sort(key=lambda f: f.turn_index)
            block = [
                "[此刻这段对话刚刚说过的几句]",
                "（按时间从远到近。它们给你场景感，让你知道刚才发生了什么。）",
            ]
            for f in in_episode:
                block.append(f"- {self._format_recall_line(f, in_episode=True)}")
            sections.append("\n".join(block))

        if not sections:
            sections.append("（此刻心里很空，没有什么浮上来。）")

        body = "\n\n".join(sections)
        return f"{body}\n\n[然后，他这样对你说：]\n{stimulus}"

    def _render_workspace_block(self) -> str:
        if not self.workspace.is_available:
            return ""
        text = self.workspace.render_for_prompt()
        return text + "\n\n" if text else ""

    # ==========================================================
    # SelfState 更新（每 N 次 perceive 触发一次轻量 LLM 调用）
    # ==========================================================
    def _maybe_update_self_state(self, stimulus: str, response: str,
                                 agenda_text: str) -> None:
        every = max(1, self.cfg.self_update_every)
        if (self._perceive_count % every) != 0:
            return
        event = f"输入：{stimulus[:400]}\n回应：{response[:400]}"
        prompt = SELF_UPDATE_PROMPT.format(
            current_state=self.self_state.render_for_prompt(max_chars=800),
            event=event,
            agenda_text=agenda_text or "（无）",
        )
        try:
            raw = self.llm.chat(
                "你是 nova 的 SelfState 维护器，只输出 FOCUS / SUMMARY / OPEN / CLOSE 控制行。",
                prompt,
                max_tokens=self.cfg.self_update_max_tokens,
            )
        except Exception as e:
            print(f"⚠️ self_state 更新失败（不致命）：{e}")
            return
        raw = _strip_think_block(raw)
        if "（无变动" in raw or "(无变动" in raw:
            return
        kwargs = parse_self_update(raw)
        if not kwargs:
            return
        if self.self_state.apply_update(**kwargs):
            try:
                self.self_state.save(self._self_state_path)
            except Exception:
                pass

    # ==========================================================
    # Episode / 对话链
    # ==========================================================
    def _get_or_start_episode(self) -> str:
        now = time.time()
        gap = now - self._last_episode_activity
        if (not self._current_episode_id) or gap > self.cfg.episode_gap_seconds:
            self._current_episode_id = uuid.uuid4().hex[:8]
            self._last_episode_fid = ""
            self._current_turn_index = 0
            self._last_episode_activity = now
        return self._current_episode_id

    def _open_episode_turn(
        self,
        content: str,
        shape: np.ndarray,
        speaker: str,
        episode_id: str,
        *,
        source: str = "memory",
        modality: str = "memory",
        kind: str = "memory",
        epistemic_state: str = "remembered",
        evidence_refs: Optional[list[str]] = None,
        action_refs: Optional[list[str]] = None,
        unresolved: bool = False,
    ) -> str:
        f = self.field.add(
            content=content,
            shape=shape,
            speaker=speaker,
            episode_id=episode_id,
            turn_index=self._current_turn_index,
            source=source,
            modality=modality,
            kind=kind,
            epistemic_state=epistemic_state,
            evidence_refs=evidence_refs,
            action_refs=action_refs,
            unresolved=unresolved,
        )
        self._current_turn_index += 1
        prev_id = self._last_episode_fid
        if prev_id:
            self.field.chain_link(prev_id, f.id,
                                  self.cfg.episode_link_forward,
                                  self.cfg.episode_link_backward)
        self._last_episode_fid = f.id
        self._last_episode_activity = time.time()
        return f.id

    def _restore_episode_state_from_field(self) -> None:
        latest_fis = None
        latest_t = 0.0
        for f in self.field:
            if f.episode_id and f.last_flow_time > latest_t:
                latest_t = f.last_flow_time
                latest_fis = f
        if latest_fis is None:
            return
        gap = time.time() - latest_t
        if gap > self.cfg.episode_gap_seconds:
            return
        self._current_episode_id = latest_fis.episode_id
        self._last_episode_activity = latest_t
        self._last_episode_fid = latest_fis.id
        max_idx = -1
        for f in self.field:
            if f.episode_id == latest_fis.episode_id:
                max_idx = max(max_idx, f.turn_index)
        self._current_turn_index = max_idx + 1
        print(f"续上一段还没结束的对话：episode={latest_fis.episode_id}，距上次互动 {int(gap)} 秒。")

    # ==========================================================
    # 缝隙场辅助
    # ==========================================================
    def _reshape(self, activated: list[Fissure], new_content: str,
                 response_shape: np.ndarray) -> None:
        for f in activated:
            p = self.field.plasticity_at(f.shape)
            f.shift_toward(response_shape, plasticity=p,
                           new_content=new_content, rewrite_threshold=0.45)

    def _maybe_create(
        self,
        content: str,
        shape: np.ndarray,
        speaker: str = SPEAKER_NONE,
        episode_id: str = "",
        *,
        source: str = "memory",
        modality: str = "memory",
        kind: str = "memory",
        epistemic_state: str = "remembered",
        evidence_refs: Optional[list[str]] = None,
        action_refs: Optional[list[str]] = None,
        unresolved: bool = False,
    ) -> Optional[str]:
        if not content.strip():
            return None
        nearest = self.field.nearest(shape, k=1)
        if nearest and nearest[0][1] >= self.cfg.create_threshold:
            return nearest[0][0].id
        f = self.field.add(
            content=content,
            shape=shape,
            speaker=speaker,
            episode_id=episode_id,
            source=source,
            modality=modality,
            kind=kind,
            epistemic_state=epistemic_state,
            evidence_refs=evidence_refs,
            action_refs=action_refs,
            unresolved=unresolved,
        )
        return f.id

    def _dream_seed(self) -> np.ndarray:
        all_f = self.field.all()
        if not all_f:
            return self.embedder.embed("我在确认自己是谁，刚才在做什么，接下来要做什么。")
        import random
        if random.random() < 0.75:
            recent = sorted(all_f, key=lambda f: f.last_flow_time,
                            reverse=True)[: max(3, min(12, len(all_f)))]
            return random.choice(recent).shape
        cold = sorted(all_f, key=lambda f: (f.flow_count, -f.quiet_seconds()))[
            : max(3, min(12, len(all_f)))]
        return random.choice(cold).shape

    def _compose_attention_seed(self, input_shape: np.ndarray) -> np.ndarray:
        """把输入形状和 SelfState 的形状混在一起当水流入水点。

        SelfState 的形状 = identity + current_focus + recent_summary 的嵌入。
        每次现算一次（很快），免得 SelfState 单独维护一个向量字段。"""
        seed = input_shape.astype(np.float32)
        weight = self.cfg.self_state_seed_weight
        if weight <= 0:
            return _normalize(seed)
        try:
            text_for_self = " ".join([
                self.self_state.identity[:80],
                self.self_state.current_focus[:80],
                self.self_state.recent_summary[:80],
                (self.reality_state.current_request.content[:120]
                 if self.reality_state.current_request and self.reality_state.current_request.status == "active"
                 else ""),
            ]).strip()
            if text_for_self:
                self_shape = self.embedder.embed(text_for_self)
                seed = seed + weight * self_shape
        except Exception:
            pass
        return _normalize(seed)

    def _tick_autosave(self) -> None:
        self._perceive_count += 1
        if self.cfg.autosave_every > 0 and self._perceive_count % self.cfg.autosave_every == 0:
            save_field(self.field, keep_backup=self.cfg.backup_keep)
            self.self_state.save(self._self_state_path)
            self.reality_state.save(self._reality_state_path)
        self.task_ledger.save()

    def _load_seeds(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        for c in chunks:
            self._maybe_create(c, self.embedder.embed(c), speaker=SPEAKER_NONE)
        self.field.sync_all()
        save_field(self.field, keep_backup=self.cfg.backup_keep)

    # ==========================================================
    # 渲染
    # ==========================================================
    def _format_recall_line(self, f: Fissure, in_episode: bool) -> str:
        content = self._truncate_for_recall(f.content, in_episode=in_episode)
        if in_episode:
            rel = self._current_turn_index - f.turn_index
            pos_label = self._relative_position_label(rel)
            role = self._speaker_label(f.speaker)
            sensory = self._sensory_label(f)
            labels = [pos_label, role, sensory]
            head = "[" + "·".join([x for x in labels if x]) + "]"
            return f"{head} {content}"
        age_label = _format_age(time.time() - f.creation_time)
        role = self._speaker_label(f.speaker)
        sensory = self._sensory_label(f)
        labels = [age_label, role, sensory]
        head = "[" + "·".join([x for x in labels if x]) + "]"
        return f"{head} {content}"

    def _truncate_for_recall(self, text: str, in_episode: bool) -> str:
        limit = (self.cfg.episode_chain_content_max_chars if in_episode
                 else max(self.cfg.max_fissure_chars,
                          self.cfg.episode_chain_content_max_chars * 2))
        text = text.strip()
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    @staticmethod
    def _sensory_label(f: Fissure) -> str:
        parts = []
        modality = getattr(f, "modality", "")
        kind = getattr(f, "kind", "")
        epistemic = getattr(f, "epistemic_state", "")
        modality_cn = {
            "hearing": "听见",
            "inner_speech": "内语",
            "seeing": "看见",
            "touching": "摸到",
            "memory": "记起",
            "proprioception": "本体感",
        }.get(modality, "")
        kind_cn = {
            "request": "请求",
            "utterance": "话语",
            "response": "回应",
            "thought": "念头",
            "observation": "观察",
            "error": "错误",
            "memory": "记忆",
        }.get(kind, "")
        epistemic_cn = {
            "observed": "已观察",
            "inferred": "推断",
            "imagined": "想象",
            "remembered": "记得",
            "unverified": "未验证",
            "verified": "有证据",
            "error": "动作失败",
        }.get(epistemic, "")
        for x in (modality_cn, kind_cn, epistemic_cn):
            if x and x not in parts:
                parts.append(x)
        if getattr(f, "unresolved", False):
            parts.append("未完成")
        return "·".join(parts)

    @staticmethod
    def _speaker_label(speaker: str) -> str:
        if speaker == SPEAKER_OUTSIDER:
            return "有人对我说"
        if speaker == SPEAKER_SELF:
            return "我说出口的话"
        if speaker == SPEAKER_DAYDREAM:
            return "我自己冒出来的念头"
        return ""

    @staticmethod
    def _relative_position_label(rel: int) -> str:
        if rel <= 1:
            return "刚刚"
        if rel == 2:
            return "上一句"
        if rel == 3:
            return "上上句"
        return f"{rel - 1} 句之前"
