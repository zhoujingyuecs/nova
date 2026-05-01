"""睡眠：定期对陶土球做整理。

和走神不同，睡眠不产生新念头，只对既有结构做三件事：

  1. 修剪（prune）
     ── 那些极少被流过、又久未被刷新、并且早已漂移得面目全非的
        缝隙，应当被删除。它们既不再是初始的样子，也没有获得新的
        持续意义——是真正意义上的"被遗忘"。

  2. 合并（merge）
     ── 当两道缝隙的形状几乎重叠（余弦相似度 > merge_threshold），
        说明它们承载着同一个意思。合并它们，保留较老那条的内容
        （沉淀更久 → 更接近"主流"用法），把流量计数加起来。
        ★ 合并时把被吸收方的所有出度链接也挪给保留方，避免链接断裂。
        ★ v0.5 新增：合并时还要修补对话链指针 prev_id / next_id ——
           被吸收方在某段对话里如果是别人的"上一句"或"下一句"，
           那条链要无缝接到保留方上，不能让对话链断裂。

  3. 链接衰减（decay_links）
     ── 所有出度链接的强度乘一个 <1 的因子。
        强度低于 floor 的链接被认为已经"裂开了"，从字典里删掉。
        这模拟了"长期不被一起想起的两件事，会在心里渐渐分开"的现象。
        注意：对话链上的暗道因为基础强度很大（forward 4.0、backward 2.5），
        即便衰减很多次也不容易掉到 floor 之下，所以"刚说过的话"的链
        会保住相当长一段时间，这是有意为之。

修剪、合并、链接衰减都是不可逆的。可以放在某个固定时刻（比如每天
凌晨）跑一次，就是字面意义的"睡眠期巩固"。

★ 关于对话型缝隙：
   对话里每一句话都是独立缝隙（speaker != ""），而且每一句的内容
   独一无二（"你刚才说的那句话"和"我刚才说的那句话"语义上接近也合
   并不到一起，因为内容字符串不同）。但万一用户多次重复完全一样的
   话（"嗯。" / "嗯。"），合并仍然有可能发生——这时候上面对对话链
   的修补就用得上了。
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
	"""对陶土球做一次睡眠整理。返回统计信息。"""
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


# ============================================================
#                       修剪
# ============================================================
def _prune(field: FissureField, cfg: NovaConfig) -> int:
	"""把已经"被遗忘"的缝隙从场上去掉。"""
	to_remove: list = []
	for f in field.all():
		if (
			f.quiet_seconds() > cfg.prune_quiet_threshold
			and f.flow_count < cfg.prune_flow_threshold
			and f.drift() > cfg.prune_drift_threshold
		):
			to_remove.append(f.id)
	# field.remove() 内部会清理所有指向被删缝隙的暗道，以及 prev_id /
	# next_id 这两种对话链指针，所以这里不用再单独处理。
	for fid in to_remove:
		field.remove(fid)
	return len(to_remove)


# ============================================================
#                       合并
# ============================================================
def _merge(field: FissureField, cfg: NovaConfig) -> int:
	"""把几乎重叠的孪生缝隙合并成一条。

	做法：贪心一遍——按 flow_count 降序遍历，每条缝隙吸收所有与它
	相似度超过阈值的"晚辈"。这样高频用过的那条留下来，作为合并的
	代表。

	合并时同时合并出度链接：
	   - 被吸收方的 outgoing_links → 累加到保留方上
	   - 其他缝隙指向被吸收方的链接 → 改写为指向保留方
	★ v0.5 新增：合并对话链指针 prev_id / next_id：
	   - 如果 fj 在某段对话里是某条 X 的下一句（X.next_id == fj.id），
	     把 X.next_id 改写成 fi.id；
	   - 如果 fj 是某条 X 的上一句（X.prev_id == fj.id），
	     把 X.prev_id 改写成 fi.id；
	   - 如果 fi 自己还没有 prev/next，把 fj 的对应指针接过来。
	"""
	if len(field) < 2:
		return 0

	# 按 flow_count 降序
	all_f = sorted(field.all(), key=lambda f: -f.flow_count)
	consumed: set = set()
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

			# ---- 合并内容：保留更早出生那条的内容（沉淀更久） ----
			if fj.creation_time < fi.creation_time:
				fi.content = fj.content
				fi.origin_shape = fj.origin_shape.copy()
				fi.creation_time = fj.creation_time

			# ---- 合并形状：流量加权平均 ----
			w_total = fi.flow_count + fj.flow_count + 1e-6
			w_i = (fi.flow_count + 0.5) / w_total
			w_j = (fj.flow_count + 0.5) / w_total
			fi.shape = _normalize(w_i * fi.shape + w_j * fj.shape)
			fi.flow_count += fj.flow_count
			fi.last_flow_time = max(fi.last_flow_time, fj.last_flow_time)

			# ---- 合并场景元数据 ----
			# speaker / episode_id / turn_index：fi 已有的优先保留。
			# 万一 fi 是没有场景的老缝隙、fj 才是新对话里的，那就把
			# 场景信息接过来——这样合并不会丢掉对话上下文。
			if not fi.speaker and fj.speaker:
				fi.speaker = fj.speaker
			if not fi.episode_id and fj.episode_id:
				fi.episode_id = fj.episode_id
				fi.turn_index = fj.turn_index

			# ---- 合并 outgoing_links：fj 的所有出度链接挪到 fi ----
			for tid, strength in fj.outgoing_links.items():
				if tid == fi.id:
					continue  # 不形成自连
				fi.link_to(
					tid,
					strength_delta=strength,
					cap=cfg.link_strength_cap,
				)

			# ---- 改写所有指向 fj 的入度链接为指向 fi ----
			# 同时这一遍也顺手改写所有指向 fj 的对话链指针 prev_id / next_id。
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

			# ---- 把 fj 自己的对话链指针接到 fi 上（如果 fi 还没有的话） ----
			# 注意要避免自指：合并后链接两端不能都是 fi。
			if fj.prev_id and fj.prev_id != fi.id and not fi.prev_id:
				fi.prev_id = fj.prev_id
			if fj.next_id and fj.next_id != fi.id and not fi.next_id:
				fi.next_id = fj.next_id

			# ---- 物理移除 fj ----
			# field.remove() 会再次扫一遍，清理任何残留的指向 fj 的
			# 暗道和对话链指针——这是兜底。
			field.remove(fj.id)
			consumed.add(fj.id)
			merged_count += 1
	return merged_count


# ============================================================
#                       链接衰减
# ============================================================
def _decay_links(field: FissureField, cfg: NovaConfig) -> int:
	"""所有缝隙的出度链接统一衰减一次。返回被裁掉的链接总数。"""
	total = 0
	for f in field._fissures.values():
		total += f.decay_links(
			factor=cfg.link_decay_factor,
			floor=cfg.link_decay_floor,
		)
	return total
