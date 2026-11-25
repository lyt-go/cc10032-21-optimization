"""对账明细 → 变更日志说明 的核心检索/流转逻辑。

设计要点：
1. 每次对账执行生成一个不可变的「对账运行」(run)，运行下保存逐条「对账明细」：
   差异类型、业务键、左侧值、右侧值、明细编号。
2. 明细分页通过 page_token 固化 id 序列；旧运行本身也不可变，因此新一轮对账
   的并发写入不会影响已经签发的分页。
3. 对账结果回写 changelog 模块清单（含运行编号），日志可经运行编号反查原明细。
4. 日志说明（note）引用的是签发时的旧差异快照；提交前若最新对账已把该业务键
   判为一致，必须选择「继续引用旧差异」或「因更正撤回」。
5. 没有任何运行记录的历史对账任务，回退读取任务行上的历史「对账结论」，
   检索口径与历史结论兼容。
"""
from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from app.store import store

FLOW_LOCK = threading.RLock()

# 内部流转字段，不参与业务字段对账
INTERNAL_FIELDS = {"id", "status", "pending", "abnormal", "_ts"}

AMOUNT_HINTS = ("金额", "余额", "amount", "amt", "money", "price", "费用", "价款", "总额")
STATUS_HINTS = ("状态", "status", "state", "阶段", "进度")

CITE_OLD = "继续引用旧差异"
WITHDRAW = "因更正撤回"
DECISION_CHOICES = (CITE_OLD, WITHDRAW)

_CITE_ALIASES = {
    "继续引用旧差异", "引用旧差异", "继续引用", "引用", "cite_old", "cite",
    "old", "keep", "continue", "continue_old",
}
_WITHDRAW_ALIASES = {
    "因更正撤回", "更正撤回", "撤回", "撤销", "withdraw", "recall", "cancel",
    "corrected", "correction",
}


# ---------------------------------------------------------------------------
# 差异分类 / 值比较
# ---------------------------------------------------------------------------
def classify_diff_type(field: Any) -> str:
    """把字段名归一到「金额差异 / 状态差异」等差异类型。"""
    name = str(field or "")
    lowered = name.lower()
    if any(hint.lower() in lowered for hint in AMOUNT_HINTS):
        return "金额差异"
    if any(hint.lower() in lowered for hint in STATUS_HINTS):
        return "状态差异"
    return f"{name}差异" if name else "值差异"


def diff_kind(diff_type: str) -> str:
    if diff_type == "金额差异":
        return "amount"
    if diff_type == "状态差异":
        return "status"
    if diff_type == "仅左侧存在":
        return "only_left"
    if diff_type == "仅右侧存在":
        return "only_right"
    return "value"


def canonical_diff_type(query: str | None) -> str | None:
    if not query:
        return None
    q = str(query).strip()
    lowered = q.lower()
    if lowered in {"amount", "money", "金额"} or "金额" in q:
        return "金额差异"
    if lowered in {"status", "state", "状态"} or "状态" in q:
        return "状态差异"
    if "仅左" in q or "only_left" in lowered:
        return "仅左侧存在"
    if "仅右" in q or "only_right" in lowered:
        return "仅右侧存在"
    return q


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def values_equal(left: Any, right: Any) -> bool:
    """比较两个侧值：数字按数值比（100 与 "100.00" 视为一致），其余按去空白文本比。"""
    if left is None and right is None:
        return True
    ld, rd = _as_decimal(left), _as_decimal(right)
    if ld is not None and rd is not None:
        return ld == rd
    return str(left if left is not None else "").strip() == str(right if right is not None else "").strip()


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# changelog 清单回写
# ---------------------------------------------------------------------------
def add_changelog_entry(values: dict[str, Any]) -> dict[str, Any]:
    """在 changelog 模块清单中追加一行（/api/changelog 可见）。"""
    with FLOW_LOCK:
        rows = store.rows("changelog")
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry: dict[str, Any] = {
            "id": new_id,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": f"CHAN-{new_id:04d}",
            "目标表": "",
            "变更类型": "对账差异",
            "变更内容": "",
            "操作时间": _now_text(),
            "操作来源": "data_reconcile",
            "影响行数": "1",
            "回滚版本": "",
            # 对账追溯扩展列
            "运行编号": "",
            "明细编号": "",
            "业务键": "",
            "差异类型": "",
            "左侧值": "",
            "右侧值": "",
            "处理结论": "",
            "发送状态": "已发送",
            "记录类别": "对账回写",
            "note_id": None,
        }
        entry.update(values)
        rows.append(entry)
        return entry


def create_draft_note(detail: dict[str, Any], *, with_changelog_row: bool = False) -> dict[str, Any]:
    """为一条明细创建「待发送」日志说明，并可同步在 changelog 清单放一行草稿。"""
    with FLOW_LOCK:
        # 幂等：同明细已有未终结说明时直接返回
        for note in store.log_notes:
            if int(note.get("detail_id", 0)) == int(detail["id"]) and note.get("status") == "待发送":
                return note

        note_id = store.next_note_id()
        note_no = f"NOTE-{note_id:06d}"
        default_content = (
            f"【对账差异说明】运行 {detail['run_no']} 明细 {detail['detail_no']}："
            f"业务键 {detail['业务键']} 的{detail['差异类型']}（字段 {detail.get('field', '')}），"
            f"左侧值「{detail['左侧值']}」，右侧值「{detail['右侧值']}」，请核实处理。"
        )
        note: dict[str, Any] = {
            "id": note_id,
            "note_id": note_id,
            "note_no": note_no,
            "说明编号": note_no,
            "detail_id": detail["id"],
            "detail_no": detail["detail_no"],
            "明细编号": detail["detail_no"],
            "run_id": detail.get("run_id"),
            "run_no": detail["run_no"],
            "运行编号": detail["run_no"],
            "task_id": detail.get("task_id"),
            "任务编号": detail.get("任务编号", ""),
            "seq": detail.get("seq"),
            "diff_type": detail["差异类型"],
            "差异类型": detail["差异类型"],
            "field": detail.get("field", ""),
            "biz_key": detail["业务键"],
            "业务键": detail["业务键"],
            "left_value": detail["left_value"],
            "right_value": detail["right_value"],
            "左侧值": detail["左侧值"],
            "右侧值": detail["右侧值"],
            "old_detail": {
                "run_no": detail["run_no"],
                "detail_no": detail["detail_no"],
                "diff_type": detail["差异类型"],
                "biz_key": detail["业务键"],
                "left_value": detail["left_value"],
                "right_value": detail["right_value"],
            },
            "content": default_content,
            "说明内容": default_content,
            "日志说明": default_content,
            "operator": "值班员",
            "status": "待发送",
            "发送状态": "待发送",
            "decision": "",
            "处理结论": "",
            "created_at": _now_text(),
            "sent_at": None,
            "target_changelog_id": None,
            "target_changelog_no": "",
            "changelog_id": None,
        }

        if with_changelog_row:
            row = add_changelog_entry({
                "目标表": "",
                "变更类型": "对账差异",
                "变更内容": default_content,
                "操作来源": "值班员日志说明草稿",
                "运行编号": detail["run_no"],
                "明细编号": detail["detail_no"],
                "业务键": detail["业务键"],
                "差异类型": detail["差异类型"],
                "左侧值": detail["左侧值"],
                "右侧值": detail["右侧值"],
                "处理结论": "",
                "发送状态": "待发送",
                "记录类别": "日志说明草稿",
                "note_id": note_id,
                "pending": True,
            })
            note["changelog_id"] = row["id"]
            note["target_changelog_id"] = row["id"]
            note["target_changelog_no"] = row["记录编号"]

        note["stale_check"] = current_key_state(note)
        store.log_notes.append(note)
        _sync_detail_note_status(detail["id"], "待发送")
        return note


# ---------------------------------------------------------------------------
# 执行对账 → 保存运行 + 明细 + 回写 changelog
# ---------------------------------------------------------------------------
def execute_reconcile(entry: dict[str, Any]) -> dict[str, Any]:
    with FLOW_LOCK:
        entry_id = int(entry.get("id", 0))
        left_table = str(entry.get("左源表", ""))
        right_table = str(entry.get("右源表", ""))
        reconcile_field = str(entry.get("对账字段", "id") or "id")
        try:
            threshold = float(str(entry.get("差异阈值", "5")))
        except (TypeError, ValueError):
            threshold = 5.0

        left_rows = store.rows(left_table) if left_table in store._tables else []
        right_rows = store.rows(right_table) if right_table in store._tables else []

        run_id = len(store.reconcile_runs) + 1
        run_no = f"RUN-{run_id:04d}"
        ts_text = _now_text()

        if not left_rows:
            # 失败也留一条运行头，便于检索口径一致
            run = {
                "id": run_id, "run_id": run_id, "run_no": run_no, "运行编号": run_no,
                "task_id": entry_id, "任务编号": entry.get("任务编号", ""),
                "left_table": left_table, "right_table": right_table,
                "reconcile_field": reconcile_field, "threshold": threshold,
                "diff_count": 0, "total": 0, "passed": False,
                "conclusion": "执行失败", "对账结论": "执行失败", "status": "执行失败",
                "created_at": ts_text, "执行时间": ts_text, "ts": time.time(),
                "type_counts": {}, "diff_type_counts": {},
                "error": f"左源表 {left_table} 无数据",
            }
            store.save_reconcile_run(run)
            return {"ok": False, "reason": f"左源表 {left_table} 无数据", "run_no": run_no, "run_id": run_id}

        left_map = {r.get(reconcile_field): r for r in left_rows if r.get(reconcile_field) is not None}
        right_map = {r.get(reconcile_field): r for r in right_rows if r.get(reconcile_field) is not None}

        only_left_keys = [k for k in left_map if k not in right_map]
        only_right_keys = [k for k in right_map if k not in left_map]

        details: list[dict[str, Any]] = []
        mismatch_fields = 0
        detail_pk_cursor = store.next_detail_seq() - 1

        def _next_detail(biz_key: Any, diff_type: str, field_name: str,
                         lv: Any, rv: Any, left_row: dict | None, right_row: dict | None) -> None:
            nonlocal mismatch_fields, detail_pk_cursor
            detail_pk_cursor += 1
            detail_pk = detail_pk_cursor
            seq = len(details) + 1
            detail_no = f"MX-{detail_pk:06d}"
            if diff_type not in ("仅左侧存在", "仅右侧存在"):
                mismatch_fields += 1
            detail = {
                "id": detail_pk,
                "detail_id": detail_pk,
                "detail_no": detail_no,
                "明细编号": detail_no,
                "run_id": run_id,
                "run_no": run_no,
                "运行编号": run_no,
                "task_id": entry_id,
                "任务编号": entry.get("任务编号", ""),
                "seq": seq,
                "diff_type": diff_type,
                "差异类型": diff_type,
                "diff_kind": diff_kind(diff_type),
                "field": field_name,
                "对账字段名": field_name,
                "biz_key": _display(biz_key),
                "business_key": _display(biz_key),
                "key": _display(biz_key),
                "业务键": _display(biz_key),
                "left_value": _display(lv),
                "right_value": _display(rv),
                "left_val": _display(lv),
                "right_val": _display(rv),
                "left": _display(lv),
                "right": _display(rv),
                "左侧值": _display(lv),
                "右侧值": _display(rv),
                "左值": _display(lv),
                "右值": _display(rv),
                "type": diff_type,
                "category": diff_type,
                "left_row": left_row,
                "right_row": right_row,
                "note_status": "未发送",
                "发送状态": "未发送",
            }
            details.append(detail)

        for key in only_left_keys:
            _next_detail(key, "仅左侧存在", reconcile_field,
                         left_map[key].get(reconcile_field, key), None, left_map[key], None)
        for key in only_right_keys:
            _next_detail(key, "仅右侧存在", reconcile_field,
                         None, right_map[key].get(reconcile_field, key), None, right_map[key])

        for key, lrow in left_map.items():
            rrow = right_map.get(key)
            if rrow is None:
                continue
            common_fields = [
                f for f in lrow.keys()
                if f in rrow and f not in INTERNAL_FIELDS and not str(f).startswith("_")
            ]
            # 右表独有字段也纳入比较口径之外（无法判断左侧值），保持与旧逻辑一致：
            # 仅比较两侧共有字段。
            for f in common_fields:
                lv, rv = lrow.get(f), rrow.get(f)
                if not values_equal(lv, rv):
                    _next_detail(key, classify_diff_type(f), f, lv, rv, lrow, rrow)

        diff_count = len(details)
        passed = diff_count <= threshold
        conclusion = "一致" if passed else "存在差异"
        type_counts: dict[str, int] = {}
        for d in details:
            type_counts[d["差异类型"]] = type_counts.get(d["差异类型"], 0) + 1

        run = {
            "id": run_id,
            "run_id": run_id,
            "run_no": run_no,
            "run_number": run_no,
            "batch_no": run_no,
            "reconcile_no": run_no,
            "运行编号": run_no,
            "task_id": entry_id,
            "任务编号": entry.get("任务编号", ""),
            "left_table": left_table,
            "right_table": right_table,
            "reconcile_field": reconcile_field,
            "threshold": threshold,
            "diff_count": diff_count,
            "total": diff_count,
            "only_left": len(only_left_keys),
            "only_right": len(only_right_keys),
            "mismatches": mismatch_fields,
            "passed": passed,
            "conclusion": conclusion,
            "对账结论": conclusion,
            "status": conclusion,
            "created_at": ts_text,
            "执行时间": ts_text,
            "ts": time.time(),
            "type_counts": type_counts,
            "diff_type_counts": dict(type_counts),
            "filter_counts": dict(type_counts),
        }
        store.save_reconcile_run(run)
        store.save_reconcile_details(details)

        # 回写任务行（保持旧行为）
        entry["差异行数"] = str(diff_count)
        entry["对账结论"] = conclusion
        entry["执行时间"] = ts_text
        entry["status"] = conclusion
        entry["pending"] = not passed
        entry["abnormal"] = not passed

        # 全局变更日志（旧逻辑：仅存在差异时写汇总）
        if not passed:
            store.append_changelog({
                "目标表": left_table,
                "变更类型": "对账差异",
                "变更内容": (
                    f"{run_no} only_left: {len(only_left_keys)}, "
                    f"only_right: {len(only_right_keys)}, mismatches: {mismatch_fields}"
                ),
                "操作时间": time.time(),
                "操作来源": "reconcile",
                "影响行数": diff_count,
                "回滚版本": "",
                "运行编号": run_no,
            })

        # changelog 模块清单：每个运行回写一条结果，日志可经运行编号反查明细
        add_changelog_entry({
            "目标表": left_table,
            "变更类型": "对账差异" if not passed else "对账一致",
            "变更内容": (
                f"对账运行 {run_no} 完成：结论{conclusion}，差异 {diff_count} 条"
                f"（仅左 {len(only_left_keys)} / 仅右 {len(only_right_keys)} / "
                f"字段不一致 {mismatch_fields}），可经运行编号 {run_no} 检索原明细。"
            ),
            "操作来源": "data_reconcile",
            "影响行数": str(diff_count),
            "运行编号": run_no,
            "处理结论": conclusion,
            "发送状态": "已发送",
            "记录类别": "对账运行汇总",
        })

        # 每条差异预置一条「待发送」日志说明（独立于变更日志清单，
        # 提交后才作为目标日志记录写入 changelog）
        draft_notes = [create_draft_note(d, with_changelog_row=False) for d in details]

        return {
            "ok": True,
            "entry_id": entry_id,
            "run_id": run_id,
            "run_no": run_no,
            "run_number": run_no,
            "batch_no": run_no,
            "reconcile_no": run_no,
            "运行编号": run_no,
            "left_table": left_table,
            "right_table": right_table,
            "diff_count": diff_count,
            "total": diff_count,
            "passed": passed,
            "conclusion": conclusion,
            "对账结论": conclusion,
            "only_left": len(only_left_keys),
            "only_right": len(only_right_keys),
            "mismatches": mismatch_fields,
            "type_counts": type_counts,
            "filter_counts": dict(type_counts),
            "details": details,
            "draft_notes": len(draft_notes),
            "run": run,
        }


# ---------------------------------------------------------------------------
# 运行 / 明细检索 + 稳定分页
# ---------------------------------------------------------------------------
def list_runs(task_id: Any = None) -> list[dict[str, Any]]:
    runs = list(store.reconcile_runs)
    if task_id is not None and str(task_id) != "":
        runs = [r for r in runs if str(r.get("task_id")) == str(task_id)]
    return runs


def get_run(run_ref: Any) -> dict[str, Any] | None:
    return store.find_reconcile_run(run_ref)


def details_of_run(run_no: str, diff_type: str | None = None) -> list[dict[str, Any]]:
    items = [d for d in store.reconcile_details if d.get("run_no") == run_no]
    if diff_type:
        canonical = canonical_diff_type(diff_type) or diff_type
        items = [
            d for d in items
            if d.get("差异类型") == canonical
            or str(diff_type) in d.get("差异类型", "")
            or d.get("field") == diff_type
        ]
    return items


def list_details(**kwargs: Any) -> dict[str, Any]:
    """加锁入口：与对账写入串行，保证「看到运行 → 明细已齐备」。"""
    with FLOW_LOCK:
        return _list_details_impl(**kwargs)


def _list_details_impl(
    *,
    run_ref: Any = None,
    task_id: Any = None,
    page: int = 1,
    size: int = 20,
    diff_type: str | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 20)))

    frozen_snapshot: dict[str, Any] | None = None
    if page_token:
        frozen_snapshot = store.page_token_snapshot(page_token)

    if frozen_snapshot is not None:
        # 已签发分页：完全按快照 id 序列返回，新运行写入不影响本页/总数
        all_ids: list[int] = list(frozen_snapshot["ids"])
        frozen_total = int(frozen_snapshot.get("total", len(all_ids)))
        run_no = frozen_snapshot.get("run_no")
        canonical_type = frozen_snapshot.get("diff_type")
        run = get_run(run_no) if run_no else None
        total = frozen_total
        start = (page - 1) * size
        page_ids = all_ids[start:start + size]
        by_id = {int(d["id"]): d for d in store.reconcile_details}
        items = [by_id[i] for i in page_ids if i in by_id]
        source = "page_token_snapshot"
    else:
        run = get_run(run_ref) if run_ref not in (None, "") else None
        if run is None and task_id not in (None, ""):
            run = store.latest_reconcile_run(task_id=task_id)
        if run is None and run_ref not in (None, ""):
            run = store.latest_reconcile_run(left_table=str(run_ref))  # type: ignore[arg-type]

        if run is None:
            # 兼容历史对账结论：任务行存在但没有任何运行/明细
            task_row = store.find("data_reconcile", int(task_id)) if str(task_id or "").isdigit() else None
            conclusion = ""
            if task_row is not None:
                conclusion = str(task_row.get("对账结论") or task_row.get("status") or "")
            return {
                "ok": True,
                "items": [],
                "total": 0,
                "page": page,
                "size": size,
                "page_token": None,
                "token": None,
                "run_no": None,
                "运行编号": None,
                "task_id": task_id,
                "conclusion": conclusion,
                "对账结论": conclusion,
                "source": "historical_conclusion" if conclusion else "none",
                "compatible": True,
                "filter_counts": {},
                "diff_type_filters": [],
            }

        run_no = run["run_no"]
        canonical_type = canonical_diff_type(diff_type)
        scoped = details_of_run(run_no, canonical_type)
        all_ids = [int(d["id"]) for d in scoped]
        total = len(all_ids)

        page_token = f"PT-{uuid.uuid4().hex[:16]}"
        store.reconcile_page_tokens[page_token] = {
            "run_no": run_no,
            "diff_type": canonical_type,
            "ids": all_ids,
            "total": total,
            "ts": time.time(),
        }
        start = (page - 1) * size
        items = scoped[start:start + size]
        source = "run"

    counts = run.get("type_counts", {}) if run else {}
    filters = [
        {"key": "金额差异", "label": "金额差异", "count": int(counts.get("金额差异", 0))},
        {"key": "状态差异", "label": "状态差异", "count": int(counts.get("状态差异", 0))},
        {"key": "仅左侧存在", "label": "仅左侧存在", "count": int(counts.get("仅左侧存在", 0))},
        {"key": "仅右侧存在", "label": "仅右侧存在", "count": int(counts.get("仅右侧存在", 0))},
    ]
    total_pages = (total + size - 1) // size if size else 0
    return {
        "ok": True,
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": total_pages,
        "total_pages": total_pages,
        "page_token": page_token,
        "token": page_token,
        "run_no": run.get("run_no") if run else None,
        "运行编号": run.get("运行编号") if run else None,
        "run_id": run.get("id") if run else None,
        "run": run,
        "conclusion": run.get("conclusion") if run else None,
        "对账结论": run.get("对账结论") if run else None,
        "source": source,
        "filter_counts": counts,
        "type_counts": counts,
        "diff_type_counts": counts,
        "diff_type_filters": filters,
        "filters": filters,
    }


# ---------------------------------------------------------------------------
# 日志说明（草稿 / 提交 / 陈旧差异判定）
# ---------------------------------------------------------------------------
def _sync_detail_note_status(detail_id: int, status: str) -> None:
    for d in store.reconcile_details:
        if int(d.get("id", 0)) == int(detail_id):
            d["note_status"] = status
            d["发送状态"] = status


def open_note(detail_ref: Any, content: str | None = None, operator: str = "值班员") -> dict[str, Any]:
    detail = store.find_reconcile_detail(detail_ref)
    if detail is None:
        raise KeyError(f"对账明细 {detail_ref} 不存在")

    # 同一明细已有待发送草稿则继续沿用（幂等打开）
    for note in store.log_notes:
        if int(note.get("detail_id", 0)) == int(detail["id"]) and note.get("status") == "待发送":
            if content is not None and str(content).strip():
                note["content"] = content
                note["说明内容"] = content
                note["日志说明"] = content
                if note.get("changelog_id") is not None:
                    row = store.find("changelog", int(note["changelog_id"]))
                    if row is not None:
                        row["变更内容"] = content
            if operator:
                note["operator"] = operator
            note["stale_check"] = current_key_state(note)
            return note

    note = create_draft_note(detail)
    if content is not None and str(content).strip():
        note["content"] = content
        note["说明内容"] = content
        note["日志说明"] = content
    if operator:
        note["operator"] = operator
    note["stale_check"] = current_key_state(note)
    return note


def current_key_state(note: dict[str, Any]) -> dict[str, Any]:
    """以最新对账运行判定 note 引用的业务键当前是否仍为差异。"""
    task_id = note.get("task_id")
    old_run_no = note.get("run_no")
    biz_key = str(note.get("biz_key", ""))

    latest = store.latest_reconcile_run(task_id=task_id) if task_id is not None else None
    if latest is None:
        # 兼容历史对账结论：直接读任务行结论
        task_row = store.find("data_reconcile", int(task_id)) if str(task_id or "").isdigit() else None
        conclusion = str((task_row or {}).get("对账结论") or (task_row or {}).get("status") or "")
        if conclusion == "一致":
            return {"state": "now_consistent", "reason": "historical_conclusion_consistent",
                    "latest_run_no": None, "conclusion": conclusion}
        if conclusion == "存在差异":
            return {"state": "still_diff", "reason": "historical_conclusion_diff",
                    "latest_run_no": None, "conclusion": conclusion}
        return {"state": "unknown", "reason": "no_run_no_conclusion", "latest_run_no": None}

    if latest.get("run_no") == old_run_no:
        return {"state": "current_diff", "reason": "latest_run_is_cited_run",
                "latest_run_no": latest.get("run_no"), "conclusion": latest.get("conclusion")}

    latest_matches = [
        d for d in store.reconcile_details
        if d.get("run_no") == latest.get("run_no") and str(d.get("业务键")) == biz_key
    ]
    if latest_matches:
        return {
            "state": "still_diff",
            "reason": "newer_run_still_diff",
            "latest_run_no": latest.get("run_no"),
            "conclusion": latest.get("conclusion"),
            "latest_details": latest_matches,
        }
    return {
        "state": "now_consistent",
        "reason": "corrected_in_newer_run",
        "latest_run_no": latest.get("run_no"),
        "conclusion": latest.get("conclusion"),
    }


def normalize_decision(decision: Any) -> str | None:
    if decision is None:
        return None
    text = str(decision).strip()
    if text in DECISION_CHOICES:
        return text
    if text.lower() in _CITE_ALIASES or text in _CITE_ALIASES:
        return CITE_OLD
    if text.lower() in _WITHDRAW_ALIASES or text in _WITHDRAW_ALIASES:
        return WITHDRAW
    return None


def decision_counts() -> dict[str, int]:
    counts = {CITE_OLD: 0, WITHDRAW: 0, "待发送": 0, "已发送": 0, "已撤回": 0}
    for n in store.log_notes:
        decision = n.get("处理结论") or n.get("decision") or ""
        if decision == CITE_OLD:
            counts[CITE_OLD] += 1
        elif decision == WITHDRAW:
            counts[WITHDRAW] += 1
        status = n.get("status", "")
        if status in counts:
            counts[status] += 1
    return counts


def next_send_range(
    run_ref: Any = None,
    *,
    after_detail_id: int | None = None,
    page: int | None = None,
    size: int = 10,
) -> dict[str, Any]:
    """计算下一次可发送的明细范围（同页剩余优先，否则翻到下一页）。"""
    size = max(1, int(size or 10))
    run = get_run(run_ref) if run_ref not in (None, "") else None
    if run is None:
        latest = store.latest_reconcile_run()
        run = latest
    if run is None:
        return {"available": False, "count": 0, "detail_ids": [], "label": "暂无可发范围"}

    details = sorted(details_of_run(run["run_no"]), key=lambda d: int(d.get("seq", d.get("id", 0))))
    total = len(details)

    after_index = 0
    if after_detail_id is not None:
        for i, d in enumerate(details, start=1):
            if int(d["id"]) == int(after_detail_id):
                after_index = i
                break

    if page is None and after_index:
        page = (after_index - 1) // size + 1
    page = max(1, int(page or 1))
    page_start = (page - 1) * size + 1
    page_end = min(total, page * size)

    same_page = [d for i, d in enumerate(details, start=1)
                 if max(page_start, after_index + 1) <= i <= page_end]
    next_page_no = page + 1
    next_start = page * size + 1
    next_page_items = [d for i, d in enumerate(details, start=1)
                       if next_start <= i <= min(total, next_start + size - 1)]

    if same_page:
        chosen = same_page
        scope = "same_page_remaining"
    elif next_page_items:
        chosen = next_page_items
        scope = "next_page"
    else:
        chosen = []
        scope = "exhausted"

    handled_ids = {
        int(n["detail_id"]) for n in store.log_notes
        if n.get("run_no") == run["run_no"] and n.get("status") in ("已发送", "已撤回")
    }
    sendable = [d for d in chosen if int(d["id"]) not in handled_ids]

    def _range_block(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"count": 0, "detail_ids": [], "detail_nos": [],
                    "from_index": None, "to_index": None,
                    "from_detail_no": None, "to_detail_no": None}
        seqs = [int(d.get("seq", 0)) for d in items]
        return {
            "count": len(items),
            "detail_ids": [int(d["id"]) for d in items],
            "detail_nos": [d["detail_no"] for d in items],
            "from_index": min(seqs),
            "to_index": max(seqs),
            "from_detail_no": items[0]["detail_no"],
            "to_detail_no": items[-1]["detail_no"],
        }

    block = _range_block(sendable)
    label = ""
    if block["count"]:
        label = (
            f"下一次可发范围：明细序号 {block['from_index']}–{block['to_index']}"
            f"（{block['from_detail_no']} ~ {block['to_detail_no']}），共 {block['count']} 条"
        )
    else:
        label = "后续没有待发送的日志说明"

    return {
        "available": block["count"] > 0,
        "scope": scope,
        "run_no": run["run_no"],
        "运行编号": run["run_no"],
        "page": page,
        "size": size,
        "after_index": after_index,
        "next_page": next_page_no if next_page_items else None,
        "same_page_remaining": _range_block(same_page),
        "next_page_range": _range_block(next_page_items),
        "handled_detail_ids": sorted(handled_ids),
        # 常见命名别名，方便不同检索口径直接取值
        "start_index": block["from_index"],
        "end_index": block["to_index"],
        "start_detail_no": block["from_detail_no"],
        "end_detail_no": block["to_detail_no"],
        "start": block["from_index"],
        "end": block["to_index"],
        "next_sendable_ids": block["detail_ids"],
        "range": label,
        **block,
        "label": label,
        "text": label,
    }


def submit_note(
    note_ref: Any,
    content: str | None = None,
    decision: str | None = None,
    operator: str | None = None,
    *,
    page: int | None = None,
    size: int = 10,
) -> dict[str, Any]:
    note = store.find_log_note(note_ref)
    if note is None:
        return {"ok": False, "code": "note_not_found", "reason": f"日志说明 {note_ref} 不存在"}

    # 幂等：已终结的说明重复提交时不重复投递
    if note.get("status") in ("已发送", "已撤回"):
        target = None
        if note.get("target_changelog_id") is not None:
            target = store.find("changelog", int(note["target_changelog_id"]))
        rng = next_send_range(note.get("run_no"), after_detail_id=note.get("detail_id"),
                              page=page or note.get("page_hint"), size=size)
        return {
            "ok": True, "duplicate": True, "note": note,
            "decision": note.get("处理结论"), "target_entry": target,
            "changelog_entry": target, "next_send_range": rng,
            "decision_counts": decision_counts(),
        }

    if content is not None and str(content).strip():
        note["content"] = content
        note["说明内容"] = content
        note["日志说明"] = content
    if operator:
        note["operator"] = operator

    state_info = current_key_state(note)
    state = state_info["state"]
    chosen = normalize_decision(decision)

    if state == "now_consistent" and chosen is None:
        return {
            "ok": False,
            "code": "stale_decision_required",
            "reason": (
                f"最新对账运行 {state_info.get('latest_run_no') or ''} 已把业务键 "
                f"{note.get('业务键')} 判为一致；提交前必须说明是"
                f"「{CITE_OLD}」还是「{WITHDRAW}」。"
            ),
            "stale": True,
            "state_info": state_info,
            "note": note,
            "choices": list(DECISION_CHOICES),
        }

    with FLOW_LOCK:
        now = _now_text()
        note["sent_at"] = now
        note["stale_check"] = state_info

        cited_old_diff = False
        withdrawn = False
        if chosen == WITHDRAW or (state == "now_consistent" and chosen == WITHDRAW):
            chosen = WITHDRAW
            withdrawn = True
            note["status"] = "已撤回"
            note["发送状态"] = "已撤回"
            note["decision"] = WITHDRAW
            note["处理结论"] = WITHDRAW
            audit_sentence = (
                f"【{WITHDRAW}】业务键 {note['业务键']} 在最新对账"
                f"（{state_info.get('latest_run_no') or '历史结论'}）中已修正为一致，"
                f"本说明撤回 {note['run_no']} / {note['detail_no']} 记录的旧差异"
                f"（{note['差异类型']}：左侧「{note['左侧值']}」、右侧「{note['右侧值']}」），不再对外发送。"
            )
            if WITHDRAW not in str(note.get("content", "")):
                note["content"] = f"{note.get('content', '')}\n{audit_sentence}".strip()
            changelog_type = "更正撤回"
            record_category = "对账更正撤回"
        else:
            # 继续引用旧差异（陈旧时必须显式选择；未陈旧直接发送也算引用当前差异）
            stale_cite = state == "now_consistent"
            chosen = CITE_OLD if stale_cite else (chosen or "差异仍成立")
            cited_old_diff = True
            note["status"] = "已发送"
            note["发送状态"] = "已发送"
            note["decision"] = chosen
            note["处理结论"] = chosen
            if stale_cite:
                audit_sentence = (
                    f"【{CITE_OLD}】最新对账运行 {state_info.get('latest_run_no') or ''} "
                    f"已将业务键 {note['业务键']} 判为一致；经值班员确认，本说明继续引用旧运行 "
                    f"{note['run_no']} 明细 {note['detail_no']} 的旧差异"
                    f"（{note['差异类型']}：左侧「{note['左侧值']}」、右侧「{note['右侧值']}」）。"
                )
                if CITE_OLD not in str(note.get("content", "")):
                    note["content"] = f"{note.get('content', '')}\n{audit_sentence}".strip()
            changelog_type = "对账差异"
            record_category = "旧差异引用说明" if stale_cite else "差异说明"

        note["说明内容"] = note["content"]
        note["日志说明"] = note["content"]

        run = get_run(note.get("run_no"))
        existing_row = None
        if note.get("changelog_id") is not None:
            existing_row = store.find("changelog", int(note["changelog_id"]))

        target_values = {
            "目标表": (run or {}).get("left_table", ""),
            "变更类型": changelog_type,
            "变更内容": note["content"],
            "操作来源": "值班员日志说明",
            "影响行数": "1",
            "运行编号": note["run_no"],
            "明细编号": note["detail_no"],
            "业务键": note["业务键"],
            "差异类型": note["差异类型"],
            "左侧值": note["左侧值"],
            "右侧值": note["右侧值"],
            "处理结论": note["处理结论"],
            "发送状态": note["status"],
            "记录类别": record_category,
            "note_id": note["id"],
            "status": "已记录",
            "pending": False,
        }
        if existing_row is not None:
            # 待发送草稿原地转为目标日志记录，清单编号保持稳定
            existing_row.update(target_values)
            target_entry = existing_row
        else:
            target_entry = add_changelog_entry(target_values)
        note["changelog_id"] = target_entry["id"]
        note["target_changelog_id"] = target_entry["id"]
        note["target_changelog_no"] = target_entry["记录编号"]
        _sync_detail_note_status(note["detail_id"], note["status"])

        store.append_changelog({
            "目标表": target_entry["目标表"],
            "变更类型": changelog_type,
            "变更内容": note["content"],
            "操作时间": time.time(),
            "操作来源": "reconcile_note",
            "影响行数": 1,
            "运行编号": note["run_no"],
            "明细编号": note["detail_no"],
            "业务键": note["业务键"],
            "处理结论": note["处理结论"],
        })

        counts = (run or {}).get("type_counts", {})
        rng = next_send_range(note.get("run_no"), after_detail_id=note.get("detail_id"),
                              page=page, size=size)
        return {
            "ok": True,
            "note": note,
            "decision": note["处理结论"],
            "stale": state == "now_consistent",
            "state_info": state_info,
            "cited_old_diff": cited_old_diff,
            "withdrawn": withdrawn,
            "target_log": "changelog",
            "target_entry": target_entry,
            "changelog_entry": target_entry,
            "log_entry": target_entry,
            "target_changelog_no": target_entry["记录编号"],
            "filter_counts": counts,
            "diff_type_counts": counts,
            "decision_counts": decision_counts(),
            "next_send_range": rng,
        }


def list_notes(
    *,
    run_ref: Any = None,
    detail_ref: Any = None,
    status: str | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    notes = list(store.log_notes)
    if run_ref not in (None, ""):
        run = get_run(run_ref)
        run_no = run["run_no"] if run else str(run_ref)
        notes = [n for n in notes if n.get("run_no") == run_no or n.get("运行编号") == run_no]
    if detail_ref not in (None, ""):
        detail = store.find_reconcile_detail(detail_ref)
        if detail is not None:
            notes = [n for n in notes if int(n.get("detail_id", 0)) == int(detail["id"])]
        elif str(detail_ref).isdigit():
            notes = [n for n in notes if int(n.get("detail_id", 0)) == int(detail_ref)]
    if status:
        notes = [n for n in notes if n.get("status") == status or n.get("发送状态") == status]
    if decision:
        canonical = normalize_decision(decision) or decision
        notes = [n for n in notes
                 if n.get("处理结论") == canonical or decision in str(n.get("处理结论", ""))]
    return notes


# ---------------------------------------------------------------------------
# 经运行编号追查明细 / 日志
# ---------------------------------------------------------------------------
def changelog_trace(run_ref: Any = None, detail_ref: Any = None) -> dict[str, Any]:
    run = get_run(run_ref) if run_ref not in (None, "") else None
    detail = store.find_reconcile_detail(detail_ref) if detail_ref not in (None, "") else None
    if detail is not None and run is None:
        run = get_run(detail.get("run_no"))
    run_no = (run or detail or {}).get("run_no")

    details = details_of_run(run_no) if run_no else []
    if detail is not None:
        details = [d for d in details if int(d["id"]) == int(detail["id"])] or [detail]

    changelog_rows = [
        r for r in store.rows("changelog")
        if (run_no and str(r.get("运行编号", "")) == str(run_no))
        or (detail is not None and str(r.get("明细编号", "")) == str(detail.get("detail_no")))
    ]
    global_rows = [
        r for r in store._global_changelog
        if (run_no and str(r.get("运行编号", "")) == str(run_no))
        or (detail is not None and str(r.get("明细编号", "")) == str(detail.get("detail_no")))
    ]
    return {
        "ok": True,
        "run": run,
        "run_no": run_no,
        "运行编号": run_no,
        "detail": detail,
        "details": details,
        "total": len(details),
        "changelog_entries": changelog_rows,
        "changelog_rows": changelog_rows,
        "global_changelog": global_rows,
    }


# ---------------------------------------------------------------------------
# 验收造数：超过一页的「金额差异 + 状态差异」，以及纠正工具
# ---------------------------------------------------------------------------
ACCEPTANCE_LEFT_TABLE = "recon_acc_order_left"
ACCEPTANCE_RIGHT_TABLE = "recon_acc_order_right"
ACCEPTANCE_TASK_NO = "DATA-ACCEPT"


def setup_acceptance_case(total: int = 23, amount_count: int = 12) -> dict[str, Any]:
    """构造超过一页的金额/状态差异，并执行首次对账。

    默认 23 条差异：前 12 条金额不一致，后 11 条状态不一致。
    """
    amount_count = min(int(amount_count), int(total))
    left_rows: list[dict[str, Any]] = []
    right_rows: list[dict[str, Any]] = []
    for i in range(1, total + 1):
        key = f"ACC-{i:03d}"
        if i <= amount_count:
            left_rows.append({"业务键": key, "金额": 1000 + i * 10, "状态": "已完成",
                              "客户": f"客户{i}", "status": "已核对"})
            right_rows.append({"业务键": key, "金额": 1200 + i * 10, "状态": "已完成",
                               "客户": f"客户{i}", "status": "已核对"})
        else:
            left_rows.append({"业务键": key, "金额": 2000 + i * 10, "状态": "已完成",
                              "客户": f"客户{i}", "status": "已核对"})
            right_rows.append({"业务键": key, "金额": 2000 + i * 10, "状态": "处理中",
                               "客户": f"客户{i}", "status": "已核对"})
    with FLOW_LOCK:
        store._tables[ACCEPTANCE_LEFT_TABLE] = left_rows
        store._tables[ACCEPTANCE_RIGHT_TABLE] = right_rows

        task = next((r for r in store.rows("data_reconcile")
                     if r.get("任务编号") == ACCEPTANCE_TASK_NO), None)
        if task is None:
            task_id = max((int(r.get("id", 0)) for r in store.rows("data_reconcile")), default=0) + 1
            task = {
                "id": task_id,
                "status": "待执行",
                "pending": True,
                "abnormal": False,
                "任务编号": ACCEPTANCE_TASK_NO,
                "左源表": ACCEPTANCE_LEFT_TABLE,
                "右源表": ACCEPTANCE_RIGHT_TABLE,
                "对账字段": "业务键",
                "差异阈值": "0",
                "执行时间": "",
                "差异行数": "",
                "对账结论": "",
            }
            store.rows("data_reconcile").append(task)
        else:
            task["左源表"] = ACCEPTANCE_LEFT_TABLE
            task["右源表"] = ACCEPTANCE_RIGHT_TABLE
            task["对账字段"] = "业务键"
            task["status"] = "待执行"
    result = execute_reconcile(task)
    result["task_id"] = task["id"]
    result["acceptance_task_no"] = ACCEPTANCE_TASK_NO
    result["left_table"] = ACCEPTANCE_LEFT_TABLE
    result["right_table"] = ACCEPTANCE_RIGHT_TABLE
    return result


def correct_value(
    *,
    biz_key: str | None = None,
    detail_ref: Any = None,
    field_name: str | None = None,
    rerun: bool = False,
) -> dict[str, Any]:
    """把右侧值纠正为左侧值（默认纠正该业务键的全部差异字段），可顺带重跑。"""
    detail = store.find_reconcile_detail(detail_ref) if detail_ref not in (None, "") else None
    key = biz_key or (detail or {}).get("业务键")
    if not key and detail is not None:
        key = detail.get("biz_key")
    if not key:
        return {"ok": False, "reason": "必须提供 biz_key / detail_ref"}

    run = get_run((detail or {}).get("run_no")) if detail else store.latest_reconcile_run()
    left_table = (run or {}).get("left_table") or ACCEPTANCE_LEFT_TABLE
    right_table = (run or {}).get("right_table") or ACCEPTANCE_RIGHT_TABLE
    reconcile_field = (run or {}).get("reconcile_field") or "业务键"

    left_row = next((r for r in store.rows(left_table) if str(r.get(reconcile_field)) == str(key)), None)
    right_row = next((r for r in store.rows(right_table) if str(r.get(reconcile_field)) == str(key)), None)
    if left_row is None or right_row is None:
        return {"ok": False, "reason": f"业务键 {key} 在左右侧表中不全存在"}

    corrected_fields: list[str] = []
    targets = [field_name] if field_name else [
        f for f in left_row.keys()
        if f in right_row and f not in INTERNAL_FIELDS and not str(f).startswith("_")
    ]
    for f in targets:
        if not values_equal(left_row.get(f), right_row.get(f)) or field_name:
            right_row[f] = left_row.get(f)
            corrected_fields.append(f)

    rerun_result = None
    if rerun:
        task_id = (run or {}).get("task_id")
        task_row = store.find("data_reconcile", int(task_id)) if task_id is not None else None
        if task_row is None:
            task_row = next((r for r in store.rows("data_reconcile")
                             if r.get("左源表") == left_table), None)
        if task_row is not None:
            rerun_result = execute_reconcile(task_row)

    return {
        "ok": True,
        "biz_key": key,
        "corrected_fields": corrected_fields,
        "left_table": left_table,
        "right_table": right_table,
        "rerun": rerun_result is not None,
        "rerun_result": rerun_result,
        "new_run_no": (rerun_result or {}).get("run_no"),
    }
