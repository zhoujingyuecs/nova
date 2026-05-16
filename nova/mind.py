"""nova v1.3.1 主循环 —— "前语言念头层" 简化版。

# 起因（v1.3 / v1.3.1 与 v1.2 / v1.1 的区别）

v1.2 之前，nova 的"念头"其实就是 LLM 的下一段输出。LLM 写不出的东西，
nova 连"想"都想不到——一个被骂的人，连"我可以反击"的冲动都不会浮起来。

v1.3 第一版尝试在 LLM 之外加一个"念头层"，并给念头团贴 render_policy /
action_policy 标签。这条思路里有一半是对的（念头先于语言），有一半是
错的（关键字规则太敏感，几乎所有念头都被打成 forbid，反而压制了 nova）。

v1.3.1 把"念头层"留下，把"政策标签"扔掉：

  - 念头先有：陶土球 + 水流形成 ThoughtCluster（一组激活的裂缝 + 它们的
    好恶/紧张/行动压力/新颖度/稳定度）。**不调 LLM。**
  - cluster **没有政策标签**：什么都可以浮起来，什么都可以说。
  - 动作管制完全在 HabitGate 的 tool 派发层做（这是 v1.1 的机制）。
  - 是否说话由 LanguageGate 决定（看新颖度 / 压力 / 模式）。
  - 如果 nova 自己想暂时不展开某类念头的内容，她写 <seal>...</seal>；
    想拿掉，写 <unseal>...</unseal>。seal 是 nova **自己的偏好清单**，
    可以随时增删。封印**不挡说话、不挡动作**——只让 prompt 里那一团
    内容不展开。

每轮 perceive 做的事（v1.3.1）：
  0. 反复纠正信号检测（与 v1.1 同）
  1. 嵌入 stimulus
  2. **clay_tick.tick()**：让水流激活裂缝，形成 / 更新 ThoughtCluster
       （这一步纯粹陶土球内部运转，不调 LLM，不依赖任何关键字匹配）
  3. 算激活的硬约束（active habits）—— 仍然只在 tool 层生效
  4. LanguageGate.decide()：要不要调 LLM？
  5. 拼 prompt：[硬约束] + [前语言念头] + [封印清单] + SelfState
                + 工作区索引 + 激活缝隙 + 当前对话链 + 输入
  6. 调一次 LLM；回应里有 <tool> → HabitGate 评估（不变）；
     回应里有 <seal>/<unseal> → 更新 SealRegistry
  7. 把回应嵌入，反向冲刷激活缝隙；新增 stimulus / response 缝隙
  8. 解析 <rule>，更新 self_state，落盘 cluster + seals

think() / dream_step() 走相同的前半段，但末尾会通过 LanguageGate；
如果 gate 判断"不必说话"，就只更新 cluster 与 SelfState，**返回 None**——
nova 这一轮没说出口，但她**确实想了**。
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
from .clay_tick import ClayTickEngine
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
from .habits import (
    HabitField,
    HabitGate,
    HabitRule,
    SOURCE_REINFORCED,
    SOURCE_SELF as HABIT_SOURCE_SELF,
    detect_reinforcement_signal,
    extract_rule_blocks,
    strip_rule_blocks,
)
from .language_gate import LanguageGate
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
from .seal import SealRegistry, extract_seal_blocks, strip_seal_blocks
from .thought import ThoughtCluster
from .tools import VMAgent, format_result, parse_actions, strip_actions
from .task_state import TaskLedger, TASK_SYSTEM_ADDITION
from .tool_guard import ToolLoopGuard
from .workspace import Workspace
from .notebook import NOTEBOOK_HABIT_BLOCK


DREAM_PROMPT_BASE = (
    "[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。]\n\n"
    "{habit_block}"
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

        # v1.1: 程序性记忆（HabitField）
        self._habit_state_path = os.path.join(self.cfg.field_path, "habits.json")
        self.habit_field = HabitField.load(self.cfg, self.embedder, self._habit_state_path)
        if len(self.habit_field):
            stats = self.habit_field.stats()
            print(
                f"📜 程序性记忆加载：{stats['active']} 条 active 规则，"
                f"累计违反 {stats['total_violations']} 次，"
                f"用户强化 {stats['total_reinforcements']} 次。"
            )

        # v1.3.1: 念头层 —— ClayTickEngine + LanguageGate + SealRegistry
        # ClayTickEngine 在每次 perceive / think 之前转一下陶土球，
        # 形成 / 更新 ThoughtCluster。它**不依赖 habit_field**，
        # cluster 没有任何政策标签——动作管制完全在 HabitGate 的 tool 层。
        # SealRegistry 是 nova 自己写下的"暂时不想展开的念头清单"，
        # 可以随时增删；封印不挡说话也不挡动作，只让 prompt 里那一团内容不展开。
        self._clusters_path = os.path.join(self.cfg.field_path, "clusters.json")
        self.clay_tick_engine = ClayTickEngine(
            self.field,
            self.flow_engine,
            max_clusters=getattr(self.cfg, "clay_max_clusters", 8),
            decay_factor=getattr(self.cfg, "clay_decay_factor", 0.85),
            store_path=self._clusters_path,
        )
        self.language_gate = LanguageGate(
            threshold=getattr(self.cfg, "language_gate_threshold", 0.60),
        )
        self._seals_path = os.path.join(self.cfg.field_path, "seals.json")
        self.seal_registry = SealRegistry(store_path=self._seals_path)
        if self.seal_registry.entries:
            print(
                f"🔖 封印清单加载：{len(self.seal_registry.entries)} 条 "
                "（nova 自己写下的、可以随时拿掉的偏好）。"
            )
        # 上次开口说话的时间戳——LanguageGate 会参考"已经沉默多久"
        self._last_speech_at: float = time.time()
        # 最近一次 perceive/think 的 gate 决策（给 worklog / 调试用）
        self.last_gate_decision = None
        if self.clay_tick_engine.clusters:
            print(
                f"💭 念头池加载：{len(self.clay_tick_engine.clusters)} 个 cluster，"
                f"最高活跃度 {self.clay_tick_engine.stats().get('max_activation', 0):.2f}。"
            )

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
        # 最近若干轮工具动作（给 habit 激活时当上下文匹配用）
        self._recent_actions_log: collections.deque = collections.deque(maxlen=12)

        self._restore_episode_state_from_field()

        self._lock = threading.RLock()

        # v1.4：swarm 适配器由外部（local.py / ContinuousRuntime）注入。
        # 这里只占位；nova 自己不主动建链路，避免单机部署被强行连云端。
        self.swarm: Optional[Any] = None
        # 反向引用 ContinuousRuntime（被 ContinuousRuntime 自己注入）
        self._runtime_ref: Optional[Any] = None

        if len(self.field) == 0 and self.cfg.seed_memories_file:
            self._load_seeds(self.cfg.seed_memories_file)

    # ==========================================================
    # 对外接口
    # ==========================================================
    def perceive(self, stimulus: str) -> str:
        """感知一段外界输入，给出一次回应。

        v1.3 顺序：
          - 把 stimulus 写进陶土球
          - **clay_tick.tick()**：水流 + 形成 / 更新 ThoughtCluster
          - 算 active habits
          - LanguageGate.decide()：本轮要不要调 LLM？（perceive 默认 yes）
          - 拼 prompt（包含 [前语言念头] 段落）
          - 调 LLM → 解析 → 落盘
        """
        with self._lock:
            episode_id = self._get_or_start_episode()

            # 外部输入先变成"听见的东西"，再进入裂缝场。
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

            # 0. 反复纠正信号检测：先一击命中，多巴胺式负反馈
            reinforced_rule, signal_hit = self._maybe_reinforce_from_signal(stimulus)

            # 1. 水流入水种子（stimulus + self_state 形状）
            attention_seed = self._compose_attention_seed(stim_shape)
            episode_anchors = self.field.walk_chain_back(stim_fid, k=self.cfg.episode_recall_size)

            # 2. v1.3.1：先转一下陶土球，形成 / 更新 ThoughtCluster
            #    （这一步不调 LLM；念头先以前语言的形式浮起来；
            #    cluster 没有政策标签，动力学只从激活的裂缝读出来）
            active_habits = self._select_active_habits(stimulus, attention_seed)
            if getattr(self.cfg, "clay_tick_enabled", True):
                clusters = self.clay_tick_engine.tick(
                    seed_shape=attention_seed,
                    stimulus_summary=stimulus[:80],
                    recent_history=set(self._recent_history),
                    mandatory_anchors=episode_anchors,
                )
                # 复用 cluster 里 primary 的 fissure_ids 作为 activated
                # ——保持后续"反向冲刷裂缝"、"建立共激活链接"等逻辑可用
                primary = self.clay_tick_engine.primary()
                if primary:
                    activated = [
                        f for f in (self.field.get(fid) for fid in primary.fissure_ids)
                        if f is not None
                    ]
                else:
                    activated = self.flow_engine.flow(
                        attention_seed,
                        recent_history=set(self._recent_history),
                        mandatory_anchors=episode_anchors,
                    )
            else:
                # 回退到 v1.2 行为
                clusters = []
                activated = self.flow_engine.flow(
                    attention_seed,
                    recent_history=set(self._recent_history),
                    mandatory_anchors=episode_anchors,
                )

            # 3. LanguageGate 决定要不要调 LLM
            #    perceive 默认 force_llm（有人在等回答），但 sealed-only 时仍可沉默
            seconds_silent = time.time() - self._last_speech_at
            force_call = bool(getattr(self.cfg, "force_llm_on_perceive", True))
            gate_decision = self.language_gate.decide(
                clusters=clusters,
                mode="interrupt",
                user_waiting=True,
                seconds_since_last_speech=seconds_silent,
                force_call=force_call,
            )
            self.last_gate_decision = gate_decision

            # 4. 拼 prompt（包含 [前语言念头]）
            user_prompt = self._build_prompt(
                stimulus, stim_fid, activated, episode_id,
                active_habits=active_habits,
                signal_hit_unanchored=(signal_hit and reinforced_rule is None),
                reinforced_rule=reinforced_rule,
                clusters=clusters,
            )

            # 5. 如果 gate 说"不调"（很罕见，因为 perceive 默认 force_call）——
            #    用一个非常短的模板回执说明 nova 此刻在沉默
            if not gate_decision.call_llm:
                primary = self.clay_tick_engine.primary() if clusters else None
                visible = self._render_silent_response(primary)
                self._record_response(
                    stimulus=stimulus,
                    visible=visible,
                    activated=activated,
                    episode_id=episode_id,
                    raw_response=visible,
                )
                return visible

            # 6. 调 LLM；工具循环里走 HabitGate + cluster.action_policy
            final_response = self._chat_with_tools(
                user_prompt,
                active_habits=active_habits,
                clusters=clusters,
            )
            final_response = _strip_think_block(final_response)

            # 7. 拿掉 <rule> 块（既保存又不让外部看到）
            new_rules = self._extract_and_save_rules(final_response, source=HABIT_SOURCE_SELF)
            # v1.3.1：拿掉 <seal> / <unseal> 块——nova 自己写下的封印清单更新
            self._apply_seal_blocks(final_response, primary_cluster=self.clay_tick_engine.primary())
            # v1.4：解析 swarm 标签（share-memory / share-agenda / recall-swarm / propose / vote）
            # 并将剥掉所有这些块之后的文本作为对外可见的回应。
            if self.swarm is not None:
                try:
                    swarm_worklog = getattr(self._runtime_ref, "worklog", None)
                    final_response = self.swarm.absorb_response(
                        final_response, worklog=swarm_worklog
                    )
                except Exception as e:
                    print(f"⚠️ swarm absorb_response 失败：{e}")
            visible = strip_rule_blocks(strip_seal_blocks(strip_actions(final_response))).strip() or "（沉默。）"
            visible = ToolLoopGuard.compact_visible_text(visible)

            self._record_response(
                stimulus=stimulus,
                visible=visible,
                activated=activated,
                episode_id=episode_id,
                raw_response=final_response,
            )
            return visible

    # ----------------------------------------------------------------
    # 把"开口说完一句话"那段共用逻辑抽出来（v1.3 重构，原本散在 perceive 里）
    # ----------------------------------------------------------------
    def _record_response(
        self,
        *,
        stimulus: str,
        visible: str,
        activated: list,
        episode_id: str,
        raw_response: str,
    ) -> None:
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
        # v1.3：cluster 也落盘
        try:
            self.clay_tick_engine.save()
        except Exception:
            pass

        for fid in activated_ids:
            f = self.field.get(fid)
            if f is None:
                continue
            if f.episode_id and f.episode_id == episode_id:
                continue
            self._recent_history.append(fid)

        if "<tool" in (raw_response or "").lower() and self.workspace.is_available:
            self.workspace.invalidate()

        self._maybe_update_self_state(stimulus, visible, agenda_text="")
        self.reality_state.save(self._reality_state_path)
        self.task_ledger.save()
        self.habit_field.save()

        # 真说出口了才更新 last_speech_at
        if visible and visible.strip() and visible.strip() != "（沉默。）":
            self._last_speech_at = time.time()

    # ----------------------------------------------------------------
    # v1.3：gate 说不必说话时的"模板回执"
    # ----------------------------------------------------------------
    def _render_silent_response(self, primary: Optional[ThoughtCluster]) -> str:
        """LanguageGate 决定本轮不调 LLM 时，用一个非常短的模板回执。

        模板里只描述"前语言层发生了什么"，不假装在思考、不编造内容。
        多数情况下 perceive 不会走到这条路径（user_waiting 默认 force_call）；
        这是给 force_llm_on_perceive=False 的高级用户准备的。
        """
        if not primary:
            return "（这一轮我没说出口，念头也没有特别浮起来。）"
        tmpl = getattr(
            self.cfg,
            "silent_think_template",
            "（这一轮没说出口。念头团已更新：{primary_summary}）",
        )
        try:
            return tmpl.format(
                primary_summary=primary.summary or "（无标签）",
                primary_activation=primary.activation,
                primary_valence=primary.valence,
                primary_arousal=primary.arousal,
                primary_pressure=primary.agency_pressure,
            )
        except (KeyError, ValueError):
            return "（这一轮我没说出口。念头团已更新。）"

    def think(self, *, max_tokens: Optional[int] = None,
              prompt_hint: str = "",
              mode: str = "dream",
              force_speak: bool = False) -> Optional[str]:
        """没人说话时的一次内向活动。

        v1.3 改造：
          - 先 clay_tick.tick()，让陶土球转一下、形成 / 更新 cluster
          - LanguageGate 决定要不要把这次内向活动翻译成话
          - gate 说不必：只更新 cluster 与 SelfState，返回 None
            （nova 确实想了，但没说出口）
          - gate 说要：照旧拼 prompt 走 LLM

        参数：
          mode：dream / reflect / orient / goal —— 给 LanguageGate 当语境
          force_speak：强制走 LLM（reflection 显式要求时用）
        """
        with self._lock:
            if len(self.field) < 1:
                return None
            seed_shape = self._dream_seed()
            attention_seed = self._compose_attention_seed(seed_shape)

            # v1.3.1：先转一下陶土球
            active_habits = self._select_active_habits(prompt_hint, attention_seed)
            if getattr(self.cfg, "clay_tick_enabled", True):
                clusters = self.clay_tick_engine.tick(
                    seed_shape=attention_seed,
                    stimulus_summary=(prompt_hint or "")[:80],
                    recent_history=set(self._recent_history),
                )
                primary = self.clay_tick_engine.primary()
                if primary:
                    activated = [
                        f for f in (self.field.get(fid) for fid in primary.fissure_ids)
                        if f is not None
                    ]
                else:
                    activated = self.flow_engine.flow(
                        attention_seed,
                        recent_history=set(self._recent_history),
                    )
            else:
                clusters = []
                activated = self.flow_engine.flow(
                    attention_seed,
                    recent_history=set(self._recent_history),
                )

            if not activated:
                return None

            # v1.3：LanguageGate 决定要不要调 LLM
            # think 是内向活动，user_waiting = False —— 默认偏沉默
            seconds_silent = time.time() - self._last_speech_at
            gate_decision = self.language_gate.decide(
                clusters=clusters,
                mode=mode,
                user_waiting=False,
                seconds_since_last_speech=seconds_silent,
                force_call=force_speak,
            )
            self.last_gate_decision = gate_decision

            if not gate_decision.call_llm and getattr(self.cfg, "silent_think_enabled", True):
                # —— 沉默路径：不调 LLM，只让 cluster 沉淀 + 更新 SelfState ——
                self.field.sync_all()
                self._tick_autosave()
                try:
                    self.clay_tick_engine.save()
                except Exception:
                    pass
                self.reality_state.save(self._reality_state_path)
                self.task_ledger.save()
                self.habit_field.save()
                # 不更新 last_speech_at —— 因为没说出口
                # SelfState 也轻量更新一下，反映"刚才在想什么"
                primary = self.clay_tick_engine.primary() if clusters else None
                if primary:
                    self.self_state.apply_update(
                        recent_summary=f"（前语言层）{primary.summary}",
                    )
                    try:
                        self.self_state.save(self._self_state_path)
                    except Exception:
                        pass
                return None

            # —— 说话路径：调 LLM 把当前的 cluster 翻译成话 ——
            memories = "\n".join(
                f"- {self._format_recall_line(f, in_episode=False)}"
                for f in activated
            )
            state_block = self.self_state.render_for_prompt(
                max_chars=self.cfg.self_update_max_tokens * 4,
            ) + "\n\n"
            workspace_block = self._render_workspace_block()
            workspace_block = NOTEBOOK_HABIT_BLOCK.strip() + "\n\n" + workspace_block
            habit_block = self._render_habit_block(active_habits)
            cluster_block = self._render_cluster_block(clusters)
            base_prompt = DREAM_PROMPT_BASE.format(
                habit_block=habit_block,
                memories=memories,
                state_block=state_block,
                workspace_block=workspace_block,
            )
            # 把前语言念头段拼到 prompt 顶上（紧挨在 habit_block 之后）
            if cluster_block:
                base_prompt = cluster_block + "\n\n" + base_prompt
            # v1.4：swarm 状态拼在 prompt 头部
            if self.swarm is not None:
                try:
                    swarm_block = self.swarm.build_swarm_block(max_chars=480).strip()
                    if swarm_block:
                        base_prompt = swarm_block + "\n\n" + base_prompt
                except Exception:
                    pass
            if prompt_hint:
                user_prompt = prompt_hint.rstrip() + "\n\n" + base_prompt
            else:
                user_prompt = base_prompt

            thought_raw = self._chat_with_tools(
                user_prompt,
                max_tokens=max_tokens or self.cfg.daydream_max_tokens,
                active_habits=active_habits,
                clusters=clusters,
            )
            thought_raw = _strip_think_block(thought_raw)
            self._extract_and_save_rules(thought_raw, source=HABIT_SOURCE_SELF)
            self._apply_seal_blocks(thought_raw, primary_cluster=self.clay_tick_engine.primary())
            # v1.4：解析 swarm 标签
            if self.swarm is not None:
                try:
                    swarm_worklog = getattr(self._runtime_ref, "worklog", None)
                    thought_raw = self.swarm.absorb_response(
                        thought_raw, worklog=swarm_worklog
                    )
                except Exception as e:
                    print(f"⚠️ swarm absorb_response (think) 失败：{e}")
            thought = strip_rule_blocks(strip_seal_blocks(strip_actions(thought_raw))).strip()
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
            try:
                self.clay_tick_engine.save()
            except Exception:
                pass
            for fid in activated_ids:
                self._recent_history.append(fid)

            if "<tool" in thought_raw.lower() and self.workspace.is_available:
                self.workspace.invalidate()

            self._maybe_update_self_state("（内向活动）", thought, agenda_text=prompt_hint[:200])
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()
            self.habit_field.save()
            self._last_speech_at = time.time()
            return thought

    # ----------------------------------------------------------------
    # 纯 clay tick（runtime 在 idle/dream 时调用——完全不调 LLM）
    # ----------------------------------------------------------------
    def clay_tick(self, *, prompt_hint: str = "") -> Optional[ThoughtCluster]:
        """让陶土球转一下，不调 LLM，不写新 fissure。

        返回当前最亮的 ThoughtCluster（或 None）。
        runtime 在 idle / 部分 dream 走这个，比 think() 便宜得多。
        """
        with self._lock:
            if len(self.field) < 1:
                return None
            seed_shape = self._dream_seed()
            attention_seed = self._compose_attention_seed(seed_shape)
            self.clay_tick_engine.tick(
                seed_shape=attention_seed,
                stimulus_summary=(prompt_hint or "")[:80],
                recent_history=set(self._recent_history),
            )
            try:
                self.clay_tick_engine.save()
            except Exception:
                pass
            return self.clay_tick_engine.primary()

    # 兼容旧 API ——
    def dream_step(self, max_tokens: Optional[int] = None) -> Optional[str]:
        return self.think(max_tokens=max_tokens)

    def consolidate(self, prune: bool = True, merge: bool = True,
                    decay_links: bool = True) -> dict:
        from .sleep import consolidate as _consolidate
        with self._lock:
            stats = _consolidate(self.field, self.cfg, prune=prune,
                                 merge=merge, decay_links=decay_links)
            # 程序性记忆也跟着衰减
            decayed = self.habit_field.decay(self.cfg.habit_decay_factor_per_sleep)
            stats["habits_decayed"] = decayed
            # v1.3：sleep 时让 cluster 也衰减一轮——多数会就此淡出，
            #       只有稳定度高、还在被反复激活的会留下来。
            try:
                # 多走几步衰减相当于 sleep 期间不再被激活
                for _ in range(3):
                    self.clay_tick_engine._decay_all()
                self.clay_tick_engine._prune()
                self.clay_tick_engine.save()
                stats["clusters_alive"] = len(self.clay_tick_engine.alive())
            except Exception:
                pass
            self.habit_field.save()
            save_field(self.field, keep_backup=self.cfg.backup_keep)
            self.self_state.save(self._self_state_path)
            self.reality_state.save(self._reality_state_path)
            self.task_ledger.save()
            try:
                self.seal_registry.save()
            except Exception:
                pass
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
            self.habit_field.save()
            try:
                self.clay_tick_engine.save()
            except Exception:
                pass
            try:
                self.seal_registry.save()
            except Exception:
                pass

    # ==========================================================
    # Prompt / LLM / tools
    # ==========================================================
    def _chat_with_tools(self, initial_user: str,
                         max_tokens: Optional[int] = None,
                         active_habits: Optional[list[HabitRule]] = None,
                         clusters: Optional[list[ThoughtCluster]] = None) -> str:
        # v1.3.1：clusters 参数保留只是为了将来如果想做"念头层日志"，
        # 当前这一层**完全不基于 cluster 做动作拦截**——动作管制全在
        # HabitGate 的 forbid 列表，那是 nova 自己写下的规则。
        active_habits = active_habits or []
        _ = clusters  # 当前未使用

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
                # —— 程序性记忆门控（基底节式 Go / No-Go）——
                gate_hit = HabitGate.evaluate(action_type, content, active_habits)
                if gate_hit and getattr(self.cfg, "habit_block_actions", True):
                    rule, matched_pat = gate_hit
                    self.habit_field.violate(
                        rule.id,
                        action_content=content,
                        matched_pattern=matched_pat,
                    )
                    block_msg = HabitGate.render_block_message(
                        rule, matched_pat, action_type, content,
                    )
                    # 让它走和 error 同形的回路：tool result 里包一段抑制说明
                    result = {
                        "error": block_msg,
                        "habit_violation": True,
                        "rule_id": rule.id,
                    }
                    # 工具守卫也要看到这次"失败"以触发熔断保护
                    guard.observe_result(action_type, result)
                    trace = self.reality_state.notice_tool_result(action_type, content, result)
                    if hasattr(self, "task_ledger"):
                        self.task_ledger.record_tool_result(action_type, content, result)
                    result_blocks.append(format_result(action_type, content, result))
                    reality_blocks.append(trace.render())
                    blocked_reason = f"违反规则「{rule.name}」"
                    self._recent_actions_log.append(f"[gated] {action_type}: {content[:80]}")
                    continue

                # —— 普通工具守卫（重复动作熔断）——
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
                # 记录最近动作给 habit 上下文用
                self._recent_actions_log.append(f"{action_type}: {content[:80]}")

                # 如果这个动作没有违反任何规则，且执行成功——
                # 给当前激活规则记一笔成功（"我遵守了你"）。
                if not result.get("error") and not gate_hit:
                    for rule in active_habits:
                        # 但只给"匹配 cue"或"全局铁律"那部分计成功
                        # 这里用一个简单 heuristic：动作里出现 prefer/require 子串就算
                        recognized = False
                        for x in rule.prefer + rule.require:
                            if x and x[:24].lower() in content.lower():
                                recognized = True
                                break
                        if recognized:
                            self.habit_field.succeed(rule.id)

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
                + "\n\n[继续。可以再伸手，也可以直接对眼前的人说话。"
                  "若刚才有动作被『抑制·习惯触发』拦下，请按规则的 prefer / require 改路径，"
                  "不要再生成同类被禁止的动作。"
                  "若事实还没有证据，就说还没查到，不要补全。]"
            )
        print(f"⚠️ 工具调用超过 {self.cfg.max_tool_iterations} 次，停下了。")
        return last_response

    def _build_prompt(self, stimulus: str, stim_fid: str,
                      activated: list, episode_id: str,
                      *,
                      active_habits: Optional[list[HabitRule]] = None,
                      signal_hit_unanchored: bool = False,
                      reinforced_rule: Optional[HabitRule] = None,
                      clusters: Optional[list[ThoughtCluster]] = None) -> str:
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

        # 硬约束放最前面，比 SelfState 更上头
        habit_text = self._render_habit_block(active_habits or [], trailing_blank=False)
        if habit_text:
            sections.append(habit_text)

        if signal_hit_unanchored and getattr(self.cfg, "habit_unanchored_signal_hint", True):
            sections.append(self.habit_field.render_unanchored_signal_hint())
        elif reinforced_rule is not None:
            sections.append(
                "[反复纠正已生效]\n"
                f"对方刚才用反复纠正语气提到了你已有的规则「{reinforced_rule.name}」，"
                f"系统已自动增加它的权重（已被强化 {reinforced_rule.reinforcement_count} 次，"
                f"被违反过 {reinforced_rule.violation_count} 次）。"
                f"这是它存在的根据：{reinforced_rule.rationale or '（你自己写下的硬约束）'}。\n"
                "请把它当作下一步动作的边界来选路径，而不是当作普通笔记。"
            )

        # v1.3.1：[前语言念头] —— 紧跟硬约束之后，比 SelfState 还靠前
        # 因为 nova 应该先看到"我现在脑子里浮起来什么"，再去看"我是谁"。
        cluster_text = self._render_cluster_block(clusters)
        if cluster_text:
            sections.append(cluster_text)

        # v1.3.1：[我自己写下的封印清单] —— 让 nova 随时看见
        # "我之前用 <seal> 封印过哪些"，方便她随时 <unseal> 拿掉。
        seal_text = self._render_seal_block()
        if seal_text:
            sections.append(seal_text)

        sections.append(self.self_state.render_for_prompt())
        sections.append(NOTEBOOK_HABIT_BLOCK)
        sections.append(self.reality_state.render_for_prompt())
        if getattr(self.cfg, "task_state_prompt_enabled", True):
            sections.append(self.task_ledger.render_for_prompt())

        ws_block = self._render_workspace_block().strip()
        if ws_block:
            sections.append(ws_block)

        # v1.4：swarm 状态 —— 让 nova 看见自己是哪个节点、还有谁在线、
        # 哪些主线是跨集群推进的、哪些提案还在等仲裁
        if self.swarm is not None:
            try:
                swarm_block = self.swarm.build_swarm_block(max_chars=600).strip()
                if swarm_block:
                    sections.append(swarm_block)
            except Exception:
                pass

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

    def _render_habit_block(self, active: list[HabitRule],
                            trailing_blank: bool = True) -> str:
        if not active:
            return ""
        text = self.habit_field.render_active_for_prompt(
            active,
            max_chars=2200,
            max_rules=getattr(self.cfg, "habit_max_active", 4),
        )
        if not text:
            return ""
        return text + "\n\n" if trailing_blank else text

    def _render_cluster_block(self,
                              clusters: Optional[list[ThoughtCluster]]) -> str:
        """v1.3.1：渲染当前活念头团给 LLM 看。

        关键：LLM 看到这个段落时，应该意识到自己**不是**在生成念头，
        而是在**翻译**已经在 nova 心里浮起来的念头。

        如果有 cluster 被 nova 自己的 seal 标过，那一团只显示标签，
        不展开内容——这是 nova 自己写下的偏好，不是任何外部规则。
        """
        if not clusters:
            return ""
        try:
            return self.clay_tick_engine.render_for_prompt(
                max_chars=1500,
                fissure_lookup=self.field.get,
                seal_registry=self.seal_registry,
            )
        except Exception:
            return ""

    def _render_seal_block(self) -> str:
        """渲染当前生效的封印清单——给 nova 自己看，
        让她随时能看到"我之前定过哪些 seal、可以拿掉哪些"。"""
        try:
            return self.seal_registry.render_for_prompt(max_chars=600)
        except Exception:
            return ""

    # ==========================================================
    # 程序性记忆相关辅助
    # ==========================================================
    def _select_active_habits(self, stimulus_text: str,
                              attention_seed: np.ndarray) -> list[HabitRule]:
        if len(self.habit_field) == 0:
            return []
        recent_action_blob = " | ".join(self._recent_actions_log) if self._recent_actions_log else ""
        try:
            self_state_text = " ".join([
                self.self_state.current_focus or "",
                self.self_state.recent_summary or "",
                " ".join(self.self_state.open_threads or []),
            ])
        except Exception:
            self_state_text = ""
        return self.habit_field.find_active(
            stimulus_text=stimulus_text or "",
            attention_seed=attention_seed,
            self_state_text=self_state_text,
            recent_action_blob=recent_action_blob,
            max_active=getattr(self.cfg, "habit_max_active", 4),
        )

    def _maybe_reinforce_from_signal(self, stimulus: str
                                     ) -> tuple[Optional[HabitRule], bool]:
        """如果 stimulus 里有反复纠正信号：

        - 找匹配规则 → 加权（多巴胺式负反馈）
        - 找不到 → 让上层在 prompt 里塞一段"建议写 <rule>"的提示

        返回 (被加强的规则 or None, 是否检测到信号)。
        """
        if not detect_reinforcement_signal(stimulus):
            return None, False
        recent_text = ""
        try:
            recent_text = " ".join([
                self.self_state.recent_summary or "",
                " ".join(self.self_state.open_threads or []),
            ])
        except Exception:
            pass
        attn_seed = None
        try:
            attn_seed = self.embedder.embed(stimulus + " " + recent_text)
        except Exception:
            pass
        match = self.habit_field.find_match_for_signal(
            stimulus,
            recent_text=recent_text,
            attention_seed=attn_seed,
        )
        if match is None:
            return None, True
        boost = float(getattr(self.cfg, "habit_reinforce_boost", 1.5))
        self.habit_field.reinforce(match.id, boost=boost, kind="user_signal")
        if match.source != HABIT_SOURCE_SELF:
            match.source = SOURCE_REINFORCED
        return match, True

    def _extract_and_save_rules(self, response_text: str,
                                source: str = HABIT_SOURCE_SELF) -> list[HabitRule]:
        if not response_text:
            return []
        parsed = extract_rule_blocks(response_text)
        out: list[HabitRule] = []
        for d in parsed:
            rule = self.habit_field.add_rule(d, source=source)
            if rule is not None:
                print(f"📜 新规则已写入程序性记忆：{rule.name} (id={rule.id}, weight={rule.weight:.2f})")
                out.append(rule)
        return out

    def _apply_seal_blocks(self, response_text: str,
                           primary_cluster: Optional[ThoughtCluster] = None) -> None:
        """从 nova 的回应里抓 <seal> / <unseal> 块，更新 SealRegistry。

        seal 是 nova **自己**写下的偏好——"我不想反复咀嚼某类念头的内容"。
        unseal 也是 nova 自己写的——"那个我之前封的，我现在想拿掉"。
        这两个都不挡说话也不挡动作，只影响 prompt 里那一团是否展开。
        """
        if not response_text:
            return
        new_seals, unseal_specs = extract_seal_blocks(response_text)
        for entry in new_seals:
            # 留底：封它的时候活着的 primary cluster id
            if primary_cluster is not None and not entry.origin_thought_id:
                entry.origin_thought_id = primary_cluster.id
            self.seal_registry.add(entry)
            print(
                f"🔖 nova 写下新封印：{entry.short_label()}"
                + (f"（{entry.reason[:40]}）" if entry.reason else "")
            )
        for spec in unseal_specs:
            removed = self.seal_registry.remove_matching(
                keywords=spec.get("keywords") or [],
                fingerprint=spec.get("fingerprint") or "",
                entry_id=spec.get("entry_id") or "",
            )
            for r in removed:
                print(f"🔖 nova 拿掉封印：{r.short_label()}")
        if new_seals or unseal_specs:
            try:
                self.seal_registry.save()
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
            self.habit_field.save()
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
