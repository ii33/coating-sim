# Windows Qt 6.12 原生呈现兼容策略（2026-10-20）

## 最终策略与边界

`coat_sim/ui/settings.py:configure_qt_dpi_scale()` 在 QApplication/QGuiApplication 创建前调用共享配置。仅 `win32` 且运行时 Qt 为 `6.12.x`，并且环境中 **同时不存在** `QT_QUICK_BACKEND` 与 `QSG_RHI_BACKEND` 两个键时，默认设置 `QT_QUICK_BACKEND=software`。任意显式后端值（包括空字符串）均优先；其他平台及 Qt 版本保留原默认行为。不设置或修改 `QSG_RENDER_LOOP`、`QSG_RHI_BACKEND`。

环境状态由各启动进程持有，只对新进程生效，不进行运行中热切换，不修改持久用户配置。此策略仅影响 Qt Quick 界面呈现，不改变 CPU/CUDA 等物理计算后端、算法、文件格式或数值语义。没有新增异步任务或取消边界。

`coat_sim/ui/launcher.py` 在共享配置后同步实际 `software_mode`，沿用已有 `--software` 的视觉兼容分支，关闭软件呈现不支持的 MultiEffect，保留正常控件、输入和动画。没有修改页面交互断言、模块准入或保存/关闭确认。

软件呈现可能降低复杂场景吞吐量，这是本项目针对该环境实测的兼容策略，并非已确认的 Qt 上游缺陷。原生实测版本为 Qt 6.12.0；将缺省策略限定到 6.12.x 属于本项目兼容约定，其他补丁版本未逐一做原生验收。

## 诊断依据

全部原生探针使用原项目 `.venv`、Windows 原生平台及 `require_escalated`；配置和空工程隔离到 Temp，不导入 offscreen/software 测试守卫，不操作用户现场窗口。最终呈现验收使用顶层 `app.exec()` 与纯 QTimer；早期 QTest.qWait 属性级结果不作为呈现验收依据。

- 默认 D3D11 的最小原生窗口动画时间停滞。basic 可让属性推进，但屏幕仍停留首帧。
- 完整原版 ProjectRuntime 的 basic + D3D11 反控：原生 HWND 鼠标消息已使模型层数变为 1，`frameSwapped` 仍为 2；实际桌面仍显示空膜堆。Qt `grabWindow()` 已变化，说明属性/离屏抓图通过不能证明实际呈现。
- 同一完整工程软件正控：帧持续增长，实际桌面动画区域哈希变化；原生点击后模型层数为 1，实际屏幕出现新增 100nm 膜层。
- OpenGL 能推进 Qt 帧和模型，但 GDI/Pillow 抓到浏览器而非 OpenGL 表面；该捕获不可判断真实呈现。用户独立肉眼验证回复“没有红块”，故不采用 OpenGL 为最终默认方案。

basic 的第一轮用户重启反证被保留：新启动器、助手、工程运行时均已采用 basic / Fusion，用户仍报告无动画、设计页不可点击。不能将该候选描述为成功修复。

## 最终验证

定向契约测试 `tests/test_qt_quick_render_loop_startup.py` 共 14 项通过，覆盖平台/版本范围、显式后端含空值、共享入口及 render loop 不变。

无显式后端的新进程确认 `GraphicsApi.Software` 且未设置 render loop。完整 ProjectRuntime 真实准入后：

- 顶层 app.exec / QTimer 下动画区域真实桌面哈希持续变化，frameSwapped 为 22、30、42、53、73。
- 原生 HWND 消息加层后模型为 1，实际屏幕显示新增膜层。
- 原生标题按钮完成最大化、恢复、最小化；设计模块菜单及仿真子菜单展开，经正式菜单路由打开光谱仿真。
- 干净工程 dirty=false，正常生命周期关闭后 QQuickWindow 销毁，无 allowClose 绕过。

启动器实际组件确认缺省软件后端与 `softwareMode=true`，最大化、恢复、最小化有效。助手实际组件的右键菜单可展开；最终纯定时探针在获取弹出窗口包装器时遇到生命周期异常，因此不把该探针的 hover/ESC 后续步骤算通过，也不据此扩大产品修改。早期组件级菜单开合/hover结果单独保留，不替代最终现场验收。

**用户最终现场验收：原版全部重开后，明确确认点击和动画均恢复。** 验收记录位于 `artifacts/film_synthesis_20261020/gui-incident-20261020/accepted-original-gui.json`；只读核实三个新进程为 software、非 offscreen。未要求用户重复操作。

证据目录：`artifacts/film_synthesis_20261020/gui-incident-20261020/native-probe/`。重点包括 `final-default-software-interaction-result.json`、`final-default-software-after-native-add.png`、`final-default-software-menus-result.json` 及对应探针脚本。反控保留 `full-runtime-native-interaction-basic-result.json` 与 stale 桌面图。

## 官方机制参考

Qt 官方 [Scene Graph — Driving Animations](https://doc.qt.io/qt-6/qtquick-visualcanvas-scenegraph.html#driving-animations) 解释渲染循环与动画驱动；[Qt Quick Software Adaptation](https://doc.qt.io/qt-6/qtquick-visualcanvas-adaptations-software.html) 说明软件呈现及限制。本项目版本限定和兼容默认值基于本次实测，官方文档不构成上游缺陷确认。
