# 膜系工作流文档契约

当前更新（2026-10-24）：波段 mean/pointwise 两组、版本、raw coverage 与严格判定以[双约束契约](film-band-criteria-contract.md)为准。旧 flat 目标与旧协议不迁移。

日期：2026-10-15

`.cfilm` 仍是 UTF-8 JSON、膜系 schema v1。新增可选 `workflow_settings` 根字段，包含 `simulation` / `optimization` 对象。旧文件缺字段时使用各页面默认设置，不强制改名或写入新字段。文件和 SQLite 存储都复用 `validate_design` / `FilmDesignService`，不引入按文件路径同步状态。

## 所有权与接口

`FilmDesignEditSession` 是配对设计、仿真、优化的唯一文档所有者，含未保存膜系和全部已提交配置。`workflow_configuration(scope) -> dict` 返回隔离配置；`set_workflow_configuration(scope, configuration) -> bool` 在锁内校验有限 JSON 数据、原子更新并标脏。相同值是 no-op。设置只推进 session revision，不推进 physical `design_revision`。新增订阅类别 `workflow_settings`；文档替换通过 `design` 通知同时恢复整份配置。

设计适配器提供 `getWorkflowConfiguration`、`setWorkflowConfiguration` 和 `workflowConfigurationChanged`。仿真适配器导出/载入 calculation parameters、plot、calculation bands、analysis、PNG export；优化适配器导出/载入 targets、variables、solver settings。各适配器负责语义校验，不在QML复制文档。配置写入不改厚度精度，不触发错误的优化设计冲突。优化候选写回保留当前配置，不能覆盖任务开始后新增的设置。

旧文档没有仿真配置时，仿真构造与打开继承设计 `first_surface`；已有仿真配置时以其保存值为准。`set_design_value('first_surface', value)` 只接受 `front` / `back`，属于物理设计编辑并推进 `design_revision`；非法值不修改文档。

## 打开、发送及取消

公共 `reuseWindowOpen(document_path='', payload=None) -> bool` 接受同一窗口新意图。无输入仅激活；同一 source/session 刷新不因 dirty 阻拦；不同文档存在未保存修改时先确认并保留当前输入/图表，取消无副作用。验证/加载失败保留原状态。来源重绑由 controller 自己完成，launcher 不改私有归属。

设计发送至仿真先提交焦点字段/表达式草稿，再复用配对窗口并请求计算。忙碌时合并为一次 pending recompute，旧 token 不能清除新任务busy或覆盖结果。普通设计编辑只同步不自动计算；优化成功写回与显式发送触发计算。配置载入不自动启动额外任务。关闭/取消和来源失效继续设置合作式cancel event，完成边界丢弃过期结果。

## 显示边界

标题厚度对所有正/背面膜层汇总，不含基片；按材料ID汇总物理nm，QW继续使用既有参考波长/材料n换算。标题优先显示合计及材料，再显示正/背分项；单面模式保留背面时标记“背面草稿”，参数区固定说明统计范围。只在文本显示时保留三位小数；无法换算的层显示数量，不能伪装总厚度完整。

自动X范围使用已选择曲线中至少一条有限值的波长样本并集，裁掉没有有效曲线的两端，不额外加5%空白；内部缺口仍保留。单有效点用最小非零显示跨度。无有效数据使用参数范围明确呈现空图；用户临时缩放优先，不因自动range重新计算被覆盖。手动范围仍受用户控制，不改变计算、统计或导出全量数据。

暂态平移由仿真控制器唯一持有。`plotData.pan_bounds` 是当前可见通道/偏振原始有限 `(λ,q)` 样点的联合包络；隐藏曲线、非有限配对和非正波长不扩域。QML只传绘图区内的逻辑鼠标增量，不另持有轴范围。视窗跨度小于数据域且与域相交时保持跨度并夹在数据域内；覆盖全域、平坦/单点退化轴、没有有效点或显式完全无数据范围保持原轴。反复边界拖动为no-op。应用绘图设置及复位仍恢复准确用户轴范围；0–100%等显式范围不会被自动收窄。

图例按内容占宽，窄窗在浮动操作下方换行；图例、操作和轴外文字不启动平移。结果说明置于轴下方，普通游标读数保持单行并可查看完整提示，部分无效状态保留两行；游标变化不改变绘图区高度。直接矢量曲线保留viewport裁剪，避免Qt6.12软件离屏层在分数DPR下的纵向偏移；软件后端像素验收不能替代真实GPU或多显示器实机验收。

仿真顶部操作以原顺序响应式换行，按真实按钮内容与主题高度计算工具条高度；默认和720逻辑px宽度保持一行，533/400逻辑px可用工作区保留全部八个按钮，不用提高窗口minimum来隐藏溢出。结果区的最小高度随实际剩余空间下调，使轴下说明留在窗口内。动作、快捷键、对话框、Tab顺序与控制器所有权不变。

`FilmSpectrumPlot.themeColors` 是必传显示输入，由唯一生产消费者 `FilmSimulationQuick` 显式传入 `AppWindow.themeColors`。此入口沿用每个引擎的只读 Theme bridge，主题切换更新绑定，不新增主题写入者或全局注册；直接创建组件遗漏此输入应明确失败。曲线、图例和内联图标采用 `pragma ComponentBehavior: Bound` 及 required delegate 属性，内部引用具名外层 id，避免依赖隐式 model context。语义参见 [Qt QML ComponentBehavior](https://doc.qt.io/qt-6/qtqml-documents-structure.html#componentbehavior)，回归同时检查缺输入和 light/dark/light 响应更新。

## 验收

旧文件/全部配置 file roundtrip、配置脏状态与设计修订分离、失败/取消原子性、内存同步、发送自动计算、过期结果拒绝、部分有效曲线/全部无效曲线/用户缩放、连续页切换/侧栏折叠焦点及scroll分别报告通过、失败或未测。

## 优化配置的膜层引用恢复（2026-10-15）

优化器读取文档已有配置时，以 `(side, layer_id)` 稳定引用为唯一匹配依据。设计重建、删除膜层后，仍存在的变量保留启用状态与 nm 上下界；已不存在的旧变量只从本次恢复的编辑状态中排除，显示数量和旧引用。新膜层保留当前厚度推导的默认范围，但不自动勾选，须由用户选择后再参与优化。目标、算法与其它合法设置继续恢复。不得按层序、材料或厚度猜测旧约束对应哪一个新层。

初次载入或替换来源的恢复不会回写或标脏文档；后续显式编辑/刷新才由 session 正常持久化当前配置。重复引用、无效字段、无效数值仍整份原子拒绝；公开 `loadOptimizationConfiguration(dict) -> bool` 继续严格校验所有引用，只有文档来源恢复使用允许孤立旧引用的内部路径。来源正在计算时沿原有失效/刷新边界延期恢复，不应用迟到结果。完整恢复说明由控制器唯一持有，标题状态与侧栏不复制错误文本。

膜系优化的生产求解、解析导数及前向计算当前仍为 CPU 路径；设备目录不能把 GPU 枚举成功等同于该任务已实现 GPU 求解。设备选择继续展示服务端声明的兼容设备，但优化任务的非 CPU 项须禁用并说明原因，提交边界仍复核 CPU 绑定。此变更不修改优化算法、光学公式或文件格式。

优化窗口的进度用迭代数、评估数、最大违约值及原始进度说明显示；取消、结果应用和保存仍由原控制器处理。优化详情沿用右侧常驻折叠 rail 与双箭头，F4 切换；窄窗口展开为同一实例的覆盖侧栏，不重建业务、编辑或滚动状态，顶栏不再另设详情按钮。设备选择 Popup 的位置与高度受当前窗口工作区约束，内容超高可滚动；关闭仍吞掉外部点击，避免顺便开始计算。
