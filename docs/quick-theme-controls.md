# Qt Quick 通用主题控件

## 单行响应式操作栏（2026-10-16）

`ResponsiveOperationBar.qml` 接受原页面的直接子控件，替换顶部 `Flow`；
`spacing`、`implicitHeight` 和现有对象名继续可用。项目窗口选择器与资源下拉
固定在同一行，资源信息按钮紧跟下拉。原按钮放不下时进入“...”菜单，
不换行，也不省略操作。菜单实时引用原按钮的 `text`、`enabled`、`visible`、
`checkable` 与 `checked`，通过原按钮的原生 `click()` 执行动作；原按钮和
控制器仍是业务状态、确认、错误处理与快捷键的唯一所有者。

非按钮控件默认保留；`operationBarMinimumWidth` / `operationBarPreferredWidth`
可提供逻辑宽度，`operationBarFlexible: true` 允许状态文字缩至剩余空间。
复合控件可通过 `operationBarAction` 暴露真实按钮，以及可选的
`operationBarOverflowAnchor` 接收菜单锚点（正常显示时为 null）。
`ProjectModuleSelector` 只显示当前窗口下拉；项目名称统一显示在窗口标题栏，
不占用工具栏宽度；
`ProjectResourceControls` 根据实际分配宽度和主题控件的隐式宽度，依次呈现
新建、保存、创建副本、导入/导出，仅把放不下的动作移至自己的次级菜单；
宽度恢复后自动回到行内，不设固定宽度上限。创建副本共用一个原生按钮及
原守卫/控制器接口，菜单仅引用该按钮，不重复显示已在行内的动作。
更多入口仅在有溢出动作或保存沉积配置等独立菜单项时显示。
唯一省略按钮由整条操作栏托管，始终在所有行内动作之后；复合资源控件
不再在中间占用它的位置。菜单隐藏项与隐藏分隔线高度为零，不能留下空白占位。
工程资源名称只在资源下拉显示，包含未保存的新资源；未保存资源的真实目录选择仍为 -1，
不新增伪造的目录项。尾部重复名称隐藏，独立全局旧窗口保留其原名称识别。
内部资源动作折叠时也退出 Tab 焦点与无障碍树，并把原有焦点转到更多入口。
存在工程资源控件时，整栏溢出项注入它现有的“...”菜单；通用溢出按钮
和其重复菜单项均不创建可见入口。全局页面则使用通用“...”菜单，
任何宽度都只有一个省略菜单入口。溢出项身份不变时保留菜单对象，
实时文案或禁用状态变化不会重建正在交互的菜单项。

溢出只改变布局：原按钮移至操作栏裁剪范围外，不能被鼠标命中，并临时
退出 Tab 焦点与无障碍树；不覆盖其 `visible` / `enabled` 绑定。
当前焦点进入溢出时转至“...”按钮，恢复宽度后还原原焦点策略。
原子菜单锚在可见的“...”按钮上。宽度与可见性变化立即重新布局，
字体及其余尺寸变化由组件自有零延迟 Timer 合并，
先采样后布局，避免隐式尺寸与父布局互相绑定；`layoutPending` 仅表示
下一轮布局是否待执行，销毁组件也销毁 Timer。布局不重建业务控件、
草稿、焦点编辑器或控制器，不改变异步操作与取消边界。

定向真实 Qt 回归入口：`tests/test_responsive_operation_bar.py`，覆盖宽窄往返、
动态禁用/文案/可见性、勾选状态、快捷键、原子菜单锚点、六类工程资源
同排几何，以及窄栏保存、创建副本和导入/导出动作等价性。

标题栏状态、紧凑顶部操作与信息侧栏的共享输入/输出、唯一状态所有者和生命周期边界见 [共享标题栏状态与信息侧栏](window-status-sidebar-contract.md)（2026-10-12）。

本目录提供一组可选的 Qt Quick Controls 包装组件，供窗口按需引入。它们不持有业务状态或计算算法；业务动作仍由页面和控制器负责。颜色、字号、间距、圆角、控件高度和交互透明度来自现有 [Qt Quick 主题契约](quick-theme-contract.md) 的 `Theme.snapshot`，并随快照通知更新。

## 复用范围

当前界面已大量使用 `QtQuick.Controls` 原生 `Button`、`TextField`、`Dialog`、`GroupBox` 和 `Label`。业务输入封装包括 `SimulationValueField.qml`（焦点期间保留本地编辑文本）与 `SourceValueField.qml`（标签、输入框和提示）；`ReferenceSections.qml` 使用 `Frame` 展示分段内容。此次新增控件沿用原生 Controls 的焦点、键盘、输入法和辅助技术行为，只统一通用样式。

| 组件 | 用途 | 主要接口 |
|---|---|---|
| `ThemeButton` | 常规或强调操作按钮 | `text`、`primary`、原生 `enabled`、`onClicked` |
| `ThemeField` | 单行文本输入 | 原生 `text`、`placeholderText`、`inputMethodHints`；`accessibleLabel`、`accessibleDescription` |
| `ThemeSection` | 可折叠内容区域 | `title`、`expanded`、`accessibleLabel`；默认子项进入内容布局 |
| `ThemeDialogBase` | 通用对话框表面、标题和调色板 | 原生 `title`、`modal`、`standardButtons`、尺寸与对话框信号 |
| `ThemeSidebar` | 带标题的纵向滚动信息栏 | `title`、`sidebarWidth`；默认子项进入内部 `ColumnLayout` |
| `ThemePopupHeader` | 有业务状态的弹窗标题与全文提示 | `title`、`statusText`、`statusSeverity`、`statusDetail`、`tooltipBoundsItem`；不处理 Dialog 的接受、拒绝和关闭 |

## 使用示例

从 `qml/` 下的页面引用子目录：

```qml
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    ThemeControls.ThemeButton {
        text: "保存"
        primary: true
        onClicked: controller.save()
    }

    ThemeControls.ThemeField {
        Layout.fillWidth: true
        accessibleLabel: "样品名称"
        placeholderText: "请输入样品名称"
        text: controller.sampleName
        onTextEdited: controller.setSampleName(text)
    }

    ThemeControls.ThemeSection {
        Layout.fillWidth: true
        title: "高级选项"
        expanded: false
        Label { text: "由页面提供的内容仍属于页面本身。" }
    }

    ThemeControls.ThemeDialogBase {
        id: confirmDialog
        title: "确认操作"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label { text: "对话框业务动作和关闭时机由调用页面决定。" }
    }
}
```

若页面加载时尚未为该 `QQmlEngine` 安装 `Theme` 上下文桥，组件的 `Theme.snapshot` 绑定不可用；安装方式见主题契约。不要在页面中修改快照。

## 外观、输入与无障碍

- 组件只使用主题契约中已定义的 token。`ThemeButton` 的强调态使用 accent、hover 和 pressed 色；普通态使用 raised surface 与主题 hover/pressed 透明度叠层。焦点环使用 `colorFocusRing` 和 `focusRingWidth`。
- 字体取 `fontFamily`、`fontSizeBody`、语义字重；控件高度、间距、圆角与边框取对应布局 token。`fontSize*` 已含 `fontScale`，布局指标已含 `densityScale`，页面不应再次相乘。
- 内置 `fontFamily` 默认使用应用随包注册的 Noto Sans CJK SC；仅改变字体族不应改写用户字号设置，当前默认字号仍为 9 pt。主题包显式声明的 `tokenOverrides.fontFamily` 优先于内置值。
- 禁用状态以 `colorDisabledSurface` 和 `colorDisabledText` 表示，不整体降低控件透明度，以免文本对比度进一步变差。`opacityDisabled` 可用于合适的非文本装饰；hover 与 pressed 叠层读取 `opacityHover`、`opacityPressed`。
- `ThemeButton` 保留 Controls.Button 的鼠标、键盘和自动可访问动作名。图标按钮应在实例上提供有意义的 `Accessible.name`。`ThemeField` 优先设置 `accessibleLabel`，并可补充 `accessibleDescription`；它保留原生编辑、选择、验证器、输入法提示和控制器绑定职责。
- `ThemeField` 和 `ThemeMaterialPicker` 内的文本输入选区背景使用 `colorSelection`，选中文字和插入光标使用 `colorText`；光标由共享控件显式绘制，避免浅色主题下跟随平台样式变得不清晰。切换主题时仍由同一 `Theme.snapshot` 更新，不新增业务状态或 token。
- 折叠区标题是可通过 Tab 聚焦、可由键盘激活的按钮；`accessibleLabel` 可覆盖标题名，描述会报告展开状态。内容由调用方创建，不在收起/展开时重置业务数据。
- `ThemeDialogBase` 将对话框名称与 `Accessible.Dialog` 角色附加在实际视觉 `contentItem`（`Item`）上。Qt 的 [Dialog 类型](https://doc.qt.io/qt-6/qml-qtquick-controls-dialog.html)继承自 Popup，而 [Accessible 附加类型](https://doc.qt.io/qt-6/qml-qtquick-accessible.html)用于 QML Item，因此语义属性放在视觉内容项而不是 Dialog 对象上。它保留原生 Dialog 模态、标准按钮、关闭规则、大小和信号行为，具体由调用方指定；内容与控件可按原生 Controls 组合。

## 坐标与边界

主题间距、高度、圆角和边框宽度均为 Qt Quick 逻辑像素，字号是逻辑 pt。Qt 与操作系统负责屏幕 device-pixel scaling；QML 不要额外乘 `Screen.devicePixelRatio`。中文字体缺字时交由 Qt 字体回退处理，不从网络加载字体。

这些通用控件不设置或重映射科学图表 colormap、曲线、材料 `n/k` 数据颜色。图表与数据颜色继续由专用可视化配置负责。高对比调色板使用主题契约的项目默认值，尚不代表合规认证；这些组件也尚未做 GUI、多屏 DPI 或屏幕阅读器实机验收。

## 页面状态与交互约定

业务页面复用同一组状态语义：加载中需可见并避免重复提交；空状态与加载、失败状态分开呈现，并给出可理解的下一步；失败时保留仍有效的输入/结果，提供可访问的错误说明和合适的重试或返回操作。具体文案和恢复动作属于页面，但不得把失败显示成空结果。

保存只有在持久化成功后才清除 dirty 状态；覆盖必须有明确的调用意图。新建、打开、加载其他文档或关闭会丢弃未保存内容时，沿用共享确认语义：接受才执行，取消保留文档与当前位置；失败不更新路径或伪装成已保存。确认/编辑对话框关闭后，若原控件仍存在，应恢复其焦点；页面返回或轻量更新不应意外切换标签、重置滚动或抢走当前编辑焦点。异步响应只应用于仍有效的操作/文档代次，窗口关闭后不再更新已销毁页面。

页面尺寸、字体和间距只使用主题快照的逻辑 pt/逻辑 px；`fontScale` 与 `densityScale` 已由主题服务应用，页面不得重复缩放。屏幕 DPR 由 Qt 处理。普通顶层窗口沿用 [共享窗口几何策略](window-geometry-policy.md)；独立窗口在其规范中只记录相对这些默认行为的例外。

## 图片预览光标的窗口与生命周期（2026-10-12）

`coat_sim/ui/preview_cursor.py::PreviewCursor` 接收一个 `QQuickItem target`、
`active` 和 `normalShape`；每个实例仅拥有该目标的局部光标，不设置全局覆盖，
不接管鼠标、键盘、滚轮或窗口状态。光标位图使用目标实际视觉窗口的 DPR，
包括独立 `Popup.Window`；无附着窗口时沿用 DPR 1 的回退。

窗口从 `QGuiApplication.allWindows()` 中按 `contentItem` 的视觉祖先关系确定，
不通过 `QQuickItem.window()` 获取和保留窗口。已验证的 Linux
PySide6/Shiboken 6.12.0 中，后者会让 Python 包装器形成窗口与子项的相互
父子关系，尽管 C++ QObject 树没有变化；该现象不代表所有 Qt 版本的行为。
窗口和 QML 引擎继续由原页面所有者销毁。

零延迟的应用回调读取实例当前目标和 active 状态；重复调度合并为一次，
切换目标先释放旧目标的局部光标，停用时恢复 normalShape。目标在回调前
销毁时清除目标引用，不再操作已删除项。回归入口为
`tests/test_preview_cursor.py`，覆盖主窗口/弹窗 DPR、包装器所有权、恢复和
删除目标后的迟到回调。没有停用 Python GC，也没有通过保留已删除对象
延长页面生命周期。本变更不改变光学、采样或计算算法的数值语义。


## 统一控件接口

页面通过 `import "theme_controls" as ThemeControls` 使用以下共享控件：

- `ThemeButton` 保留 Button 的输入和信号。主动作设 `primary: true`；工具选择态使用标准 `checkable: true` 与 `checked`；无背景工具按钮设 `flat: true`。
- `ThemeField` 用于文本编辑，继续绑定原有 `text`、`placeholderText`、`inputMethodHints` 和 `onTextEdited`。
- `ThemeComboBox` 保留 ComboBox 的 `model`、`currentIndex`、`currentText` 与 `onActivated`，并统一弹出列表、悬停、选中、焦点和禁用显示。
- `ThemeSpinBox` 保留数值范围、`value`、`stepSize`、编辑器和输入法行为。用户通过键盘、编辑器或步进按钮实际改变值时触发一次 `valueModified`；程序赋值和范围边界上的无变化操作不触发。
- `ThemeLabel` 使用默认正文色；次要文字设 `muted: true`，标题设 `heading: true`。
- `ThemeSurface` 提供面板底色、边框和布局间距；`ThemeSection` 用于可折叠分组；`ThemeDialogBase` 用于统一弹窗外观。

`AppWindow` 同步窗口 palette 的 window、surface、text、button、placeholder、highlight、link 与 disabled roles。需要跨 Qt Controls style 保持一致时，按钮、编辑框、下拉框和数值框请使用共享控件，不依赖 palette 单独覆盖其绘制。

通用图表可用 `colorBorder` 绘制网格、`colorBorderStrong` 绘制轴线、`colorTextMuted` 绘制次要标签；材料分类色、n/k 蓝橙色和 colormap 由科学图表组件保留，不受通用控件主题重映射。

## 新窗口布局评审与验收

新增窗口开始实现前，由主协调助手审核一份简短布局方案，说明信息分组、主次操作、窗口宽度与可用空间、滚动及缩放/小屏适配、主题统一。审核通过后再实现；完成后通过真实渲染和实际交互验收，不能只查静态代码。此要求只针对新增窗口，不增加每次局部微调的文档或用户审批流程。


## 数值输入与展示

数值 `ThemeField` 可设置 `numericDisplayValue` 绑定原始数值。未聚焦时由共享 `NumericFormat.js` 格式化为最多 3 位小数；接近零且会被舍入成 0 的非零值改用科学计数法，尾数最多保留 3 位小数。该属性只控制展示，计算、控制器和文件继续持有原始值。数值编辑时不改写输入文本；原值变化、组件失焦后才同步展示文本，因此仅聚焦、失焦不会改变原值或触发 dirty。

所有可编辑输入每次重新获得焦点默认全选，便于直接覆盖；同一编辑会话内再次点击仍允许定位光标与局部选择。共享 `InputFocusBehavior` 在鼠标释放完成原生定位之后全选，Tab / Shift+Tab 与程序焦点在事件后全选；拖动选择和中文 IME 组合保持原生行为。`ThemeField`、可编辑 `ThemeSpinBox` / `ThemeComboBox` 和材料搜索框复用该机制，同应用的窗口观察器补齐原生 `TextInput` / `TextEdit` / `TextArea` 及复合控件内嵌编辑器；只读输出与日志不参与。保留 `ThemeField.selectAllOnFirstFocus` 名称用于兼容，含义是每次新焦点会话是否默认全选。只有设置 `numericDisplayValue` 才启用数值展示格式化；名称、ID、路径、表达式和自由文本不设此属性、不做小数格式化。validator、范围和提交逻辑仍是输入真值；格式化不参与校验或写回。


## 表格字段键盘导航

需要逐行编辑的表格列可将 `ThemeField.handleColumnNavigation(event, requestFocus)` 绑定到同列相邻行：`↑`/`↓` 与 `Enter`/`Shift+Enter` 分别移动到上一行/下一行/上一行。目标行通过 `focusAndSelectAll()` 获得焦点并全选，便于继续覆盖输入。IME 正在组合时先由 TextField/输入法处理；若 Up/Down/Enter 仍未被处理，则在字段内拦截，避免冒泡触发行导航或提交；列表首尾仍拦截导航键但不移动焦点，避免误触发对话框默认操作、增删行或计算动作。未接入该处理器的 ComboBox、表达式文本区和其他控件保留其现有键盘行为。

## 膜系设计响应式表单与符号表（2026-10-14）

`FilmDesignQuick` 的 workspace 是编辑列宽度的唯一所有者。填充该列的 TabBar、表头和页面不把 `parent.width` 当作自身布局最大值，避免当前尺寸反过来约束父布局，导致小窗恢复大窗后保留旧宽。表达式区展开时，小于 1030 逻辑 px 的窗口把侧栏放到编辑区下方；收起时保留原 rail。展开、收起、切换页面或缩放不重建控制器、表单、符号模型或表达式草稿。

基片与介质页按实际可用列宽使用四列标签/值对或两列堆叠，低于约 640 逻辑 px 时优先堆叠。材料、光学数据集、实际厚度与单位、单/双面继续使用原 controller 接口及数据语义。基片与表达式 ScrollView 按原生 ScrollBar 的实际宽度预留空间，不重复缩放 Theme token 或 DPR；窄窗口和大字号下保留纵向滚动，不能通过横向裁切隐藏参数。

表达式文本、操作行、列头与符号列表显式允许宽度收缩，列头与 delegate 继续共用 `FilmDesignLayerColumnMetrics`。多行 QW/nm 列头高度跟随实际文本，列表高度读取实际内容高度，不再按固定 38 px 估计行数；单字符号字段使用紧凑主题 padding。膜层和符号表既有同列 Up/Down、Enter/Shift+Enter 导航、焦点全选和 IME 边界保留。回归入口是 `tests/test_film_design_layout_geometry.py`，涵盖默认/窄/大窗口往返、反复展开、页面切换、浅色/深色与 125% 字号/密度、四行符号及同列键盘导航。

膜系局部组件的新增布局/字体绑定通过显式 `themeTokens` 输入读取同一主题快照：`FilmDesignQuick` 从 `AppWindow.themeColors` 传给表达式面板与列尺寸对象；基片页从其 `editorWindow.themeColors` 传给介质编辑器。该输入只用于显示，不拥有或复制业务状态。嵌套委托使用 Bound 词法作用域和明确的组件 id，不通过泛型 `parent` 猜控件类型。

## Film generator numeric source values

FilmStackDialog owns the total-thickness and reference-wavelength inputs through ThemeField.numericDisplayValue. buildParameters reads those source values; localized and rounded display text must never become a numeric execution parameter. Text edits update the source, while formatting on focus loss changes presentation only. The equal-stack default remains exactly 7000 nm, including locales that display it as 7,000.

The offscreen Qt regressions in `tests/test_windows_runtime_regressions.py` cover grouped en_US/de_DE defaults, repeated focus/blur, fractional text edits for both generator modes, and invalid input rejection without changing the draft.


## 输入提交、失焦与延后操作（2026-10-15）

单行输入继续由原生 validator、accepted / editingFinished 和页面提交逻辑验证、写回；共享策略在原生 accepted 已触发后使用控件自有零延迟 Timer 失焦。验证器拒绝的 Enter 不提交、不移走焦点。原生 Qt 在 Enter 和随后失焦时均可能发 editingFinished，业务提交应维持现有幂等约定。SpinBox / ComboBox 失焦释放其真实焦点作用域，避免仅清除子编辑器 focus 后又被原生控件恢复。

每个 QQuickWindow 的自有被动事件过滤器始终返回 false，不创建覆盖 Item，也不改变 WheelHandler 或原生模态 Overlay 的命中路径；点击空白或不取焦点的按钮后再结束旧编辑；不消费按钮点击、菜单选择、滚轮或拖动，也不从新编辑器抢焦点。多行表达式、备注与聊天只统一新焦点全选，既有换行 / 发送规则保留；表格的同列 Enter / Shift+Enter 导航继续先拦截，避免触发窗口默认动作。

材料选择器新获焦保留并全选当前材料名称，未输入时仍展示完整候选；仅真正编辑的查询才筛选或按唯一精确名称提交。弹出候选在首次选择完成后打开，避免覆盖层在鼠标按下期间取消原生释放。点击候选仍只触发一次 activated；无匹配查询按 Enter 恢复原选择并失焦。

应用的 ThemeEngineBridge 在 QApplication 已创建后安装一次窗口观察器入口；每个窗口拥有各自的观察器和 Timer，仅持有编辑器弱引用，销毁后不再处理。独立聊天窗口与弹出 Dialog 共用同一窗口观察入口，不需要逐页添加捕获层。焦点选择、数值展示同步和材料失焦处理均使用所属组件的 Timer，不保留可在 QML 上下文销毁后运行的 Qt.callLater 闭包。`tests/test_input_focus_policy.py` 用真实 Qt 鼠标 / 键盘覆盖重新获焦、局部拖选、复合输入、材料菜单、表格、多行、IME 和销毁前迟到操作，并以仍存活的旧式 callLater 回调输出作日志捕获负对照。该警告的消除不等于确认其他长序列 SIGSEGV 的根因。
