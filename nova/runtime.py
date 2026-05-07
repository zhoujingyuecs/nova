"""ContinuousRuntime：nova 的常驻运行内核。

设计要点：
    nova 一直在跑 → Executive 选择下一步 → 主线推进 / 反思 / 睡眠 / 取向 / 走神
    用户说话只是 interrupt：打断一下，回应，然后回到原来的主线。

v1.0 简化：
- 去掉了独立的 PurposeKernel（旧 purpose.py），它的"自我取向"功能
  内联到 executive 的 build_orientation_prompt 和 SelfState 里。
  nova 不再多维护一份 purpose.json；意义"就是"她当下的 SelfState
  + 自生 agenda 的组合。
- 反思时会把最近一段 worklog 喂给 nova，让她基于事实判断进展，
  这是用户要求的"有事实依据的自我评价"。

它依赖 Nova 的稳定公共能力：
  perceive(stimulus)
  think(prompt_hint=...)        新：替代旧 dream_step，可以传主线提示
  consolidate()                 睡眠
  save()                        最终落盘
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import re
import threading
import time
import traceback
from typing import Any, Optional

from .agenda import (
    Agenda, AgendaItem,
    STATUS_ACTIVE, STATUS_BLOCKED, STATUS_DONE,
)
from .executive import (
    ExecutiveController,
    Decision,
    MODE_DREAM,
    MODE_GOAL,
    MODE_IDLE,
    MODE_INTERRUPT,
    MODE_ORIENT,
    MODE_REFLECT,
    MODE_SLEEP,
    build_goal_prompt,
    build_reflection_prompt,
    build_orientation_prompt,
)
from .worklog import WorkLog


_STATUS_RE = re.compile(r"^\s*\[STATUS\]\s*(.+?)\s*$", re.I | re.M)
_SUMMARY_RE = re.compile(r"^\s*\[SUMMARY\]\s*(.+?)\s*$", re.I | re.M)
_NEXT_RE = re.compile(r"^\s*\[NEXT\]\s*(.+?)\s*$", re.I | re.M | re.S)
_AGENDA_RE = re.compile(r"^\s*\[AGENDA\]\s*(.+?)\s*$", re.I | re.M)


@dataclass
class Interrupt:
    text: str
    source: str = "user"
    as_agenda: bool = False
    wait: bool = False
    response_queue: Optional["queue.Queue[str]"] = None
    ts: float = 0.0


@dataclass
class RuntimeState:
    running: bool
    mode: str
    current_agenda_id: str = ""
    current_agenda_title: str = ""
    last_tick_at: float = 0.0
    last_sleep_at: float = 0.0
    tick_count: int = 0
    pending_interrupts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.mode,
            "current_agenda_id": self.current_agenda_id,
            "current_agenda_title": self.current_agenda_title,
            "last_tick_at": self.last_tick_at,
            "last_sleep_at": self.last_sleep_at,
            "tick_count": self.tick_count,
            "pending_interrupts": self.pending_interrupts,
        }


class ContinuousRuntime(threading.Thread):
    """nova 的持续运行线程。"""

    def __init__(
        self,
        nova: Any,
        *,
        interval_seconds: float = 5.0,
        idle_interval_seconds: float = 30.0,
        sleep_every_seconds: float = 6 * 3600,
        save_every_ticks: int = 5,
        agenda_path: Optional[str] = None,
        worklog_path: Optional[str] = None,
        autostart_agenda: Optional[str] = None,
        initial_commission: Optional[str] = None,
        on_event: Optional[Any] = None,
    ):
        super().__init__(daemon=True, name="nova-continuous-runtime")
        self.nova = nova
        self.cfg = getattr(nova, "cfg", None)
        base_path = getattr(self.cfg, "field_path", "./field") if self.cfg else "./field"
        os.makedirs(base_path, exist_ok=True)

        self.agenda = Agenda(agenda_path or os.path.join(base_path, "agenda.json"))
        self.agenda.load()

        # 启动时可以给一个外部委托，但它不再是 nova 的存在根基。
        # 不给委托时，runtime 会进入 self_orientation。
        commission = initial_commission or autostart_agenda
        if commission:
            self.agenda.add_if_absent(
                commission,
                source="commission",
                priority=0.8,
                drive="continuity",
                next_action="把这条外部委托拆成下一步并开始推进。",
            )

        self.worklog = WorkLog(worklog_path or os.path.join(base_path, "worklog.jsonl"))
        self.executive = ExecutiveController(sleep_every_seconds=sleep_every_seconds)
        self.interval_seconds = interval_seconds
        self.idle_interval_seconds = idle_interval_seconds
        self.save_every_ticks = max(1, save_every_ticks)
        self.on_event = on_event

        self._interrupts: "queue.Queue[Interrupt]" = queue.Queue()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.RLock()
        self._state = RuntimeState(
            running=False,
            mode="init",
            last_sleep_at=time.time(),
        )

    # ---------- public API ----------
    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def submit_interrupt(
        self,
        text: str,
        *,
        source: str = "user",
        as_agenda: bool = False,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """投递一个外部打断。

        wait=True 时会阻塞等待 nova 回答，适合 CLI/page/socket；
        wait=False 时只投递，适合外部事件。"""
        response_queue: Optional[queue.Queue[str]] = queue.Queue(maxsize=1) if wait else None
        intr = Interrupt(
            text=text,
            source=source,
            as_agenda=as_agenda,
            wait=wait,
            response_queue=response_queue,
            ts=time.time(),
        )
        self._interrupts.put(intr)
        self._wake_event.set()
        if not wait:
            return None
        try:
            assert response_queue is not None
            return response_queue.get(timeout=timeout)
        except queue.Empty:
            return "（nova 正在忙，打断已收到，但这次等待超时。）"

    def add_agenda(
        self,
        title: str,
        description: str = "",
        *,
        priority: float = 0.7,
        source: str = "user",
        next_action: str = "",
    ) -> AgendaItem:
        item = self.agenda.add_if_absent(
            title,
            description,
            source=source,
            priority=priority,
            drive="continuity",
            next_action=next_action,
        )
        self.worklog.append("agenda", f"新增/更新主线：{item.title}", agenda_id=item.id)
        self._wake_event.set()
        return item

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = self._state.to_dict()
        current = self.agenda.current()
        if current:
            data["current_agenda_id"] = current.id
            data["current_agenda_title"] = current.title
        data["active_agenda"] = [i.to_dict() for i in self.agenda.active()[:10]]
        data["recent_work"] = [e.to_dict() for e in self.worklog.recent(limit=10)]
        # SelfState 也暴露出去——它是"我此刻是什么状态"的简短摘要
        ss = getattr(self.nova, "self_state", None)
        if ss is not None:
            data["self_state"] = ss.to_dict()
        task_ledger = getattr(self.nova, "task_ledger", None)
        if task_ledger is not None:
            data["task_state"] = task_ledger.to_dict()
        return data

    def status_text(self) -> str:
        state = self.status()
        current = state.get("current_agenda_title") or "（暂无主线）"
        mode = state.get("mode")
        ticks = state.get("tick_count")
        pending = state.get("pending_interrupts")
        parts = [
            f"nova runtime: mode={mode}, ticks={ticks}, pending_interrupts={pending}",
            f"当前主线：{current}",
        ]
        task_state = state.get("task_state") or {}
        active_task = task_state.get("active_task")
        if active_task:
            parts.append(f"当前用户任务：{active_task.get('goal', '')[:200]} [{active_task.get('status', '')}]")
        
        ss = state.get("self_state")
        if ss:
            parts.append("")
            parts.append("SelfState：")
            parts.append(f"  我是：{ss.get('identity', '')[:200]}")
            parts.append(f"  我此刻在做：{ss.get('current_focus', '')[:200]}")
            parts.append(f"  刚才发生：{ss.get('recent_summary', '')[:200]}")
            threads = ss.get("open_threads", []) or []
            if threads:
                parts.append("  我想回头继续的事：")
                for t in threads[:5]:
                    parts.append(f"    - {t}")
        parts.append("")
        parts.append("Agenda：")
        parts.append(self.agenda.summary_text(limit=8))
        parts.append("")
        parts.append("最近工作：")
        parts.append(self.worklog.summary_text(limit=8))
        return "\n".join(parts)

    def recent_work_text(self, *, limit: int = 12) -> str:
        return self.worklog.summary_text(limit=limit)

    # ---------- thread main ----------
    def run(self) -> None:
        with self._lock:
            self._state.running = True
            self._state.mode = "running"
        self.worklog.append("runtime", "continuous runtime started")
        self._emit("runtime_started", {})
        try:
            while not self._stop_event.is_set():
                decision = self._choose()
                self._apply_decision(decision)
                wait = (self.idle_interval_seconds
                        if decision.mode in {MODE_DREAM, MODE_IDLE}
                        else self.interval_seconds)
                self._wake_event.wait(wait)
                self._wake_event.clear()
        finally:
            self._safe_save()
            self.worklog.append("runtime", "continuous runtime stopped")
            with self._lock:
                self._state.running = False
                self._state.mode = "stopped"
            self._emit("runtime_stopped", {})

    def run_once(self) -> Decision:
        """不启动线程，手动跑一个 tick。便于测试。"""
        decision = self._choose()
        self._apply_decision(decision)
        return decision

    # ---------- decision / actions ----------
    def _choose(self) -> Decision:
        with self._lock:
            last_sleep_at = self._state.last_sleep_at
            last_tick_at = self._state.last_tick_at
        return self.executive.choose(
            agenda=self.agenda,
            has_interrupt=not self._interrupts.empty(),
            last_sleep_at=last_sleep_at,
            last_tick_at=last_tick_at,
        )

    def _apply_decision(self, decision: Decision) -> None:
        with self._lock:
            self._state.mode = decision.mode
            self._state.current_agenda_id = decision.agenda_id
            self._state.pending_interrupts = self._interrupts.qsize()
            self._state.last_tick_at = time.time()
            self._state.tick_count += 1
            tick_count = self._state.tick_count

        try:
            if decision.mode == MODE_INTERRUPT:
                self._handle_interrupt()
            elif decision.mode == MODE_GOAL:
                self._goal_step(decision.agenda_id)
            elif decision.mode == MODE_REFLECT:
                self._reflection_step(decision.agenda_id)
            elif decision.mode == MODE_SLEEP:
                self._sleep_step("executive threshold")
            elif decision.mode == MODE_ORIENT:
                self._orientation_step()
            elif decision.mode == MODE_DREAM:
                self._dream_step()
            elif decision.mode == MODE_IDLE:
                self.worklog.append("idle", decision.reason)
            else:
                self.worklog.append("runtime",
                                    f"unknown decision: {decision.mode}",
                                    detail=decision.reason)
        except Exception as e:
            detail = traceback.format_exc()
            self.worklog.append("error", f"runtime tick failed: {e}",
                                detail=detail, agenda_id=decision.agenda_id)
        finally:
            if tick_count % self.save_every_ticks == 0:
                self._safe_save()

    def _handle_interrupt(self) -> None:
        try:
            intr = self._interrupts.get_nowait()
        except queue.Empty:
            return

        task_ledger = getattr(self.nova, "task_ledger", None)
        if task_ledger is not None:
            task_ledger.observe_user_message(intr.text)
        
        if intr.as_agenda:
            item = self.agenda.add_if_absent(
                intr.text[:80],
                intr.text,
                source=intr.source,
                priority=0.8,
                drive="continuity",
                next_action="把外部打断转成可执行的下一步。",
            )
            self.worklog.append("agenda", f"外部打断进入主线：{item.title}",
                                agenda_id=item.id)

        prompt = f"""【外部打断】

你本来正在持续运行。现在有一个来自 {intr.source} 的打断。
请先回应这个打断；如果它改变了你的主线，请在心里接住这个改变。

当前 agenda：
{self.agenda.summary_text(limit=8)}

最近工作：
{self.worklog.summary_text(limit=8)}

打断内容：
{intr.text}
"""
        response = self._call_perceive(prompt)
        self.worklog.append("interrupt",
                            f"处理外部打断：{intr.text[:80]}",
                            detail=response)
        if intr.response_queue is not None:
            intr.response_queue.put(response)
        self._emit("interrupt", {"text": intr.text, "response": response})

    def _goal_step(self, agenda_id: str) -> None:
        item = self.agenda.get(agenda_id)
        if item is None:
            self.worklog.append("goal", f"agenda not found: {agenda_id}")
            return
        prompt = build_goal_prompt(
            item,
            recent_work=self.worklog.summary_text(limit=10),
            agenda_text=self.agenda.summary_text(limit=10),
        )
        # 主线推进用 think 而不是 perceive：是 nova 自己在做事，不是被外人问
        response = self._call_think(prompt)
        status, summary, next_action = self._parse_control_response(response)
        summary = summary or response[:300]

        if status == "DONE":
            self.agenda.mark_done(item.id, evidence=summary)
            kind = "done"
        elif status == "BLOCKED":
            self.agenda.mark_blocked(item.id, reason=next_action or summary)
            kind = "blocked"
        elif status == "SLEEP":
            self.agenda.record_progress(item.id, summary, next_action=next_action)
            kind = "sleep-request"
        else:
            self.agenda.record_progress(item.id, summary, next_action=next_action)
            kind = "goal"

        self.worklog.append(kind, summary, detail=response, agenda_id=item.id)
        self._emit(kind, {"agenda_id": item.id, "summary": summary, "status": status})
        if status == "SLEEP":
            self._sleep_step("requested by goal step")

    def _reflection_step(self, agenda_id: str) -> None:
        item = self.agenda.get(agenda_id)
        if item is None:
            return
        prompt = build_reflection_prompt(
            item,
            recent_work=self.worklog.summary_text(limit=12),
        )
        response = self._call_think(prompt)
        status, summary, next_action = self._parse_control_response(response)
        summary = summary or response[:300]
        if status == "DONE":
            self.agenda.mark_done(item.id, evidence=summary)
        elif status == "BLOCKED":
            self.agenda.update(item.id, status=STATUS_BLOCKED,
                               evidence=next_action or summary, failure=False)
        else:
            # 反思后把 failures 稍微降回来，给新策略机会
            item.failures = max(0, item.failures - 1)
            self.agenda.record_progress(item.id, summary, next_action=next_action)
        self.worklog.append("reflection", summary, detail=response, agenda_id=item.id)

    def _orientation_step(self) -> None:
        """没有外部主线时，nova 自己寻找下一条值得延续的线。"""
        ss = getattr(self.nova, "self_state", None)
        self_state_text = ss.render_for_prompt(max_chars=600) if ss else "（无 self_state）"
        prompt = build_orientation_prompt(
            agenda_text=self.agenda.summary_text(limit=10),
            recent_work=self.worklog.summary_text(limit=12),
            self_state_text=self_state_text,
        )
        response = self._call_think(prompt)
        agenda_title = ""
        next_action = ""
        m = _AGENDA_RE.search(response)
        if m:
            agenda_title = m.group(1).strip()
        m = _NEXT_RE.search(response)
        if m:
            next_action = m.group(1).strip().split("\n")[0].strip()

        if not agenda_title:
            self.worklog.append("orientation",
                                "自我取向没有形成清晰主线。",
                                detail=response)
            return

        item = self.agenda.add_if_absent(
            agenda_title,
            description="由 nova 在 self_orientation 中自发生成。",
            source="self",
            priority=0.65,
            drive="continuity",
            next_action=next_action,
            tags=["self_generated"],
        )
        self.worklog.append("orientation",
                            f"自我取向生成主线：{item.title}",
                            detail=response, agenda_id=item.id)
        self._emit("orientation", {"agenda_id": item.id,
                                   "agenda": item.title})

    def _sleep_step(self, reason: str) -> None:
        before = self._field_count()
        result = ""
        if hasattr(self.nova, "consolidate"):
            try:
                stats = self.nova.consolidate()
                result = str(stats)
            except Exception as e:
                result = f"consolidate 失败：{e}"
        else:
            result = "nova 没有 consolidate；跳过实际睡眠整理。"
        after = self._field_count()
        with self._lock:
            self._state.last_sleep_at = time.time()
        removed = self.worklog.compact(keep_last=500)
        self.worklog.append(
            "sleep",
            f"睡眠整理：{reason}；field {before} → {after}；worklog compact removed={removed}",
            detail=result,
        )
        self._safe_save()

    def _dream_step(self) -> None:
        if hasattr(self.nova, "think"):
            thought = self.nova.think()
        elif hasattr(self.nova, "dream_step"):
            thought = self.nova.dream_step()
        else:
            self.worklog.append("idle", "nova 不支持 think/dream_step；空闲 tick 跳过。")
            return
        self.worklog.append("dream", (thought or "（无明显念头。）")[:500],
                            detail=thought or "")

    # ---------- helpers ----------
    def _call_perceive(self, prompt: str) -> str:
        if not hasattr(self.nova, "perceive"):
            raise RuntimeError("Nova object has no perceive(stimulus) method")
        response = self.nova.perceive(prompt)
        return str(response or "").strip()

    def _call_think(self, prompt_hint: str) -> str:
        """让 nova 用 think 推进一轮——它走的是内向活动路径，
        而不是把 prompt 当成一句"用户输入"塞给 perceive。"""
        if hasattr(self.nova, "think"):
            response = self.nova.think(prompt_hint=prompt_hint)
            if response is not None:
                return str(response).strip()
        # fallback：回退到 perceive
        return self._call_perceive(prompt_hint)

    def _parse_control_response(self, response: str) -> tuple[str, str, str]:
        status_m = _STATUS_RE.search(response or "")
        summary_m = _SUMMARY_RE.search(response or "")
        next_m = _NEXT_RE.search(response or "")
        status = (status_m.group(1).strip().upper() if status_m else "CONTINUE")
        if status not in {"CONTINUE", "DONE", "BLOCKED", "SLEEP"}:
            status = "CONTINUE"
        summary = summary_m.group(1).strip() if summary_m else ""
        next_action = next_m.group(1).strip() if next_m else ""
        next_action = re.split(r"\n\s*\[[A-Z]+\]", next_action)[0].strip()
        return status, summary, next_action

    def _safe_save(self) -> None:
        try:
            self.agenda.save()
        except Exception as e:
            self.worklog.append("error", f"agenda save failed: {e}")
        try:
            if hasattr(self.nova, "save"):
                self.nova.save()
            elif hasattr(self.nova, "field"):
                from .persistence import save_field
                save_field(self.nova.field)
        except Exception as e:
            self.worklog.append("error", f"nova save failed: {e}")

    def _field_count(self) -> str:
        try:
            return str(len(getattr(self.nova, "field")))
        except Exception:
            return "?"

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, payload)
        except Exception:
            pass
