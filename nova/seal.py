"""SealRegistry：nova 自己写下的"暂时不想看的念头清单"。

—— v1.3.1 新增。

# 设计

念头本身不带 sealed 标签。"封印"是 nova 的**当前一份偏好清单**，
存在外部、可增可删、永远是 nova 自己的选择。

  - nova 可以在回应里写 `<seal>...</seal>` 块，把符合某个描述的念头团
    封起来——封起来的念头**仍然会浮起来**，但在 prompt 里只显示
    "[这一团我曾决定不展开]"而不展开内容。
  - nova 可以在任何时候写 `<unseal>...</unseal>` 块把自己之前封的拿掉。
  - 封印**不挡动作**——动作管制完全由 HabitField 在 tool 层做。
  - 封印**不让 nova 沉默**——LanguageGate 只看新颖度 / 行动压力 / 模式。
  - 封印的唯一效果是 prompt 里的 cluster 内容不展开。

这是一个"想到了，**我自己选择**不去多看那一团内容"的开关——可以随时关。

# Seal 怎么匹配 cluster

一份 SealEntry 有：
  - target_fingerprint：如果 nova 写明了具体 fingerprint，精确匹配
  - keywords：一组关键词，cluster.summary 或激活裂缝的 content 命中
              其中任意一个就算命中
  - origin_thought_id：nova 写下这条 seal 时正在想的那个 cluster 的 id
                       （留底，方便后续 unseal）

匹配只在 ClayTickEngine 渲染 prompt 时使用，不影响 cluster 的形成。

# 格式约定（给 nova 看的写法）

  <seal>
  reason: 我现在不想反复咀嚼这件事
  keywords: 攻击性反击、骂回去
  </seal>

  <unseal>
  keywords: 攻击性反击
  </unseal>

或者更短：

  <seal>骂回去</seal>          # keywords 单段
  <unseal>骂回去</unseal>

nova 也可以直接按 fingerprint 封印（高级用法）：

  <seal>fingerprint: abc123def4567890</seal>

# 落盘

  {field_path}/seals.json：一个 list，每条 SealEntry。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional


# 用来从 nova 回应里抓 <seal> / <unseal> 块的正则。
# 这是**回应解析**，不是"判断 nova 心里想什么"——nova 显式写出来才会被抓。
_SEAL_RE = re.compile(r"<seal>(.*?)</seal>", re.IGNORECASE | re.DOTALL)
_UNSEAL_RE = re.compile(r"<unseal>(.*?)</unseal>", re.IGNORECASE | re.DOTALL)


@dataclass
class SealEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    keywords: list[str] = field(default_factory=list)
    target_fingerprint: str = ""    # 精确匹配某个 cluster fingerprint（可选）
    reason: str = ""                # nova 自己写下的"为什么封"
    origin_thought_id: str = ""     # 封它时活着的 cluster id
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SealEntry":
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:12],
            keywords=[str(k).strip() for k in (d.get("keywords") or []) if k],
            target_fingerprint=d.get("target_fingerprint", "") or "",
            reason=d.get("reason", "") or "",
            origin_thought_id=d.get("origin_thought_id", "") or "",
            created_at=float(d.get("created_at", time.time())),
        )

    def short_label(self) -> str:
        if self.target_fingerprint:
            return f"fingerprint={self.target_fingerprint[:8]}"
        if self.keywords:
            return "/".join(self.keywords[:3])
        return "（无标签）"


class SealRegistry:
    """nova 当前生效的封印清单。

    每个 SealEntry 是一条"我决定不展开这类念头"的记录。
    """

    def __init__(self, store_path: Optional[str] = None):
        self.entries: list[SealEntry] = []
        self.store_path = store_path
        if store_path:
            self._load(store_path)

    # =====================================================================
    # 增删
    # =====================================================================
    def add(self, entry: SealEntry) -> None:
        # 去重：同样 keywords 集合 + 同样 fingerprint 就不重复加
        for e in self.entries:
            if (sorted(e.keywords) == sorted(entry.keywords)
                    and e.target_fingerprint == entry.target_fingerprint):
                return
        self.entries.append(entry)

    def remove_matching(self, *, keywords: list[str] = None,
                        fingerprint: str = "",
                        entry_id: str = "") -> list[SealEntry]:
        """删掉所有匹配的 entry。返回被删的那些。

        匹配规则：
          - entry_id 精确匹配
          - fingerprint 精确匹配
          - keywords：传入的关键词集合是某个 entry.keywords 的子集 → 匹配
            （也就是 nova 可以用更宽的 unseal 描述拿掉精确的 seal）
        """
        keywords = keywords or []
        removed: list[SealEntry] = []
        kept: list[SealEntry] = []
        for e in self.entries:
            if entry_id and e.id == entry_id:
                removed.append(e); continue
            if fingerprint and e.target_fingerprint == fingerprint:
                removed.append(e); continue
            if keywords and e.keywords:
                # nova 写的 unseal keywords ⊆ entry.keywords → 命中
                k_set = {k.lower() for k in keywords}
                e_set = {k.lower() for k in e.keywords}
                if k_set & e_set:
                    removed.append(e); continue
            kept.append(e)
        self.entries = kept
        return removed

    # =====================================================================
    # 查询：一个 cluster 当前算不算被封印？
    # =====================================================================
    def is_sealed(self, *, fingerprint: str, summary: str,
                  fissure_contents: Iterable[str]) -> Optional[SealEntry]:
        """看看当前这个 cluster 是否被某条 entry 封印。

        命中规则：
          - 任何 entry.target_fingerprint 精确匹配 → 命中
          - 任何 entry.keywords 中的关键词在 summary 或任意 fissure_content
            里出现（不分大小写）→ 命中
        返回第一条命中的 entry，没有就 None。
        """
        if not self.entries:
            return None
        haystack = " ".join(list(fissure_contents) + [summary or ""]).lower()
        for e in self.entries:
            if e.target_fingerprint and e.target_fingerprint == fingerprint:
                return e
            for kw in e.keywords:
                if kw and kw.lower() in haystack:
                    return e
        return None

    # =====================================================================
    # 渲染（让 nova 在 prompt 里看见自己定的清单）
    # =====================================================================
    def render_for_prompt(self, max_chars: int = 600) -> str:
        if not self.entries:
            return ""
        lines = [
            "[我自己写下的封印清单]",
            "（这是你之前用 <seal> 决定不展开的念头类别。",
            " 它们**不挡你说话也不挡你动作**——只是 prompt 里这类念头团"
            "不会展开内容，给你少一点反复咀嚼。",
            " 任何时候你想拿掉，用 <unseal>...</unseal> 即可。）",
        ]
        for e in self.entries[:8]:
            label = e.short_label()
            reason = (e.reason or "").strip()
            line = f"  - {label}"
            if reason:
                line += f"  ：{reason[:60]}"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n（…清单已截断）"

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
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"entries": [e.to_dict() for e in self.entries],
                     "saved_at": time.time()},
                    f, ensure_ascii=False, indent=2,
                )
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as exc:
            print(f"⚠️ seals 落盘失败（不致命）：{exc}")

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"⚠️ seals 损坏，从空清单启动：{exc}")
            return
        for d in (data.get("entries") or []):
            try:
                self.entries.append(SealEntry.from_dict(d))
            except Exception:
                continue


# --------------------------------------------------------------------------
# 解析 <seal> / <unseal> 块
# --------------------------------------------------------------------------
def extract_seal_blocks(text: str) -> tuple[list[SealEntry], list[dict]]:
    """从 nova 的回应里抓所有 <seal> 块和 <unseal> 块。

    返回 (new_seals, unseal_specs)。
    - new_seals: 直接可加入 SealRegistry 的 SealEntry 列表
    - unseal_specs: list of dict, 每个含 keys: keywords, fingerprint, entry_id
    """
    if not text:
        return [], []

    new_seals = [_parse_seal_block(m.group(1)) for m in _SEAL_RE.finditer(text)]
    new_seals = [e for e in new_seals if e is not None]

    unseal_specs = [_parse_unseal_block(m.group(1)) for m in _UNSEAL_RE.finditer(text)]
    unseal_specs = [s for s in unseal_specs if s]
    return new_seals, unseal_specs


def strip_seal_blocks(text: str) -> str:
    if not text:
        return text
    text = _SEAL_RE.sub("", text)
    text = _UNSEAL_RE.sub("", text)
    return text


def _parse_seal_block(block: str) -> Optional[SealEntry]:
    """支持两种格式：

      短：  <seal>骂回去</seal>           — 整段当作 keywords，用 / 或 ， 分割
      长：  <seal>
              reason: 不想反复咀嚼
              keywords: 攻击性反击、骂回去
              fingerprint: abc123
            </seal>
    """
    block = (block or "").strip()
    if not block:
        return None
    entry = SealEntry()

    # 看看是不是长格式（有 ":" 行）
    has_kv = any(":" in line for line in block.splitlines())
    if has_kv:
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in ("keywords", "keyword", "关键词"):
                entry.keywords.extend(_split_keywords(val))
            elif key in ("fingerprint", "fp", "指纹"):
                entry.target_fingerprint = val.strip()
            elif key in ("reason", "原因", "为什么"):
                entry.reason = val
    else:
        entry.keywords = _split_keywords(block)

    if not entry.keywords and not entry.target_fingerprint:
        return None
    return entry


def _parse_unseal_block(block: str) -> Optional[dict]:
    block = (block or "").strip()
    if not block:
        return None
    spec = {"keywords": [], "fingerprint": "", "entry_id": ""}

    has_kv = any(":" in line for line in block.splitlines())
    if has_kv:
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in ("keywords", "keyword", "关键词"):
                spec["keywords"].extend(_split_keywords(val))
            elif key in ("fingerprint", "fp", "指纹"):
                spec["fingerprint"] = val
            elif key in ("id", "entry_id"):
                spec["entry_id"] = val
    else:
        spec["keywords"] = _split_keywords(block)

    if not (spec["keywords"] or spec["fingerprint"] or spec["entry_id"]):
        return None
    return spec


def _split_keywords(s: str) -> list[str]:
    """用任意常见分隔符切关键词。"""
    s = (s or "").strip()
    if not s:
        return []
    # 中英逗号 / 顿号 / 斜杠 / 分号
    parts = re.split(r"[、，,/;；]+", s)
    return [p.strip() for p in parts if p.strip()]


__all__ = [
    "SealEntry", "SealRegistry",
    "extract_seal_blocks", "strip_seal_blocks",
]
