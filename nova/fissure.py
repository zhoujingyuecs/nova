"""缝隙（Fissure）：陶土球上的一道裂痕。

每条缝隙承载一段记忆。它有：
  - content：当前承载的文本（"裂缝里盛着的东西"）
  - shape：当前的形状向量（embedding）。形状即语义位置：
            形状相似的缝隙在球里也是相邻的。
  - origin_shape：刚被刻下时的形状。和当前 shape 的距离，
                  就是这道缝隙"漂移了多远"——它原本承载的
                  记忆，已经被多少次水流冲刷得变了样子。
  - flow_count：历史上有多少次水流流经此处。
  - last_flow_time：最近一次被填满的时间戳。

  ★ 新增：outgoing_links —— 这道缝隙通向哪些其他缝隙的"地下通道"
    -----------------------------------------------------------
    旧版本的水流只看几何相似度（语义近不近），结果就是水容易
    陷在一团相似的回忆里转圈，像个实心球。

    显式链接是为了把"陶土球"里那些**真正的裂缝**显式地刻出来——
    两条在语义上很远的回忆，如果它们在一段经历里挨着出现过，
    就该有一条直通的暗道。

    这就是"高低不平的洞穴"，让水能从一个山谷流到另一个山谷，
    而不只是顺着等高线绕圈。

    存的是 dict[target_id, strength]：
      - 单向。A→B 不等于 B→A。
      - 想要双向就显式存两条（A→B 和 B→A）。
      - strength 是浮点数，可以累加：每次共同被想起就强化一次。
      - 没有上限——但消费时我们会用 log 之类把它压一压。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Fissure:
	# ---------- 内容与形状 ----------
	content: str
	shape: np.ndarray            # 当前形状（单位向量）
	origin_shape: np.ndarray     # 出生时的形状（不可变，用于度量漂移）

	# ---------- 身份与统计 ----------
	id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
	flow_count: int = 0
	last_flow_time: float = field(default_factory=time.time)
	creation_time: float = field(default_factory=time.time)

	# ---------- 显式裂缝（这是这次改动的核心） ----------
	# key: 目标缝隙的 id
	# value: 链接强度（>0；越大越倾向走这条暗道）
	outgoing_links: dict = field(default_factory=dict)

	def __post_init__(self):
		# 把形状统一归一化到单位球面上，所有相似度都用余弦
		self.shape = _normalize(self.shape)
		self.origin_shape = _normalize(self.origin_shape)

	# ==========================================================
	#                       动力学
	# ==========================================================
	def shift_toward(self, target_shape: np.ndarray, plasticity: float,
					 new_content: Optional[str] = None,
					 rewrite_threshold: float = 0.45) -> None:
		"""被水流刷过：朝水流形状偏移一点。

		plasticity ∈ [0, 1]：
		  - 0 ：完全不变（永恒的记忆）
		  - 1 ：被水流完全替换（瞬时记忆）

		如果偏移之后，当前形状已经离 origin_shape 太远（漂移度
		超过 rewrite_threshold），说明原本承载的内容已经"被冲走了"。
		此时把 content 改写为最近一次刷过的水流文本——这道缝隙
		的"含义"已经变了。
		"""
		new_shape = (1.0 - plasticity) * self.shape + plasticity * target_shape
		self.shape = _normalize(new_shape)
		self.flow_count += 1
		self.last_flow_time = time.time()

		if new_content is not None and self.drift() > rewrite_threshold:
			self.content = new_content
			self.origin_shape = self.shape.copy()  # 漂移归零，记忆"翻了个新篇"

	def drift(self) -> float:
		"""当前形状与初始形状的余弦距离 ∈ [0, 2]，多数情况 ∈ [0, 1]。"""
		return float(1.0 - np.dot(self.shape, self.origin_shape))

	def age_seconds(self) -> float:
		return time.time() - self.creation_time

	def quiet_seconds(self) -> float:
		return time.time() - self.last_flow_time

	# ==========================================================
	#                      链接管理
	# ==========================================================
	def link_to(self, target_id: str, strength_delta: float = 1.0,
				cap: float = 16.0) -> None:
		"""加一条（或加强一条）通往 target_id 的暗道。

		如果已存在，强度累加；并夹一个上限，防止热点链接无止境膨胀。
		"""
		if target_id == self.id:
			return  # 不允许自连——会让水流原地打转
		current = self.outgoing_links.get(target_id, 0.0)
		new_strength = min(current + strength_delta, cap)
		self.outgoing_links[target_id] = new_strength

	def unlink(self, target_id: str) -> None:
		self.outgoing_links.pop(target_id, None)

	def decay_links(self, factor: float = 0.95, floor: float = 0.05) -> int:
		"""所有出度链接强度统一乘一个 <1 的衰减因子。

		低于 floor 的链接被认为已经"裂开了"，从字典里删掉。
		返回被删掉的链接数。睡眠期会用到。
		"""
		removed = []
		for tid in list(self.outgoing_links.keys()):
			s = self.outgoing_links[tid] * factor
			if s < floor:
				removed.append(tid)
			else:
				self.outgoing_links[tid] = s
		for tid in removed:
			del self.outgoing_links[tid]
		return len(removed)

	# ==========================================================
	#                       序列化
	# ==========================================================
	def to_dict(self) -> dict:
		return {
			"id": self.id,
			"content": self.content,
			"flow_count": self.flow_count,
			"last_flow_time": self.last_flow_time,
			"creation_time": self.creation_time,
			# 链接也存下来。空 dict 也写进去，结构清晰
			"outgoing_links": dict(self.outgoing_links),
		}

	@classmethod
	def from_dict(cls, d: dict, shape: np.ndarray, origin_shape: np.ndarray) -> "Fissure":
		f = cls(
			content=d["content"],
			shape=shape,
			origin_shape=origin_shape,
			id=d["id"],
			flow_count=d.get("flow_count", 0),
			last_flow_time=d.get("last_flow_time", time.time()),
			creation_time=d.get("creation_time", time.time()),
		)
		# 兼容旧存档：旧版没有 outgoing_links，给空 dict 即可
		raw_links = d.get("outgoing_links", {}) or {}
		f.outgoing_links = {str(k): float(v) for k, v in raw_links.items()}
		return f


def _normalize(v: np.ndarray) -> np.ndarray:
	n = float(np.linalg.norm(v))
	if n < 1e-9:
		return v
	return v / n
