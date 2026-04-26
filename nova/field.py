"""陶土球（FissureField）：所有缝隙的集合。

它的核心职责：
  1. 持有所有缝隙；
  2. 高效地查找"附近的缝隙"——这就是空间中的连接关系；
  3. 计算局部水流密度，决定可塑性。

我们没有显式存储邻接表。"两道缝隙是否相连"是一个由它们当下
形状直接决定的几何性质：相似度高的就连着，相似度低的就断开。
缝隙形状变了，连接关系也跟着变——这正是水流不断重塑空间的样子。

矩阵向量化：所有缝隙形状摞成一个 (N, d) 的 numpy 矩阵 _matrix。
增删改时同步更新。这样查询 "找最近 k 条缝隙" 只是一次矩阵乘法，
在十几万条缝隙以内都不会成为瓶颈。再大可以换 FAISS。
"""

from __future__ import annotations

import math
import time
from typing import Iterable, Optional

import numpy as np

from .config import NovaConfig
from .fissure import Fissure, _normalize


class FissureField:
	def __init__(self, cfg: NovaConfig, embedding_dim: int):
		self.cfg = cfg
		self.dim = embedding_dim
		self._fissures: dict[str, Fissure] = {}
		self._order: list[str] = []                          # id 的稳定顺序
		self._matrix: np.ndarray = np.zeros((0, self.dim), dtype=np.float32)

	# ---------- 基础操作 ----------
	def __len__(self) -> int:
		return len(self._fissures)

	def __iter__(self) -> Iterable[Fissure]:
		return iter(self._fissures.values())

	def get(self, fid: str) -> Optional[Fissure]:
		return self._fissures.get(fid)

	def all(self) -> list[Fissure]:
		return [self._fissures[i] for i in self._order]

	# ---------- 增删 ----------
	def add(self, content: str, shape: np.ndarray) -> Fissure:
		# 截断过长的内容（避免单条记忆吞掉整个水量）
		if len(content) > self.cfg.max_fissure_chars:
			content = content[: self.cfg.max_fissure_chars] + "…"

		f = Fissure(content=content, shape=shape.copy(), origin_shape=shape.copy())
		self._fissures[f.id] = f
		self._order.append(f.id)
		self._matrix = np.vstack([self._matrix, f.shape[None, :]]) if len(self._matrix) else f.shape[None, :].copy()
		return f

	def remove(self, fid: str) -> None:
		if fid not in self._fissures:
			return
		idx = self._order.index(fid)
		self._order.pop(idx)
		del self._fissures[fid]
		self._matrix = np.delete(self._matrix, idx, axis=0)

	# ---------- 同步矩阵 ----------
	def _sync_row(self, fid: str) -> None:
		"""缝隙形状变化后，同步矩阵那一行。"""
		idx = self._order.index(fid)
		self._matrix[idx] = self._fissures[fid].shape

	def sync_all(self) -> None:
		"""批量同步——在一次水流结束后调用一次即可。"""
		if not self._order:
			return
		shapes = np.stack([self._fissures[i].shape for i in self._order])
		self._matrix = shapes.astype(np.float32)

	# ---------- 查询 ----------
	def nearest(self, shape: np.ndarray, k: int = 5,
				exclude: Optional[set[str]] = None) -> list[tuple[Fissure, float]]:
		"""按余弦相似度找最相似的 k 条缝隙。"""
		if len(self) == 0:
			return []
		shape = _normalize(shape)
		sims = self._matrix @ shape                             # (N,)
		# 排除集合
		if exclude:
			for fid in exclude:
				if fid in self._order:
					sims[self._order.index(fid)] = -np.inf
		# 取前 k
		k = min(k, len(self))
		top_idx = np.argpartition(-sims, k - 1)[:k]
		top_idx = top_idx[np.argsort(-sims[top_idx])]
		return [(self._fissures[self._order[i]], float(sims[i])) for i in top_idx]

	# ---------- 局部密度（决定可塑性） ----------
	def local_flow_density(self, position: np.ndarray) -> float:
		"""估计 position 周围最近一段时间内有多少水流经过。

		密度 = Σ_{f 在邻域内} flow_count(f) * exp(-age / τ)

		被水流频繁刷过的区域密度高 → 可塑性高 → 短期记忆。
		"""
		if len(self) == 0:
			return 0.0
		position = _normalize(position)
		sims = self._matrix @ position                          # (N,)
		mask = sims > (1.0 - self.cfg.density_radius)
		if not mask.any():
			return 0.0

		now = time.time()
		tau = self.cfg.density_time_constant_seconds
		density = 0.0
		for i in np.where(mask)[0]:
			f = self._fissures[self._order[int(i)]]
			age = now - f.last_flow_time
			density += f.flow_count * math.exp(-age / tau)
		return float(density)

	def plasticity_at(self, position: np.ndarray) -> float:
		"""在某位置上，水流应该以多大力度重塑那里的缝隙。"""
		density = self.local_flow_density(position)
		p = self.cfg.base_plasticity + self.cfg.density_plasticity_gain * math.log1p(density)
		return float(min(p, self.cfg.max_plasticity))
