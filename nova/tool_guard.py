"""Tool loop guard for Nova v1.1.

The guard is generic. It prevents the LLM/tool loop from repeatedly trying the
same broken action or producing the same long response over and over.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import re
from typing import Iterable


_WS_RE = re.compile(r"\s+")


def normalize_signature(text: str, *, limit: int = 500) -> str:
    text = _WS_RE.sub(" ", (text or "").strip())
    return text[:limit]


def short_hash(text: str) -> str:
    return hashlib.sha1(normalize_signature(text).encode("utf-8", errors="ignore")).hexdigest()[:12]


class ToolLoopGuard:
    def __init__(
        self,
        *,
        max_same_action: int = 2,
        max_same_error: int = 2,
        max_repeated_response: int = 2,
    ):
        self.max_same_action = max(1, max_same_action)
        self.max_same_error = max(1, max_same_error)
        self.max_repeated_response = max(1, max_repeated_response)
        self.action_counts: Counter[str] = Counter()
        self.error_counts: Counter[str] = Counter()
        self.response_counts: Counter[str] = Counter()

    def check_response(self, response: str) -> tuple[bool, str]:
        sig = short_hash(response)
        self.response_counts[sig] += 1
        if self.response_counts[sig] > self.max_repeated_response:
            return False, "模型连续生成高度重复的回应，已熔断，应该停止伸手并向用户汇报当前边界。"
        return True, ""

    def check_action(self, action_type: str, content: str) -> tuple[bool, str]:
        sig = f"{action_type}:{short_hash(content)}"
        self.action_counts[sig] += 1
        if self.action_counts[sig] > self.max_same_action:
            return False, f"重复工具动作超过上限：{action_type} {normalize_signature(content, limit=120)}"
        return True, ""

    def observe_result(self, action_type: str, result: dict) -> tuple[bool, str]:
        error = str(result.get("error") or "")
        stderr = str(result.get("stderr") or "")
        rc = result.get("returncode")
        if error or (rc not in (None, 0) and stderr):
            sig = f"{action_type}:{short_hash(error or stderr)}"
            self.error_counts[sig] += 1
            if self.error_counts[sig] > self.max_same_error:
                return False, f"同类工具失败重复出现：{(error or stderr)[:180]}"
        return True, ""

    @staticmethod
    def compact_visible_text(text: str, *, max_run: int = 2) -> str:
        """Remove repeated paragraphs while preserving order."""
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
        if not paragraphs:
            return (text or "").strip()
        seen: Counter[str] = Counter()
        kept: list[str] = []
        for p in paragraphs:
            sig = short_hash(p)
            seen[sig] += 1
            if seen[sig] <= max_run:
                kept.append(p)
        return "\n\n".join(kept).strip()
