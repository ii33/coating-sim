# 共享标题栏状态与信息侧栏

日期：2026-10-13。此契约只改变 UI 展示与布局，不改变物理计算、文件格式或数值数据。

## 标题栏接口与所有权

接口真值是 `coat_sim/ui/qml/AppWindow.qml` 的属性和 `WindowChrome.qml` 的实现：

| 属性 | 类型 / 默认 | 页面职责 |
|---|---|---|
| `caption` | string，默认窗口 title | 业务窗口名、文件名和 dirty 标记；保留原有文件所有权 |
| `statusText` | string，空 | 当前操作提示；空文本不显示状态控件 |
| `statusSeverity` | string，`info` | `info / success / warning / error / busy`；由已有业务状态决定，不从提示文本猜错误 |
| `statusDetail` | string，空 | 完整说明；空值时使用 statusText 全文 |
| `titleBarHeight` | readonly real | 共享标题栏高度 |
| `contentTopMargin` | readonly real | 顶部操作的 y 或顶边距，紧接标题栏 |

页面/控制器是状态、文件名和 dirty 的唯一所有者。共享控件无通知队列、计时清除、保存、重试或异步业务动作。页面把现有底部提示绑定到这些属性，并去掉底部状态占位；不同页面原有的加载、空结果、失败详情和重试入口继续保留。错误使用“错误”文字和完整可访问说明，不只依赖颜色。

标题栏正文为单行省略；caption 保留完整可访问文字与悬停提示。状态区域是纯展示：单行省略显示严重性标签和当前短文本，Accessible 名称包含完整状态说明；状态文本不接受鼠标/滚轮、不进入键盘焦点顺序、不支持文本选择，也不显示 tooltip 或弹层。状态区域上的按下/移动/双击事件继续由既有标题栏拖动区处理，以保留拖动和双击最大化/还原；状态区域右边界在系统窗口按钮之前，系统按钮仍可点击。关闭按钮仍调用窗口既有 close/dirty 确认路径。标题栏高度与主题尺寸使用逻辑 px，窗口移动/缩放沿用既有系统逻辑。

所有尺寸为主题快照的逻辑 px；字号为逻辑 pt，不重复乘 fontScale、densityScale 或 DPR。`contentTopMargin` 随共享控件高度与间距变化，页面不再使用独立的 46/50/54 px 标题占位。普通窗口继续遵守 [共享窗口几何策略](window-geometry-policy.md)。

## 信息侧栏接口与边界

`theme_controls/ThemeSidebar.qml` 提供 `title: string`、`sidebarWidth: real = 320` 和默认子项 `body`（内部 ColumnLayout 的 data）。组件内置纵向 ScrollView、主题表面和边框；调用页面控制真实宽度、可见性及窄窗口布局。业务内容和展开/选中状态仍由页面/控制器持有。不要在重复打开、收起或轻量状态刷新时重建业务对象或清空编辑草稿。

侧栏长名称用标签/值网格或换行布局；中英字段继续保留“中文 (English)：”表达，单位和数值不被省略。加载、空数据、失败需由窗口明确展示，失败时保留有效结果及错误详情。折叠使用共享 `ThemeSection`，内容不因展开/收起被销毁。保存/覆盖/关闭确认、焦点和滚动恢复、异步响应代次、取消及窗口销毁边界沿用 [共享页面与控件规范](quick-theme-controls.md)。

## 本批文件所有权和接入范围

- 本 UI 主持任务：AppWindow、WindowChrome、ThemeSidebar、MainWindow、ProcessWindow，以及点数任务冻结后接手的 FilmSimulationQuick、FilmSpectrumPlot；仿真控制器只允许绘图默认的定向补丁并保留已有点数/取消修改。
- 设计库任务 `01a12cb8-f87a-7069-8fed-d99b4a04e0d4` 独占 FilmDesignQuick；修正板任务 `01a12cda-56de-7044-ab4c-01bee7b3f0f6` 独占 MaskWindow，由各自作者按本接口接入。
- FilmOptimizationWindow 由针插与隧穿任务 `01a12d0a-4091-72d9-a866-72044b8d40de` 拥有，由该作者按本契约接入，不由本批修改。
- MCP/CLI/能力服务属于接口解耦任务；本批不竞争修改。其他窗口的实际接入与验证须单独列清，不能仅凭共享组件改变就宣称所有窗口已经迁移。

其余窗口沿用同一标题栏接口；原控制器或页面属性继续拥有业务状态、文件名/dirty、保存与取消边界。迁移只替换展示绑定并释放原底部状态占位，不改变校验或任务执行。表单字段旁的校验、说明、结果摘要和队列项自身状态保留在原上下文。没有业务状态的窗口或对话框保持空 `statusText`，不合成提示。AssistantChatWindow 直接使用 WindowChrome 的同名状态属性和公开 `barHeight`，不引入不存在的 AppWindow 属性。About/Launcher 的内部插图位置不是标题栏边距，只有经上下文确认的主内容或操作区偏移才调整。

## 有业务状态的弹窗标题

本节的 `ThemePopupHeader` 是普通 `Dialog/Popup.Item` 内的内容 heading，不是系统窗口标题栏，也不复用 `WindowChrome` 的拖动命中区。`MaterialLibraryDialog` 的独立模式使用它；hostedInWindowChrome 模式将内部标题/状态设为空，真正的窗口状态仍由宿主 `WindowChrome` 纯展示。下面保留的弹窗全文阅读交互不会覆盖系统窗口按钮或阻断标题栏拖动/双击，不属于上节的系统标题栏状态交互。

弹窗全文提示放在该标题下方的实际剩余空间中，空间不足时滚动，不上移覆盖父窗口的关闭键。悬停展开后第一次点击状态仍须取得键盘焦点；Esc 只收起全文提示，随后状态更新应解除提示收起状态，并将全文滚动位置复位到首行。这些是控件自身的展示状态，不复制或清除业务消息。

MaterialLibraryDialog 的独立模式是父窗口内的 `Popup.Item`，有独立标题和业务操作结果，使用 `theme_controls/ThemePopupHeader.qml` 显示标题状态，不能借覆盖父窗口状态来制造第二所有者。既有 ThemeDialogBase/WindowChrome 的实现保持冻结，不因弹窗头部而改变已验收窗口。

`ThemePopupHeader` 的公开输入是 `title: string = ""`、`statusText: string = ""`、`statusSeverity: string = "info"`、`statusDetail: string = ""` 和 `tooltipBoundsItem: Item = null`；调用方传当前窗口的 `Overlay.overlay` 作为提示边界。状态字段的含义、严重性文字和完整 Accessible 名称与主标题栏一致；悬停/聚焦全文和长文本滚动只属于本节的内容弹窗 heading，不沿用到无交互的系统窗口标题栏。标题和状态各自单行省略，不能把完整长状态直接撑高标题；完整提示限制在窗口内，键盘可聚焦并滚动到末行。组件不提供窗口按钮、不执行 accept/reject、不处理业务取消，也不持有可变业务状态。弹窗原来的 modal、标准按钮、关闭/覆盖确认和焦点恢复仍由原 Dialog 所有。

材料库状态唯一来源仍是 library bridge。hostedInWindowChrome 模式隐藏 Dialog 原底部状态，由 MaterialLibraryHost 从已加载实例的桥接派生 statusText/statusSeverity/statusDetail；实例未就绪时状态为空，原中心加载/失败/重试 UI 保留。实例就绪后 invalid 显示原 error，已有 loading 显示 busy，否则显示原 dirty 前缀与 status；完整状态来自相同表达式。独立模式将这条原表达式绑定弹窗头部，同样去掉底部占位，字段错误和 n/k 局部加载提示保留原处。该桥接不被重建、不通过状态同步改变选中材料、草稿或异步结果。

2026-10-14 工程资源迁移：普通设计窗口的旧版包浏览弹窗已经移除，其 QML 组件不再发布。兼容 reader/exporter 与旧文件读取仍保留，参见[冻结输入与旧版兼容](portable-assets.md)。工程包有独立的启动器入口与工程身份契约，不能由旧结果浏览弹窗改名代替。源、基片和材料的操作栏拥有模块选择器与文件操作；状态仍由各自 bridge 提供，取消与关闭沿用现有草稿确认路径。

## 指定布局与绘图行为

MaterialLibraryDialog 独立模式与宿主共用画布时，从包含它的 Window 读取已有 `contentTopMargin`（宿主未提供时保持原居中行为），让头部状态命中区和全文提示避开宿主关闭控件。顶部位置增加后，可用高度也必须相应收缩，保证较大的 Theme token 下弹窗底边仍在父项内；所有值是逻辑 px，不再次乘 DPR。hosted 模式继续使用原位置/高度规则，状态只由 MaterialLibraryHost 从同一 library bridge 派生。该边界不引入新的业务状态、关闭动作或文件语义。

膜厚仿真右侧为与膜系页面同风格的可折叠均匀性单 rail 栏，只显示 PV/RMS 等均匀性指标；数据定义、状态及 F4 边界见 [沉积均匀性侧栏契约](deposition-uniformity-sidebar-contract.md)。工艺“方案文件”“机器结构预览·俯视”“温度与真空”分别折叠，修正板优化进入侧栏。膜系图表“放大、缩小、复位”及操作说明放入视图浮层，数值诊断从侧栏入口打开；缩放、拖动、重绘和采样悬停保持。新仿真默认只显示反射曲线；用户显式绘图配置及全量计算/导出数据继续保留。这是展示默认，不修改求解算法。

ProcessWindow 的 `optimizationPanelOpen` 是窄窗口优化侧栏可见性的唯一所有者。宽度小于 1120 逻辑 px 时，顶栏 `optimizationPanelToggle`、侧栏关闭按钮和遮罩点击只改变这个 bool；宽度达到 1120 时侧栏固定显示。`processOptimizationSidebar` 只创建一次，不使用 Loader，抽屉切换不重置编辑值或内部滚动，不取消正在运行的优化。窗口关闭继续走原有 `cancelOptimization()` 与 pendingClose 边界；保存、覆盖和发送前保存确认沿用原控制器路径。当前页面的关闭处理只拦截正在运行的优化，没有 dirty 关闭确认；本批没有新增该确认。三个折叠区分别持有展开状态，收起时保留内容实例。标题栏用首条校验错误作摘要，`statusDetail` 保留全部错误和原状态说明。

FilmSimulationQuickController 的配对仿真已提交设置由来源 session 的 `workflow_settings` 唯一持久化；独立仿真拥有自己的文档保存与 dirty 状态。计算结果与临时 pan/zoom 仍由仿真内存独占，不变成设计文件中的第二份结果账本。`setPlotSettings` 的显式曲线选择及优化 handoff 复制的曲线继续生效；来源同步与输入替换按 [膜系工作流文档契约](film-workflow-document-contract.md) 保留对应配置、确认、代次与取消边界。标题/dirty 读取公开文档状态，原动作区的设计名、路径及内存来源说明继续保留。MainBridge 的 status/busy 展示沿已有类型化状态，不从字符串猜严重级别；原失败文本仍完整可访问。共享 severity 属性用于已有类型化错误状态的页面。

验收使用各作者隔离 Qt 设置与网络的实际渲染及交互测试，报告 DPI、主题、窄窗口、长状态、反复切换与生命周期的实际覆盖。
