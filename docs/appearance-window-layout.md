# 外观光追窗口布局与 UI 调用约定

日期：2026-10-13。`FilmAppearanceWindow.qml` 与光追入口复用
`AppearanceWorkspace.qml`；光追入口默认先选择资产并打开同窗外观工作区，
几何追迹保留为“几何光追（高级）”模式。布局参考
`FilmSimulationQuick.qml` 的紧凑操作栏、中央视图和右侧信息栏，复用共享
AppWindow / ThemeButton / ThemeField / ThemeSection。
GPU 接入后，本窗口复用现有冻结资产请求，CPU 与 CUDA 进程各自独立；只在显式选择 CUDA 后启动异步设备探测。

## 布局与状态所有权

- 顶部单行操作栏显示冻结来源摘要及全文提示，提供参数栏开关、取消、渲染。
  渲染为主动作；加载面光源/环境 SPD 的原入口位于各光源分组。
- 中央视图保留几何相机预览和最终渲染结果，两种视图切换及操作说明悬浮在
  视图内；相机缩放/重置使用原控制器的 gesture/zoom/reset 调用。
- 空窗与载入资产后均使用顶对齐的资产、光源、相机、计算四类导航，
  每次显示一个参数页。四页常驻同一个 StackLayout，各自 ScrollView 保留
  滚动位置与编辑控件；切换类别不重新创建参数。资产页显示继承来源和
  实体厚度，光源页包含面光源和环境标量 SPD。页内 ThemeSection 折叠
  不会重建编辑控件。
  整栏收起为窄轨，保留展开入口。F4 和栏内/顶栏按钮切换同一个
  `parametersExpanded`（由可复用 workspace 所有）；收起前焦点回到顶栏
  按钮，保留原失焦提交语义。
- 普通与窄窗口始终保持中央视图在左、参数栏在右，不把视图挤到表单下方。
  展开栏宽度在 300–370 逻辑 px 之间随可用宽度调整，收起为 44 逻辑 px。
  字体、间距、控件高度来自 Theme token；Qt 负责 DPR，页面不重复缩放。
- `AppearanceWindowController` 继续独占冻结来源、draft、phase/status/error、
  相机姿态、已完成结果和本地渲染进程；workspace 仅拥有分组/侧栏展开
  状态与 `previewMode`。类别 `selectedCategory` 为瞬时 UI 状态，由窗口外壳
  所有，通过 `categorySelectionRequested` 更新，同窗模式切换保留类别。
  状态仅绑定 AppWindow 标题栏，错误全文通过标题栏查看。
  输入校验及陈旧结果说明保留对应侧栏/视图上下文。

## 调用、异步与关闭边界

NumberField/VectorFields 继续在 editingFinished 调用 setField；Checkbox、
光谱类型和 CSV 导入沿用 setFlag、setSpectrumKind、loadSpectrum。
startRender/cancelRender 和 requestClose/resolveClose/shutdown 保持原调用。
UI 不增加计算入口、不接管队列，不因侧栏折叠、缩放窗口或切换视图取消任务。
结果失败时保留已有有效图像；STALE 提示继续由 previewStale 派生。
异步发布、取消、关闭确认和数据验证仍由现有控制器负责。

光追父控制器只管理资产和模式，向 appearance 子控制器提供冻结来源。
同窗模式切换不重建 draft；运行时禁止替换来源。复用组件通过
`selectAssetRequested` 请求父窗打开既有资产对话框，并以 `externalBusy`
屏蔽与父窗几何任务冲突的启动动作。普通空请求不显示错误；真正的资产
失败保留上次有效来源。详细交互边界见 [几何光追契约](geometric-raytrace.md)。
`showModeSelector` / `geometricModeRequested` 只提供高级模式导航，
`activeWorkspace` 使同窗任一时刻只有当前工作区的 F4 生效。
资产对话框实际关闭并返回窗口后，焦点恢复到当前可见的资产入口；成功
选择资产时待异步载入完成再恢复，不聚焦隐藏的几何栏按钮。

渲染入口默认绑定本地 scalar Mitsuba CPU 子进程。计算页可显式选择 NVIDIA CUDA 与 GPU 精度 float32 / mixed；只有独立子进程完成真实 CUDA 设备执行探测时才可提交，探测失败显示原因，绝不静默回退 CPU。mixed 的光学运算为 float64，几何和图像仍为 float32，结果一律标注未获图像精度资格认证。首帧 JIT 编译阶段不伪造 ETA，逐波长完成后只显示按实测速率估算的剩余时间。取消先要求波长边界协作；若 CUDA 子进程在当前帧或 JIT 中超过 10 秒，仅停止该任务进程，部分文件不会成为已完成结果。
冻结平面圆形基片保留原始半径和真实捕获厚度场，采用明确记录的捕获节点边缘预览约定。中央几何视图用 64 段弦近似显示圆盘轮廓，仅用于取景；物理渲染仍使用解析圆盘和圆柱。曲面、任意轮廓和 RGB 光谱替代均不接受。

## 验证边界

使用真实 QQmlEngine、Qt offscreen/software 渲染与实际点击、F4、编辑和
相机动作验证布局、反复折叠、滚动位置与结果保持。保存宽/窄窗口和
浅/深主题截图并检查实际像素；本轮 100% 与 150% DPI 分别在独立进程设置
QT_SCALE_FACTOR=1 / 1.5。覆盖普通空窗、实际测试资产、失败后保留来源、
反复模式切换、真实文件对话框 Escape 和取消专用 QProcess 的关闭边界。
软件 Quick 后端不绘制自定义 QSG 几何节点；这些截图证明控件与布局，
不证明实体几何像素或物理渲染。真实多显示器/触摸板/硬件光追未验收。
