# Windows 源码启动与 Python 自动部署

2026-10-14：取消构建器 EXE 封装，使用 `dependency_manager.cmd` 启动源码。
该脚本不构建 PyInstaller、Nuitka 或 cx_Freeze 成品，不随仓库分发 Python 二进制。
管理器本身只依赖 Python 标准库；项目依赖仍按现有计划/确认流程安装到项目自己的
`.venv`，自动部署管理器 Python 不等于批准项目依赖安装或启动项目。

## 使用

在完整源码目录运行，保留 `tools/dependency_manager/runtime_manifest.json` 等文件：

```bat
rem 双击或运行：启动管理器 GUI
 dependency_manager.cmd
rem CLI；没有操作参数时默认 inspect
 dependency_manager.cmd -Cli doctor --json
 dependency_manager.cmd -Cli inspect --groups desktop --json
rem 仅检查；没有可用 Python 时返回 2，不下载/安装、不创建管理目录或日志
 dependency_manager.cmd -Cli -CheckOnly
rem 指定正常 CPython；该路径不应属于项目或其他 venv
 dependency_manager.cmd -BootstrapPython "C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
rem 帮助不需要 Python，也不会下载
 dependency_manager.cmd -Help
```

启动器选项放在管理器操作前；其余操作和选项原样转交 `dependency_manager.py`。
`-Cli` 选择控制台默认操作并只验证 Tcl/Tk 初始化；GUI 默认还实际创建、隐藏、销毁
Tk 窗口，验证桌面可用性。CLI 的 JSON 保留在标准输出；启动诊断和日志位置写标准错误。
GUI 启动失败时尽力显示 Windows 错误框；没有桌面时保留控制台诊断。

`-BootstrapPython` 优先，也可用当前进程的 `COAT_DEPENDENCY_PYTHON` 指定。
明确指定的解释器无效时阻断，不偷偷改用别的 Python。未指定时依次检查：

1. `%LOCALAPPDATA%\DepositionDependencyManager\cpython-<manifest version>\python.exe`
2. PATH 中的 `python.exe`、`python3.exe`（跳过会打开 Store 的 WindowsApps 别名）
3. 只读检查 HKCU/HKLM 的 PythonCore 注册项，包括 32/64 位视图、架构后缀和 free-threaded 注册标签

候选须为匹配当前 Windows 架构的正常 CPython 3.10–3.13，具有 `venv`、`ensurepip`
和可初始化的 Tcl/Tk。GUI 还要求真实 Tk 初始化成功。`-I -S -B` 隔离用户环境、
用户/系统 sitecustomize 和字节码写入；不加载项目 Qt 或第三方包。
CPython ≤3.13 的 `-S` 会隐藏 venv 的 prefix 变化，因此同时拒绝解释器旁或上一级
的 `pyvenv.cfg`，并在 Python 探针里复核，不能只依据 prefix 判定普通解释器。

## 无可用 Python 时

自动部署仅使用现有 `runtime_manifest.json` 中的版本、对应架构官方完整 installer
URL 和 SHA-256；没有另一个独立版本 pin。下载前显示官方来源、许可链接、用户目录
和维护/卸载记录说明。标准 Python installer 没有另行勾选许可步骤；继续安装接受
其标准许可，成功后再次提示 [CPython 许可](https://docs.python.org/3.13/license.html)。

- 仅用户目录安装，带 Tcl/Tk、pip、venv；`Include_tcltk=1`
- `InstallAllUsers=0`；不修改 PATH、不安装 launcher、不建文件关联/快捷方式
- 仅 `www.python.org` 的 HTTPS 地址与同源 HTTPS 重定向，保留正常证书验证
- 下载限制 128 MiB、180 秒总预算和 10 秒连接/读取超时，重定向最多 5 次
- SHA-256 校验通过后才运行 installer；使用 .NET 只读流计算，不依赖 PowerShell
  的 hash cmdlet/module autoload，不修改 PSModulePath；installer 等待上限 600 秒
- 只接受新目标；已有不可用目录、链接/reparse point、同一 minor 已注册的 CPython
  或安装锁会阻断。下载后再次核对目标和注册项，避免进入已有 Python 的维护流程
- 验证系统 UCRT 可加载且达到官方 installer 的就绪版本；缺失/过旧/不可验证时阻断。
  不要求系统目录有 VC DLL，不请求管理员提权或重启
- 成功后复核版本、架构、目录身份、venv/ensurepip/Tcl/Tk，再启动现有管理器

失败/中断可能留下部分 Python 安装；不删除、覆盖或自动修复这些文件。请检查日志和
目标目录，再选择已修复的正常 Python。独占 `runtime-install.lock` 防止同一管理目录
并行安装；意外结束留下的锁需确认没有 installer 仍在运行后由用户处理。脚本不自动
删除既有锁；installer 超时或终止状态不确定时，也保留本次锁和下载供检查。只清理本次创建的唯一临时下载目录；不会清理项目 `.venv`。

默认日志：`%LOCALAPPDATA%\DepositionDependencyManager\logs\bootstrap-<id>.log`。
日志仅写受控状态，不记录原始异常、探针 stderr、完整 argv、代理地址或认证内容。
installer 子进程不继承代理认证环境变量。管理器操作返回码原样传递；启动器失败为 2，
`-CheckOnly` 为 0（可用）或 2（缺失/阻断）。PowerShell 自身的策略/解析错误可能返回 1。
Ctrl+C/会话关闭与系统进程终止不保证回滚安装，不应把中断视为已恢复干净状态。

## 代理

默认 `-BootstrapProxyMode env` 读取当前进程一致的 HTTP_PROXY/HTTPS_PROXY，或仅在
两者缺失时使用 ALL_PROXY，并对官方下载尊重 NO_PROXY。它不读取或修改系统代理。
只支持 `http://` CONNECT 代理；HTTPS-proxy TLS-in-TLS 与 SOCKS 明确阻断。

```bat
 dependency_manager.cmd -BootstrapProxyMode none
 dependency_manager.cmd -BootstrapProxyMode http -BootstrapProxy http://127.0.0.1:10808
rem CLI 示例中，启动器和管理器均明确选择直连
 dependency_manager.cmd -Cli -BootstrapProxyMode none benchmark --proxy-mode none --json
```

启动器代理仅用于初次 Python 下载；管理器的镜像/代理仍由其现有选项或 GUI 控件选择。
认证代理只从当前进程环境传入，例如 `COAT_DEPENDENCY_PROXY` 与
`-BootstrapProxyMode http`，不在参数中放用户名、密码或 token，也不保存凭据。

## 操作系统与策略前提

需要 Windows PowerShell 5.1 和交互桌面（GUI）。`.cmd` 在 64 位 Windows 上优先使用
Sysnative 选择本机 PowerShell，避免误选 32 位 Python。受管理版本的 Windows 支持与
完整 installer 参数以 [官方 Windows 文档](https://docs.python.org/3.13/using/windows.html#installing-without-ui)
为准；当前清单的 Python 3.13 要求 Windows 8.1 或更高版本。本启动器保守要求系统
`ucrtbase.dll` 可加载、固定版本为 10.0 且至少 10.0.10137.0，以避免进入 installer 的
可选 UCRT 部署；缺失/过旧时请由管理员按组织流程完成
[Microsoft 官方 UCRT 系统更新](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment)。
Windows 10/11 的 UCRT 是操作系统组件。系统目录没有 `vcruntime140.dll` 不会阻断。
启动器不会为此提权，不改变长路径策略、系统网络设置或信任证书。

启动器使用 `powershell.exe -NoProfile -File`，严格保留机器的执行策略。
如果 Windows 禁止运行未签名脚本，PowerShell 会在启动器代码执行前报告错误；
`.cmd` 提示获取管理员批准的签名脚本，并指向本说明。没有 `-ExecutionPolicy` 绕过、
`Set-ExecutionPolicy` 或读取脚本后改用字符串执行。已有 Python 时，可用原源码入口
`python dependency_manager.py`。没有 Python 且脚本被组织策略禁止时，需要用户/管理员
完成组织批准的 Python/脚本部署；不能声称此环境已能自动启动。

## 前提判定的官方源码依据

针对清单所选的 CPython v3.13.16 核对了官方 installer 源码：

- [exe_files.wxs](https://github.com/python/cpython/blob/v3.13.16/Tools/msi/exe/exe_files.wxs)
  将 `vcruntime140.dll` 和条件启用的 `vcruntime140_1.dll` 放到 `InstallDirectory`，与
  `python.exe` 同目录，由本工具已启用的 `Include_exe=1` 部署。它们不要求预先存在于 System32
- [PythonBootstrapperApplication.cpp](https://github.com/python/cpython/blob/v3.13.16/Tools/msi/bundle/bootstrap/PythonBootstrapperApplication.cpp)
  的 `IsCrtInstalled` 检查 UCRT 10.0.10137.0；`WillElevate` 针对全用户安装/全用户 launcher。
  本工具保持两者关闭，并以系统 UCRT 就绪作为更保守的启动条件
- [crt.wxs](https://github.com/python/cpython/blob/v3.13.16/Tools/msi/bundle/packagegroups/crt.wxs)
  另有未就绪时的每用户 UCRT 分支。此处不启用该 fallback，也不代替用户部署 OS 组件；
  因此这项系统 UCRT gate 是本项目的保守边界，不能写成官方 installer 永远需要全局 VC redistributable
- [pythonba.vcxproj](https://github.com/python/cpython/blob/v3.13.16/Tools/msi/bundle/bootstrap/pythonba.vcxproj)
  的 bootstrapper 使用静态 `MultiThreaded` CRT；不应把已安装 Python 的 DLL 搜索路径
  误当作 installer 启动前提

这次修正仅去除错误的全局 VC DLL 前提，并补齐 UCRT 版本验证；下载/hash、用户目录、
注册项、无提权/无系统设置变更的边界不变。真实首次安装仍须干净 Windows 验收。

## 状态所有权与验证边界

PowerShell 仅拥有本次探针、受管理 Python 下载/安装、日志和进程等待；唯一安装锁
保护受管理目录。现有 Python CLI/Tk GUI 拥有依赖计划、确认、pip 工作、项目获取及
项目 `.venv`，接口仍以现有 `manager.py`/`project_deploy.py` 为准。启动器同步等待，
不创建 Qt 页面，也不改变 GUI 的异步任务、取消或关闭边界。

离线测试：

```sh
python -m unittest tests.test_dependency_bootstrap_windows -v
python -m unittest discover -s tests -p "test_dependency*.py" -v
```

在 Windows 上且有 PowerShell 时，unittest 自动执行 `tests/dependency_bootstrap_windows_offline.ps1`：
真实参数往返，manifest/HTTPS/hash/代理校验，以及模拟的安装成功、注册项竞态、
下载失败、部分安装保留、已有锁保护。该 harness 不使用网络、不开真实 installer、
不写 registry。Linux 另外实际运行 `-I -S -B` 的 stdlib 探针和中文/空格路径中的源码入口，
并创建一次性 venv 验证 `-S` 下不会误判普通 Python。非 Windows（即使已安装 pwsh）或 PowerShell 缺失时会明确跳过原生
harness；源码静态断言不能替代原生 PowerShell 或干净 Windows 安装验收。

仍待干净非管理员 Windows VM：无 Python/Tk/Qt 时的真实官方下载与 hash、安装无 UAC/
PATH/launcher/关联变化、GUI 显示、CLI JSON、重复启动复用、同 minor 注册项/损坏目录
阻断、网络失败和中断恢复。Windows source CI 可验证语法和离线 harness，但现有
Python 的 CI runner 不能证明无 Python 的首次启动，也不能替代真实桌面验收。
