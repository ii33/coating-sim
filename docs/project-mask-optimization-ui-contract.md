# 修正板窗口默认工艺优化 UI 契约

状态：已实现并通过定向契约 / Qt 验收，2026-10-15。本文只规定界面与控制器边界，不改变优化算法、文件格式或数值语义。算法仍以 `docs/algorithms.html` 为准。

## 所有者与默认输入

- 项目会话的 `ProjectResourceWorkspace` / `ProjectBinding` 是项目身份及 retained editor drafts 的所有者；优化入口属于修正板窗口。不得从当前可见的工艺窗口取进程数据，也不得按文件名、最近打开记录或全局资源库猜默认工艺。
- 项目优化入口只解析当前修正板窗口所属项目的已指定默认配置。通过 `load_project_default(store)` 获取 ready plan 和完整依赖图；`plan.project_id` 必须等于该 workspace binding，计划的 project revision 与 configuration ID 必须仍匹配当前 manifest。默认工艺、机器、基片、源与已配置修正板均从同一解析图取值。
- `empty_project`、`no_default`、配置/依赖缺失、类型冲突、哈希错误和过期计划是不同的可见失败状态。失败不得静默切换到其他工艺或当前已打开的工艺草稿。
- 开始前只读检查默认依赖闭包中已打开控制器的快照，以及尚未打开的恢复草稿。若闭包文档有 dirty 草稿（包括焦点文本/pending UI），阻止提交并报告对应资源；要求用户在资源编辑器中保存或丢弃后重试。其他项目和不在默认闭包内的其他工艺/资源草稿不得被替代为本次输入，也不阻止与其无关的默认闭包计算；界面须标明本次依据的是已保存的项目默认工艺与机器。
- 解析和 dirty 检查失败不改动项目文档、窗口草稿或默认配置。优化输入由该次解析计划冻结；提交后更改焦点、切换项目或修改资源，不可重定向已提交作业。

## 协调器接口、生命周期与结果

- UI 层独立 `MaskOptimizationCoordinator` 接收修正板资源控制器所属的项目服务/绑定；持有默认输入解析、优化状态、worker/observer 和待保存候选。修正板 QML 绑定协调器状态，不构造一个完整 `ProcessBridge` 作为隐藏的第二个工艺编辑器。
- `MaskBridge.startOptimization(settings, options)` 每次开始均重新解析默认图及 dirty 状态，不复用过期选择；任何 `ProjectError` 显示为失败，不退回当前工艺。显式 `MaskBridge.cancelOptimization()` 仅请求取消该优化作业，并显示等待取消检查点的状态。
- 项目关闭冻结时拒绝新提交与持久化写回；已经提交到 scheduler 的作业仍有独立生命周期。隐藏/关闭项目资源窗口只销毁 presentation，workspace retained controller/coordinator 与 observer 继续工作，不隐式取消计算。workspace 最终 dispose 时 detach/reap observer，不杀死已提交任务；若冻结期间收到结果，延迟结果写回直至解除冻结。显式 `cancelOptimization()` 仅由用户取消按钮触发，并显示等待取消检查点的状态。
- 结果产生新的修正板候选，不覆盖原板。保存候选使用相同的项目绑定和项目服务；当前项目已变化、服务被冻结或候选越权时须失败，不可写入当前前台项目。
- 项目模式打开源/修正板依赖编辑器、工艺引用的机器/基片和优化产物工艺，必须经过当前 `ProjectBinding` 的 project desktop 路由，核对目标 kind、managed document ID 与 owner binding；禁止退回全局 `launcher.openWindow`。若目标编辑器已打开同一 document，仅显示/激活窗口并保留其 dirty 草稿；目标为另一 document 时沿用已有替换确认。无依赖时“新建”进入同项目空白资源编辑器，不自动绑定尚未保存的新资源。打开优化产物时先由当前项目服务导入复制其完整工艺依赖闭包，再打开新工艺草稿，不切换项目默认配置。独立模式保留原 launcher 路径。
- 解析状态（加载中、无默认、缺依赖、默认闭包 dirty）、计算中、已取消、失败与成功必须清楚区分。失败保留可理解的原因；进度/结果仅属于启动时冻结的项目身份及默认配置。小窗口下内容可滚动，不能遮住取消、保存结果或状态。

## 当前 UI 接口与默认参数映射

`MaskBridge` 暴露 `optimization`、`pendingMasks`、`optimizationMasks`、`optimizationMaskCount`、`optimizationSourceCount`、`optimizationAvailability`、`optimizationDefaultSummary`、`optimizationParameters`、`optimizationDefaultConfigurationId`、`optimizationDefaultRevision` 与 `optimizationReady`。其操作槽为 `refreshOptimizationDefault() -> bool`、`startOptimization(settings, options)`、`cancelOptimization()`、`saveOptimizedMask(url, index, overwrite)` 与 `saveOptimizedMaskNamed(name, index)`；项目默认解析、任务 observer 和候选保存由它唯一拥有的 `MaskOptimizationCoordinator` 执行。解析错误通过 `optimizationAvailability` 显示，启动快照携带项目 ID/修订和配置/工艺 document ID。

优化对话框在每次显示时重新解析默认配置。无默认或损坏闭包时仍可打开对话框读取失败原因，只有开始按钮因 `optimizationReady` 禁用；项目编辑冻结时入口禁用。默认配置中的 `parameters.settings` 先作为实际优化 `SimulationSettings` 基底，再由对话框可见字段覆盖，因此未显示设置继续保留。`parameters.time_step_s` 有值时用于步长输入并按现有 `time_fields` 换算；否则若存有 `settings.time_steps`，显示等价步长但未经用户编辑时原步数不变；用户编辑步长后再换算。两者均缺省时沿用旧优化窗的 0.025 秒。转速、网格、两种初相来自 `parameters.settings`；计算设备、周期快算开关和残余阈值分别来自 `backend`、`fast_cycle` 与 `fast_cycle_residual_percent`，仅在对应配置字段未提供时才采用旧界面默认。修正板优化继续使用其既有评分器：当前只映射 `fast_cycle`（精确/周期快评）与残余阈值；默认配置的主沉积 `algorithm`、`algorithm_options` 均不传给该评分器，界面提示此边界。优化专用的模式、搜索网格和评分预算仍由该窗自身控件提供。搜索网格初始值为 `min(81, settings.grid_size)`（未配置时按 181 回退，因此初值 81），仅用于满足现有范围并避免大于复算网格；这是界面初值映射，不改算法默认或算法行为，用户显式编辑仍使用原验证规则。

未被手动编辑的配置字段随“重新解析默认工艺与机器”更新；手动修改继续作为覆盖项保留。用户可用“从默认配置重置参数”恢复所有联动默认值。开始时再次解析当前默认闭包；若配置 ID、项目修订与对话框最近解析时不同，则拒绝本次提交并要求重新解析，防止把旧参数错误地配给新默认工艺。项目默认闭包的 dirty 判断只读 workspace 恢复快照：`dirty`、非空 `pending_ui`（即使 `dirty` 为 false）和嵌套材料 `dirty`/`pending_ui` 均参与；不调用提交/失焦，不把非闭包或其他项目草稿当作本次输入。

每个完成结果保留启动时的 project ID/revision/configuration/process ID。刷新默认不会改写这个归属；结果信息和保存状态标出来源配置。`saveOptimizedMaskNamed` 只在原项目会话仍活动且 mutation gate 未冻结时通过原 `ProjectResourceService.create_resource("mask", profile)` 创建新资源，不读取可见窗口当前项目，也不覆盖原板；传入名称写入新候选独立的 `name`（项目资源列表以此显示修正板名称），保留候选的 `description` 概述、`notes` 备注与全部物理字段。候选的内存快照及原板均不因保存而改变；多个候选可分别保存，重开工程后仍保留各自名称。

顶部操作栏统一使用 `Accessible.name = "操作栏"` 并按可用宽度换行；没有额外可见标题。修正板优化对话框用 ScrollView 承载参数、两个候选保存、打开优化工艺、进度和状态。

## 机器依赖控件

- 所有模式的机器结构中，蒸发源与修正板仍按机器槽位编辑；项目模式的项目资源选择器展示该槽位引用的当前资源，并以选择项目资源完成绑定；非项目模式可显示现有引用的文件名。机器编辑区不再显示或编辑源/修正板文件路径输入框。源的坐标、高度（`z_mm`）、材料、发射模型及各自现有单位/计算语义不变；移除的是文件路径文本输入，不是源高度字段。
- 项目资源下拉选择只接受同项目、同 kind 的 managed document。外部文件入口称“导入”：项目模式沿用机器 bridge 的 `setDependencyFile`，其 `ProjectResourceService.relative_reference(owner, target)` 会先通过 `import_resource` 将所选资源及完整类型化依赖闭包复制入当前项目，再写入新 managed identity；不修改外部原文件，也不保存外部路径。
- 非项目/独立模式继续保留原有文件选择与路径引用语义。机器编辑区的依赖按钮统一称“导入”；仅项目模式承诺复制到当前工程，独立模式仍按原有路径引用语义工作。
- 删除槽位按钮以叉号显示，但保留明确 `Accessible.name` 与 tooltip（包含资源类型和槽位，如“删除第 1 位蒸发源”）。无关联新建动作统一文案“新建”；已有依赖可以保留“编辑”。修正板窗口没有机器依赖时不显示“新建机器”按钮；已加载机器时显示“编辑机器”，原“加载机器”入口仍保留。

## 顶部栏与窗口适配

- 机器、修正板和工艺页面的顶部操作行统一称为“操作栏”，命名/无障碍说明使用同一短语，不新增共享全窗口导航或全局导航状态。
- 修正板优化的主入口和参数/状态/结果区域位于修正板窗口；工艺窗口不再提供该入口。窗口变窄或放大字号时顶部栏换行，编辑区仍可滚动，所有主操作可达。
- 机器、修正板与工艺普通窗口的顶部不显示旧资产包按钮，也不创建 `AssetBundleDialog`；旧 `.coatasset`/资产包底层读取兼容性不在此变更范围内，不将该入口改名伪装成项目资源包。
- 工艺窗口移除优化侧栏后，宽窗口的主编辑/滚动区不得继续预留固定优化栏宽度。

## 验收

- 契约测试覆盖空项目、无默认、默认缺依赖/类型错误、同项目 default closure、跨项目绑定、默认闭包 dirty（已打开和恢复草稿）、无关 dirty 工艺、每次开始重新 resolve、外部依赖闭包导入 copy 且原文件不变。
- Qt/QML 测试覆盖优化入口仅在修正板窗口、工艺窗口无入口、机器缺依赖时无“新建机器”、有依赖时可编辑、项目模式无路径输入且当前资源直接显示、导入而非外部引用、依赖编辑器路由保持同项目/dirty 防护、删除叉号的无障碍名称/tooltip、顶部操作栏统一、小窗口可滚动且可取消。
- 物理回归覆盖源高度字段/文档值完整保留；本次不跑长优化或大规模仿真。运行现有定向测试，并报告任何未运行的真实窗口/桌面验收。

定向验收文件：`tests/test_mask_optimization_ui.py`（默认闭包、真实 QML 点击、参数映射与评分器边界、生命周期、候选保存、同项目依赖和产物路由、外部复制、窄窗与销毁回调）；`tests/test_process_compact_sidebar_ui.py`、`tests/test_process_editor_navigation_qt.py`（移除入口 / 侧栏后的完整编辑空间及原导航）；`tests/test_project_resource_adapter.py`（第二源槽未提交高度文本的恢复）；`tests/test_theme_controls_static.py`、`tests/test_numeric_format.py`（共享输入控件行为）。

## 任意修正板数量（2026-10-15）

机器和工艺不设修正板数量上限。安装板与工艺板位以保存的完整列表为唯一状态；板位为非负整数，运行时按实际机器安装数校验，不截断依赖、候选或结果。优化界面显示全部安装板、下一可用新板槽（空机器至少提供两个起始槽），多选可联合优化任意数量的板。固定板 + 可变板模式仍是一个固定角色和一个可变角色，可选择任意安装板位；不会取前两块或以 1−固定槽推算变量槽。既有 allow_two_masks 名称兼容保留，值 true 表示允许同源任意数量的已选择板联合优化，不表示两块上限。

机器界面的添加、删除及保存/重开均保留完整板列表。优化参数与候选状态所有者仍为 MaskOptimizationCoordinator；重新解析后按最新完整安装列表构建槽位，启动时检查真实安装范围、新增槽连续性、重复项和几何数值。快照完整收集每个板依赖并按路径去重；原单文件/资产包字节安全边界仍有效，文件计数不再从两块板推导。CPU/GPU 使用同一逐板射线遮挡或规则，GPU 仍须真实可用，未在无硬件环境宣称 GPU 运行验收。
