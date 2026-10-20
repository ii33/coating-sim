# Windows Qt 6.12：保留 GPU 的帧更新调度兼容策略

更新：2026-10-21。版本适用范围为本项目兼容约定；已实测的 Qt 版本为 **6.12.0**，不能视为全部 6.12.x 或所有显示拓扑已验证。

## 启动契约

`coat_sim/ui/settings.py:_configure_qt_quick_frame_updates()` 由 `configure_qt_dpi_scale()` 在创建 QApplication/QGuiApplication 前调用。仅 `win32` 且运行时 Qt 版本为 `6.12.x` 时，执行 `os.environ.setdefault("QT_D3D_NO_VBLANK_THREAD", "1")`。任意显式值，包括 `0` 和空字符串，均保持原值；其他平台、版本保持原行为。

环境状态归各启动进程所有，只对新进程生效，不热切换、不改持久用户配置。兼容策略不设置或修改 `QT_QUICK_BACKEND`、`QSG_RHI_BACKEND`、`QSG_RENDER_LOOP`、`QSG_NO_VSYNC`、`QT_D3D_ADAPTER_INDEX`，不绑定厂商、GPU 型号、adapter 索引或独显输出，不改变系统 GPU 偏好。CPU/CUDA 计算后端、物理算法和文件格式不变；未新增异步任务或取消边界。

原 `1db889a` 的缺省软件呈现仅为临时降级，用户明确不接受作为最终方案；本次已移除该缺省。已有 `--software` 和显式 `QT_QUICK_BACKEND=software` 仍可作为备用；启动器继续按实际软件后端同步既有 `softwareMode` 视觉兼容分支。正常 GPU 路径保留 MultiEffect 等原有视觉行为。

## 根因链与依据

Qt **exact tag v6.12.0** 的官方实现提供以下机制：

1. [`QWindowsWindow::requestUpdate()`](https://raw.githubusercontent.com/qt/qtbase/v6.12.0/src/plugins/platforms/windows/qwindowswindow.cpp) 在 DXGI VSync 服务支持窗口时依赖该服务回调投递更新，并停用平台基类更新计时器。
2. [`QDxgiVSyncService`](https://raw.githubusercontent.com/qt/qtbase/v6.12.0/src/gui/rhi/qdxgivsyncservice.cpp) 在 `WaitForVBlank` 失败或耗时不超过 1ms 时只休眠、不发出该轮通知；构造时 `QT_D3D_NO_VBLANK_THREAD` 非零会禁用此通知服务，让窗口请求退回平台计时器路径。
3. 独立只读 COM 采样在当前 DISPLAY1 / HMONITOR 65537、输出所属 NVIDIA LUID `[59888,0]` 上，观察到 `WaitForVBlank` 连续成功却远快于 1ms；间隔 20ms 重测的五次仍为约 31.7–69.3µs。采样未改设备、驱动或系统设置。

这与首帧后不更新、basic 下属性变化但屏幕不变，以及仅切换通知线程参数后恢复硬件呈现的对照一致。该结论针对当前实测环境，不表述为 Qt 上游已确认的缺陷；虚拟显示设备与当前输出拓扑是环境线索，不能据此认定单一驱动责任。

该参数改变的是平台更新通知来源，不是禁用 GPU 或强制关闭 Present vsync。实测窗口 `format().swapInterval()` 始终为 1，未设置 `QSG_NO_VSYNC`；官方机制也未在此参数分支修改图形后端或交换间隔。

## 原生对照

所有探针使用原项目 `.venv`、Windows 原生平台及 `require_escalated`；配置、空工程和助手 IPC 根均隔离到 Temp。未运行硬编码用户助手根的外部 Start 包装器，未操作用户现场窗口。呈现检查使用顶层 `app.exec()` 和纯 QTimer、实际桌面像素、原生 HWND 鼠标消息与模型状态；早期仅有 QTest.qWait 或 grabWindow 的通过不作呈现验收。

| 完整 ProjectRuntime：同一默认 D3D11 硬件路径 | 属性与实际桌面 | 原生加层 |
| --- | --- | --- |
| 通知线程参数缺省 | frameSwapped 恒 2，实际桌面不变 | 模型已为 1，画面仍为空 |
| 显式参数 `0` | 同上，swapInterval=1 | 同上 |
| 显式参数 `1`，两次独立正控 | 帧持续增长，诊断红矩形实际移动，桌面哈希变化 | 模型为 1，屏幕实际显示新增 100nm 行 |
| 最终 helper 自生效，无显式 backend/RHI/loop/flag | 仅补参数 `1`，其余环境仍未设置；默认 D3D11、swapInterval=1，实际呈现恢复 | 原生加层与屏幕一致 |

原生 Qt 日志确认默认所选硬件为 NVIDIA RTX 4070 Ti SUPER，vendor `10DE` / device `2689` / flags `0`，`Prefer software device=0`；不是 WARP。型号只作为这次证据，不进入产品选择逻辑。截图与 GPU 读回会影响帧计数，计数仅证明持续更新，不作为性能基准。

另一独立实例通过 Qt 官方 [`QT_D3D_ADAPTER_INDEX`](https://raw.githubusercontent.com/qt/qtbase/v6.12.0/src/gui/rhi/qrhid3d11.cpp) 参数临时选择 Intel：原生日志确认 Intel Graphics `8086:7D67`、flags `0`、非 WARP；renderer LUID `[64425,0]`，输出仍为当前 DISPLAY1 / HMONITOR 65537。其动画、原生加层和真实桌面呈现均通过。这覆盖“Intel 渲染到当前输出”的混合路径，**不覆盖核显物理输出或多物理屏**；当前 Intel 的 EnumOutputs 没有输出，不能推断这些未测拓扑。

## 交互与入口验证

显式参数 `1` 的完整工程已验证最大化、恢复、最小化、实际两级模块菜单、正式路由打开光谱仿真；干净工程 dirty=false 正常关闭后 QQuickWindow 销毁，未绕过关闭策略。真实 LauncherWindow 组件保持 `softwareMode=false`，hover 和标题按钮通过；真实助手浮标组件的右键菜单开合、hover/highlight、Esc 关闭通过，未发送消息或触发退出菜单动作。

最终无环境覆盖的公开 `coat_sim.ui.project_runtime.main()` 路径已单独验证：只附加诊断定时器，未替换准入或业务控制器；真实创建 Temp 工程、注册、进入顶层 app.exec，最终 helper 仅补通知参数，实际 D3D11、swapInterval=1、真实桌面动画和原生加层通过。实际 GPU 用户现场验收由父端另行记录，不把先前软件模式验收当作 GPU 验收。

## 已排除的候选与限制

- basic 只恢复属性计时，仍停留首帧；用户重启确认 basic 已生效后仍无动画和视觉点击响应，因此不是最终策略。
- 软件呈现能恢复画面，但属于用户不接受的降级，保留为显式备用。
- OpenGL Qt 帧计数与模型推进，但 GDI/Pillow 对该表面的捕获不可用；用户独立肉眼验证“没有红块”，不采用为已验收方案。
- 未改变驱动、显示拓扑或系统配置；尚未覆盖核显物理输出、多屏切换或其他 Qt 补丁版本。

契约测试位于 `tests/test_qt_quick_render_loop_startup.py`，覆盖平台/版本、显式通知参数含 0/空串、backend/RHI/loop/no-vsync/adapter 环境保持、共享启动不强制软件。

证据在 `artifacts/film_synthesis_20261020/gui-incident-20261020/native-probe/gpu-vblank-notification/`，包括缺省/0/1 对照、重复正控、原生日志、默认公开入口、Intel 混合路径和只读 DXGI 采样。
