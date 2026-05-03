"""睡眠：定期对陶土球做整理。

和走神不同，睡眠不产生新念头，只对既有结构做三件事：

  1. 修剪（prune）
     极少被流过、又久未被刷新、并且早已漂移得面目全非的缝隙，
     被认为是"被遗忘"的，删掉。

  2. 合并（merge）
     当两道缝隙的形状几乎重叠（余弦相似度 > merge_threshold），
     说明它们承载着同一个意思。合并它们，保留较老那条的内容
     （沉淀更久 → 更接近"主流"用法），把流量计数加起来。
     合并时把被吸收方的所有出度链接也挪给保留方，避免链接断裂。
     合并时还要修补对话链指针 prev_id / next_id ——
     被吸收方在某段对话里如果是别人的"上一句"或"下一句"，
     那条链要无缝接到保留方上，不能让对话链断裂。

  3. 链接衰减（decay_links）
     所有出度链接的强度乘一个 <1 的因子。强度低于 floor 的链接
     被删掉。这模拟"长期不被一起想起的两件事，会在心里渐渐分开"。

这些操作不可逆。可以放在某个固定时刻（比如每天凌晨）跑一次，
就是字面意义的"睡眠期巩固"。
"""
from __future__ import annotations

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import _normalize


def consolidate(
    field: FissureField,
    cfg: NovaConfig,
    prune: bool = True,
    merge: bool = True,
    decay_links: bool = True,
) -> dict:
    stats = {
        "pruned": 0,
        "merged": 0,
        "links_decayed": 0,
        "before": len(field),
        "after": 0,
    }

    if prune:
        stats["pruned"] = _prune(field, cfg)
    if merge:
        stats["merged"] = _merge(field, cfg)
    if decay_links:
        stats["links_decayed"] = _decay_links(field, cfg)

    field.sync_all()
    stats["after"] = len(field)
    return stats


def _prune(field: FissureField, cfg: NovaConfig) -> int:
    to_remove: list = []
    for f in field.all():
        if (
            f.quiet_seconds() > cfg.prune_quiet_threshold
            and f.flow_count < cfg.prune_flow_threshold
            and f.drift() > cfg.prune_drift_threshold
        ):
            to_remove.append(f.id)
    for fid in to_remove:
        field.remove(fid)
    return len(to_remove)


def _merge(field: FissureField, cfg: NovaConfig) -> int:
    if len(field) < 2:
        return 0

    all_f = sorted(field.all(), key=lambda f: -f.flow_count)
    consumed: set = set()
    merged_count = 0

    for fi in all_f:
        if fi.id in consumed:
            continue
        neighbors = field.nearest(
            fi.shape, k=12, exclude={fi.id} | consumed
        )
        for fj, sim in neighbors:
            if sim < cfg.merge_threshold:
                break

            # 内容：保留更早出生那条
            if fj.creation_time < fi.creation_time:
                fi.content = fj.content
                fi.origin_shape = fj.origin_shape.copy()
                fi.creation_time = fj.creation_time

            # 形状：流量加权平均
            w_total = fi.flow_count + fj.flow_count + 1e-6
            w_i = (fi.flow_count + 0.5) / w_total
            w_j = (fj.flow_count + 0.5) / w_total
            fi.shape = _normalize(w_i * fi.shape + w_j * fj.shape)
            fi.flow_count += fj.flow_count
            fi.last_flow_time = max(fi.last_flow_time, fj.last_flow_time)

            # 场景元数据：fi 已有的优先保留
            if not fi.speaker and fj.speaker:
                fi.speaker = fj.speaker
            if not fi.episode_id and fj.episode_id:
                fi.episode_id = fj.episode_id
                fi.turn_index = fj.turn_index

            # outgoing_links：fj 的全部挪到 fi
            for tid, strength in fj.outgoing_links.items():
                if tid == fi.id:
                    continue
                fi.link_to(
                    tid,
                    strength_delta=strength,
                    cap=cfg.link_strength_cap,
                )

            # 改写所有指向 fj 的入度链接 + 对话链指针
            for other in field._fissures.values():
                if other.id in (fi.id, fj.id):
                    continue
                if fj.id in other.outgoing_links:
                    strength = other.outgoing_links.pop(fj.id)
                    other.link_to(
                        fi.id,
                        strength_delta=strength,
                        cap=cfg.link_strength_cap,
                    )
                if other.prev_id == fj.id:
                    other.prev_id = fi.id
                if other.next_id == fj.id:
                    other.next_id = fi.id

            # 把 fj 的对话链指针接到 fi 上（如果 fi 还没有的话）
            if fj.prev_id and fj.prev_id != fi.id and not fi.prev_id:
                fi.prev_id = fj.prev_id
            if fj.next_id and fj.next_id != fi.id and not fi.next_id:
                fi.next_id = fj.next_id

            field.remove(fj.id)
            consumed.add(fj.id)
            merged_count += 1
    return merged_count


def _decay_links(field: FissureField, cfg: NovaConfig) -> int:
    total = 0
    for f in field._fissures.values():
        total += f.decay_links(
            factor=cfg.link_decay_factor,
            floor=cfg.link_decay_floor,
        )
    return total
