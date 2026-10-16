# 独立依赖管理器

> 当前交付方向（2026-10-14）：已取消 EXE 封装，改用源码和 Windows Python
> 引导脚本。没有可用 Python 时先部署带 venv/pip/Tcl/Tk 的完整 Python，
> 再启动依赖管理器；项目依赖仍安装到项目虚拟环境。下方打包记录仅为历史
> 验证证据，打包入口已停用，CI 不再生成二进制。


项目附带的维护工具，GUI 优先兼容性，再近似项目文案/颜色。源代码只使用
Python 标准库，绝不导入 `coat_sim`、Qt、NumPy 或目标环境的包。
Windows 使用 `dependency_manager.cmd`：先检查正常 CPython、venv、ensurepip 和 Tcl/Tk，
不存在时由 PowerShell 部署固定官方完整 Python，再启动源码 GUI。项目环境损坏
不会妨碍管理器使用独立解释器。首次安装需要网络及满足系统前提，不能承诺
任何电脑都能无条件启动。详见[启动引导说明](dependency-manager-bootstrap.md)。

## 启动和部署入口

源码：

```sh
python dependency_manager.py gui --project /path/to/deposition-sim
python dependency_manager.py doctor --json
python dependency_manager.py inspect --groups desktop,sandbox,test --json
python -S dependency_manager.py inspect --json
```

Windows 双击 `dependency_manager.cmd` 打开 GUI；`dependency_manager.cmd -Cli`
启动 CLI，`-CheckOnly` 仅检查、不安装。此入口不修改系统 PATH 或 PowerShell
执行策略。已安装 Python 的其他系统继续使用上方源码命令。

默认目标为项目 `.venv`。`--venv` 明确选择其他隔离 venv；完整路径/解释器与
前缀身份都会展示。系统 Python、含 system-site-packages 的环境、symlink
目标或身份不一致的解释器不可作为安装目标。现存非 venv 目录不会被覆盖。
当前不会自动重建缺失/损坏解释器的现有 venv：请保留该目录，选择新的目标
venv；不要删除其中无关数据。缺 pip 的有效 venv 可通过 repair/ensurepip 修复。

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
GUI 的安装/修复按钮先异步检查计划，确认后才执行。同一后台函数提供
CLI/GUI，不阻塞 Tk 主线程。取消会尽力终止 pip 的进程组/Windows 子进程树，
可能已经部分安装；再次检查并确认新计划后重试。关闭运行中窗口先询问。
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

GUI 中认证代理只在内存保存且输入遮罩。CLI 不接收 argv 里的认证信息；
可在当前进程环境设置 `COAT_DEPENDENCY_PROXY` 后用 `--proxy-mode http`。
日志/计划移除 URL userinfo、query 和常见 token，子进程代理放环境变量而非
命令行；不默认持久保存凭据，凭据不进仓库。其他同用户进程仍可能读取
进程环境，敏感代理账号应采用最小权限。

测速先有界获取 simple/pip 页面，再读取固定 `pip-25.2-py3-none-any.whl`
样本最多 256 KiB；报告实际来源、字节数、完整索引请求延迟和含连接开销的
吞吐。每镜像有总时限，取消/超时都不会挂 GUI。结果只是网络估计，特别是
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

确认中披露官方来源、hash、用户目录和 [CPython 许可](https://docs.python.org/3.13/license.html)。
校验完成前不执行下载文件。正常完整 installer 带 pip/venv，参数按
[官方 Windows 文档](https://docs.python.org/3.13/using/windows.html#installing-without-ui)：
仅用户目录，禁止 PATH、launcher、默认关联、快捷方式；会产生每用户维护/
卸载记录。检测到同一 minor CPython 已注册时阻断，避免 installer 维护已有
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

CLI `--profile runtime`（默认）和GUI“项目依赖配置”使用同一清单：完整
`desktop`为requirements.txt的15项直接运行依赖，不包含pytest、Ruff、mypy
或打包工具。`--profile development`在已选择的运行feature基础上仅增加
pytest/mpmath和requirements-analysis.txt中Ruff/mypy的直接固定项，合计19项。
它不会递归安装analysis-lock、自动打开Mitsuba/Dr.Jit或选择GPU。
CPU/GPU、AI助手、外观渲染和sandbox是另外的显式feature选择。

### 历史封装实验（已停止）

2026-10-14 曾验证 cx_Freeze/PyInstaller 候选方案，随后取消 EXE
封装。两个构建入口现均拒绝执行，即使带 `--test-build` 也不会创建二进制。
CI 仅运行源码检查。保留的 `build_catalog` 和构建依赖清单只供历史实验追溯，
不是运行或开发配置，不会被正常部署安装。

此前 PyInstaller Linux 候选包的 CLI 隔离 smoke 已通过，但没有完成 Windows
二进制和真实 GUI 验收；不得将它作为正式发布包。旧原生依赖、许可清单见
[历史分发记录](dependency-manager-distribution.md)，后续来源变更不能沿用此结论。

源码离线测试覆盖计划、确认、配置隔离、取消、哈希/架构/路径冲突、网络失败、
镜像来源、GUI worker与打包门禁。真实GitHub认证/下载、目标依赖安装和Windows
运行时安装仍需端到端验收。Tk真实渲染/交互、Windows/DPI/任务树终止也需平台验证。

## 从 GitHub 获取本项目

GUI“获取本项目”页默认使用公开仓库 ii33/coating-sim，也可显式选择旧开发源或配置 CNB HTTPS 地址；仅接受新目录。
分支/标签解析后记录完整提交SHA；先获取源码，展示部署计划，经确认后只在
项目.venv安装依赖。沿用依赖页的runtime/development、计算后端，以及镜像代理选项。
CLI使用project-plan与project-deploy；后者读取原计划，不再次拉取代码。
现有Git认证可以复用，本工具不接收或保存凭据。详见
[部署约定](dependency-manager-deployment.md)。
