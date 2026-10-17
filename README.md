# 行星盘沉积仿真 · Deposition Sim

中文 | [English](README.en.md)

面向光学薄膜研究与工程开发的桌面工具：将膜系设计、行星式蒸发沉积、膜厚分布、光谱分析和结果资产组织到独立工程中。

**当前为开发快照 `dev.20261016.19`，不是正式 Release。** 项目尚未完成全面物理标定；仿真和优化结果用于分析、比较与研究，不能替代设备标定、工艺验证或光学测量。应用与独立安装器的版本和验收范围分别记录。

## 能做什么

- **沉积建模**：设计蒸发源、基片、机器、修正板和工艺，计算膜厚分布及均匀性，运行有适用边界的工艺和修正板优化。
- **薄膜光学**：编辑膜系与材料 n/k 数据，计算光谱、分析波段与极值，进行厚度优化及实验性结构搜索。
- **工程与资产**：按工程隔离输入、未保存修改和结果；在统一资产库中浏览资源、编辑材料，并按所属范围回收或恢复符合条件的资产。
- **结果与自动化**：管理计算队列和结果资产，使用几何光追与外观渲染工具；部分功能提供 CLI、MCP 和可选 AI 助手入口。不同入口的能力与权限并不相同，见[入口能力矩阵](docs/entrypoint-capabilities.md)。
- **安装入口**：[独立安装器](https://github.com/ii33/coating-sim-installer)维护源码及依赖部署，应用提供声明性依赖合同。

模型、单位、采样、近似与参考来源集中在[算法图解](docs/algorithms.html)。该单文件 HTML 可下载后离线打开；GitHub 文件预览不等同于渲染页面。[软件说明书](docs/software_manual.html)提供更完整的操作说明。

## 快速开始

### 使用独立安装器

请使用 [ii33/coating-sim-installer](https://github.com/ii33/coating-sim-installer) 或其 [CNB 镜像](https://cnb.cool/nonereason/coating-sim-installer)。Windows EXE 下载、安装命令及确认流程统一见独立项目的 [Release](https://github.com/ii33/coating-sim-installer/releases) 和 [说明](https://github.com/ii33/coating-sim-installer/blob/main/README.md)。主项目不再携带安装器副本。

### 已有 Python：运行应用源码

使用完整兼容 Python（推荐 3.12），在源码目录创建独立环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py
```

Linux/macOS 将解释器路径换为 `.venv/bin/python`；Linux 另需发行版的 Qt 系统图形库。外观渲染还需要当前声明在 `requirements-test.txt` 的 Mitsuba/DrJit；独立安装器默认选择 `desktop + appearance`。各平台统一使用项目解释器运行 `launcher.py`；安装器生成的启动脚本和快捷方式也使用此入口。项目侧文件与兼容边界见[安装与依赖合同](docs/installation.md)。

### 完整示例工程

打开 `example/synthetic-demo/project.coatproj` 可加载一套完整、自包含的合成工程，包含材料、膜系、机器、源、基片、修正板、工艺及默认沉积配置。建议先复制示例目录再编辑；[示例操作与验证说明](example/README.md)列出了预期计算结果、优化步骤和低采样的适用边界。该示例不代表实测设备或生产标定。

### 第一个工程

1. 运行 `launcher.py`，新建工程到独立目录，或打开已有的 `.coatproj` 文件。
2. 从膜系设计开始；通过工程导航打开沉积工艺、机器、源、修正板、基片、资产库或计算窗口。工程资源与全局资源分开管理，外部输入经显式导入复制进入工程。
3. 根据任务准备材料和输入，先用小规模 CPU 计算检查结果。光谱与外观受材料光学常数、适用波段及模型假设影响。
4. 显式保存沉积仿真配置。**最近一次成功保存的沉积配置成为下次打开的入口**，并加载其依赖；只保存机器、工艺或膜系不会切换入口。未保存内容的异常恢复需另外审阅确认。
5. 保存后可通过“工程管理…”导出 `.cproject` 工程包，再导入新的独立目录。包包含受管输入，不含运行结果、恢复内容、凭据或全局库，不能替代完整结果备份。详见[工程包说明](docs/project-package-contract.md)。

每个工程在独立进程中运行；再次打开同一工程会激活原会话。关闭启动器不会关闭工程；共享助手菜单的“退出应用”会检查未保存内容并协调退出。最近列表的普通移除只移除记录；回收工程目录是另一个需确认的操作。

## 安装与依赖声明

安装实现、更新、运行时准备及安装包构建统一归属[独立安装器](https://github.com/ii33/coating-sim-installer)和 [CNB 镜像](https://cnb.cool/nonereason/coating-sim-installer)。主项目保留 requirements、锁文件、环境规范及计算设备运行时能力；alpha.1 消费兼容边界见[合同](docs/installation.md)。

## 平台与计算边界

- **CPU 是默认起点。** 不同算法开放的后端不同，当前桌面膜系优化工作路径为 CPU。
- **GPU 按任务区分。** Intel 核显 DirectML 平面沉积数值已有有限验证，遮板边界仍为实验范围；默认设备不保证绑定 Intel。膜系 XPU 批评估超时，桌面优化保持 CPU；外观没有 Intel GPU 后端，CPU 外观可用。设备弹窗说明支持范围与不可用原因，详见[核显支持与实测限制](docs/intel-integrated-gpu-support.md)。CUDA、DirectML 仍依赖具体环境；DirectML 配置限定 Windows / Python 3.10–3.12。
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

- [独立安装器与依赖合同](docs/installation.md) · [已有运行时 ZIP 指南](docs/portable-bundle.md) · [工程与结果资产包](docs/portable-assets.md)

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

安装器发行记录及第三方运行时许可统一见[独立安装器](https://github.com/ii33/coating-sim-installer)。

[项目目录与历史工具清理说明](docs/development/root-layout.md)。

[研究资料接入与普通 MCP 服务配置](docs/research-integration-contract.md) · [研究证据格式](docs/algorithms.html#project-research-evidence)

[光谱反演新光学数据集：操作指南与限制](docs/optical-inversion-guide.md) · [算法与非唯一性](docs/algorithms.html#optical-constant-inversion)
