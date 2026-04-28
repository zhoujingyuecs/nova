"""把陶土球摊到平面上看。

向量是 d 维的，肉眼看不见；用 PCA 或 t-SNE 投到 2D 就能可视化。

  - 点的位置 ── 缝隙在语义空间的位置（投影后）
  - 点的大小 ── flow_count（被流过的次数，越大越显眼）
  - 点的颜色 ── 最近一次被刷过的距今时间（越红越新鲜，越蓝越冷僻）
  - ★ 点之间的细线 ── 显式的 outgoing_links（暗道）
                      线粗细随链接强度变化

这给你两个直觉：
  ① 高频区域成团聚集 ── 那是 nova 当前的"思维焦点"，
                        也是高可塑性 / 短期记忆的来源；
  ② 散落在外围的冷蓝点 ── 那是稳定的长期记忆。
  ③ ★ 跨簇的线 ── 那些就是真正意义上的"裂缝"，水流跨山谷的暗道。

PCA 默认快、确定、零参数；t-SNE 看局部结构更好但慢且不稳定。
默认 PCA。

中文字体 ── 如果系统里有 Noto / 思源 / 文泉驿 / 苹方 / 雅黑等
任意一种，会自动用上；没有就降级为英文标签，避免一堆方框。
"""

from __future__ import annotations

import os
from typing import Literal, Optional

import numpy as np

from .field import FissureField


_PREFERRED_CJK_FONTS = (
	"Noto Sans CJK SC",
	"Noto Sans CJK TC",
	"Source Han Sans SC",
	"Source Han Sans CN",
	"WenQuanYi Zen Hei",
	"WenQuanYi Micro Hei",
	"PingFang SC",
	"Hiragino Sans GB",
	"Microsoft YaHei",
	"SimHei",
)


def _setup_cjk_font() -> bool:
	"""尝试启用一种系统里能找到的中文字体。返回是否成功。"""
	import matplotlib

	from matplotlib import font_manager, rcParams

	available = {f.name for f in font_manager.fontManager.ttflist}
	for name in _PREFERRED_CJK_FONTS:
		if name in available:
			rcParams["font.sans-serif"] = [name] + list(rcParams["font.sans-serif"])
			rcParams["axes.unicode_minus"] = False
			return True
	return False


def render_field(
	field: FissureField,
	output_path: str,
	method: Literal["pca", "tsne"] = "pca",
	title: Optional[str] = None,
	annotate_top: int = 6,
	draw_links: bool = True,
	max_links_drawn: int = 400,
) -> Optional[str]:
	"""把当前缝隙场画成一张 PNG。

	annotate_top: 在最热的几条缝隙旁边标注其内容片段。
	draw_links:   是否画出 outgoing_links。
	max_links_drawn: 链接太多时只随机画这么多条，避免图变成毛毡。
	"""
	if len(field) < 3:
		return None

	import matplotlib

	matplotlib.use("Agg")
	import matplotlib.pyplot as plt

	has_cjk = _setup_cjk_font()

	# 投影
	matrix = field._matrix
	if method == "tsne" and len(field) >= 5:
		from sklearn.manifold import TSNE

		proj = TSNE(
			n_components=2,
			perplexity=min(30, max(5, len(field) // 3)),
			init="pca",
			random_state=0,
		).fit_transform(matrix)
	else:
		from sklearn.decomposition import PCA

		proj = PCA(n_components=2, random_state=0).fit_transform(matrix)

	fissures = field.all()
	id_to_idx = {f.id: i for i, f in enumerate(fissures)}
	flow_counts = np.array([f.flow_count for f in fissures], dtype=np.float32)
	quiet = np.array([f.quiet_seconds() for f in fissures], dtype=np.float32)
	freshness = np.exp(-quiet / 86400.0)
	sizes = 30.0 + 18.0 * np.log1p(flow_counts)

	fig, ax = plt.subplots(figsize=(11, 11))

	# ---- 画链接（先画，所以会在散点下面） ----
	if draw_links:
		import random

		all_links = []
		for src in fissures:
			si = id_to_idx[src.id]
			for tid, strength in src.outgoing_links.items():
				ti = id_to_idx.get(tid)
				if ti is None:
					continue
				all_links.append((si, ti, float(strength)))
		if len(all_links) > max_links_drawn:
			all_links = random.sample(all_links, max_links_drawn)

		for si, ti, strength in all_links:
			# 强度 → 透明度 + 粗细
			alpha = min(0.05 + 0.04 * np.log1p(strength), 0.35)
			lw = 0.3 + 0.15 * np.log1p(strength)
			ax.plot(
				[proj[si, 0], proj[ti, 0]],
				[proj[si, 1], proj[ti, 1]],
				color="#666666",
				alpha=alpha,
				linewidth=lw,
				zorder=1,
			)

	scat = ax.scatter(
		proj[:, 0],
		proj[:, 1],
		s=sizes,
		c=freshness,
		cmap="RdYlBu_r",
		alpha=0.78,
		edgecolors="black",
		linewidth=0.45,
		vmin=0.0,
		vmax=1.0,
		zorder=2,
	)

	# 注释最热的若干条（中文字体不可用时跳过 —— 一堆方框反而难看）
	if annotate_top > 0 and has_cjk:
		hot_idx = np.argsort(-flow_counts)[:annotate_top]
		for i in hot_idx:
			snippet = fissures[i].content
			if len(snippet) > 22:
				snippet = snippet[:22] + "…"
			ax.annotate(
				snippet,
				(proj[i, 0], proj[i, 1]),
				fontsize=8,
				alpha=0.85,
				xytext=(6, 4),
				textcoords="offset points",
				zorder=3,
			)

	cbar = plt.colorbar(scat, ax=ax, fraction=0.04, pad=0.02)
	cbar.set_label(
		"新鲜度（最近被刷过 ↑）" if has_cjk else "freshness (recent ↑)",
		rotation=270,
		labelpad=18,
	)

	if title is None:
		title = "nova 的陶土球" if has_cjk else "nova's clay ball"
	stats = field.link_stats()
	subtitle = (
		f"{len(field)} 道缝隙 · {stats['total_links']} 条暗道 · {method.upper()} 投影"
		if has_cjk
		else f"{len(field)} fissures · {stats['total_links']} links · {method.upper()} projection"
	)
	ax.set_title(f"{title}\n{subtitle}", fontsize=13)
	ax.set_xticks([])
	ax.set_yticks([])
	for spine in ax.spines.values():
		spine.set_visible(False)
	plt.tight_layout()

	os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
	plt.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="white")
	plt.close(fig)
	return output_path
