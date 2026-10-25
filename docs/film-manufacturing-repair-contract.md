# 显式选层的成对制造修复算子契约

状态：独立 opt-in 算子，不接入 `run_hybrid` 自动阶段，不改变 v4 合成作业协议。显式当前设计 GUI 使用独立本地 CPU 作业，见 [GUI 契约](film-algorithm-gui-contract-20261025.md) 和 [调度器契约](film-manufacturing-job-contract.md)；默认不自动选择或应用。

## 所有权与输入

`ManufacturingRepairRequest` 指定现有冻结 `FilmSynthesisRequest`、源设计指纹、明确选中的缺陷层 ID、正有限 `reference_nm`、两路相同的优化候选调用/迭代/时间上限。指纹复用 `batch_needle_parent_fingerprint`，绑定真实冻结材料内容、几何、环境和有序层 ID。没有自动薄层阈值，没有固定 25 nm 或层数。输入源快照必须有完整原始目标训练证据，可附同设计的完整独立审计证据；它不是重新计算证明。

A 仅将选中的、原厚度小于 reference 的层升到 reference，其余行不改。B 仅删除这些正厚层，然后调用既有 cleanup 合并相邻且材料 ID 和光学数据集 ID 均相同的层。删除不等价于原膜系；其后的同引用合并才是代数整理。层序始终基片向外，基片不参与合并。每个保留行使用 `max(原生严格下限, min(该路线初始厚度, reference))`，是显式保守修复 guard，不是把近似制造参考解释成全局硬阈值。不能靠归一化或额外零层绕过实际 nm 边界。

## 调用与资源

算子按 A、B 固定顺序执行，各至多一次起始完整 FJ、N 次优化完整 FJ、一次完整审计。N 是调用上限，不承诺两路完成调用数、层点工作或 wall 相同。起始刷新、优化、审计分别记账。完整候选/审计调用不是物理 kernel 调用的等价单位；调用者的 evaluator/auditor/admission 仍负责实际 F/FJ、点数、层点数、块、共享预算和工作线程资源。

`evaluate` 和 `audit` 接收 live `cancel_check`，必须在各物理块前检查；`admit` 在每次完整候选或整次审计之前运行，须先为整次工作保留容量。审计可含多个实际物理调用，其 `audit_working_set_bytes` 必须声明完整审计工作集，包含审计器存活数组/记录、canonical 验证及最多三个完整返回证据的临时投影，不能填单块峰值代替所有同时存活数组。外部已有 archive/worker/调用者保留数组由 `external_retained_bytes` 声明，排除算子已经按对象身份计入的 request/source；未知外部所有者不能假设为零。算子在冻结内存策略下合并外部存活量、独立且去重的本地完整证据快照以及当前求解器/审计工作集，检查后才分派。数值工作集是保守项目模型，不是 OS 硬内存隔离保证。

每路计时包含刷新、优化和审计。到时不补做审计；取消或共享预算停止后不为返回值再计算。路线 A 到其独立时间上限可继续有自身配额的 B，但全局取消/共享预算/资源停止立即终止整个算子。推荐外部共享停止使用 `ManufacturingRepairStop` 或既有 `_StructureCancelled`；外部检查/求值/准入中的 `SynthesisStageBudget`、`SynthesisResourceGuard` 分别转换为整个算子的 shared_budget/resource_guard，不能被精修器局部捕获后继续审计。观察器抛出的同名异常仍原样传播，而不是静默停止成功。

## 完整证据和返回

每个完整有效 trial 在中断检查之前提交本地 current/strict-best 快照，再调用已有 archive 和 progress 回调，保持设计和完整原始条件/覆盖的精确关联。快照只投影已有公开 canonical score 字段，不保留额外导数/调试负载。archive 接收隔离的完整 design/score/原始 coverage 响应投影，不含 Jacobian；progress、admission、audit estimate 均收到隔离副本。当前求解器继续使用原来的稠密状态，不改数学公式。观察者异常不吞掉。源、A 起点、A 冠军、B 起点、B 冠军为独立角色，有界保留；审计仅附到精确同设计的冠军。

源 canonical 校验和 request/evidence 复制之前也做组合资源准入。若已取消或资源不足以执行这一步，返回 untouched 源参考、空路线及 source_validated=false，不声称本次验证了该输入。正常路径返回完整源参考、每路起点/当前点/严格训练冠军/审计、明确停止原因、调用账本、边界和删除合并元数据。调用账本区分已准入与完成；部分块的实际成本由调用者账本保留。只有完整审计的修复候选参与 `best_repaired` 比较；它允许原始目标仍失败。排名先检查原始训练与独立审计的 nominal AND，再使用既有原审计最大违规/RMS 顺序，不加层数成本。只有原源具有完整审计、且新候选的这一完整审计 rank 更好，才可成为 `spectral_incumbent`。原始 nominal 判定与 solver tolerance 分离；绝不因修复制造缺陷就把光谱较差候选覆盖为原光谱冠军。此算子不承诺每波段/均值/逐点指标同时占优，不修改用户目标。

## 既有 minimax 的窄扩展

`refine_synthesis_full_minimax(..., lower_bounds_nm=...)` 可选精确层 ID 映射；必须包含且仅包含当前唯一 ID，有限正 nm 下限，不得放松原生界限。初始真实厚度必须满足。省略或 None 走原标量路径；无新 scorer，无目标替换，无隐藏余量。

## 当前资源模型与小样本验证协议

当前为现有完整 Python 证据路径，不是未合并的 packed 研究实现。设 P 为原目标波长×角度样本数、Q 为原 signed 条件数、n=max(1,层数)，求解器工作集代理为 `128 MiB + 384P(n+1) + 9·256P + 9·1024Q + 256Q(n+1) + 128n²` bytes，再加实际身份去重的本地完整快照和外部所有者。包含六个可能不同的响应/J、完整 Python 条件/覆盖及三个传入/观察者投影；mean-only 的覆盖存储同样随 P 增长，不能只按 Q 估计。源校验先按审计加密倍数和输入实际存储保留空间。此为保守项目模型，不保证大型用户网格可在 512 MiB 运行，也不把 512 MiB 变成产品固定限制。

预声明物理验证：AR/HR/带通/混合 R/T × mean-only/pointwise-only/both × seed 0/1，共 24 个合成材料 TMM 小夹具。每波段显式 5 点、0°/15°、非偏振、独立审计 multiplier=2；不是用户网格降采样或精度声明。非缺陷 QW 厚度按 seed 在 ±10% 扰动，命名缺陷 2 nm，参考 8 nm，严格下限 1 nm、上限 300 nm、无总厚度/层数人为上限。源刷新+独立审计限 5 s，两路各至多 1+6 FJ 与一次完整审计且各限 5 s（含审计），整 cell 15 s，显式测试资源 cap 512 MiB；不足时保留停止证据，不调整阈值。物理 kernel 的 F/FJ、波长点、层点单独统计。低配额或 SLSQP 停止不意味着达标；源光谱冠军仍保留。协议为本项目约定，验证有界算子行为而非跨膜系优化优越性。
