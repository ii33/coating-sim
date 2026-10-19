# Qt Quick 光谱仿真窗口

`FilmSimulationQuick.qml` 提供可编辑的薄膜光谱仿真窗口。计算继续调用 `FilmDesignService`；膜系沿用 `coat_sim.core.film_design` 的模型和 schema v1 JSON。控制器接受现有模型 JSON，也可调用同一 service 的加载入口读取文件。

## 功能核对

| 功能 | 当前状态 | 行为和边界 |
| --- | --- | --- |
| 光谱计算 | 已实现 | 保留 R/T/A 的完整百分比结果；计算仍由现有科学计算 service 完成。 |
| 波段与采样 | 已实现 | 默认 400–700 nm、301 点、1 nm；波长范围、步长、点数联动，不设置固定采样点数上限；拒绝无法表示的重复波长网格。 |
| 入射参数 | 已实现 | 角度范围为 `[0, 90)` 度；支持 s、p、非偏振，以及前/后表面。 |
| 光源 | 已实现 | 平坦光源、5778 K 默认黑体和随包 CIE 表格光源；可加载的表格光源由现有 service 校验。 |
| n/k 处理 | 已实现 | n 外推策略可选端点保持或报错；k 开关使用膜系 `use_k`。缺失或越出 k 数据域的区间按核心模型规则置零，域内才插值。 |
| 膜系输入 | 已实现 | JSON 文件和现有模型 JSON 输入都走已有校验器。由设计页打开时绑定准确的 `FilmDesignEditSession`；设计修订变化同步最新未保存 snapshot 并使旧计算失效。独立打开或查看队列候选时仍使用隔离输入。当前兼容模型 schema v1；不支持旧代码未支持的无版本 JSON 变体。 |
| 结果说明 | 已实现 | 可折叠查看基底处理、n/k 来源范围、插值/零值区间和计算警告。 |
| 图线和坐标 | 已实现 | 可选 R/T/A 与入射、反射、透射、吸收光谱；屏幕曲线使用 Qt Quick Shapes 场景图路径，支持网格、自动/手动 X/Y 范围和主刻度。Y 轴界面按百分数输入，内部及计算结果按比例值保存。PNG 导出仍由 CPU Agg 后端生成全分辨率位图。 |
| 设置取消 | 已实现 | 计算与绘图草稿仅在“应用参数”且整体校验通过时提交。取消或关闭对话框不会更改设置。成功显式应用绘图设置后，临时缩放复位到新坐标设置。 |
| 图表操作 | 已实现 | 鼠标滚轮围绕指针缩放，拖动平移，悬停显示最近采样点的波长和可见曲线值，“复位”回到自动或手动坐标范围。操作只更新矢量路径的视口数据，不生成或异步替换整幅预览图像；此状态只影响显示，不更改仿真参数。 |
| CSV 导出 | 已实现 | 通过现有 service 导出全采样点和元数据；显示降采样不影响 CSV。 |
| PNG 导出 | 已实现 | 可设 4–18 × 3–12 英寸、72–300 DPI 和透明背景。默认 9 × 5 英寸、160 DPI、不透明；使用当前曲线、坐标设置和视口，绘制全量结果数据。 |
| 异步结果保护 | 已实现 | 参数或膜系修改会使当前代次失效并设置取消事件，旧计算完成后不能覆盖新状态；重新计算期间禁用重复启动。 |
| 配对会话与快捷键 | 已实现 | 设计页和仿真页按准确来源 controller/session 对象配对并复用窗口；普通设计编辑只同步输入，不自动计算；设计页不再提供发送光谱按钮。每次手动计算启动前和 F5 都读取同工程准确 session 的最新内存 snapshot 与仿真参数；F6 开始或复用同组优化，F4 折叠/展开右侧统计。优化写回后自动刷新一次，若旧计算仍运行则合并为一个 pending recompute。唯一 active calculation token 控制 busy 所有权，旧回调不得清除其他计算的状态。配对仿真显式加载另一设计后脱离来源 session，避免后续 F5 恢复旧设计或向旧设计回写。 |
| 关闭窗口 | 已实现并有边界 | 关闭时使代次失效、设置当前计算的取消事件并取消尚未开始的任务。核心在波长块之间合作式检查事件；收到取消时抛出 `InterruptedError` 且不返回部分结果。窗口关闭后迟到回调会被丢弃，不更新 UI。 |
| 显示降采样 | 已实现 | 场景图曲线按桶保留采样端点与局部高低点，最多 1800 个样本索引；统计、CSV 与 PNG 导出始终使用完整计算结果。 |

## 统一工具栏与参数草稿契约（2026-10-14）

光谱仿真工具栏只保留一个“仿真参数”和一个“导出”入口。参数弹窗内按“计算参数”和“绘图参数”分组，导出菜单提供“导出 CSV”和“导出 PNG”。保存膜系、计算/F5、优化、统计与诊断侧栏保持原有入口和行为。此项是界面及提交边界调整，不改变薄膜计算、采样算法、文件格式或导出结果精度。

- 参数弹窗输入是控制器已提交的计算设置与绘图设置的独立草稿；打开时复制当前值。切换分组、改变字段、取消、Escape 或关闭都不提交配置。重新打开读取当前已提交状态，丢弃上次未应用的字段和错误；关闭后焦点返回“仿真参数”入口。
- “应用参数”同时校验两组草稿。任一组无效时保持弹窗打开并显示具体错误，两组已提交状态都保持原值，不产生部分配置保存、计算取消或重算。成功后两组由同一控制器提交，继续通过现有工作流配置接口保存到准确来源 session；独立文档仍在显式保存时持久化至 `.cfilm`。控制器是已提交配置、计算代次和结果的唯一状态所有者，QML 只持有临时草稿。
- 只有计算输入实际改变才沿用原有过期结果、取消事件和代次失效行为。只改图线、坐标、刻度或网格时保留有效计算结果；应用绘图设置仍复位临时缩放。新增 s/p 叠加需要尚未计算的偏振分量时，沿用原有 generation 保护的补算，不并行重复提交。
- 统一弹窗沿用 `ThemeDialogBase`；内容可纵向滚动，取消与应用操作保持可触达。宽高限于宿主可用空间，不重复应用 DPR、字体或密度倍率。绘图颜色及主题仍由既有图表配置和 Theme 通知负责。
- 可复用 `FilmPlotSettingsForm` 的输入为 `controller` 和必需的 `themeTokens`；宿主显式传入当前主题快照，不由表单读取隐式全局 Theme。独立兼容包装使用自身 `ThemeDialogBase.themeTokens`，主窗口使用 `window.themeColors`，两者继续随主题通知更新。
- “导出”按钮及菜单动作只在已有有效、非过期、非 busy 结果时可用。CSV 菜单动作继续打开原有保存路径对话框；PNG 菜单动作先打开原有尺寸/DPI/透明背景设置，再选择保存路径。关闭菜单、取消 PNG 设置或取消保存路径不写文件。连续重新打开菜单不得重复触发导出；每次只执行用户选中的动作。
- Qt Widgets 兼容窗口使用同一组工具栏入口及草稿提交约定，原独立设置类保留供内部接口和既有调用复用，不额外呈现重复按钮。

回归入口覆盖统一入口、菜单动作、取消与重复打开、两组参数的无效草稿保护和配置保存；原光谱测试继续验证 F5、侧栏、主题、全采样 CSV 与 PNG 导出。

## 大采样仿真的接口约定（2026-10-11）

- 输入仍由 `coat_sim/core/film_design_service.py::simulation_settings` 校验：波长使用真空 nm，范围为有限正数；单波长计算至少 1 点，非零波段至少 2 点；步长为有限正数，点数为正整数。采样点数不设置固定业务上限。计算出的步长若低于该波长范围的浮点可分辨精度，波长数组含重复点，或数组长度无法由运行时表示，应给出具体错误；不得夹限、静默截点或改用较粗步长。默认仍是 400–700 nm、301 点、1 nm。Qt Quick 的十进制文本字段受 JavaScript 安全整数范围限制，超过该界面精度的值会明确报错；Python 桌面控件保留大于有符号 32 位的整数输入。
- 输入/输出由代码接口为真：`simulate(design, settings, *, cancel_event=None)` 接收完整膜系和仿真设置，成功返回 schema v1 的全量 `coat_sim.film_spectrum`；`simulate_frozen(root, payload, *, cancel_event=None)` 对冻结快照复用同一计算。取消事件可在启动前或每个波长块之间检查；取消抛出 `InterruptedError`，不返回部分谱。Quick 控制器持有唯一 active generation、取消事件和已提交结果；QML 只持有未提交的参数草稿。服务/控制器的参数错误和物理域错误显示为失败；被取消或 generation 过期的结果不得提交。
- 数值核心按连续波长分块，最多 4096 点/块；块样本数再按 `len(front_layers) + len(back_layers) + 3` 保守估算的介质数自适应缩小，使用“介质数 × 每点每介质 256 字节”的工作量估计，并以 32 MiB 为项目工作块目标（均为本项目约定，不是完整进程峰值承诺）。单面计算也保守计入设计背面层数。块边界仍参与相邻采样判定；结果对象保留全部波长、R/T/A、光源与加权曲线。资源提示估算已提交参数的完整输出列表及当前块的工作数组，随点数、介质数和附加偏振数组增长，仅提示内存开销，不构成另一项固定点数/计算配额。完整波长网格与结果仍需 O(N) 内存，极大请求可能因实际内存不足失败；取消需等待当前 NumPy 运算结束。显示曲线可继续按视口采样，但统计、CSV 与 PNG 均消费完整仿真结果。
- 控制器内部提交已同步的整数点数，省略派生显示步长。`setSimulationSettings` 接收的步长若与该点数在当前波段上的派生值完全相同，则保留整数；独立编辑的步长仍按原服务规则优先换算。QW 参考查询只记录一次，后续块不得把参考查询混入光谱计数；源谱与带积分在全局网格上计算，不按块分别归一化。
- 该边界只适用于光谱仿真；材料/离散光源数据有效性、波长物理域、薄膜层数、光学插值域和优化预算仍沿用各自现行契约。光源输入表格的长度限制是独立数据输入限制，不随求解点数放宽。

## 验收状态

- 2026-10-14，补充统一入口的 Windows QA 工具兼容和静态类型修正后，包含 `test_film_plot_layout_tool` 的上述联合目标为 95 passed、2412 deselected、3 subtests passed；最后导入与类型窄化整理后的工具 Qt 专属复测为 1 passed。光谱修复范围的 mypy/Ruff 对现有诊断基线比较新增为 0，不增加基线债务。本环境没有执行工具的 Windows 主入口。
- 2026-10-14，统一工具栏候选在 Python 3.12.14 / PySide6 6.12.0 下通过定向官方 runner：94 passed、2412 deselected、3 subtests passed。范围为 `test_film_simulation_toolbar`、`test_film_simulation_quick`（包含 UI refinements）、`test_film_simulation_settings`、`test_film_plot_settings`、`test_film_design_gui`、`test_film_workflow_document`、`test_film_floating_ui` 和 `test_film_spectrum_plot_bounds`。新增用例覆盖实际菜单点击、CSV/PNG 保存路径取消、PNG 设置取消和重复打开、双组预校验失败保持计算代次与取消事件、修改后 `.cfilm` 保存重载、两页草稿取消及小窗固定操作。已有用例继续覆盖全量导出、侧栏、主题和视口。Qt Quick 的 420 × 440 逻辑 px 两页已离屏渲染检查，Qt Widgets 的 320 × 300 滚动和字段焦点 Escape 路径通过。该结果是离屏定向回归，不代表桌面、GPU 或全量套件验收。

  复现命令（使用已安装项目测试依赖的 Python）：

  ```bash
  QT_QPA_PLATFORM=offscreen PYTHONPATH=.:tests python tools/run_tests.py all -- -k 'test_film_simulation_toolbar or test_film_simulation_quick or test_film_simulation_settings or test_film_plot_settings or test_film_design_gui or test_film_workflow_document or test_film_floating_ui or test_film_spectrum_plot_bounds'
  ```

- 专属测试覆盖范围/步长联动、v1 JSON 文件加载与无效 JSON 拒绝、膜系模型输入、显示降采样、k 零值区间说明、百分比坐标换算、视图缩放/平移/复位、游标采样和 PNG 参数校验；还包含离屏 Fusion QML 加载、301 点 R/T/A 与核心结果对照、CSV/PNG 导出、过期结果丢弃和关闭期间工作线程收尾用例。
- 2026-10-11 点数修改的现行验收、复现命令和真实限制见 [点数限制取消验收记录](film-simulation-points-acceptance-20261011.md)，包含独立全网格数值/来源对照、实际 API/MCP 与 UI 大点数路径、取消和完整导出。
- 早期历史记录：使用项目 `.conda` Python 3.12.14 和隔离 pytest 目录运行后，专属测试为 13 passed。离屏 Fusion QML 加载成功，ThemeDialogBase 修复后未捕获到 Dialog 无障碍警告；301 点 R/T/A 与核心 service 对照一致，CSV 行数和透明 PNG 像素/alpha 校验通过，过期结果丢弃及关闭期间线程收尾用例通过。该数字不代表本次新增测试数量。
- PNG 渲染通过项目共享 `FONT_ROOT` 定位并注册包内 Noto Sans CJK SC（SIL OFL 1.1）；系统缺少该包内字体时，再尝试已安装的 Microsoft YaHei/SimHei 字体。缺字警告断言、Fusion QML 加载和图表导出复跑均通过。真实鼠标手势的 GUI 视觉验收与便携包验收仍待完成。

## 图表 PNG 预览

下图由 301 点合成玻璃样本生成，显示全量 R/T/A 曲线；中文坐标轴与图例无缺字。

光谱仿真 PNG 图表预览（历史引用目标，当前公开源码不包含此文件）

## 文件和接入

- 控制器：`coat_sim/ui/film_simulation_quick_controller.py`
- 主窗口：`coat_sim/ui/qml/FilmSimulationQuick.qml`
- 光谱参数：`coat_sim/ui/qml/FilmSimulationParameters.qml`
- 统一参数弹窗：`FilmSimulationQuick.qml` 内的计算/绘图页签；共享绘图草稿：`coat_sim/ui/qml/FilmPlotSettingsForm.qml`。原 `FilmPlotSettings.qml` 保留为独立兼容包装。
- 图表导出设置：`coat_sim/ui/qml/FilmPlotExportSettings.qml`
- 图表：`coat_sim/ui/qml/FilmSpectrumPlot.qml`
- 回归用例：`tests/test_film_simulation_quick.py`、`tests/test_film_simulation_toolbar.py`、`tests/test_film_simulation_quick_ui_refinements.py` 与 `tests/test_film_design_gui.py`
- Windows 视觉验证工具 `tools/verify_film_plot_layout.py` 通过统一“仿真参数”入口进入绘图页签；其 `verify_plot_settings_workflow` 使用当前窗口所属的真实模态对话框及唯一按钮组，覆盖应用、取消、重开和中文范围校验。`tests/test_film_plot_layout_tool.py` 以离屏实际计算和合成材料验证相同工具流程；工具的 Windows 主入口限制保持不变，离屏回归不替代 Windows 实机验收。

调用 `create_film_simulation_quick_window(root=None, service=None, design=None)` 可创建独立 Quick 窗口；调用方需在窗口生命周期内持有返回的 engine、window 和 controller。共享应用入口由 `coat_sim/ui/launcher.py` 创建及复用配对 Quick 窗口。QML 主题依赖 `Theme.snapshot`、`Theme.revision`、`Theme.mode` 以及共享的 `qml/theme_controls/` 控件。


## Qt Quick spectral band statistics and polarization overlays

- The main result area places a multi-band calculation panel beside the spectrum plot. Its initial band follows the default 400–700 nm solver range; users can add or remove bands. A result reports mean, minimum, and maximum R/T in percent for each band.
- Band endpoints use exact samples when available, otherwise linear interpolation between adjacent solved samples. Mean values use trapezoidal wavelength integration divided by the band width, including nonuniform grids. The requested band must stay inside the solved wavelength domain; no extrapolation is performed. Missing results, invalid ranges, out-of-domain ranges, malformed grids, and non-finite data each return a visible status.
- Calculation values stay as fractions in the controller and are converted to percentages only for display. The calculation does not resample or mutate the service result and does not change CSV/PNG export data.
- Plot settings can overlay s and p curves for the selected R/T/A channels. They are labeled separately from the current (s, p, or unpolarized) result; if a requested channel is absent, the controller starts a new generation-guarded service calculation. At normal incidence equal values remain separately identified by polarization.
- The absorption-coefficient policy comes only from the loaded design's `use_k` field. The simulation-parameter dialog no longer exposes a duplicate k toggle. Existing core behavior for missing/out-of-range k remains unchanged.
- The window no longer sets local width, height, minimum size, or maximized defaults; it uses the shared `AppWindow` geometry contract. The collapsed “数值诊断” section contains compact n/k coverage and calculation warnings, not the full material definition.
- Implementation: `coat_sim/ui/film_simulation_quick_controller.py`, `coat_sim/ui/qml/FilmSimulationQuick.qml`, `FilmSimulationCalculationItems.qml`, `FilmSpectrumPlot.qml`, `FilmSimulationParameters.qml`, and `FilmPlotSettings.qml`. Focused tests for the nonuniform integral, no-extrapolation/NaN statuses, and polarization result preservation are in `tests/test_film_simulation_quick.py`.

- Targeted Qt regression: 10 passed in 15.348 s, exit code 0. This covered the shared geometry/show-and-click path, design `use_k` retention, R/T/A plus s/p dialog selection and curves, band add/update/remove with nonuniform-grid statistics, and unchanged CSV bytes with full-resolution PNG export. Persistent log: `tests/artifacts/film-simulation-quick-targeted-qt-20261008-attempt3.log`.

## 光谱极值与波长通带分析（2026-10-10）

- 统计面板选择 R 或 T、已实际计算的偏振分量，以及 0–100% 阈值；默认阈值 50%。结果同时显示所选曲线的偏振、入射角与计算波长域。
- 全局最大/最小包含计算域端点；局部峰谷只认两侧都有采样邻点的极值。连续完全相等的采样值组成平台，只有平台值严格高于/低于两侧才记作局部峰/谷，并报告平台波长范围。全局并列极值逐个列出。非递增、长度不符或非有限采样返回错误状态，不跳过 NaN。
- 通带定义为已计算样点之间曲线值大于或等于所设阈值的波长区间。跨阈值边界只在相邻已计算样点间线性插值，不外推。区间触及计算域边界时报告“截断 / 仅测得部分宽度”，不得把观测宽度显示为完整带宽。多个分离区间分别报告各自宽度，不合并求和。
- 长波方向明确表示波长增大（频率降低）；短波方向表示波长减小（频率升高）。此为波长轴标签约定，避免把频率方向的高/低通含义直接套到反向的波长轴上。
- `FilmSimulationQuickController` 是极值、阈值、偏振选择与计算结果的唯一状态所有者。QML 只展示当前提交结果；仿真仍由后台线程运行，过期代次不能提交。窗口关闭会丢弃回调，但不能中断已进入核心 `simulate` 的 Python 计算。图表预览的曲线由 `FilmSpectrumPlot.qml` 的 `QtQuick.Shapes` 路径从已提交曲线数据构建；更新曲线时整组序列快照一次替换，视口交互保留当前曲线帧。
- 本节极值/通带规则为本项目显示约定，不改变薄膜计算结果、CSV/PNG 输入值或计算精度；未找到用于这些 UI 阈值和截断判定的外部依据，仅为本项目实现。Qt Shapes 的路径与场景图渲染能力见 Qt 官方文档：[`ShapePath`](https://doc.qt.io/qt-6/qml-qtquick-shapes-shapepath.html)、[`Shape`](https://doc.qt.io/qt-6/qml-qtquick-shapes-shape.html)；实际合成后端依赖启动配置和运行环境。
- Implementation: `coat_sim/core/film_spectrum_analysis.py`, `coat_sim/ui/film_simulation_quick_controller.py`, `coat_sim/ui/qml/FilmSimulationCalculationItems.qml`, and `coat_sim/ui/qml/FilmSpectrumPlot.qml`; focused analysis and Qt scenegraph checks are in `tests/test_film_spectrum_analysis.py` and `tests/test_film_spectrum_image_provider_isolated.py`.

关闭仿真窗口时 controller 会使当前计算 token 失效、取消仍可取消的任务，并立即清除结果光谱、统计分析快照和绘图派生缓存；已进入 Python `simulate` 的工作不会被强制中断，迟到回调也不会恢复已退休的结果。

厚度优化可从配对仿真页打开；它通过同一个来源设计 controller/session 创建或复用优化窗口。正常完成且 session、来源生命周期、请求 revision、operation token 和编辑 epoch 均仍匹配时，候选经会话原子写回，再刷新这张仿真图。取消、失败、窗口关闭、加载替换文档、旧结果或优化期间任何尚未提交的用户编辑都会拒绝自动写回，并在优化页保留可见状态。未达采样目标的状态仍明确显示。独立仿真或队列候选没有可写设计会话，继续作为隔离预览。


## Plot and optimizer-result handoff

On-screen spectrum curves are represented as Qt Quick Shapes paths in the scene graph from an atomic snapshot of the committed calculation and plot settings. Mouse zoom and pan update the vector viewport directly, so they do not clear the existing curves while waiting for an asynchronous image decode. PNG export remains a CPU-generated, full-resolution Agg raster export; it does not enlarge the on-screen path or affect calculation precision. The actual Qt Quick scenegraph backend is selected by application startup and the platform: code inspection confirms the app only explicitly selects the software backend in `--software` mode, while QSG diagnostics can be enabled with `--graphics-info`; a loaded graphics DLL alone is not proof of GPU rendering. Current runtime GPU backend remains unverified.

An optimization launched from a paired design session writes a normally completed, current candidate through the session's atomic revision check and asks its paired simulation to recalculate. It reuses that simulation even if calculation is already in progress: the active result is discarded as stale and at most one follow-up calculation starts with the latest design. Cancelled, failed, stale, closed, or conflicting work does not write to the source. An explicitly selected queue candidate remains a separate frozen simulation preview; it never replaces a paired simulation or writes to its source design.

The shared queue's explicit film-result action may open a replay-validated best
candidate from a completed, cancelled, or failed job in another independent
simulation window. That handoff is labeled as a queue best candidate, preserves
the actual target-attainment flag, and does not apply to or overwrite the source
design. A job without a verified candidate has no result-open action.

## 统计栏折叠与独立快照优化（2026-10-11）

`FilmSimulationQuick.qml` 是 `statisticsExpanded` 的唯一状态所有者。统计子组件通过 `toggleRequested` 请求切换，按钮与 F4 共用一个处理函数；折叠前将焦点移出可能被隐藏的阈值字段，折叠宽度为 44 个逻辑像素并随 Theme 控件尺寸保持可触达。展开、折叠和恢复仍由同一个绑定驱动。

用户显式加载文件或 JSON 后，仿真窗口脱离原设计 session。F6 以该仿真实例的 owner token 复用只读优化器，并开始优化当前内存快照；再次 F6 更新尚未构造的待加载快照，或刷新已有的空闲优化器，busy 时不并行提交。此路径不会写回原设计窗口。

## 2026-10-13 文档与绘图区补充

配对窗口的参数、计算波段、绘图、分析和PNG导出设置即时提交到唯一来源session；独立窗口可保存包含全部现有工作流配置的 `.cfilm`，独立未保存关闭有放弃确认。公开复用入口 `reuseWindowOpen` 对同session更新不挡dirty，对不同文档先确认；取消/验证失败保留来源、参数及图表。详见 [工作流文档契约](film-workflow-document-contract.md)。

绘图区按钮、图例和结果说明均为不占绘图行的浮层，缩放用放大镜图形。自动X范围使用全部可见曲线有限样本的波长并集，不加两端空白；无有效曲线回到输入范围，单点保留最小非零显示跨度，内部缺口分段绘制。手动坐标和用户临时zoom优先，重算与同来源更新保留临时zoom，显式图表设置应用或复位清除zoom。屏幕降采样仍只影响显示；配置、计算和导出不截断厚度/结果精度。仿真侧栏以与设计一致的64逻辑像素功能条承载统计/诊断与24逻辑像素折叠按钮，普通内容宽384–520；窄窗口内容可缩至280，以保留绘图区域。各内容实例常驻，切换不重置统计编辑或滚动。

## 同工程手动计算与来源关闭（2026-10-14）

- 光谱仿真每次手动计算/F5 都从准确绑定的编辑会话读取当前未保存膜系和 revision，不寻找或加载已有 `.cfilm` 文件，不以文档 ID、名称或材料 ID 相同推断来源，也不通过手算打开设计窗口。项目来源的 service/editor 必须与当前 workspace 的同一对象一致，其他工程来源在绑定入口拒绝。
- 项目工作编辑会话的寿命属于 workspace。设计窗口仅是它的 presentation；关闭该窗口会断开 presentation 信号，但保留此准确 owner 的内存绑定。手动计算仍读取 owner 最新草稿，重开窗口不是计算前提。无显式输入的工程仿真构造器默认绑定当前 owner；传入独立设计快照的构造器、独立窗口、显式加载文件/JSON及队列候选继续使用隔离输入；显式加载会解除绑定，哪怕加载的设计 ID 恰好与项目草稿相同，也不会把参数回写该 owner。
- `FilmSimulationQuickController` 保存来源同步错误。来源 owner 的 snapshot.error 非空、表达式正在处理、无效来源设计或来源工作流配置会取消旧代次、清除旧结果并阻止启动，不能由当前窗口旧快照的普通校验把同步错误消除后继续计算。来源 error 没有错误类型，故本轮也保守阻止仍有保存错误的来源，明确提示先修正或清除当前设计错误，不自行清错。修正来源或完成表达式工作后，下次刷新/手算重新验证并清除仿真侧同步错误。异步结果仍按 active token、generation 和 project operation 身份提交。
- 控制器是已提交配置、来源绑定、计算代次和结果的唯一所有者；QML 只持有参数弹窗草稿。新回归在 `tests/test_film_manual_draft_sync.py`：每次手算读取最新未保存值、关闭设计 presentation 后 retained draft、失败不回退旧设计、真实 session 拒绝非法编辑/表达式处理中不回退保留的合法快照、同 ID 显式隔离预览、显式构造器输入不夺取 owner 和其他工程 owner 拒绝。执行器使用 held future，不跑高负载计算。
- 小窗弹窗测试以 Qt Quick `ensurePolished()` 和真实 `grabWindow()` 的首个渲染帧为几何观察边界，随后核对原 cancel/apply 边界断言；不通过固定延时、扩大容差或等待直到越界消失来放行。Qt 的 `processEvents()` 单独不能证明布局 polish 已完成。


## 2026-10-19 次网格与绘图范围草稿

- `FilmSimulationQuickController.plotViewport` 是主/次刻度位置的唯一数值所有者；QML 只绘制接收到的列表。次网格不带数值标签，网格开关同时控制主/次线，主标签独立保留。生成规则、坐标单位和限额见 [算法文档14i-11b](algorithms.html#film-spectrum-minor-grid-20261019)。
- 统一参数弹窗中的 `FilmPlotSettingsForm` 持有未应用草稿。最小/最大范围在自动模式下也可编辑；有效值实际改变时，该轴草稿切换为手动。失焦、完成编辑和应用前都提交原始编辑文本到字段自己的数值源，避免格式化显示回退覆盖输入。错误仍由既有参数验证阻止应用，不能部分提交计算或绘图配置。取消/关闭后重新打开读取已提交值。
- 在有效绘图区双击请求同一个参数弹窗的绘图页签；曲线控制器不直接持有窗口状态，`FilmSimulationQuick.qml` 消费信号并在打开后清除一次性页签标志。其他入口仍从默认参数页签打开。窗口尺寸、应用/取消、焦点恢复、异步计算代次与取消边界沿用已有契约。
- 绘图显示和字段草稿变化不修改已计算的 R/T/A 样本。只有应用到 controller/session 的计算输入改变时，才沿用原有结果失效与取消行为。
