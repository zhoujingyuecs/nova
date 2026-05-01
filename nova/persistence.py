"""缝隙场的存盘与读取。

存档格式刻意做得朴素，方便人眼检查：
  field/
    meta.json        —— 维度、版本号、缝隙数等
    fissures.json    —— 每条缝隙的可读字段
                        （id、content、时间、计数、出度链接、★场景元数据）
    shapes.npy       —— 当前形状矩阵 (N, d) float32
    origins.npy      —— 出生形状矩阵 (N, d) float32

shapes.npy 与 fissures.json 中的顺序严格对齐。

版本演进：
  v1: 原始版本（无链接）
  v2: 加上 outgoing_links 字段
  v3: ★ 加上 speaker / episode_id / turn_index / prev_id / next_id
       —— 缝隙现在记得"是谁说的、属于哪段对话、这段话里的第几句、
          紧挨着的前一句和后一句是哪条"。

旧存档自动兼容：v1/v2 读进来时，新字段都是空的，相当于没有场景信息——
nova 不会因此崩溃，只是新对话开始之前，老的回忆都散在那里没有链。
随后续的 perceive 慢慢长出新对话的链。
"""

from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import Fissure


_VERSION = 3


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

	# 第一遍：加载所有缝隙（带链接和场景元数据），构建场
	for d, s, o in zip(fissure_dicts, shapes, origins):
		fis = Fissure.from_dict(d, shape=s, origin_shape=o)
		field._add_fissure(fis)

	# 第二遍：清理所有失效引用——
	#   ① 指向已不存在缝隙的 outgoing_links 暗道
	#   ② 指向已不存在缝隙的 prev_id / next_id 对话链指针
	# 理论上不该出现，但万一（手动改动了文件，或者并发存档）就不会崩。
	valid_ids = set(field._fissures.keys())
	cleaned_links = 0
	cleaned_chain = 0
	for fis in field._fissures.values():
		for tid in list(fis.outgoing_links.keys()):
			if tid not in valid_ids:
				del fis.outgoing_links[tid]
				cleaned_links += 1
		if fis.prev_id and fis.prev_id not in valid_ids:
			fis.prev_id = ""
			cleaned_chain += 1
		if fis.next_id and fis.next_id not in valid_ids:
			fis.next_id = ""
			cleaned_chain += 1
	if cleaned_links > 0:
		print(f"⚠️ 清理了 {cleaned_links} 条指向不存在缝隙的失效链接")
	if cleaned_chain > 0:
		print(f"⚠️ 清理了 {cleaned_chain} 条断裂的对话链指针")

	field.sync_all()

	# 简短报告一下加载情况
	stats = field.link_stats()
	loaded_version = meta.get("version", 1)
	chain_nodes = stats.get("chain_nodes", 0)
	print(
		f"📦 缝隙场加载（存档版本 v{loaded_version}）："
		f"{stats['node_count']} 条缝隙，"
		f"{stats['total_links']} 条暗道，"
		f"其中 {chain_nodes} 条带对话链，"
		f"平均链强度 {stats['mean_link_strength']:.2f}"
	)
	if loaded_version < _VERSION:
		print(
			f"   （从 v{loaded_version} 升级到 v{_VERSION}：旧缝隙没有场景元数据，"
			f"新对话从此刻开始会带上 speaker / episode_id / 对话链。）"
		)
	return field
