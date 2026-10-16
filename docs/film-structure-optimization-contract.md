# 膜系结构优化队列与 UI 契约（2026-10-12）

本文说明现有结构搜索如何构造、冻结、排队、验证并安全应用结果。接口真值是 `coat_sim/core/film_optimization_structure.py`、`coat_sim/core/film_optimization_job.py`、调度器结果能力检查和 Quick Controller 的实际签名；本文描述模块边界，不另造接口。

## 请求与搜索边界

固定拓扑模式由 `FilmOptimizationRequest.solver` 选择局部求解器。结构搜索由独立的 `FilmStructureOptimizationRequest.mode` 选择 `combined`、`needle_only` 或 `growth_only`；结构请求不提交 `solver_id`，其内部 `base_request.solver` 固定为 `baseline`。结构搜索是在固定拓扑厚度优化外增加有界的插入和生长候选搜索。

请求以冻结的设计快照、优化目标、厚度变量、显式候选材料/光学集合和结构硬约束构成。当前主要默认值为：

```python
FilmStructureOptimizationRequest(
    base_request: FilmOptimizationRequest,
    candidate_materials: tuple[FilmNeedleMaterialReference, ...],
    mode: str = "combined",
    max_total_thickness_nm: float = 2000.0,  # Dmax
    min_thickness_nm: float = 0.5,           # dmin
    max_thickness_nm: float = 200.0,         # dmax
    max_total_layers: int = 32,              # 实际行数，遵守文档4096层资源防护
    max_added_layers: int = 1,
    position_samples: int = 17,
    candidate_budget: int = 128,
    needle_thickness_samples: int = 9,
    growth_steps_nm: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0),
    growth_phase_fractions: tuple[float, ...] = (0.25, 0.5, 1.0),
    growth_cycles: int = 1,
    seed: int = 0,
)
```

UI 只将用户明确勾选的 `FilmNeedleMaterialReference(material_id, optical_set_id)` 放入请求。冻结时，核心层捕获原设计和这些材料/光学集合的记录与摘要；worker 重放时只使用冻结快照，不重新读取可变材料目录。未选材料不成为候选。

厚度单位为 nm。`max_total_thickness_nm` 是 front 膜系总厚度上限；每个持久化层都满足全局 dmin/dmax，所选基础厚度变量还满足其自身区间与全局区间的交集。`max_total_layers` 统计持久化的实际行数；内部针插会把宿主层拆成两个同材料的正厚度行，因此这些行也计入上限。当前实现将 `max_added_layers` 限为 1、将 `growth_cycles` 限为 1，不另设128层业务上限；`max_total_layers` 仍遵守文档4096层解析资源防护。固定层序、针插、结构与严格队列payload不再限制8个基础厚度变量；层数、变量和采样共同影响计算资源，建议低预算试跑。

坐标沿 `design_snapshot.front_layers` 存储顺序：z=0 在基底侧，z 向入射介质方向增加；光学求解器按相反顺序计算。每个已有层界面都会扫描；每层另取 `position_samples` 个均匀内点，分数为 `k/(position_samples+1)`，其中 k=1…N。内部针插将宿主层分成两行正厚度同材料层；零厚度只用于内存中的解析灵敏度探测，不写入设计。

`candidate_budget` 限制进入精确评估阶段的正厚度插入/生长 proposal 数；计数独立于完整目标网格评估预算，即使后续评估失败或取消，该已发起的 proposal 也计数。`base_request.max_evaluations` 限制完整候选评估调用总数，训练评分与 holdout 验证共用此上限。一次候选评估覆盖该候选的完整所选目标网格；带 Jacobian 的调用会在内部产生若干 Jacobian 子调用。每次完整评估只消耗一个 `objective_evaluations` 预算项，内部子调用另记入 `jacobian_evaluations` 遥测，不另设一个独立的请求预算。`validation_evaluations` 单独统计 holdout 网格评估次数；它们仍占用相同的完整评估总预算。失败的完整评估调用也已消耗一项预算。候选预算不限制 `growth_steps_nm`、`growth_phase_fractions` 或 `growth_cycles` 的取值本身。

## 实际接口与结果

以下签名与当前实现一致。优化器的 checkpoint 回调收到 `FilmStructureOptimizationResult` 对象；序列化在调用方显式执行。

```python
freeze_film_structure_optimization_request(
    service: Any, request: FilmStructureOptimizationRequest,
) -> FilmStructureOptimizationRequest

optimize_film_design_with_structure(
    service: Any,
    request: FilmStructureOptimizationRequest,
    *,
    progress_callback: Callable[[FilmOptimizationProgress], None] | None = None,
    cancel_event: Any = None,
    checkpoint_callback: Callable[[FilmStructureOptimizationResult], None] | None = None,
) -> FilmStructureOptimizationResult

validate_film_structure_payload(
    service: Any, payload: Mapping[str, Any],
) -> dict[str, Any]

film_structure_request_to_payload(
    request: FilmStructureOptimizationRequest,
) -> dict[str, Any]
film_structure_request_from_payload(
    payload: Mapping[str, Any],
) -> FilmStructureOptimizationRequest
film_structure_result_to_payload(
    request: FilmStructureOptimizationRequest | None,
    result: FilmStructureOptimizationResult,
) -> dict[str, Any]
film_structure_checkpoint_to_payload(
    request: FilmStructureOptimizationRequest | None,
    result: FilmStructureOptimizationResult,
) -> dict[str, Any]
film_structure_result_from_payload(
    payload: Mapping[str, Any],
) -> FilmStructureOptimizationResult
```

请求序列化为独立的 `coat_sim.film_structure_optimization_request` envelope，并拒绝 `solver` 与 `solver_id`。结果和 checkpoint 使用 `coat_sim.film_structure_optimization_result` envelope；外层包含请求、候选、结构审计、部分结果、状态和消息。`validate_film_structure_payload(service, payload)` 验证该 envelope 并返回经过检查的 payload；它不接收 request 与 typed result 参数。

候选记录含设计 revision、持久化设计、厚度映射、训练/holdout 质量信息和条件误差；结构记录含模式、拓扑审计、trace、计数器和新增针插层 ID。重放会在冻结训练评分及硬约束通过后设置 `partial_result.candidate_verified`；holdout 是否完成由 `validation_complete` 独立表示。已验证但 holdout 未完成的候选可以查看，不能应用。

`partial_result.is_final` 与 `partial_result.best_so_far` 必须是互补的严格布尔值：最终结果为 `is_final=True, best_so_far=False`；checkpoint/nonfinal 结果为 `is_final=False, best_so_far=True`。这两个字段位于 `partial_result`，不位于 envelope 顶层。只有最终 `completed` 且 holdout 完整的结构结果具备自动写回资格；checkpoint 即使底层基础结果状态已为 completed，也仍然是 nonfinal，不能应用。

## 状态所有权、取消与写回

`FilmOptimizationQuickController` 持有页面输入、模式、显式候选勾选、草稿、进度及当前 operation token。`FilmDesignEditSession` 持有原设计和 `design_revision`。Scheduler 持有 job ID、任务状态及持久化 checkpoint。worker 只接收冻结请求并返回结果，不直接修改编辑 session。

页面通过 `freeze_spec(service, "film_optimization", {"request": request})` 提交。executor 仍是 `film_optimization`；job 根据独立 request kind 调用结构序列化、计算和 envelope 重放验证。结构 worker 将 typed checkpoint/result 转成 payload 后写入 scheduler。核心任务完成/取消/失败时，UI 用 operation token 丢弃过期完成信号；取消和失败可以保留已验证候选供查看，但不会把它们升级成成功最终结果，也不会自动应用。

应用必须同时满足：操作仍是当前 token、结果为最终 `completed`、holdout 验证完整、来源窗口及 session 仍有效，且来源 `design_revision` 与开始操作时相同、未提交编辑 epoch 未变化。controller 最后调用现有来源入口 `applyOptimizedDesign(candidate_design, base_design_revision, base_edit_epoch)`。来源会原子检查 revision 与编辑 epoch；冲突时保留用户设计并将候选标记为过期。成功后更新原 session 的 snapshot，并通过现有来源变更信号让模拟页刷新和重新计算。worker 不创建新窗口、不直接写 session。

`tests/test_film_structure_queue_ui.py` 覆盖了真实 scheduler worker 的结构请求重放、typed checkpoint/final envelope、严格 final/checkpoint 标志、operation token/revision/edit-epoch 防护、失败/取消/holdout pending 禁止应用，以及真实来源 session 写回后通过既有模拟 controller 执行一次数值模拟并交付一次结果。该测试不声称覆盖 MainWindow 的可视端到端流程。

窗口和控件沿用 [共享 Qt Quick 页面与控件规范](quick-theme-controls.md) 与 [窗口几何策略](window-geometry-policy.md)。此契约不更改共享主题、标题栏、主窗口或 launcher。


取消采用协作式检查。搜索在候选与阶段边界轮询取消标记；完整网格评估在每个角度的每个波长分块开始前检查。已开始的 helper 返回后才会观察到取消。被中断的候选未完成完整评分，不会作为 incumbent 或 checkpoint 归档；此前已完成评分且通过验证的 incumbent 可保留查看，holdout 未完成的候选仍不可应用。
