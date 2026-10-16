# 助手 typed 操作接入建议

日期：2026-10-12。状态：**CAP-01 专用 MCP 接口已注册并接入助手主通道与 simulation 执行域；隔离合成端到端契约测试已新增**。这些测试不调用外部模型或真实调度器守护进程，也不代表 UI 弹窗与真实后台 worker 的系统级验收。外观渲染仍未接入。`project_tools` 发现工具不等于获准调用；唯一接口真值仍是注册函数签名、角色 allowlist、数据结构和契约测试。共享 `CapabilityService` 目录当前仍未列出这两个 film operation；它们是 `coat_sim.mcp.server` 的专用注册工具，不能描述为共享 capability catalog 项。

## 膜系优化：复用现有队列执行器

当前可复用的数值请求是 `coat_sim.core.film_optimization.FilmOptimizationRequest`，请求含 `design_snapshot`、`design_revision`、稳定层 ID 变量、目标、`max_iterations`、`max_evaluations`、solver 和 seed。队列边界由 `coat_sim.core.film_optimization_job.freeze_job_payload(service, request)` 生成 v1 冻结 payload；桌面队列以 `freeze_spec(service, "film_optimization", {"request": payload})` 产生现有 worker spec。`scheduler.worker._film_optimization` 调用 `execute_film_optimization_job(...)`，调度器提供 job ID、取消信号、进度、检查点与终态状态；结果必须经过 `validate_film_optimization_result` / `validate_checkpoint` 的既有重放验证。

实际注册签名是 `estimate_film_optimization(request: dict[str, Any])` 与 `submit_film_optimization(request: dict[str, Any], client_request_id: str | None = None)`，实现位于 `coat_sim/mcp/server.py` 并由 `coat_sim/core/film_compute.py` 适配。运行时把 submit 身份固定为 `<request_id>:<tool_call_id>`，同时记录 job intent；模型不能提供或改写该标识。两个业务操作的输入都只含序列化 `request`；submit 另收运行时注入的幂等令牌，没有 mode/session/worker/backend/approval 字段。MCP 外层 schema 由注册签名生成，且 server 使用 `FilmEstimateArguments` / `FilmSubmitArguments` 的 strict Pydantic 校验拒绝额外字段与数值 coercion；`request` 的内层 shape 由 `film_optimization_job.deserialize_request()` 验证：必需字段为 `schema_version`、`kind`、`design_snapshot`、`design_revision`、`variables`、`objectives`、`max_iterations`、`max_evaluations`、`initial_step_fraction`、`min_step_fraction`、`acceptable_normalized_error`、`frozen_material_snapshot`；可选字段为 `solver`、`seed`、`multistart_count`、`population_size`、`validation_samples_multiplier`。`FilmOptimizationRequest` / `serialize_request()` 是字段和默认值来源，不在 MCP 中再实现另一套参数别名或 coercion。

实际 MCP schema 与下方一致；契约测试从 registry 读取并核对，`request` 的深层类型由上述现有 decoder 校验，不应误称空字典 schema 已完成深层约束。

```json
{
  "type": "object",
  "required": ["request"],
  "additionalProperties": false,
  "properties": {
    "request": {"type": "object"},
    "client_request_id": {"type": ["string", "null"]}
  }
}
```

估算接口的 properties 只有 `request`，同样 `required: ["request"]` 且 `additionalProperties: false`。`request.design_revision` 是输入来源版本；freeze 后 `request.frozen_material_snapshot.sha256` 是序列化设计与所引用光学材料快照的摘要，应随冻结 payload 原样保留；不要用实时材料目录重读来替代。调度器的 `input_digest` 是完整 JobSpec 的另一个摘要，不能与材料快照摘要混称。`estimate_film_optimization` 调用 `CoatService.estimate_compute_task("film_optimization", frozen_parameters)`，未知历史时保留 `estimated_s: null` 和 `sample_count: 0`；回执绑定规范化的冻结请求。submit 只复用输入摘要完全一致且仍处于未绑定 estimated 状态的回执；同一 `client_request_id` 输入变更返回 `request_id_conflict`，pending/approved 回执不能被新 ID 认领。

返回沿用现有队列回执，至少保留 `client_request_id`、`job_id`、`state` 和估算回执引用。查重必须使用程序注入的原 `client_request_id`，同一请求重试不得创建第二个 job。已有队列查询/控制操作应继续复用 `find_calculation_job(client_request_id)`、`calculation_job_status(job_id)`、`cancel_calculation_job(job_id)`、`calculation_job_result(job_id)`；四者以同一调度器记录为唯一状态来源，不在 assistant adapter 建第二本账。

助手接入的 allowlist 已显式更新：film estimate 属于只读集合；submit 只属于主助手与 `simulation` 执行域，并进入 `SUBMIT_TOOLS` 以复用原始身份、ledger 和自动 release-request 流程；design、correction 没有 film submit 权限。`ProjectGateway` 先用 registry schema 做参数预检，server 仍执行独立 strict typed 校验；schema 发现本身不改变权限。runtime 在 prepared receipt 请求 release 时，把真实会话和当前 request ID 注入既有 `request_compute_confirmation`；mode 由持久化 UI 事件读取。聊天中该 film 估算未知时返回 `awaiting_confirmation`，不创建 scheduler job；模型不能提交 approve 参数。仅由受信任桌面/策略回调决定 accepted/declined，不能用默认模式或模型自报预算替代。

预算继续由 `FilmOptimizationRequest` 及核心校验定义：默认 200 iterations、1000 evaluations、最多 8 个厚度变量和 20,000 个 wavelength-angle 条件；现有请求上限为 10,000 iterations 与 100,000 evaluations，seed 是 0–4294967295。solver 专属字段沿用现有请求验证，不静默替换 solver 或回退到 baseline。新 wrapper 不得创建第二份评估预算或在队列之外逐点调用 `simulate_film_design`。具体规模估算必须使用冻结后的请求；缺 estimator、超限、材料快照错误或调度器不支持 executor 时均在提交前返回明确错误。

结果状态与候选质量必须分开。Scheduler outcome 的终态是 `succeeded`、`cancelled` 或 `failed`；核心 `FilmOptimizationResult.state` 是 `completed`、`cancelled` 或 `failed`。两层原样保留，不能把 core 的 `completed` 改写为 scheduler `succeeded`，也不能把后者当作目标已达成。`targets_met` 和 `validation_*` 按核心实际结果原样保留，不从成功、失败或取消状态推导达标布尔值。取消/失败时，若队列返回已有通过重放验证的检查点，可同时返回 scheduler 原终态、错误和候选；只有核心 `partial_result` 实际含 `best_so_far` / `candidate_verified` 时才传递这些字段，wrapper 不合成它们。没有可验证检查点时，不生成候选。取消工具回执只表示提出取消；只有之后读取到 scheduler 终态才称作结束。assistant 操作只提交、查询、展示或导出候选，不调用 `FilmDesignEditSession.apply_optimized_design`，不改变来源设计。

`ProjectGateway` 在 MCP mutation 已 dispatch 后遇到 transport failure 或 task cancellation 时，统一保留 `effect=unknown` 的恢复记录并阻止同一业务请求重放。调用方先用原 `client_request_id` 查找既有 job，再据状态决定下一步；不得以新模型 tool-call ID 或新 `client_request_id` 掩盖同一 request。mutation 身份在 gateway 内由操作名和规范化参数决定，去重身份不含 `client_request_id`；find 查询则保留它作为实际查询键。读取操作取消不记录 mutation 屏障。该本地网关保证仅防止同一会话 guard 内重放，不替代 scheduler 持久化的请求去重。

### 使用流程

1. 主助手通过 `project_tools` 读取这两个实际 schema；simulation 执行助手使用其获准工具。先取得真实设计快照、来源 revision、材料数据和目标，再沿用现有 `serialize_request` 格式，不猜填来源版本或冻结 hash。
2. 调用 estimate，再提交同一 request。运行时负责 submit ID、ledger 及 `request_compute_confirmation` 的真实 session/request；调用者不要加入 approve、mode、worker 或 backend。没有本机历史时耗时为 null。
3. `awaiting_confirmation` 时等待系统放行，并结束当前计算等待；可以继续其他对话。取得实际 job ID 后使用原 find/status/result 查询；prepared、pending、queued 和 cancel receipt 都不当作已完成。
4. 读取 scheduler 终态与 typed 优化结果，分别报告错误、核心状态、验证后的最佳候选和实际 `targets_met`。取消后再次查询终态；丢失提交回执时先按原 `client_request_id` 恢复。助手不会自动应用候选，来源设计仍由原编辑会话的 revision 保护负责。

## 外观渲染：当前保留独立入口

真实可复用接口是 `coat_sim.appearance.contract.validate_job(job, *, max_pixels=1_048_576, max_wavelengths=241, max_spp=4096)` 和 `coat_sim.appearance.render.render_job(job, output_dir, *, normalized=False, cancel_event=None, display_exposure=0.01)`。输入是自包含 schema v1 appearance job；基础工作量上限另有 20,000,000 个像素×spp×波长标量样本。CLI `coat_sim.appearance.cli.main(argv=None)` 同步调用 render，写谱、XYZ、sRGB、PNG 和 `manifest.json`；CLI 本身没有 job ID、查询或外部取消协议，也没有独立 RSS/输出总量硬上限。

现有 Qt 窗口的 controller 持有自己的 QProcess、冻结输入 hash、cancel marker 和最后一个已验证预览。worker 在完整波长帧之间检查取消；缺少有效完成 manifest、job ID/input hash/image digest 校验失败时不接受半成品。这个本地窗口生命周期不等于 MCP / scheduler 服务。CAP-02 是可选产品能力；当前没有 assistant/MCP wrapper。若之后明确纳入产品，应先提供受项目 root 约束的唯一输出目录、显式工作量预算、局部取消 owner 和仅从完成 manifest 返回结果的 adapter，并验证不加载 Qt 的执行路径；不得直接把本地 CLI 路径参数暴露给 assistant，也不得声称它已有 scheduler 任务或复用 GUI 窗口状态。

## HPC-01：显式性能路径与接入门禁

队列 executor 已支持 `enable_forward_cache` 和 `performance_callback`，worker 默认 CPU worker 数为 1；缓存由 `COAT_SIM_FILM_FORWARD_CACHE=1` 显式 opt-in，默认不启用。该开关复用现有 executor，不需要新 AI wrapper。它仍调用原 optimizer、原预算和冻结材料；助手请求不得自行选择 GPU/backend 或改变性能开关。CPU 服务级与候选 worker 测量不能证明 GUI → scheduler → result 全路径收益或 Qt UI 线程响应。GPU FP64/complex128 仍缺目标 CUDA 硬件验收。CAP-01 submit 只调用同一个 `film_optimization` executor，不自动开启缓存或宣称加速；GUI 阻塞和目标硬件验证仍为独立验收项。

## 契约测试范围

`tests/test_ai_film_operations_contract.py` 包含两层隔离验证。运行时行为测试直接调用 `BudgetAndTools.wrap_tool_execute` 与真实 `ProjectGateway`，验证主通道自动注入 submit ID、prepared 后以真实 runtime session/request 发起 release request、schema 额外字段在 dispatch 前拒绝、estimate 可重复读取、simulation 角色仅获得 film estimate/submit 且 design/correction 的提交被拒绝，以及 apply 工具不出现在允许 schema 中。mutation dispatch 取消后再次更换 tool-call ID 仍被同一个 gateway guard 以 `effect=unknown` 阻止。

服务集成测试调用实际 MCP registry、`film_compute`、`CoatService`、持久化 estimate/request store、现有 trusted confirmation callback 和真实 `SchedulerStore`，但 scheduler transport / materials / project root 都是测试内合成数据。它沿同一个序列化 request 验证 strict schema、冻结 `design_revision` 和材料 SHA-256、未知估时与 chat manual release、job find/status/result、`partial_result.kind=best_candidate`、候选 `targets_met` 原值，以及 cancel receipt 与 scheduler cancelled 终态的区别。拒绝结果读取和取消后无候选的错误也被覆盖。它不启动模型、MCP stdio 子进程、scheduler daemon、GPU 或 GUI；trusted confirmation callback 是测试明确传入的放行决策，不是模拟模型获批，也不替代真实 UI 弹窗与后台 worker 验收。现有 `tests/test_film_optimization_job.py`、`tests/test_scheduler_film_optimization_result.py`、`tests/test_capability_queue_route.py` 和 `tests/test_assistant_typed_operations_contract.py` 分别覆盖核心冻结/候选重放、调度器 typed result、共享队列路由与助手请求/重放边界。
