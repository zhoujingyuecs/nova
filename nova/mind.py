"""Nova：一次"感知—回应"的完整循环。

这个文件是 nova 的主类。它把所有部件接到一起：嵌入器、缝隙场、
水流引擎、本地大模型、走神线程、睡眠整理、可视化。

每一次 perceive(stimulus) 经过下面这些步骤：

  1. 把 stimulus 编码成种子形状；
  2. 让水流从种子出发，在缝隙场里走一遭，激活一批缝隙；
  3. 把激活的缝隙内容拼成"此刻浮现的回忆"，连同 stimulus 喂给本地 LLM；
  4. LLM 输出 → 这是水流当下整体的姿态；
  5. 用输出的 embedding 反过来"刻"一遍流过的所有缝隙：
     - 各处刻入的力度（可塑性）由该处的水流密度决定；
     - 形状漂移大到一定程度时，缝隙的 content 也被改写。
  6. 如果输出本身在语义上足够新颖，就在球上多开一道新缝隙。

走神（dream_step）走的是同一条管道，唯一区别是：
  - 种子来自内部（最近热缝隙 + 扰动），不是外界；
  - 提示词换成"自言自语"模板；
  - 生成长度更短。
"""

from __future__ import annotations

import os
import random
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


DREAM_PROMPT_TEMPLATE = (
	"[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。\n"
	"下面这些片段浮上心头：]\n\n"
	"{memories}\n\n"
	"[你现在脑子里在想什么？写一两句就好，像在自言自语，"
	"不要长篇大论，也不要解释自己在做什么。]"
)


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

		# 因为 perceive 和 dream_step 都会调用 LLM/改场，必须互斥
		self._lock = threading.RLock()

		# 第一次启动且为空，载入种子记忆
		if len(self.field) == 0 and self.cfg.seed_memories_file:
			self._load_seeds(self.cfg.seed_memories_file)

	# ==========================================================
	#                       主流程
	# ==========================================================
	def perceive(self, stimulus: str) -> str:
		"""感知一段外界输入，给出一次回应。线程安全。"""
		with self._lock:
			seed_shape = self.embedder.embed(stimulus)
			activated = self.flow_engine.flow(seed_shape)

			user_prompt = self._build_prompt(stimulus, activated)
			response = self.llm.chat(self.cfg.system_prompt, user_prompt)

			response_shape = self.embedder.embed(response)

			self._reshape(activated, response, response_shape)
			self._maybe_create(response, response_shape)
			self._maybe_create(stimulus, seed_shape)

			self.field.sync_all()
			self._tick_autosave()

			return response

	def dream_step(self, max_tokens: int = 256) -> Optional[str]:
		"""做一次走神。返回 nova 心里浮起的那句话；
		如果场太小或没激活到任何缝隙，返回 None。
		"""
		with self._lock:
			if len(self.field) < 3:
				return None

			seed_shape = self._dream_seed()
			activated = self.flow_engine.flow(seed_shape)
			if not activated:
				return None

			memories = "\n".join(f"- {f.content}" for f in activated)
			user_prompt = DREAM_PROMPT_TEMPLATE.format(memories=memories)
			thought = self.llm.chat(
				self.cfg.system_prompt, user_prompt, max_tokens=max_tokens
			)
			if not thought.strip():
				return None

			thought_shape = self.embedder.embed(thought)
			self._reshape(activated, thought, thought_shape)
			self._maybe_create(thought, thought_shape)

			self.field.sync_all()
			self._tick_autosave()
			return thought

	# ==========================================================
	#                    睡眠 / 可视化
	# ==========================================================
	def consolidate(self, prune: bool = True, merge: bool = True) -> dict:
		"""睡眠期整理：见 nova/sleep.py 的注释。"""
		from .sleep import consolidate as _consolidate

		with self._lock:
			stats = _consolidate(self.field, self.cfg, prune=prune, merge=merge)
			save_field(self.field)
		return stats

	def visualize(
		self, output_path: str, method: str = "pca", **kwargs
	) -> Optional[str]:
		"""把陶土球画一张 PNG。"""
		from .visualize import render_field

		with self._lock:
			return render_field(self.field, output_path, method=method, **kwargs)

	def save(self) -> None:
		with self._lock:
			save_field(self.field)

	# ==========================================================
	#                       内部
	# ==========================================================
	def _build_prompt(self, stimulus: str, activated: list[Fissure]) -> str:
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
		activated: list[Fissure],
		response_text: str,
		response_shape: np.ndarray,
	) -> None:
		for f in activated:
			plasticity = self.field.plasticity_at(f.shape)
			f.shift_toward(response_shape, plasticity, new_content=response_text)

	def _maybe_create(self, content: str, shape: np.ndarray) -> None:
		if not content.strip():
			return
		neighbors = self.field.nearest(shape, k=1)
		if neighbors and neighbors[0][1] >= self.cfg.create_threshold:
			return
		self.field.add(content, shape)

	def _dream_seed(self) -> np.ndarray:
		"""走神的种子：

		大多数时候用最近被刷过的某条缝隙做种子（连续意识感）；
		偶尔用一条随机的冷缝隙（突然想起一件没头没脑的事）；
		然后加少量噪声，避免每次都重复同一个起点。
		"""
		all_f = self.field.all()
		if random.random() < 0.85:
			weights = np.array(
				[np.exp(-f.quiet_seconds() / 3600.0) + 0.05 for f in all_f]
			)
		else:
			# 偶尔从冷缝隙出发：突然想起一件没头没脑的事
			weights = np.array(
				[1.0 / (1.0 + f.flow_count) for f in all_f]
			)
		weights = weights / weights.sum()
		seed = all_f[int(np.random.choice(len(all_f), p=weights))]

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
		if not os.path.exists(path):
			return
		with open(path, "r", encoding="utf-8") as f:
			text = f.read()
		chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
		if not chunks:
			return
		shapes = self.embedder.embed_batch(chunks)
		for content, shape in zip(chunks, shapes):
			self.field.add(content, shape)
		self.field.sync_all()
		save_field(self.field)
