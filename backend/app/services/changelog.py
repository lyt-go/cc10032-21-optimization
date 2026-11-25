"""changelog 领域服务：变更日志记录、对账追溯与日志说明投递。

业务逻辑：
1. 变更记录：目标表的增量变更、回滚版本、影响行数
2. 变更追溯链路：data_reconcile → changelog（对账结果回写清单，
   日志经由运行编号查找原明细）
3. 日志说明：从旧差异打开发送前草稿；若重跑已修正，提交时必须写明
   「继续引用旧差异」或「因更正撤回」
4. 失败场景：回滚版本不存在、记录冲突、历史清理误删
5. 与上下游流水线：消费 data_reconcile 的对账差异；输出变更追溯信息

跨模块数据流：
  data_reconcile → changelog（对账差异写入变更日志）
"""
from __future__ import annotations

import time
from typing import Any

from app.services import reconcile_flow as flow
from app.services.base import BaseModuleService
from app.store import store


class ChangelogService(BaseModuleService):
    MODULE = "changelog"
    ENTITY = "变更记录"
    OPERATION_TERM = "查询"
    LIST_FIELDS = [
        "记录编号", "目标表", "变更类型", "变更内容", "操作时间", "操作来源",
        "影响行数", "回滚版本",
        # 对账追溯扩展列（追加在末尾，保持既有取数/筛选口径）
        "运行编号", "明细编号", "业务键", "差异类型", "处理结论", "发送状态", "记录类别",
    ]
    REQUIRED_FIELDS = ["记录编号", "目标表", "变更类型"]
    STATUS_ORDER = ["已记录", "已回滚", "已归档"]
    ACTION_RULES = {"发起回滚": "已回滚", "归档记录": "已归档"}
    NEGATIVE_ACTIONS = []

    PIPELINE = "changelog_pipeline"
    UPSTREAM_MODULES = ["data_reconcile"]
    PRODUCES_ARTIFACT = "变更追溯记录"

    # 查询参数别名 → 清单字段
    FILTER_ALIASES = {
        "run_no": "运行编号",
        "runNo": "运行编号",
        "detail_no": "明细编号",
        "detailNo": "明细编号",
        "biz_key": "业务键",
        "bizKey": "业务键",
        "decision": "处理结论",
        "handle": "处理结论",
        "send_status": "发送状态",
        "note_status": "发送状态",
        "diff_type": "差异类型",
        "category": "记录类别",
    }

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """在通用筛选之上兼容 run_no / decision 等英文检索别名。"""
        normalized: dict[str, str] = {}
        for key, value in (filters or {}).items():
            target_field = self.FILTER_ALIASES.get(key, key)
            normalized[target_field] = value
        return super().list_entries(
            filters=normalized, keyword=keyword, status=status, page=page, size=size)

    def filter_counts(self) -> dict[str, Any]:
        """两个处置口径的筛选条数：继续引用旧差异 / 因更正撤回。"""
        decision_counts = flow.decision_counts()
        rows = store.rows(self.MODULE)
        type_counts: dict[str, int] = {}
        for row in rows:
            ctype = str(row.get("变更类型", ""))
            if ctype:
                type_counts[ctype] = type_counts.get(ctype, 0) + 1
        return {
            "decision_counts": decision_counts,
            "处理结论筛选条数": {
                flow.CITE_OLD: decision_counts.get(flow.CITE_OLD, 0),
                flow.WITHDRAW: decision_counts.get(flow.WITHDRAW, 0),
            },
            "type_counts": type_counts,
        }

    def query_table_changelog(self, table_name: str) -> dict[str, Any]:
        """查询目标表的完整变更历史（从全局变更日志中提取）。

        与 data_reconcile 的链路：对账发现的差异会写入全局变更日志与模块清单，
        changelog 模块提供对外查询接口，追溯数据变更轨迹；带运行编号的记录会
        附带对应对账运行与原明细，方便定位被比较的实际行。
        """
        records = store.changelog_for_table(table_name)
        changes_by_type = {}
        for r in records:
            ctype = r.get("变更类型", "unknown")
            changes_by_type[ctype] = changes_by_type.get(ctype, 0) + 1

        enriched_latest: list[dict[str, Any]] = []
        for r in records[-5:]:
            item = dict(r)
            run_no = r.get("运行编号")
            if run_no:
                run = flow.get_run(run_no)
                item["对账运行"] = run
                if r.get("明细编号"):
                    item["原明细"] = store.find_reconcile_detail(r.get("明细编号"))
            enriched_latest.append(item)

        return {
            "ok": True,
            "table": table_name,
            "total_changes": len(records),
            "by_type": changes_by_type,
            "latest_records": enriched_latest,
            "source": "global_changelog",
        }

    def trace_by_run(self, run_ref: Any) -> dict[str, Any]:
        """经由运行编号查找原明细与回写日志。"""
        return flow.changelog_trace(run_ref=run_ref)

    def trace_by_detail(self, detail_ref: Any) -> dict[str, Any]:
        return flow.changelog_trace(detail_ref=detail_ref)

    # ---- 日志说明 ----
    def open_note(self, detail_ref: Any, content: str | None = None) -> dict[str, Any]:
        return flow.open_note(detail_ref, content)

    def submit_note(self, note_ref: Any, **kwargs: Any) -> dict[str, Any]:
        return flow.submit_note(note_ref, **kwargs)

    NOTE_ACTIONS = {"提交说明", "发送说明", "发送日志说明", "提交日志说明", "发送"}

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """兼容通用动作入口提交日志说明；其余走回滚/归档状态流转。"""
        context = context or {}
        if action in self.NOTE_ACTIONS or action in flow.DECISION_CHOICES:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            note = None
            if entry.get("note_id") is not None:
                note = store.find_log_note(int(entry["note_id"]))
            if note is None and entry.get("明细编号"):
                note = flow.open_note(entry["明细编号"], entry.get("变更内容"))
            if note is None:
                return None, "该记录没有关联的对账明细，不能作为日志说明提交"

            decision = action if action in flow.DECISION_CHOICES else context.get("decision")
            remark = context.get("remark")
            if not decision and remark:
                text = str(remark)
                if flow.CITE_OLD in text:
                    decision = flow.CITE_OLD
                elif flow.WITHDRAW in text:
                    decision = flow.WITHDRAW
                else:
                    parsed = flow.normalize_decision(text)
                    decision = parsed
            result = flow.submit_note(
                note["id"], content=context.get("content"), decision=decision)
            if not result.get("ok"):
                return None, str(result.get("reason", "日志说明提交失败"))
            target = result.get("changelog_entry") or entry
            if result.get("withdrawn"):
                return target, f"日志说明已按「{flow.WITHDRAW}」撤回"
            return target, f"日志说明已提交到变更日志清单（{target.get('记录编号', '')}）"

        return super().run_action(entry_id, action, context)

    def rollback_change(self, entry_id: int) -> dict[str, Any]:
        """回滚变更记录

        失败场景：
        - 回滚版本不存在
        - 记录已回滚无法再次回滚
        - 回滚依赖冲突
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"记录 {entry_id} 不存在"}
        if entry.get("status") == "已回滚":
            return {"ok": False, "reason": "该记录已经回滚过，无法重复回滚"}

        rollback_version = str(entry.get("回滚版本", ""))
        if not rollback_version:
            return {"ok": False, "reason": "无回滚版本信息，无法回滚"}

        entry["status"] = "已回滚"
        entry["pending"] = True
        entry["abnormal"] = False

        batch_id = f"ROLLBACK_{entry_id}_{int(time.time())}"
        self.produce_artifact([{
            "change_id": entry_id,
            "table": entry.get("目标表"),
            "rollback_version": rollback_version,
            "ts": time.time(),
        }], batch_id)

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": entry.get("目标表"),
            "rollback_version": rollback_version,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["global_changelog_total"] = len(store._global_changelog)
        base["log_notes"] = len(store.log_notes)
        return base
