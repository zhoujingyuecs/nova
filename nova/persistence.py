"""缝隙场的存盘与读取。

存档格式刻意做得朴素，方便人眼检查：
  field/
    meta.json        —— 维度、版本号、缝隙数等
    fissures.json    —— 每条缝隙的可读字段（id、content、时间、计数…）
    shapes.npy       —— 当前形状矩阵 (N, d) float32
    origins.npy      —— 出生形状矩阵 (N, d) float32

shapes.npy 与 fissures.json 中的顺序严格对齐。
"""

from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import Fissure


_VERSION = 1


def save_field(field: FissureField, path: Optional[str] = None) -> None:
	path = path or field.cfg.field_path
	os.makedirs(path, exist_ok=True)

	fissures = field.all()
	meta = {
		"version": _VERSION,
		"dim": field.dim,
		"count": len(fissures),
		"embedding_model": field.cfg.embedding_model,
	}
	with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
		json.dump(meta, f, ensure_ascii=False, indent=2)

	with open(os.path.join(path, "fissures.json"), "w", encoding="utf-8") as f:
		json.dump([fis.to_dict() for fis in fissures], f, ensure_ascii=False, indent=2)

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
		# 嵌入模型换了，老缝隙跟新空间对不上号——放弃旧记忆，从空开始。
		# 实际生产环境可以做迁移，这里直接抛给你提示。
		raise RuntimeError(
			f"维度不匹配：旧缝隙场 dim={meta.get('dim')}，"
			f"当前嵌入器 dim={embedding_dim}。请清空 {path} 或使用同一嵌入模型。"
		)

	with open(os.path.join(path, "fissures.json"), "r", encoding="utf-8") as f:
		fissure_dicts = json.load(f)
	shapes = np.load(os.path.join(path, "shapes.npy"))
	origins = np.load(os.path.join(path, "origins.npy"))

	for d, s, o in zip(fissure_dicts, shapes, origins):
		fis = Fissure.from_dict(d, shape=s, origin_shape=o)
		field._fissures[fis.id] = fis
		field._order.append(fis.id)
	field.sync_all()
	return field
