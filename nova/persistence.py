"""缝隙场的存盘与读取。

存档格式刻意做得朴素，方便人眼检查：
  field/
    meta.json        —— 维度、版本号、缝隙数等
    fissures.json    —— 每条缝隙的可读字段（id、content、时间、计数、★出度链接）
    shapes.npy       —— 当前形状矩阵 (N, d) float32
    origins.npy      —— 出生形状矩阵 (N, d) float32

shapes.npy 与 fissures.json 中的顺序严格对齐。

★ 这次新增：fissures.json 里每条缝隙带上 outgoing_links。
   旧版本的存档（没有这个字段）依然能读出来——只是所有链接为空。
   新建立的链接会随后续的 perceive/dream 慢慢长出来。
"""

from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import Fissure


# v1：原始版本（无链接）
# v2：加上 outgoing_links 字段
_VERSION = 2


def save_field(field: FissureField, path: Optional[str] = None) -> None:
	path = path or field.cfg.field_path
	os.makedirs(path, exist_ok=True)

	fissures = field.all()
	stats = field.link_stats()
	meta = {
		"version": _VERSION,
		"dim": field.dim,
		"count": len(fissures),
		"embedding_model": field.cfg.embedding_model,
		"link_stats": stats,
	}
	with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
		json.dump(meta, f, ensure_ascii=False, indent=2)

	with open(os.path.join(path, "fissures.json"), "w", encoding="utf-8") as f:
		json.dump(
			[fis.to_dict() for fis in fissures],
			f,
			ensure_ascii=False,
			indent=2,
		)

	if fissures:
		shapes = np.stack([fis.shape for fis in fissures]).astype(np.float32)
		origins = np.stack([fis.origin_shape for fis in fissures]).astype(np.float32)
	else:
		shapes = np.zeros((0, field.dim), dtype=np.float32)
		origins = np.zeros((0, field.dim), dtype=np.float32)
	np.save(os.path.join(path, "shapes.npy"), shapes)
	np.save(os.path.join(path, "origins.npy"), origins)


def load_field(cfg: NovaConfig, embedding_dim: int,
			   path: Optional[str] = None) -> FissureField:
	path = path or cfg.field_path
	field = FissureField(cfg, embedding_dim)

	meta_path = os.path.join(path, "meta.json")
	if not os.path.exists(meta_path):
		return field

	with open(meta_path, "r", encoding="utf-8") as f:
		meta = json.load(f)
	if meta.get("dim") != embedding_dim:
		# 嵌入模型换了，老缝隙跟新空间对不上号
		raise RuntimeError(
			f"维度不匹配：旧缝隙场 dim={meta.get('dim')}，"
			f"当前嵌入器 dim={embedding_dim}。请清空 {path} 或使用同一嵌入模型。"
		)

	fissures_json_path = os.path.join(path, "fissures.json")
	shapes_path = os.path.join(path, "shapes.npy")
	origins_path = os.path.join(path, "origins.npy")
	if not (os.path.exists(fissures_json_path)
			and os.path.exists(shapes_path)
			and os.path.exists(origins_path)):
		return field

	with open(fissures_json_path, "r", encoding="utf-8") as f:
		fissure_dicts = json.load(f)
	shapes = np.load(shapes_path)
	origins = np.load(origins_path)

	# 第一遍：加载所有缝隙（带链接），构建场
	for d, s, o in zip(fissure_dicts, shapes, origins):
		fis = Fissure.from_dict(d, shape=s, origin_shape=o)
		field._add_fissure(fis)

	# 第二遍：清理失效链接（指向已经不存在的 id 的）
	# 这种情况理论上不该发生，但以防万一
	valid_ids = set(field._fissures.keys())
	cleaned = 0
	for fis in field._fissures.values():
		for tid in list(fis.outgoing_links.keys()):
			if tid not in valid_ids:
				del fis.outgoing_links[tid]
				cleaned += 1
	if cleaned > 0:
		print(f"⚠️ 清理了 {cleaned} 条指向不存在缝隙的失效链接")

	field.sync_all()
	# 简短报告一下加载情况
	stats = field.link_stats()
	print(
		f"📦 缝隙场加载：{stats['node_count']} 条缝隙，"
		f"{stats['total_links']} 条暗道，"
		f"平均强度 {stats['mean_link_strength']:.2f}"
	)
	return field
