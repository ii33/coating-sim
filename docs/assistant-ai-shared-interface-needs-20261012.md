# AI 适配与共享入口协调记录

日期：2026-10-12 UTC。AI 作者只持有 runtime/provider/assistant 和本任务独立契约测试；共享 capabilities/MCP/CLI、生产算法、队列与 GUI 由其他作者持有。本文记录最小依赖，不注册能力，也不授权执行。

## CAP-01 已收到的真实接口

`coat_sim.core.film_compute` 和 `coat_sim.mcp.server` 已提供 `estimate_film_optimization(request: dict[str, Any])` 与 `submit_film_optimization(request: dict[str, Any], client_request_id: str | None = None)`。外层严格拒绝额外字段，内层复用 v1 FilmOptimizationRequest decoder、冻结与重放校验；原 estimate/真实会话放行/scheduler 链提供唯一 job 状态。本 AI 分支仅显式纳入主助手和 simulation 执行角色，继续复用稳定请求 ID、ledger 和原放行回调。设计与 correction 角色不取得 film submit 权限；没有 apply 工具。

共享有限 CapabilityService catalog 与独立 MCP 注册仍是不同入口。不能以 catalog 没列出 film 操作推断 MCP 没注册，也不能因 MCP 列出就自动授权所有角色。独立 AI 适配阶段的历史证据文档已从当前检出移除；现有 [共享入口契约](entrypoint-service-contract.md) 与 [typed operations 契约](assistant-typed-operations-contract.md) 描述当前接口边界，不替代那份历史运行证据。

## CAP-02 待共享服务边界

当前可复用服务为 `appearance.contract.validate_job`、`appearance.render.render_job`，以及不加载 Qt 的 `ui.appearance_worker` 冻结 asset/profile 解析和完成校验逻辑。现有 CLI 同步路径和 GUI 的 QProcess/cancel marker 生命周期都不提供助手可共享的 estimate、job 身份、结果查询或真实会话放行。本分支不复制计算、不建另一套队列、不直接把 CLI 路径或 GUI 内部状态暴露给模型。

如果共享作者将其纳入助手产品能力，建议固定接口为 `estimate_appearance_render(request: dict[str, Any])` 和 `submit_appearance_render(request: dict[str, Any], client_request_id: str | None = None)`；这是待实现建议名，不是当前公共 API。实现必须先确定以下边界：

- 内层 request 使用既有冻结 appearance job / launch context 真值，明确来源 asset SHA-256、冻结 v7 process profile SHA-256 与厚度场来源；只从获准 data root 解析资产，不接受任意系统路径、worker 命令或模型自报批准。
- 复用既有校验与 20,000,000 标量样本上限、像素/波长/spp 上限；未知运行时长如实返回 null，先绑定同一不可变输入估算，再经真实 session/request 与持久化 UI 放行策略提交。
- 唯一生命周期持有者由共享服务明确。若接入现有 scheduler，沿用原 job/find/status/cancel/result；若未接入，不能合成 scheduler 状态。取消回执不等于终态，派发丢失由原 client_request_id 查询恢复。
- 输出目录由服务在获准 root 内创建独立新目录，拒绝覆盖；只接受完整完成 manifest、输入 hash、产物 digest 一致的结果。半成品不能当成功，取消/失败应保留错误与实际已验证产物，不覆盖最后有效预览。
- display exposure 只影响显示映射，来源 revision/材料快照/厚度场不得改写。依然不能自动 apply 设计或用实时来源替换冻结输入。

这些边界需要共享服务和其契约测试先落地，然后 AI 作者才能加固定角色 allowlist、稳定 ID 与真实调用闭环。当前 CAP-02 保持待协调，未做不完整注册。

## HPC 已有门禁

worker 仅在 `COAT_SIM_FILM_FORWARD_CACHE=1` 时使用现有可选缓存，默认关闭；当前 adapter 强制 CPU 单 worker。AI 复用 film submit 路径，不添加 backend/device/profile 参数，不自行开启缓存。HPC-01 真实 GUI→queue→worker→result 的响应与收益、HPC-02 目标 CUDA FP64/complex128 均未验收，不能以 CPU 源码/测试代替。
