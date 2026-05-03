"""把 ContinuousRuntime 暴露给 page.py 的小桥（同机部署时可用）。

用法（在 page.py 中，创建 app 和 runtime 之后）：

    from nova.page_runtime_bridge import attach_runtime_routes
    attach_runtime_routes(app, runtime)

会新增：
    GET  /status
    GET  /worklog?limit=20
    GET  /agenda
    GET  /self_state
    POST /agenda    JSON: {"title": "...", "description": "...", "priority": 0.8}
"""
from __future__ import annotations

from typing import Any


def attach_runtime_routes(app: Any, runtime: Any) -> None:
    try:
        from flask import jsonify, request
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("attach_runtime_routes requires Flask") from exc

    @app.get("/status")
    def nova_runtime_status():  # type: ignore[unused-ignore]
        return jsonify(runtime.status())

    @app.get("/worklog")
    def nova_runtime_worklog():  # type: ignore[unused-ignore]
        limit = int(request.args.get("limit", 20))
        return jsonify([e.to_dict() for e in runtime.worklog.recent(limit=limit)])

    @app.get("/agenda")
    def nova_runtime_agenda():  # type: ignore[unused-ignore]
        return jsonify([item.to_dict() for item in runtime.agenda.all()])

    @app.get("/self_state")
    def nova_runtime_self_state():  # type: ignore[unused-ignore]
        ss = getattr(runtime.nova, "self_state", None)
        return jsonify(ss.to_dict() if ss is not None else {})

    @app.post("/agenda")
    def nova_runtime_add_agenda():  # type: ignore[unused-ignore]
        data = request.get_json(force=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400
        item = runtime.add_agenda(
            title,
            data.get("description", ""),
            priority=float(data.get("priority", 0.8)),
            source=data.get("source", "page"),
            next_action=data.get("next_action", ""),
        )
        return jsonify(item.to_dict())
