"""陶土球（FissureField）：所有缝隙的集合。

它的核心职责：
  1. 持有所有缝隙；
  2. 高效地查找"附近的缝隙"——这是几何上的连接（语义相似度）；
  3. 管理"暗道"——缝隙之间的显式有向链接（共同被想起→建立通道）；
  4. 维护"对话链"——同一段交互内 prev_id/next_id 形成的时间链表；
  5. 计算局部水流密度，决定可塑性。

为什么需要"显式链接"——
  纯几何近邻有个毛病：水容易陷在一个语义簇里转。譬如"下雨"附近
  全是一些和雨有关的回忆，水流过来流过去都在这个簇内。但人脑里
  "下雨" 和 "外婆家檐角的风铃"在语义上隔得很远，却又常常一起浮
  起来——这是经验赋予的暗道，不是空间上能直接量出来的。

  显式链接是用来记录这种"经验性贴近"的。它单向、可累加、可衰减，
  类似神经网络里的赫布学习（Hebbian Learning）：一起激活就一起加强。

对话链（episode chain）
  一段对话里 turn N 和 turn N+1 之间有一种**比"经验暗道"更结实**
  的连接——prev_id/next_id 显式存指针，并额外建立强度很高的暗道。

矩阵向量化：所有缝隙形状摞成一个 (N, d) 的 numpy 矩阵 _matrix。
增删改时同步更新。这样查询"找最近 k 条缝隙"只是一次矩阵乘法，
在十几万条缝隙以内都不会成为瓶颈。
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
        self._order: list[str] = []
        self._matrix: np.ndarray = np.zeros((0, self.dim), dtype=np.float32)
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
        self._order.pop(idx)
        self._matrix = np.delete(self._matrix, idx, axis=0)
        self._idx = {fid_: i for i, fid_ in enumerate(self._order)}
        del self._fissures[fid]

        for other in self._fissures.values():
            if fid in other.outgoing_links:
                del other.outgoing_links[fid]
            if other.prev_id == fid:
                other.prev_id = ""
            if other.next_id == fid:
                other.next_id = ""

    # ==========================================================
    #                     矩阵同步
    # ==========================================================
    def sync_all(self) -> None:
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
        if len(self) == 0:
            return []
        shape = _normalize(shape)
        sims = self._matrix @ shape
        if exclude:
            for fid in exclude:
                idx = self._idx.get(fid)
                if idx is not None:
                    sims[idx] = -np.inf
        k = min(k, len(self))
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
    #               对话链（episode chain）
    # ==========================================================
    def chain_link(self, prev_id: str, next_id: str,
                   forward_strength: float, backward_strength: float) -> bool:
        if prev_id == next_id:
            return False
        prev_f = self._fissures.get(prev_id)
        next_f = self._fissures.get(next_id)
        if prev_f is None or next_f is None:
            return False

        next_f.prev_id = prev_id
        prev_f.next_id = next_id
        prev_f.link_to(next_id, strength_delta=forward_strength,
                       cap=self.cfg.link_strength_cap)
        next_f.link_to(prev_id, strength_delta=backward_strength,
                       cap=self.cfg.link_strength_cap)
        return True

    def walk_chain_back(self, start_id: str, k: int) -> list:
        out = []
        f = self._fissures.get(start_id)
        if f is None:
            return out
        cursor_id = f.prev_id
        seen = {start_id}
        for _ in range(k):
            if not cursor_id or cursor_id in seen:
                break
            cur = self._fissures.get(cursor_id)
            if cur is None:
                break
            out.append(cur)
            seen.add(cursor_id)
            cursor_id = cur.prev_id
        out.reverse()
        return out

    # ==========================================================
    #               局部密度（决定可塑性）
    # ==========================================================
    def local_flow_density(self, position: np.ndarray) -> float:
        if len(self) == 0:
            return 0.0
        position = _normalize(position)
        sims = self._matrix @ position
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
        density = self.local_flow_density(position)
        p = self.cfg.base_plasticity + self.cfg.density_plasticity_gain * math.log1p(density)
        return float(min(p, self.cfg.max_plasticity))

    # ==========================================================
    #                      统计/调试
    # ==========================================================
    def link_stats(self) -> dict:
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
