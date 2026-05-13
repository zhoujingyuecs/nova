"""HabitField：程序性记忆层。

—— 这是 v1.1 的核心新增。

老版本的 nova 有"联想记忆"——FissureField + ConsciousnessFlow 能把
相关的旧片段浮上来。但你被她坑过的那种重复犯错，并不是因为她记不起来，
而是因为：**想起来 ≠ 改变行为**。

人之所以能"长记性"，靠的不只是回忆，而是几个物理回路：
  - 海马（hippocampus）：在相似场景下补全旧经历     ← FissureField 已经在做
  - 前额叶（PFC）：把当前规则维持在工作状态          ← 这一层缺失
  - 基底节（basal ganglia）：动作选择的 Go / No-Go    ← 这一层缺失
  - 多巴胺：根据后果改写动作权重                       ← 这一层缺失
  - 习惯回路：cue → action 的快速通路                  ← 这一层缺失

老版本 nova 的回忆只能"被拼进 prompt 当材料"，它不能阻止 LLM 沿着
默认生成模式继续写出错误动作。所以你看到的反复犯错——明明记得"用
weibo_tool"，下一秒还是写了 weibo_loop_orchestrator——本质是：
回忆没有变成抑制控制（inhibitory control）。

HabitField 就是补上这一层。它不是新的笔记本，也不是另一个记忆库。
它是**会改变 nova 下一步动作的硬约束集合**：

  HabitRule
    - cue        什么场景下这条规则生效（关键词 + 形状向量）
    - forbid     这种场景下禁止包含哪些字符串的工具动作
    - forbid_except   例外：动作里同时含这些则放行
    - require    必须做什么（人类可读）
    - prefer     推荐怎样做（人类可读）
    - rationale  为什么有这条规则（最影响 nova 是否信服）
    - weight     强度，决定 prompt 里的顺序和语气
    - reinforcement / violation 计数

相比"在 notes 里写一句话"，HabitRule 多做四件事：

  1) 它会在每次 perceive / think 之前主动评估：当前场景是否激活？
     激活的规则被放到 prompt 顶部，作为"硬约束"，不再混在普通回忆里。
  2) nova 生成 <tool> 块要执行时，HabitGate 先扫一遍 forbid 列表。
     命中就直接拦下来，把"这是一次违反规则的尝试"作为 tool result 回灌
     给 nova。她不会真的把错误动作打到虚拟机里。
  3) 用户使用反复纠正语言（"我说了很多次" / "你又..." / "记住"）时，
     系统会自动加强最相关的那条规则。这是"被骂一次→以后类似场景动作
     权重降低"的多巴胺式负反馈。
  4) 规则自身从 nova 的 <rule>...</rule> 块产生。她在对话中识别出
     "这是一条长期不变的硬约束"时，写一段 <rule> 出来，系统替她保存。

只有 forbid / require / prefer 是"硬"的，其余字段都是给人和给 nova
读的注释。匹配是朴素的子串扫描，不需要 LLM 调用。

—— 不是用来代替笔记的：
笔记本（notes/）适合写"事实、上下文、复杂步骤"——它们是给 nova 自己
事后查阅用的。HabitRule 只适合写"在这种场景下，绝对不要做 X / 必须
做 Y"。两者并行，互不替代。
"""
from __future__ import annotations

import collections
import dataclasses
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np


# ----------------------------------------------------------------------
# 字段常量
# ----------------------------------------------------------------------
SOURCE_USER = "user"            # 直接由用户陈述派生
SOURCE_SELF = "self"            # nova 自己识别后写的 <rule>
SOURCE_SYSTEM = "system"        # 启动时种子加载
SOURCE_REINFORCED = "reinforced"  # 反复加强后形成

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
STATUS_SUPERSEDED = "superseded"


# ----------------------------------------------------------------------
# HabitRule
# ----------------------------------------------------------------------
@dataclass
class HabitRule:
    """一条程序性记忆规则。

    它的存在意义是改变 nova 下一步要选哪个动作，不是描述世界。
    """
    name: str
    cue_text: str = ""                                # 触发场景的描述（也用作 embedding 来源）
    cue_keywords: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)              # 禁止包含的子串（OR）
    forbid_except: list[str] = field(default_factory=list)        # 例外子串（同时包含则放行）
    require: list[str] = field(default_factory=list)             # 必须做的描述
    prefer: list[str] = field(default_factory=list)              # 推荐做法
    rationale: str = ""
    weight: float = 1.0
    confidence: float = 0.7
    source: str = SOURCE_SELF
    activation_count: int = 0
    violation_count: int = 0
    success_count: int = 0
    reinforcement_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_activated_at: float = 0.0
    last_violated_at: float = 0.0
    status: str = STATUS_ACTIVE
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    # 这两条不进 JSON，运行时计算
    cue_shape: Optional[np.ndarray] = None
    _cue_keywords_lower: Optional[list[str]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cue_shape", None)
        d.pop("_cue_keywords_lower", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "HabitRule":
        # 容忍历史字段缺失
        allowed = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in (data or {}).items() if k in allowed}
        if "name" not in kwargs:
            kwargs["name"] = data.get("title") or "（未命名规则）"
        rule = cls(**kwargs)
        rule._refresh_keyword_cache()
        return rule

    def _refresh_keyword_cache(self) -> None:
        self._cue_keywords_lower = [k.strip().lower() for k in self.cue_keywords if k and k.strip()]

    def keywords_lower(self) -> list[str]:
        if self._cue_keywords_lower is None:
            self._refresh_keyword_cache()
        return self._cue_keywords_lower or []

    # ------------------------------------------------------------------
    # 强度调节
    # ------------------------------------------------------------------
    def boost(self, delta: float, *, cap: float = 20.0) -> None:
        self.weight = float(min(self.weight + delta, cap))
        self.updated_at = time.time()

    def decay(self, factor: float, *, floor: float = 0.2) -> None:
        self.weight = float(max(self.weight * factor, floor))


# ----------------------------------------------------------------------
# 解析 <rule>...</rule> 块
# ----------------------------------------------------------------------
RULE_BLOCK_RE = re.compile(r"<rule\b[^>]*>(.*?)</rule>", re.DOTALL | re.IGNORECASE)


_LIST_KEYS = {"forbid", "forbid_except", "allow_if", "require", "prefer", "cue_keywords"}
_KEY_ALIASES = {
    "title": "name",
    "cue": "cue_text",
    "keywords": "cue_keywords",
    "allow_if": "forbid_except",
    "exception": "forbid_except",
    "must": "require",
    "should": "prefer",
    "because": "rationale",
    "reason": "rationale",
    "why": "rationale",
}


def _split_csv(value: str) -> list[str]:
    parts: list[str] = []
    for piece in re.split(r"[,，;；、\n]+", value):
        piece = piece.strip().strip("\"'`")
        if piece:
            parts.append(piece)
    return parts


def parse_rule_block(block: str) -> Optional[dict]:
    """把 <rule>...</rule> 内部解析成可喂给 HabitRule.from_dict 的字典。

    支持两种风格混用：

        name: 微博工具铁律
        cue: 微博, 评论, 发帖
        forbid:
          - weibo_loop_orchestrator
          - state.json
          - 9222
        forbid: 扫码
        allow_if: weibo_tool.py
        because: 登录态保存在 weibo_tool.py 的浏览器会话里
    """
    text = block.strip()
    if not text:
        return None

    raw: dict[str, list[str] | str] = collections.defaultdict(list)
    cur_key: Optional[str] = None
    cur_inline_set = False  # 当前 key 是否已经收过 inline 值

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            cur_key = None
            cur_inline_set = False
            continue

        # `- xxx` 风格的列表项
        if cur_key and re.match(r"^\s*[-*]\s+", line):
            value = re.sub(r"^\s*[-*]\s+", "", line).strip().strip("\"'`")
            if value:
                if isinstance(raw[cur_key], str):
                    raw[cur_key] = [str(raw[cur_key])]
                raw[cur_key].append(value)  # type: ignore[arg-type]
            continue

        # `key: value` 风格
        m = re.match(r"^\s*([\w\u4e00-\u9fa5_-]+)\s*[:：]\s*(.*)$", line)
        if not m:
            # 非 key:value 行，忽略，避免污染
            cur_key = None
            cur_inline_set = False
            continue
        key = m.group(1).strip().lower()
        value = m.group(2).strip().strip("\"'`")
        key = _KEY_ALIASES.get(key, key)
        cur_key = key
        cur_inline_set = bool(value)

        if not value:
            # 后续行将以 - xxx 形式来填这个 key
            if key in _LIST_KEYS and key not in raw:
                raw[key] = []
            continue

        if key in _LIST_KEYS:
            existing = raw.get(key)
            if isinstance(existing, list):
                existing.extend(_split_csv(value))
            else:
                raw[key] = _split_csv(value)
        else:
            raw[key] = value

    if not raw.get("name") and not raw.get("forbid") and not raw.get("require"):
        return None

    out: dict = {}
    if "name" in raw:
        out["name"] = str(raw["name"]).strip()
    if "cue_text" in raw:
        out["cue_text"] = str(raw["cue_text"]).strip()
    elif "cue_keywords" in raw and isinstance(raw["cue_keywords"], list):
        out["cue_text"] = "、".join(raw["cue_keywords"][:6])
    if "cue_keywords" in raw:
        kws = raw["cue_keywords"]
        if isinstance(kws, str):
            kws = _split_csv(kws)
        out["cue_keywords"] = list(kws)
    elif out.get("cue_text"):
        # 简单从 cue_text 抽关键词
        kws = _split_csv(out["cue_text"])
        if kws:
            out["cue_keywords"] = kws

    for list_key in ("forbid", "forbid_except", "require", "prefer"):
        v = raw.get(list_key)
        if v is None:
            continue
        if isinstance(v, list):
            out[list_key] = [str(x).strip() for x in v if str(x).strip()]
        else:
            out[list_key] = _split_csv(str(v))

    if "rationale" in raw:
        out["rationale"] = str(raw["rationale"]).strip()
    if "weight" in raw:
        try:
            out["weight"] = float(raw["weight"])
        except (TypeError, ValueError):
            pass
    if "confidence" in raw:
        try:
            out["confidence"] = float(raw["confidence"])
        except (TypeError, ValueError):
            pass

    # 规则必须至少有一个 forbid 或 require/prefer，否则没意义
    if not (out.get("forbid") or out.get("require") or out.get("prefer")):
        return None

    if not out.get("name"):
        # 用 forbid 或 cue 凑一个名字
        seed = (out.get("forbid", [""]) or [""])[0] or out.get("cue_text", "未命名规则")
        out["name"] = (seed[:24] or "未命名规则").strip()

    return out


def extract_rule_blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for m in RULE_BLOCK_RE.finditer(text or ""):
        parsed = parse_rule_block(m.group(1))
        if parsed:
            out.append(parsed)
    return out


def strip_rule_blocks(text: str) -> str:
    """把 <rule> 块从对外可见文本里去掉，留对话本体。"""
    return RULE_BLOCK_RE.sub("", text or "").strip()


# ----------------------------------------------------------------------
# 反复纠正信号检测
# ----------------------------------------------------------------------
# 故意宽松：宁可多触发一次"看看 nova 是不是又走老路了"，
# 也不要漏掉一次明显的反复纠正。
_REINFORCEMENT_PATTERNS = [
    re.compile(r"我.{0,4}说.{0,3}过.{0,4}(很多|多少|n?\s*次|遍)", re.I),
    re.compile(r"(说了|讲了|提了).{0,4}(很多|多少|多少次|n\s*次|多遍)", re.I),
    re.compile(r"我都说(过)?.{0,5}(多少|多少遍|n?\s*遍)", re.I),
    re.compile(r"为什么.{0,6}(又|还在|还要|忘)", re.I),
    re.compile(r"(你.{0,3})?怎么.{0,5}又", re.I),
    re.compile(r"再.{0,3}这么.{0,2}(干|做)", re.I),
    re.compile(r"还是.{0,4}没.{0,3}(记住|改)", re.I),
    re.compile(r"信誓旦旦.{0,8}(忘|又)", re.I),
    re.compile(r"是否.{0,3}还?.{0,2}记得", re.I),
    re.compile(r"记住", re.I),
    re.compile(r"别老", re.I),
    re.compile(r"别再.{0,3}(自己|又)", re.I),
    re.compile(r"又来", re.I),
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\bagain\b", re.I),
    re.compile(r"\bremember\b", re.I),
]


def detect_reinforcement_signal(user_text: str) -> bool:
    if not user_text:
        return False
    for pat in _REINFORCEMENT_PATTERNS:
        if pat.search(user_text):
            return True
    return False


# ----------------------------------------------------------------------
# HabitField：规则集合
# ----------------------------------------------------------------------
class HabitField:
    """所有 HabitRule 的容器。

    ╴ 持久化：单一 habits.json，原子写入。
    ╴ 检索：基于关键词命中 + cue embedding 余弦相似度。
    ╴ 修改：rule 加权 / 衰减 / 归档。
    """

    def __init__(self, cfg, embedder, path: str):
        self.cfg = cfg
        self.embedder = embedder
        self.path = path
        self._rules: dict[str, HabitRule] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, cfg, embedder, path: str) -> "HabitField":
        hf = cls(cfg, embedder, path)
        if not os.path.exists(path):
            # 第一次启动：尝试从 seed_habits_file 注入
            seed_path = getattr(cfg, "seed_habits_file", None)
            if seed_path and os.path.exists(seed_path):
                hf._load_seeds(seed_path)
            return hf
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ habits.json 损坏，从空规则集重启：{e}")
            return hf
        items = data.get("rules", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for d in items:
            try:
                rule = HabitRule.from_dict(d)
                hf._rules[rule.id] = rule
            except Exception:
                continue
        # 加载完后立刻补 cue embedding（懒计算）
        hf._refresh_all_embeddings()
        return hf

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            data = {
                "version": 1,
                "saved_at": time.time(),
                "rules": [r.to_dict() for r in self._sorted_rules()],
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"⚠️ habits 落盘失败（不致命）：{e}")

    def _load_seeds(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ seed habits 加载失败：{e}")
            return
        items = data if isinstance(data, list) else data.get("rules", [])
        for d in items:
            try:
                rule = HabitRule.from_dict({**d, "source": d.get("source") or SOURCE_SYSTEM})
                self._rules[rule.id] = rule
            except Exception:
                continue
        if items:
            print(f"🧷 注入 {len(items)} 条种子规则到 HabitField。")

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._rules)

    def all(self) -> list[HabitRule]:
        return self._sorted_rules()

    def get(self, rule_id: str) -> Optional[HabitRule]:
        return self._rules.get(rule_id)

    def _sorted_rules(self) -> list[HabitRule]:
        return sorted(
            self._rules.values(),
            key=lambda r: (r.status != STATUS_ACTIVE, -r.weight, -r.updated_at),
        )

    # ------------------------------------------------------------------
    # 增删
    # ------------------------------------------------------------------
    def add_rule(self, rule_dict: dict, *, source: Optional[str] = None) -> Optional[HabitRule]:
        if not rule_dict:
            return None
        with self._lock:
            data = dict(rule_dict)
            if source:
                data.setdefault("source", source)
            try:
                rule = HabitRule.from_dict(data)
            except Exception as e:
                print(f"⚠️ 解析规则失败：{e}")
                return None
            # 重名归并：同 name 的规则视为更新
            existing = self._find_by_name(rule.name)
            if existing is not None:
                merged = self._merge(existing, rule)
                self._rules[merged.id] = merged
                self._embed_cue(merged)
                self.save()
                return merged
            self._embed_cue(rule)
            self._rules[rule.id] = rule
            self.save()
            return rule

    def _find_by_name(self, name: str) -> Optional[HabitRule]:
        norm = self._norm(name)
        for r in self._rules.values():
            if r.status == STATUS_ACTIVE and self._norm(r.name) == norm:
                return r
        return None

    @staticmethod
    def _norm(text: str) -> str:
        return "".join(ch.lower() for ch in (text or "") if not ch.isspace()).strip("。.!？?！")

    def _merge(self, old: HabitRule, new: HabitRule) -> HabitRule:
        """同名规则合并：保留累积计数，吸收新字段。"""
        merged = HabitRule(
            name=old.name,
            cue_text=new.cue_text or old.cue_text,
            cue_keywords=list({*old.cue_keywords, *new.cue_keywords}),
            forbid=list({*old.forbid, *new.forbid}),
            forbid_except=list({*old.forbid_except, *new.forbid_except}),
            require=list({*old.require, *new.require}),
            prefer=list({*old.prefer, *new.prefer}),
            rationale=new.rationale or old.rationale,
            weight=max(old.weight, new.weight),
            confidence=max(old.confidence, new.confidence),
            source=new.source or old.source,
            activation_count=old.activation_count,
            violation_count=old.violation_count,
            success_count=old.success_count,
            reinforcement_count=old.reinforcement_count,
            created_at=old.created_at,
            updated_at=time.time(),
            last_activated_at=old.last_activated_at,
            last_violated_at=old.last_violated_at,
            status=STATUS_ACTIVE,
            id=old.id,
        )
        return merged

    def archive(self, rule_id: str) -> bool:
        with self._lock:
            r = self._rules.get(rule_id)
            if r is None:
                return False
            r.status = STATUS_ARCHIVED
            r.updated_at = time.time()
            self.save()
            return True

    # ------------------------------------------------------------------
    # 嵌入
    # ------------------------------------------------------------------
    def _embed_cue(self, rule: HabitRule) -> None:
        if self.embedder is None:
            return
        text = rule.cue_text.strip() or "、".join(rule.cue_keywords[:6]) or rule.name
        if not text:
            return
        try:
            rule.cue_shape = self.embedder.embed(text).astype(np.float32)
        except Exception as e:
            print(f"⚠️ 规则 cue embedding 失败：{e}")
            rule.cue_shape = None

    def _refresh_all_embeddings(self) -> None:
        for r in self._rules.values():
            if r.cue_shape is None:
                self._embed_cue(r)

    # ------------------------------------------------------------------
    # 激活：当前场景下哪些规则该被搬到 prompt 顶部
    # ------------------------------------------------------------------
    def find_active(
        self,
        *,
        stimulus_text: str = "",
        attention_seed: Optional[np.ndarray] = None,
        self_state_text: str = "",
        recent_action_blob: str = "",
        max_active: int = 4,
    ) -> list[HabitRule]:
        """挑出当前场景下生效的规则。

        激活条件（满足任一）：
          - cue_keywords 与"当前文本"任一匹配
          - cue_shape 与 attention_seed 余弦相似度高于 cfg.habit_activation_threshold
          - cue_keywords 与 self_state.current_focus / recent_actions 匹配
          - rule.weight 已经爆表（极强习惯，全局生效）

        返回按权重排序的前 N 条 active 规则。
        """
        if not self._rules:
            return []

        active_threshold = float(getattr(self.cfg, "habit_activation_threshold", 0.42))
        always_on_weight = float(getattr(self.cfg, "habit_always_on_weight", 6.0))

        ctx_blob = " ".join([
            stimulus_text or "",
            self_state_text or "",
            recent_action_blob or "",
        ]).lower()

        scored: list[tuple[float, HabitRule]] = []
        for r in self._rules.values():
            if r.status != STATUS_ACTIVE:
                continue

            score = 0.0
            keyword_hit = False
            kws = r.keywords_lower()
            if kws:
                for kw in kws:
                    if kw and kw in ctx_blob:
                        keyword_hit = True
                        break
            if keyword_hit:
                score = 1.0

            sim = -1.0
            if attention_seed is not None and r.cue_shape is not None:
                try:
                    seed = attention_seed
                    n_seed = float(np.linalg.norm(seed))
                    n_cue = float(np.linalg.norm(r.cue_shape))
                    if n_seed > 1e-9 and n_cue > 1e-9:
                        sim = float(np.dot(seed, r.cue_shape) / (n_seed * n_cue))
                except Exception:
                    sim = -1.0
                if sim >= active_threshold:
                    score = max(score, 0.5 + 0.5 * (sim - active_threshold) / max(1e-6, 1.0 - active_threshold))

            # cue 完全为空（既无关键词也无 cue_text）→ 一直激活
            if not kws and not (r.cue_shape is not None):
                score = max(score, 0.5)

            # 极高权重的规则视为 always-on（被反复加强过的"铁律"）
            if r.weight >= always_on_weight:
                score = max(score, 0.4)

            if score > 0:
                # 用 rule.weight 把强度叠上去
                final = score * (1.0 + 0.15 * float(r.weight))
                scored.append((final, r))

        scored.sort(key=lambda x: -x[0])
        out: list[HabitRule] = []
        for _, r in scored[:max_active]:
            out.append(r)

        # 把命中的规则计数 + 1，并把 last_activated_at 更新（注意：不在这里 save，
        # 让上层在合适的时机统一落盘）
        for r in out:
            r.activation_count += 1
            r.last_activated_at = time.time()
        return out

    # ------------------------------------------------------------------
    # 反馈：违反 / 成功 / 强化 / 衰减
    # ------------------------------------------------------------------
    def violate(
        self,
        rule_id: str,
        *,
        action_content: str = "",
        matched_pattern: str = "",
    ) -> Optional[HabitRule]:
        with self._lock:
            r = self._rules.get(rule_id)
            if r is None:
                return None
            r.violation_count += 1
            r.last_violated_at = time.time()
            r.updated_at = r.last_violated_at
            # 违反后强烈加权（基底节式负反馈）
            r.boost(0.6 + 0.4 * min(r.violation_count, 5))
            r.confidence = min(1.0, r.confidence + 0.05)
            return r

    def succeed(self, rule_id: str) -> Optional[HabitRule]:
        with self._lock:
            r = self._rules.get(rule_id)
            if r is None:
                return None
            r.success_count += 1
            r.boost(0.05)
            r.confidence = min(1.0, r.confidence + 0.01)
            r.updated_at = time.time()
            return r

    def reinforce(
        self,
        rule_id: str,
        *,
        boost: float = 1.0,
        kind: str = "user_signal",
    ) -> Optional[HabitRule]:
        with self._lock:
            r = self._rules.get(rule_id)
            if r is None:
                return None
            r.reinforcement_count += 1
            r.boost(boost)
            r.confidence = min(1.0, r.confidence + 0.05)
            r.updated_at = time.time()
            return r

    def decay(self, factor: float = 0.99) -> int:
        """睡眠时调用：长期没被激活的规则缓慢衰减。"""
        n = 0
        with self._lock:
            now = time.time()
            for r in self._rules.values():
                if r.status != STATUS_ACTIVE:
                    continue
                age_days = (now - max(r.last_activated_at, r.created_at)) / 86400.0
                if age_days < 1.0:
                    continue
                old = r.weight
                r.decay(factor)
                if r.weight != old:
                    n += 1
        return n

    # ------------------------------------------------------------------
    # 反复纠正信号 → 找最贴近的现有规则
    # ------------------------------------------------------------------
    def find_match_for_signal(
        self,
        user_text: str,
        *,
        recent_text: str = "",
        attention_seed: Optional[np.ndarray] = None,
        threshold: float = 0.45,
    ) -> Optional[HabitRule]:
        """在用户发出反复纠正信号时，找出最匹配的旧规则。"""
        if not self._rules:
            return None
        # 关键词匹配优先
        ctx_blob = (user_text + " " + recent_text).lower()
        keyword_candidates: list[HabitRule] = []
        for r in self._rules.values():
            if r.status != STATUS_ACTIVE:
                continue
            for kw in r.keywords_lower():
                if kw and kw in ctx_blob:
                    keyword_candidates.append(r)
                    break
        if keyword_candidates:
            return max(keyword_candidates, key=lambda r: r.weight)

        # 没有关键词命中：尝试 embedding
        if attention_seed is None and self.embedder is not None and user_text:
            try:
                attention_seed = self.embedder.embed(user_text + " " + recent_text)
            except Exception:
                attention_seed = None

        if attention_seed is None:
            return None

        best: Optional[HabitRule] = None
        best_sim = threshold
        for r in self._rules.values():
            if r.status != STATUS_ACTIVE or r.cue_shape is None:
                continue
            n_seed = float(np.linalg.norm(attention_seed))
            n_cue = float(np.linalg.norm(r.cue_shape))
            if n_seed < 1e-9 or n_cue < 1e-9:
                continue
            sim = float(np.dot(attention_seed, r.cue_shape) / (n_seed * n_cue))
            if sim > best_sim:
                best_sim = sim
                best = r
        return best

    # ------------------------------------------------------------------
    # Prompt 渲染
    # ------------------------------------------------------------------
    def render_active_for_prompt(
        self,
        active: list[HabitRule],
        *,
        max_chars: int = 1800,
        max_rules: int = 4,
    ) -> str:
        if not active:
            return ""
        active = active[:max_rules]
        lines: list[str] = [
            "[当前生效的硬约束 / Active Habits]",
            "（这是你的程序性记忆。它们不是供你参考的笔记——它们是你下一步动作的边界。",
            "  违反任何一条都会被系统直接拦截，不会真的执行；不要试图绕过。）",
        ]
        for r in active:
            lines.append("")
            stars = "★" * min(5, max(1, int(round(min(r.weight, 5.0)))))
            tag = "self" if r.source == SOURCE_SELF else r.source
            counter = []
            if r.violation_count:
                counter.append(f"被违反 {r.violation_count} 次")
            if r.reinforcement_count:
                counter.append(f"用户强化 {r.reinforcement_count} 次")
            if r.success_count:
                counter.append(f"成功遵守 {r.success_count} 次")
            counter_text = ("；" + "；".join(counter)) if counter else ""
            lines.append(f"【{r.name}】 强度 {stars}（来源 {tag}{counter_text}）")
            if r.cue_text or r.cue_keywords:
                cue = r.cue_text or "、".join(r.cue_keywords[:8])
                lines.append(f"  触发：{cue}")
            if r.forbid:
                lines.append("  绝对禁止（动作里出现这些就被拦截）：")
                for x in r.forbid[:8]:
                    lines.append(f"    - {x}")
            if r.forbid_except:
                lines.append("  例外（同时含这些子串则放行）：")
                for x in r.forbid_except[:6]:
                    lines.append(f"    - {x}")
            if r.require:
                lines.append("  必须遵守：")
                for x in r.require[:6]:
                    lines.append(f"    - {x}")
            if r.prefer:
                lines.append("  推荐做法：")
                for x in r.prefer[:6]:
                    lines.append(f"    - {x}")
            if r.rationale:
                lines.append(f"  原因：{r.rationale}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

    def render_unanchored_signal_hint(self) -> str:
        return (
            "[未捕获的反复纠正信号]\n"
            "对方刚刚发出了反复纠正语气（比如『我说过很多次』『为什么又』『记住』），"
            "但你的程序性记忆里没有匹配的硬约束。\n"
            "—— 如果这是一条长期不变的规则，请在你的回应里写一段 <rule>...</rule>，"
            "让它进入你下一次动作选择的边界。如果只是临时情绪宣泄就不必处理。\n"
        )

    # ------------------------------------------------------------------
    # 调试 / 状态
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._rules),
                "active": sum(1 for r in self._rules.values() if r.status == STATUS_ACTIVE),
                "archived": sum(1 for r in self._rules.values() if r.status == STATUS_ARCHIVED),
                "total_violations": sum(r.violation_count for r in self._rules.values()),
                "total_reinforcements": sum(r.reinforcement_count for r in self._rules.values()),
            }


# ----------------------------------------------------------------------
# HabitGate：在 <tool> 真的被打到 VM 之前的 Go / No-Go
# ----------------------------------------------------------------------
class HabitGate:
    """基底节式动作门控。

    nova 生成的 <tool> 块进入 _chat_with_tools 的派发循环，HabitGate 在
    每次派发前扫描 active rules 的 forbid 列表。命中即拦截，向 nova 返回
    一段"你刚才差点违反规则 X"的 tool result，让她重新选下一步。
    """

    @staticmethod
    def evaluate(
        action_type: str,
        content: str,
        active_rules: list[HabitRule],
    ) -> Optional[tuple[HabitRule, str]]:
        if not active_rules or not content:
            return None
        lowered = content.lower()
        for rule in active_rules:
            for pat in rule.forbid:
                if not pat:
                    continue
                if pat.lower() in lowered:
                    # 是否被例外救回？
                    saved = False
                    for ex in rule.forbid_except:
                        if ex and ex.lower() in lowered:
                            saved = True
                            break
                    if saved:
                        continue
                    return rule, pat
        return None

    @staticmethod
    def render_block_message(rule: HabitRule, matched: str, action_type: str, content: str) -> str:
        lines: list[str] = [
            f"[抑制·习惯触发] 你刚才生成的 {action_type} 动作触发了规则「{rule.name}」的禁忌。",
            f"  匹配子串：{matched}",
        ]
        if rule.rationale:
            lines.append(f"  这条规则为什么存在：{rule.rationale}")
        if rule.prefer:
            lines.append("  你应该改成：")
            for x in rule.prefer[:5]:
                lines.append(f"    - {x}")
        if rule.require:
            lines.append("  必须遵守：")
            for x in rule.require[:5]:
                lines.append(f"    - {x}")
        # 强行抓回 nova 的注意力
        lines.append(
            f"  这条规则已经被你违反 {rule.violation_count} 次，"
            f"被用户强化 {rule.reinforcement_count} 次。"
            f"请不要再尝试同类动作；若上一步真的失败，换一种被允许的路径。"
        )
        return "\n".join(lines)
