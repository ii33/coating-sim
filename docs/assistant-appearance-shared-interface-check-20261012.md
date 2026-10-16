# 外观 AI 接口准备与共享依赖核对

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

日期：2026-10-12 UTC。对应 CAP-02。复核者 复核、实现者 实施 AI 侧；保留共享代码在建期间的接口发现及最终解决方式。AI 侧独立验收与兼容回归的阶段数据是历史记录；对应的独立证据文档已从当前检出移除，不作为当前重跑结果。[外观共享服务契约](appearance-shared-service-contract.md) 描述现存接口边界，也不替代历史验收证据。

## 最终接口澄清

补充接口：`inspect_appearance_asset(asset_path)` 返回 `schema_version=1`、`root`、规范 `asset_path`、整个资产的 `asset_sha256` 与 `asset_bytes`，路径解析和摘要来自同一受限校验快照。AI 侧只读调用这一注册工具，直接使用其返回路径和摘要，不计算或猜补用户文件摘要。

成功原结果封套使用 `outputs`，键为 `display`、`manifest`、`radiance_spectrum`、`xyz`、`linear_srgb`；每项有共享验证后的 `path`、`sha256`、`bytes`、`media_type`。原 find/status/cancel/result 名称保持。下文准备期提到的 `files[]` 是曾提出的交付形式建议；最终实现以 `outputs` 为真值，不要求共享接口增造 `files[]`。AI 仅在明确外观结果分支中把这些已验证路径交给原附件注册方法，保留输出封套原值。冻结后实际服务链、微型 CPU 渲染、五项引用和损坏拒绝已通过独立验收。

## 当前接口足以准备的部分

已读取 [共享服务契约](appearance-shared-service-contract.md)、`core/appearance_compute.py`、当前 MCP 注册与原 runtime。真实签名为 `estimate_appearance_render(request: dict[str, Any])` 和 `submit_appearance_render(request: dict[str, Any], client_request_id: str | None = None)`；外层 strict、禁止额外字段。AI 侧不需要新批准 API 或新状态机。

public request 必需 `asset_path`、`asset_sha256`、`render_inputs`；后者恰含 `wavelength_nm`、`camera`、`area_source`、`environment_spectrum`、`resolution`。可选 geometry mode/mm 与 display exposure 沿用共享契约和现有 validate_job，不在 AI 侧再实现物理校验。来源资产应来自实际读取的获准 `.coatasset`，摘要不得猜填；模型不能构造内部 frozen/context/job 或输出路径。

准备固定 main/simulation estimate/submit allowlist；submit 加入原 SUBMIT_TOOLS，由 runtime 注入原 `<request_id>:<tool_call_id>`、ledger 及真实 session/request 放行。其他执行角色不取得 submit 权限；不开放新的窗口或 apply 操作。

所需回执字段与共享现有链一致：`estimate_id`、`kind=appearance_render`、`input_digest`、`state`、`estimated_s=null`、`operations`，submit 另有 `client_request_id`、`needs_release_request` 和 `next_action=request_compute_confirmation`。pending/queued/running/取消请求与 scheduler 终态必须分别报告。ComputeTaskStore 的批准输入摘要与 JobSpec 的执行摘要是不同锚点，不混称。

## 最终共享冻结所需

- 资产文件指纹须参与估算与 submit 采用的同一个完整 identity；同 ID 不同资产内容、request、曝光或采样拒绝冲突。已批准回执不能由新 ID 认领。来源变化在 release/worker 重建处继续拒绝，不能据首次校验绕过。
- 实际 registered schema 与本文字段一致；公开 submit 不能接批准、root、worker/backend/transport、job ID 或 output 字段。真实 session 放行和默认本地路由保持原所有者。
- 准备阶段发现：`data_files.file_info` 只给 size/mime/path，`read_simulation_asset` 的 summary/members 含各包内成员摘要，没有整个文件摘要。最终通过独立获准只读 `inspect_appearance_asset` 解决；不能拿成员 SHA、profile SHA、文件名或 model 自算假值代替资产 SHA，也不为此开放通用脚本。
- `calculation_job_result` 只有 succeeded 且完成索引、队列摘要和五个文件全部重新验证时返回外观结果。failed/cancelled/部分帧没有外观成功结果，不能借用 film best_candidate 语义。
- 准备阶段发现：仅有队列目录的 `result_file` 或 `result_artifacts[].local_path` 不足以安全展示渲染图片。最终通过完成校验后的 `outputs` 提供上述五项固定产物；AI 不猜拼路径、不读取任意队列文件、不自行改共享 schema。

准备期只跑 fake peer + 真实 runtime/Gateway 的独立权限、schema、身份、release 与取消测试。共享作者确认冻结后，实际 registry/service/产物校验在隔离合成资产上运行，最终结果单列；18 项共享源 hash 复核一致。MCP、core、queue、GUI 和 renderer 均不由 AI 分支修改。
