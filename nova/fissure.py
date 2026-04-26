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

水流冲刷时，缝隙形状朝水流形状偏移；偏移的速度由"可塑性"
决定，可塑性又由局部水流密度决定（见 field.py）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Fissure:
	content: str
	shape: np.ndarray            # 当前形状（单位向量）
	origin_shape: np.ndarray     # 出生时的形状（不可变，用于度量漂移）

	id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
	flow_count: int = 0
	last_flow_time: float = field(default_factory=time.time)
	creation_time: float = field(default_factory=time.time)

	def __post_init__(self):
		# 把形状统一归一化到单位球面上，所有相似度都用余弦
		self.shape = _normalize(self.shape)
		self.origin_shape = _normalize(self.origin_shape)

	# ---------- 动力学 ----------
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

	# ---------- 序列化 ----------
	def to_dict(self) -> dict:
		return {
			"id": self.id,
			"content": self.content,
			"flow_count": self.flow_count,
			"last_flow_time": self.last_flow_time,
			"creation_time": self.creation_time,
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
		return f


def _normalize(v: np.ndarray) -> np.ndarray:
	n = float(np.linalg.norm(v))
	if n < 1e-9:
		return v
	return v / n
