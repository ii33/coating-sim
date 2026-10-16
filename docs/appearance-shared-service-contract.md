# 外观共享计算入口契约

日期：2026-10-12；离线时限与 CUDA 接入：2026-10-13；源码边界文字核对：2026-10-14。实现前冻结的 CAP-02 契约；验收记录另存 `docs/remediation/20261012-appearance-shared/`。

## 输入与状态所有者

MCP 增加 `estimate_appearance_render(request: dict[str, Any])` 与
`submit_appearance_render(request: dict[str, Any], client_request_id: str | None = None)`。
外层严格拒绝额外字段、类型强制转换、NaN/Infinity。request 只接受：

- `asset_path`：获准项目 `data` 内的 `.coatasset`；解析实际路径后检查边界，拒绝 symlink 越界。
- `asset_sha256`：64 位小写十六进制预期内容摘要。
- `render_inputs`：必须含 `wavelength_nm`、`camera`、`area_source`、`environment_spectrum`、`resolution`；唯一额外可选字段是 `circle_boundary_sampling`，值只可为 `captured_node_mean_with_local_nearest_fallback`。带膜圆形基片需显式该预览采样约定，不能称实测边缘厚度。物理有效性与采样上限由现有 `appearance.contract.validate_job` 唯一定义。
- `geometry_thickness_mode`：默认 `film_design_snapshot`，或 `explicit_override`；后者须提供正有限 `geometry_thickness_mm`，前者不接受覆盖值。
- `display_exposure`：默认 0.01，正有限，仅影响显示映射。
- `backend` / `precision`：可选。默认 `cpu` / `native`；显式 `gpu_cuda` 只接受 `float32` 或 `mixed`，默认 `mixed`。选择 CUDA 不静默降级 CPU，结果仍为 unqualified。CPU 的默认 aggregate 标量预算保留，CUDA 只解除该 aggregate 预算，其余域/像素/SPP/诊断限制照旧。实现与硬件限定证据见 [GPU 契约](gpu-preview-contract.md) 和 [产品接入记录](gpu-product-integration-evidence-20261013.md)。

模型不能提供 asset_root、上下文、冻结包、job_id、输出路径、worker、任意执行路由、批准标志或队列模式。公开 backend/precision 只在上述枚举内选择，不能指定 worker 或远端。服务构造坐标单位和基板几何，复用 v7 资产/膜系/逐层场适配器。长度与坐标沿用现有契约：几何 mm、逐层场 nm、波长 nm；不新增物理公式。

只读发现工具 `inspect_appearance_asset(asset_path: str)` 严格只接受一个路径字段，复用上述root/data/.coatasset边界和既有私有快照。返回字段恰为 `schema_version=1`、`root`、规范化 `asset_path`、`asset_sha256`、`asset_bytes`。整体SHA和大小来自同一快照；资产合法性摘要读取也只解析该快照，不从成员摘要拼凑或读取另一份源字节。该工具不表示v7/逐层场一定可渲染，领域有效性继续由estimate判断。源在快照后变动时描述仍为当时快照，后续estimate按预期SHA拒绝变动并要求重新发现。

固定 headless 冻结子进程复用 `ui.appearance_worker` 的资产验证和构 job 函数，并调用现有 validate_job。该函数临时替换光学求值器并输出状态，故禁止在 MCP 进程内直接调用。子进程不渲染、不访问网络、不加载用户代码；固定模块与参数。2026-10-13 大场接入修订：公开控制请求及冻结输入 JSON 仍至多 16,000,000 UTF-8 字节，保留完整逐层场的冻结输出与队列 spec 则分别有 60,000,000 字节上限，真实紧凑序列化的本地 RPC 消息仍须低于既有 64 MiB 服务门禁。文件读取前检查大小，超限明确失败，不抽样逐层场。离线冻结与渲染均不设默认壁钟时限，存活的冻结子进程可以继续工作；进程退出立即读取返回码并检查输出，非零退出、缺失/非法输出仍报真实失败，不等待已退出的子进程继续计算。执行阶段既有取消边界、IPC 失效/进程退出检查和资源安全校验不变。失败在创建估算/队列记录前返回；不改变沙箱、网络或工具超时。

服务内部 `appearance_render` 冻结包绑定原始规范请求、服务根、资产摘要、上下文摘要和原始渲染 job 摘要；公共工具不接受该包。执行前在隔离 worker 重读资产并重建，逐项核对摘要。资产变动须重新估算，不能沿用旧批准。

ComputeTaskStore 是估算、稳定请求 ID、批准与输入摘要的唯一所有者；SchedulerStore 是执行、进度、取消、终态和结果的唯一所有者。复用现有 `claim_unbound_estimate` 和 `submit_confirmed`，不另建外观状态机。默认 ID 为规范冻结输入摘要派生；同 ID 不同输入报冲突。估算耗时为 unknown/null，报告 width/height/spp/wavelength 数量及现有标量预算，始终走现有真实来源与手动批准策略。

## 路由、输出与取消

本共享公共入口仅支持现有默认项目本地 `SchedulerClient`，公共输入没有 transport 选择。服务对象额外注入的路由明确失败，不静默改投本地。现存远端外观路径属于单独的远端服务/工作区契约，不能据其存在向此公共 adapter 注入路由；也不暴露为通用 MCP estimate kind。

estimate 输出沿用 ComputeTaskStore 的公开回执：`estimate_id`、`kind=appearance_render`、`input_digest`、`summary`、`estimated_s=null`、`sample_count=0`、`operations` 与既有状态字段。submit 初次/认领输出同一回执，加 `client_request_id`；`state=prepared`、`needs_release_request=true`、`next_action=request_compute_confirmation`。已请求批准但待确认时为 `awaiting_confirmation`；批准后原提交链返回原 `job_id`/状态。查询及结果继续使用已注册 `find_calculation_job`、`calculation_job_status`、`calculation_job_result`，取消使用 `cancel_calculation_job`；新工具不能批准、取消或重写其他工具。

内部参数为 `{frozen: envelope}`。envelope 字段恰为 `schema=coat_sim.appearance_frozen_request`、`schema_version=1`、`root`、`request`、`context`、`context_sha256`、`job`、`job_sha256`；摘要为排序、紧凑、UTF-8、禁止非有限值的 canonical JSON SHA-256。完整输入摘要由原 ComputeTaskStore 计算。

输出由程序唯一生成在 `<root>/data/tmp/appearance/<scheduler-job-id>/render`；已有目录拒绝覆盖。现有 renderer 原样产生 radiance_spectrum.npy、xyz.npy、linear_srgb.npy、display.png、manifest.json。完成验证检查每个文件的限定路径、非 symlink、大小、摘要，以及 NPY dtype/shape、PNG 尺寸与 manifest 的分辨率/波长/显示曝光绑定。完成索引原子写入 scheduler job 目录；持久队列结果保存其 SHA-256，不能由 data 中可编辑文件自证成功。

只接受 succeeded 终态且通过重新验证的完整外观结果；排队、运行、失败、取消和部分帧没有可读取的外观成功结果。外观不是优化候选，不生成 best_candidate、不自动应用到设计、不改 GUI 最近预览。晚到取消仍沿用 scheduler 终态，不能把磁盘残留改报成功。

新成功结果的 `outputs` 包含 `display`、`manifest`、`radiance_spectrum`、`xyz`、`linear_srgb`；每条字段恰为 `path`、`sha256`、`bytes`、`media_type`。path由服务生成并验证，hash/大小绑定完成索引的对应文件；类型为image/png、application/json或application/x-npy。模型直接使用返回引用，不按job目录猜路径。旧无outputs封套仍可通过原完整校验；若携带outputs则每条必须与同一已验证索引相符。既有 `calculation_job_result` 原样返回该封套，API不扩展任意文件读取权限。

取消复用 FileCancelEvent；确认收到取消请求不代表已终止。现有 renderer 在波长帧之间检查，单帧内无法保证立即退出；完整 postprocess/验证后还检查取消。预览/结果只读通过原有任务状态与结果入口取得。

## 验收门槛

实际 SDK 发现/调用两个工具；严格输入与拒绝资产越界/摘要错/非法预算；估算、稳定 ID、真实来源批准与同 ID 冲突；明确拒绝远端/未知路由；新建合成 v7 资产的冻结/重建；小尺寸真实 renderer（依赖可用时）；完整输出摘要/形状检查与损坏拒绝；运行取消保持运行直至 worker 终态、无部分成功；固定子进程硬 guard 防止测试意外 daemon。再运行现有 typed film 8 项回归并核对其 helper 和两个工具函数未改动。

当前实现位置：`core/appearance_compute.py`、`core/appearance_freeze_worker.py`、`core/appearance_result.py`，以及 MCP/compute_estimation/scheduler 的薄分支。Renderer、process_adapter、Qt 页面与薄膜物理算法不修改。本契约解释接口边界，代码类型、签名和契约测试是接口真值。
