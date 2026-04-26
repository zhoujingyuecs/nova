"""睡眠：定期对陶土球做整理。

和走神不同，睡眠不产生新念头，只对既有结构做两件事：

  1. 修剪（prune）
     ── 那些极少被流过、又久未被刷新、并且早已漂移得面目全非的
        缝隙，应当被删除。它们既不再是初始的样子，也没有获得新的
        持续意义——是真正意义上的"被遗忘"。

  2. 合并（merge）
     ── 当两道缝隙的形状几乎重叠（余弦相似度 > merge_threshold），
        说明它们承载着同一个意思。合并它们，保留较老那条的内容
        （沉淀更久 → 更接近"主流"用法），把流量计数加起来。

修剪和合并都是不可逆的。只在你确实希望释放空间、清理重复时调用。

可以放在某个固定时刻（比如每天凌晨）跑一次，就是字面意义的"睡眠期巩固"。
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
) -> dict:
	"""对陶土球做一次睡眠整理。返回统计信息。"""
	stats = {"pruned": 0, "merged": 0, "before": len(field), "after": 0}

	if prune:
		stats["pruned"] = _prune(field, cfg)

	if merge:
		stats["merged"] = _merge(field, cfg)

	field.sync_all()
	stats["after"] = len(field)
	return stats


def _prune(field: FissureField, cfg: NovaConfig) -> int:
	"""把已经"被遗忘"的缝隙从场上去掉。"""
	to_remove: list[str] = []
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
	"""把几乎重叠的孪生缝隙合并成一条。

	做法：贪心一遍——按 flow_count 降序遍历，每条缝隙吸收所有与它
	相似度超过阈值的"晚辈"。这样高频用过的那条留下来，作为合并的
	代表。
	"""
	if len(field) < 2:
		return 0

	# 按 flow_count 降序
	all_f = sorted(field.all(), key=lambda f: -f.flow_count)
	consumed: set[str] = set()
	merged_count = 0

	for fi in all_f:
		if fi.id in consumed:
			continue
		# 找出所有与它非常相似的其他缝隙
		neighbors = field.nearest(
			fi.shape, k=12, exclude={fi.id} | consumed
		)
		for fj, sim in neighbors:
			if sim < cfg.merge_threshold:
				break  # nearest 是按相似度降序的
			# 合并：保留更早出生那条的内容（沉淀更久）
			if fj.creation_time < fi.creation_time:
				fi.content = fj.content
				fi.origin_shape = fj.origin_shape.copy()
				fi.creation_time = fj.creation_time
			# 形状取流量加权平均
			w_total = fi.flow_count + fj.flow_count + 1e-6
			w_i = (fi.flow_count + 0.5) / w_total
			w_j = (fj.flow_count + 0.5) / w_total
			fi.shape = _normalize(w_i * fi.shape + w_j * fj.shape)
			fi.flow_count += fj.flow_count
			fi.last_flow_time = max(fi.last_flow_time, fj.last_flow_time)
			field.remove(fj.id)
			consumed.add(fj.id)
			merged_count += 1
	return merged_count
