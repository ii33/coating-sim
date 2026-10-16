# 膜系求解器扩展契约（2026-10-14）

本轮仅算法 core、新测试和文档；不改窗口、会话、调度器。旧请求字段及默认行为保持兼容。输入快照和稳定层 ID 的状态所有者仍为调用方；求解器只返回候选，不直接写回会话。

缓存、预算、唯一条件分块、稳定评分与needle配置修复已落盘；当前版本、实际回归和未覆盖范围见 最终修复证据（历史验证记录，未随公开源码发布）。
修复前阶段独立报告 `film-optimization-stage-report-20261011.md` 已从当前检出移除；本契约说明求解器边界，不替代该历史运行记录。

## 固定层序

- `FilmOptimizationRequest` 追加 `solver: str = "baseline"`、`seed: int = 0`、`multistart_count: int = 4`、`population_size: int = 12`、`validation_samples_multiplier: int = 2`。既有 max_iterations/max_evaluations 是整次搜索共享上限。
- 方法 ID：`baseline`（原17点+坐标搜索）、`powell`（SciPy有边界方向集）、`nelder_mead`（SciPy有边界单纯形）、`multistart`（seed随机起点+Powell）、`differential_evolution`、`simulated_annealing`（有边界Metropolis）、`trf`、`lbfgsb`。实际可用性随解析导数能力和SciPy依赖明确报错，不静默换方法。
- 原始验收键是 `(max_normalized_error, weighted_rms_error)`；每次完整光学计算才可更新最佳候选和checkpoint。搜索可用代理，但结果始终保留原始键更佳者。
- 导数求解器仅使用残差平方和：TRF最小二乘，L-BFGS-B平方和及解析梯度。原始不光滑字典序键不传给梯度法。
- 搜索预算包括初始候选、失效候选、所有起点及导数/函数请求对应的完整候选光学计算；不计缓存命中。取消仅在完整候选计算边界生效，初始候选仍保证可返回。错误候选被记录，初始候选失效则抛错。
- 重型残差/Jacobian/条件误差缓存须有固定容量；函数和Jacobian同点可复用，淘汰后重新计算须计入实际预算。最佳档案只有一个，不为所有历史候选保留完整误差表。当前实现采用容量2的LRU，成功/失败共用容量；初始普通谱值和首次Jacobian分别计次，预算1时不重算Jacobian。
- 搜索结束保留原指标最佳候选；另以 `(N-1)*multiplier+1` 波长点（单波长仍1点），相同角度和偏振进行加密验证。验证不参与选优、不修改搜索计数；训练、验证结果分别报告。验证取消则标记未完成，不能宣称留出通过。
- `FilmOptimizationResult` 可追加带默认值元数据：solver/seed/termination_reason/failed_evaluations/validation_*；已有字段的含义不变，尤其 `targets_met` 仍代表原请求网格。

## 内部无导数接口

新增 `film_optimization_solvers.py`。`run_solver(method, initial, lower, upper, evaluate, *, max_iterations, seed, multistart_count, population_size, initial_step_fraction, min_step_fraction)`：输入和evaluate向量都是归一化厚度坐标[0,1]；主模块adapter按 `nm=lower_nm+x*span_nm` 转为物理厚度，再返回有限非负标量代理。所有预算、取消、最佳候选由外层控制，通过 `SolverStop` 中止。返回 `SolverOutcome(iterations, reason)`。外层保证完整候选原指标保留；方法不得绕过evaluate或使用外部并行worker。梯度Jacobian由每nm乘对应span_nm转换为归一化变量导数。

## 解析导数与结构合成

导数新模块独立于现有service文件；先通过中心差分、单位、吸收、斜入射s/p/非偏振对照，再公开能力域。无法可靠覆盖的结构显式拒绝，禁止悄悄有限差分冒充解析。

needle使用单独request/result与入口；只接受显式冻结候选材料、厚度与层数预算。使用有限厚度插入试探并局部精修，明确为本项目needle启发式（不是专有实现、不是零厚度连续敏感度法）。完整失败/取消回退到此前已验收设计，稳定旧层ID保留，新增ID确定性生成。

具体类型为 `FilmNeedleMaterialReference(material_id, optical_set_id)`、
`FilmNeedleOptimizationRequest(base_request, candidate_materials, max_added_layers=1, max_total_layers=4096, min_thickness_nm=0.5, max_thickness_nm=200, insertion_trials_per_interface=1, seed=0)`；
先以 `freeze_film_optimization_needle_request(service, request)` 冻结材料，再调用 `optimize_film_design_with_needle(...)`。
`max_added_layers=1` 是默认预算，可显式设置更大的正整数以顺序接受多层插入；每轮保留已接受新增层，并共享基础请求的迭代/评估预算。实际可新增数量还受 `max_total_layers` 剩余空间与设计文件4096层解析边界约束，不另设单层计算量限制。这与下述实验性经典结构请求的单轮算法边界不同。
结果 `FilmNeedleOptimizationResult` 持有 `base_result`、新增ID、接受/回退计数、失败计数及结构变化标记。
旧请求可无变量以仅探索新增层；仍经共享 `_validate_optimization_controls` 校验基础solver/seed/容差/步长/预算/多起点/群体/验证倍数，并经needle路径完整验证目标。
needle不是固定层序 `solver` 方法ID；旧有限试插仍是独立Python API。2026-10-11新增实验性经典敏感度结构请求 `FilmStructureOptimizationRequest`，以独立kind在现有 `film_optimization` executor中分派，不向旧固定层序v1塞入结构字段。结构材料冻结、硬约束、循环、验证和安全回写边界见 [结构合成契约](film-structure-optimization-contract.md)；原理与适用域见 [指南](film-structure-optimization-guide.md)。

## 队列序列化

队列v1请求追加上述五个可选solver字段；旧payload缺省仍读为旧默认值，未知字段或无效类型显式拒绝。
完整结果追加可选 `solver_validation` 元数据，候选字段和训练指标含义保持；checkpoint仍采用原训练候选结构。
队列重放只对冻结候选评分，不再次优化；完整验证元数据需在相同冻结材料与加密网格复核。
重放与入口在每个objective内统计唯一请求条件，不跨objective合并相同波长的不同目标。
主入口、needle和重放均按全局网格非重叠切块，单次服务至多10001点；单点尾块保留。评分共用二进制指数缩放RMS；有限结果不因中间平方溢出或√w·e下溢变成无穷或0，真正非有限逐条件/解析派生值显式拒绝。

## 文件归属与验收

集成作者：`film_optimization.py`及新集成测试；无导数作者：`film_optimization_solvers.py`及自身测试；导数作者：`film_optimization_derivatives.py`及自身测试；needle作者：`film_optimization_needle.py`及自身测试。Sol维护本契约、使用说明及algorithms.html，审查全部关键证据。当前接管修复由Luna实施、Sol独立审查；测试仅指向明确文件，使用纯内存材料适配和禁用pytest缓存，不写TEMP、不遍历旧pytest目录。材料持久化及浏览器显示验收范围见最终报告。无提交、发布、计费LLM请求。
