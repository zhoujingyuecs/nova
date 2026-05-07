"""Task, evidence, and internalization state for Nova v1.1.

This module is deliberately task-agnostic.  It does not know about any
particular benchmark or test task.  Its job is to keep Nova honest about:

- what the current user task is;
- what would count as completion;
- which claims are supported by which evidence;
- what may be written as a durable lesson.

The design follows the RSVI loop:
Reach -> Search -> Verify -> Internalize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
import uuid
from typing import Any, Optional


STATUS_ACTIVE = "active"
STATUS_RESEARCHING = "researching"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

EVIDENCE_USER_STATEMENT = "user_statement"
EVIDENCE_TOOL_OBSERVATION = "tool_observation"
EVIDENCE_WEB_CLAIM = "web_claim"
EVIDENCE_LOCAL_FILE_CLAIM = "local_file_claim"
EVIDENCE_MODEL_ADVICE = "model_advice"
EVIDENCE_HYPOTHESIS = "hypothesis"
EVIDENCE_EXTERNAL_VERIFIED = "external_verified_result"

TRUST_UNVERIFIED = "unverified"
TRUST_OBSERVED = "observed"
TRUST_VERIFIED = "verified"
TRUST_CONTRADICTED = "contradicted"

INTERNALIZE_RAW = "raw_observation"
INTERNALIZE_HYPOTHESIS = "hypothesis"
INTERNALIZE_CANDIDATE = "candidate_strategy"
INTERNALIZE_BLOCKED = "blocked_error"
INTERNALIZE_VERIFIED = "verified_lesson"


TASK_SYSTEM_ADDITION = """
——
关于用户任务、证据和内化。

外部用户给出的明确目标，是当前最高优先级任务。只要任务没有完成、失败、阻塞或被用户取消，
不要让走神、梦境、自我修复、旧主线把它抢走。内部主线可以存在，但不能覆盖当前用户目标。

你必须区分：
* 用户说的话：user_statement，只说明用户这样说了。
* 文件内容：local_file_claim，只说明文件里这样写了。
* 网页内容：web_claim，只说明网页返回了这样的文本。
* shell/python/web 的返回：tool_observation，只说明这次动作的结果。
* 其他模型或建议源的话：model_advice，默认未验证。
* 自己冒出来的想法：hypothesis，不是事实。
* 外部世界已确认结果：external_verified_result，才算强证据。

任务完成必须满足 success_condition。没有满足时，只能说“候选、进展、未确认、被阻塞”，不能说完成。
写 notes 可以记录原始观察，但只有经过验证的经验才可以当成 verified_lesson 影响以后判断。
""".strip()


@dataclass
class Evidence:
    claim: str
    source_type: str = EVIDENCE_HYPOTHESIS
    trust: str = TRUST_UNVERIFIED
    source: str = ""
    ref: str = ""
    notes: str = ""
    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim": self.claim,
            "source_type": self.source_type,
            "trust": self.trust,
            "source": self.source,
            "ref": self.ref,
            "notes": self.notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            claim=str(data.get("claim") or ""),
            source_type=str(data.get("source_type") or EVIDENCE_HYPOTHESIS),
            trust=str(data.get("trust") or TRUST_UNVERIFIED),
            source=str(data.get("source") or ""),
            ref=str(data.get("ref") or ""),
            notes=str(data.get("notes") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )


@dataclass
class TaskState:
    goal: str = ""
    status: str = STATUS_ACTIVE
    success_condition: str = ""
    constraints: list[str] = field(default_factory=list)
    next_action: str = ""
    blockers: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "success_condition": self.success_condition,
            "constraints": list(self.constraints),
            "next_action": self.next_action,
            "blockers": list(self.blockers),
            "evidence_ids": list(self.evidence_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            goal=str(data.get("goal") or ""),
            status=str(data.get("status") or STATUS_ACTIVE),
            success_condition=str(data.get("success_condition") or ""),
            constraints=list(data.get("constraints") or []),
            next_action=str(data.get("next_action") or ""),
            blockers=list(data.get("blockers") or []),
            evidence_ids=list(data.get("evidence_ids") or []),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class TaskLedger:
    """A small persistent ledger for the current user task and its evidence."""

    def __init__(self, path: str):
        self.path = path
        self.active_task: Optional[TaskState] = None
        self.evidence: dict[str, Evidence] = {}
        self.internalization_log: list[dict[str, Any]] = []

    @classmethod
    def load(cls, path: str) -> "TaskLedger":
        ledger = cls(path)
        if not path or not os.path.exists(path):
            return ledger
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return ledger
        task_data = data.get("active_task")
        if task_data:
            ledger.active_task = TaskState.from_dict(task_data)
        for item in data.get("evidence", []) or []:
            ev = Evidence.from_dict(item)
            ledger.evidence[ev.id] = ev
        ledger.internalization_log = list(data.get("internalization_log") or [])
        return ledger

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "active_task": self.active_task.to_dict() if self.active_task else None,
            "evidence": [ev.to_dict() for ev in self.evidence.values()],
            "internalization_log": self.internalization_log[-200:],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def observe_user_message(self, text: str) -> None:
        """Create/update active task when the user gives a clear request.

        This intentionally uses conservative heuristics. The LLM still sees the
        task block and can refine the task in natural language, but this keeps a
        durable anchor so interrupts are not swallowed by internal agenda.
        """
        text = (text or "").strip()
        if not text:
            return
        ev = self.add_evidence(
            claim=text[:800],
            source_type=EVIDENCE_USER_STATEMENT,
            trust=TRUST_OBSERVED,
            source="user",
        )
        lowered = text.lower()
        cancel_markers = ["取消", "不用了", "算了", "stop", "cancel"]
        if self.active_task and any(m in lowered for m in cancel_markers):
            self.active_task.status = STATUS_CANCELLED
            self.active_task.next_action = "等待用户给出新的目标。"
            self.active_task.touch()
            self.save()
            return
        request_markers = [
            "帮我", "查", "看看", "改", "写", "整理", "分析", "修", "做", "想想", "给我",
            "please", "can you", "could you", "help me", "find", "search", "fix", "write",
        ]
        is_request = any(m in text for m in request_markers) or text.endswith("?") or text.endswith("？")
        if is_request:
            if self.active_task is None or self.active_task.status in {STATUS_DONE, STATUS_CANCELLED}:
                self.active_task = TaskState(
                    goal=text[:240],
                    status=STATUS_ACTIVE,
                    success_condition="用用户能确认的方式完成这次请求；不确定时说明证据边界。",
                    constraints=["合法", "不伪造证据", "不把未验证内容说成事实"],
                    next_action="澄清目标、获取证据或执行下一步。",
                    evidence_ids=[ev.id],
                )
            else:
                self.active_task.goal = text[:240]
                self.active_task.status = STATUS_ACTIVE
                if ev.id not in self.active_task.evidence_ids:
                    self.active_task.evidence_ids.append(ev.id)
                self.active_task.next_action = "优先处理用户刚刚更新的目标。"
                self.active_task.touch()
            self.save()

    def add_evidence(
        self,
        *,
        claim: str,
        source_type: str,
        trust: str = TRUST_UNVERIFIED,
        source: str = "",
        ref: str = "",
        notes: str = "",
    ) -> Evidence:
        ev = Evidence(
            claim=(claim or "").strip(),
            source_type=source_type,
            trust=trust,
            source=source,
            ref=ref,
            notes=notes,
        )
        self.evidence[ev.id] = ev
        if self.active_task and ev.id not in self.active_task.evidence_ids:
            self.active_task.evidence_ids.append(ev.id)
            self.active_task.touch()
        return ev

    def record_tool_result(self, action_type: str, action_input: str, result: dict[str, Any]) -> Evidence:
        if result.get("error"):
            claim = f"{action_type} 动作失败：{result.get('error')}"
            trust = TRUST_OBSERVED
        elif action_type == "web":
            text = str(result.get("text") or "")
            claim = f"web 返回了内容：{text[:500]}" if text else "web 动作成功但没有可读内容。"
            trust = TRUST_OBSERVED
        elif action_type in {"shell", "python"}:
            stdout = str(result.get("stdout") or "")
            stderr = str(result.get("stderr") or "")
            rc = result.get("returncode")
            claim = f"{action_type} 返回码={rc}；stdout={stdout[:300]}；stderr={stderr[:300]}"
            trust = TRUST_OBSERVED
        else:
            claim = f"工具 {action_type} 返回：{str(result)[:500]}"
            trust = TRUST_OBSERVED
        return self.add_evidence(
            claim=claim,
            source_type=EVIDENCE_TOOL_OBSERVATION,
            trust=trust,
            source=action_type,
            ref=action_input[:200],
        )

    def set_blocked(self, reason: str) -> None:
        if not self.active_task:
            return
        self.active_task.status = STATUS_BLOCKED
        if reason and reason not in self.active_task.blockers:
            self.active_task.blockers.append(reason)
        self.active_task.next_action = "向用户说明阻塞原因，或换一条可验证路径。"
        self.active_task.touch()
        self.save()

    def mark_done(self, evidence_summary: str = "") -> bool:
        """Mark done only when there is at least one verified external evidence.

        The caller can still leave the task active if completion is uncertain.
        """
        if not self.active_task:
            return False
        ids = set(self.active_task.evidence_ids)
        strong = [
            ev for ev in self.evidence.values()
            if ev.id in ids and ev.source_type == EVIDENCE_EXTERNAL_VERIFIED and ev.trust == TRUST_VERIFIED
        ]
        if not strong:
            return False
        self.active_task.status = STATUS_DONE
        self.active_task.next_action = "任务已用强证据确认完成。"
        if evidence_summary:
            self.add_evidence(
                claim=evidence_summary,
                source_type=EVIDENCE_EXTERNAL_VERIFIED,
                trust=TRUST_VERIFIED,
                source="completion",
            )
        self.active_task.touch()
        self.save()
        return True

    def request_internalization(self, *, text: str, kind: str, evidence_ids: Optional[list[str]] = None) -> bool:
        """Return whether a note is safe to treat as durable lesson.

        Raw notes may always be written to workspace, but only verified lessons
        should influence future behavior.
        """
        evidence_ids = evidence_ids or []
        allowed = kind == INTERNALIZE_VERIFIED and any(
            (self.evidence.get(eid) and self.evidence[eid].trust == TRUST_VERIFIED)
            for eid in evidence_ids
        )
        self.internalization_log.append({
            "ts": time.time(),
            "kind": kind,
            "allowed_as_durable_lesson": allowed,
            "evidence_ids": evidence_ids,
            "text": text[:500],
        })
        self.save()
        return allowed

    def render_for_prompt(self, max_evidence: int = 6) -> str:
        lines: list[str] = []
        lines.append("[当前用户任务 / TaskState]")
        if not self.active_task:
            lines.append("（暂无明确 active_user_task。若用户给出目标，先接住目标再行动。）")
        else:
            t = self.active_task
            lines.append(f"goal: {t.goal}")
            lines.append(f"status: {t.status}")
            lines.append(f"success_condition: {t.success_condition}")
            if t.constraints:
                lines.append("constraints: " + "；".join(t.constraints[:6]))
            if t.next_action:
                lines.append(f"next_action: {t.next_action}")
            if t.blockers:
                lines.append("blockers: " + "；".join(t.blockers[-4:]))
            evs = [self.evidence[eid] for eid in t.evidence_ids if eid in self.evidence][-max_evidence:]
            if evs:
                lines.append("evidence:")
                for ev in evs:
                    lines.append(f"- [{ev.source_type}/{ev.trust}] {ev.claim[:220]}")
        lines.append("")
        lines.append("规则：没满足 success_condition 就不要说完成；model_advice/web_claim/local_file_claim 都不是最终事实；可写 raw_observation，但 verified_lesson 需要强证据。")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_task": self.active_task.to_dict() if self.active_task else None,
            "evidence_count": len(self.evidence),
            "recent_evidence": [ev.to_dict() for ev in list(self.evidence.values())[-10:]],
        }
