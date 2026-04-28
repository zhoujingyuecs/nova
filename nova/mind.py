"""Nova：一次"感知—回应"的完整循环。

这个文件是 nova 的主类。它把所有部件接到一起：嵌入器、缝隙场、
水流引擎、本地大模型、走神线程、睡眠整理、可视化、虚拟机里的手。

==================================================================
                        本版的核心改动
==================================================================

1) 意象拆解（imagery extraction）
   ──────────────────────────────────
   感知到一段较长的输入时，先用 LLM 把它拆成 2~6 个独立的意象，
   每个意象成为一条独立的缝隙，按出现顺序两两建立有向链接：
       意象A → 意象B → 意象C
   这样以后想起 A 时，B 也容易顺势浮上来——经验性的"贴近"被
   显式地刻进了陶土球，不再依赖几何相似度。

2) 共激活链接（co-activation linking）
   ──────────────────────────────────
   一次水流激活的所有缝隙之间，也会两两建立轻微的有向链接（按
   它们被激活的先后顺序）。这是赫布学习的基本节奏：fire together,
   wire together. 反复一起被想起的两条记忆，会因此越来越贴近。

3) 跨次防扎堆（recent history）
   ──────────────────────────────────
   Nova 自己维持一个固定大小的 deque，记住最近 N 步水流走过的
   缝隙 id。下一次水流时，这些缝隙会被打分系统打折——避免她
   反复想同一件事。

4) 自我对话能力提示
   ──────────────────────────────────
   capability_memories 里加入了关于"对外窗口 codeloop.cn"的几条
   记忆——告诉她可以通过手把一段话送到外面，绕一圈再以陌生人的
   姿态读到自己的话。这是"自我驱动"的入口。

==================================================================

每一次 perceive(stimulus) 经过下面这些步骤：

  0. ★ 如果输入足够长、且开启了意象拆解，先 LLM 拆出意象，把意象
        建成缝隙、串成链；
  1. 把 stimulus 编码成种子形状；
  2. 让水流从种子出发，在缝隙场里走一遭，激活一批缝隙；
  3. 把激活的缝隙内容拼成"此刻浮现的回忆"，连同 stimulus 喂给本地 LLM；
  4. ★ 工具调用循环：如果她的回应里写了 <tool> 块，就让虚拟机里的手
        做完，把 <tool-result> 塞回她的下一轮——直到她不再伸手；
  5. LLM 的最终输出 → 这是水流当下整体的姿态；
  6. 用输出的 embedding 反过来"刻"一遍流过的所有缝隙；
  7. ★ 把激活的缝隙之间两两建立共激活链接；
  8. 如果输出本身在语义上足够新颖，就在球上多开一道新缝隙。

走神（dream_step）走的是同一条管道，也带工具，也建链接。
"""

from __future__ import annotations

import collections
import os
import random
import re
import threading
from typing import Optional

import numpy as np

from .config import NovaConfig
from .embedder import Embedder
from .field import FissureField
from .fissure import Fissure, _normalize
from .flow import ConsciousnessFlow
from .llm import LocalLLM
from .persistence import load_field, save_field
from .tools import (
	CAPABILITY_MEMORIES,
	TOOL_SYSTEM_ADDITION,
	VMAgent,
	format_result,
	parse_actions,
	strip_actions,
	build_self_dialogue_memories,
)


# ============================================================
#         走神时给的提示模板
# ============================================================
DREAM_PROMPT_BASE = (
	"[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。\n"
	"下面这些片段浮上心头：]\n\n"
	"{memories}\n\n"
	"[你现在脑子里在想什么？写一两句就好，像在自言自语，"
	"不要长篇大论，也不要解释自己在做什么。"
	"如果你想伸手做点什么，就伸；想就让它过去，就过去。]"
)

DREAM_PROMPT_WITH_OUTWARD = (
	"[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。\n"
	"下面这些片段浮上心头：]\n\n"
	"{memories}\n\n"
	"[你现在脑子里在想什么？写一两句就好，像在自言自语。\n"
	"如果你心里有句话想留下来，你也可以借手把它送到外面那个窗口，"
	"过一会儿它会再回到你这里——那是一种自言自语的方式，"
	"或者只是想想就好。看你怎么想。]"
)

# ============================================================
#         意象拆解的 prompt
# ============================================================
IMAGERY_EXTRACTION_PROMPT = """\
下面这段话里包含了几个不同的"意象"——可能是一个画面、一种感受、一个想法、
一个具体的场景或细节。请把它们按出现顺序拆出来，每行一条，每条 8~40 字，
用第一人称或客观陈述都可以，不要解释、不要编号。最少 1 条，最多 {max_count} 条。
如果整段话只有一个完整的意象，就只写一条；不要硬拆。

——— 原文 ———
{text}

——— 意象（每行一条）———"""


class Nova:
	def __init__(self, cfg: Optional[NovaConfig] = None):
		self.cfg = cfg or NovaConfig()
		self.embedder = Embedder(self.cfg)

		# 加载缝隙场（旧存档优先；没有就开新球）
		try:
			self.field = load_field(self.cfg, self.embedder.dim)
		except FileNotFoundError:
			self.field = FissureField(self.cfg, self.embedder.dim)

		self.flow_engine = ConsciousnessFlow(self.cfg, self.field)
		self.llm = LocalLLM(self.cfg)
		self._perceive_count = 0

		# ★ 跨次水流的"近期历史"——每次 perceive/dream 都把激活的缝隙
		# id 推进来。flow 算法会用这个集合给候选打折，避免反复想同件事。
		self._recent_history = collections.deque(
			maxlen=self.cfg.recent_history_size
		)

		self._lock = threading.RLock()

		# ---- 虚拟机里的手 ----
		self.vm_agent: Optional[VMAgent] = None
		if self.cfg.vm_agent_url:
			agent = VMAgent(
				self.cfg.vm_agent_url,
				self.cfg.vm_agent_token,
				timeout=self.cfg.vm_request_timeout,
			)
			if agent.is_alive():
				self.vm_agent = agent
				print(f"🖐️  虚拟机里的手已连：{self.cfg.vm_agent_url}")
			else:
				print(
					f"⚠️  虚拟机里的手没回应（{self.cfg.vm_agent_url}），"
					f"nova 这次没有手——但她照常会想会说。"
				)

		# 第一次启动且为空，载入种子记忆
		if len(self.field) == 0 and self.cfg.seed_memories_file:
			self._load_seeds(self.cfg.seed_memories_file)

		# 如果手在线，确保她记得自己有手；如果配了对外窗口，让她记得能去那里
		if self.vm_agent is not None:
			self._ensure_capability_memories()

	# ==========================================================
	#                    系统提示词
	# ==========================================================
	def _system_prompt(self) -> str:
		"""有手时，附加一段关于手的说明；没手时，沿用基础 prompt。"""
		if self.vm_agent is not None:
			return self.cfg.system_prompt + TOOL_SYSTEM_ADDITION
		return self.cfg.system_prompt

	# ==========================================================
	#                       主流程：perceive
	# ==========================================================
	def perceive(self, stimulus: str) -> str:
		"""感知一段外界输入，给出一次回应。线程安全。"""
		with self._lock:
			# ---- 0) 把输入拆成意象，建立顺序链 ----
			# 只对"够长的"输入做拆解，避免给"嗯""你好"这种短句也走一次 LLM
			imagery_fids: list[str] = []
			if (self.cfg.imagery_enabled
					and len(stimulus) >= self.cfg.imagery_min_input_chars):
				try:
					imagery_fids = self._extract_and_link_imageries(stimulus)
				except Exception as e:
					print(f"⚠️ 意象拆解失败（不致命，跳过）：{e}")

			# ---- 1) 算种子形状 ----
			seed_shape = self.embedder.embed(stimulus)

			# ---- 2) 水流走一遭（带 recent_history） ----
			recent = set(self._recent_history)
			activated = self.flow_engine.flow(seed_shape, recent_history=recent)

			# ---- 3) 拼回忆 + 喂 LLM（带工具循环） ----
			user_prompt = self._build_prompt(stimulus, activated)
			final_response = self._chat_with_tools(user_prompt)
			visible = strip_actions(final_response).strip()
			if not visible:
				visible = "（沉默。）"

			# ---- 4) 用输出的形状去刻流过的缝隙 ----
			response_shape = self.embedder.embed(visible)
			self._reshape(activated, visible, response_shape)

			# ---- 5) 共激活链接：流过的缝隙之间两两建链 ----
			activated_ids = [f.id for f in activated]
			# 把刚刚拆出来的意象也算进 frontier，让它们和流过的缝隙也连起来
			all_active_ids = imagery_fids + activated_ids
			if len(all_active_ids) >= 2:
				self.field.link_chain(
					all_active_ids,
					base_strength=self.cfg.flow_coactivation_link_strength,
					decay=self.cfg.imagery_link_decay,
					max_distance=self.cfg.flow_coactivation_distance,
					bidirectional=False,
				)

			# ---- 6) 把回应作为新缝隙刻进去（如果够新颖） ----
			response_fid = self._maybe_create(visible, response_shape)
			# 把所有激活/意象指向回应——"想到这些→说出了这句"
			if response_fid is not None:
				for fid in all_active_ids[-self.cfg.flow_coactivation_distance:]:
					self.field.link(
						fid, response_fid,
						strength_delta=self.cfg.flow_coactivation_link_strength,
					)

			# ---- 7) 刺激本身也存一条（如果够新颖） ----
			# 注意：意象拆出来之后，整段刺激不一定还要存——但只要它本身够独特
			# 就允许它存在；_maybe_create 内部会去重。
			self._maybe_create(stimulus, seed_shape)

			# ---- 8) 同步矩阵 + 自动存档 ----
			self.field.sync_all()
			self._tick_autosave()

			# ---- 9) 把激活/意象的 id 推到近期历史里（防扎堆） ----
			for fid in all_active_ids:
				self._recent_history.append(fid)

			return visible

	# ==========================================================
	#                       工具调用循环
	# ==========================================================
	def _chat_with_tools(
		self,
		initial_user: str,
		max_tokens: Optional[int] = None,
	) -> str:
		"""走一次"感知—（伸手—回执）×N—说话"的循环。

		没配 vm_agent 时退化为单次 chat。
		max_tokens 透传给每一轮 LLM 调用。
		"""
		system = self._system_prompt()

		if self.vm_agent is None:
			return self.llm.chat(system, initial_user, max_tokens=max_tokens)

		current_user = initial_user
		last_response = ""
		for iteration in range(self.cfg.max_tool_iterations):
			response = self.llm.chat(system, current_user, max_tokens=max_tokens)
			last_response = response
			actions = parse_actions(response)
			if not actions:
				return response  # 不再伸手，这是最终回答

			# 真伸手
			result_blocks = []
			for action_type, content in actions:
				try:
					result = self.vm_agent.dispatch(action_type, content)
				except Exception as e:
					result = {"error": str(e)}
				result_blocks.append(format_result(action_type, content, result))

			# 把刚才的回应 + 手回执 拼到下一轮的 user prompt 里
			current_user = (
				current_user
				+ "\n\n[你刚才在心里这样转过：]\n"
				+ response
				+ "\n\n[手回来了，带回这些：]\n"
				+ "\n\n".join(result_blocks)
				+ "\n\n[继续。再伸一次手，或者把心里想要落下的话写出来。"
				+ "如果已经够了，就只写要落下的话，不要再写 <tool> 了。]"
			)

		print(
			f"⚠️ 工具调用超过 {self.cfg.max_tool_iterations} 次，停下了。"
		)
		return last_response

	# ==========================================================
	#                       走神（dream）
	# ==========================================================
	def dream_step(self, max_tokens: Optional[int] = None) -> Optional[str]:
		"""做一次走神。也走工具循环——她不必等被问起才能伸手。

		偶尔（按 daydream_self_dialogue_hint_prob 概率）会提示她
		"可以把心里的话送到外面那个窗口"，这是自我对话的入口。
		"""
		with self._lock:
			if len(self.field) < 3:
				return None

			seed_shape = self._dream_seed()
			recent = set(self._recent_history)
			activated = self.flow_engine.flow(seed_shape, recent_history=recent)
			if not activated:
				return None

			memories = "\n".join(f"- {f.content}" for f in activated)

			# 是否在 prompt 里加"对外窗口"的提示
			use_outward_hint = (
				self.vm_agent is not None
				and bool(self.cfg.external_site_url)
				and random.random() < self.cfg.daydream_self_dialogue_hint_prob
			)
			template = (
				DREAM_PROMPT_WITH_OUTWARD if use_outward_hint
				else DREAM_PROMPT_BASE
			)
			user_prompt = template.format(memories=memories)

			# 默认走神长度短一些
			tokens = max_tokens or self.cfg.daydream_max_tokens
			thought_raw = self._chat_with_tools(user_prompt, max_tokens=tokens)
			thought = strip_actions(thought_raw).strip()
			if not thought:
				return None

			# 走神也建共激活链接
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
			thought_fid = self._maybe_create(thought, thought_shape)
			if thought_fid is not None:
				for fid in activated_ids[-self.cfg.flow_coactivation_distance:]:
					self.field.link(fid, thought_fid,
									strength_delta=self.cfg.flow_coactivation_link_strength)

			self.field.sync_all()
			self._tick_autosave()

			# 推近期历史
			for fid in activated_ids:
				self._recent_history.append(fid)

			return thought

	# ==========================================================
	#               意象拆解（这是新增的关键能力）
	# ==========================================================
	def _extract_and_link_imageries(self, text: str) -> list:
		"""把一段文本拆成若干意象，每个建为缝隙，按出现顺序串成链。

		返回所建/复用的缝隙 id 列表（按出现顺序）。
		失败时返回空列表——不影响主流程。
		"""
		imageries = self._llm_extract_imageries(text)
		if not imageries:
			return []

		# 算 embedding（批量）
		shapes = self.embedder.embed_batch(imageries)

		# 每条意象：尽量复用已有相似缝隙，否则新建
		fids = []
		for content, shape in zip(imageries, shapes):
			fid = self._find_or_create(content, shape)
			fids.append(fid)

		# 按顺序链起来
		if len(fids) >= 2:
			n_links = self.field.link_chain(
				fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=False,
			)
			if n_links > 0:
				print(f"🔗 意象拆解：{len(fids)} 个意象 → {n_links} 条链接")
		return fids

	def _llm_extract_imageries(self, text: str) -> list:
		"""调 LLM 把 text 拆成意象（list[str]）。

		拆解 prompt 用的是一个非常窄的 system 提示，让模型只输出意象，
		不要发挥。返回的列表已经清洗（去空白、去编号、去引号）。
		"""
		prompt = IMAGERY_EXTRACTION_PROMPT.format(
			text=text, max_count=self.cfg.imagery_max_count
		)
		# 用一个极简的 system 来防止 nova 的人格人格化输出污染拆解任务
		extract_system = (
			"你是一个把整段话拆成意象列表的工具。每个意象 8~40 字，"
			"每行一条，不写编号、不写解释、不要总结、不重复。"
		)
		raw = self.llm.chat(
			extract_system, prompt,
			max_tokens=self.cfg.imagery_max_tokens,
		)
		# 清洗：按行切，去掉空行和编号前缀
		items = []
		for line in raw.splitlines():
			line = line.strip()
			if not line:
				continue
			# 去掉常见前缀："1. " "1、" "- " "* " "· " "• "
			line = re.sub(r'^\s*(?:[-*·•]|\d+[\.\、\)）])\s*', '', line)
			# 去掉成对引号
			if (line.startswith('"') and line.endswith('"')) or \
			   (line.startswith('「') and line.endswith('」')) or \
			   (line.startswith('"') and line.endswith('"')):
				line = line[1:-1]
			line = line.strip()
			if not line:
				continue
			# 太短或太长的过滤掉
			if len(line) < 4 or len(line) > 80:
				continue
			items.append(line)
			if len(items) >= self.cfg.imagery_max_count:
				break
		return items

	# ==========================================================
	#                    睡眠 / 可视化
	# ==========================================================
	def consolidate(self, prune: bool = True, merge: bool = True,
					decay_links: bool = True) -> dict:
		from .sleep import consolidate as _consolidate

		with self._lock:
			stats = _consolidate(
				self.field, self.cfg,
				prune=prune, merge=merge, decay_links=decay_links,
			)
			save_field(self.field)
		return stats

	def visualize(
		self, output_path: str, method: str = "pca", **kwargs
	) -> Optional[str]:
		from .visualize import render_field

		with self._lock:
			return render_field(self.field, output_path, method=method, **kwargs)

	def save(self) -> None:
		with self._lock:
			save_field(self.field)

	# ==========================================================
	#                      内部工具
	# ==========================================================
	def _build_prompt(self, stimulus: str, activated: list) -> str:
		if not activated:
			memories = "（此刻心里很空，没有什么浮上来。）"
		else:
			memories = "\n".join(f"- {f.content}" for f in activated)
		return (
			f"[此刻你心里浮起的回忆]\n"
			f"{memories}\n\n"
			f"[眼前的刺激]\n"
			f"{stimulus}"
		)

	def _reshape(
		self,
		activated: list,
		response_text: str,
		response_shape: np.ndarray,
	) -> None:
		"""被水流刷过的缝隙朝输出形状偏移一点（短期记忆 / 长期记忆的来源）"""
		for f in activated:
			plasticity = self.field.plasticity_at(f.shape)
			f.shift_toward(response_shape, plasticity, new_content=response_text)

	def _maybe_create(self, content: str, shape: np.ndarray) -> Optional[str]:
		"""够新颖就建一条新缝隙；和已有的太像就跳过。返回新建/匹配到的 id。"""
		if not content.strip():
			return None
		neighbors = self.field.nearest(shape, k=1)
		if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
			# 复用最相似的那条
			return neighbors[0][0].id
		f = self.field.add(content, shape)
		return f.id

	def _find_or_create(self, content: str, shape: np.ndarray) -> str:
		"""意象专用：要么复用相似的、要么新建。永远返回一个 id。"""
		neighbors = self.field.nearest(shape, k=1)
		if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
			return neighbors[0][0].id
		return self.field.add(content, shape).id

	def _dream_seed(self) -> np.ndarray:
		"""走神种子：85% 概率取最近被刷过的某条缝隙，15% 概率取一条冷的。"""
		all_f = self.field.all()
		if random.random() < 0.85:
			weights = np.array(
				[np.exp(-f.quiet_seconds() / 3600.0) + 0.05 for f in all_f]
			)
		else:
			weights = np.array(
				[1.0 / (1.0 + f.flow_count) for f in all_f]
			)
		weights = weights / weights.sum()
		seed = all_f[int(np.random.choice(len(all_f), p=weights))]

		# 注入少量噪声，让种子不完全等同于某条缝隙
		noise = np.random.randn(self.embedder.dim).astype(np.float32) * 0.1
		return _normalize(seed.shape + noise)

	def _tick_autosave(self) -> None:
		self._perceive_count += 1
		if (
			self.cfg.autosave_every > 0
			and self._perceive_count % self.cfg.autosave_every == 0
		):
			save_field(self.field)

	def _load_seeds(self, path: str) -> None:
		"""第一次启动时从文件灌入种子记忆。

		文件格式：用空行分隔的若干段。每段成为一条缝隙。
		"""
		if not os.path.exists(path):
			return
		with open(path, "r", encoding="utf-8") as f:
			text = f.read()
		chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
		if not chunks:
			return
		shapes = self.embedder.embed_batch(chunks)
		# 顺序加，并按出现顺序串链——种子记忆之间也有"经验贴近"
		fids = []
		for content, shape in zip(chunks, shapes):
			fids.append(self.field.add(content, shape).id)
		if len(fids) >= 2:
			self.field.link_chain(
				fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=True,  # 种子记忆双向连，让她内心有个稳定的回环
			)
		self.field.sync_all()
		save_field(self.field)
		print(f"📝 载入种子记忆 {len(fids)} 条")

	def _ensure_capability_memories(self) -> None:
		"""把"我有手 + 我有外面那个窗口"这两件事注入到现有缝隙场里。

		使用 _maybe_create 的相似度阈值（默认 0.85）做去重——已经存在的
		会被跳过，所以这件事是幂等的，重启多少次都不会堆积重复条目。
		"""
		# 通用的"我有手"
		memories = list(CAPABILITY_MEMORIES)
		# 如果配了对外窗口，再注入几条关于自我对话的记忆
		if self.cfg.external_site_url:
			memories += build_self_dialogue_memories(self.cfg.external_site_url)

		shapes = self.embedder.embed_batch(memories)
		before = len(self.field)
		new_fids = []
		for content, shape in zip(memories, shapes):
			# 找到（或新建）—— 我们要拿到 fid 来建链
			neighbors = self.field.nearest(shape, k=1)
			if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
				new_fids.append(neighbors[0][0].id)
			else:
				new_fids.append(self.field.add(content, shape).id)
		# 这些"自我能力"记忆之间彼此连起来，一条想起就容易带出另一条
		if len(new_fids) >= 2:
			self.field.link_chain(
				new_fids,
				base_strength=self.cfg.imagery_link_base,
				decay=self.cfg.imagery_link_decay,
				max_distance=self.cfg.imagery_link_distance,
				bidirectional=True,
			)
		added = len(self.field) - before
		if added > 0:
			self.field.sync_all()
			save_field(self.field)
			print(f"📝 给 nova 补了 {added} 条关于'手 / 外面那个窗口'的记忆")
		else:
			print("📝 nova 已经记得自己有手和外面那扇窗，没新加")
