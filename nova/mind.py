"""Nova v0.8：Self Loop 直接替换版主循环。

这一版把 v0.6 的“主意识短字符串”升级为 SelfField + DriveSystem：
主意识不只是 prompt 前面的一段文本，而会参与 attention seed，改变
普通记忆水流的入水位置。nova 的自我改进也不靠人类手动调 prompt，
而是通过 Metacognition / SkillBook / SelfModificationLog 在运行中
沉淀和调整。
"""
from __future__ import annotations

import collections
import json
import os
import random
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
from .notes import NotesBook
from .persistence import load_field, save_field
from .self_field import SelfField
from .drives import DriveSystem
from .metacognition import Metacognition
from .skills import SkillBook
from .self_modification import SelfModificationLog
from .autonomy import choose_autonomy_mode, build_dream_header
from .tools import (
    CAPABILITY_MEMORIES,
    TOOL_SYSTEM_ADDITION,
    VMAgent,
    format_result,
    parse_actions,
    strip_actions,
)


DREAM_PROMPT_BASE = (
    "[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。]\n\n"
    "{consciousness_block}"
    "[下面这些片段浮上心头——是素材，不是替代品：]\n\n"
    "{memories}\n\n"
    "[你现在脑子里在想什么？写一两句就好，像在自言自语。"
    "主意识仍是你的主线；如果你想伸手做点什么，就伸；如果该沉淀经验，就沉淀。]"
)

IMAGERY_EXTRACTION_PROMPT = """\
下面这段话里包含了几个不同的“意象”——可能是一个画面、一种感受、一个想法、
一个具体的场景或细节。请把它们按出现顺序拆出来，每行一条，每条 8~40 字。
不要解释、不要编号。最少 1 条，最多 {max_count} 条。

——— 原文 ———
{text}

——— 意象（每行一条）———"""

NOTES_UPDATE_PROMPT = """\
你正在帮 nova 维护她的“笔记本”：一份她确认知道、以后可以直接依赖的清单。
只有明确的步骤、工具用法、重要事实、被纠正的误解、长期偏好值得记。
一时情绪、隐喻、一次性场景不要记。多数情况下请只输出“（无变动。）”。

〖当前笔记本〗
{notes_text}

〖Self Loop / 主意识〗
{main_consciousness}

〖刚刚发生〗
{event}

请输出 0~3 行动作。每行严格用以下格式之一：
[ADD] 新笔记内容
[UPDATE id=<id>] 修订后的内容
[REMOVE id=<id>]
如果没有任何要变的，只输出：
（无变动。）
"""

_NOTES_ADD_RE = re.compile(r"^\s*\[\s*ADD\s*\]\s*(.+?)\s*$", re.IGNORECASE)
_NOTES_UPDATE_RE = re.compile(r"^\s*\[\s*UPDATE\s+id\s*=\s*([A-Za-z0-9_]+)\s*\]\s*(.+?)\s*$", re.IGNORECASE)
_NOTES_REMOVE_RE = re.compile(r"^\s*\[\s*REMOVE\s+id\s*=\s*([A-Za-z0-9_]+)\s*\]\s*$", re.IGNORECASE)
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


def _parse_notes_actions(raw: str) -> list[tuple]:
    actions = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NOTES_ADD_RE.match(line)
        if m:
            actions.append(("add", m.group(1).strip()))
            continue
        m = _NOTES_UPDATE_RE.match(line)
        if m:
            actions.append(("update", m.group(1).strip(), m.group(2).strip()))
            continue
        m = _NOTES_REMOVE_RE.match(line)
        if m:
            actions.append(("remove", m.group(1).strip()))
    return actions


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
        self._perceive_count = 0
        self._recent_history = collections.deque(maxlen=self.cfg.recent_history_size)

        self._current_episode_id: str = ""
        self._last_episode_activity: float = 0.0
        self._last_episode_fid: str = ""
        self._current_turn_index: int = 0
        self._main_consciousness: str = ""

        self._self_loop_paths = {
            "self_field": os.path.join(self.cfg.field_path, "self_field.json"),
            "drives": os.path.join(self.cfg.field_path, "drives.json"),
            "skills": os.path.join(self.cfg.field_path, "skills.json"),
            "self_modification": os.path.join(self.cfg.field_path, "self_modification.json"),
        }
        self.self_field = SelfField(
            self.embedder.dim,
            max_chars=getattr(self.cfg, "self_loop_self_max_chars_in_prompt", 1800),
        )
        self.drives = DriveSystem(self.embedder.dim)
        self.metacognition = Metacognition()
        self.skills = SkillBook(
            path=self._self_loop_paths["skills"],
            max_skills=getattr(self.cfg, "skills_max_total", 80),
        )
        self.self_modification = SelfModificationLog(
            path=self._self_loop_paths["self_modification"],
        )

        notes_path = os.path.join(self.cfg.field_path, "notes.json")
        self.notes = NotesBook(
            path=notes_path,
            max_total=self.cfg.notes_max_total,
            max_chars_per_note=self.cfg.notes_max_chars_per_note,
        )
        self.notes.load()

        self._restore_episode_state_from_field()
        self._load_main_consciousness()
        if getattr(self.cfg, "self_loop_enabled", True):
            self._load_self_loop()

        self._lock = threading.RLock()

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

        if len(self.field) == 0 and self.cfg.seed_memories_file:
            self._load_seeds(self.cfg.seed_memories_file)
        if self.vm_agent is not None:
            self._ensure_capability_memories()

    # ==========================================================
    # 对外接口
    # ==========================================================
    def perceive(self, stimulus: str) -> str:
        """感知一段外界输入，给出一次回应。"""
        with self._lock:
            episode_id = self._get_or_start_episode()

            stim_shape = self.embedder.embed(stimulus)
            stim_fid = self._open_episode_turn(
                content=stimulus,
                shape=stim_shape,
                speaker=SPEAKER_OUTSIDER,
                episode_id=episode_id,
            )

            imagery_fids: list[str] = []
            if self.cfg.imagery_enabled and len(stimulus) >= self.cfg.imagery_min_input_chars:
                try:
                    imagery_fids = self._extract_and_link_imageries(
                        stimulus, speaker=SPEAKER_OUTSIDER, episode_id=episode_id
                    )
                    for ifid in imagery_fids:
                        self.field.link(stim_fid, ifid, strength_delta=self.cfg.imagery_link_base)
                        self.field.link(ifid, stim_fid, strength_delta=self.cfg.imagery_link_base * 0.7)
                except Exception as e:
                    print(f"⚠️ 意象拆解失败（不致命，跳过）：{e}")

            episode_anchors = self.field.walk_chain_back(stim_fid, k=self.cfg.episode_recall_size)
            attention_seed = self._compose_attention_seed(stim_shape)
            activated = self.flow_engine.flow(
                attention_seed,
                recent_history=set(self._recent_history),
                mandatory_anchors=episode_anchors,
            )

            user_prompt = self._build_prompt(stimulus, stim_fid, activated, episode_id)
            final_response = self._chat_with_tools(user_prompt)
            final_response = _strip_think_block(final_response)
            visible = strip_actions(final_response).strip() or "（沉默。）"

            response_shape = self.embedder.embed(visible)
            self._reshape(activated, visible, response_shape)

            response_fid = self._open_episode_turn(
                content=visible,
                shape=response_shape,
                speaker=SPEAKER_SELF,
                episode_id=episode_id,
            )

            activated_ids = [f.id for f in activated]
            soft_chain_ids = imagery_fids + activated_ids
            if len(soft_chain_ids) >= 2:
                self.field.link_chain(
                    soft_chain_ids,
                    base_strength=self.cfg.flow_coactivation_link_strength,
                    decay=self.cfg.imagery_link_decay,
                    max_distance=self.cfg.flow_coactivation_distance,
                    bidirectional=False,
                )
            for fid in soft_chain_ids[-self.cfg.flow_coactivation_distance:]:
                self.field.link(fid, response_fid, strength_delta=self.cfg.flow_coactivation_link_strength)

            self.field.sync_all()
            self._tick_autosave()

            for fid in soft_chain_ids:
                f = self.field.get(fid)
                if f is None:
                    continue
                if f.episode_id and f.episode_id == episode_id:
                    continue
                self._recent_history.append(fid)

            if getattr(self.cfg, "self_loop_enabled", True):
                self._self_loop_after_perceive(
                    stimulus=stimulus,
                    visible=visible,
                    stim_shape=stim_shape,
                    response_shape=response_shape,
                    episode_id=episode_id,
                )
            else:
                self._main_consciousness = visible[: self.cfg.main_consciousness_max_chars]
                self._save_main_consciousness()

            self._update_notes_from_perceive(stimulus, visible)
            return visible

    def dream_step(self, max_tokens: Optional[int] = None) -> Optional[str]:
        """做一次内向活动。v0.8 中它由 DriveSystem 选择模式。"""
        with self._lock:
            if len(self.field) < 1:
                return None
            seed_shape = self._dream_seed()
            mode = choose_autonomy_mode(self.drives, self.self_field) if getattr(self.cfg, "self_loop_enabled", True) else "free_dream"
            if getattr(self.cfg, "self_loop_enabled", True):
                seed_shape = self._compose_attention_seed(seed_shape)

            activated = self.flow_engine.flow(seed_shape, recent_history=set(self._recent_history))
            if not activated:
                return None

            memories = "\n".join(f"- {self._format_recall_line(f, in_episode=False)}" for f in activated)
            consciousness_block = self._render_consciousness_block_for_dream()
            user_prompt = DREAM_PROMPT_BASE.format(memories=memories, consciousness_block=consciousness_block)
            if getattr(self.cfg, "self_loop_enabled", True):
                user_prompt = build_dream_header(mode) + user_prompt

            thought_raw = self._chat_with_tools(user_prompt, max_tokens=max_tokens or self.cfg.daydream_max_tokens)
            thought_raw = _strip_think_block(thought_raw)
            thought = strip_actions(thought_raw).strip()
            if not thought:
                return None

            activated_ids = [f.id for f in activated]
            if len(activated_ids) >= 2:
                self.field.link_chain(
                    activated_ids,
                    base_strength=self.cfg.flow_coactivation_link_strength * 0.7,
                    decay=self.cfg.imagery_link_decay,
                    max_distance=self.cfg.flow_coactivation_distance,
                    bidirectional=False,
                )
            thought_shape = self.embedder.embed(thought)
            self._reshape(activated, thought, thought_shape)
            thought_fid = self._maybe_create(thought, thought_shape, speaker=SPEAKER_DAYDREAM)
            if thought_fid is not None:
                for fid in activated_ids[-self.cfg.flow_coactivation_distance:]:
                    self.field.link(fid, thought_fid, strength_delta=self.cfg.flow_coactivation_link_strength)

            self.field.sync_all()
            self._tick_autosave()
            for fid in activated_ids:
                self._recent_history.append(fid)

            if getattr(self.cfg, "self_loop_enabled", True):
                self._self_loop_after_daydream(thought=thought, thought_shape=thought_shape, mode=mode)
            else:
                self._main_consciousness = thought[: self.cfg.main_consciousness_max_chars]
                self._save_main_consciousness()
            return thought

    def consolidate(self, prune: bool = True, merge: bool = True, decay_links: bool = True) -> dict:
        from .sleep import consolidate as _consolidate
        with self._lock:
            stats = _consolidate(self.field, self.cfg, prune=prune, merge=merge, decay_links=decay_links)
            save_field(self.field)
            self._save_main_consciousness()
            self._save_self_loop()
            self.notes.save()
            return stats

    def visualize(self, output_path: str, method: str = "pca", **kwargs) -> Optional[str]:
        from .visualize import render_field
        with self._lock:
            return render_field(self.field, output_path, method=method, **kwargs)

    def save(self) -> None:
        with self._lock:
            save_field(self.field)
            self._save_main_consciousness()
            self._save_self_loop()
            self.notes.save()

    # ==========================================================
    # Prompt / LLM / tools
    # ==========================================================
    def _system_prompt(self) -> str:
        if self.vm_agent is not None:
            return self.cfg.system_prompt + TOOL_SYSTEM_ADDITION
        return self.cfg.system_prompt

    def _chat_with_tools(self, initial_user: str, max_tokens: Optional[int] = None) -> str:
        system = self._system_prompt()
        if self.vm_agent is None:
            return self.llm.chat(system, initial_user, max_tokens=max_tokens)

        current_user = initial_user
        last_response = ""
        for _ in range(self.cfg.max_tool_iterations):
            response = self.llm.chat(system, current_user, max_tokens=max_tokens)
            last_response = response
            actions = parse_actions(response)
            if not actions:
                return response
            result_blocks = []
            for action_type, content in actions:
                try:
                    result = self.vm_agent.dispatch(action_type, content)
                except Exception as e:
                    result = {"error": str(e)}
                result_blocks.append(format_result(action_type, content, result))
            current_user = (
                current_user
                + "\n\n[你刚才在心里这样转过：]\n"
                + response
                + "\n\n[手回来了，带回这些：]\n"
                + "\n\n".join(result_blocks)
                + "\n\n[继续。可以再伸手，也可以直接把要落下的话写出来。]"
            )
        print(f"⚠️ 工具调用超过 {self.cfg.max_tool_iterations} 次，停下了。")
        return last_response

    def _build_prompt(self, stimulus: str, stim_fid: str, activated: list[Fissure], episode_id: str) -> str:
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
        if getattr(self.cfg, "self_loop_enabled", True):
            block = self._render_self_loop_prompt_header()
            if block:
                sections.append(block)
        elif self._main_consciousness:
            sections.append("[你现在的状态——你的主意识]\n" + self._main_consciousness)

        notes_block = self._render_notes_block_for_prompt()
        if notes_block:
            sections.append(notes_block)

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

    def _render_self_loop_prompt_header(self) -> str:
        blocks = [
            self.self_field.render_prompt_block(max_chars=getattr(self.cfg, "self_loop_self_max_chars_in_prompt", 1800)),
            self.drives.render_prompt_block(),
        ]
        if getattr(self.cfg, "skills_enabled", True):
            skill_block = self.skills.render_prompt_block(max_chars=getattr(self.cfg, "self_loop_skills_max_chars_in_prompt", 1200))
            if skill_block:
                blocks.append(skill_block)
        if getattr(self.cfg, "self_modification_enabled", True):
            patch_block = self.self_modification.render_prompt_block()
            if patch_block:
                blocks.append(patch_block)
        return "\n\n".join(b for b in blocks if b)

    def _render_consciousness_block_for_dream(self) -> str:
        if getattr(self.cfg, "self_loop_enabled", True):
            block = self._render_self_loop_prompt_header()
            return block + "\n\n" if block else ""
        if self._main_consciousness:
            return f"[你现在的状态——你的主意识]\n{self._main_consciousness}\n\n"
        return ""

    def _render_notes_block_for_prompt(self) -> str:
        if not getattr(self.cfg, "notes_enabled", True):
            return ""
        txt = self.notes.render_for_prompt(max_chars=self.cfg.notes_max_chars_in_prompt)
        if not txt:
            return ""
        return "[你已经学会的事 / 你确认知道的事实]\n" + txt

    # ==========================================================
    # Self Loop
    # ==========================================================
    def _load_self_loop(self) -> None:
        self.self_field.load(self._self_loop_paths["self_field"])
        self.drives.load(self._self_loop_paths["drives"])
        self.skills.load()
        self.self_modification.load()
        self.self_field.ensure_bootstrap(self.embedder.embed)
        self.drives.ensure_bootstrap(self.embedder.embed)
        self._main_consciousness = self.self_field.render_main_text() or self._main_consciousness

    def _save_self_loop(self) -> None:
        if not getattr(self.cfg, "self_loop_enabled", True):
            return
        try:
            self.self_field.save(self._self_loop_paths["self_field"])
            self.drives.save(self._self_loop_paths["drives"])
            if getattr(self.cfg, "skills_enabled", True):
                self.skills.save()
            if getattr(self.cfg, "self_modification_enabled", True):
                self.self_modification.save()
        except Exception as e:
            print(f"⚠️ Self Loop 存档失败（不致命）：{e}")

    def _compose_attention_seed(self, input_shape: np.ndarray) -> np.ndarray:
        if not getattr(self.cfg, "self_loop_enabled", True):
            return input_shape
        seed = input_shape.astype(np.float32)
        self_shape = self.self_field.current_shape()
        drive_shape = self.drives.current_shape()
        seed = (
            seed
            + getattr(self.cfg, "self_loop_self_seed_weight", 0.55) * self_shape
            + getattr(self.cfg, "self_loop_drive_seed_weight", 0.25) * drive_shape
        )
        return _normalize(seed)

    def _self_loop_after_perceive(self, *, stimulus: str, visible: str, stim_shape: np.ndarray, response_shape: np.ndarray, episode_id: str) -> None:
        self.self_field.observe_turn(
            user_text=stimulus,
            response_text=visible,
            user_shape=stim_shape,
            response_shape=response_shape,
            episode_id=episode_id,
            embed_fn=self.embedder.embed,
        )
        self.drives.observe_event(stimulus=stimulus, response=visible)
        if getattr(self.cfg, "metacognition_enabled", True):
            actions = self.metacognition.reflect(stimulus=stimulus, response=visible)
            for action in actions:
                self.self_field.apply_action(action, embed_fn=self.embedder.embed)
                self.drives.apply_action(action, embed_fn=self.embedder.embed)
                if getattr(self.cfg, "skills_enabled", True):
                    self.skills.apply_action(action)
            if getattr(self.cfg, "self_modification_enabled", True):
                self.self_modification.observe_actions(actions, self.drives, self.skills)
        if getattr(self.cfg, "skills_enabled", True):
            self.skills.observe_event(stimulus=stimulus, response=visible)
        self._main_consciousness = self.self_field.render_main_text()
        self._save_self_loop()

    def _self_loop_after_daydream(self, *, thought: str, thought_shape: np.ndarray, mode: str = "free_dream") -> None:
        self.self_field.observe_daydream(thought, thought_shape, embed_fn=self.embedder.embed, mode=mode)
        self.drives.observe_event(daydream=thought)
        if getattr(self.cfg, "metacognition_enabled", True):
            actions = self.metacognition.reflect(daydream=thought)
            for action in actions:
                self.self_field.apply_action(action, embed_fn=self.embedder.embed)
                self.drives.apply_action(action, embed_fn=self.embedder.embed)
                if getattr(self.cfg, "skills_enabled", True):
                    self.skills.apply_action(action)
            if getattr(self.cfg, "self_modification_enabled", True):
                self.self_modification.observe_actions(actions, self.drives, self.skills)
        self._main_consciousness = self.self_field.render_main_text()
        self._save_self_loop()

    # ==========================================================
    # Episode / field helpers
    # ==========================================================
    def _get_or_start_episode(self) -> str:
        now = time.time()
        gap = now - self._last_episode_activity
        if (not self._current_episode_id) or gap > self.cfg.episode_gap_seconds:
            self._current_episode_id = uuid.uuid4().hex[:8]
            self._last_episode_fid = ""
            self._current_turn_index = 0
            if getattr(self.cfg, "self_loop_enabled", True) and hasattr(self, "self_field"):
                self.self_field.decay_session(getattr(self.cfg, "self_loop_episode_session_decay", 0.55))
                self.drives.observe_event(response="新 episode 开始，短期场景退潮但核心自我保持。")
                self._main_consciousness = self.self_field.render_main_text()
            else:
                self._main_consciousness = ""
            self._last_episode_activity = now
        return self._current_episode_id

    def _open_episode_turn(self, content: str, shape: np.ndarray, speaker: str, episode_id: str) -> str:
        f = self.field.add(content=content, shape=shape, speaker=speaker, episode_id=episode_id, turn_index=self._current_turn_index)
        self._current_turn_index += 1
        prev_id = self._last_episode_fid
        if prev_id:
            self.field.chain_link(prev_id, f.id, self.cfg.episode_link_forward, self.cfg.episode_link_backward)
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

    def _reshape(self, activated: list[Fissure], new_content: str, response_shape: np.ndarray) -> None:
        for f in activated:
            p = self.field.plasticity_at(f.shape)
            f.shift_toward(response_shape, plasticity=p, new_content=new_content, rewrite_threshold=0.45)

    def _maybe_create(self, content: str, shape: np.ndarray, speaker: str = SPEAKER_NONE, episode_id: str = "") -> Optional[str]:
        if not content.strip():
            return None
        nearest = self.field.nearest(shape, k=1)
        if nearest and nearest[0][1] >= self.cfg.create_threshold:
            return nearest[0][0].id
        f = self.field.add(content=content, shape=shape, speaker=speaker, episode_id=episode_id)
        return f.id

    def _find_or_create(self, content: str, shape: np.ndarray, speaker: str = SPEAKER_NONE, episode_id: str = "") -> str:
        fid = self._maybe_create(content, shape, speaker=speaker, episode_id=episode_id)
        if fid is not None:
            return fid
        nearest = self.field.nearest(shape, k=1)
        return nearest[0][0].id if nearest else self.field.add(content=content, shape=shape, speaker=speaker, episode_id=episode_id).id

    def _dream_seed(self) -> np.ndarray:
        all_f = self.field.all()
        if not all_f:
            return self.embedder.embed("我在确认自己是谁，刚才在做什么，接下来要做什么。")
        if random.random() < 0.75:
            recent = sorted(all_f, key=lambda f: f.last_flow_time, reverse=True)[: max(3, min(12, len(all_f)))]
            return random.choice(recent).shape
        cold = sorted(all_f, key=lambda f: (f.flow_count, -f.quiet_seconds()))[: max(3, min(12, len(all_f)))]
        return random.choice(cold).shape

    def _tick_autosave(self) -> None:
        self._perceive_count += 1
        if self.cfg.autosave_every > 0 and self._perceive_count % self.cfg.autosave_every == 0:
            save_field(self.field)
            self._save_main_consciousness()
            self._save_self_loop()
            self.notes.save()

    def _load_seeds(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        for c in chunks:
            self._maybe_create(c, self.embedder.embed(c), speaker=SPEAKER_NONE)
        self.field.sync_all()
        save_field(self.field)

    def _ensure_capability_memories(self) -> None:
        for c in CAPABILITY_MEMORIES:
            self._maybe_create(c, self.embedder.embed(c), speaker=SPEAKER_NONE)
        self.field.sync_all()

    # ==========================================================
    # Imagery / notes
    # ==========================================================
    def _extract_and_link_imageries(self, text: str, speaker: str = "", episode_id: str = "") -> list[str]:
        imageries = self._llm_extract_imageries(text)
        if not imageries:
            return []
        shapes = self.embedder.embed_batch(imageries)
        fids = []
        for content, shape in zip(imageries, shapes):
            fids.append(self._find_or_create(content, shape, speaker=speaker, episode_id=episode_id))
        if len(fids) >= 2:
            self.field.link_chain(fids, base_strength=self.cfg.imagery_link_base, decay=self.cfg.imagery_link_decay, max_distance=self.cfg.imagery_link_distance)
        return fids

    def _llm_extract_imageries(self, text: str) -> list[str]:
        prompt = IMAGERY_EXTRACTION_PROMPT.format(text=text, max_count=self.cfg.imagery_max_count)
        system = "你是一个把整段话拆成意象列表的工具。每行一条，不写编号、不解释。"
        raw = self.llm.chat(system, prompt, max_tokens=self.cfg.imagery_max_tokens)
        raw = _strip_think_block(raw)
        items = []
        for line in raw.splitlines():
            line = re.sub(r"^\s*(?:[-*·•]|\d+[\.、\)）])\s*", "", line.strip())
            if (line.startswith("「") and line.endswith("」")) or (line.startswith("\"") and line.endswith("\"")):
                line = line[1:-1].strip()
            if 4 <= len(line) <= 80:
                items.append(line)
            if len(items) >= self.cfg.imagery_max_count:
                break
        return items

    def _update_notes_from_perceive(self, stim: str, response: str) -> None:
        if not getattr(self.cfg, "notes_enabled", True):
            return
        event = f"他对我说：{stim[:600]}\n我刚刚回应：{response[:600]}"
        prompt = NOTES_UPDATE_PROMPT.format(
            notes_text=self.notes.render_for_update_prompt(max_chars=self.cfg.notes_max_chars_in_update_prompt),
            main_consciousness=self._main_consciousness or self.self_field.render_main_text(),
            event=event,
        )
        try:
            raw = self.llm.chat("你是 nova 的笔记本维护器，只输出 ADD / UPDATE / REMOVE 动作。", prompt, max_tokens=self.cfg.notes_update_max_tokens)
        except Exception as e:
            print(f"⚠️ 笔记本更新失败（不致命）：{e}")
            return
        raw = _strip_think_block(raw)
        actions = _parse_notes_actions(raw)
        if not actions:
            return
        changed = []
        for a in actions:
            if a[0] == "add":
                n = self.notes.add(a[1])
                if n:
                    changed.append(f"+ [{n.id}] {n.content}")
            elif a[0] == "update":
                if self.notes.update(a[1], a[2]):
                    changed.append(f"~ [{a[1]}] {a[2]}")
            elif a[0] == "remove":
                if self.notes.remove(a[1]):
                    changed.append(f"- [{a[1]}]")
        if changed:
            self.notes.save()
            print("----------")
            print(f"📓 笔记本变动（共 {len(changed)} 条，总 {len(self.notes)} 条）：")
            for c in changed:
                print("  " + c)
            print("----------")

    # ==========================================================
    # Rendering / persistence helpers
    # ==========================================================
    def _format_recall_line(self, f: Fissure, in_episode: bool) -> str:
        content = self._truncate_for_recall(f.content, in_episode=in_episode)
        if in_episode:
            rel = self._current_turn_index - f.turn_index
            pos_label = self._relative_position_label(rel)
            role = self._speaker_label(f.speaker)
            head = f"[{pos_label}·{role}]" if role else f"[{pos_label}]"
            return f"{head} {content}"
        age_label = _format_age(time.time() - f.creation_time)
        role = self._speaker_label(f.speaker)
        head = f"[{age_label}·{role}]" if role else f"[{age_label}]"
        return f"{head} {content}"

    def _truncate_for_recall(self, text: str, in_episode: bool) -> str:
        limit = self.cfg.episode_chain_content_max_chars if in_episode else max(self.cfg.max_fissure_chars, self.cfg.episode_chain_content_max_chars * 2)
        text = text.strip()
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    @staticmethod
    def _speaker_label(speaker: str) -> str:
        if speaker == SPEAKER_OUTSIDER:
            return "有人对我说"
        if speaker == SPEAKER_SELF:
            return "我说出口的话"
        if speaker == SPEAKER_DAYDREAM:
            return "我自己冒出来的念头"
        return ""

    def _relative_position_label(self, rel: int) -> str:
        if rel <= 1:
            return "刚刚"
        if rel == 2:
            return "上一句"
        if rel == 3:
            return "上上句"
        return f"{rel - 1} 句之前"

    def _main_consciousness_path(self) -> str:
        return os.path.join(self.cfg.field_path, "main_consciousness.json")

    def _load_main_consciousness(self) -> None:
        path = self._main_consciousness_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._main_consciousness = (d.get("main_consciousness") or "").strip()
        except Exception as e:
            print(f"⚠️ 主意识读取失败（忽略）：{e}")

    def _save_main_consciousness(self) -> None:
        try:
            os.makedirs(self.cfg.field_path, exist_ok=True)
            tmp = self._main_consciousness_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"main_consciousness": self._main_consciousness, "saved_at": time.time()}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._main_consciousness_path())
        except Exception as e:
            print(f"⚠️ 主意识落盘失败（不致命）：{e}")
