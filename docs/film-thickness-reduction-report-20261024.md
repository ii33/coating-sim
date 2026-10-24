# 固定拓扑总厚度缩减：原语恢复与验证（2026-10-24）

基线：`281ce5f018609d3c493c23176119750fa6acfc50`。原语里程碑仅助手主机；未推送远端。当时未接线；后续默认接线见本文末节。

## 已实现边界

- `reduce_total_thickness` 固定材料、层序、层数和 ID，以原完整均值/逐点约束求总物理厚度的局部下降。只有本成本算子要求名义可行起点；不限制光谱寻优或灵敏度优化入口。
- 采用原规范化约束与解析 Jacobian；不增余量，不接受 epsilon 违规，不用 solver tolerance 替代严格门限。
- 显式 `D <= D_initial` 是不排除任何更优解的初始成本子水平集，不修改用户制造上限。
- 缓存仅一个点；原候选不变；父预算/取消/归档/资源异常原样传播；最多 4 次 nextafter 加 8 次二分重算。
- 原语输出只代表训练网格通过，最终候选仍须父任务原独立审计。

## 实测矩阵

真实生产 TMM + 解析厚度导数，合成、无色散测试材料。每个波段 9 点，0°/15°、非偏振；每次最多 24 次 SLSQP 迭代。独立审计为现有 dense/offset/extremum 路径，默认采样倍率 4。层数前后相同。耗时不是受控性能基准。

| 类别 | 活动指标 | 层数 | 初始 D / nm | 最终 D / nm | 减薄 / nm | 父光学调用 | 原训练 | 独立密网格 | 求解状态 |
|---|---|---:|---:|---:|---:|---:|---|---|---|
| HR | mean | 12 | 1048.609679 | 891.088198 | 157.521481 | 89 | 通过 | 通过 | iteration_limit |
| HR | pointwise | 12 | 1048.609679 | 901.464782 | 147.144897 | 53 | 通过 | 通过 | solver_failure |
| HR | both | 12 | 1048.609679 | 901.464782 | 147.144897 | 51 | 通过 | 通过 | solver_failure |
| AR | mean | 1 | 112.268280 | 77.242395 | 35.025885 | 21 | 通过 | 不通过 | solver_converged |
| AR | pointwise | 1 | 112.268280 | 81.488630 | 30.779649 | 6 | 通过 | 通过 | solver_converged |
| AR | both | 1 | 112.268280 | 81.488630 | 30.779649 | 6 | 通过 | 通过 | solver_converged |
| mixed_RT | mean | 1 | 62.500000 | 21.140420 | 41.359580 | 19 | 通过 | 不通过 | solver_converged |
| mixed_RT | pointwise | 1 | 62.500000 | 22.166595 | 40.333405 | 22 | 通过 | 通过 | solver_converged |
| mixed_RT | both | 1 | 62.500000 | 22.166595 | 40.333405 | 22 | 通过 | 通过 | solver_converged |

9/9 保持原训练网格通过；7/9 通过独立密网格。AR mean 的密网格违规为 4.134002285841043e-7，mixed R/T mean 为 3.282870277759864e-6（归一化值）。两项不算合格减薄，不能替换已有审计冠军。它们揭示粗/密完整波段积分差异，不是添加安全余量的依据。

HR 的 solver_failure/iteration_limit 不等于候选失败：返回的是此前实际通过的更薄候选；不能将此报告为求解收敛，更不证明最优。

## 无成本子水平集对照与零改善证据

对照只在 scipy 调用包装器中移除最后一行 D <= D_initial 约束，光学目标、材料、起点、迭代数完全相同。混合 R/T 三种活动指标均从 62.5 nm 起步，求解器分别收敛到 141.105804/148.040701/148.040701 nm 的另一干涉支路；安全回退均返回原 62.5 nm，减薄 0。加子水平集后分别返回 21.140420/22.166595/22.166595 nm；前一项训练通过但密网格不通过。

这不是所有工作量都改善的证据：HR both 的调用从无子水平集 19 增至 51；mixed pointwise/both 从 14 增至 22。此算子仍需父调度限制，而非任意追加计算。完整 18 行对照含调用、耗时、审计数量及状态：`benchmarks/film_synthesis/20261024/thickness-reduction-smoke.json`。

## 验证与复现

- 新模块 39 项测试，包括 9 个跨类真实 TMM 算例，原训练网格另经独立直接前向重算；另有密网格拒绝的显式测试。
- 合并 canonical/strict/minimax/refinement 定向回归：185 passed。不是全仓库测试；无 GUI、安装器或平台验收。
- Ruff 检查新模块/测试通过；HTML 算法目录和本节同步。
- 测试命令：`python -m pytest tests/test_film_synthesis_thickness.py tests/test_film_synthesis_band_criteria.py tests/test_film_synthesis_refinement.py tests/test_film_synthesis_multispectral_minimax.py tests/test_film_synthesis_hr_minimax.py tests/test_film_synthesis_nominal_acceptance.py tests/test_film_synthesis_strict_bounds.py -q --disable-warnings`。
- 表格复现：在仓库根运行 `PYTHONPATH=. python benchmarks/film_synthesis/20261024/record_thickness_reduction_smoke.py`。脚本需要源码测试夹具；不用于安装版。

## 原语里程碑的精确集成建议（后续实施见末节）

1. 在 `run_hybrid` 的各局部 helper 附近增加一个有界成本阶段，使用 `hybrid_evaluate`、`archive`、`stage_check` 和额外包含当前档案的 `resource_check`；例如单次 `begin_slice("nominal_thickness_reduction", .15, 32)`、`max_iterations=16`。数量是调度量子而非物理层数上限。保留至少一次原独立审计的现有预算预留。
2. 首次入口应放在 `run_hybrid` 的 `while not targets_verified()` 正常结束之后、函数返回前，且仅在仍有预算且已有非空名义可行/独立审计冠军时准入。这样既覆盖 engineering-prior 的早成功 break，又覆盖末尾 minimax 成功；不能依赖下一轮（成功后不会有），也不能挂到通用 `terminal_hybrid_cleanup`（该路径在预算/错误结束也会被调用）。裸基片成功无需此成本算子。
3. 源候选首先取 `audited_best[0]` 并只读保存其匹配审计；可进一步取固定上限的同层数可行档案候选，但不为本首版扩大搜索。父 archive 可记完整训练候选；最终输出仍只由 `audit_archive` 的 both_nominal 排序选择，绝不把训练最薄直接写为审计冠军。
4. 阶段正常得到改进点后，结束训练切片，再在保留预算内调用 `audit_archive(reduced, allow_feedback_repair=False)`。审计失败保留原冠军，并记录实际数值而非整体成功；任何取消/截止后都不补做光学求值。阶段预算/资源异常由父级记为 skip/partial，不吞用户取消。
5. 第一轮接口宜使用显式冻结选项区分禁用/启用，默认行为、序列化及 GUI 是否暴露由父审查决定；对照须同原目标与相同总预算，纳入增量光学调用和成功审计数量。
6. 若加一次可选补救，应使用审计得出的完整均值波段网格及其正确归一化梯形权重，冻结为一次 guidance 快照，再以原完整审计检验；不能把均值失败变成某一波长的点约束。当前原语 `_original_reduction` 要求与传入 request 完全一致的网格，不能仅给 `hybrid_evaluate` 偷换 request；需父层明确追加、计数并保留 original-score 语义的求值适配器。若无预算/适配器，保留冠军即完成，不加隐藏余量。

后续必测：默认 hybrid 真实路径、禁用/启用同预算比较；已达标先验的早返回覆盖；低预算不启动、切片中断可恢复、回调取消不追加审计、总时间截止不补算；密网格拒绝不替换旧冠军；从零跨类别收益与开销。当前独立原语并不代表这些集成验收已经完成。

## 默认 hybrid 接线完成（同日后续）

- 显式冻结请求升级 v3；`enable_thickness_reduction: bool = True`，默认 hybrid 使用，legacy 忽略。v1/v2 直接拒绝，无兼容解码器。MCP、GUI 选项、保存/重开、冻结、队列、CLI 和当前基准生产者保留 true/false。
- 仅在正常搜索成功出口选择一个已独立审计的非空冠军；默认 16 次求解迭代、最多 32 次父候选求值，保留原审计预算。每次缓存/求值检查父取消与截止；不会从通用终态清理补算。
- 真实相同条件对照：每个案例均从相同已可行种子开始，CPU 1 worker，总预算 300。此表不是从零复杂多光谱算法进步证据。

| 类别 | 指标 | 固定层数 | 关闭时 D / nm | 开启最终 D / nm | 额外父求值 | 处理结论 |
|---|---|---:|---:|---:|---:|---|
| HR | mean | 12 | 1048.609679 | 1048.609679 | 32 | 32 次切片用尽，无改善 |
| HR | pointwise | 12 | 1048.609679 | 1048.609679 | 32 | 32 次切片用尽，无改善 |
| HR | both | 12 | 1048.609679 | 1048.609679 | 32 | 32 次切片用尽，无改善 |
| AR | mean | 1 | 112.268280 | 112.268280 | 23 | 密网格拒绝，保留冠军 |
| AR | pointwise | 1 | 112.268280 | 81.488630 | 8 | 独立审计通过并替换 |
| AR | both | 1 | 112.268280 | 81.488630 | 8 | 独立审计通过并替换 |
| mixed_RT | mean | 1 | 62.500000 | 62.500000 | 21 | 密网格拒绝，保留冠军 |
| mixed_RT | pointwise | 1 | 62.500000 | 22.166595 | 24 | 独立审计通过并替换 |
| mixed_RT | both | 1 | 62.500000 | 22.166595 | 24 | 独立审计通过并替换 |

4/9 得到合格减薄；2/9 因独立审计失败保留原冠军；3/9 在有界切片内没有可行改善。9/9 的最终返回均继续通过原独立审计。不能将原语更长运行中的 7/9 与本有界默认接线的 4/9 混为一谈，也不能掩盖无收益调用成本。

新增 17 项集成测试覆盖上述 9 对、legacy 不变、低预算不启动、密均值拒绝不替换、阶段预算、取消前/中、截止后不审计、数值工作集与档案分别能放入但总和超限时不派发。终态压缩旧测试显式关闭成本阶段，以继续独立核对压缩自身计数；默认真实路径由新测试覆盖。新字段的 25 项边界/UI 测试由独立定向检查验证。最终总计与全静态门禁结果在提交后报告。

输出数据：`benchmarks/film_synthesis/20261024/thickness-integration-smoke.json`。复现：`PYTHONPATH=. python benchmarks/film_synthesis/20261024/record_thickness_integration_smoke.py`。未增加均值补救或隐藏余量，复杂多光谱研究下一阶段另做有界分支备选局部精修竞争，不能用本后处理替代。

## 最终验证记录

- 固定工具链全静态门禁：1,134 个源文件，新增诊断 0、已解决旧债 0、语法缺陷 0；没有增加 baseline allowance。
- 大范围 synthesis/band/current-suite 回归：1,205 passed、4 skipped、65 subtests passed；仅两个 current-suite 用例因缺少新字段而失败。补齐当前登记的显式 false 对照后，最终受影响集合重跑 123 passed、65 subtests passed；这两个失败均已覆盖验证。未重跑此前全部大范围集合，不能把该过程说成一次无失败的全仓库运行。
- 大范围命令：`COAT_ASSISTANT_RUNTIME_ROOT=/tmp/coating-reduction-wide-tests-20261024 python -m pytest tests/test_film_synthesis*.py tests/test_film_band*.py tests/test_film_current_representative_suite.py -q --disable-warnings --maxfail=12`。
- 最终受影响集合：current representative suite、reduction contract/GUI、thickness primitive/integration、benchmark runner。
- 相比当前主线 aa91eca，只保留现有静态债务文件，不改变其内容；新增请求/结果边界版本为 3，无安装器或应用发行版本号更新。当前16项代表登记的成本算子设为 false 是明确控制臂，不可据此声称测过默认开启的同类基准。
