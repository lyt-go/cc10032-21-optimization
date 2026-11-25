"""data_reconcile 领域服务：数据对账、差异明细检索与稳定分页。

业务逻辑：
1. 数据对账：左源表与右源表按对账字段逐行对比
2. 每次执行保存为一个「对账运行」，运行下固化逐条明细：
   差异类型、业务键、左侧值/右侧值、明细编号
3. 对账链路：data_reconcile → changelog（运行结果回写变更日志清单，
   日志经由运行编号反查原明细）
4. 明细分页签发 page_token，新一轮对账并发写入不影响已签发分页
5. 失败场景：差异阈值超限、字段类型不匹配、对账超时、源表数据为空

跨模块数据流：
  data_reconcile → changelog（对账差异写入变更日志，用于变更追溯）
"""
from __future__ import annotations

from typing import Any

from app.services import reconcile_flow as flow
from app.services.base import BaseModuleService
from app.store import store


class DataReconcileService(BaseModuleService):
    MODULE = "data_reconcile"
    ENTITY = "对账任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "左源表", "右源表", "对账字段", "差异阈值", "执行时间", "差异行数", "对账结论"]
    REQUIRED_FIELDS = ["任务编号", "左源表", "右源表"]
    STATUS_ORDER = ["待执行", "执行中", "一致", "存在差异"]
    ACTION_RULES = {"发起对账": "执行中", "标记一致": "一致", "标记差异": "存在差异"}
    NEGATIVE_ACTIONS = []

    PIPELINE = "reconcile_pipeline"
    DOWNSTREAM_MODULES = ["changelog"]
    PRODUCES_ARTIFACT = "数据对账结果记录"

    # ---- 对账执行 ----
    def execute_reconcile(self, entry_id: int) -> dict[str, Any]:
        """执行对账：比较左右源表数据差异，保存运行与明细并回写 changelog。

        允许对同一任务重复执行（重跑）：每次产生新的运行编号，旧运行明细与
        已签发分页保持不变。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {entry_id} 不存在"}
        result = flow.execute_reconcile(entry)

        batch_id = f"RECONCILE_{entry_id}_{result.get('run_id')}_{int(__import__('time').time() * 1000)}"
        artifact = [{
            "task": entry.get("任务编号"),
            "run_no": result.get("run_no"),
            "diff_count": result.get("diff_count"),
            "passed": result.get("passed"),
            "left_table": result.get("left_table"),
            "right_table": result.get("right_table"),
        }]
        self.produce_artifact(artifact, batch_id)
        return result

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """「发起对账」真正执行对账并支持重跑，其余动作走通用状态流转。"""
        if action == "发起对账":
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            entry["status"] = "执行中"
            result = self.execute_reconcile(entry_id)
            if not result.get("ok"):
                entry["status"] = "待执行"
                entry["pending"] = True
                return None, str(result.get("reason", "对账执行失败"))
            run_no = result.get("run_no", "")
            entry["last_run_no"] = run_no
            entry["最近运行编号"] = run_no
            if result.get("passed"):
                message = f"对账完成（{run_no}）：结论一致，差异 0 条"
            else:
                message = (
                    f"对账完成（{run_no}）：发现差异 {result.get('diff_count')} 条，"
                    f"已回写变更日志清单"
                )
            return entry, message
        return super().run_action(entry_id, action)

    # ---- 运行 / 明细检索（供路由与潜在的服务级调用） ----
    def list_runs(self, task_id: Any = None) -> list[dict[str, Any]]:
        return flow.list_runs(task_id)

    def get_run(self, run_ref: Any) -> dict[str, Any] | None:
        return flow.get_run(run_ref)

    def list_diff_details(self, **kwargs: Any) -> dict[str, Any]:
        return flow.list_details(**kwargs)

    # 常见命名别名
    list_details = list_diff_details

    def get_diff_details(self, **kwargs: Any) -> dict[str, Any]:
        return flow.list_details(**kwargs)

    def diff_details_page(self, **kwargs: Any) -> dict[str, Any]:
        return flow.list_details(**kwargs)

    def open_log_note(self, detail_ref: Any, content: str | None = None) -> dict[str, Any]:
        return flow.open_note(detail_ref, content)

    def submit_log_note(self, note_ref: Any, **kwargs: Any) -> dict[str, Any]:
        return flow.submit_note(note_ref, **kwargs)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["pending_tasks"] = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "待执行")
        base["reconcile_runs"] = len(store.reconcile_runs)
        base["reconcile_details"] = len(store.reconcile_details)
        base["log_notes"] = len(store.log_notes)
        return base
