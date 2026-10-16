# 交互式在线安装器与高级依赖 CLI

当前为开发源码，新交互安装流程尚未完成原生端到端验收，不是正式 Release。管理器 GUI 已取消，EXE 封装已停止。安装器自身仅使用 Python 标准库，不导入 `coat_sim`、Qt、NumPy 或目标环境的包；不要求安装第三方 Python 包。

## 推荐入口与选择

- Windows：双击 `install.bat`。不要求预装 Python、Git 或 PowerShell 7；使用系统 Windows PowerShell 5.1。脚本策略或签名阻止执行时明确退出，不绕过策略。
- Linux/macOS：运行 `sh install.sh`。先检查本地受管理 Python，再检查 PATH 中完整 CPython 3.10–3.13（含 venv/pip）。当前无 Python 时仅给出官方安装指引，不自动修改系统包；不能承诺所有平台从零安装。
- 已有合用 Python：`python dependency_manager.py`，或 `python dependency_manager.py interactive --project /path/to/install-root`。

终端依次询问安装目录、源码（1 CNB 默认，2 GitHub）、是否镜像加速（默认否）、是否开发环境（默认否），再自动发现 Python 并提供项目内独立 Python 选项、显示计算环境建议和安装计划。独立 Python 先展示计划，再以一次明确确认决定是否安装。缺少 Python/Git 时还需审阅官方来源、校验值、许可和目标目录。

默认 CNB 为 `https://cnb.cool/nonereason/coating-sim`；GitHub 为 `https://github.com/ii33/coating-sim`。镜像加速选择清华 TUNA PyPI，只改变包源，不改变 Git URL。完整用户依赖自动选择 `desktop + appearance`；开发模式额外加入测试和静态检查工具。计算建议与 Python 版本必须兼容；检测到设备或安装包不表示驱动、数值与完整算法已经验收。

新安装布局：安装根目录下 `.runtime/python`、`.runtime/git`，源码 `app/`，依赖 `app/.venv`。若选择的根目录本身或其 `app/` 已是合法工程，只处理该工程依赖，不拉取覆盖源码；已有 venv 保持原解释器。未知目录内容不覆盖，不删除无关文件。最终确认包含创建 `app/launch-coating.cmd` 或 `.sh`，不覆盖已有同名文件，也不默认创建桌面快捷方式。安装后对真实 `.venv` 做有界 GPU 数值验证；安装成功与 GPU 可用分别报告，失败提示 CPU 方案，不自动修改驱动或重装。终端显示人读摘要，详细脱敏 JSON 保存在安装根目录 `.dependency-manager/logs`。

Windows 官方完整 Python 安装器可能在根目录外写入维护缓存及每用户注册/卸载记录；项目内安装不等于完全便携、零系统痕迹。引导日志在 `.dependency-manager/logs`，运行时安装日志在 `.runtime`。安装超时或子进程终止不确定时保留锁、下载及恢复材料，供人工检查；不自动重试或删除部分安装。详见[引导说明](dependency-manager-bootstrap.md)。

## 高级 CLI 与目标保护

```sh
python dependency_manager.py doctor --json
python dependency_manager.py inspect --groups desktop,appearance --json
python -S dependency_manager.py inspect --json
```

原有非 GUI CLI 仍支持检查、计划、安装/修复、镜像测速、运行时、源码部署和启动入口。`dependency_manager.cmd -Cli` 只检查；高级 CLI 的默认组与交互式完整用户组分别定义，不应互相推断。

默认依赖目标为项目 `.venv`；`--venv` 可明确选择其他隔离 venv。系统 Python、含 system-site-packages 的环境、链接目标或身份不一致的解释器不可作为安装目标。现存非 venv 或损坏环境不覆盖、不自动重建；保留原目录并选择新的目标。缺 pip 的有效 venv 可通过 repair/ensurepip 修复。

## 依赖组和计划

- `cpu`：真实运行声明中的 NumPy、SciPy、Matplotlib、Pillow、Pydantic
- `gui`：PySide6-Essentials/Addons，并带 CPU 组
- `assistant`：MCP/助手/代理支持，并带 CPU 组
- `interchange`：keyring、ezdxf、openpyxl，并带 CPU 组
- `desktop`：以上四个运行组，覆盖 requirements.txt 的 15 项直接依赖
- `appearance`：CPU 组 + requirements-test.txt 中 Mitsuba/DrJit 精确配对
- `test`：CPU 组 + pytest/mpmath；独立选择 appearance 获取真实渲染测试依赖
- `sandbox`：requirements-sandbox.txt 的 pygal/importlib-metadata/zipp 精确 pin

版本检查依据项目 specifier；pin 不符显示 conflict，即使 `pip check` 能通过。
已安装包还检查所请求 extras 的递归 metadata 依赖，不导入包运行代码。
`install` 仅补缺失；`repair` 同时修正选中要求冲突。解析器可能替换相关依赖，
工具不会显式卸载无关包。先以 `--only-binary=:all:` 做 wheel dry-run，确认可
解析后才安装；没有 wheel 时失败，不自动编译来源不明的代码。

CLI 先看计划，再复制当前 `plan_id` 确认执行；目标、要求、镜像或身份改变需
重做计划：

```sh
python dependency_manager.py plan --groups cpu --python /path/to/python --proxy-mode none --json
python dependency_manager.py repair --groups cpu --python /path/to/python --proxy-mode none --plan-id PLAN_ID --yes --events
```

`--events` 输出 JSON lines（phase/log/result）；`--json` 只输出最终 JSON。
交互终端与高级 CLI 复用同一计划和执行函数。确认后才执行；Ctrl+C 取消会尽力终止 pip 的进程组/Windows 子进程树，
可能已经部分安装；再次检查并确认新计划后重试。
操作锁阻止同一目标的两个管理器同时修改；异常退出留下的 lock 不会自动删。

## 镜像测速和代理

内置 [PyPI](https://docs.pypi.org/api/index-api/)、
[清华 TUNA](https://mirror.tuna.tsinghua.edu.cn/help/pypi/)、
[USTC](https://ipv4.mirrors.ustc.edu.cn/help/pypi.html)，只接受维护方文档支持的
HTTPS simple 地址。正常 TLS 验证，不加 trusted-host、不改全局 pip 配置。

```sh
python dependency_manager.py benchmark --mirrors pypi,tuna,ustc --proxy-mode none --json
python dependency_manager.py plan --mirror tuna --proxy-mode http --proxy http://127.0.0.1:10808 --json
```

上例端口只是示例，工具不假定本机已开代理。`none` 直连；`env` 使用当前有效
HTTP CONNECT 环境代理；`http` 仅本次显式 HTTP CONNECT 代理。HTTPS-proxy TLS-in-TLS / SOCKS 不支持，会明确报错；
不会因为安装了项目 socksio 就假装标准库工具能使用它。代理统一用于测速、
官方运行时下载和 pip；不会改系统代理/网络设置。

高级 CLI 不接收 argv 里的认证信息；
可在当前进程环境设置 `COAT_DEPENDENCY_PROXY` 后用 `--proxy-mode http`。
日志/计划移除 URL userinfo、query 和常见 token，子进程代理放环境变量而非
命令行；不默认持久保存凭据，凭据不进仓库。其他同用户进程仍可能读取
进程环境，敏感代理账号应采用最小权限。

测速先有界获取 simple/pip 页面，再读取固定 `pip-25.2-py3-none-any.whl`
样本最多 256 KiB；报告实际来源、字节数、完整索引请求延迟和含连接开销的
吞吐。每镜像有总时限，取消/超时有明确返回。结果只是网络估计，特别是
镜像跳到 PyPI CDN 时不能称为镜像自身下载速度；推荐只有用户接受才改选源。

## 没有项目 Python：受管理运行时

两条路线：选择已有正常 CPython 3.10–3.13（须有 venv/ensurepip），或获取
固定官方 Windows CPython 3.13.16。下载地址、SHA-256、架构和许可固化在
`runtime_manifest.json`，来自 [Python.org 3.13.16 发布页](https://www.python.org/downloads/release/python-31316/)，
不会联网自动改 manifest 或降级。Windows x64/x86/ARM64 各有独立 SHA；
ARM64 installer 的官方状态为 Experimental，并不保证项目依赖都有 wheel。

```sh
python dependency_manager.py runtime-plan --proxy-mode none --json
python dependency_manager.py runtime-install --proxy-mode none --plan-id PLAN_ID --yes --events
```

确认中披露官方来源、hash、所选安装根目录内的运行时目录和 [CPython 许可](https://docs.python.org/3.13/license.html)。
校验完成前不执行下载文件。正常完整 installer 带 pip/venv，参数按
[官方 Windows 文档](https://docs.python.org/3.13/using/windows.html#installing-without-ui)：
非系统级安装，禁止 PATH、launcher、默认关联、快捷方式；官方安装器仍可能在所选目录外产生维护缓存及每用户注册/卸载记录。检测到同一 minor CPython 已注册时阻断，避免 installer 维护已有
环境；请选择现有解释器。系统 UCRT 前提未验证时阻断，不请求管理员提权；VC DLL 由官方安装器随 Python 安装到其自身目录。
启动脚本部署的完整解释器用于创建 `.venv`；官方 embedded 包缺 pip、不用于普通
pip 管理。Linux/macOS 初版无官方等价可解包 CPython 引导，明确提示选择已有
解释器；不私自换其他来源。

项目实测环境为 Python 3.12。3.13 是新受管理候选，wheel dry-run/最终版本
检查不能替代数值/GUI 实机验收。DirectML 仅按现有项目的 Windows
Python3.10–3.12 profile提供：选择3.13受管理运行时时，在下载前就阻断；可选
已有兼容解释器，不自动降级。CUDA12只开放Windows/Linux x86-64 CuPy profile，
硬件/驱动仍手工验证；ROCm/XPU/任意vendor构建初版manual-only。

## 依赖配置与验收

### 运行与开发依赖

CLI `--profile runtime`（默认）和交互安装使用同一清单：完整
`desktop`为requirements.txt的15项直接运行依赖，不包含pytest、Ruff、mypy
或打包工具。`--profile development`在已选择的运行feature基础上仅增加
pytest/mpmath和requirements-analysis.txt中Ruff/mypy的直接固定项，合计19项。
profile 本身不会递归安装 analysis-lock 或自动选择 GPU。交互安装另行自动选择 desktop + appearance，因此包含 Mitsuba/Dr.Jit；高级 CLI 的组与后端仍可显式选择，sandbox 不默认加入。

### 历史封装实验（已停止）

2026-10-14 曾验证 cx_Freeze/PyInstaller 候选方案，随后取消 EXE
封装。两个构建入口现均拒绝执行，即使带 `--test-build` 也不会创建二进制。
CI 仅运行源码检查。保留的 `build_catalog` 和构建依赖清单只供历史实验追溯，
不是运行或开发配置，不会被正常部署安装。

此前 PyInstaller Linux 候选包的 CLI 隔离 smoke 已通过，但没有完成 Windows
二进制和真实 GUI 验收；不得将它作为正式发布包。旧原生依赖、许可清单见
[历史分发记录](dependency-manager-distribution.md)，后续来源变更不能沿用此结论。

源码离线测试覆盖计划、确认、配置隔离、取消、哈希/架构/路径冲突、网络失败、
镜像来源与打包门禁。既有在线安装与旧 GUI 的历史验收不等于新交互流程验收；新入口的真实下载、运行时安装、目标依赖安装、原生启动及进程取消仍需端到端验证。

## 获取本项目源码

交互安装默认选择 CNB，也可选择公开 GitHub；新安装源码放到安装根目录的 `app/`。高级 CLI 使用 `project-plan` / `project-deploy`，CNB 需指定 `--source cnb --repository-url https://cnb.cool/nonereason/coating-sim`，GitHub 使用 `--source github`。

新部署只接受新目录。分支/标签解析后记录完整提交 SHA；先准备源码，再展示完整依赖计划，经确认后仅在该 checkout 的 `.venv` 安装依赖。执行使用已确认计划，不再次拉取代码。已有合法工程在交互模式只安装依赖，不自动更新源码；工具不接收或保存 Git 凭据。CNB 匿名读取及管理器计划已验证，不代表完整部署或所有平台验收。详见[部署约定](dependency-manager-deployment.md)。

## 已有 Python 自动识别与 DirectML 阻止提示

新建目标环境且未显式指定创建用 CPython 时，管理器会从已知位置有限探测已有
Python，并选择首个与后端兼容的正常解释器。显式填写的路径不会被自动替换。
现有目标 venv 的解释器始终优先；选择另一个创建 Python 不会改变已有 venv。
自动识别只读，不会下载或安装 Python，也不会更改 PATH。

DirectML 的 Windows / Python 3.10–3.12 检查发生在 pip 下载之前，因此此类
阻止不能靠切换清华等镜像解决。提示会列出实际探测的平台、版本；探测失败时
显示 unknown 和失败原因，不把管理器自身的 Python 当作目标版本。

若现有环境使用不兼容版本，请保留该环境。依赖管理可显式选择兼容 Python，
并将新目标设为未使用的 `.venv-directml`；项目部署固定使用 checkout/.venv，
因此应选择新的部署目录并选择兼容 Python，而不是修改已有环境。
现有受管理下载清单仅提供 Python 3.13，不能用于当前 DirectML 配置。
