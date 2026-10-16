# 启动器窗口单实例与打开意图

日期：2026-10-13。仅改变同一启动器进程中的窗口路由和生命周期，不改变物理算法、文件格式或数值语义。

## 输入、输出与唯一状态所有者

`LauncherBridge.open_window(module_key, document_path='', *, mode='', payload=None) -> str`、QML `openWindow`、`openOrActivate` 和 `launch` 共用同一类型路由。每个 module key 最多一个已注册窗口或尚在导入/创建的请求。第一次打开返回新 instance ID；后续打开返回同一 ID，恢复最小化、显示并请求前台激活。没有路径或 payload 的 `new` 是打开该类型窗口的意图，不清空现有草稿。独立 `--module` 启动和独立窗口辅助函数仍按各自进程拥有窗口，不增加跨进程锁。

启动器唯一拥有实例 ID、组件创建、异步请求及释放；页面/controller 继续唯一拥有文档、dirty、会话、编辑草稿、结果与取消。`WindowRegistry` 读取同一实例，不拥有第二份窗口或文档账本。打开请求的返回 ID 表示窗口已存在或正在创建，不表示文件加载、确认或计算成功。

## 复用时的文件/设计意图

- 相同当前文档路径的再次打开只激活，保留未保存内容和编辑位置，不当作刷新。
- 新文件或设计 payload 传给 controller 的 `reuseWindowOpen(document_path: str = '', payload: object = None) -> bool`（如果提供）。True 表示接受或进入原确认流程；False 表示拒绝并保留现有内容。controller 负责初始化延后、相同 session 同步与不同来源切换，launcher 不写私有 session 或绕过保存/放弃确认。
- 未提供该接口的文档模块复用既有 `requestOpenFile` / `openDocument`；工艺的 `confirmDocumentReplace` 仍由原页面处理。其余 dirty 模块拒绝替换并发出 `launchError`，提示先保存或使用该窗口的打开动作；不会自动放弃内容。
- 无安全重定向 API 的 session/外观 payload 拒绝替换并给出错误，不创建第二个同类型窗口，也不静默丢弃意图。已有同来源 companion 仍用原 `refreshFromSource` / `refreshDetachedSource` 和开始计算动作。
- 创建期间的重复无载荷请求合并。尚未建立 controller 的新载荷可替换初始请求；controller 已建立后，最新的显式载荷在同一实例注册后应用。取消或失败清除此实例的延后意图；迟到回调不能创建第二实例或更新已销毁窗口。

## 关闭、销毁与错误

窗口原 close/dirty 确认不变。被拒绝的 close 保留 registry；接受的关闭（不可见）或 destruction 释放此实例、presentation 等待和延后意图，重新打开得到新 ID。最小化不释放实例。注册表清理检查失效的 Qt 对象，重复清理幂等。

IPC `open_window` 使用同一实例并保留现有 force 门禁；fields/data/path 仍由原 WindowAdapter 校验和应用。模块导入结束但 QML 尚在创建时仍报告 loading，不误报 closed 或丢弃待应用参数。force 关闭可以取消尚在创建的请求；没有 force 时明确报告 window_loading。

定向回归：`tests/test_launcher_single_instance.py`。Qt offscreen 验证不替代真实桌面前台焦点、系统最小化及浅深主题像素验收。

## Quick 膜系 IPC 适配

`QuickFilmWindowAdapter` 只读取设计 controller 的公开 `state`、初始化状态与 session 快照，或仿真 controller 的公开 `designJson`、`designPath`、`settings`、`documentDirty`、`exportSimulationConfiguration()` 与结果状态。旧 Widgets 后端仍使用原 `FilmWindowAdapter`，不把 Widgets 私有控件结构套到 Quick controller。

Quick 字段白名单以 `DESIGN_FIELDS` / `SIMULATION_FIELDS` 为接口真值。完整文档与路径替换统一调用公开 `reuseWindowOpen`；未知字段、pointer、意图路径赋值及同时替换文档/编辑字段明确拒绝。dirty 文档的外部替换仍须 `force=true`；force 不绕过 controller 的原确认。回执 `pending_confirmation=true` 表示应先在原窗口处理确认，取消保留旧内容。初始化未完成返回 loading，失败明确返回 failed 和初始化错误。

本适配只声明实际已接通的 Quick 动作：设计 `new/save`，仿真 `calculate/export_csv/export_png`；其他旧 Widgets 动作不会被伪称可用。calculate 继续拒绝直接执行并返回 `compute_confirmation_required`。保存/导出要求明确 overwrite 布尔值、受 root 约束的路径、有效格式及当前可导出结果。不能通过字段或动作参数指定任意 QObject 方法、私有属性、会话对象或计算门禁。人工设计页面的“发送到仿真”沿原 GUI 请求进入同一仿真实例并请求计算，start 在来源替换确认之后由 controller 合并执行。

### 定向验收范围

新增实例回归覆盖全部启动器 module key 的重复路由、导入/QML 创建合并、最新显式意图、dirty 保留、控制器确认/拒绝、最小化恢复、关闭取消、接受关闭、destruction、取消后的迟到导入、实际 About QML 的独立路由和真实 Quick 设计/仿真 list/read/更新门禁。浅深主题文本光标与菜单验证另见共享控件回归。真实 Windows 原生拖动、系统前台焦点以及用户机器截图由独立验收执行，offscreen 结果不替代它们。
