# 保留原名义规格的有限场景稳健优化

日期：2026-10-23。本文是独立敏感性工作流的增量契约，配合 [膜厚敏感性契约](film-sensitivity-contract.md) 使用。接口真值为 `coat_sim/core/film_sensitivity.py` 的类型、序列化与校验函数；约束求解位于 `coat_sim/core/film_sensitivity_constrained.py`。实现中的新策略不能反向改变旧请求含义。算法条目见 [离线算法文档](algorithms.html#film-robust-nominal-preserving-20261023)。

## 1. 目的、策略与不作出的承诺

新策略在原名义规格可行域内，降低用户明确给出的厚度误差场景的光学违规。它不增加额外安全 margin，不把原目标变成更宽松的目标，不把训练场景分数当成真实生产良率。

`FilmSensitivityRequest.optimization_policy`：

- `robustness_only`：保留旧默认。有限域坐标搜索直接降低选定误差目标，名义是否合格独立报告；不承诺保持名义可行。
- `nominal_preserving`：显式选择的新局部约束策略。`operation="optimize"` 时仅接受 `finite_scenario_worst` 或 `finite_scenario_expected`，调用 SLSQP 并提供解析目标/约束导数。原网格的每条名义条件以及显式厚度上下界都是硬约束；只让完整评价且原名义合格的候选更新可行 incumbent。

`linear_sensitivity_norm` 仍可分析或沿旧策略搜索，但不是新约束策略的替代目标。当前没有 CVaR、连续不确定集合内层最坏搜索、chance constraint 或自动制造良率优化。文献提出过这些方向，不等于本版已实现。`operation="analyze"` 可以保留该 policy 字段，但不施加优化可行性要求或改变分析评分。

## 2. 输入、单位与兼容性

### 2.1 冻结请求

继续使用独立 request/result v1：设计快照、来源 revision、完整原 R/T 目标与采样条件、冻结材料 n/k、误差定义、场景及其权重、变量和预算共同确定一次运行。材料或目标编辑不会改变运行中的冻结对象。普通厚度优化请求不接受敏感性字段；必须显式构造独立请求或使用 `sensitivity_request_from_optimization`。

新增字段附在已有 dataclass 默认字段之后，保留旧位置参数调用含义：

| 对象与字段 | 默认值 | 含义 |
|---|---|---|
| `FilmSensitivityRequest.optimization_policy` | `robustness_only` | 明确选择旧策略或 `nominal_preserving` |
| `FilmSensitivityRequest.error_amplitude_semantics` | `bounded_amplitude` | 明确幅度是有界幅度还是 `standard_deviation` |
| `FilmThicknessError.absolute_nm` | `0.0` | 非负、加性的绝对物理厚度尺度，nm |
| `FilmThicknessError.relative_fraction` | `0.0` | 非负、加性的相对厚度尺度，0.01 表示 1% |

旧 `FilmThicknessError(layer_id, magnitude, unit)` 保留。`magnitude` 现可省略为零，`unit` 默认 `absolute_nm`；`unit` 仍只能是 `absolute_nm` 或 `relative_fraction`。这些零是中性兼容值，不是对设备误差为零的推断。每条误差通过唯一 `layer_id` 选择已有前表面层，且请求整体至少有一个非零尺度。

规范化序列化省略未启用的默认策略、默认幅度语义及零值新增误差项，保持既有旧请求规范身份；显式非默认字段、实际场景系数与正权重参与冻结身份。未知字段、NaN/Inf、重复层/场景、非法维度仍拒绝，不能静默丢弃。

### 2.2 物理坐标

所有厚度变换先解析成实际 nm。目标的波长为 nm、入射角为 deg；R/T 使用核心原有无量纲功率分数，百分数只作显示换算。扰动模型不使用 QW 显示值代替物理厚度，也不改变层序、材料或光学环境。

设 `errors` 中第 i 项选择层 i，当前候选的名义厚度为 dᵢ。定义：

- Aᵢ = absolute_nmᵢ + [unitᵢ = absolute_nm] · magnitudeᵢ；
- rᵢ = relative_fractionᵢ + [unitᵢ = relative_fraction] · magnitudeᵢ；
- aᵢ(dᵢ) = Aᵢ + rᵢ dᵢ；
- δdₛᵢ = cₛᵢ aᵢ(dᵢ)，d′ₛᵢ = dᵢ + δdₛᵢ。

这里的方括号表示条件为真时取 1，否则取 0。旧 `magnitude` 与新项相加，不互相覆盖；同一层的全部项共用一个有符号系数 cₛᵢ。比如明确给定 A=0.4 nm、r=0.01、d=50 nm，则 a=0.9 nm。这个例子只演示算式，不是默认工艺参数。

在 `standard_deviation` 语义下，这也是完全相关分量的 σ=A+r·d，不是独立分量的 √(A²+(r·d)²)。本版不能借由同时填写两个尺度表达独立的绝对噪声和相对噪声。需要该模型时须新增明确的独立潜因子接口，不能自行改算 RSS。

## 3. 显式场景、幅度语义与相关模型

`FilmSensitivityScenario(scenario_id, coefficients, weight=1.0)` 的系数长度和顺序必须恰好匹配 `request.errors`，不是 `variables` 或层表的隐式排序。ID 非空且唯一，权重有限且严格为正。

- `bounded_amplitude`：每个 cₛᵢ∈[-1,1]，a 是所声明的最大绝对幅度。显式有限场景不意味着整个 box 已被覆盖。
- `standard_deviation`：c 是 σ 单位的有符号乘数，可大于 1 或小于 -1，不截断、不 clip；仅此字段并不自动声明 Gaussian 分布。
- 任何场景产生负数或非有限实际厚度都明确失败，不把厚度夹到零，不悄悄删除场景或重抽到“通过”为止。超出设计变量边界与负物理厚度是不同概念：变量边界限制名义设计，有限误差不自动受同一名义边界截断。

相关性通过 `error_model` 明确选择：

| 模型 | 系数结构 | 实际含义与限制 |
|---|---|---|
| `independent` | 每误差层可有不同 cₛᵢ | 便捷生成器对各层抽独立潜因子；手填有限点本身不证明统计独立 |
| `common_mode` | 同一场景内所有误差层系数相同 | 一个批次潜因子，不是逐层重复抽样 |
| `material_common_mode` | 同一 `material_id` 的误差层系数相同 | 每材料一个潜因子；不同层幅度仍可不同 |
| `linear_drift` | cₛᵢ = zₛ hᵢ | 一个斜率潜因子按完整前表面层序作用，不按选中变量/误差子集重新编号 |

完整前表面层数为 L、层的零基索引为 kᵢ 时，L>1 下 hᵢ=-1+2kᵢ/(L-1)；L=1 时 h=1（本项目约定）。因此三层的 h 为 (-1,0,1)，中心层不受此纯线性斜率扰动。显式系数须符合该比例；有界语义仍逐个验证 c∈[-1,1]。这不是 AR(1)、任意协方差、累积随机游走或光学监控反馈模型。

### 可重现场景生成器

`generate_film_sensitivity_scenarios(design, errors, *, sample_count, seed, error_model, distribution, scenario_prefix="process")` 返回显式 `tuple[FilmSensitivityScenario, ...]`，不在求解过程中重新抽样：

- `sample_count`、非负整数 `seed`、相关模型及 `distribution` 都须明确提供。
- `bounded_uniform`：对所需潜因子抽 Uniform[-1,1]，配合请求的 `bounded_amplitude` 语义。
- `gaussian`：对所需潜因子抽标准正态，不截断，配合请求的 `standard_deviation` 语义。
- 同种材料共模在每个场景只抽一次材料因子；线性漂移只抽一次斜率，再乘 h。独立材料/层因子的抽样与同层绝对/相对项共用因子的语义不可混淆。
- 调用方将生成的系数整体写入冻结请求；重放以实际系数为准，不依赖随机库版本。种子、数量、分布及用途应作为调用方的实验记录保存，而不是杜撰请求类型并不存在的字段。

生成器有限制以避免意外巨量分配：单次 sample_count∈[1,100000]，且 `sample_count × len(errors) ≤ 2000000`；超过时明确拒绝，需调用方分批。它不填充用户没给的 nm/% 精度、批次偏差、相关系数、良率目标或测量来源。缺少实测工艺校准时，生成云应描述为“假设压力测试”，不能描述为设备真实分布。

均匀 bound 与 σ 不等价：Uniform[-a,a] 的标准差为 a/√3。将相同数值从 bounded 切为 sigma 改变了模型，不是单位显示切换。

## 4. 分数公式与原名义硬门

令 c 表示原请求中的一个完整光学采样条件（目标行、波长、角度、偏振和 metric）；yₛc 为有限扰动的完整相干前向结果，b_c 为正 `normalization_scale`，w_c 为正目标权重。定义非负原违规：

- value 目标：vₛc=|yₛc−target_c|；
- range 目标：vₛc=max(0, lower_c−yₛc, yₛc−upper_c)，未给的单侧不参与；
- eₛc=vₛc/b_c。

有限场景目标严格为：

```text
finite_scenario_worst = max_(s,c) sqrt(w_c) * e_sc

finite_scenario_expected
  = sqrt( sum_s p_s sum_c w_c * e_sc^2
          / (sum_s p_s * sum_c w_c) )
```

各场景使用同一原条件集与权重。expected 等于“各场景的条件加权 RMS，再取场景权重 RMS”，不是场景 RMS 的算术平均，不是均值 signed loss，也不是通过概率。权重 pₛ 不必预归一化，但必须正；pₛ 不改变 worst。即使场景有一项严重失败，也可能被 expected 平均稀释，因此逐场景 `finite_targets_met` 仍单列。

原名义硬门直接核对未扰动完整响应：范围目标遵守原 lower/upper；value 目标保持精确相等语义。权重与 normalization_scale 只改数值评分，不改变通过零界。`acceptable_normalized_error` 与 SLSQP 的数值停止容差不放宽原规格；`solver_acceptable` 独立记录，不能替代 `nominal_targets_met`。

仅 `strict_upper_bound_objective_indices` 明确指定的 range 上界使用严格 `< upper`，不从数值大小猜测严格性。约束求解时用 `nextafter(upper, -∞)` 作为可表示的闭上界，最终仍按实际响应 `< upper` 验收；这是一 ULP 的浮点表达方式，不是附加工艺 margin。未指定的上界仍 `≤ upper`。在严格边界恰好相等时，连续非负超限分数可能为零但通过标志为假，必须看原不等号，不能仅用分数零判通过。

若用户输入的原目标已含 margin，该值就是不可变原目标，保持不动；本策略不再附加第二层 margin。当前目标类型是逐采样条件，不把目标行解释为积分/区间平均值。

## 5. 求解、解析导数与候选接受

### 5.1 误差层与变量分别映射

`errors` 决定哪些层被扰动，`variables` 决定哪些名义厚度可以改变。两者可不同、顺序也可不同：可只优化少数层而扰动全部已明确指定的层；不能因为未优化某层就漏掉其误差。误差列表之外的层不自动获得误差，也不能把“所有允许变量”说成“整个膜堆均被误差覆盖”。

设 xⱼ 是变量 j 对应的实际名义厚度。若该层有误差项，则：

```text
∂d′_si / ∂x_j = 1 + c_si * r_i    （同一层）
∂y_sc / ∂x_j = (∂y_sc / ∂d′_sj) * (1 + c_sj * r_j)
```

未受误差的变量链因子为 1；未优化的误差层仍改变场景前向，但没有对应自由变量列。r 必须包含旧 `magnitude` 中相对项和新 `relative_fraction` 之和。a 随当前候选 d 变化，不能冻结成初态 additive offset；c 才是固定 common-random-number 场景坐标。

相干 TMM 复用 `simulate_with_thickness_jacobian`，同时返回所需层的解析 Jacobian。nominal 与有限扰动使用各自实际设计点的 Jacobian；`nominal + J·δd` 仅作一阶诊断，不作为有限场景真实值或求解中的真实前向替代品。

### 5.2 有限场景约束解

新策略使用有界 SLSQP，向目标及原名义约束提供解析导数。将每个场景的精确 d′ₛᵢ≥0 线性条件与用户名义变量界求交，再以有效域可表示端点映射无量纲坐标，Jacobian 同乘有效域跨度；不放宽以nm表示的原界。精确端点、初始物理厚度锚点及近端点插值避免反变换越界1 ULP；候选入档前再次核对真实厚度界，正式回放的严格界不放宽。物理根采用与forward相同的d+c(A+rd)运算复核，仅向有效域内最多检查64个相邻可表示值，仍不可用则明确失败；此为数值护栏而非工艺阈值，绝不截断系数或把负扰动夹零。worst约束与Jacobian的√w·值/scale使用frexp/ldexp稳定乘除，避免先计算可溢出的√w/scale；不删条件、不换目标。worst 的辅助 epigraph 变量 z 表达所有有限场景/条件的加权违规上界；expected 使用相同场景目标的解析链式导数。仅可优化声明的名义变量，不改拓扑、层数、材料、网格或规格。当前数值停止参数为 SLSQP `ftol=1e-10`；用户未给 `max_iterations` 时约束路径使用 100 次内部迭代上限，给定时使用原值。`max_evaluations=None` 不额外设置候选评价上限。这些是求解调度参数，不是工艺参数、原规格容差或连续域精度保证。

SLSQP 的中间试探点可能名义不合格；这不是通过结果，更不能覆盖 incumbent。每次接受候选都需要完整名义及全部场景评价，并单独经过原名义 checker。仅 solver success、局部 KKT/步长停止、罚函数降低或缓存命中不能代替这一门禁。最终结果的真实终态与已保留的完整可行候选分别保存。

初始设计必须在显式名义厚度边界内且逐条通过原名义规格，否则新策略明确失败，不自动 clamp、修复名义目标或另启 nominal optimizer。需要名义可行性恢复时应另行运行并记录，成功后重新冻结稳健请求。物理/材料错误也不得冒充“初态不可行”。

## 6. 状态所有者、预算、取消与保存

唯一数值状态所有者仍是核心运行，拥有冻结请求、评价器、完整分析、求解器和 incumbent；GUI 只管理代际、取消意图与队列观察。编辑设置不重标记运行结果，关闭窗口不自动应用。

- 输入结构错误在进入运行前明确拒绝。
- `analyze` 的完成仅表示名义/线性/所列有限场景完成，不要求名义合格，也不宣称已优化。
- `nominal_preserving` 的不可行初态返回失败，不能作为“完成的稳健优化”。完整初态分析可作为诊断保留，但不是可应用的可行 incumbent。
- 完成分析为 `completed/analysis_complete`；新约束优化局部完成为 `completed/constrained_tolerance`，旧策略保持 `completed/step_tolerance`。
- 新策略初态名义失败为 `failed/initial_nominal_infeasible`；物理失败为 `failed/physical_evaluation_failed`。
- 迭代/评价预算耗尽为 `partial/iteration_limit` 或 `partial/evaluation_limit`；用户取消为 `cancelled/cancelled`。
- SLSQP 的最终试探未通过精确名义 checker 为 `partial/constrained_nominal_rejected`；其他非成功停止为 `partial/constrained_solver_stopped`。两者都保留先前可行 incumbent，不把 solver 停止误报为完成。不能把有候选等同于成功终态。
- 取消在完整物理调用/分块边界协作检查。只保存取消之前已完整评价并满足原名义硬门的 incumbent；不发布半个场景云或末次未评分向量。
- 预算/取消发生在可行基线评价之前时，可能没有候选；不得制造空结果的通过标志。完整可行候选已经存在时，保留该候选和全部对应分析，不因后续试探失败将其丢弃。

计数区分候选评价、实际前向、Jacobian 与失败评价；不能仅用外层循环次数声称成本改善。原始采样不缩减，不为适配预算静默删场景。缓存绑定完整冻结设计/材料/目标/误差/场景/策略而非只绑定层 ID；资源上限是本项目护栏，不是误差精度假设。现有完整结果估计超过 256 MiB 时明确拒绝，分析缓存受 32 项和 64 MiB 双限；约束求解所需附加导数存储也须计入资源估计。

保存/恢复和应用继续走独立结果物理重放、来源 revision/编辑 epoch 核对及显式应用确认。partial/failed/cancelled 的可行候选可以查看或保存，不因此自动拥有应用权限。原子文件写入失败不得产生半份新报告或破坏既有报告。

## 7. 验证集合与报告用语

场景“训练、调参、holdout”按用途严格分开：

1. 训练集参与优化，冻结其显式系数，baseline 与 candidate 使用同一场景表做成对比较。
2. 调参/validation 集可用于选择策略、误差档、变量、seed 或候选；一旦用于这些决策，就不是最终 holdout。
3. holdout 在最终候选/参数固定后才评价。若据其结果改设计或选解，该集合已经成为调参集，需要新的未使用 holdout 才能再次称独立最终验收。

不同 seed 不能单独证明独立性或无泄漏；应同时记录场景用途、全部系数、分布、幅度语义、相关模型、误差来源、物理网格与是否被用于决策。无需为使用生成器而接受任何固定工艺数字。

至少报告 baseline/candidate 的原网格名义是否合格、全部逐条件原响应、所选代理目标、逐场景通过标志、误差定义及计算预算。可以报告“这组显式有限场景的通过比例”，但不把它重命名为真实生产良率。只有对已声明概率模型进行合适独立抽样时，才可讨论该模型下的 Monte Carlo 通过率及其统计不确定性；本策略并不自动提供该统计认证，更不提供实物批次保证。

本模块 `nominal_targets_met` 只说明原请求采样网格的完整核对通过。区间之间、未采角度、细网格/局部极值、不同 n/k 或实际设备可能失败；细网格复核有价值，也不等于连续域证明。worst 仅对已列有限场景取最大，不是所有有界误差组合的 supremum；expected 仅是代理损失 RMS。固定拓扑局部优化没有全局最优或“总能找到可行设计”保证。

## 8. 验证要点

- 旧请求默认策略、默认幅度语义、规范序列化与缓存身份兼容；旧坐标搜索路径未被新策略偷偷替换。
- 非负附加项、旧项叠加、σ/bound 区分；sigma 系数超过 1 保留，bounded 越界拒绝，负厚度明确失败而非 clip。
- 全局共模、同材料共模和完整层序线性漂移的系数与线性响应定义；误差/变量不同顺序或仅部分重叠。
- 相对误差链式因子与有限差分对照，包含旧相对 `magnitude`、新相对项、混合绝对项、边界和相关场景。
- worst 与 expected 按明确公式对照，目标/场景权重改变不造成“平均 RMS”误实现。
- 原范围/精确 value/严格上界逐条硬保留；`acceptable_normalized_error` 不救活原名义失败，初态失败不自动修复。
- 最终、预算、取消、失败、检查点与独立物理重放的完整性；只保留可行 incumbent，未完成末点不可覆盖。
- 生成器同输入可重放、所列分布及相关组正确、参数必填、资源越界明确拒绝；训练/调参/holdout 用途不混淆。

## 9. 来源及与本实现的边界

- Birge, Kärtner & Nohadani, *Improving thin-film manufacturing yield with robust optimization*, Applied Optics 50, C36–C40 (2011), [DOI:10.1364/AO.50.000C36](https://doi.org/10.1364/AO.50.000C36)，[DESY 开放全文](https://bib-pubdb1.desy.de/record/97205/files/32DE6A27-C2C7-A869-C92E4A7D3539AB91_206412.pdf)。支持“最坏代理与制造良率不等价”的方法边界，不验证本项目当前参数或实现。
- Pervak, Trubetskov & Tikhonravov, *Robust synthesis of dispersive mirrors*, Optics Express 19, 2371–2380 (2011), [DOI:10.1364/OE.19.002371](https://doi.org/10.1364/OE.19.002371)，[作者机构全文](https://attoworld.de/fileadmin/user_upload/tx_attoworld/publications/paper_OptExpr_Y2011_M01_D31_V19_P2371.pdf)。支持邻域 design cloud 的方法来源，不能将其特定溅射样品误差数值当成本项目蒸发设备默认值，也不意味着本文同潜因子参数化与论文完全相同。
- [SciPy SLSQP 官方文档](https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html)。约束局部求解与显式 Jacobian 的技术入口；停止容差与 success 不是原光学规格认证。
- [OptiLayer Statistical error analysis 官方文档](https://optilayer.com/optilayer-tools/error-analysis/statistical-error-analysis/)。说明工业工具区分绝对/相对误差与材料相关误差；不提供本项目真实工艺精度。
- 本项目约定：旧项与附加项共用有符号潜因子、四类相关模型的具体编码、线性漂移 h、两个有限场景评分、原名义硬门、兼容序列化、资源/取消/incumbent 协议。未找到外部依据证明这些组合已经在本项目实物工艺中验证，不作此声称。
