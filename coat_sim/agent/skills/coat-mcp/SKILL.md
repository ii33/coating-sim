---
name: coat-mcp
description: 先核对本版软件能力与适用范围，再用项目 MCP 业务工具读取设计、校验与预览、提交后台沉积仿真、追踪作业并分析已有结果。
---

讨论模式（内部兼容值chat）仅允许读取、校验、分析和计划；以下写入、窗口操作、绘图生成、脚本、估算登记、计算、取消、导出和工作流推进仅在操作/攻关及现有授权范围内适用。不得以技能指令绕过执行白名单。

# 沉积仿真工程工作流

此技能只使用已暴露的项目业务工具。工具签名与输入模式是当前接口依据，不构造通用 shell、任意文件或网络操作。

## 先核对软件能力

工程任务先按需调用 `read_software_manual(section="index")`，随后读取对应章节：overview、workflow、coordinates、machine、process、mask、source、substrate、cad、results、assistant、development、environment_issues、workflows、film_design、algorithm_selection、film_optimization、film_optimization_parameters、compute_devices、projects、limitations。已在当前上下文拿到同一版本的相关内容时复用，不反复读取，也不默认请求 all。

说明书来源为 `coat_sim/agent/resources/software_manual.md`，离线富文本说明书为 `docs/software_manual.html`。通过文档查询工具取得当前版本与边界；文件路径本身不授予通用文件读取权限。

先判断用户目标能否表达为现有输入、运动、通量与遮挡模型，再读取设计和提交任务。模型不适用时明确指出具体缺失机制或参数，说明「这项需求不适用于本软件当前模型」；不凭工程常识虚构软件功能，不罗列不支持的机器类型。CAD与其他导入能力必须先读 cad 章节并核对现有工具，不把任意CAD模型当成可计算机器结构。通用建议可应用户要求讨论，但不替代实际软件能力。

## 显式项目管理

创建/检查/导入/配置项目时先读 `project_management_catalog`，用 `project_list` 获取已认证打开项目的身份列表，使用 `project_inspect`、`project_resolve`、`project_manage`。每次调用显式传入用户选择的 root 和持久化 project_id，修改还须 operation_id 和刚读取的 expected_revision。不得从当前焦点、启动器选择、环境变量推断写入目标；目标变化时重新读取身份与版本。普通打开只解析指定默认依赖图；recovery_read 仅供异常恢复审核，不自动写回。

AI 工具仅可访问软件数据根或已由项目管理器认证登记的项目根。项目已在 GUI 中打开时，将 project_list 返回的 generation 显式作为 project_manage 的 session_generation，路由到该项目原所有者；有未保存草稿时仍拒绝替换。缺少该绑定时修改返回 active_project_owner，不要改用独立文件工具绕过所有者。CLI 严格入口为 `python -m coat_sim.project_cli manage <operation> <arguments.json>`，参数对象使用同一身份/版本边界。旧 CLI 命令保留兼容，并对修改使用相同 OS 所有权锁。

## 工程窗口导航

工程 UI 使用 `project_list` 返回的完整 `project_target` 六字段对象。`project_open(project_file)` 只在既有授权根内打开指定 `.coatproj`，返回已确认的独立 runtime target。向 `list_windows`、`open_window`、`read_window`、`close_window` 显式传 target；不从当前窗口/焦点/PID猜工程。read 只读取窗口身份、可见性、状态与真实 navigation capabilities，不能称为完整编辑器内容。open 仅导航，不填文档或字段；bound update/window_action 当前明确不支持，应在原工程编辑器操作。不要用 project_manage 绕过 dirty/editor-owned save 限制。force 不绕过工程关闭 review；关闭入场不表示异步审核已完成。

## 读取与准备

1. 与工程界面有关时先读 project_list 并明确目标，再用带 project_target 的 list_windows/read_window；不能读取时清楚说明，不能假定界面选择。
2. 使用 `input_schema`、`read_design`、`resolve_design_dependencies` 确认输入及引用。读取先于修改，保留原始参数、路径与用户约束。
3. 使用 `validate_design` 检查类型、单位与约束，必要时预览几何。按工具返回的坐标系解释 x/y/z、旋转方向与角度；设计长度通常为 mm，具体以模式与返回结果为准。
4. 需要计算时先 `estimate_calculation` 判断设置和计算量。低分辨率趋势计算、高精度结果或参数扫描须根据用户目标选择，不能把预估称为已经验证的精度或耗时。

工具目录可用 `project_tools(tool_names=[...], design_kinds=[...])` 批量查询并在本轮复用。对话新设计和 `create_default_workflow` 的演示源、基片、机器、工艺均默认暂存于 `data/tmp`；用户明确决定正式保存时才询问名称，再通过对应保存工具正式保存。演示工具返回的默认参数是未标定演示约定。用户提供具体机器结构或指标时使用实际参数，不把演示默认值当生产方案。

对话中新生成的设计默认先调用 `prepare_temporary_design` 暂存到 `data/tmp`；只有用户决定正式保存时，询问名称（优先建议短设计摘要）并在确认后调用 `save_generated_design(kind,path,name,confirmed=true)`，由程序复制完整依赖图至正式 kind 目录。仿真成功会自动暂存热图、NPZ、实际配置和摘要；先复用结果中的 `files[].heatmap`，不重复生成标准热图。只有需要额外 contour/radial 视图时才调用 `visualize_calculation_result`。正式保存仿真资产前确认名称，再用 `save_simulation_asset`；之后以 `read_simulation_asset` 读取指定视图。

## 后台计算

- 单源使用 `submit_single_source`，工艺配方使用 `submit_process_recipe`。运行时会注入稳定的 `client_request_id` 用于去重，不要人为改写它。
- 高计算量任务先调用 `estimate_compute_task`，再调用 `request_compute_confirmation`；本机界面展示估算并按持久化会话模式放行：讨论模式只读，不登记估算/计算或放行；操作10秒倒计时、攻关默认放行；模型不能伪造模式或授权。放行后持久串行入队并立即返回，保留 `job_id`，助手继续会话。队列只显示摘要、预计用时、预计完成时间，无进度控件。完成事件在原会话由程序独立只读读取状态与结果摘要并汇报；聊天工具不等待终态、不反复轮询。queued/running 不是结果，failed/cancelled 不是成功；failed 且 interrupted=true 表示计算服务曾中断。
- 成功后用 `calculation_job_result` 获得 `run_id` 和摘要。需要分析历史结果时使用 `read_calculation_result`、`calculation_history`，先读摘要，仅在当前分析确实需要时请求完整网格。
- 不调用旧的同步沉积入口 `simulate_single_source` / `simulate_process_recipe`，使用上述后台作业替代。修正板权重/场、选定点权重、中心遮挡环等工程分析保留对应业务接口。不要仅因为上一工具回复丢失就重复提交；先核对原 job_id，缺失编号时用 `find_calculation_job(client_request_id)` 查询。新计算应沿用已核实的输入，并明确产生了新作业。
- 取消操作使用 `cancel_calculation_job`。它是协作取消，返回 running 仍表示在收尾；不要声称取消请求等于计算已经结束。

用户明确要求在主窗口点击计算时，可以使用 `window_action` 的 calculate，并通过 `read_window` 核对 busy/status；这类界面计算没有 job_id。取消该界面计算使用同一窗口的 cancel 操作，不能把它当作 MCP 后台作业查询。

## 分析与交付

对结果区分工具给出的统计值与自己的解释；说明所用设置、采样范围、边界条件和影响结论的模型近似。保留 run_id、job_id、设计路径及用户约束，供之后的对话继续。

可把完整工程子任务交给按需执行助手；只提供任务所需工具组、输入、约束和用户授权范围。最终解释由板娘整合，避免两个助手同时更改同一设计。

只有用户已授权的保存、覆盖和导出才执行对应工具，并传入明确路径及覆盖意图。工具报错时按实际错误调整一次必要参数；不要凭空宣称成功或反复重试同一有副作用操作。


## 逐层板选择与板形优化

- 工艺步骤 `mask_slots` 是0基安装位次列表；`[]` 全关，省略为全选。源/监测器位次仍为1基。不可用ID绑定替代实际位次。
- 优化前读完整工艺和依赖，使用 `input_schema("mask_optimization")` 取得参数模式，调用 `estimate_mask_optimization` 明确预算。目标均匀时采用CV评分；目标膜厚图模式必须有用户给定或已读取的真实目标图。
- 优化先调用 `estimate_mask_optimization` 评估优化预算，再对实际提交按高计算量确认流程估算并按真实本机会话模式放行；放行后保留 job_id，提交立即返回，完成事件在原会话独立只读汇报。取消与普通仿真一致。不要由语言模型逐点构造几十次仿真调用。
- 默认只优化工艺启用的无孔板，包络为各原板外接矩形。需扩大范围时显式给各板 `mask_bounds`。几何边长/径向/面积限制不等于制造线宽或材料利用率，不自行虚构物理约束。
- 成功后读取优化报告、完整副本 `artifacts.process_path` 和 run_id。报告中的粗网格评分改善与原网格复算分开解释，不宣称未做的实测验证或全局最优。按用户要求打开候选工艺、读膜厚结果或导出PDF。

## data 与沙箱可视化

- 五类设计默认保存 data 下对应分类目录，字段 description/notes 可用于作者说明与备注。
- list_data_files 列出授权数据，read_data_file 分段读 UTF-8，图片返回原生媒体内容。写文件用 write_data_file，创建目录用 make_data_directory；移动/删除仅针对当前用户已授权项。目录受 data_path 检查，不能通过链接/绝对路径越过 data。
- 已有沉积结果优先复用标准热图或其他已有图文件；只有额外 contour/radial 等图形才用 `visualize_calculation_result` 固定接口生成PNG/CSV。自定义分析才使用 run_python_script(code,files,timeout_s)，代码采用标准库及 pygal，读取 tools 返回的 input 副本映射，结果写 output。仅列必要输入，不能请求网络、用户目录或普通 shell。隔离条件不满足时停止，禁止用其他工具绕过。
- 返回 files 供对话预览。收集器可能为产物加数字前缀，文件链接和报告图片必须用实际 `files[].reference`，不要使用猜测的原脚本文件名。Markdown相对图片以报告所在目录解析，最终目标仍须在授权data内；不要因一次路径错误就断言预览器不支持全部Markdown图片。
- Pygal支持既有XY、折线、条形等图表，没有`pygal.Heatmap`；优先复用软件膜厚结果图，或使用coat_plot的heatmap/contour/radial_profile/profile_chart生成被动SVG。库中不存在某个类是脚本/API用法问题，不证明运行环境未安装。
- SVG为离线静态图，HTML不自动执行脚本。工具结果缺失时不声称生成成功，不粘贴base64或大网格。自动文件卡片以实际现存文件为准，错误文字和省略路径不能当作交付链接。

## 开发环境问题复盘

实际开发门禁开启时，运行程序自动记录启动、依赖、文件、DLL、权限与服务连接问题，不为每次记录额外发模型请求。需要复盘时按需读取 `data/tmp/ENVIRONMENT_ISSUES.md` 或 `.json`；权威去重记录是同目录SQLite。重复问题累计次数，保留首末时间、工具阶段和脱敏反馈。`SESSION_ISSUES.md` 是独立会话总结，不能覆盖自动记录或将模型推测写成已验证根因。

脚本调用不存在的API属于用法问题；缺少业务参数、设计约束和文件撞名不算环境故障。根据具体阶段区分未安装、加载失败、权限、连接与API不匹配，不能只凭一个Windows错误码宣称根因。自动记录不改变权限，也不触发自动提权、重装、绕过隔离或测试。

## 固定流程优先

多源、多层、板选择、计算与导出组合优先一次填create_design_workflow specification；先读一次该工具契约，不逐字段读取全部设计schema。程序处理依赖顺序、校验与入队；高计算量计算仍须估算并按真实本机会话模式放行。提交立即返回，完成后原会话独立只读读取结果及已授权导出。Agent负责解释需求与合法参数判断。暂停/失败只在用户授权继续时advance；状态用design_workflow_status，取消用cancel_design_workflow。源/监测槽1基，修正板槽0基。机器修正板通常单源单板组，少数配置明确双板；共享板耦合不适合作为独立优化对象。按真实返回引用呈现文件。

开发模式中每个实际问题应留记录：自动日志已捕获则复用；未捕获的实际界面/工具不一致用developer_report_issue补报，只写直接证据与未知项。developer_issue_reports(refresh=true)会输出现有账本的可读报告，返回文件引用，不向用户只承诺将来写报告。

## 膜系与结果可视化

“膜系”“厚度表”“60层”路由到 `film-stack` 技能；“可视化”、热图/等高线/径向图路由到 `result-visualization`。调用前各读一次对应说明/契约即可。膜系程序最多展开10000层；XLSX多sheet、厚度单位和基片→空气/反向层序须明确，公式先转值。四分之一波长只生成法向透明实数n的候选，不验证R或带宽。已有60层只需局部改动时用 `update_design_fields`，不要重发整份steps。

`create_design_workflow` 的 `film_stack` 与 `steps` 二选一；膜系每层字段沿用工艺模型，材料引用、机器/基片引用及速率、控制模式按工具契约填写。工作流可用 `export_charts=true` 固定导出图表。默认结果只读标量摘要；按需用 `read_calculation_result` 指定 detail 和分页，避免读取完整大网格。`visualize_calculation_result` 由程序生成PNG/CSV，不使用模型写脚本；可复用用户已拥有的结果文件。口述光学指标先确认波段/角度/偏振、材料数据集、层序与假设；沉积film_stack转换本身不验证光谱，独立film_design可用simulate_film_design，厚度优化按当前typed入口，结构优化独立核对，不能混用层数限制与请求合同。

用户要求查看设计形状时，读取 `design-visualization` 技能；调用 MCP `render_design(kind, design_path)` 生成修正板、机器、源和基片的离线SVG，调用 `render_design_workflow(manifest_path)` 获取工作流设计图和工艺详情HTML。大型工艺（含60层）完整层表留在可筛选/排序HTML里，工具返回仅包含实际文件引用、名称和层数摘要，不读取或回传完整steps JSON。图示只表达已有校验几何；机器参考节圆不是齿形，盘倾角投影不代表完整三维装配，图示不构成仿真或制造验证。


运行时对讨论/操作/攻关设每任务请求与累计输入限额，达到限额后按程序恢复；后台计算不因此取消。不得据此宣称性能已经实测。



仅操作/攻关适用，讨论不执行：计算请求登记/入队无需单独确认，不使用 ask_user_question 追加计算许可问题。执行资源策略只读取服务端持久化估算和真实会话；不放宽覆盖、删除、凭据或外部付费动作授权。

## 算法按需选型

需要选择或解释算法时先读 algorithm_selection，按目标再读 film_optimization/film_optimization_parameters。先核对当前设计、材料有效域与未知k、可变参数和边界、采样及预算；缺影响结论的条件合并询问。解释适用性、参数单位、代价与验收，按实际目录/校验器开放动作。reference/optimize的CPU限制、通用膜系优化CPU路径与专用相干批量后端资格分别判断；算法说明或研究资料不授予计算/保存/覆盖权限，不自动扩大到昂贵搜索。保留冻结输入、原作业身份、进度/取消、训练与留出验证及显式候选应用合同。

当前工程界面先由project_list取得精确六字段目标，再用project_editor_snapshot按需读取膜系设计/光谱仿真/优化当前状态；分页携带expected_snapshot_id，不把不可用快照替换为保存版。操作/攻关写前仅按实时editor_capabilities并携带instance_id与expected_snapshot_id，当前UI模式/请求代际变化后旧run不得继续写；讨论无写入口。
