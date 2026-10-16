# 行星盘沉积仿真 · Deposition Sim

中文 | [English](README.en.md)

面向光学薄膜研究与工程开发的桌面工具：将膜系设计、行星式蒸发沉积、膜厚分布、光谱分析和结果资产组织到独立工程中。

**当前为开发快照 `dev.20261016.14`，不是正式 Release。** 项目尚未完成全面物理标定；仿真和优化结果用于分析、比较与研究，不能替代设备标定、工艺验证或光学测量。目前从源码运行，不提供已完成发行验收的安装包或依赖管理器 EXE。

## 能做什么

- **沉积建模**：设计蒸发源、基片、机器、修正板和工艺，计算膜厚分布及均匀性，运行有适用边界的工艺和修正板优化。
- **薄膜光学**：编辑膜系与材料 n/k 数据，计算光谱、分析波段与极值，进行厚度优化及实验性结构搜索。
- **工程与资产**：按工程隔离输入、未保存修改和结果；在统一资产库中浏览资源、编辑材料，并按所属范围回收或恢复符合条件的资产。
- **结果与自动化**：管理计算队列和结果资产，使用几何光追与外观渲染工具；部分功能提供 CLI、MCP 和可选 AI 助手入口。不同入口的能力与权限并不相同，见[入口能力矩阵](docs/entrypoint-capabilities.md)。
- **依赖管理**：纯终端交互式在线安装器与标准库高级 CLI，检查 Python 和计算环境、审阅安装计划、选择源码和镜像，将完整用户依赖放入隔离虚拟环境。

模型、单位、采样、近似与参考来源集中在[算法图解](docs/algorithms.html)。该单文件 HTML 可下载后离线打开；GitHub 文件预览不等同于渲染页面。[软件说明书](docs/software_manual.html)提供更完整的操作说明。

## 快速开始

先下载并解压本仓库源码，或使用 Git 克隆本仓库，然后在源码根目录执行以下命令。推荐 **Python 3.12**，首次安装依赖需要网络。建议先使用 CPU；可选计算后端须单独安装和验证。

### 推荐：交互式在线安装

Windows 双击 `install.bat`；Linux/macOS 在终端运行 `sh install.sh`。按提示选择安装根目录、源码（默认 [CNB](https://cnb.cool/nonereason/coating-sim)，也可选 [GitHub](https://github.com/ii33/coating-sim)）、镜像加速及是否安装开发环境（默认否）。安装器自动检查 Python/计算环境，安装完整用户依赖 `desktop + appearance`；开发模式额外加入测试与静态检查工具。镜像加速只影响 PyPI 包源，不改 Git 仓库地址。

新安装布局为 `<安装根目录>/.runtime/python`、`.runtime/git`、`app/` 源码和 `app/.venv` 依赖。选择已有合法工程时只处理该工程依赖，不覆盖源码或更换现有 venv 解释器。未知内容、损坏环境或不兼容后端会明确报错。

Windows 不要求预装 Python、Git 或 PowerShell 7；引导使用系统 Windows PowerShell 5.1，安全策略阻止脚本时不会绕过。官方 Python 安装器可能在安装目录外写入维护缓存和每用户注册/卸载记录，因此这不是完全便携、零系统痕迹的安装。当前新交互流程仍待原生端到端验收，不是已验收 Release。详细边界见[安装器指南](docs/dependency-manager.md)。

安装结束后对实际项目 `.venv` 做有界 GPU 数值探测；依赖安装成功与 GPU 可用分别报告，探测失败不会自动修改驱动或重装。

### Windows：已有 Python

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py
```

推荐双击 `install.bat` 使用终端交互式在线安装器；已有合用 Python 时可运行 `python dependency_manager.py`。安装器自身不需要第三方 Python 包，不启动管理器 GUI。

**没有 Python？** Windows 引导脚本会尝试从官方来源向所选安装目录部署包含 pip 和 venv 的完整 CPython。此路径需要网络并受系统前提约束；无 Python 的干净 Windows 首次安装仍未完成端到端实机验收。受管理 Python 候选与推荐的 Python 3.12 不完全相同，DirectML 等后端还有限制。请先阅读[引导说明](docs/dependency-manager-bootstrap.md)，不要把源码检查通过当作安装成功保证。

PowerShell 7+ 开发环境可使用 `start-dev.bat` / `start-dev.ps1`：优先项目 `.venv`，其次 `.conda`，不回退系统 Python、不安装依赖。`start-dev.bat -Check` 只检查依赖。详见[启动入口](docs/project-launcher-entry.md)。

### Linux：已有 Python

交互安装入口为 `sh install.sh`，安装器自身不需要第三方 Python 包或 Tk。当前 Linux/macOS 未实现无 Python 的安全自动引导；需先按官方渠道安装兼容完整 Python（含 venv/pip），Linux 另需发行版要求的 Qt 图形库。不能据此承诺所有平台从零自动安装。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python launcher.py
```

Qt 平台插件问题可对照 [Qt for Linux 系统要求](https://doc.qt.io/qt-6/linux-requirements.html)。无图形会话的测试环境不能代替原生桌面交互验收。macOS 尚未完成原生端到端验收，不在此承诺可用。

### 完整示例工程

打开 `example/synthetic-demo/project.coatproj` 可加载一套完整、自包含的合成工程，包含材料、膜系、机器、源、基片、修正板、工艺及默认沉积配置。建议先复制示例目录再编辑；[示例操作与验证说明](example/README.md)列出了预期计算结果、优化步骤和低采样的适用边界。该示例不代表实测设备或生产标定。

### 第一个工程

1. 运行 `launcher.py`，新建工程到独立目录，或打开已有的 `.coatproj` 文件。
2. 从膜系设计开始；通过工程导航打开沉积工艺、机器、源、修正板、基片、资产库或计算窗口。工程资源与全局资源分开管理，外部输入经显式导入复制进入工程。
3. 根据任务准备材料和输入，先用小规模 CPU 计算检查结果。光谱与外观受材料光学常数、适用波段及模型假设影响。
4. 显式保存沉积仿真配置。**最近一次成功保存的沉积配置成为下次打开的入口**，并加载其依赖；只保存机器、工艺或膜系不会切换入口。未保存内容的异常恢复需另外审阅确认。
5. 保存后可通过“工程管理…”导出 `.cproject` 工程包，再导入新的独立目录。包包含受管输入，不含运行结果、恢复内容、凭据或全局库，不能替代完整结果备份。详见[工程包说明](docs/project-package-contract.md)。

每个工程在独立进程中运行；再次打开同一工程会激活原会话。关闭启动器不会关闭工程；共享助手菜单的“退出应用”会检查未保存内容并协调退出。最近列表的普通移除只移除记录；回收工程目录是另一个需确认的操作。

## 依赖管理器

完整说明见[依赖管理器指南](docs/dependency-manager.md)。已有 Python 时：

```sh
python dependency_manager.py
python dependency_manager.py inspect --groups desktop --json
python dependency_manager.py benchmark --mirrors pypi,tuna,ustc --proxy-mode none --json
python dependency_manager.py plan --groups desktop --profile runtime --action install --python /path/to/python3.12 --proxy-mode env --json
# 审阅计划，替换解释器路径及返回的 PLAN_ID，再执行：
python dependency_manager.py install --groups desktop --profile runtime --python /path/to/python3.12 --proxy-mode env --plan-id PLAN_ID --yes --json
```

`/path/to/python3.12` 是已有解释器的示例绝对路径，Windows 请改为实际 `.exe` 路径。默认安装目标为项目 `.venv`。`runtime` 只选择运行依赖，`development` 追加开发工具；外观渲染等可选组须显式选择。计划参数、目标或依赖改变后需重新生成计划；`--yes` 不能代替匹配的 `plan_id`。

镜像测速只是网络估计。代理选项只作用于本次工具进程，不修改系统代理或全局 pip 配置；不要把认证信息写进命令行或提交到仓库。虚拟环境用于依赖隔离，不是安全沙箱。

### 版本检查与在线获取源码

本地检查默认不联网；显式远端检查才连接所选来源。它报告版本、commit、工作树和环境信息，不自动更新正在使用的安装。

```sh
python dependency_manager.py project-check --project . --json
python dependency_manager.py project-check --project . --check-remote --source github --ref main --json
python dependency_manager.py project-plan --deployment-dir ../coating-sim-checkout --source github --ref main --python /path/to/python3.12 --profile runtime --json
# 审阅完整 commit、目录与依赖计划后，使用上一步的部署 PLAN_ID：
python dependency_manager.py project-deploy --deployment-dir ../coating-sim-checkout --plan-id PLAN_ID --yes --json
```

公开仓库为 [ii33/coating-sim](https://github.com/ii33/coating-sim)，Git 来源为 `https://github.com/ii33/coating-sim.git`。交互安装默认使用 CNB：`https://cnb.cool/nonereason/coating-sim`，也可选择 GitHub。高级 CLI 使用 CNB 时指定 `--source cnb --repository-url https://cnb.cool/nonereason/coating-sim`；上面的 CLI 示例的 GitHub 选择不改变交互默认值。

部署只接受新目录，将依赖装入该 checkout 的 `.venv`，不覆盖现用安装、不自动启动应用，也不迁移用户工程。源码获取需要 Git；Windows 可通过下述独立确认步骤安装私有 MinGit，不能据此承诺任意干净电脑一键成功。完整流程与验收限制见[源码部署指南](docs/dependency-manager-deployment.md)。

### Windows 没有 Git

交互安装会在缺少 Git 时展示私有 Git 计划并请求确认；高级 CLI 可先审阅再执行：

```sh
python dependency_manager.py git-plan --json
python dependency_manager.py git-install --plan-id PLAN_ID --yes --json
```

工具从官方 Git for Windows 来源下载固定版本 MinGit，校验 SHA-256 后放入所选安装根目录下的 `.runtime/git`，不修改系统 PATH。默认目录安装成功后会被源码部署自动发现；自选私有目录在两条命令中使用相同的 `--git-dir`，随后用 `--git <目录>/cmd/git.exe` 明确选择。下载的 Git 及所带组件保留各自许可证，不由项目 MIT 重新许可。

Windows 从零流程为：启动 `install.bat` → 选择安装根目录并审阅 Python 引导 → 选择源码、镜像和是否开发环境 → 审阅需要的私有 Git 安装 → 获取源码并审阅完整依赖部署。最终确认计划同时创建 `app/launch-coating.cmd`（或 `.sh`），不覆盖同名文件，不默认创建桌面快捷方式。每步检查结果，不自动跨过确认；Linux 仍需预先提供 Python、Git 和系统图形库。离线自动化测试不能替代无 Python / 无 Git Windows 实机的下载、安装、快捷方式及启动验收。

### 创建启动脚本与快捷方式

在部署并检查项目 `.venv` 后，可生成只绑定该项目环境的启动入口：

```sh
python dependency_manager.py launch-plan --project . --shortcut none --json
# 审阅文件名和目标，使用本次启动入口计划的 PLAN_ID：
python dependency_manager.py launch-create --project . --shortcut none --plan-id PLAN_ID --yes --json
```

默认生成项目根目录的 `Coating Simulator.cmd`（Windows）或 `Coating Simulator.sh`（Linux）。Windows 可将两条命令中的 `none` 改为 `desktop` 或 `start-menu`，显式创建使用项目图标的快捷方式；自选目录用 `custom --shortcut-dir <已有目录>`。名称可用 `--launch-name` 指定。已有同名文件不会被覆盖，也不会自动启动程序、注册开机启动或回退系统 Python。原生 Windows 快捷方式仍需在目标机器验证。

## 平台与计算边界

- **CPU 是默认起点。** 不同算法开放的后端不同，当前桌面膜系优化工作路径为 CPU。
- **GPU 不是通用开关。** CUDA、DirectML 依赖操作系统、Python、驱动、设备与具体任务；安装成功或设备被探测到不等于数值验收通过。DirectML 配置限定 Windows / Python 3.10–3.12。
- **外观 CUDA 仍是研究预览。** 已有特定 NVIDIA 硬件上的低分辨率流程验证，但不能外推为所有设备或完整分辨率的性能、颜色准确度保证。该外观后端未实现 AMD / Intel GPU 支持。
- **原生验证与自动化测试分开。** Windows 原生冒烟、Linux 离屏测试、CI、网络安装和物理标定各有不同范围。干净系统首次安装、跨平台/驱动覆盖与完整在线部署仍需单独验收。

进一步阅读：[计算设备选择](docs/compute-host-selection.md)、[GPU 外观验证边界](docs/gpu-product-integration-evidence-20261013.md)、[测试说明](docs/testing.md)。历史验收文档描述当时的快照，不自动适用于后续修改。

## 开发与贡献

欢迎可复现的问题报告、文档改进与有测试的修复。报告请附版本或 commit、操作系统、Python/后端版本、最小复现步骤和脱敏日志；不要上传私人设计、凭据或完整运行目录。

安装开发/测试依赖后，使用项目测试入口：

```sh
python -m pip install -r requirements-test.txt
python tools/run_tests.py quick
python tools/run_tests.py all
```

完整收集使用 `all --collect-only`；算法、契约和 Qt 分层测试，以及需要显式启用的真实 GPU/远程测试，见[测试说明](docs/testing.md)。静态检查另见[静态分析指南](docs/static-analysis.md)。

算法修改须同时更新可离线阅读的 `docs/algorithms.html`，说明输入输出、单位、公式、图解、实现位置、近似和可靠来源。新增或跨模块 UI 行为先记录状态所有者及失败/取消边界，并补充回归测试；具体要求见 [AGENTS.md](AGENTS.md)。

安全问题请勿在公开 Issue 中粘贴可利用细节、密钥或私有数据。若仓库启用了 GitHub 私密漏洞报告，请使用该入口；否则先向维护者询问私密报告渠道，不要公开敏感附件。

## 文档导航

- [算法图解与参考来源](docs/algorithms.html) · [软件说明书](docs/software_manual.html)
- [膜系光谱仿真](docs/film-simulation-quick.md) · [优化方法](docs/film-optimization-methods.md)
- [统一资产与材料编辑](docs/unified-asset-library-contract.md) · [最近保存配置策略](docs/project-recent-configuration-contract.md)
- [CLI / MCP / 助手能力矩阵](docs/entrypoint-capabilities.md) · [MCP 接入](docs/mcp.md)
- [工作流](docs/workflows.md) · [更新日志](docs/CHANGELOG.md)

## 作者与许可

作者与维护者：**ii33**。本项目自有代码采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 ii33。

第三方内容单独适用各自许可，不由本项目 MIT 声明重新许可：

- 随附 refractiveindex.info 光学数据快照保留 [CC0 1.0](data/optical_catalog_sources/sourced_optical_constants_20261006_v1/refractiveindex_LICENSE) 及各数据来源记录。
- CIE 光源与观察者数据及注明的改编数据采用 CC BY-SA 4.0，须保留归属、许可和修改说明；详见[数据声明](data/film_design/cie-illuminants-20261006/NOTICE_DATA.md)。此许可不扩展到整个软件或全部 CIE 出版物。
- 随附 Noto CJK 字体采用 [SIL OFL 1.1](coat_sim/ui/assets/fonts/noto-cjk/LICENSE)。Python、Qt 及其他依赖仍适用各自许可证。
- 图标与插画按各自出处和权利声明处理，见[素材记录](docs/artwork-provenance.md)；公开纳入许可不等于已确认其来源或授予额外再分发权。

历史[封装记录](docs/dependency-manager-distribution.md)不代表当前存在已验收发行包。
