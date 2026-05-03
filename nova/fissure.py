"""缝隙（Fissure）：陶土球上的一道裂痕。

每条缝隙承载一段记忆。它有：
  - content：当前承载的文本（"裂缝里盛着的东西"）。短，不超过几百字。
            真正的细节请去工作区里写一篇 .md，缝隙只刻一个浮起的影子。
  - shape：当前的形状向量（embedding）。形状即语义位置：
            形状相似的缝隙在球里也是相邻的。
  - origin_shape：刚被刻下时的形状。和当前 shape 的距离，
                  就是这道缝隙"漂移了多远"——它原本承载的
                  记忆，已经被多少次水流冲刷得变了样子。
  - flow_count：历史上有多少次水流流经此处。
  - last_flow_time：最近一次被填满的时间戳。

  outgoing_links —— 这道缝隙通向哪些其他缝隙的"地下通道"
    -----------------------------------------------------------
    单纯几何近邻有个毛病：水容易陷在一团相似的回忆里转。
    显式链接让两条在语义上很远的回忆，只要它们在一段经历里挨着出现过，
    就有一条直通的暗道。这就是"高低不平的洞穴"，让水能从一个山谷
    流到另一个山谷，而不只是顺着等高线绕圈。

  场景元数据：
    speaker     —— 谁说的：「外人」「我」「走神」 或 ""（无来源）
    episode_id  —— 同一段连续交互的标识；同一场对话里所有缝隙共享
    turn_index  —— 在这段交互里第几句（0,1,2,...）
    prev_id     —— 同一段对话里紧邻的上一句
    next_id     —— 同一段对话里紧邻的下一句

    这些字段是"软"的：不影响 shape，不参与几何近邻；它们只是缝隙
    的身世标签。但 mind 在拼回忆时会读它们，渲染成"[5 分钟前·有人
    对我说]"这样的小标签，给 nova 还原场景。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---- speaker 的有限集合（约定，不强校验） ----
SPEAKER_OUTSIDER = "外人"   # 别人对她说的
SPEAKER_SELF = "我"          # 她说出口的
SPEAKER_DAYDREAM = "走神"    # 没人在场时她自己冒出来的念头
SPEAKER_NONE = ""            # 种子记忆 / 抽象意象 等


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

    # ---------- 显式裂缝（暗道） ----------
    outgoing_links: dict = field(default_factory=dict)

    # ---------- 场景元数据 ----------
    speaker: str = SPEAKER_NONE
    episode_id: str = ""
    turn_index: int = 0
    prev_id: str = ""
    next_id: str = ""

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
            self.origin_shape = self.shape.copy()  # 漂移归零

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
        if target_id == self.id:
            return  # 不允许自连
        current = self.outgoing_links.get(target_id, 0.0)
        new_strength = min(current + strength_delta, cap)
        self.outgoing_links[target_id] = new_strength

    def unlink(self, target_id: str) -> None:
        self.outgoing_links.pop(target_id, None)

    def decay_links(self, factor: float = 0.95, floor: float = 0.05) -> int:
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
            "outgoing_links": dict(self.outgoing_links),
            "speaker": self.speaker,
            "episode_id": self.episode_id,
            "turn_index": self.turn_index,
            "prev_id": self.prev_id,
            "next_id": self.next_id,
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
        raw_links = d.get("outgoing_links", {}) or {}
        f.outgoing_links = {str(k): float(v) for k, v in raw_links.items()}
        f.speaker = d.get("speaker", SPEAKER_NONE) or SPEAKER_NONE
        f.episode_id = d.get("episode_id", "") or ""
        f.turn_index = int(d.get("turn_index", 0) or 0)
        f.prev_id = d.get("prev_id", "") or ""
        f.next_id = d.get("next_id", "") or ""
        return f


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return v
    return v / n
