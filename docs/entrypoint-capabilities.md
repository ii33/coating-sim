# 看板娘、MCP 与 CLI 能力矩阵

初始审计：2026-10-11；当前源码边界复核：2026-10-16。范围是看板娘工具路由、MCP 注册工具、CLI 命令及其共享调度服务。本文说明入口的源码接入状态；目录可见不代表该轮、该模式或本地调用者已获准执行。讨论（兼容值chat）只读；所有写入、估算登记、提交/取消、导出、脚本及外部action均被执行层阻断，操作/攻关每次非只读dispatch还须通过UI-owned模式/代际检查，缺可信来源失败关闭。具体签名和模式白名单以代码与契约测试为准。原始运行记录 `entrypoint-service-evidence-20261011.md` 已从当前检出移除；当前定向检查及未闭环项见 本轮边界复核（历史验证记录，未随公开源码发布）。

## 入口总览

| 入口 | 入口点和授权边界 | 同步性、任务与结果 | 文件、错误和副作用边界 |
| --- | --- | --- | --- |
| 看板娘 | 主助手只使用 `project_tools` 查询当前允许目录/JSON Schema，`project_action` 经 `ProjectGateway` 调用 `coat_sim/mcp/server.py`。讨论的 `DISCUSSION_TOOLS` 是严格只读子集（排除持久估算收据），与 `READ_TOOLS`、`MAIN_TOOLS` 及执行助手的 windows/design/simulation/correction 子集不同；研究库另走 provider 双门禁。 | 沉积、修正板优化、遮挡分析和脚本先估算，再依真实 UI 模式放行；后台事件回到原会话。助手通常不循环等待，程序在后台读取任务终态。同步域工具仍会在本次工具调用内完成。 | 参数按 JSON Schema 校验；成功写入在本轮去重，传输中断或效果不明时标 `unknown` 并阻止重放，使用原 `client_request_id`/作业 ID 核对。外部 MCP 是另一个显式配置的入口，不自动发送历史、附件或项目文件。 |
| 本软件 MCP | `coat_sim/mcp/server.py` 以 stdio 暴露工具；设置页可另启绑定 `127.0.0.1` 的 Streamable HTTP 服务并复制临时 Bearer 配置。业务路径仍受默认 `CoatService` 的项目根及 `data` 限定。 | 大部分工具函数本身是同步 RPC；计算工具立即返回共享 scheduler 的估算/放行回执或作业状态。`wait_calculation_job` 最长等待 60 秒；MCP 进程退出不取消已接收作业。膜系光谱工具直接同步完成。 | MCP 参数/核心校验错误转换为 `ToolError`；成功计算返回摘要及 artifact descriptor，不以 RPC 内联 NumPy 网格为主。自动化写入受项目路径与覆盖参数约束。HTTP 只监听回环接口；它不是远程部署或跨网卡服务。 |
| CLI | 当前存在的专用入口包括 `algorithm_cli`（沉积工艺）、`python -m coat_sim.scheduler`（冻结作业/队列管理）及 `appearance.cli`（CPU 外观渲染）。`python -m coat_sim.capability_cli` 只开放下方 22 项共享 catalog 操作。历史文档记载的 `ray_trace_cli` 在当前源码快照缺失，不能给出已可运行的承诺。 | `algorithm_cli --detach` 提交后退出但任务继续；调度 CLI 用 job ID 状态/取消/取结果；appearance CLI 在进程内运行任务，不产生 scheduler job ID。共享操作 CLI 不提交或批准计算。 | 本地 CLI 以操作系统当前用户权限运行；共享操作 CLI 的 `--root` 显式指定并约束业务根目录，JSON 参数最多 32 MiB。其他现存 CLI 各用其自身输入/文件约束；错误用退出码/JSON 回执报告，scheduler 结果使用校验后的 artifact。 |

看板娘的 `ProjectGateway` 只接受当轮工具目录中的真实工具名和 schema；校验失败不会调用服务。目录单次最多读取 12 个工具 schema，开发诊断工具仅在源码开发门禁打开时出现。外部 MCP 连接与内置 `project_action` 分离，项目内容许可关闭时不能把项目数据复制到外部工具。

## 领域能力矩阵

### 膜系设计、仿真与优化

| 具体操作 | 看板娘 | MCP | CLI | 生命周期、数据和限制 |
| --- | --- | --- | --- | --- |
| 膜系设计 schema、材料目录、校验、加载、表达式展开、保存 | 可经 `project_action` 调用 `film_design_schema`、`film_materials`、`validate_film_design`、`load_film_design`、`expand_film_expression`、`save_film_design`；执行助手能力集可能不含保存。 | 同名具体工具直接注册。material/校验/加载/保存/表达式展开已注册共享服务操作，但 `film_design_schema` 仍是独立 MCP 工具。材料目录只返回元数据，不传完整光学数组。保存需 `overwrite` 显式布尔值；文件按 CoatService 根限制。 | 共享 CLI 可执行 `film_materials`、`validate_film_design`、`load_film_design`、`save_film_design`、`expand_film_expression`；它没有单独的 `film_design_schema` 操作。通用 `input_schema(kind="film_stack")` 可读沉积层表生成/导入 schema。专用膜系 CLI 无设计编辑流程。 | 同步输入/输出 JSON；校验返回规范化设计或领域错误；保存是文件副作用，默认不覆盖。光学材料的角色、波段、来源及未知 `k` 不被推测补齐。设计结构由 `film_design_schema` 和服务校验负责。 |
| 光谱仿真与 JSON/CSV 产物 | 操作/攻关可调用 `simulate_film_design`。历史独立 `film_simulation` 窗口有 `calculate` 兼容路径；当前工程绑定窗口不据此开放计算动作。讨论仅可读取已有光谱。 | `simulate_film_design` 通过 `CapabilityService.call` 委托 `film_result_service`；MCP 窗口 `film_simulation.calculate` 是单独的 UI worker 路径。 | 共享 CLI 可同步调用 catalog 中的 `simulate_film_design`；`algorithm_cli` 不执行此类仿真。 | 直接仿真同步完成，没有 scheduler `job_id`、任务状态或 cancel。默认生成 `data/results/film_<uuid>.json` 和同名 CSV；指定 JSON 必须是 `data/results` 直属文件，默认不覆盖。先发布 CSV，JSON 发布失败时 CSV 保留并在错误中报告路径。回执含摘要、warnings、路径与输入/产物 SHA-256，不把完整光谱数组放进短摘要。GUI calculate 由后台 UI worker 执行、忙时禁重入，但不等同于持久 scheduler 任务。 |
| 膜系光学优化 | main 与 simulation 角色可调用 `estimate_film_optimization`、`submit_film_optimization`；estimate 虽保留在旧 READ_TOOLS 分类但会登记收据，讨论 DISCUSSION_TOOLS 明确排除；操作/攻关的 submit 使用运行时注入的稳定 ID 和原会话放行。design/correction/windows 没有该 submit 权限，也没有自动应用候选的工具。 | 两项 typed 工具已实际注册；只接受 v1 `coat_sim.film_optimization_request` 厚度请求。结构请求在估算/队列写入前拒绝；底层 structure worker 不扩张公开入口。 | 22 项共享 CLI catalog 不含这两个工具。没有专用膜系优化请求 CLI；scheduler CLI 只接已冻结 `JobSpec`，不是领域请求向导。 | ComputeTaskStore 拥有估算、请求身份和放行绑定，SchedulerStore 拥有任务/取消/终态/结果；未知效果不得重放。取消回执须再查原 job 终态。`succeeded`、优化 completed 和 targets_met 分开；失败/取消只可读取验证过的 best candidate。详见 厚度/结构边界（历史验证记录，未随公开源码发布）。 |

### 沉积设计与修正板

| 具体操作 | 看板娘 | MCP | CLI | 生命周期、数据和限制 |
| --- | --- | --- | --- | --- |
| 源、基片、机器、工艺与修正板设计 | 可读取 schema、`read_design`、`validate_design`、`preview_design`、依赖解析、字段 patch、临时设计、保存/导入/导出，并经白名单窗口控制编辑器。`create_design_workflow` 可一次建依赖并安排后续仿真。 | MCP 有对应 `input_schema`、read/validate/save、预览、依赖、DXF 导入/导出、窗口工具和工作流工具。字段/JSON Pointer、窗口动作和 `force` 按 `read_window` 返回的白名单契约。 | 共享 CLI 支持 `input_schema`、`read_design`、`validate_design`、`preview_design`、`resolve_design_dependencies`、`check_machine_structure`，不开放常规设计保存/字段 patch/窗口/工作流。`algorithm_cli` 无设计文件编辑器，scheduler CLI 只收冻结 JobSpec。 | 一般操作同步读取/校验；计算工作流后台排队。目录默认为项目 `data/{sources,substrates,masks,machines,processes}`。MCP/助手自动化不能用任意文件路径越界；保存目录需存在，覆盖需显式 `overwrite=true`，正式保存/资产保存遵守确认流程。几何/依赖错误来自领域服务。 |
| 单源与工艺沉积仿真 | 助手操作/攻关仅用 `submit_single_source`、`submit_process_recipe` 经估算及真实模式放行提交；`simulate_*` 为本软件 MCP 的兼容别名，不属于助手讨论可执行目录。讨论不发起/放行计算、操作模式倒计时 10 秒、攻关模式按策略放行；模型不能自报模式或批准。 | 同名提交工具；兼容 `simulate_*` 也只是估算/放行/排队，不同步返回场数组。源/基片/机器/板、工艺依赖会在入队前校验并冻结快照。 | `algorithm_cli --process` 是已有工艺 CLI，可选 `fast/reference/optimize`；参数包含 settings、网格、步数/步长、backend、快速周期、预算、容差、转速边界、评估数。支持 `--job-id` 查重与 `--detach`。 | MCP/助手返回作业 ID 和状态，结果由 `calculation_job_result`、`read_calculation_result` 或 artifact API 读取；取消用 scheduler ID。CLI 同步观察到终态并拉取 `result.npz`、可用时 `.coatasset` 和 `report.json`；Ctrl-C 只分离观察，取消需另走队列命令。CLI 创建一个不存在的输出目录并写 `status.json`；结果目录已经存在时拒绝覆盖。CLI 默认不启用 per-layer field capture；启用时其容量上限 256 MiB。 |
| 快速、参考与优化算法 | `submit_process_recipe` 参数选择 `fast/reference/optimize`；优化只指 bounded carrier-rpm search。 | MCP 同一 process 工具和算法约束；参考与载盘转速优化为 bounded CPU 路径，显式 GPU/DirectML 不可用。 | `algorithm_cli` 三种选择与 MCP 领域操作对应；共享 scheduler job ID 可跨客户端读取。 | fast 复用既有沉积算法；reference 做有界 CPU 时间细化；optimize 对载盘 RPM 搜索并验证。返回 `algorithm_report`；它们不证明全部空间/物理误差或生产工艺有效。settings/time-step 需按实际 schema 校验。 |
| 修正板遮挡/场分析及板轮廓优化 | 可用 `submit_mask_weights`、`submit_mask_field`、`submit_mask_optimization`，先估算和模式放行；`correction_board_weights/field` 等旧名转入后台队列。 | 有估算、提交、`mask_optimization_groups`、状态/取消/结果工具。分析输入绑定机器、源、基片/修正板设计；优化读取工艺启用关系。 | 没有单独修正板 CLI；底层 scheduler CLI 可处理已经正确冻结的通用 job spec。 | 后台结果是标量摘要/文件引用，需要时分页读取或下载。修正板轮廓优化当前仅固定板高、无孔外轮廓，组冲突/目标映射不满足时报错；报告区分全工艺评分与单源目标评分。候选粗网格改善不保证原网格或全局最优，也不做制造校核。 |

### 材料、队列与结果资产

| 具体操作 | 看板娘 | MCP | CLI | 生命周期、数据和限制 |
| --- | --- | --- | --- | --- |
| 材料库和光学常数 | 可读材料目录/记录和验证光学数据；design/simulation 执行助手还可 create/update/copy 自定义材料。 | `list_material_library`、`read_material`、`validate_material_optics`、`create_material`、`update_material`、`copy_material`。膜系材料另由 `film_materials` 只返元数据。 | 共享 CLI 只读 `list_material_library`、`read_material`、`validate_material_optics`；无通用材料写入操作。 | 内置材料只读；MCP/助手的自定义记录原子创建/更新；没有专门 material delete API。更新需完整记录并保留角色、数据源与光学集合；已冻结进设计/资产的快照不随材料库编辑更新。通用 `delete_data_path` 仍受 `data` 根限制，不等同于材料服务的删除操作。 |
| 任务估算、确认、队列、状态、取消 | 可估算任务、申请确认、查看 queue snapshot、按 ID status/wait/find/result/cancel。提交查重与未知副作用账本避免误重放。主助手通常由后台结束事件续接。 | 同名工具与持久队列；stdio 关闭只断开观察者，不取消已接收作业。`wait_calculation_job` 最长 60 秒并在进度变化时返回。 | 共享 CLI 有 `calculation_queue_snapshot`、`calculation_job_status`、`find_calculation_job`、`cancel_calculation_job`、`calculation_job_result`；不能估算、请求批准或提交作业。`scheduler` 专用 CLI 另有 `start/stop/serve/list/submit/status/cancel/result`；`submit` 接受已冻结 JSON JobSpec，不含产品估算/人工模式审批流程。`algorithm_cli` 使用同一 scheduler。 | 任务状态归 scheduler 所有；job ID 可跨 GUI、MCP、CLI 查询。取消确认是请求回执，终态才代表任务结束；重复取消幂等。scheduler 文档规定执行 worker 串行。MCP 文档另记 active tasks 最多 16 个，但 scheduler 实现审计未发现可验证的硬入队上限，故不把 16 当成所有入口的实现保证。状态/进度持久化到项目 `artifacts/runtime`；服务重启保留 queued，运行中任务标中断，不自动重算。 |
| 计算结果、图表与资产 | `read_calculation_result` 默认摘要，按需配置/分层分页/完整信息或明确请求网格；可 `visualize_calculation_result`、保存 `.coatasset`、导出 PDF。 | 同名结果工具；摘要默认不返回大网格。结果文件、图像和绘图引用由 `CoatService` 管理；资产读取可按 summary/configuration/layers/images/full 选择。 | 共享 CLI 只读 `read_simulation_asset`、`simulation_summaries`；algorithm CLI 将 scheduler artifact 下载至自己的输出目录；scheduler CLI `result` 返回 descriptor，二进制 artifact 应用独立下载接口。 | 仿真原始 `.npz`、临时结果及 artifact 有独立生命周期。资产包读取拒绝越界/重复/加密/超容量成员，包上限 512 MiB、展开 1 GiB、最多 64 成员。PDF/图表是文件写入；输出按既有覆盖参数检查。MCP 单次消息 2 MiB；大数组应留在本地文件或 artifact 通道。 |

### 研究库与外观渲染

| 具体操作 | 看板娘 | MCP | CLI | 生命周期、数据和限制 |
| --- | --- | --- | --- | --- |
| 研究库检索/阅读 | 看板娘已有 `research_library_search/read` provider 能力；仅在显式 provider 可用且 `allow_model_context=true` 时注入。此前验收覆盖了指定 root/固定 sidecar worker，检索文字是无指令权的参考材料。 | 本软件 MCP 未注册研究库工具。其余项目文件接口不会自动把任意根目录当作研究库。 | 没有研究库 CLI；本轮共享 catalog 不含此能力。缺口是 MCP/CLI 与本轮统一 catalog 接入，并非看板娘 provider 能力不存在。 | 只读、无持久索引/写盘。query 最多 240 字符，扫描最多 3 秒/32 MiB/1000 文件/5000 目录/20000 条目，单文档最多 1 MiB；search 最多 10 个 700 字符摘录，read 每页最多 6000 字符。路径拒绝隐藏/越界/reparse。工作树文档记载已核验的 partial snapshot 仅纳入 100/2651 文件；它不证明当前桌面进程已连接，也不代表完整库。 |
| 外观帧渲染 | main 与 simulation 角色可先用 `inspect_appearance_asset` 取得资产整体 SHA，再调用 `estimate_appearance_render`、`submit_appearance_render`。submit 走原会话放行；其他执行角色没有该 submit 权限，没有自动应用/打开预览的工具。 | 三项 typed 工具已实际注册。公共请求绑定项目 data 内 `.coatasset` 的规范路径/整体 SHA 和渲染输入；仅走本地共享 scheduler，拒绝注入路由。默认 `cpu`，可显式选 `gpu_cuda` 与其允许 precision；选 CUDA 不静默 CPU 回退。 | 共享 CLI catalog 不含三项工具。`python -m coat_sim.appearance.cli <job.json> <output_dir>` 为独立同步 CPU 入口，支持显示曝光、CPU workers 和 progress；不创建 scheduler job。CUDA 研究 CLI 另见 [GPU 契约](gpu-preview-contract.md)。 | 共享队列复用 estimate/ID/放行/状态/取消/结果链；只有 succeeded 且完整产物重验通过才是成功帧，无 best_candidate。模型使用服务返回的 outputs 引用，不猜路径。当前支持平面矩形或原点居中圆形基片，矩形区域光源及 CIE 360–830 nm；圆形边界须显式采用项目预览采样约定。CPU 默认保留 20,000,000 aggregate 标量预算；CUDA 只解除这一 aggregate 预算，其余既有上限与诊断仍在，CUDA 输出为 unqualified。请求/冻结字节门禁及取消非抢占边界见 [共享契约](appearance-shared-service-contract.md)。 |

## 文件与内存边界速查

| 范围 | 当前明示限制 |
| --- | --- |
| MCP data 文件 | `list_data_files` 单页最多 1000 条；`read_data_file` 最多 64,000 字符/次，图像最多 8 MiB；`write_data_file` 单文件最多 32 MiB，均限制在项目 `data` 下。 |
| 沙箱 Python | Windows LPAC；512 MiB、单进程、脚本超时 1–300 秒、最多 30 输入文件/32 MiB、40 个输出/32 MiB、stdout+stderr 1 MiB，工作目录 64 MiB/1000 项轮询上限。没有后端时失败，不降级为普通 Python。 |
| scheduler | JSON 控制请求体最多 64 MiB；大型结果走 artifact 二进制通道，按 1 MiB 块传输。进程内保留所有活动任务及最近 100 个终态任务；估算/内存 warning 不构成新增硬计算规模限制。 |
| CLI 外部文件 | 当前 `algorithm_cli` 与 appearance CLI 是本地命令，不继承 MCP 的 `data` 自动化目录约束；appearance 输入按 job contract 限制。历史 `ray_trace_cli` 源文件缺失，其命令不可作为当前可用入口。 |

## 共享操作层本轮落地

`CapabilityService.catalog()` 当前由实现签名和 `POLICIES` 一一校验生成 22 项操作。实际列表如下；目录中的 `available` 表示方法已注册，不表示模型请求已获准，也不增加桌面操作、材料写入、设计/沉积提交或作业批准权限。

| 类型 | 当前共享操作 | 实际委托所有者/方法 |
| --- | --- | --- |
| 膜系 | `film_materials`、`validate_film_design`、`load_film_design`、`save_film_design`、`expand_film_expression`、`simulate_film_design` | `CoatService.film_material_options`、`validate_film_design`、`load_film_design`、`save_film_design`、`expand_film_expression`；仿真走 `film_result_service.simulate_film_result`，再调用 `CoatService` 校验/仿真/CSV 导出方法。 |
| 设计与机器检查 | `input_schema`、`read_design`、`validate_design`、`preview_design`、`resolve_design_dependencies`、`check_machine_structure` | `CoatService.load`、`validate`、`preview`、`dependencies`、`check_machine_structure`；schema 使用 `design_json_schema`、`film_stack_json_schema` 和现有 Pydantic schema。 |
| 材料与资产只读 | `list_material_library`、`read_material`、`validate_material_optics`、`read_simulation_asset`、`simulation_summaries` | `MaterialLibrary.list/read`、`CoatService.validate_material_optics/read_simulation_asset/simulation_summaries`。 |
| 已有任务查询/取消请求 | `calculation_queue_snapshot`、`calculation_job_status`、`find_calculation_job`、`calculation_job_result`、`cancel_calculation_job` | 注入或惰性创建的 `SchedulerClient.snapshot/status/find_request/result/cancel`；取消只发原作业的取消请求。 |

### 22 项逐操作契约

表中看板娘入口指 `project_action` 只能调用当前会话/模式已下发 schema 且被 `READ_TOOLS`、`MAIN_TOOLS` 或执行助手子集允许的同名工具；MCP 列为同名已注册 wrapper；本地共享 CLI 用 `python -m coat_sim.capability_cli --root <项目目录> call <operation>`。目录发现不授权。MCP wrapper 将共享错误转成 `ToolError`；共享 CLI 返回 JSON，成功退出码 0、失败 2。同步操作没有任务 ID 或自身取消接口，除队列查询/取消外不接管 scheduler 生命周期。

所有参数都必须是 JSON object，按方法签名动态构建 strict Pydantic schema，拒绝额外字段和类型强转，非有限数字也拒绝；缺失/错型/多余字段为 `invalid_arguments`。CLI 的 JSON decoder 另拒绝重复键。未知操作是 `unavailable_operation`；保留领域 `ServiceError.code`；文件系统错误为 `io_error`，其他输入/领域值错误为 `invalid_request`，未分类异常为 `operation_failed`，文字脱敏且不传栈。`execute()` 统一返回 `{ok, operation, result}` 或 `{ok:false, operation, error:{code,message,effect}}`；`valid:false` 是成功返回的业务校验结果。只读操作失败 effect=`none`；写入或取消调用失败的效果可能未知时 effect=`unknown`，调用方不得自动重放。

边界约定：MCP 的单条协议消息上限为 2 MiB，共享 CLI JSON 输入上限 32 MiB；看板娘图像结果另有每轮最多 8 张/32 MiB 的合计预算。共享能力层没有额外总结果/RSS 限额；看板娘文本请求仍受当前模型上下文/输出预算控制。逐行提到的 `CoatService` 路径检查继续执行；CLI 的 `--root` 是运行进程的本地 OS 用户显式选择，MCP/看板娘权限不会延伸过去。较大资产仍由原资产/artifact 分页或下载协议传输。

| 操作 | 看板娘 / MCP / CLI 支持 | 实际服务方法与输入 → 输出 | 生命周期、错误、权限与副作用 |
| --- | --- | --- | --- |
| `film_materials` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.film_material_options(purpose=film or substrate)` → 材料 id/name/只读与可设计元数据、光学集元数据和 `k_known`，不含光学数组。 | 同步只读；项目膜系材料选项。输入 purpose 限定为 `film` 或 `substrate`。 |
| `validate_film_design` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.validate_film_design(data)` → `{ok:true, design}` 规范化膜系设计。 | 同步，无文件副作用；领域校验失败返回统一领域错误。 |
| `load_film_design` | 同名工具；三入口均受上方授权约定限制。 | `CoatService._path(path)` 与 `CoatService.load_film_design(path)` → 解析后路径及设计 JSON。 | 同步只读；路径留在 CoatService 项目根约束内。 |
| `save_film_design` | 同名工具；看板娘执行子集可能不含保存；MCP wrapper 与共享 CLI 可调用。 | `CoatService.save_film_design(path,data,overwrite=false)` → 保存回执/路径。 | 同步写文件；路径受项目根约束，覆盖必须传严格布尔 `true`；策略副作用 `write`，执行中失败可能标 `unknown`。 |
| `expand_film_expression` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.expand_film_expression(expression,symbols)` → `{layers:[...]}`。 | 同步展开，不创建文件或 scheduler job；表达式错误保留领域错误。 |
| `simulate_film_design` | 主助手同名工具；MCP 同名 wrapper；共享 CLI 可同步调用。 | `film_result_service.simulate_film_result` 编排 `CoatService` 校验、仿真、CSV 导出与 JSON 发布；`data,settings?,output_path?,overwrite=false` → 摘要、warnings、产物路径及输入/产物 SHA-256。 | 同步计算；无 job ID、状态或 cancel。默认写 `data/results/film_<uuid>.json` 与同名 CSV；显式 JSON 只能是 `data/results` 直接子文件且默认不覆盖。CSV/JSON 分别原子发布，JSON 失败会保留并报告 CSV；写入失败不得自动重放。 |
| `input_schema` | 同名工具；三入口均受上方授权约定限制。 | `design_json_schema`、`film_stack_json_schema` 或现有 Pydantic schema；`kind` 为 source/substrate/mask/machine/process/settings/mask_optimization/film_stack → `{kind,schema}`。 | 同步只读，不读取/写入设计，不生成膜系；schema 的 `film_stack` 不等同新增膜系生成器/公开设计库。 |
| `read_design` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.load(kind,path)`；kind 为 source/substrate/mask/machine/process，detail 为 summary 或 full（默认 summary）→ 文档 JSON、valid 与 errors；process summary 使用 `process_summary`。 | 同步只读；只解析项目数据下既有设计文件，路径检查与领域解析错误由 CoatService 执行。 |
| `validate_design` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.validate(kind,data?,path?,include_data=false)` → kind、`valid`、errors，可选规范化 data。 | 同步只读；`valid:false` 是正常校验结果，不是调用错误；路径由 CoatService 限制。 |
| `preview_design` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.preview(kind,design_path)` 或 `preview(kind,design_data,path=intended_path)`；输入必须且仅能提供 `design_path` 或 `data` → `PreviewResponse`。 | 同步预览，不提交任务、不保存正式设计；设计和意向路径仍由领域服务检查。 |
| `resolve_design_dependencies` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.dependencies(kind,design_data,path)`，kind 为 source/substrate/mask/machine/process → `DependenciesResponse`。 | 同步只读解析依赖，不创建/冻结 scheduler job；路径受项目根和现有引用校验约束。 |
| `check_machine_structure` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.check_machine_structure(machine_path?,machine_data?,substrate_path?,substrate_data?)` → 结构检查结果。 | 同步只读几何/引用检查；输入可用文件路径或数据，结构问题按结果/领域错误返回。 |
| `list_material_library` | 同名工具；三入口均受上方授权约定限制。 | `MaterialLibrary.list(offset=0,limit=20)` → 材料分页。 | 同步只读；没有材料库写入/删除权限，分页参数交由库服务校验。 |
| `read_material` | 同名工具；三入口均受上方授权约定限制。 | `MaterialLibrary.read(identifier)` → 已有材料记录。 | 同步只读；identifier 查找失败保留材料库领域错误；材料光学数组不由膜系 `film_materials` 读取。 |
| `validate_material_optics` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.validate_material_optics(datasets,wavelength?,wavelength_unit?=None)`；wavelength_unit 可选 nm 或 um → 光学数据校验结果。 | 同步纯校验，不修复或保存材料；结构/波段错误留在结果或领域错误中。 |
| `read_simulation_asset` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.read_simulation_asset(path,detail,offset=0,limit=20)`；detail 为 summary/configuration/layers/images/designs/full，默认 summary → 请求的资产摘要/分页。 | 同步只读；沿用资产路径、校验和、解包成员/展开尺寸限制，不执行任意归档提取；大字段分页或用原 artifact 通道。 |
| `simulation_summaries` | 同名工具；三入口均受上方授权约定限制。 | `CoatService.simulation_summaries(limit=20,offset=0)` → 历史仿真摘要页。 | 同步只读；历史文件访问仍受项目数据目录与分页校验约束。 |
| `calculation_queue_snapshot` | 同名工具；三入口均受上方授权约定限制。 | `SchedulerClient.snapshot(after_event=0,limit=100)` → 队列快照和增量事件。 | 同步查询现有 scheduler；不提交作业。首次访问可能按原客户端行为启动/连接本机 scheduler；状态所有者仍是 scheduler。 |
| `calculation_job_status` | 同名工具；三入口均受上方授权约定限制。 | `SchedulerClient.status(job_id)` → 指定作业状态/进度。 | 同步查询已有 job ID；不启动第二份任务、不取消。未知 ID 为原调度领域错误。 |
| `find_calculation_job` | 同名工具；三入口均受上方授权约定限制。 | `SchedulerClient.find_request(client_request_id)` → 原请求对应的 job ID/状态或未找到结果。 | 同步查重查询；不提交或重放未知副作用请求，依赖 scheduler 原请求账本。 |
| `calculation_job_result` | 同名工具；三入口均受上方授权约定限制。 | `SchedulerClient.result(job_id)` → 原作业终态结果/descriptor。 | 同步读取原作业；不启动新计算。结果可用性和 artifact 权限受 scheduler 状态、路径与下载协议约束，不把大数组内联到短响应。 |
| `cancel_calculation_job` | 同名工具；三入口均受上方授权约定限制。 | `SchedulerClient.cancel(job_id)` → 取消请求回执。 | 同步返回请求，但取消由 scheduler 异步执行；必须再次读取 `calculation_job_status` 确认终态。不能取消未知 job，也没有进程强杀能力。 |

`CapabilityService.call` 只接收 JSON object，Pydantic 模式为 strict 且拒绝额外字段；`catalog()` 返回每项输入 schema、owner、effect、boundary、lifecycle 与授权说明。`execute()` 返回统一 `{ok, operation, result}` 或 `{ok:false, operation, error}`；错误码包括 `unavailable_operation`、`invalid_arguments`、`io_error`、`invalid_request`、`operation_failed`，原 `ServiceError.code` 保留。读失败的 effect 为 `none`；写操作开始后无法判定是否已部分落盘时为 `unknown`，不能自动重放。

共享操作当前在三入口的接入状态：

| 能力 | 看板娘 | MCP | `capability_cli` |
| --- | --- | --- | --- |
| 22 个 catalog 操作 | 仍由 `runtime.py` 的 `READ_TOOLS`/`MAIN_TOOLS` 和执行助手子集授权，再经 `ProjectGateway`；目录不扩大 allowlist。 | 22 个 registered wrappers 均经 `CapabilityService.call`；仍由原 MCP schema/ToolError 包装负责 transport。 | 可 `catalog` 或 `call <operation>`；接收 `--arguments` 或 stdin 一个 JSON object。参数最多 32 MiB，重复字段/非有限数字拒绝；成功 exit 0、失败 exit 2。`--root` 指定本地项目根且 `CoatService` 限制在该根内。 |
| 提交/批准和材料写入 | 仍走既有任务工具和真实模式放行；没有新增通用方法调用权。 | 仍走既有专用工具；共享层没有沉积/膜系优化 submit、估算批准或材料 create/update/delete。 | 没有通用 submit、estimate approve、material write、Python、窗口或开发诊断命令。 |

同步共享操作没有自身后台任务或取消语义；`simulate_film_design` 是写文件的同步计算。队列操作只管理已存在 scheduler job ID；取消调用返回的是取消请求回执，须读取原 job 状态确认终态。共享 CLI 的 JSON path 受显式 `--root` 限定，但它作为本地命令仍由运行该进程的用户发起；MCP 的授权策略不会自动延伸到 CLI。

共享同步操作不拥有草稿/GUI 状态。任务 ID、状态、结果与取消继续由原 scheduler 所有；提交仍使用既有沉积、修正板优化或膜系优化入口。共享 CLI 的 JSON 参数和错误 envelope 不等同于专用的 `algorithm_cli`、`ray_trace_cli`、scheduler CLI 或 appearance CLI。

参数化初始膜系生成器、公开参考层表和对应 Quick UI 已存在：`film_design_templates.py`、`film_design_reference_recipes.py` 与控制器实现目录/映射预览，session 负责替换/撤销，用户设计库使用 SQLite。其限定历史验收见 [生成器阶段](film-design-generator-stage-report-20261011.md) 与 [参考层表阶段](film-design-reference-library-stage-report-20261011.md)；这些日期不等于本轮重验。当前没有为这套光学初始设计/参考映射注册独立 MCP 工具或共享 CLI catalog 操作。已有 MCP `build_film_stack` 是沉积工艺层表生成入口，不能代替光学设计模板、参考层表映射或用户设计库操作。

## 代码与契约依据

- 入口路由：`coat_sim/agent/runtime.py` 的 READ/MAIN/EXECUTION 工具集合；`coat_sim/agent/tool_dispatch.py` 的 `ProjectGateway` schema、参数校验、去重和未知副作用处理。
- MCP 能力：`coat_sim/mcp/server.py` 的 `@mcp.tool()` 注册及 `window_action`/队列包装；`docs/mcp.md` 的路径、确认、队列、结果契约。
- 持久任务：`coat_sim/scheduler/`；`docs/independent-scheduler.md` 的冻结输入、取消、重启和 artifact 契约。
- 现存专用命令：`coat_sim/algorithm_cli.py`、`coat_sim/scheduler/__main__.py`、`coat_sim/appearance/cli.py`。历史 `coat_sim/ray_trace_cli.py` 未在本轮源码树恢复，不能据旧文档确认其可运行。
- 共享层：`docs/entrypoint-service-contract.md`、`coat_sim/core/capabilities.py`、`coat_sim/capability_cli.py` 及相应契约测试；能力表须按实际 catalog 更新。
- 当前源码合同测试：`tests/test_capability_schema_cache.py`、`tests/test_capability_queue_route.py`、`tests/test_capability_effect_stage.py`、`tests/test_film_design_mcp.py`、`tests/test_film_compute_entrypoints.py`、`tests/test_ai_film_operations_contract.py`、`tests/test_appearance_compute_entrypoints.py`、`tests/test_ai_appearance_operations_contract.py` 与 `tests/test_scheduler_service.py`。它们覆盖共享 catalog/MCP/CLI、film MCP、队列边界和 catalog 外 typed 计算入口；fake peer 验证不等于真实第三方验收。
- 研究库先前 31 项 provider 与 14 项 worker 测试数字属于历史记录；原同名测试文件不在本轮源码树，不能记为当前执行。当前源码仍有 provider 双门禁和有界只读实现，完整 MCP/CLI 产品范围待定。
- `tests/test_entrypoint_capability_documentation.py` 只发现真实 SDK 注册/schema 和角色集合，检查本文两条 typed surface 与代码一致；不派发业务、启动 worker 或读取用户数据。当前命令/结果见本轮边界复核，不以发现目录证明真实 provider、GPU、Windows 或发行验收。


## 当前界面读取与受控操作（2026-10-16）

`project_editor_snapshot` 已登记为只读工具，经精确六字段 project_target 路由到原工程owner，读取膜系设计/光谱仿真/优化真实当前状态；分页带expected_snapshot_id，未保存草稿不替换成保存版。讨论目录允许该工具；绑定窗口的update_window/window_action仅操作/攻关按实时白名单、instance_id和expected_snapshot_id执行，不开放未声明动作或整体路径替换。更完整边界、算法目录与离线规模测量见[助手算法能力合同](assistant-algorithm-capability-contract.md)。
