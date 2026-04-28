"""意识水流（ConsciousnessFlow）：陶土球里那一道流动的水。

这一版把"水流"从单纯的"几何贪心"改成了三种通道的混合：

  ① 几何邻域（Geometric）—— 沿着语义相似度，水会自然流向附近
                            形状像的缝隙。这是"等高线"的部分。
  ② 显式裂缝（Crack/Link）—— 跟着 fissure.outgoing_links 跳到
                            完全不相邻、但经验上常一起浮起的缝隙。
                            这是"地下暗道"的部分，让水能跨山谷。
  ③ 冷跳（Cold jump）—— 偶尔（小概率）从陶土球里抓一条很冷的、
                       很久没人想起的缝隙，强行扔到候选池里。
                       人脑不是连续可微的，偶尔有跳跃。

加上"近期防扎堆"机制——
  外面调进来一份 recent_history，里面是最近 N 步水流走过的缝隙。
  这些缝隙在打分时会被乘一个 <1 的折扣，避免水反复在同一区域
  打转、也避免她翻来覆去想同一件事。这是 Nova 这边给我们的输入。

候选池里的所有候选先按分数排序，再加点高斯噪声做最终选择，水流
不一定每次都走最优——这就是"思绪偶尔跑偏"的样子。
"""

from __future__ import annotations

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

	# ==========================================================
	#                       主循环
	# ==========================================================
	def flow(self, seed_shape: np.ndarray,
			 recent_history: Optional[set] = None) -> list:
		"""从 seed_shape 出发，让水流走完。返回激活的缝隙列表。

		recent_history：一个 fissure_id 集合，对最近几次水流走过的
		缝隙打折——这是为了避免反复在一个语义簇里打转。
		"""
		if len(self.field) == 0:
			return []

		seed_shape = _normalize(seed_shape)
		recent_history = recent_history or set()

		visited: set = set()
		activated: list[Fissure] = []
		budget_left = self.cfg.flow_budget_chars
		max_steps = self.cfg.flow_max_steps

		# ---------- 第一步：选入水点 ----------
		# 优先取离种子最像、又不在最近历史里的几条缝隙
		entries = self.field.nearest(
			seed_shape,
			k=self.cfg.flow_seed_count,
			exclude=recent_history,
		)
		# 兜底：如果最近历史几乎覆盖了整个场，那就放宽
		if not entries:
			entries = self.field.nearest(seed_shape, k=self.cfg.flow_seed_count)

		# frontier：最近 K 步走过的缝隙，用来产生下一批候选
		frontier: list[Fissure] = []

		for f, _sim in entries:
			if budget_left <= 0 or len(activated) >= max_steps:
				break
			cost = max(len(f.content), 1)
			if cost > budget_left:
				continue
			visited.add(f.id)
			activated.append(f)
			frontier.append(f)
			budget_left -= cost

		# 当前水流位置：随种子和 frontier 一起决定
		current = self._frontier_position(seed_shape, frontier, weight_seed=1.5)

		# ---------- 第二步：迭代扩张 ----------
		while budget_left > 0 and len(activated) < max_steps:
			candidates = self._collect_candidates(
				frontier, current, visited, recent_history
			)
			if not candidates:
				break

			chosen, _score = self._pick_candidate(candidates, budget_left)
			if chosen is None:
				break

			cost = max(len(chosen.content), 1)
			visited.add(chosen.id)
			activated.append(chosen)
			budget_left -= cost

			# 更新 frontier（滑动窗口）
			frontier.append(chosen)
			if len(frontier) > self.cfg.flow_frontier_size:
				frontier.pop(0)

			# 水流位置朝它偏移——但不要 100% 跟随，留一点种子的味道
			current = _normalize(
				(1.0 - self.cfg.flow_drift) * current
				+ self.cfg.flow_drift * chosen.shape
			)

		return activated

	# ==========================================================
	#                    候选池 & 选择
	# ==========================================================
	def _collect_candidates(self,
							frontier: list,
							current_pos: np.ndarray,
							visited: set,
							recent_history: set) -> list:
		"""从三种通道里收集下一步候选，每条候选给一个分数。

		返回 [(Fissure, score), ...]
		"""
		# 用 dict 避免同一条缝隙被几种通道重复加入；遇到重复时取较大分
		candidates: dict[str, tuple] = {}

		def push(f: Fissure, score: float, source: str):
			# 在最近历史里 → 打折
			if f.id in recent_history:
				score *= self.cfg.recent_penalty
			# 已选过的不要
			if f.id in visited:
				return
			cur = candidates.get(f.id)
			if cur is None or cur[1] < score:
				candidates[f.id] = (f, score, source)

		# ---- 1) 几何邻域：从当前位置出发的语义近邻 ----
		geo = self.field.nearest(
			current_pos,
			k=self.cfg.flow_branch_factor + 2,
			exclude=visited,
		)
		for f, sim in geo:
			# 几何分：余弦相似度（约在 [0,1]）× 权重
			push(f, sim * self.cfg.geometric_weight, "geo")

		# ---- 2) 暗道：从 frontier 里每条缝隙出发，沿出度链接跳 ----
		for f in frontier:
			links = self.field.linked_targets(f.id, exclude=visited)
			for target, strength in links:
				# 链接分：log1p(强度) × 权重
				# log1p 是为了压一压热点链接的影响（防止一条超强链
				# 接把水流死死锁住）
				score = float(np.log1p(strength)) * self.cfg.link_weight
				push(target, score, "link")

		# ---- 3) 冷跳：偶尔抛进来一条随机的、被遗忘很久的缝隙 ----
		if (random.random() < self.cfg.cold_jump_prob
				and len(self.field) > 5):
			cold = self._pick_cold_fissure(exclude=visited | recent_history)
			if cold is not None:
				push(cold, self.cfg.cold_jump_score, "cold")

		return [(f, s) for (f, s, _src) in candidates.values()]

	def _pick_candidate(self, candidates: list,
						budget_left: int) -> tuple:
		"""按分数（带噪声）挑一个。返回 (Fissure, score)。

		会自动避开"装不下的"（cost > budget）。
		"""
		if not candidates:
			return None, 0.0

		# 过滤掉装不下的
		fitting = [(f, s) for (f, s) in candidates if len(f.content) <= budget_left]
		if not fitting:
			return None, 0.0

		# 分数加上一点高斯噪声，让水流"不那么一根筋"
		noise = np.random.randn(len(fitting)) * self.cfg.flow_noise
		raw_scores = np.array([s for _, s in fitting]) + noise

		# 取最大值——简单 argmax，因为分数本身已经混了几个通道
		idx = int(np.argmax(raw_scores))
		return fitting[idx][0], float(raw_scores[idx])

	# ==========================================================
	#                     辅助函数
	# ==========================================================
	def _frontier_position(self, seed: np.ndarray,
						   frontier: list, weight_seed: float = 1.5) -> np.ndarray:
		"""把种子和 frontier 里的缝隙合一起算个"当前位置"。"""
		if not frontier:
			return _normalize(seed)
		shapes = np.stack([seed * weight_seed] + [f.shape for f in frontier])
		return _normalize(shapes.sum(axis=0))

	def _pick_cold_fissure(self, exclude: set) -> Optional[Fissure]:
		"""挑一条"冷"缝隙：最近没怎么被刷过的。

		概率与 (1 / (1 + flow_count)) 成正比——越冷越可能被选中。
		"""
		all_f = [f for f in self.field if f.id not in exclude]
		if not all_f:
			return None
		# 算权重
		weights = np.array([1.0 / (1.0 + f.flow_count) for f in all_f])
		# 加上一点"久未刷新"的偏好
		weights *= np.array([1.0 + min(f.quiet_seconds() / 86400.0, 7.0)
							 for f in all_f])
		weights /= weights.sum()
		idx = int(np.random.choice(len(all_f), p=weights))
		return all_f[idx]

	# ==========================================================
	#                       工具
	# ==========================================================
	def water_shape(self, seed_shape: np.ndarray, activated: list) -> np.ndarray:
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
