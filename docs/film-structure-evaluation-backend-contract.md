# 结构候选评估与后端适配边界（2026-10-14）

状态：纯候选接口与串行 CPU 验收边界；性能批量适配尚未接入结构循环。高性能任务 `01a12d16-9282-70e4-b2ac-679ef61600ff` 已交付独立 CPU 服务；结构核心、循环、队列及优化窗口仍由针插任务拥有。两边不同时修改 `film_optimization.py` 或共享缓存计数。

## 冻结输入与纯计算输出

现有可复用光学入口真值为 `FilmDesignService.simulate(design, settings)` 和 `simulate_with_thickness_jacobian(design, materials, variables, settings, *, zero_thickness_layer_ids=())`（受限零厚关键字由本轮新增，默认调用语义不变）。后端适配不能依赖 live 材料目录、UI session、全局当前设计或磁盘 cfilm 中转。

一个候选输入包括：来源操作代次与候选 ID；独立复制的完整设计层序；经摘要校验的冻结材料记录；本次明确波长网格、入射角、偏振和 first_surface；需要 Jacobian 时的有序变量 ID 与物理 nm 单位。零厚结构探针额外列出精确为零的 front 层 ID，限临时计算，不作为合法持久设计或可应用结果。层 ID 用于匹配，不以行号推断旧层身份。

一次调用返回本次候选 ID、请求对应的波长 nm 与 R/T 数组。导数调用另返回 `jacobian_R`、`jacobian_T`，shape 为 `[wavelength, variable]`，列顺序对应输入变量，单位为 fraction/nm。偏振平均是 s/p 功率及功率 Jacobian 的平均，不能平均复振幅。支持域、材料采样语义与精确临界层拒绝沿用 CPU 核心；不支持的组合明确报错，不能静默换模型、网格、精度或材料。

输入冻结与摘要校验在主循环/提交边界完成，后端可以复用只读 prepared arrays，但不能改输入。批量结果按候选 ID 重组；worker 完成顺序不得改变顺序、消耗计数、并列候选决策或随机种子序列。

## 唯一所有者

结构合成主循环负责候选产生、硬约束检查、真实工作预算、seed、取消边界、代次检查、评分、原指标归档及 checkpoint。后端只做光谱/导数计算，不选择 best、不启动下一阶段、不写 UI/会话、不自行扩大预算。

主循环在实际计算前检查各层 dmin/dmax、总厚 Dmax、层数与材料合法性；零厚探针是显式计算例外。当前结构模式总评估预算包括完整训练网格评分、解析 Jacobian 和内部加密验证请求的实际计算，失败请求也记账；加密验证另报计数，不能偷偷参与代理选优。既有固定拓扑/有限试插的验证预算语义保持兼容。具体计数真值以 `FilmStructureCounters` 为准。

已取消每目标10001个波长、训练总条件20000及验证总条件200000的人工配额；合法输入仍须满足数值/物理有效性要求，搜索仍受用户明确设置的正整数停止预算控制，不引入0/None无限预算哨兵。10001仅是工作分块大小；训练与内部派生验证网格按底层波长数及波长×实际介质数的单块缓冲策略切片，不截断完整网格，实际分配或后端资源失败明确报告。切片端点不重复，worst/RMS在全体条件上汇总。共享额度按一次完整候选训练或验证请求计，失败请求也消耗一次；角度、目标与波长切片的底层调用不分别消耗候选额度。只有非空变量的解析导数调用才增加 Jacobian 计数。验证异常属于失败，预算不足或取消须保留已验证 best，并单独报告未完成验证。

未来批量调用必须先由主循环预留有限评估 token，附操作代次与候选 ID；禁止多个 worker 直接修改同一计数或缓存。取消使主循环停止产生与提交新候选，已开始的工作按实际 started/completed/failed/cancelled 状态记账；尚未开始的预留可明确释放。取消或旧代次返回不能更新当前 archive；允许保留已经完整验证的 best。不能以取消为由给已经执行的计算免费记账。

原训练验收键为 `(max_normalized_error, weighted_rms_error)`，光滑残差平方和只供局部精修。后端不得用代理下降替代原指标改善。完整候选、失败与取消的终态、加密验证完成与否需分开。应用设计仍由既有 session revision/edit epoch/token 保护，后端无回写权限。

## 待共同确认的性能 API

性能任务可在独立新模块中定义批量协议/后端策略，并提交最小适配 patch。需先交换其可实现的支持域、数组精度、错误与取消语义、计费单位及 deterministic result order；再将结构核心内部纯评估函数对接。当前不得假称已有批量/GPU 实现或并行速度提升。

核心作者已确认以下共享候选入口，文件到位后再据实际代码/测试冻结：

```python
evaluate_frozen_structure_candidate(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any], *,
    variable_ids: Sequence[str] = (),
    cancel_event: Any = None,
    with_jacobian: bool = False,
    zero_thickness_layer_ids: Sequence[str] = (),
    derived_validation_grid: bool = False,
) -> Mapping[str, Any]
```

约定输出键为 `design`、`score`、`smooth_loss`、`gradient_by_layer_id`、`jacobian_calls`、`responses`。`score` 内含 `max_error`、`weighted_max_error`、`weighted_rms_error`、`key=(max_error, weighted_rms_error)` 和 `errors: tuple[FilmConditionError,...]`；`smooth_loss` 为加权带符号残差平方和或 None；`gradient_by_layer_id` 为其对物理 nm 的导数映射或 None。`responses` 为每个 objective/angle 的波长、实际值及按层 ID 的 Jacobian 数组行，可对接后端批量物理计算。

`with_jacobian=False` 只请求原指标，`smooth_loss` 和 `gradient_by_layer_id` 为None，调用方不能强转为数值。请求标量代理但没有厚度变量时，可用 `with_jacobian=True, variable_ids=()`；它返回平方和，不计非空变量Jacobian。最终基准工具的有限宽度诊断已按此契约修复；历史诊断失败记录保留，不重新扫描或增加实验预算。

该函数不修改预算或归档；调用方根据一次完整工作更新计数。需要原始 R/T/J arrays 的后端应在此函数内部的物理计算边界适配，再由同一个候选评分层合成 score。公开字典序 worst/RMS 不冒称存在同一个平滑梯度。

`derived_validation_grid=True` 仅使用已冻结请求内可推导的验证网格；不能以此接受超限公共训练目标。Jacobian计数按已启动的非空变量帮助函数调用计，包含进入函数后失败或取消的调用；在下一片段开始前检测到取消，不计未启动调用。完整候选请求预算与底层调用数是不同计数，不能据相同请求额度推断相同计算量。

本文件解释边界，不能代替实际类型、函数签名和契约测试。任何签名变更先协调文件所有者。

## 已冻结 CPU 服务的适配评估

已阅读 [性能服务契约](film-performance.md) 和 CPU 阶段包（历史验证记录，未随公开源码发布）。`FrozenFilmEvaluator.evaluate_thickness_batch(layer_ids, candidates, cancel=...)` 的 nm 厚度、功率 R/T/A、冻结快照和取消语义可复用；同一层序的增长候选或有限针宽候选可共享一个 prepared evaluator。层内位置改变会改变拆层厚度，材料/插针位置/层序改变须另建冻结拓扑，不能把不同拓扑误塞入同一厚度矩阵。

经典零厚敏感度和局部精修还需 CPU 解析 Jacobian。现有批量服务只返回前向 R/T/A；不能直接替代此处包含逐条件评分、代理梯度和 J 计数的整个接口。自适应精修以及训练后 holdout 的先后预算边界也须由主循环保留。批量适配应只替换前向计算边界，按有限同拓扑族预留已有额度，完成后由现有评分层组装结果；不能再次使用第二份 BudgetLedger。

本轮决定先签署串行 CPU 结构正确性，不将尚未有结构轨迹/预算对照测试的批量 hook 加入验收版本。没有材料/单位上的硬冲突；剩余工作是上述适配和相同 seed、候选顺序、取消点及工作计数的结构专用对照。独立性能模块的速度数字不作为当前结构 GUI/队列加速的证据，CUDA 实机仍未验收。
