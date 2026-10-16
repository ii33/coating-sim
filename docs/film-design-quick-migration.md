# 膜系设计 Qt Quick 迁移对照

日期：2026-10-11

本文记录膜系设计窗口的基线功能、Quick 文件职责和本轮验证边界。迁移采用独立 Quick 控制器与 QML 页面；本轮没有修改旧 Widgets 窗口或共享入口。

## 功能基线

旧 `FilmDesignWindow` 的设计能力包括：新建设计；JSON 文件打开和保存；SQLite 设计库读写及表达式模板；名称、备注、参考波长、`use_k`、`assume_lossless`；基片材质、数据集、独立厚度单位、单双面与自动模式；入射/出射介质（空气常数 n=1 或材料数据集）；正/背面层的新增、删除、排序和编辑；QW 与物理 nm 并列；多行表达式、A–Z 数据集符号、折叠和展开；G1/G2 与模板展开。

权威 schema 校验、文件和 SQLite 读写、材料及表达式数据由既有 `FilmDesignService` 与 core 完成。Quick 控制器复用这些 API，不复制光学计算或表达式解析实现。QW 物理厚度继续调用 `physical_thickness_nm` / `prepare_optical_set`。

## 本轮专属文件

| 文件 | 职责 |
| --- | --- |
| `coat_sim/ui/film_quick_controller.py` | Quick 状态、稳定 ID 模型、设计读写委托、材料/数据集 picker 数据及派生列。 |
| `coat_sim/ui/qml/FilmDesignQuick.qml` | 三标签设计页面、结构示意、表达式和参数区。 |
| `coat_sim/ui/qml/FilmDesignLayerRow.qml` | 膜层编辑行、QW/nm 列及行内材料和数据集 picker。 |
| `coat_sim/ui/qml/FilmDesignMediumEditor.qml` | 入射/出射介质编辑及材料/数据集 picker。 |
| `coat_sim/ui/qml/FilmDesignExpressionPanel.qml` | 多行表达式、符号、模板和提交控件。 |
| `coat_sim/ui/qml/FilmDesignLayerColumnMetrics.qml` | 层表表头与列宽对齐。 |
| `tests/test_film_quick_controller.py` | 控制器级 schema、读写、稳定 ID 和派生属性覆盖。 |
| `tests/test_film_quick_material_bindings_qt.py` | 材料/数据集实际选择、单双面、布局及 round-trip 的定向 Qt 用例。 |

本轮只新增或调整 Film 专属文件；未修改 `film_design.py`、`film_simulation_settings.py`、主题服务、材料库源码或共享入口。

## 材料与数据集绑定

材料 picker 复用材料库已有记录，并按稳定 material ID 合并 medium、film、substrate 三类可绑定选项；过滤掉没有 optical set 的记录，Air 排在首位。新设计在 canonical Air 记录和 `ideal_air_n1_k0` 数据集存在时默认使用它们。材料目录仍由材料库提供，本控制器不新增或复制材料数据。

2026-10-12 补足 shipped catalog 的理想 Air 数据集，明确为本项目 n=1/k=0 数学工程模型；1 nm–1 mm 仅为软件序列化/求值域。模型、来源和普通 copy/override/restore 边界见 [理想 Air 数据集契约](ideal-air-optical-contract.md)。测试不再注入 synthetic Air 掩盖真实目录缺项；picker 和 reference bridge 仍保留精确数据集所属校验。

基片与介质使用各自的数据集模型，避免误用介质列表查询基片数据集。有效的旧数据集选择保持不变；当记录没有有效选择且未声明 preferred/default ID 时，沿用现有 optical set 顺序选择首个有效数据集。基片厚度属性独立保存。单双面切换时保留出射介质草稿，单面状态下禁用出射编辑。

独立 `FilmDesignSubstratePage.qml` 通过 `editorWindow` / `pageTabs` 输入显示基片与介质字段，页面不另持一份设计状态。其数据集激活把 index 交给主窗口 `commitSubstrateSetSelection(index)`；主窗口从当前 controller 的 `designMeta.substrate.material_id` 与 `opticalSetsForSubstrate` 解析稳定 ID，再委托会话提交，不读取独立页面的私有 QML id。重复选择已有 ID 不提交或推进修订。2026-10-12 的原始页面恢复、独立回调证据及尚缺 `FilmDesignMediumEditor.qml` 的加载阻断见 恢复记录（历史验证记录，未随公开源码发布）；独立回调通过不等于加载页完整验证。

旧常数 n/k 介质保持原 schema 值；只有用户明确选择材料后，介质才改为材料绑定。层和表达式行由稳定 ID 定位，QML picker 展示短备注及 n/k 范围，不把整份光学数组复制进 QML。

## 文件格式和计算边界

Quick 文件对话框使用 `.cfilm` filter/default suffix，文件内容仍委托既有 service 按 schema v1 序列化；扩展名不会改变 payload。Quick 不会自动把 `.json` 文件改名或迁移为 `.cfilm`。设计 JSON 内的 `substrate_outward` 层序、正/背面数组、基片、介质和表达式结构由旧 schema/service 约束。

厚度换算、材料光学值、表达式展开和物理仿真仍由现有 core/service 负责。本轮只改选择、呈现和绑定路径，没有新增物理计算或几何算法，因此无需改动 `docs/algorithms.html`。

## 跨窗口状态编辑边界（2026-10-11）

每个设计窗口的 `FilmDesignQuickController` 独占一个 `FilmDesignEditSession(service)`；设计、脏状态、文件路径、错误、待确认操作和 `design_revision` 都由该会话持有。膜系仿真与优化通过准确的来源 controller/session 对象加入这一组，不按文件路径、设计名称或“当前活跃窗口”猜测归属，也不创建平行草稿。core 真值位于 `coat_sim/core/film_design_session.py`：`snapshot() -> SessionSnapshot` 返回隔离视图，`subscribe(callback) -> unsubscribe` 通知提交后的变化，`apply_optimized_design(candidate_design, base_design_revision) -> bool` 在同一锁内校验 revision、校验候选并原子写回。Quick controller 还在每次字段输入时推进 `pendingEditEpoch`，通过 `applyOptimizedDesign(candidate_design, base_design_revision, base_edit_epoch) -> bool` 在委托会话前拒绝尚未提交的冲突草稿。

设计页的“发送到光谱仿真”在提交当前焦点编辑与表达式草稿后，将来源 controller 交给 launcher。launcher 按同一 session 对象复用已打开或尚在加载的配对仿真；第一次打开才创建窗口。配对 `FilmSimulationQuickController` 保存来源 controller/session 引用并订阅设计变化。会话设计更新会换入最新 snapshot、使旧计算失效并刷新页面，但普通设计编辑不自动启动计算；设计页显式发送、按 F5 或点击“计算并绘图”用最新未保存设计与当前仿真参数提交计算。仿真参数、统计面板和绘图设置由该仿真 controller 编辑并提交到来源 session 的 `workflow_settings.simulation`；计算结果和临时缩放仍由仿真 controller 独占。

优化请求冻结提交时的 `design`、`design_revision` 和来源编辑 epoch；队列、job ID、取消状态仍由现有 scheduler 所有。仅当结果为正常 `completed`、本次 operation token 仍有效、优化器尚未进入关闭/取消、来源窗口与文档仍有效、编辑 epoch 未变化且当前 session revision 与请求一致时，Quick controller 才调用 `apply_optimized_design`。来源 controller 在该原子写回成功后通知配对仿真刷新。若仿真正运行旧计算，旧回调不能清除新 operation 的 busy 状态；自动刷新合并为一个 pending recompute，旧计算收尾后用最新 snapshot 启动一次。用户在优化期间编辑即使尚未触发 `editingFinished` 也会推进编辑 epoch，结果会显示冲突并拒绝写回；candidate 留在优化窗口供查看/另存。取消、失败、关闭、文档替换、旧 token、revision 冲突或候选校验失败均不写回；错误在对应窗口显示。`targets_met=false` 保持可见且不会显示为达标。

设计页 F5 先提交当前焦点字段和表达式，再打开或激活该设计 session 的仿真并计算；F6 同样先提交输入，再打开或激活配对优化并开始一次优化。仿真页 F5 重算当前设计，F6 打开或激活同组优化并开始一次优化。未完成加载的窗口会记录单次 start 意图，重复按键复用同一窗口和操作。F4 展开/折叠设计表达式区或仿真统计区。快捷键与按钮共用防重入入口。窗口关闭后其异步结果不再投递；进入核心计算的 Python 工作无法强制中断，只能在完成边界丢弃。加载新设计只有在确认并成功后替换 session 草稿、推进 design revision 并同步配对仿真；失败或取消保留原草稿和图表。配对仿真中显式加载另一膜系或设置另一设计 JSON 后会脱离原 session，之后 F5 只计算这份独立设计，优化结果也不会写入原设计。

Qt controller 只投影会话 snapshot 与稳定 ID 行模型。QObjects、QML、`QSettings` 和 `QRunnable`/`QThreadPool` 生命周期留在 UI adapter；session 不反向依赖 UI。拒绝的编辑不得部分改动草稿；保存/加载只有在 service 操作成功后才提交新路径与 dirty 状态。异步表达式操作携带基础修订，过期结果不覆盖新编辑。快照隔离可变编辑数据，但不得在逐次输入热路径复制完整 n/k 光学样本数组。

**当前实现与接口真值：** `coat_sim/core/film_design_session.py`、`coat_sim/ui/film_quick_controller.py`、`film_simulation_quick_controller.py`、`film_optimization_quick_controller.py`、`launcher.py` 和对应 QML 页面决定类型、签名、输入输出及错误处理；本节只规定职责边界，不复制一套接口。2026-10-13 的可选 `workflow_settings` 扩展和设置所有权见 [膜系工作流文档契约](film-workflow-document-contract.md)；物理字段和 schema v1 版本保留。后续改动触及这些边界，仍按 [算法与格式验证契约](film-design-validation.md) 和项目 `AGENTS.md` 更新相应文档。

## 默认启动的异步初始化契约

当 FilmDesignQuickController 未传入 service 时，controller 先在 GUI 线程建立窗口所需的 Qt 对象和明确标记为未就绪的占位快照，再通过 QThreadPool 读取该窗口 service 的材料选项、构造单窗口 FilmDesignEditSession、生成 SessionSnapshot 与初始 picker 投影。worker 不创建或操作 QObject、QML 模型或 QSettings；完成结果由 controller 在 GUI 线程一次性安装 session、快照和 picker 数据后发布就绪状态。显式传入 service 的调用默认保持同步，测试或调用方可用 defer_initialization 明确选择模式。

FilmDesignQuickController 是初始化状态的唯一所有者：initializationState 的取值为 loading、ready、failed 或 cancelled；initializationReady、initializationInProgress、initializationError 由 initializationChanged 通知。页面在 ready 前以可见遮罩阻断编辑，加载中显示等待状态，失败显示错误并提供重试。重试使用新代次，旧代次结果不会应用。用户在加载期间关闭窗口时，controller 将结果标为取消并允许关闭；这不会中断已运行的只读 Python 目录读取，只会在完成边界丢弃结果。每个窗口仍创建自己的 session，不共享草稿。FilmDesignQuick.qml 与 FilmDesignQuickController 是该输入、输出、失败及取消边界的真值实现。

### 膜层重排会话接口

`FilmDesignEditSession.move_layer(side, row_id, delta) -> bool` 继续只支持相邻移动 `delta ∈ {-1, 1}`。`move_layer_to_index(side, row_id, insertion_index) -> bool` 接收插入间隙索引，合法范围为 `0..rowCount`；源行位于目标间隙之前时，移除源行后将目标位置减一。该接口在 session 锁内完成数组重排并只产生一次 dirty、revision 和订阅通知；无效 side、行 ID 或索引不修改草稿。`FilmDesignQuickController.moveLayerToIndex(...)` 将 UI 插入间隙直接交给该原子接口，避免拖动多行时发布中间顺序。

## 验证状态

Python AST 静态解析通过 controller 与两份 Film controller 测试文件。`FilmDesignQuickController.metaObject()` 确认 `setSubstrateValue(QString,QString)`、`setMediumValue(QString,QString,QString)` 已注册，并确认 `setDesignValue` 原有 PyObject/String/bool overload 保留。

定向 Qt 用例 `test_direct_air_zns_nbk7_selection_datasets_layout_and_round_trip` 已通过，1/1，11.535 秒，进程退出码 0。它实际点击选择基片 N-BK7/ZnS、入射 ZnS 与两个 optical set 后回到 Air、出射 Air/N-BK7；还验证单双面切换与出射值保留、`use_k` 位于参数网格、`.cfilm` 保存重开，以及旧常数 n=1.37 读写保值。

调试期间确认材料选择后可见窗口的 C++ 地址不变，窗口仍在 `QGuiApplication.allWindows()` 中；失效的是 `QQmlApplicationEngine.rootObjects()` 返回的 Python wrapper，因此测试从当前可见窗口重新绑定 QML 控件。实测还发现基片数据集 ComboBox 的 `currentValue` 在切换到 N-BK7 后未跟随设计值，已改为声明式 `currentIndex` 绑定；并在 sides 值实际从 double 改为 single 时才切回正面页，重复激活 single 不再跳页。

成功运行的持久日志：`build/film_quick_qt_validation/material_bindings_sides_guard_20261009-030448/`（`stdout.log`、`stderr.log`、`result.txt`）。本轮未运行仿真、完整测试套件或构建，不记录性能结论。

## 膜系快捷键与折叠焦点（2026-10-11）

设计页 F4 与侧栏按钮调用同一折叠函数；折叠前若焦点仍在侧栏，先把焦点移至页签，使输入控件完成 `editingFinished`。2026-10-13 的统一侧栏保留 64 逻辑像素功能条与 24 逻辑像素折叠按钮，窄窗口改为底部水平功能条；高亮背景只包住固定高度按钮，不沿整列拉伸。

设计页 F5/F6 先提交当前编辑字段和表达式草稿，再通过 launcher 复用准确 session 配对的仿真/优化窗。优化窗 F6 调用 `startOptimizationFromShortcut()`：若配对设计版本已更新，先刷新设计并保留可匹配层的变量范围，再校验输入并只提交一次。脱离来源的仿真窗口以自身实例 token 标识一个只读优化快照；重复 F6 复用该优化窗，窗口仍在加载时仅更新待加载快照和一次启动意图。

2026-10-12 补充：用户原始 `FilmDesignMediumEditor.qml` 已恢复，与此前恢复的
`FilmDesignSubstratePage.qml` 组成完整基片/介质页依赖。原文件 SHA256 与验证记录见
`docs/remediation/20261012-progressive-refactor/SUBSTRATE_PAGE_RESTORATION.md`。

## 2026-10-13 工作流与显示补充

设计、优化、仿真已提交配置通过同一 session 保存到 `.cfilm`；旧文件缺配置时恢复默认，无文件时继续内存同步。保存、覆盖、关闭确认沿用 session，配置独立于物理设计revision。设计显式发送光谱仿真自动请求计算；重复/忙碌意图合并，不以文件路径重建同步。

标题摘要按全部正/背面膜层及材料ID汇总物理nm，不含基片；显示三位小数，完整内部精度保留，无法换算层明确标记。窄侧栏toggle为24逻辑像素；各section保活，切换只恢复最后有效focus意图与各自ScrollView状态。参数化方法的选型提示来自既有模板applicability。
