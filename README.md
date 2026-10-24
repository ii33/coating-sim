# 光学镀膜 · 设计与仿真

中文 | [English](README.en.md)

面向光学薄膜研究与工程开发的桌面工具：将膜系设计、行星式蒸发沉积、膜厚分布、光谱分析和结果资产组织到独立工程中。

**当前为开发快照，不是正式 Release；准确版本见 [version.txt](coat_sim/version.txt)。** 项目尚未完成全面物理标定；仿真和优化结果用于分析、比较与研究，不能替代设备标定、工艺验证或光学测量。应用与独立安装器的版本和验收范围分别记录。

## 能做什么

- **沉积建模**：设计蒸发源、基片、机器、修正板和工艺，计算膜厚分布及均匀性，运行有适用边界的工艺和修正板优化。
- **薄膜光学**：编辑膜系与材料 n/k 数据，计算光谱、分析波段与极值，进行厚度优化及实验性结构搜索。
- **工程与资产**：按工程隔离输入、未保存修改和结果；在统一资产库中浏览资源、编辑材料，并按所属范围回收或恢复符合条件的资产。
- **结果与自动化**：管理计算队列和结果资产，使用几何光追与外观渲染工具；部分功能提供 CLI、MCP 和可选 AI 助手入口。不同入口的能力与权限并不相同，见[入口能力矩阵](docs/entrypoint-capabilities.md)。
- **安装入口**：[独立安装器](https://github.com/ii33/coating-sim-installer)维护源码及依赖部署，应用提供声明性依赖合同。

第一次使用可先看[软件说明书的操作流程](docs/software_manual.html#workflow)、[助手设置](docs/software_manual.html#settings)和[按目标选择算法](docs/software_manual.html#algorithm_selection)。公式、单位、采样、近似与参考来源见[算法图解](docs/algorithms.html)。两份 HTML 都可下载后离线打开；GitHub 文件预览不等同于渲染页面。

从裸片起步的自动合成已接入现有优化页，物理结构与组合段已接入参数生成器；操作见[GUI指南](docs/film-synthesis-gui-guide.md)，接口见[执行契约](docs/film-synthesis-execution-contract.md)、[结构参数](docs/film-physical-structures-contract.md)及[算法图解14t/14u](docs/algorithms.html#film-synthesis-audit-20261020)。默认不设搜索总量预算，支持进度、取消和已审计候选；当前能力范围与未支持项以契约为准。本轮同源GUI验收见[阶段报告](docs/film-synthesis-gui-stage-report-20261020.md)。角度输入、两膜料结构与动态候选行的后续修复见[交互修复验收](docs/film-synthesis-gui-repair-report-20261020.md)。仅n材料的显式无损近似见[未知k策略](docs/film-synthesis-unknown-k-contract.md)及[算法14v](docs/algorithms.html#film-synthesis-unknown-k-20261020)。

2026-10-21：默认高反结构/全局搜索接入及19进程调度修复见[同请求三次确认结果](docs/film-synthesis-hr-results-20261021.md)；[冻结验收协议](docs/film-synthesis-hr-acceptance-20261021.md)与[多类指标矩阵](docs/film-synthesis-benchmark-matrix-20261021.md)分别记录已通过范围和待核验参考，不以单例推断全面达标。 [扩展验证阶段](docs/film-synthesis-extension-results-20261021.md)记录腔/AR薄匹配默认接入、8类24次有限审计通过、真实DE和19worker证据，也明确保留零容差密审计HR失败与困难类别待验证；完整数值及材料/过渡带/制造界在冻结矩阵中。

2026-10-21：真实均匀层超宽带增透结构已接入默认纯AR合成，包括非QW交替层、1/2/3簇与全膜光程预算扫描；见[落地与云端同预算协议](docs/film-synthesis-ar-clusters-stage-20261021.md)及[算法14aa](docs/algorithms.html#film-synthesis-ar-clusters-20261021)。新结构的云端多种子对照尚待执行，不能由本地回归推断性能优于v23。

2026-10-22：默认混合结构广筛/解析批针/全原约束精修与独立膜厚敏感性已接入正式工作流；见[使用入口](docs/film-synthesis-gui-guide.md)、[敏感性边界](docs/film-sensitivity-workflows-contract.md)和[真实验收及未通过范围](docs/film-hybrid-integration-report-20261022.md)。复杂四窗口正式冷启动长测尚未达标，不以研究已知处方重放替代。

Git同提交云端交接入口与CPU命令见[Git交接指南](docs/film-synthesis-git-handoff-20261021.md)。

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
- **GPU 按任务区分。** Intel 核显 DirectML 平面沉积数值已有有限验证，遮板边界仍为实验范围；默认设备不保证绑定 Intel。膜系 XPU 后端 FP64/complex128 已在 sakuya 的 Intel 8086:7D67 上完成限定验证，速度不普遍优于 CPU；优化窗口已提供显式实验路径：固定拓扑差分进化、延迟批量、单条件单角度、前表面半无限基片，检测并选择 UUID 后才使用 XPU；默认 CPU，显式 GPU 失败不回退 CPU；外观没有 Intel GPU 后端，CPU 外观可用。设备弹窗说明支持范围与不可用原因，详见[核显支持与实测限制](docs/intel-integrated-gpu-support.md)。CUDA、DirectML 仍依赖具体环境；DirectML 配置限定 Windows / Python 3.10–3.12。
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
- [固定膜系参考配方](docs/algorithms.html#film-reference-fixed-recipes) · [参考配方契约](docs/film-design-reference-library-contract.md)
- [助手算法与工程知识检索](docs/assistant-knowledge-contract.md) · [工程记忆与研究访问](docs/assistant-memory-research-contract.md)
- [材料数据适用性诊断](docs/material-data-readiness.md) · [ITO 条件化光学数据](data/optical_catalog_sources/rii_ito_minenkov_20261018_v1/README.md)
- [本地 refractiveindex 参考库](docs/refractiveindex-local-contract.md) · [离线浏览与按需导入](docs/refractiveindex-browser-contract.md) · [CLI/MCP](docs/refractiveindex-tools-contract.md)
- [厂商 AGF 目录及空气参考近似](docs/vendor-optical-catalog-contract.md) · [官方参考来源与离线许可](docs/reference-sources.md)
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

[研究资料接入与普通 MCP 服务配置](docs/research-integration-contract.md) · [统一离线资料与助手入口](docs/unified-offline-library-contract.md) · [研究证据格式](docs/algorithms.html#project-research-evidence)

[光谱反演新光学数据集：操作指南与限制](docs/optical-inversion-guide.md) · [算法与非唯一性](docs/algorithms.html#optical-constant-inversion)

[可迁移看板娘工具集：能力、开关与后端迁移](docs/assistant-toolkit-capability-map.md) · [接口契约](docs/assistant-toolkit-contract.md) · [宿主 provider 边界](docs/assistant-toolkit-provider-contract.md)

助手具备独立持久工作区与[精简长期记忆](docs/assistant-memory-contract.md)。[工程 BBS](docs/project-bbs-contract.md)用于长期保存日志、进度和研究/工作报告，支持修订历史与回复，区别于即时对话。讨论模式只开放 BBS 追加评论/回复，其他修改仍受操作模式约束。

当前各路线的GUI、CLI/MCP与研究边界见[算法落地只读清单](docs/film-synthesis-algorithm-rollout-20261020.md)。

[未知 k 显式无损假设阶段与验证](docs/film-synthesis-unknown-k-report-20261020.md)（默认严格、已知吸收保留、真实ZnS原生执行和保存证据）。

[从零合成CPU候选并行契约](docs/film-synthesis-parallel-contract.md)；离线算法图解见14w。

[取消时应用当前最好候选验收报告](docs/film-optimization-cancel-apply-report-20261020.md)；[取消时应用当前最好候选契约](docs/film-optimization-cancel-apply-contract.md)；离线算法图解见 [14x](docs/algorithms.html#film-cancel-best-20261020)。

[膜系优化输入草稿契约](docs/film-optimization-input-draft-contract.md)。

[CPU多核与GPU评估阶段报告](docs/film-synthesis-performance-report-20261020.md)

[从零合成GPU整轮验收报告](docs/film-synthesis-gpu-forward-report-20261020.md)；[实际60层多光谱对照](docs/film-sixty-layer-multispectral-report-20261020.md)；[从零合成GPU前向实施契约](docs/film-synthesis-gpu-forward-contract.md)；离线算法[14y](docs/algorithms.html#film-synthesis-gpu-forward-20261020)。

波段目标支持独立启用平均值与绝对值（逐点），见[当前双约束契约](docs/film-band-criteria-contract.md)及[离线算法 14at](docs/algorithms.html#film-band-criteria-20261024)。新协议明确拒绝旧格式；RMS 权重更新不放宽名义阈值。
