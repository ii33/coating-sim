# Windows 原生测试夹具复核（2026-10-16）

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

本次仅修订测试，产品源码、工程格式、计算协议和 `dev.20261016.01` 版本不变。
基于已发布 `69f6cb48` 的 Windows 原生环境 隔离副本诊断；云端集成另外包含材料编辑器真实就绪等待修复。

## 根因与修复

- 讨论模式测试把整个 `socket.socket` 类型替换为 Mock，破坏 Windows Proactor 对已有自用管道的 `isinstance` 判断。改为 Mock 原类型的 `__init__`，保留类型与已有管道，新建 Python socket 仍抛出 `No network`。全局外部网络保护未改；不连接真实模型供应商。
- 源窗口真实最小宽度为 640，原生窗口管理器不会接受测试要求的 360。测试按 `max(360, minimumWidth)` 调整窗口，并添加真实相邻模块按钮制造溢出，继续使用原鼠标点击及保存、复制、传输、勾选状态断言。
- Windows 鼠标测试显式等待窗口 active/exposed。实机测试进程必须可见启动，不能用 `WindowStyle Hidden` 后把无法点击归因于产品。
- 产品在 QML 加载前配置 Fusion，测试初始化补同一默认值；显式环境覆盖仍保留。

## 不需要产品变更的环境差异

- 两项 MCP 测试在受限环境创建事件循环 socketpair 时停滞，尚未进入 MCP；原生 Proactor 下原测试不改即通过。
- `os.link` 在受限执行环境返回 WinError 5；原生环境发布成功，并继续拒绝覆盖已存在目标。不改文件权限，不增加覆盖或降级写入路径。
- 两项 POSIX 工程进程测试在 Windows 按原有标记跳过，不计作 Windows 通过；其精确 `69f6cb48` GitHub CI 结果均为通过。

## 证据与验收边界

- Windows 原生环境 最终补丁定向报告：28 通过，0 失败，0 跳过；完整补丁 SHA256 为 `aaf2f454ace11b3d2691a1198c731cbef87dc15ccd82e7ccf4b1398b940fbd84`。该数字不是全测试套件，也不与其他轮次重复累计。
- Windows 28 项由讨论整文件 14、操作栏选例 4、MCP 2、设备弹窗 7、源窗口最小尺寸保存 1 组成。与下列云端 25 项重合 18 项，不能按“25＋3”计算。
- 独立断网探针验证类型身份与已有自用管道判断保持、新建 socket 和外部 `asyncio.open_connection` 被拒绝、`asyncio.to_thread` 正常返回；没有建立外部连接。
- 云端应用同一补丁后，`test_assistant_discussion_mode.py` 与 `test_responsive_operation_bar.py` 两整文件：25 通过，13.17 秒。
- 三个变更测试文件 Ruff 检查通过。Mypy 保留 `conftest.py` 原有 16 条诊断，修改前后逐诊断文本一致；另外两文件无诊断。未扩大静态基线。
- 独立只读复核无 P1/P2；`git diff --check` 通过。
- 新提交完整 GitHub CI 及精确最终提交的 Windows 原生环境 更新仍需单独确认；本文不宣称全部验收完成。

Windows 原生环境 原始诊断记录位于本次隔离验收目录的 `windows-diagnostics-20261016/windows-diagnostic-result.json`，保留首轮失败和后续因果复验，不以修复后结果抹去原记录。
