"""Terminal feedback and safe completion handoff for independent reduction only."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .film_display_format import format_film_thickness

if TYPE_CHECKING:
    from .film_optimization_quick_controller import FilmOptimizationQuickController


def reduction_status(controller: FilmOptimizationQuickController) -> str | None:
    c = controller
    if c.busy or c.loading:
        return None
    value = c._reduction_result
    error = c._error
    if value is None:
        if error:
            return f"减量优化未完成：{error}；原设计未改变。"
        if c._result_terminal_state in {"已取消", "取消", "cancelled"}:
            return "减量优化已取消；尚无完整验证候选，原设计未改变。"
        return None
    result = value.base_result
    source, candidate = value.metrics["baseline"], value.metrics["candidate"]
    changes = (f"层数 {source['layer_count']} → {candidate['layer_count']}；总厚 "
               f"{format_film_thickness(source['total_thickness_nm'])} → "
               f"{format_film_thickness(candidate['total_thickness_nm'])} nm")
    reason = str(error or result.message or "未提供终止原因")
    if result.state == "failed" or (error and c._result_terminal_state == "失败"):
        return f"减量优化失败：{reason}；返回候选已保留，未自动应用。{changes}"
    if result.state == "cancelled":
        applied = "候选已应用，可撤销" if c._candidate_applied else "候选已保留，未自动应用"
        return f"减量优化已取消；{applied}。{changes}。终止原因：{reason}"
    if error:
        retained = "候选已应用，未保存，可撤销" if c._candidate_applied else "原设计未改变，候选已保留"
        return f"减量操作未完成：{error}。{retained}。{changes}"
    if not value.is_final:
        return f"尚未收到完整减量终态；阶段候选未自动应用。{changes}。{reason}"
    if not value.candidate_verified or not value.summary.get("candidate_feasible"):
        return ("减量搜索已结束，未找到同时通过完整原目标与独立复核的候选；原设计未改变。"
                f"{changes}。终止原因：{reason}")
    if not value.summary.get("reduction_achieved"):
        return f"减量搜索已结束，未找到合格改善；原设计未改变。{changes}。终止原因：{reason}"
    if c._candidate_applied:
        return f"减量优化完成，合格改善已应用（未保存，可撤销）。{changes}。"
    blocked = ("来源设计窗口已关闭" if c._source_closed else
               "当前尝试已请求取消" if c._cancel_requested or c._cancel_intent_accepted else
               "当前窗口正在关闭" if c._pending_close or c._shutting_down else
               "当前为独立只读快照" if c._detached_source or c.session is None else
               "来源设计或未提交编辑已改变" if c.candidateStale else
               "来源项目或会话不允许安全回写")
    return f"减量搜索已完成；合格候选已保留，未自动应用：{blocked}。{changes}"


def complete_reduction(controller: FilmOptimizationQuickController,
                       operation_id: int, error: object) -> None:
    """Reuse the existing audited apply/undo transaction; never replay optics here."""
    c = controller
    value = c._reduction_result
    if (error or value is None or not value.is_final or value.state != "completed"
            or not c._reduction_eligible() or c._candidate_applied
            or c._shutting_down or c._detached_source or c._source_closed
            or c._pending_close or c._cancel_requested or c._cancel_intent_accepted
            or operation_id != c._operation_serial or not c._project_current()):
        return
    if not c.canApply:
        return
    source = c._source_controller
    if not c._apply_candidate():
        return
    opener = getattr(source, "openSimulationAndCalculate", None)
    if callable(opener):
        try:
            if not opener():
                raise ValueError("光谱仿真窗口未确认打开")
        except Exception as exc:
            c._error = f"减量候选已应用，但光谱刷新未完成：{type(exc).__name__}: {exc}"
    c._progress_text = reduction_status(c) or c._progress_text
    c.changed.emit()
    c.plotDataChanged.emit()
