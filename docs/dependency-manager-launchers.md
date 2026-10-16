# 部署启动入口

2026-10-16。`tools/dependency_manager/launchers.py` 是启动入口计划、验证与
文件写入的唯一所有者；CLI/Tk GUI 仅收集选项、展示完整计划并确认执行。
不启动应用，不注册开机启动项，不修改已有快捷方式，不安装依赖。

## API 与边界

- `plan_launch_entries(root, *, shortcut='none', output_dir=None, name='Coating Simulator')`
  返回 schema_version=1 的完整计划，含 status、errors、warnings、plan_id、
  将写入的 files、python、icon、icon_sha256、输入文件身份及脚本文本。
- `execute_launch_entries(plan, *, confirmed=False)` 重新生成并逐项比较计划；
  未确认、目录/文件身份变化、名称冲突或被篡改的计划返回 blocked。
  写入成功才返回 success；部分写入失败返回 failed 并列出已创建文件，不假装回滚。
- 创建的是部署根目录内 `<name>.cmd`（Windows）或 `<name>.sh`（Linux）。
  始终只使用部署 `.venv` 的 Python，创建时验证正常 CPython 3.10–3.13、
  venv prefix 和禁用 system site packages。运行时缺少解释器直接报错，
  不回退系统 Python、`.conda` 或 PATH。`-E` 防止继承 PYTHONHOME/PYTHONPATH。
  脚本相对于自身定位部署目录，设置工作目录并传递参数/退出码。
- shortcut 为 none（默认）、desktop、start-menu 或 custom。Windows 桌面/
  开始菜单路径使用当前用户系统文件夹 API，支持重定向；custom 要求指定已有目录。
  Linux 仅创建 shell 脚本，不新增 `.desktop` 或 macOS 支持。
- 所有同名内容，包括旧版工具生成的文件，一律保留并阻断。界面让用户选择新名称，
  或自行检查并处理旧文件后重新计划；此版本不提供自动覆盖选项。

Windows 原生 `.lnk` 通过系统 Windows PowerShell 5.1 的 WScript.Shell COM
创建；参数放 UTF-8 JSON 文件并通过 `-LiteralPath` 读取，不拼入 PowerShell 代码。
使用 `-File` 且不绕过执行策略。应用启动直接指向 `.venv/Scripts/python.exe`，
无需 PowerShell 7 或任何 PowerShell 运行时。生成失败（策略、COM、缺失系统组件）
会明确失败，并且先暂存快捷方式，避免这些失败留下启动脚本。

图标直接引用现有 `coat_sim/ui/assets/icons/launcher.ico`，沿用
`coat_sim/ui/icons.py` 的 `icon_path('launcher', desktop_shortcut=True)`。
本次核对 SHA-256：
`77a6336340f91c55368ee1b6c459a82f1e964515546522fd547ab47d71d29cc5`。
不生成或替换 ICO。快捷方式固定完整绝对部署路径；移动部署后需重新生成快捷方式。

执行是短同步文件事务，PowerShell 最多等待 30 秒，不支持中途取消。
GUI 应保持单一操作，禁用并发与取消；关闭时等待当前事务结果，不能把关闭窗口
描述为已撤销文件创建。文件写入独占创建；跨文件操作不保证整体原子性。

## 验收

`python -m unittest tests.test_dependency_launchers -v` 使用自造临时目录，
覆盖确认、重复创建/陌生文件保留、计划修改、目标身份、图标、无系统环境回退、
中文/空格/特殊字符路径、真实 Linux shell 参数/工作目录/退出状态及 COM 失败。
这些测试不会修改用户桌面或启动真正项目 GUI。

Windows 原生验收仍须在隔离目录检查 `.lnk` 的 TargetPath、Arguments、
WorkingDirectory 和 IconLocation，以及真实 cmd 参数、中文路径、桌面重定向、
非管理员启动和策略阻断。Linux 的模拟 Windows 单测不替代该验收。
