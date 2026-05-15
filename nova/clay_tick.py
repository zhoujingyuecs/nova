"""ClayTickEngine：陶土球的"自转"——不调 LLM 的思考。

—— v1.3.1 重写：动力学完全来自地形（裂缝本身的元数据），不再扫描文本。

# 一句话

  tick() 让陶土球转一下：水流激活一组裂缝，形成或更新 ThoughtCluster；
  cluster 的好恶 / 紧张 / 行动压力 完全由**激活的裂缝是什么**决定，
  不由"输入文本里有没有出现某些词"决定。

# 工作流

  1. 取入水种子（来自外界刺激或当前 SelfState 形状）
  2. ConsciousnessFlow.flow() 收集激活的裂缝（已有逻辑）
  3. 把激活的裂缝折叠成一个 ThoughtCluster
       - 同一 fingerprint（同样的裂缝组合）→ 复用旧 cluster 并 reactivate
       - 否则新建
  4. 计算 cluster 的动力学
       - valence ← 裂缝的 epistemic_state / kind（error 拉低；observed 中性）
       - arousal ← unresolved 比例、kind=request/error 比例、agency 来源
       - agency_pressure ← unresolved + error 的密度
       这里**不看 stimulus 文本一个字**——动力学只从地形里读出来。
  5. 衰减所有旧 cluster；丢掉 activation 跌到 0 的
  6. 返回当前还活着的 cluster 列表（按 activation 排序）

# 关于 policy

v1.3 第一版用关键字 + habit weight 给 cluster 自动打 forbid 标签——
那是错的。v1.3.1 删掉了整个 policy 机制：

  - cluster 不再有 render_policy / action_policy
  - habit 不再影响 cluster
  - 动作管制完全交给 HabitGate 在 tool 派发层做
  - 是否说话交给 LanguageGate 看新颖度 / 模式 / 压力

如果 nova 想暂时不展开某团念头，她写 `<seal>` 块——那是 SealRegistry
管的事，不是 cluster 自己的属性。
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional, TYPE_CHECKING

import numpy as np

from .fissure import Fissure
from .perception import (
    EPISTEMIC_ERROR,
    EPISTEMIC_OBSERVED,
    EPISTEMIC_UNVERIFIED,
)
from .thought import ThoughtCluster, fissure_fingerprint

if TYPE_CHECKING:
    from .field import FissureField
    from .flow import ConsciousnessFlow


# --------------------------------------------------------------------------
# ClayTickEngine
# --------------------------------------------------------------------------
class ClayTickEngine:
    """陶土球的自转引擎。

    它只对 FissureField + ConsciousnessFlow 施加影响，并返回当前的
    念头团。**不调 LLM，不扫描文本，不依赖 habit_field。**
    """

    def __init__(
        self,
        field: "FissureField",
        flow_engine: "ConsciousnessFlow",
        *,
        max_clusters: int = 8,
        decay_factor: float = 0.85,
        store_path: Optional[str] = None,
    ):
        self.field = field
        self.flow_engine = flow_engine
        self.max_clusters = max_clusters
        self.decay_factor = decay_factor
        self.store_path = store_path

        self.clusters: dict[str, ThoughtCluster] = {}
        if store_path:
            self._load(store_path)

    # =====================================================================
    # 主入口
    # =====================================================================
    def tick(
        self,
        seed_shape: np.ndarray,
        *,
        stimulus_summary: str = "",
        recent_history: Optional[set] = None,
        mandatory_anchors: Optional[list] = None,
    ) -> list[ThoughtCluster]:
        """让陶土球转一下：

          - 衰减所有旧 cluster
          - 用水流激活当前裂缝
          - 形成 / 更新一个 cluster
          - 返回所有当前还活着的 cluster（按 activation 排序）

        stimulus_summary 只用来给新 cluster 一个一句话标签，
        **不参与动力学计算**——动力学只从裂缝读。
        """
        # 0. 先让旧的衰减
        self._decay_all()

        # 1. 水流（已经是 v1.0 的逻辑，复用即可）
        activated = self.flow_engine.flow(
            seed_shape,
            recent_history=recent_history,
            mandatory_anchors=mandatory_anchors,
        )

        if not activated:
            return list(self._alive_sorted())

        # 2. 折叠成一个 cluster
        self._form_or_reactivate_cluster(
            activated_fissures=activated,
            stimulus_summary=stimulus_summary,
        )

        # 3. 修剪 cluster 数量
        self._prune()

        # 4. 返回活着的
        return list(self._alive_sorted())

    # =====================================================================
    # 取活着的 / 排序
    # =====================================================================
    def alive(self) -> list[ThoughtCluster]:
        return list(self._alive_sorted())

    def primary(self) -> Optional[ThoughtCluster]:
        """最亮的那一个。"""
        alive = self._alive_sorted()
        return alive[0] if alive else None

    def _alive_sorted(self) -> list[ThoughtCluster]:
        alive = [c for c in self.clusters.values() if c.is_alive()]
        alive.sort(key=lambda c: c.activation, reverse=True)
        return alive

    # =====================================================================
    # 形成 / 重新激活
    # =====================================================================
    def _form_or_reactivate_cluster(
        self,
        *,
        activated_fissures: list[Fissure],
        stimulus_summary: str,
    ) -> ThoughtCluster:
        fissure_ids = [f.id for f in activated_fissures]
        fp = fissure_fingerprint(fissure_ids)

        # 已经存在的 cluster？fingerprint 完全相同 → 直接 reactivate
        existing = next(
            (c for c in self.clusters.values() if c.fingerprint == fp),
            None,
        )

        if existing is not None:
            existing.reactivate()
            self._update_dynamics(existing, activated_fissures)
            return existing

        # 新 cluster
        cluster = ThoughtCluster(
            fingerprint=fp,
            fissure_ids=fissure_ids,
            activation=1.0,
            stability=0.05,
            novelty=1.0,
        )
        self._update_dynamics(cluster, activated_fissures)
        cluster.summary = self._auto_summary(activated_fissures, stimulus_summary)
        self.clusters[cluster.id] = cluster
        return cluster

    # =====================================================================
    # 动力学：完全从地形（激活的裂缝）推断
    # =====================================================================
    def _update_dynamics(
        self,
        cluster: ThoughtCluster,
        activated_fissures: list[Fissure],
    ) -> None:
        """从激活的裂缝本身推 valence / arousal / agency_pressure。

        **不看输入文本一个字**——这些信号在裂缝创建时就已经写进了
        每条裂缝的 kind / epistemic_state / unresolved 字段。这里
        只是把它们汇总成 cluster 级的标量。

        每条裂缝贡献：

          valence:
            +0.10  epistemic_state = observed       （这件事我确认过）
             0     epistemic_state = unverified
            -0.30  epistemic_state = error          （错误 / 失败的痕迹）
            -0.10  kind = error                     （记录的就是个错）
            +0.05  kind = response                  （我自己回应过的，中性偏正）

          arousal:
            +0.20  unresolved = True
            +0.15  kind = request
            +0.20  kind = error
            +0.10  epistemic_state = error
            +0.05  epistemic_state = unverified

          agency_pressure:
            +0.25  unresolved = True
            +0.15  kind = request
            +0.10  kind = error                    （想修一修的冲动）

        之后求平均并夹到合理区间。
        """
        n = max(1, len(activated_fissures))
        v_sum = 0.0
        a_sum = 0.0
        p_sum = 0.0

        for f in activated_fissures:
            ep = getattr(f, "epistemic_state", "") or ""
            kd = getattr(f, "kind", "") or ""
            ur = bool(getattr(f, "unresolved", False))

            # valence
            if ep == EPISTEMIC_ERROR:
                v_sum -= 0.30
            elif ep == EPISTEMIC_OBSERVED:
                v_sum += 0.10
            # （EPISTEMIC_UNVERIFIED 等其它 epistemic_state 不影响 valence）

            if kd == "error":
                v_sum -= 0.10
            elif kd == "response":
                v_sum += 0.05

            # arousal
            if ur:
                a_sum += 0.20
            if kd == "request":
                a_sum += 0.15
            elif kd == "error":
                a_sum += 0.20
            if ep == EPISTEMIC_ERROR:
                a_sum += 0.10
            elif ep == EPISTEMIC_UNVERIFIED:
                a_sum += 0.05

            # agency_pressure
            if ur:
                p_sum += 0.25
            if kd == "request":
                p_sum += 0.15
            elif kd == "error":
                p_sum += 0.10

        valence = max(-1.0, min(1.0, v_sum / n))
        arousal = max(0.0, min(1.0, a_sum / n))
        pressure = max(0.0, min(1.0, p_sum / n))

        # 平滑：和现有值做一个加权平均，避免一 tick 跳到完全相反的情绪
        if cluster.activation_count > 1:
            blend = 0.6
            cluster.valence = (1 - blend) * cluster.valence + blend * valence
            cluster.arousal = (1 - blend) * cluster.arousal + blend * arousal
            cluster.agency_pressure = (1 - blend) * cluster.agency_pressure + blend * pressure
        else:
            cluster.valence = valence
            cluster.arousal = arousal
            cluster.agency_pressure = pressure

    # =====================================================================
    # 自动 summary：在没有 LLM 的情况下也要能给一句话标签
    # =====================================================================
    def _auto_summary(
        self,
        activated_fissures: list[Fissure],
        stimulus_summary: str,
    ) -> str:
        """挑活水流里最具代表性的一条做标签——不调 LLM。

        优先级：刚刚的 stimulus_summary > 最近的 unresolved > 第一条非空 content。
        """
        if stimulus_summary:
            return _truncate(stimulus_summary.replace("\n", " "), 80)
        for f in activated_fissures:
            if getattr(f, "unresolved", False) and f.content.strip():
                return _truncate(f.content.replace("\n", " "), 80)
        for f in activated_fissures:
            if f.content and f.content.strip():
                return _truncate(f.content.replace("\n", " "), 80)
        return "（无明显主题的念头团）"

    # =====================================================================
    # 衰减 / 修剪
    # =====================================================================
    def _decay_all(self) -> None:
        dead = []
        for cid, c in self.clusters.items():
            c.decay(factor=self.decay_factor)
            if not c.is_alive():
                dead.append(cid)
        for cid in dead:
            self.clusters.pop(cid, None)

    def _prune(self) -> None:
        # 清理已不存在的 fissure id（被 sleep consolidation 删过的）
        valid_ids = set(self.field._fissures.keys())
        for c in self.clusters.values():
            c.fissure_ids = [fid for fid in c.fissure_ids if fid in valid_ids]
            if c.fissure_ids:
                c.fingerprint = fissure_fingerprint(c.fissure_ids)

        # 数量超过 max_clusters，按 activation 排序，砍掉尾巴
        if len(self.clusters) <= self.max_clusters:
            return
        ranked = sorted(
            self.clusters.values(),
            key=lambda c: c.activation,
            reverse=True,
        )
        keep = {c.id for c in ranked[: self.max_clusters]}
        self.clusters = {cid: c for cid, c in self.clusters.items() if cid in keep}

    # =====================================================================
    # 渲染（给 prompt 顶部用）
    # =====================================================================
    def render_for_prompt(
        self,
        *,
        max_chars: int = 1500,
        fissure_lookup=None,
        seal_registry=None,
    ) -> str:
        """渲染当前活的念头团给 LLM 看。

        seal_registry（可选）：如果传了，nova 自己封印过的 cluster
        只显示标签和动力学，不展开 fissure 内容。这是 nova 自己写
        过 <seal> 的结果——不是任何外部规则强加的。
        """
        alive = self._alive_sorted()
        if not alive:
            return ""

        lines = [
            "[前语言念头浮起来的样子]",
            "（这些念头**已经**在你心里浮起来了。你不是从虚空里生成念头，"
            "你的工作是把这些浮起来的念头翻译成你自己的话。",
            " 这一段不带任何禁令——什么都可以说，什么都可以做。",
            " 如果想让某一类念头**以后不再展开内容**，写 <seal>...</seal>；"
            "想拿掉封印，写 <unseal>...</unseal>。封印不挡你说话也不挡你动作，"
            "只是 prompt 里这一团不展开。）",
        ]
        for i, c in enumerate(alive, 1):
            lines.append("")
            lines.append(f"#{i}  {c.render_short()}")
            # 取这一团激活的裂缝的 content
            contents: list[str] = []
            if fissure_lookup:
                for fid in c.fissure_ids[:8]:
                    f = fissure_lookup(fid)
                    if f is None:
                        continue
                    content = (f.content or "").replace("\n", " ").strip()
                    if content:
                        contents.append(content)

            # 看 nova 自己有没有封它
            sealed_by = None
            if seal_registry is not None:
                sealed_by = seal_registry.is_sealed(
                    fingerprint=c.fingerprint,
                    summary=c.summary,
                    fissure_contents=contents,
                )

            if sealed_by is not None:
                lines.append(
                    f"  痕迹：（这一团你之前用 <seal> 标过——"
                    f"标签：{sealed_by.short_label()}"
                    + (f"；原因：{sealed_by.reason[:40]}"
                       if sealed_by.reason else "")
                    + "。它仍然亮在你心里，但内容不在这里展开。"
                    "如果想拿掉封印，写 <unseal>。）"
                )
            else:
                lines.append("  痕迹：")
                shown = 0
                for content in contents[:6]:
                    if shown >= 6:
                        break
                    lines.append(f"    - {_truncate(content, 110)}")
                    shown += 1
                if shown == 0:
                    lines.append("    - （裂缝内容已被消解或为空）")

        text = "\n".join(lines)
        return _truncate_block(text, max_chars)

    # =====================================================================
    # 持久化
    # =====================================================================
    def save(self, path: Optional[str] = None) -> None:
        path = path or self.store_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            data = {
                "saved_at": time.time(),
                "clusters": [c.to_dict() for c in self.clusters.values()],
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            print(f"⚠️ clusters 落盘失败（不致命）：{e}")

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ clusters 损坏，从空念头池启动：{e}")
            return
        for d in (data.get("clusters") or []):
            try:
                c = ThoughtCluster.from_dict(d)
                if c.is_alive():
                    self.clusters[c.id] = c
            except Exception:
                continue

    # =====================================================================
    # 调试
    # =====================================================================
    def stats(self) -> dict:
        alive = self._alive_sorted()
        return {
            "total": len(self.clusters),
            "alive": len(alive),
            "max_activation": float(alive[0].activation) if alive else 0.0,
            "max_arousal": max((c.arousal for c in alive), default=0.0),
            "max_pressure": max((c.agency_pressure for c in alive), default=0.0),
            "max_novelty": max((c.novelty for c in alive), default=0.0),
        }


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def _truncate_block(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n（…前语言层渲染被截断）"


__all__ = ["ClayTickEngine"]
