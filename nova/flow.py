"""意识水流（ConsciousnessFlow）：陶土球里那一道流动的水。

输入：一个种子形状（外界刺激的 embedding，或上一次思考留下的姿态）。
过程：
  1. 在球里找几条与种子形状最像的缝隙作为入水点；
  2. 从入水点出发，做"有偏的随机游走"——
     - 朝相似的邻居流（这是水的惯性）；
     - 但也会因表面张力跳到稍远些的缝隙（这是意识的跳跃）；
  3. 每流过一条缝隙就消耗一些"水量"（用文本字符数粗略对应 token 数）；
  4. 水量耗尽 → 水流停下。
输出：被水流填满的缝隙列表（按填满的先后顺序）。

这些缝隙的 content 就是"此刻浮上心头的回忆"，是大模型的输入材料。
"""

from __future__ import annotations

import heapq
import random
from typing import Optional

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import Fissure, _normalize


class ConsciousnessFlow:
	def __init__(self, cfg: NovaConfig, field: FissureField):
		self.cfg = cfg
		self.field = field

	def flow(self, seed_shape: np.ndarray) -> list[Fissure]:
		"""从 seed_shape 出发，让水流走完。返回激活的缝隙列表。"""
		if len(self.field) == 0:
			return []

		seed_shape = _normalize(seed_shape)
		visited: set[str] = set()
		activated: list[Fissure] = []
		budget_left = self.cfg.flow_budget_chars
		max_steps = self.cfg.flow_max_steps

		# 优先队列：(-priority, tiebreak, fissure)。priority 越大（越像）越先访问。
		queue: list[tuple[float, float, Fissure]] = []

		def push(f: Fissure, position: np.ndarray) -> None:
			if f.id in visited:
				return
			sim = float(np.dot(f.shape, position))
			noise = random.gauss(0.0, self.cfg.flow_noise)
			priority = -(sim + noise)                             # heapq 是小顶堆
			heapq.heappush(queue, (priority, random.random(), f))

		# 初始入水点：取与种子最像的几条缝隙
		seeds = self.field.nearest(seed_shape, k=self.cfg.flow_seed_count)
		for f, _sim in seeds:
			push(f, seed_shape)

		# 当前水流位置：随时被流过的缝隙拖动
		current = seed_shape.copy()

		while queue and budget_left > 0 and len(activated) < max_steps:
			_pri, _tie, f = heapq.heappop(queue)
			if f.id in visited:
				continue

			cost = max(len(f.content), 1)
			if cost > budget_left:
				# 这条吃不下，但可能后面有更短的还能装。继续看队列。
				continue

			# 流过这一条
			visited.add(f.id)
			activated.append(f)
			budget_left -= cost
			# 水流位置朝它偏移一点（"水带上了它的味道"）
			current = _normalize(0.7 * current + 0.3 * f.shape)

			# 把它的邻居加入候选
			neighbors = self.field.nearest(
				f.shape, k=self.cfg.flow_branch_factor + 1, exclude=visited
			)
			for nb, _sim in neighbors:
				push(nb, current)

		return activated

	def water_shape(self, seed_shape: np.ndarray, activated: list[Fissure]) -> np.ndarray:
		"""水流当下整体的"姿态"——种子加上沿途带的味道，平均而成。

		这只是个粗略表达，实际刻入缝隙的形状以 LLM 输出的 embedding 为准。
		"""
		if not activated:
			return _normalize(seed_shape)
		shapes = np.stack([seed_shape] + [f.shape for f in activated])
		# 沿途的缝隙轻一点权重，种子重一点权重
		weights = np.concatenate([[2.0], np.ones(len(activated))])
		weights /= weights.sum()
		return _normalize((shapes * weights[:, None]).sum(axis=0))
