> 历史验证记录：目标进展交接现已移除；下列命令/文件名仅记录当时证据，不是当前可执行功能。

# 算法 GUI 适配与本地主机验证（2026-10-25）

基线：`ef38fd9804d719a10c7a9ee25fef0456d2c9d378`。本阶段只在助手自有 Linux 主机实现和测试，没有操作 sakuya、运行 GPU、制作 Release 或更改用户工程文件。版本仍为当前开发快照。本报告不构成 Windows 原生或生产工艺验证。

## 已接入

- 原“算法 / 从零合成 / 统一混合”增加默认关闭的目标进展交接。四项显式原文输入，两个相对下限为无量纲比例，0.001=0.1%；无默认阈值。非法输入保留原文并阻止开始/保存/关闭，不静默使用旧值。开关与草稿配置往返，实际 policy 仍用现有 v4 冻结字段。legacy 不静默忽略已启用策略。
- 既有优化窗口增加“工艺薄层修复…”主题对话框。明确勾选真实前层 ID，参考 nm 与每路评价/迭代/秒数初始为空。A 保留增厚，B 删除合并，不推断制造硬下限，不自动选择或应用。
- 新 `film_manufacturing_repair` 仅使用已有本地 CPU scheduler。冻结来源、原目标和材料，后台完成原训练/独立审计与两路比较；保留完整证据与不同工作单位的账本，不在 UI 上计算光谱或在取消后补做物理 replay。
- 原/A/B 分别显示层数、最薄层、总厚度 D、逐波段固定网格诊断均值、采样 min/max、最大违反及训练/独立审计 nominal 状态。整体完成不等于两路都有审计，更不等于通过原目标。
- 完整审计路线才可显式选择并应用。未通过原目标时保留警示，应用前重新核对完整原目标、raw/QW 来源、冻结物理指纹、实时材料内容、来源修订/未提交编辑及项目代次；一次应用形成一笔可撤销、未保存修改。普通完成、取消和关闭都不会自动应用。
- 完整 JSON 导出、覆盖确认与专门只读队列历史查看已接入。历史窗口无来源/应用上下文，调用共享几何管理器后显示并复用窗口；不退回 generic 仿真资产或额外物理复算。

## 验证证据

以下运行有重叠，不能将各数相加当作独立测试总量。

1. 新目标进展 GUI：16 passed，12.14 s。默认关闭、空白和非法草稿、真实 Qt 输入、配置往返、legacy 拒绝、关闭保留旧候选，以及实际本地 scheduler 的 v4 spec/result 同值冻结。
2. 制造 GUI/source 最终定向：18 passed，20.74 s；新增历史几何钩子后独立 source/queue/history 4 passed。真实来源编辑器验证应用前零修改、明确应用一次、一步撤销恢复、同 ID 材料内容漂移拒绝；readiness 属性不反复展开网格或读取目录。
3. 广覆盖 GUI 定向：127 passed，92.52 s。包括新界面及既有 current/best 实时曲线、终态曲线、控制 RPC、优化布局/恢复、随机化、总厚度缩减与敏感性工作流。后续 readiness 与几何的小改动另有上项最终定向覆盖。
4. 制造生产作业适配器：29 passed；包含真实隔离 scheduler 子进程、完整证据和请求身份、10001 点物理块取消、源审计取消保留训练证据、A 首个完整试探后取消不审计/不启动 B、mean-only 资源准入、显式外部所有者、alias-expanded 输出存活范围，以及数值阶段可容纳但组合输出超 130 MiB 时第一物理调用前拒绝。
5. 作业/worker 广覆盖：118 passed，37.63 s；另有 result capability、远端拒绝、不可变输入和独立敏感性等回归 146 passed、1 skipped、9 subtests passed。新数值边界由独立复核另跑 11 项纯所有权/容器/分块/输出上限用例，全过。
6. 新 UI/helper/test Python 文件直接 Ruff 与 mypy 通过；整个仓库的 baseline-aware 静态检查结果见下方最终 gate。

真实 Qt 输入与截图使用 PySide6 6.12.0 的离屏软件后端，覆盖明/暗主题、125% 缩放、530↔1100 逻辑宽度、复选、输入、运行、取消、关闭和重开。未执行完整仓库 all suite 或用户原生 Windows 实机验收。

已有随机化 IPC 用例最初在只读默认 application-state 路径下无法接纳。使用标准私有 `XDG_STATE_HOME` 和受支持的 `COAT_ASSISTANT_RUNTIME_ROOT`（均在测试 `/tmp` 目录）后，相同未改动用例通过。没有为此添加产品 fallback、迁移或工程重置。

可复现的定向命令：

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QSG_RHI_BACKEND=software \
XDG_STATE_HOME=/tmp/coating-gui-state COAT_ASSISTANT_RUNTIME_ROOT=/tmp/coating-gui-runtime \
python -m pytest -p tools.pytest_qt_lifecycle \
  tests/test_film_objective_progress_gui.py \
  tests/test_film_manufacturing_quick_controller.py \
  tests/test_film_manufacturing_gui_integration.py -q
python tools/run_static_analysis.py check --output artifacts/static-analysis/algorithm-gui
```

## 限制

资源预留是保守项目模型，不是 OS 内存隔离。一次完整独立审计在候选调用配额中计一次，其内部 evaluator/kernel/点工作另报；中断调用可能已有物理块，账本显式保留 partial 标记，不伪装成精确完成总成本。有限审计不是连续光谱证书。

本阶段验证接口、生命周期、资源与事实呈现，不证明目标进展策略更快、更优或工艺修复能满足实际制造要求；不将某路线 nominal 失败或时间/资源停止写成成功。

输入和所有权边界见 [GUI 契约](film-algorithm-gui-contract-20261025.md)、[Qt Quick 界面契约](film-manufacturing-quick-contract.md)、[本地作业契约](film-manufacturing-job-contract.md) 与 [算法图解 14ax](algorithms.html#film-manufacturing-pair-20261025)。

最终同源新 GUI 定向 gate：35 passed in 32.46 s，覆盖全部三个新增 GUI 测试模块（含最终 history geometry、readiness/action fence 及实际 IPC）。

最终 repository baseline-aware 静态 gate：1160 个源文件，新增诊断 0、fatal syntax 0，现有 baseline debt 未变更（mypy 3946 / qmllint 1655 / Ruff 1414 为既有诊断总数，不宣称零历史问题）。没有更新或放宽 baseline。

发布前整数配额边界复审修复：两项整数配额现按 Decimal 原文判定正整数并精确转换，拒绝 `2.0000000000000001` 等近整数小数，完整保留 `9007199254740993` 和合法整数科学计数法；原有限正数输入域与配额停止含义不变。该跟进的制造专项 43 passed，全部新增 GUI 模块最终同源 63 passed in 53.65 s；repository 静态再次为 1160 源文件、0 新诊断、0 fatal，baseline 未改变。
