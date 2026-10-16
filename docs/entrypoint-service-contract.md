# 看板娘、MCP、CLI 共享能力契约

日期：2026-10-12。能力现状及未覆盖项见 [能力矩阵](entrypoint-capabilities.md)。原实际运行与交付 hash 记录 `entrypoint-service-evidence-20261011.md` 已从当前检出移除；本文是接口契约，不替代历史运行证据。本次只收口已经验证的调用边界，不改变物理算法、数值语义、文件格式或计算授权。

## 输入、输出和状态所有者

`coat_sim.core.capabilities.CapabilityService(service, scheduler=None)` 由调用者注入受项目根目录约束的 `CoatService`。`call(operation, arguments=None)` 只接受固定白名单中的具体操作和 JSON 对象，检查缺失/未知参数、严格布尔参数后调用已有领域服务。不存在按任意方法名执行、任意 Python、窗口控制或模型调用入口。`catalog()` 返回方法名、参数、状态、副作用和权限约束；代码签名与契约测试为真值。目录列出能力不代表请求已授权。

`execute(operation, arguments=None)` 返回 `{ok, operation, result}` 或 `{ok:false, operation, error:{code,message,effect}}`。业务校验结果如 `valid:false` 保留在 result，不与调用失败混淆。`CapabilityError` 保留 `ServiceError.code`；类型/结构错误为 `invalid_arguments`，领域输入错误为 `invalid_request`，文件读写错误为 `io_error`，未知操作为 `unavailable_operation`。错误文字使用既有脱敏方法。尚未执行的请求 effect=none；写入开始后发生无法判定的失败为 unknown，禁止自动重放。膜系 JSON 发布失败会明确保留并报告已保存 CSV 的路径。

领域数据由现有设计/材料/资产服务拥有；新层不缓存草稿或建立第二份 GUI 会话。同步膜系计算接受调用者显式提供的设计对象，不能从桌面窗口偷取当前未保存内容。内存会话同步仍由现有膜系控制器/会话契约管理。

任务状态由已有 scheduler 独占。查询/结果/取消复用原任务 ID，不复制任务或另建队列。取消请求的返回值不是取消终态，必须继续查询。结果仅在原服务允许的终态取得，数组/产物仍按既有 artifact 协议显式读取。此层不注册任何提交/批准能力，查询可能按现有客户端策略启动本机调度服务；CLI 退出不取消现存作业。

## 薄入口和授权

MCP 保留具体已注册工具的类型签名；选定工具委托共享层，并将共享错误转成带同一错误码的 ToolError。看板娘继续用 ProjectGateway 的当前允许工具集合、模式放行、去重、未知副作用核对和输出预算。不会新增通用 MCP `call` 工具或从共享目录自动扩大看板娘白名单。

MCP 的 UI 控制模块和客户端只在第一次显式窗口工具调用时加载/创建，由同一 `_ui` 客户端持有 IPC 状态；实际窗口/草稿状态仍归桌面 UI 所有。输入仍是原操作名与参数，输出仍是原 `request` 回执，`UiControlError.code` 原样转换为 ToolError。创建由锁保护；业务工具和目录发现不初始化 UI 客户端。窗口操作原有异步结果、确认和取消边界保持现有窗口规范，不增加新的窗口权限。

`CapabilityMCPServer.call_tool` 在 SDK 的类型转换/字段过滤前调用 `validate_arguments`；`list_tools` 仅为已有具体注册工具提供共享签名生成的 schema。看板娘也在发送这些调用前执行同一无副作用参数校验。调用发出后的写入失败保守标记 unknown；错误文字中的 effect 声明不能降低此判断、解除重放封锁或授予权限。

参数中的签名默认值先归一化，再计算看板娘去重/失败计数身份；省略默认值和显式传入默认值属于同一次业务请求。`find_calculation_job` 的 `client_request_id` 是查询输入，须保留在该身份中，避免无效 ID 的失败次数阻止另一个合法 ID。提交工具的请求令牌去重规则保持原样。

`python -m coat_sim.capability_cli --root <项目目录> catalog` 读取共享目录；`... call <具体操作> --arguments '<JSON对象>'` 调用同一层，结果为一条 JSON，成功 exit=0，失败 exit=2。也可从 stdin 输入 JSON 对象；不加载设置/聊天凭据、不依赖 Qt/MCP SDK/LLM。仅本地 CLI 调用者显式给出的 root 生效，MCP 的根目录权限不会因此扩大。

已有 algorithm_cli、ray_trace_cli、scheduler CLI、appearance CLI 保留；计算提交仍走这些入口各自已经存在的授权/冻结/队列契约。共享层不暴露通用提交、批准估算、终止进程、删除文件、外部 MCP、研究库根目录连接或远程执行。

所有写入继续使用既有服务的 root/path、覆盖和确认校验；布尔值不能用字符串或整数替代。材料写入与正式保存本轮未加入新 CLI 白名单。正式资产保存、设计临时转正式等门禁仍由原入口管理。资产读取不授予归档解压、跨根路径、任意文件读写权限。

此处“正式保存”指现有 `save_generated_design` / `save_simulation_asset` 的确认流程；`save_film_design` 仍是已有显式路径的膜系文件保存能力，默认不覆盖，已在三入口共享白名单中。材料修改、资产正式保存和设计依赖图转存没有加入新 CLI。

## 膜系产物边界

`coat_sim.core.film_result_service` 承担原 MCP 的直接计算与 JSON/CSV 产物编排，调用 `CoatService.validate_film_design/simulate_film_design/export_film_csv`。输出默认新建 `data/results/film_<uuid>.json` 及同名 CSV；显式路径必须是 `data/results` 直接子级 `.json`。覆盖默认 false；先校验目标再计算。返回既有 summary、warnings、artifact_paths、input_sha256、artifact_payload_sha256；哈希和统计定义原样保留。同步调用没有 job_id、队列状态或取消 API；若需要可取消后台膜系任务，应使用既有优化/队列契约，不伪装成同步操作支持取消。

CSV 与 JSON 分别原子发布，不承诺两个文件的跨文件事务。CSV 成功但 JSON 失败时保留 CSV 并报告部分副作用。调用者核对产物后再决定重试。本次不新增采样限制，不修改另一并行任务拥有的点数校验路径。

Windows 的新 JSON no-clobber 发布使用同目录 `os.rename`，目标已存在时失败；POSIX 保留硬链接发布。显式覆盖仍由既有 `replace_with_retry` 完成。哈希、摘要统计与文件内容定义没有改变。

## 验证与剩余边界

通过实际 CLI 子进程、MCP 已注册工具、看板娘 ProjectGateway 的离线调用，验证选定操作输入/输出/错误码、非法路径和覆盖一致性；禁止导入 PySide6/Qt、GUI、模型运行时的进程仍须能够调用共享层和 CLI。队列生命周期另以注入的隔离调度存储/客户端验证，不接管用户正在运行的服务。

研究库当前需显式 provider 与 model-context 双门禁；未统一注册。外观渲染和沉积提交保留各自生命周期/授权边界；新增膜系厚度 typed MCP 接入见下节，不增加原共享 CLI 22 项白名单，也不表示助手角色已获准调用。矩阵覆盖更广，实际调用范围以注册、角色白名单和契约测试为准。

## 2026-10-12 整改：执行阶段与队列路由

共享接口沿用具体操作、参数签名及 `{code,message,effect}` 错误封套。输入结构严格校验之后、业务操作调用之前，save/simulate 可执行明确的只读 preflight：复用既有 `_path`、`validate_design_path`、`film_artifact_path`、目标/no-overwrite 检查和膜系 validator；失败为 effect=none。不会按错误文字或全局错误码推断阶段。预检只复用领域规则，不写文件、不建立队列或改变数值计算；实际领域方法继续拥有并复核路径/设计/覆盖。

预检使用独立 invocation 参数副本，不修改调用者对象或 `validate_arguments` 的去重身份。默认膜系输出的 UUID/目标只选一次；同一目标交给现有领域发布器。进入具体 operation 后出现的普通异常，即使来自一个自称 none 的 CapabilityError，也按非 read 策略保留 unknown；CSV先发布/JSON后失败仍报告部分副作用，不能自动重放。cancel 操作不因本地输入看似无效就推定未派发。

`CancelledError`/`KeyboardInterrupt` 等中断保留原异常，不伪装为普通成功/校验拒绝。没有收到共享回执时，调用者不能推断 effect=none：MCP/助手请求一旦派发后失联或取消，写/取消操作必须进入未知效果核对屏障，且保留原取消异常；只读取消不因此建立写入屏障。Gateway不能用任意文本里的 `[coat_capability_error]` marker作为可信的未执行证据；本地共享类型/可验证来源的前置检查与已派发的未知结果是两条边界，AI组负责其重放与取消状态。

五项共享队列操作的 catalog 追加 route 元数据：`{selection:"default_local"|"injected", target:"local"|"caller_owned", remote_selection_from_cli:false}`。发现不会初始化 scheduler，也不披露endpoint URL/凭据。默认使用当前project root的本机SchedulerClient；明确注入的客户端由调用者拥有transport和认证，不猜其远端身份、不自动回退本机。若注入客户端显式root与业务root不一致，在队列访问前以 invalid_queue_route/effect=none拒绝；没有root属性的兼容fixture/adapter仍由注入方保证作用域。

本CLI不提供远端路由选择。`--root` 的HTTP(S) URL在构造CoatService/调度客户端之前以 unsupported_queue_route/effect=none、JSON及exit=2拒绝。正常本地路径与UNC文件系统挂载不当作远端调度API；认证、TLS、安全设置和scheduler实现均不改变。MCP队列工具说明明确其默认本机范围；原工具参数签名保留。

schema/model缓存须先测新建catalog和双重参数验证成本，且只在有意义时引入。可共享静态参数model，不能缓存root、会话、结果、scheduler、publisher或凭据；每次发现返回可独立修改的schema对象，严格bool/未知字段校验保持一致。基准/接口回归证据写新整改目录，不覆盖原审计证据。

## 2026-10-12 膜系厚度优化 typed MCP 估算接入

接口采用 `docs/assistant-typed-operations-contract.md` 的 `estimate_film_optimization(request)` 和 `submit_film_optimization(request, client_request_id=None)`；request 是 `film_optimization_job.serialize_request()` 的厚度请求 JSON，不是 JobSpec 或 worker 参数。结构搜索请求当前明确拒绝，不转换为厚度请求。原 22 项有限目录不增加提交操作；两项具体 MCP 仍需助手角色的显式 allowlist 授权，目录发现不授予权限。

`core/film_compute.py` 负责 JSON/typed 转换及估算/提交适配，复用 `deserialize_request`、`_validate_replay_request`、`_validate_optimization_controls`、`freeze_job_payload`。存储参数固定为 `{request: 序列化冻结请求}`；设计 revision 与材料 SHA-256 保留，估算回执摘要绑定全部规范化参数。不存在匹配实测标定时 `estimated_s=null`、`sample_count=0`；预算仅报告原请求控制，不新增时间或工作量预测，不加入短时自动放行种类。

状态唯一所有者是既有 `ComputeTaskStore` 和 scheduler。提交复用 `scheduler.submission.submit_confirmed`；首次返回 prepared、estimate_id 与 client_request_id。显式 estimate 后 submit 仅能复用参数摘要一致且尚未绑定的 estimated 回执；`ComputeTaskStore.claim_unbound_estimate` 在同一个 SQLite 写事务中检查并认领，多个新 ID 最多一个认领该回执。已绑定、待确认或已批准回执不能被另一个新请求自动认领；相同 ID 的并发重试仍沿原 alias。随后 `request_compute_confirmation` 绑定真实会话，再由原桌面确认/策略定时器放行。模型不能自报 mode/session/worker/backend 或批准 boolean。`scheduler/submission.py` 仅在已有授权后用原 decoder 把 film JSON 请求还原为 typed 请求交给现有 freeze_spec；调度器执行、worker、预算与数值计算不变。

原 ID 绑定的参数变化时拒绝；未提供 ID 的直接调用按规范化冻结请求摘要生成稳定 ID。助手运行时注入的原 ID 优先，重试不新建任务。拒绝/过期/停止的回执沿原门禁处理。取消仅表示提出请求，终态及结果继续从同一 SchedulerClient 读取；适配器不应用候选、不推断 targets_met、不建立第二份候选账本。派发前校验有本地阶段证据时 effect=none，派发后失联/取消的 mutation 由原 Gateway 保守保持 unknown 并核对原任务。

MCP 外层 strict schema 在 SDK 派发前拒绝未知字段；深层请求由现有 decoder/预算/冻结验证负责。输入错误在入队前拒绝。验收使用独立合成材料、临时会话和 fake scheduler transport，区分实际注册 MCP + 回执/会话闭环与真实 daemon/硬件；不把 schema 测试、取消回执或 scheduler succeeded 当作数值达标证明。

## 2026-10-12 外观本地共享入口

`estimate_appearance_render(request)` / `submit_appearance_render(request, client_request_id=None)` 的字段、状态所有者、完整结果校验和取消边界见 [外观共享计算入口契约](appearance-shared-service-contract.md)。只接获准data中的v7资产和现有渲染参数；固定冻结子进程隔离既有资产适配器的临时全局替换，真正渲染仍走原scheduler worker。通用MCP估算kind、原22项CLI目录和远端executor白名单不增加外观。薄膜接口保持冻结；外观验收单列，不沿用膜系40项回归作为其完成证据。

`inspect_appearance_asset(asset_path)` 从同一受限私有快照返回整体asset_sha256/asset_bytes及规范root/path，供构造上述请求；不是任意文件读取接口。成功结果的outputs引用由服务生成并逐项绑定完成索引，模型不猜图片目录。共享本地验收及诊断限制见 `docs/remediation/20261012-appearance-shared/REPORT.md`；角色链仍由AI作者独立验收。
