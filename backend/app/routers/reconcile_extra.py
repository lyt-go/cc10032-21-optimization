"""对账明细 / 日志说明 / 追溯的专用检索接口。

通用资源路由 /api/{module} 之外的扩展接口集中在这里；必须先于 resource 路由注册，
否则 /api/{module}/{entry_id} 的整型路径参数会拦截字符串型子资源。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services import reconcile_flow as flow
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["对账明细与日志说明"])


async def _payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _q(request: Request, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in request.query_params and str(request.query_params.get(name)).strip() != "":
            return request.query_params.get(name)
    return default


def _decision_from(data: dict[str, Any]) -> str | None:
    for key in ("decision", "choice", "处理结论", "结论", "action", "send_as", "option"):
        if data.get(key) is not None and str(data.get(key)).strip():
            return str(data.get(key))
    return None


# ===========================================================================
# 对账执行 / 运行
# ===========================================================================
@router.post("/data_reconcile/{entry_id}/execute")
@router.post("/data_reconcile/{entry_id}/rerun")
async def execute_reconcile(entry_id: int) -> dict[str, Any]:
    service = service_for("data_reconcile")
    assert service is not None
    return service.execute_reconcile(entry_id)


@router.post("/reconcile/tasks/{entry_id}/execute")
async def execute_reconcile_alias(entry_id: int) -> dict[str, Any]:
    service = service_for("data_reconcile")
    assert service is not None
    return service.execute_reconcile(entry_id)


@router.get("/data_reconcile/runs")
@router.get("/reconcile/runs")
async def list_runs(request: Request) -> dict[str, Any]:
    task_id = _q(request, "task_id", "entry_id", "任务")
    runs = flow.list_runs(task_id)
    return {"ok": True, "items": runs, "total": len(runs)}


@router.get("/data_reconcile/{entry_id}/runs")
async def list_task_runs(entry_id: int) -> dict[str, Any]:
    runs = flow.list_runs(entry_id)
    return {"ok": True, "items": runs, "total": len(runs)}


@router.get("/data_reconcile/runs/{run_ref}")
@router.get("/reconcile/runs/{run_ref}")
async def get_run(run_ref: str) -> dict[str, Any]:
    run = flow.get_run(run_ref)
    if run is None:
        return {"ok": False, "reason": f"对账运行 {run_ref} 不存在"}
    return {"ok": True, "run": run, "item": run}


# ===========================================================================
# 明细分页检索（稳定分页）
# ===========================================================================
@router.get("/data_reconcile/details")
@router.get("/data_reconcile/diffs")
@router.get("/reconcile/details")
@router.get("/reconcile/diffs")
async def list_details(request: Request) -> dict[str, Any]:
    return flow.list_details(
        run_ref=_q(request, "run_no", "run_ref", "run", "运行编号"),
        task_id=_q(request, "task_id", "entry_id", "任务编号"),
        page=int(_q(request, "page", default=1) or 1),
        size=int(_q(request, "size", "page_size", default=20) or 20),
        diff_type=_q(request, "diff_type", "type", "差异类型", "kind"),
        page_token=_q(request, "page_token", "token", "snapshot"),
    )


@router.get("/data_reconcile/{entry_id}/details")
@router.get("/data_reconcile/{entry_id}/diffs")
async def list_task_details(entry_id: int, request: Request) -> dict[str, Any]:
    run_ref = _q(request, "run_no", "run_ref", "run", "运行编号")
    return flow.list_details(
        run_ref=run_ref,
        task_id=None if run_ref else entry_id,
        page=int(_q(request, "page", default=1) or 1),
        size=int(_q(request, "size", "page_size", default=20) or 20),
        diff_type=_q(request, "diff_type", "type", "差异类型", "kind"),
        page_token=_q(request, "page_token", "token", "snapshot"),
    )


@router.get("/data_reconcile/runs/{run_ref}/details")
@router.get("/reconcile/runs/{run_ref}/details")
async def list_run_details(run_ref: str, request: Request) -> dict[str, Any]:
    return flow.list_details(
        run_ref=run_ref,
        page=int(_q(request, "page", default=1) or 1),
        size=int(_q(request, "size", "page_size", default=20) or 20),
        diff_type=_q(request, "diff_type", "type", "差异类型", "kind"),
        page_token=_q(request, "page_token", "token", "snapshot"),
    )


@router.get("/reconcile/details/{detail_ref}")
@router.get("/data_reconcile/details/{detail_ref}/view")
async def get_detail(detail_ref: str) -> dict[str, Any]:
    detail = store.find_reconcile_detail(detail_ref)
    if detail is None:
        return {"ok": False, "reason": f"对账明细 {detail_ref} 不存在"}
    note = next((n for n in store.log_notes
                 if int(n.get("detail_id", 0)) == int(detail["id"])
                 and n.get("status") == "待发送"), None)
    return {"ok": True, "item": detail, "detail": detail, "draft_note": note}


@router.get("/reconcile/filter-counts")
async def filter_counts(request: Request) -> dict[str, Any]:
    run_ref = _q(request, "run_no", "run_ref", "run", "运行编号")
    run = flow.get_run(run_ref) if run_ref else store.latest_reconcile_run()
    counts = (run or {}).get("type_counts", {})
    return {
        "ok": True,
        "run_no": (run or {}).get("run_no"),
        "filter_counts": counts,
        "diff_type_filters": [
            {"key": "金额差异", "count": int(counts.get("金额差异", 0))},
            {"key": "状态差异", "count": int(counts.get("状态差异", 0))},
        ],
        "decision_counts": flow.decision_counts(),
    }


@router.get("/reconcile/next-range")
async def next_range(request: Request) -> dict[str, Any]:
    after = _q(request, "after_detail_id", "detail_id", "after", "明细")
    return flow.next_send_range(
        _q(request, "run_no", "run_ref", "run", "运行编号"),
        after_detail_id=int(after) if str(after or "").isdigit() else None,
        page=int(_q(request, "page", default=0) or 0) or None,
        size=int(_q(request, "size", default=10) or 10),
    )


# ===========================================================================
# 日志说明：打开草稿 / 列表 / 提交
# ===========================================================================
@router.post("/reconcile/details/{detail_ref}/note")
@router.post("/data_reconcile/details/{detail_ref}/note")
async def open_detail_note(detail_ref: str, request: Request) -> dict[str, Any]:
    data = await _payload(request)
    content = data.get("content") or data.get("日志说明") or data.get("说明内容")
    try:
        note = flow.open_note(detail_ref, content, operator=str(data.get("operator", "值班员")))
    except KeyError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "note": note, "item": note}


@router.get("/reconcile/notes")
@router.get("/changelog/notes")
async def list_notes(request: Request) -> dict[str, Any]:
    notes = flow.list_notes(
        run_ref=_q(request, "run_no", "run", "运行编号"),
        detail_ref=_q(request, "detail_id", "detail_no", "明细编号"),
        status=_q(request, "status", "send_status", "发送状态"),
        decision=_q(request, "decision", "处理结论"),
    )
    return {"ok": True, "items": notes, "total": len(notes),
            "decision_counts": flow.decision_counts()}


@router.get("/reconcile/notes/{note_ref}")
@router.get("/changelog/notes/{note_ref}")
async def get_note(note_ref: str) -> dict[str, Any]:
    note = store.find_log_note(note_ref)
    if note is None:
        return {"ok": False, "reason": f"日志说明 {note_ref} 不存在"}
    return {"ok": True, "note": note, "item": note}


@router.post("/reconcile/notes/{note_ref}/submit")
@router.post("/reconcile/notes/{note_ref}/send")
@router.post("/changelog/notes/{note_ref}/submit")
@router.post("/changelog/notes/{note_ref}/send")
async def submit_note(note_ref: str, request: Request) -> dict[str, Any]:
    data = await _payload(request)
    return flow.submit_note(
        note_ref,
        content=data.get("content") or data.get("日志说明") or data.get("说明内容"),
        decision=_decision_from(data),
        operator=data.get("operator"),
        page=data.get("page"),
        size=int(data.get("size", 10) or 10),
    )


@router.post("/reconcile/details/{detail_ref}/submit")
@router.post("/data_reconcile/details/{detail_ref}/submit-note")
async def submit_detail_note(detail_ref: str, request: Request) -> dict[str, Any]:
    """一步完成「打开/沿用草稿 → 提交说明」，便于从某条差异直接处置。"""
    data = await _payload(request)
    content = data.get("content") or data.get("日志说明") or data.get("说明内容")
    try:
        note = flow.open_note(detail_ref, content)
    except KeyError as exc:
        return {"ok": False, "reason": str(exc)}
    return flow.submit_note(
        note["id"],
        content=content,
        decision=_decision_from(data),
        operator=data.get("operator"),
        page=data.get("page"),
        size=int(data.get("size", 10) or 10),
    )


# ===========================================================================
# changelog 经运行编号追溯
# ===========================================================================
@router.get("/changelog/trace")
@router.get("/reconcile/trace")
async def trace(request: Request) -> dict[str, Any]:
    return flow.changelog_trace(
        run_ref=_q(request, "run_no", "run", "运行编号"),
        detail_ref=_q(request, "detail_no", "detail_id", "明细编号"),
    )


@router.get("/changelog/trace/{run_ref}")
@router.get("/changelog/by-run/{run_ref}")
async def trace_by_run_path(run_ref: str) -> dict[str, Any]:
    return flow.changelog_trace(run_ref=run_ref)


@router.get("/changelog/filter-counts")
async def changelog_filter_counts() -> dict[str, Any]:
    service = service_for("changelog")
    assert service is not None
    return {"ok": True, **service.filter_counts()}


# ===========================================================================
# 验收造数 / 纠正工具
# ===========================================================================
@router.post("/reconcile/setup-acceptance")
@router.post("/data_reconcile/setup-acceptance")
async def setup_acceptance(request: Request) -> dict[str, Any]:
    data = await _payload(request)
    total = int(data.get("total", request.query_params.get("total", 23)) or 23)
    amount_count = int(data.get("amount_count", request.query_params.get("amount_count", 12)) or 12)
    return flow.setup_acceptance_case(total=total, amount_count=amount_count)


@router.post("/reconcile/correct")
@router.post("/data_reconcile/correct")
async def correct_row(request: Request) -> dict[str, Any]:
    data = await _payload(request)
    return flow.correct_value(
        biz_key=data.get("biz_key") or data.get("业务键"),
        detail_ref=data.get("detail_id") or data.get("detail_no") or data.get("明细编号"),
        field_name=data.get("field") or data.get("field_name") or data.get("字段"),
        rerun=bool(data.get("rerun", data.get("重跑", False))),
    )
