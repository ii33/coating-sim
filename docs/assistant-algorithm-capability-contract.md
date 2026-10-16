# 助手算法能力与按需参考合同

更新：2026-10-16。本次不改变任何数值算法、物理模型、评分或文件格式。

## 输入与能力发现

输入是当前用户目标、已有设计/工程引用、光谱或空间膜厚目标、采样、可变参数、约束及预算。唯一业务真值为当前工具目录、参数校验器和实际服务结果；手册仅解释能力，不增加执行、保存、覆盖、候选应用或外部数据发送权限。

常驻仅保留简要能力边界和按需导航。`read_software_manual` 新增三个章节：`algorithm_selection`、`film_optimization`、`film_optimization_parameters`。每节均小于默认 12000 字符；读目录或其中一节不会加载其他章节。方法说明对应现有实现与 `docs/algorithms.html` 的方法锚点，不把原始算法库整体放入常驻提示。

先读取已知设计和需求，按目标区分：沉积 fast/reference/optimize、修正板几何/厚度优化、独立膜系 R/T/A、固定层序厚度优化、插层/生长结构搜索。关键输入缺失合并询问；不得把 GUI 默认值、硬件存在或读到的研究资料当成用户目标、算法资格或授权。

已保存输入通过 project_list 的明确工程身份，project_inspect/project_resolve 的确切 .coatproj 描述文件和 load_film_design/read_design 读取。当前工程绑定 read_window 仅提供身份/状态。独立只读 project_editor_snapshot 使用 project_list 原样六字段目标，按当前 runtime 已存在 owner 捕获全部工程模块的版本化草稿与已有目标/参数；队列仅捕获当前已载入视图缓存；summary 或分页 available=false 必须如实报告。分页携带 expected_snapshot_id，过期拒绝混页，不创建窗口、保存或计算。保存版 workflow_settings.optimization 可复用，但不是未保存版或已冻结 v1 请求。不能把保存版当成当前草稿；快照不可用时请求相关输入或明确按保存版分析。

工程受控编辑仅在操作/攻关模式按实际能力开放：原三膜系沿用原合同；新增 source、substrate、machine、mask、process、materials、main、ray_trace、film_appearance 九模块按实时 schema 做单字段更新或一个有界原 GUI 动作。写入须同时满足原样精确六字段 ProjectTarget（client_id、client_generation、project_id、session_generation、root、project_file）、当前 instance_id，以及同模块最新 snapshot_id 作为 expected_snapshot_id，写后刷新。沿用原 GUI 的校验、草稿、dirty/revision、撤销和暂时无效文本语义；原有轻量预览与工作量估计照常。错误/陈旧/超白名单请求不得部分修改状态，未提交原生输入缓冲或待处理模态/对话框阻断写入，接口不代替 GUI 提交或确认。编辑入口不自动保存、开窗、切换资源、导入/导出、替换文档或选择设备/backend，不隐式建立计算放行回执、提交/取消计算或应用候选。新九模块不新增 save；原三膜系仅保留各自原合同明确声明的显式 save。新增范围不支持材料选择、光学数据集编辑/替换及其打开中的嵌套模态；film_optimization 原有受限 set_structure_material_selected 仍按原白名单保留。队列/结果与讨论模式保持只读，计算任务估算、真实模式放行、提交和取消继续走独立合同。详见[受控编辑合同](project-editor-operations-contract.md)。

设备按任务和入口核对。沉积 fast 的自动设备顺序不能泛用于 reference/optimize 或通用膜系优化。当前通用膜系优化队列为 CPU 路径；专用相干批量前向与 GPU 渲染的资格不能自动转换为优化 GPU 支持。本文不声称任何实际 GPU 或 provider 验收。

## 状态所有者、执行与错误

助手只提出算法/参数建议并按开放入口提交。设计编辑器拥有草稿与修订；业务服务拥有校验与冻结输入；计算估算/放行服务拥有资源策略；调度器拥有作业身份、进度、取消及终态；结果服务拥有候选与可应用性校验。

保持既有估算→真实会话放行→稳定提交身份→进度/取消→结果核对。讨论（内部兼容值chat）为强制只读plan模式；操作/攻关仍不扩大授权。错误按真实校验返回解释，未知效果先查原作业，禁止换 ID 重放。取消请求不等于终态；预算结束不等于达标；训练达标与完整留出验证达标分别报告；不自动应用候选。

## 委派边界修复

固定依赖 pydantic-ai-harness 0.36.0 将根运行深度记为 1。旧 `SubAgents(max_depth=1)` 会在根处关闭 `delegate_task`，虽然已经创建执行助手。改为 2 后仅允许主→执行助手；`inherit_tools=False`、`agent_folders=None` 和每领域白名单不变。

定向测试不只比较数字：真实父模型发出 `delegate_task`，真实子模型看到自身 `delegate_task` 被深度边界隐藏，即使它也持有下一层 roster；子模型不能看到父专有工具，下一层模型若被调用即失败。另用真实 `run_task` 装配检查三模式的首请求工具、能力注入与指令。

## 现存缺口：结构优化 typed MCP

当前 GUI 已有独立 `FilmStructureOptimizationRequest`（`needle_only` / `growth_only` / `combined`）。当前公开 `estimate_film_optimization` / `submit_film_optimization` 仍仅接受 v1 厚度请求。助手须说明此边界；不得把结构请求塞入厚度工具，或以任意脚本绕过合同。窗口目录没有对应动作时，GUI 可见并不等于助手可自动操作。

后续最小接入方案，尚未实现：

1. 提供只读的优化方法/参数合同发现。返回当前支持的方法、各自设计形状/依赖/设备限制、序列化请求 schema 与解释；设计相关资格由现有校验器检查，不仅列举方法名。
2. 提供名称明确的 `estimate_film_structure_optimization(request)` 与 `submit_film_structure_optimization(request, client_request_id)`，外层严格禁止额外字段。请求必须是独立结构 kind/version，内含冻结 base_request、候选材料快照和有限结构预算。保留厚度工具拒绝结构 kind 的行为。
3. 内部通过判别式 canonical 校验分流，复用 `film_structure_request_from_payload`、`freeze_film_structure_optimization_request` 与 `freeze_job_payload`。不能只放宽字典或绕过 `core/film_compute.py` 的 canonical 边界。
4. 复用现有 `film_optimization` 调度执行器：`scheduler.submission.submit_confirmed` → `freeze_spec` → `scheduler.worker._film_optimization` → `execute_film_optimization_job`；这些内部已有结构分支，仍需前端估算/输入指纹/放行/去重全链验证。
5. runtime 的 READ_TOOLS/MAIN_TOOLS/EXECUTION_TOOLS/SUBMIT_TOOLS、typed 参数校验、稳定 client_request_id 和原模式放行路由须同时接入；不能出现目录有名字而执行不可达或估算可达而提交绕过放行。
6. 结果继续通过原 job 工具读取，由 `scheduler.result_capability.validate_film_optimization_result` 的结构分支验证。补拒绝错误 kind/version/哈希/未知字段、变更输入重复 ID、未授权放行、取消/部分候选、过期修订、结果重放与不自动应用的负例。

结构参数当前有界实现要求 max_added_layers=1、growth_cycles=1；结构 base_request.max_evaluations 计入训练/Jacobian/留出网格每次完整求值，区别于厚度优化的训练预算/验证另报。

这个后续接口属于新增能力，不包含在本轮“参考对齐和委派修复”完成范围。

## 离线规模测量

运行 `PYDANTIC_AI_NO_BANNER=1 python tools/measure_assistant_context.py --output <report.json>`。

脚本调用生产 `run_task`，由本地 FunctionModel 在真正模型边界捕获装配结果并立即停止，不产生模型回复/工具动作；禁止 socket 与密钥读取，使用空临时密钥、临时数据库、空设备历史。会计入能力注入的计划、技能加载、委派工具及 meter 第二次语言指令。本地 FunctionModel 使用生产 _chat_model 的完整 OpenAI profile，包括 OpenAIJsonSchemaTransformer，随后通过固定 OpenAI mapper 序列化，包含 type/function 包装；不只统计框架原始 schema。业务目录来自真实 MCP 注册与运行时允许集，仅读目录，不调用 handler。

报告区分常驻指令、顶层 schema、按需精简业务目录、全业务 schema、按需技能文件与可选来源。字符数为 Python Unicode 字符数，字节为 UTF-8；Token 仅用项目规则 `ceil(ASCII/4)+非ASCII逐字符`。本机虽有 tiktoken 包，但未使用未缓存的分词数据，也没有联网下载。报告不是具体模型分词结果、请求开销或实际计费；动态历史/摘要、用户消息、结果、恢复记录、设备历史、已加载技能、变化的计划、外部 MCP 目录/结果、研究库摘录、媒体和服务端封装另计。


## 讨论模式执行层（2026-10-16）

`chat` 保持持久化兼容值，界面称“讨论”。只允许当前界面/设计/结果读取、纯校验、轻量派生数据、问题澄清与计划。`DISCUSSION_TOOLS` 从业务 READ_TOOLS 排除会持久登记计算收据的 estimate_compute_task/estimate_film_optimization/estimate_appearance_render。创建、保存、写入、修改、开关窗口、提交/取消、导出、脚本、开发工具和工作流推进均不开放。

防线包括：目录按模式过滤；BudgetAndTools 在 handler/ledger/RPC 前拒绝不允许的实际业务名；project_action 闭包独立拒绝；ProjectGateway 的 read_only_mode 拒绝超白名单和所有未知外层字段。名义只读 read_calculation_result 的 export_data 混合效应分支必须省略或是精确布尔 false，不接受可强制转换的字符串/数字。旧工具别名、伪造 mode/origin/approved、历史未闭调用或旧批准不会扩权。

讨论仅提供 external_tools 目录，不注册 external_action；执行 guard 同样拒绝历史外部动作与delegate_task。技能正文注明只读边界，不授予绕行路径。后台成功事件只读结果，不 advance_design_workflow 导出；取消讨论模型请求不调用后台作业取消清理。UI-owned 持久 mode/generation 对旧审批的阻断由计算策略层另行实现，不信模型参数；不取消已提交运行作业，也不限制用户直接 GUI 手动计算。

真实 FunctionModel 定向负控见 tests/test_assistant_discussion_mode.py，包含实际 project_action 调用→业务拒绝且 fake peer 零 RPC、只读 UI/校验正例、参数级导出和写入形变、恢复旧批准/未闭调用、取消讨论与后台完成事件。既有 operational 测试夹具显式设置 operate，保留其原授权与结果合同断言。


### 动态模式代际检查

运行时复用 `ComputeTaskStore.assistant_dispatch_authority` 读取 UI-owned 模式/代际和原请求绑定，不信模型 mode/origin 或旧received事件。每次非只读工具执行前、等待业务工具锁后、project_action/external_action闭包内、提交准备后自动放行前分别核对；界面切讨论或来回切回原模式均不能延续旧写请求，缺可信来源失败关闭。名义读取中的 export_data=true 同样按写动作检查。纯读取可继续。

数据库使用只读URI，不创建表、不持数据库写锁跨业务/网络RPC；已经进入的单个原子保存可完成，后续dispatch被阻断。后台工作流按原origin请求核对后才续步，讨论只读结果。用户显式停止操作任务时的取消清理仅绕过“已停止”标志，仍核对当前模式/代际，并在每个取消RPC前重查；不会因切讨论主动取消旧作业。真实FunctionModel负控覆盖operate/research→讨论、切回同模式新代际、外部action、混合效应结果导出、缺来源，以及prepared提交与自动放行之间的模式变更。
