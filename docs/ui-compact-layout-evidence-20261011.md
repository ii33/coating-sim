# 紧凑标题栏与全窗口布局验收

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

日期：2026-10-12（2026-10-11 实施，执行器恢复后补充验收）。本批由 复核者 主持，分工实现与复核按不重叠文件执行或复核。没有提交、发布、停止用户应用或修改 ACL；保留原有未提交改动。本批只改 UI 布局和新控制器的显示默认值，不改物理计算、采样、文件格式或数值结果。

接口与所有权见 [共享标题栏状态与信息侧栏契约](window-status-sidebar-contract.md)。下述视觉检查使用隔离 Qt 实际渲染的截图。

## 共享标题栏与侧栏

`AppWindow.qml` 提供 `statusText/statusSeverity/statusDetail`、`titleBarHeight/contentTopMargin`，业务窗口继续拥有状态、文件名、dirty 和关闭确认。`WindowChrome.qml` 将状态控件限制在窗口按钮之前，提供文字严重性、完整 Accessible 名称、悬停/聚焦全文及长提示滚动。`ThemeSidebar.qml` 提供带标题、换行和滚动的容器，调用页面拥有业务状态。

最终隔离测试 `tests/test_compact_window_chrome_ui.py` 在 DPR 1 和 1.5、明暗主题下通过。真实状态点击、Tab 焦点、6,536 字符提示的末行可达、更新后滚动归零、720 宽度提示避开关闭按钮、480 宽度压力检查及 40 行侧栏滚动均有运行指标。源码 hash 与截图在 共享 QA 目录（历史验证记录，未随公开源码发布），运行清单为 DPR 1（历史验证记录，未随公开源码发布） 和 DPR 1.5（历史验证记录，未随公开源码发布）。复核者 已目视 长提示悬停（历史验证记录，未随公开源码发布） 与 滚动到末行（历史验证记录，未随公开源码发布）。

验收中曾发现极长提示文字被弹层截短，并遮住首次状态点击；现已改为标题栏下方、窗口内的可滚动全文。两个原有 chrome 检查仍通过，其中一个为运行检查、一个为原生移动/缩放调用的静态检查。共享控件静态检查 7 项通过，清单见 static-checks.json（历史验证记录，未随公开源码发布）。未删除或放宽已有生命周期断言。

## 膜厚仿真

`MainWindow.qml` 用侧栏展示完整均匀性与基片面形双语摘要，保留原始文字、单位和计算/取消/进度控件。状态移到标题栏。操作栏实测 `y=36,height=implicitHeight=40`，正文从 `y=76` 连续开始。

`tests/test_main_compact_sidebar_ui.py` 在 DPR 1 和 1.5、明暗主题下通过：初始空态、结果文本换行、算法切换、1080×680、长状态、busy/cancel 绑定及历史/PDF/保存对话框开关。清单：DPR 1（历史验证记录，未随公开源码发布）、DPR 1.5（历史验证记录，未随公开源码发布）。复核者 已目视 1080 宽浅色（历史验证记录，未随公开源码发布） 和 1.5 倍深色长状态（历史验证记录，未随公开源码发布）。

本页最窄验证为 1080，未验证更小宽度。保存对话框只验证开关，未接受或写盘。没有注入真实失败；当前 MainBridge 未提供独立错误严重性字段，因此绑定原有 status 和 busy，不按文字猜测错误。没有可用文件名/dirty 标题源，保留既有静态标题。offscreen 未能认证组合框原生 F4 弹出行为。

## 工艺

`ProcessWindow.qml` 将修正板优化移到单实例侧栏；宽度不足 1120 时通过顶部入口打开抽屉。方案文件、机器结构预览和温度与真空各自折叠，隐藏时保留内容。操作栏使用 Flow 的 `implicitHeight`；原先 `childrenRect.height` 将隐藏或临时对象计入高度，实测将 40 px 操作栏撑到 472 px，现已修正并加几何断言。标题栏保留文件名和 dirty；首条错误作摘要，全文含全部错误及原状态。

`tests/test_process_compact_sidebar_ui.py` 最终 DPR 1、1.5 两次通过，目录：DPR 1（历史验证记录，未随公开源码发布）、DPR 1.5（历史验证记录，未随公开源码发布），详细清单见 evidence-manifest.md（历史验证记录，未随公开源码发布）。实际覆盖独立折叠、720 宽度、明暗主题、Space/F4、长状态和多条错误全文、侧栏实例/参数/两个滚动位置保留。取消按钮调用真实桥接取消接口；忙碌关闭等待受控 worker finished 后完成。它验证 UI 生命周期，没有启动物理优化求解。

保存用真实 CoatService 写入隔离方案：当前路径覆盖、发送前保存与取消、Save As 覆盖拒绝后的 dirty/原文件保留、接受后落盘/dirty 清除均通过。当前页面没有 dirty 关闭确认，本批未新增。

原有 `test_compact_process_and_machine_layout_is_responsive_and_scrollable` 已同步优化项的真实滚动所有者，保留所有步骤、视口和 Machine 断言，并增加独立侧栏、主滚动位置不变及空机器依赖的新建路由断言；Machine 本批迁移后再跑完整单项，在 DPR 1 通过（52.313 s、两个实际网络阻断引擎），见 result.json（历史验证记录，未随公开源码发布）。复核者 已目视先前同布局运行的 正常宽窗口（历史验证记录，未随公开源码发布） 与 窄窗口（历史验证记录，未随公开源码发布），以及 720 优化抽屉（历史验证记录，未随公开源码发布）。

导航静态套件 7 项最终全部通过，见 result.json（历史验证记录，未随公开源码发布）。旧源码字符串断言已同步为当前依赖路径接口、合并的空路径/已有路径调用、换行全文和标题栏状态，同时保留路由、选中点提示及全局状态语义；MachineWindow、MaskWindow 仅作只读核对。原 Main 新建/编辑/清空路径 Qt 用例通过，见 result.json（历史验证记录，未随公开源码发布）。

## 膜系仿真

`FilmSimulationQuick.qml` 将放大、缩小、复位和操作说明移入图表悬浮层，数值诊断从顶部入口打开侧栏。新控制器只默认显示 R；显式 T/A 和偏振配置保留在控制器内存中，计算与 CSV 仍包含完整 R/T/A。未增加文件格式或持久化配置。

最终 UI 在 DPR 1 与 1.5 各通过一个完整用例，见 DPR 1 result（历史验证记录，未随公开源码发布） 和 DPR 1.5 result（历史验证记录，未随公开源码发布）：均 exit 0、两个实际网络阻断引擎、blocked URLs 为空、运行前后源码及测试 SHA 一致。覆盖缩放、拖动、滚轮、采样悬停、复位、诊断入口、明暗、720 窄窗口、F4、参数/绘图对话框和关闭后重开。

六项 R/T/A × s/p 图例在宽窗和窄窗均完整可见，悬浮控件不盖图例；轴标题保留完整波长单位和“反射 / 透射 / 吸收（%）或相对光谱（%）”语义。长纵轴文字以两行排版，检查 glyph 的 implicitWidth 与旋转后四角边界。复核者 已目视 1.5 倍六项窄图（历史验证记录，未随公开源码发布） 和 1 倍 T/A 宽图（历史验证记录，未随公开源码发布）；只读独立复核见 film_clip_review.md（历史验证记录，未随公开源码发布）。

软件渲染曾出现 T 曲线越过绘图区的真实问题，最终保持 Shape/vector 与 GeometryRenderer，仅对软件后端的曲线层启用独立 layer 和裁切。像素检查读取 live T 曲线的实际颜色，只排除图例色条的真实小矩形，要求绘图区内至少有 20 个 T 色像素且区外为零；两档均通过。DPR 1.5 的 QImage 元数据仍报 1.0，因此比例检查使用图像宽度/逻辑窗口宽度，实测比例为 1.5。GPU 后端未单独验证。

原参数/绘图对话框键盘、取消及校验完整用例已在最终 QML 上独立通过（1 项、5.704 s、实际网络阻断引擎），见 result.json（历史验证记录，未随公开源码发布）。原 Quick 全套此前 24 项通过的运行早于最后纵轴排版；最终源码首轮为 23/24，对话框 numericValueEdited 断言失败，记录仍保留于 失败 manifest（历史验证记录，未随公开源码发布）。同一版本第二轮完整套件 最终 manifest（历史验证记录，未随公开源码发布） 为 24/24、exit 0、四个实际网络阻断引擎且 blocked URLs 为空，运行前后产品和测试 SHA 一致。2026-10-12 只读回查确认这两次及最终双 DPI UI 的源码 hash 与当前文件匹配；首轮失败原因尚未确认，不能据重跑通过称其原因已解决。生命周期断言未删除或放宽。

## 全窗口接入和环境边界

### 蒸发源、基片与资产包

SourceWindow/SubstrateWindow 的状态和 invalid 错误接入标题栏，文件名/dirty 与原保存关闭行为保留。最终整组双档验收均 exit 0，见 DPR 1 manifest（历史验证记录，未随公开源码发布） 和 DPR 1.5 manifest（历史验证记录，未随公开源码发布）：涵盖两页明暗/窄宽/长提示、实际 Save As 覆盖拒绝/接受、Source 保存后关闭和取消关闭、Substrate clean close，以及 AssetBundle 状态绑定/关闭。两页最窄宽度为 Source 640、Substrate 1000 逻辑 px，未验证低于最小宽度或 DPR 2。

2026-10-12 实际截图发现 AssetBundle 标题与长状态重叠，旧 失败布局图（历史验证记录，未随公开源码发布） 保留。实测 Dialog 从零尺寸父项推导出 -48×-48、availableWidth 为零，头部 RowLayout 因负尺寸无法约束文字。现只修 AssetBundle 接入为显式 Popup.Item/Overlay.overlay，Source/Substrate 字节保持原验收版本；共享 Header 未改。

修复后的两档实测 Dialog 为 780×660、header 为 780×38，标题 311×26、状态 429×28 且矩形不相交，状态正文宽 425；新断言还覆盖弹窗/头部四边包含、正宽、文本约束，以及关闭后 tooltip 隐藏、焦点恢复父按钮。复核者 已目视 1.5 倍最终集成图（历史验证记录，未随公开源码发布），标题状态分栏、摘要省略、全文换行可读且避开关闭键；全文阅读浮层覆盖正文符合悬停/聚焦契约。原导出/覆盖回调未修改，本轮没有触发 AssetBundle 导出写盘流程。

### 机器与设置

MachineWindow 保留文件名/dirty，并从已有 canSave/validationMessages 派生错误状态；SettingsWindow 使用已有 statusMessage，缺少类型化错误字段时保持 info，字段旁提示仍在原处。两档独立测试均 exit 0、各一个测试函数，覆盖两页明暗/窄宽/长状态及原保存和关闭处理，见 DPR 1 outer-run（历史验证记录，未随公开源码发布） 和 DPR 1.5 outer-run（历史验证记录，未随公开源码发布）。2026-10-12 回查确认当前产品/测试 SHA 与两批 manifest 匹配，各两个已安装网络工厂的 blocked URLs 为空；本轮未重复测试。

实现者 已补看 1.5 倍两页窄窗截图：完整错误提示和保存按钮可读，字段/预览没有溢出。Machine 的实际 Save As 覆盖拒绝保留旧文件和 dirty，接受后落盘并清除 dirty；Settings 原关闭清理验证包括探测关闭、凭据代次递增及取消事件。Machine 的关闭覆盖限于自绘 chrome 的 clean close，未验证 dirty-close 确认或原生系统关闭，也没有因此新增关闭策略。

### 待决事项、队列、光谱外观与光线追迹

四页的全局状态接入标题栏、顶部内容沿用 contentTopMargin；队列仅按已有 loading 映射 busy/info，卡片错误、外观和光线追迹的字段/预览校验仍保留上下文。2026-10-12 截图复核发现待决事项的原生提交 Button 在 enabled 且实际点击有效时仍为黑底深字。现只在该页改用现有 ThemeButton(primary=true)，保留 objectName、启用条件及提交回调；共享控件未改。

修复后两档补跑均 exit 0、各执行一次 test_utility_windows_compact_chrome_ui，覆盖 4 页 × 2 主题（8 个组合）并各生成 24 张图，见 DPR 1 manifest（历史验证记录，未随公开源码发布） 和 DPR 1.5 manifest（历史验证记录，未随公开源码发布）。每批安装八个网络工厂、manager creations 为零、blocked URLs 为空，服务均为 fake/local，未写真实任务队列或审批。修改前 SHA 基线和两批运行后 SHA 已记录，运行后 SHA 相同且与当前产品匹配；runner 未在每次运行起点独立采集 SHA，不能称每批均有自动 before/after 取样。

复核者 已目视 1 倍浅色按钮（历史验证记录，未随公开源码发布） 和 1.5 倍深色按钮（历史验证记录，未随公开源码发布），文字清晰；实现者 已看 1.5 倍长错误图，完整提示避开关闭和提交按钮。原 黑底按钮图（历史验证记录，未随公开源码发布） 仍保留。四页没有分别执行 F4，不能据共享 chrome 的通过结果声称覆盖各页 F4。

### 材料库、独立聊天窗、启动器与通用弹窗

MaterialLibraryHost 使用同一个 library/editor 实例派生 loading、错误、dirty 和状态；standalone 材料库将这些状态放入 ThemePopupHeader，hosted 模式由外层 AppWindow 展示，内层不重复显示全局状态。AssistantChatWindow 保留独立 WindowChrome，以 assistant.statusMessage 为唯一来源；busy 用已有字段，缺少错误类型字段时为 info，错误文本仍可读且可展开。Launcher 标题栏绑定已有 errorToast.visible/text，原有 15 秒消失策略保留。About 没有业务状态，不合成提示；ThemePageDialog 只同步紧凑顶部布局。

最终两个独立目录的 unittest 矩阵各 5/5、进程退出 0，见 DPR 1 manifest（历史验证记录，未随公开源码发布） 和 DPR 1.5 manifest（历史验证记录，未随公开源码发布）。每批八份产品/测试源码的运行前后文本 SHA 一致，四个实际 QQmlEngine 安装并保留 no-network factory，blocked requests 为空。覆盖聊天状态清除、启动器错误及到期清除、材料库 standalone/hosted 状态、通用弹窗头部明暗/390 宽度/长提示/键盘滚动，以及状态更新和清除。Launcher 当前版本另有 DPR 1 定向回归（历史验证记录，未随公开源码发布），1/1、进程退出 0。

材料库 standalone 验收曾发现头部状态与宿主关闭键有 12×7 px 交集。现定位和高度共同预留宿主 contentTopMargin：默认 Dialog 为 `[16,36,848,600]`；增大 Theme 控件 token 后为 `[16,56,848,580]`，底边 636 位于 640 高窗口内。高密度压力下 status `[380,60,468,48]`、关闭键 `[816,4,48,48]` 与全文 tooltip `[252,112,620,37]` 不相交。尺寸都是逻辑 px，DPR 没有再次乘入布局。hosted 定位及业务状态所有者不变。

复核者 与独立 实现者 已实际查看 DPR 1.5 高密度截图（历史验证记录，未随公开源码发布）（1320×960）；实现者 也查看独立 DPR 1 图。底部保存、使用、关闭保留草稿三个按钮都可见、可用且未裁切。逻辑矩形分别为 `[28,587,274,37]`、`[303,587,274,37]`、`[578,587,274,37]`，无需滚动；两档还分别记录文字区域像素计数。早期共用目录的同名 PNG 曾被另一 DPI 运行覆盖，不能用它证明特定 DPI 的 footer；最终两个目录分开保存。header 的高密度几何也改在 standalone 可见时采样，避免切换 hosted 后记录到高度零。这些是测试证据修正，没有为 footer 增加产品布局改动。

旧 首次失败 manifest（历史验证记录，未随公开源码发布） 保留。DPR 1.5 包装命令曾把 manifest 字典直接交给 SystemExit，导致测试内部为 0、外层进程为 1；记录保留于 wrapper attempt（历史验证记录，未随公开源码发布）。修正包装器按 manifest.exitCode 退出后的完整运行已退出 0。当前八份文件按测试的 UTF-8 文本统一换行方式复核均与最终清单匹配；About/Launcher 的原始字节 SHA 不同是 CRLF/LF 核对方式差异，未回滚或重写这两份文件。材料库测试只使用临时目录和内存状态，未触发真实保存；聊天和启动器均使用内存 stub，未触发真实会话或服务。没有单独验证这组各窗口的 F4 或原生系统关闭。

### 范围与最终清单

本任务拥有共享组件、MainWindow、ProcessWindow、交接后的 FilmSimulationQuick/FilmSpectrumPlot，以及控制器绘图默认值。其余原先无人拥有窗口分配为四组：Source/Substrate、Machine/Settings、Decision/Queue/Appearance/RayTrace、About/Launcher/材料库/独立聊天窗及相关对话框。上述迁移和定向验收已完成。FilmDesignQuick、MaskWindow、MCP/CLI/能力服务由其他任务拥有，没有竞争修改；FilmOptimizationWindow 由针插与隧穿任务 `01a12d0a-4091-72d9-a866-72044b8d40de` 按共享契约接入，本任务仅只读扫描这些外部页面。

全窗口只读扫描（历史验证记录，未随公开源码发布） 找到 17 个 AppWindow 页面及独立 AssistantChatWindow 的 WindowChrome；递归弹窗扫描（历史验证记录，未随公开源码发布） 保留初始记录，并追加 2026-10-12 最终复核。当前旧 statusBar/statusPane 标识符为零，未找到无人拥有的底部全局业务状态区。表单内校验、解释文本和队列项状态保留上下文。MaterialEditor/SourceReferenceDialog 等上下文子控件仅作只读分类，不能把此扫描当作对它们或外部任务的完整 UI 验收；没有业务状态的窗口/对话框不合成标题提示。

测试使用项目 `.conda` Python/PySide6 和 offscreen/software Qt，临时 IniFormat QSettings 与禁止 HTTP 的测试工厂。截图仅在测试进程加载 Windows 字体，不改产品字体或用户设置。没有声称 pytest 运行通过。原生 Windows 移动、贴边、Alt+F4、真实屏幕 DPI 和屏幕阅读器均未由 offscreen 认证；共享测试确认拖动命中区进入 moving 状态，窗口按钮/关闭路径可用。

最终 40 文件源码快照与空白检查（历史验证记录，未随公开源码发布） 同时记录原始字节 SHA 和统一换行后的文本 SHA，便于按各 runner 的计算方式复核，不能混用两种 SHA。此快照仅覆盖本任务范围；hash 不表示每个文件都执行过运行时测试。git diff --check 检查这些已跟踪文件，并对未跟踪文件补充逐行末尾空白检查；独立静态检查见前述 static-checks.json。
