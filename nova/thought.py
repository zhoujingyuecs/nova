"""ThoughtCluster：前语言的念头。

—— v1.3.1 简化版。

# 一句话

  ThoughtCluster = 当前被激活的一组裂缝 + 它们的动力学
                   （强度 / 好恶 / 紧张 / 行动压力 / 新颖度 / 稳定度）

它**在 LLM 被调用之前**就已经成型，让 nova 拥有"前语言层的念头"。
语言皮层（LLM）后到，把这个团块翻译成话。

# 关于 policy

v1.3 第一版给 cluster 加了 render_policy / action_policy 用来表达
"想到但不说 / 想到但不做"。结果是 nova 一直被这套 policy 卡住——
关键字规则太敏感，几乎所有念头都被打成 forbid。

v1.3.1 把这套删了。

  - cluster 默认就是"全开"——什么都可以说，什么都可以做。
  - 行动管制完全交给 HabitField（在 tool 派发层）；
  - 是否说话交给 LanguageGate（看新颖度 / 行动压力 / 模式）；
  - 念头本身不带任何标签，**它就是"心里浮起来的东西"**。

如果 nova 自己想压住某个具体念头团不去看它的内容，那是另一个机制
（SealRegistry），由 nova 自己用 `<seal>` / `<unseal>` 块控制。
seal 不是 cluster 的属性，是 nova 写在外面的一张可增删的清单——
可以随时自己拿掉。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field


# --------------------------------------------------------------------------
# ThoughtCluster
# --------------------------------------------------------------------------
@dataclass
class ThoughtCluster:
    """一个被激活的念头团。

    一组同时点亮的裂缝 + 它们的动力学。LLM 看着这个对象决定怎么说话，
    它本身不靠 LLM 生成，也不携带任何"该不该说 / 该不该做"的标签。
    """

    # ---- 身份 ----
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    fingerprint: str = ""    # = sha1(sorted(fissure_ids))，用于跨 tick 识别同一团
    fissure_ids: list[str] = field(default_factory=list)

    # ---- 动力学 ----
    # 激活强度：0~1。被刚激活时 ≈ 1.0，每个 tick 衰减。
    activation: float = 1.0

    # 稳定度：被多次重新激活后会上升，表示这个团正在变成"持续关注"
    stability: float = 0.0

    # 新颖度：首次出现时 = 1.0，每次再激活会衰减。novelty 高 → 值得说出口。
    novelty: float = 1.0

    # ---- 情绪色调 ----
    # 这些值由 ClayTickEngine 从激活的裂缝本身推断
    # （看裂缝的 kind / epistemic_state / unresolved），
    # 不是从输入文本里 grep 出来的。
    valence: float = 0.0          # -1（厌恶/失败/紧张）~ +1（亲近/确认）
    arousal: float = 0.0          # 0（平静）~ 1（紧张/兴奋）
    agency_pressure: float = 0.0  # 0（没什么想做的）~ 1（想行动的冲动很强）

    # ---- 元数据 ----
    summary: str = ""             # 一句话标签，不必完整自然语言
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    activation_count: int = 1

    # =====================================================================
    # 生命周期
    # =====================================================================
    def reactivate(self, *, novelty_decay: float = 0.7,
                   stability_gain: float = 0.12) -> None:
        """这个团又被同样的裂缝组合激活了——增加稳定度，降低新颖度。"""
        self.activation = min(1.0, self.activation + 0.5)
        self.stability = min(1.0, self.stability + stability_gain)
        self.novelty = max(0.0, self.novelty * novelty_decay)
        self.activation_count += 1
        self.last_active = time.time()

    def decay(self, factor: float = 0.85, floor: float = 0.02) -> None:
        """每个 tick 自然衰减。activation 跌到 floor 以下时这个团算"淡出"了。"""
        self.activation *= factor
        if self.activation < floor:
            self.activation = 0.0
        # 稳定度也会缓慢回落，但比 activation 慢得多
        self.stability = max(0.0, self.stability * (1.0 - (1.0 - factor) * 0.3))

    def is_alive(self) -> bool:
        return self.activation > 0.0

    # =====================================================================
    # 渲染
    # =====================================================================
    def render_short(self) -> str:
        """一行短描述，用来在 prompt 里给 LLM 看。"""
        meta = (
            f"活={self.activation:.2f} "
            f"好恶={self.valence:+.2f} "
            f"紧={self.arousal:.2f} "
            f"行动压力={self.agency_pressure:.2f} "
            f"新={self.novelty:.2f}"
        )
        return f"念头团（{meta}）：{self.summary or '（未命名）'}"

    # =====================================================================
    # 序列化
    # =====================================================================
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ThoughtCluster":
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:12],
            fingerprint=d.get("fingerprint", ""),
            fissure_ids=[str(x) for x in (d.get("fissure_ids") or [])],
            activation=float(d.get("activation", 0.0)),
            stability=float(d.get("stability", 0.0)),
            novelty=float(d.get("novelty", 0.0)),
            valence=float(d.get("valence", 0.0)),
            arousal=float(d.get("arousal", 0.0)),
            agency_pressure=float(d.get("agency_pressure", 0.0)),
            summary=d.get("summary", ""),
            created_at=float(d.get("created_at", time.time())),
            last_active=float(d.get("last_active", time.time())),
            activation_count=int(d.get("activation_count", 1)),
        )


# --------------------------------------------------------------------------
# 工具：从一组裂缝 id 算 fingerprint
# --------------------------------------------------------------------------
def fissure_fingerprint(fissure_ids: list[str]) -> str:
    """把一组 fissure id 折叠成稳定的 fingerprint。

    同样的 id 组合 → 同样的 fingerprint → 同样的 cluster。
    顺序无关。
    """
    if not fissure_ids:
        return ""
    s = "|".join(sorted(fissure_ids))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


__all__ = ["ThoughtCluster", "fissure_fingerprint"]
