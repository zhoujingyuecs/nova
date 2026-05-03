"""把陶土球摊到平面上看。

向量是 d 维的，肉眼看不见；用 PCA 或 t-SNE 投到 2D 就能可视化。

  - 点的位置 ── 缝隙在语义空间的位置（投影后）
  - 点的大小 ── flow_count（被流过的次数，越大越显眼）
  - 点的颜色 ── 最近一次被刷过的距今时间（越红越新鲜，越蓝越冷僻）
  - 点之间的细线 ── 显式的 outgoing_links（暗道）
                    线粗细随链接强度变化

PCA 默认快、确定、零参数；t-SNE 看局部结构更好但慢且不稳定。
默认 PCA。
"""
from __future__ import annotations

import os
from typing import Literal, Optional

import numpy as np

from .field import FissureField


def _ensure_chinese_font() -> None:
    try:
        import matplotlib
        from matplotlib import font_manager
        candidates = [
            "Noto Sans CJK SC", "Noto Sans CJK", "Source Han Sans SC",
            "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "PingFang SC",
            "Microsoft YaHei", "SimHei",
        ]
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in candidates:
            if name in available:
                matplotlib.rcParams["font.sans-serif"] = [name]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return
    except Exception:
        pass


def render_field(
    field: FissureField,
    output_path: str,
    method: Literal["pca", "tsne"] = "pca",
    *,
    label_top_k: int = 6,
    figsize: tuple = (10, 8),
    dpi: int = 120,
    show_links: bool = True,
    link_max: int = 800,
) -> Optional[str]:
    """把缝隙场画到 output_path。返回成功保存的路径，失败返回 None。"""
    if len(field) < 3:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"⚠️ 缺少 matplotlib：{e}")
        return None

    _ensure_chinese_font()

    all_f = field.all()
    shapes = np.stack([f.shape for f in all_f])

    if method == "tsne":
        try:
            from sklearn.manifold import TSNE
            xy = TSNE(n_components=2, perplexity=min(30, max(5, len(all_f) // 4)),
                      init="pca", learning_rate="auto", random_state=0).fit_transform(shapes)
        except Exception as e:
            print(f"⚠️ t-SNE 失败，回退到 PCA：{e}")
            method = "pca"
    if method == "pca":
        try:
            from sklearn.decomposition import PCA
            xy = PCA(n_components=2).fit_transform(shapes)
        except Exception as e:
            print(f"⚠️ PCA 失败：{e}")
            return None

    # 大小 = flow_count（log 压一下）
    sizes = 30 + 60 * np.log1p([f.flow_count for f in all_f])
    # 颜色 = 最近被刷过到现在的时长（小 = 新 = 红，大 = 老 = 蓝）
    import time as _time
    now = _time.time()
    quiet = np.array([now - f.last_flow_time for f in all_f])
    if quiet.max() > quiet.min():
        c = (quiet - quiet.min()) / (quiet.max() - quiet.min())
    else:
        c = np.zeros_like(quiet)

    fig, ax = plt.subplots(figsize=figsize)

    if show_links:
        idx_of = {f.id: i for i, f in enumerate(all_f)}
        edges_drawn = 0
        for f in all_f:
            for tid, strength in f.outgoing_links.items():
                if tid not in idx_of:
                    continue
                i, j = idx_of[f.id], idx_of[tid]
                lw = min(1.6, 0.15 + 0.25 * np.log1p(strength))
                ax.plot([xy[i, 0], xy[j, 0]],
                        [xy[i, 1], xy[j, 1]],
                        color=(0.6, 0.6, 0.6, 0.25), linewidth=lw, zorder=1)
                edges_drawn += 1
                if edges_drawn >= link_max:
                    break
            if edges_drawn >= link_max:
                break

    sc = ax.scatter(
        xy[:, 0], xy[:, 1],
        s=sizes, c=c, cmap="coolwarm_r", alpha=0.85,
        edgecolors="white", linewidths=0.5, zorder=2,
    )

    # 给最热的几条点贴上标签
    hot = sorted(range(len(all_f)),
                 key=lambda i: -all_f[i].flow_count)[:label_top_k]
    for i in hot:
        text = all_f[i].content[:18]
        ax.annotate(text, (xy[i, 0], xy[i, 1]),
                    fontsize=8, alpha=0.9, zorder=3)

    ax.set_title(f"FissureField · {len(all_f)} 缝隙 · {method.upper()}")
    ax.set_xticks([]); ax.set_yticks([])
    plt.colorbar(sc, ax=ax, label="新 ←→ 久")
    fig.tight_layout()

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)
    return output_path
