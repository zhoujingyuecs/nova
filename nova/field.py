"""陶土球（FissureField）：所有缝隙的集合。

它的核心职责：
  1. 持有所有缝隙；
  2. 高效地查找"附近的缝隙"——这是几何上的连接（语义相似度）；
  3. 管理"暗道"——缝隙之间的显式有向链接（共同被想起→建立通道）；
  4. ★ 维护"对话链"——同一段交互内 prev_id/next_id 形成的时间链表；
  5. 计算局部水流密度，决定可塑性。

为什么需要"显式链接"——
  纯几何近邻有个毛病：水容易陷在一个语义簇里转。譬如"下雨"附近
  全是一些和雨有关的回忆，水流过来流过去都在这个簇内。但人脑里
  "下雨" 和 "外婆家檐角的风铃"在语义上隔得很远，却又常常一起浮
  起来——这是经验赋予的暗道，不是空间上能直接量出来的。

  显式链接是用来记录这种"经验性贴近"的。它单向、可累加、可衰减，
  类似神经网络里的赫布学习（Hebbian Learning）：一起激活就一起加强。

★ 对话链（episode chain）
  一段对话里 turn N 和 turn N+1 之间有一种**比"经验暗道"更结实**
  的连接——这是字面意义上的"前一句话紧接着后一句话"。所以这种连
  接用 prev_id/next_id 显式存指针，并且额外建立一条强度很高的暗道
  （强度通常是普通共激活链接的 10 倍以上），保证：

    - 水流到 turn N+1 时，turn N 几乎一定会被带起来；
    - 水流到 turn N 时，turn N+1 也大概率会浮上来；
    - 几天之后水流不小心擦过这段对话里某一句，前后几句会跟着浮起。

矩阵向量化：所有缝隙形状摞成一个 (N, d) 的 numpy 矩阵 _matrix。
增删改时同步更新。这样查询"找最近 k 条缝隙"只是一次矩阵乘法，
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
		# id → 在 _order/_matrix 里的下标，做 O(1) 查询用
		self._idx: dict[str, int] = {}

	# ==========================================================
	#                       基础操作
	# ==========================================================
	def __len__(self) -> int:
		return len(self._fissures)

	def __iter__(self) -> Iterable[Fissure]:
		return iter(self._fissures.values())

	def get(self, fid: str) -> Optional[Fissure]:
		return self._fissures.get(fid)

	def all(self) -> list[Fissure]:
		return [self._fissures[i] for i in self._order]

	def index_of(self, fid: str) -> int:
		return self._idx[fid]

	# ==========================================================
	#                        增删
	# ==========================================================
	def add(self, content: str, shape: np.ndarray,
			speaker: str = "", episode_id: str = "",
			turn_index: int = 0) -> Fissure:
		"""新建一条缝隙。

		speaker / episode_id / turn_index 是这一版加上的"场景元数据"。
		默认空——不带任何场景信息（比如种子记忆、抽象意象）。
		对话里的输入/输出每一句都应该带上这些字段，并通过 chain_link()
		把它们前后串起来。
		"""
		# 截断过长的内容（避免单条记忆吞掉整个水量）
		if len(content) > self.cfg.max_fissure_chars:
			content = content[: self.cfg.max_fissure_chars] + "…"

		f = Fissure(
			content=content,
			shape=shape.copy(),
			origin_shape=shape.copy(),
			speaker=speaker,
			episode_id=episode_id,
			turn_index=turn_index,
		)
		self._add_fissure(f)
		return f

	def _add_fissure(self, f: Fissure) -> None:
		"""内部：把一条已经构造好的 Fissure 加进来（持久化加载也走这里）"""
		self._fissures[f.id] = f
		self._order.append(f.id)
		self._idx[f.id] = len(self._order) - 1
		row = f.shape[None, :].astype(np.float32)
		self._matrix = np.vstack([self._matrix, row]) if len(self._matrix) else row.copy()

	def remove(self, fid: str) -> None:
		"""删除一条缝隙，并修复所有指向它的引用（暗道 + 对话链）。"""
		if fid not in self._fissures:
			return
		idx = self._idx[fid]
		# 矩阵 / order 删除
		self._order.pop(idx)
		self._matrix = np.delete(self._matrix, idx, axis=0)
		# idx 表重建（被删之后所有靠后的下标都要-1）
		self._idx = {fid_: i for i, fid_ in enumerate(self._order)}
		del self._fissures[fid]

		# ---- 清理所有指向这条缝隙的悬空引用 ----
		for other in self._fissures.values():
			# 1) 暗道
			if fid in other.outgoing_links:
				del other.outgoing_links[fid]
			# 2) 对话链：要把链子缝合起来，让前后两端互相指上
			if other.prev_id == fid:
				other.prev_id = ""
			if other.next_id == fid:
				other.next_id = ""

	# ==========================================================
	#                     矩阵同步
	# ==========================================================
	def sync_all(self) -> None:
		"""批量同步矩阵——在一次水流结束后调用一次即可。"""
		if not self._order:
			self._matrix = np.zeros((0, self.dim), dtype=np.float32)
			return
		shapes = np.stack([self._fissures[i].shape for i in self._order])
		self._matrix = shapes.astype(np.float32)

	# ==========================================================
	#                  几何查询（按相似度）
	# ==========================================================
	def nearest(self, shape: np.ndarray, k: int = 5,
				exclude: Optional[set] = None) -> list:
		"""按余弦相似度找最相似的 k 条缝隙。返回 [(Fissure, sim), ...]"""
		if len(self) == 0:
			return []
		shape = _normalize(shape)
		sims = self._matrix @ shape                             # (N,)
		# 排除集合
		if exclude:
			for fid in exclude:
				idx = self._idx.get(fid)
				if idx is not None:
					sims[idx] = -np.inf
		# 取前 k
		k = min(k, len(self))
		# 处理全是 -inf 的边界
		if not np.isfinite(sims).any():
			return []
		top_idx = np.argpartition(-sims, min(k - 1, len(sims) - 1))[:k]
		top_idx = top_idx[np.argsort(-sims[top_idx])]
		results = []
		for i in top_idx:
			s = float(sims[i])
			if not np.isfinite(s):
				continue
			results.append((self._fissures[self._order[int(i)]], s))
		return results

	# ==========================================================
	#               链接查询（按"暗道"走）
	# ==========================================================
	def linked_targets(self, fid: str,
					   exclude: Optional[set] = None) -> list:
		"""沿着这条缝隙的出度链接能到达的所有缝隙。

		返回 [(目标 Fissure, 链接强度), ...]，按强度降序。
		失效（target 已删除）的链接会被静默跳过。
		"""
		src = self._fissures.get(fid)
		if not src or not src.outgoing_links:
			return []
		exclude = exclude or set()
		out = []
		for tid, strength in src.outgoing_links.items():
			if tid in exclude:
				continue
			target = self._fissures.get(tid)
			if target is None:
				continue
			out.append((target, float(strength)))
		out.sort(key=lambda x: -x[1])
		return out

	def link(self, source_id: str, target_id: str,
			 strength_delta: float = 1.0) -> bool:
		"""在两条缝隙之间加一条（或加强一条）有向链接。

		返回是否成功（两端都存在 + 不是自连）。
		"""
		if source_id == target_id:
			return False
		src = self._fissures.get(source_id)
		dst = self._fissures.get(target_id)
		if src is None or dst is None:
			return False
		src.link_to(target_id, strength_delta=strength_delta,
					cap=self.cfg.link_strength_cap)
		return True

	def link_chain(self, fissure_ids: list, base_strength: float = 1.0,
				   decay: float = 0.6, max_distance: int = 3,
				   bidirectional: bool = False) -> int:
		"""把一串缝隙按顺序两两连起来。

		- max_distance 内的所有对都连：i→i+1 强；i→i+2 弱一点；i→i+3 更弱。
		  因为人记东西不是死板的链表，更像"前后相邻一截都贴"。
		- bidirectional：是否同时建反向链接（A→B 和 B→A 都建）。
		  默认 False——单向更符合"思路有方向"的直觉。

		返回新建/加强的链接条数。
		"""
		count = 0
		n = len(fissure_ids)
		for i in range(n):
			for j in range(i + 1, min(i + 1 + max_distance, n)):
				dist = j - i
				strength = base_strength * (decay ** (dist - 1))
				if self.link(fissure_ids[i], fissure_ids[j], strength):
					count += 1
				if bidirectional:
					if self.link(fissure_ids[j], fissure_ids[i],
								 strength * 0.7):
						count += 1
		return count

	# ==========================================================
	#               ★ 对话链（episode chain）
	# ==========================================================
	def chain_link(self, prev_id: str, next_id: str,
				   forward_strength: float, backward_strength: float) -> bool:
		"""把两条缝隙串成"前后两句话"的关系。

		做三件事：
		  1) 写好 prev/next 指针：next_fis.prev_id = prev_id；
		     prev_fis.next_id = next_id；
		  2) 建一条很强的正向暗道 prev → next（凭它前一句紧跟着浮出后一句）；
		  3) 建一条略弱的反向暗道 next → prev（凭它从后一句也能想到前一句，
		     "刚刚是在说什么来着"）。

		返回是否两条缝隙都还在场上、链子接成了。
		"""
		if prev_id == next_id:
			return False
		prev_f = self._fissures.get(prev_id)
		next_f = self._fissures.get(next_id)
		if prev_f is None or next_f is None:
			return False

		# 1) 链表指针
		next_f.prev_id = prev_id
		prev_f.next_id = next_id
		# 2) 强双向暗道
		prev_f.link_to(next_id, strength_delta=forward_strength,
					   cap=self.cfg.link_strength_cap)
		next_f.link_to(prev_id, strength_delta=backward_strength,
					   cap=self.cfg.link_strength_cap)
		return True

	def walk_chain_back(self, start_id: str, k: int) -> list:
		"""从 start_id 沿 prev_id 向回走最多 k 条。

		返回的是 Fissure 列表，按"时间从早到晚"顺序——也就是说，列表
		里第一项是最早的，最后一项是 start_id 的紧邻前一条（不含
		start_id 本身）。如果链子还没那么长，就有几条返回几条。
		"""
		out = []
		f = self._fissures.get(start_id)
		if f is None:
			return out
		cursor_id = f.prev_id
		seen = {start_id}  # 防环
		for _ in range(k):
			if not cursor_id or cursor_id in seen:
				break
			cur = self._fissures.get(cursor_id)
			if cur is None:
				break
			out.append(cur)
			seen.add(cursor_id)
			cursor_id = cur.prev_id
		out.reverse()  # 按时间从早到晚
		return out

	# ==========================================================
	#               局部密度（决定可塑性）
	# ==========================================================
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

	# ==========================================================
	#                      统计/调试
	# ==========================================================
	def link_stats(self) -> dict:
		"""返回一些链接图的统计量，主要为了调试和可视化。"""
		total = 0
		nonempty = 0
		max_out = 0
		strengths = []
		chained = 0
		for f in self._fissures.values():
			n = len(f.outgoing_links)
			if n > 0:
				nonempty += 1
				max_out = max(max_out, n)
			total += n
			strengths.extend(f.outgoing_links.values())
			if f.prev_id or f.next_id:
				chained += 1
		mean_s = float(np.mean(strengths)) if strengths else 0.0
		return {
			"total_links": total,
			"nodes_with_outgoing": nonempty,
			"max_out_degree": max_out,
			"mean_link_strength": mean_s,
			"node_count": len(self),
			"chain_nodes": chained,
		}
