# 膜系高性能代码交接（2026-10-12）

结论：生产入口仍是**显式调用的服务级能力**。现有优化窗口、GUI仿真、single_local_worker队列、MCP、旧`capability_cli`及专项CLI没有自动切换到新缓存/批量后端。第二阶段新增 `film_optimization_forward_cache` worker adapter 和供唯一作者应用的 [最小接入补丁](film-performance-stage2-integration.md)，已做隔离候选验证，尚未应用共享队列文件。新增独立 `film_performance_cli` 仍只有显式运行时使用；不能把服务或隔离候选基准当作当前窗口加速。

## 公开入口与配置

下列符号分别来自`coat_sim.core.film_performance`、`film_optics_batch`、`film_evaluation_executor`、`film_batch_de`；签名以当前源代码为准。

```python
# 主服务，冻结一套材料/层序/网格/入射条件
FrozenFilmEvaluator(frozen_snapshot, settings=None, *, options=None)
FrozenFilmEvaluator.evaluate_thickness_batch(
    layer_ids, thickness_nm, *, cancel=None, zero_thickness_layer_ids=())

# 固定层序八法的显式缓存包装，返回既有 FilmOptimizationResult
optimize_film_design_accelerated(
    service, request, *, options=None, cancel_event=None,
    progress_callback=None, checkpoint_callback=None)

PerformanceOptions(
    backend="auto", cpu_workers=1, chunk_bytes=64*1024**2,
    max_result_bytes=256*1024**2, max_prepared_bytes=256*1024**2,
    calibration=None)

# 已有纯 n+i*k 数组的低层入口，绕过材料服务准备
FrozenCoherentStack.prepare(
    wavelength_nm, indices, angle_deg=0, polarization="unpolarized", beta=None)
FrozenCoherentStack.evaluate(thickness_nm, *, chunk_bytes=64*1024**2, cancel=None)
CoherentBatchEvaluator(stack, options=None)
CoherentBatchEvaluator.evaluate(thickness_nm, *, cancel=None)

# 通用独立候选；evaluator(candidate, cancel_event)，不归档、不选best
CandidateInput(candidate_id, operation_generation, payload)
BudgetLedger(max_evaluations)
ExecutionOptions(
    backend="auto", cpu_workers=1, max_in_flight=None,
    chunk_bytes=8*1024**2, max_result_bytes=64*1024**2, blas_safe=False)
execute_batch(
    candidates, evaluator, *, options=None, budget=None, cancel_event=None,
    current_generation=None, estimate_result_bytes=None)

# 独立新DE；evaluate_batch([C,V] nm) -> 同长 BatchEvaluation 序列
BatchEvaluation(key, surrogate, value)
run_batch_de(
    evaluate_batch, initial, lower, upper, *, max_iterations, max_evaluations,
    seed=0, population_size=12, max_batch_size=16,
    cancel_event=None, checkpoint_callback=None)
```

`PerformanceOptions.backend`允许`auto/cpu/gpu`；CPU workers为1–4。全部默认值是`auto`、`calibration=None`、`cpu_workers=1`，因此CPU单线程；不会自动读磁盘配置或依据旧设备成功记忆选GPU。`cpu`按显式worker数运行；`auto`匹配已测配置时覆盖worker选择，未匹配时保留调用方给定worker数（默认1），不是强制把手动4改回1。调用方须显式传入本机测量的配置dict，匹配环境摘要、核版本、候选/点/层数量、偏振、scratch的已测区域才自动选择。本机配置仅CPU，小中组1线程、大组4线程；未给GPU阈值。

`execute_batch`是CPU回调执行器：`gpu`仅返回真实CPU回退元数据。`max_in_flight=None`会设为worker数，上限不超过worker数；多线程须`blas_safe=True`且调用方保证无BLAS超订阅。设置结果上限时必须给`estimate_result_bytes(candidate)`上界。结构作者已有token时`budget=None`，不新增第二份账本。

新DE厚度坐标为物理nm；`key=(worst,rms)`作最终字典序归档，`surrogate`仅给SciPy提案。`max_batch_size`为1–64；基线和开始后的整批失败/取消计费，未调用不计费。取消批次不更新旧best。结果含`mode="deferred-batch-v1"`、seed、evaluations/failed/cancelled_evaluations等；checkpoint接收独立`BatchDEProgress`快照。它不读取`FilmOptimizationRequest`，不自动替换旧DE，不含自动留出验证，调用方需按自己的结构验收另做验证。

CLI：`python -m coat_sim.film_performance_cli capability|evaluate`。`evaluate`标准输入字段为`frozen_snapshot`、`settings`、`layer_ids`、`thickness_nm`、`performance`及可选`zero_thickness_layer_ids`；标准输出完整R/T/A与metadata。输入16MiB、原始数值输出8MiB上限，不降采样，不读live材料或写文件/队列。

## 原八法与needle的实际接入状态

| 路径 | 现有入口默认行为 | 显式调用新包装后的行为 |
| --- | --- | --- |
| baseline / powell / nelder_mead / simulated_annealing | 原串行服务，无新缓存 | 前向调用复用容量2的prepared LRU；提案仍串行 |
| multistart | 原串行起点+Powell | 前向可复用；多起点并行尚未接入 |
| differential_evolution旧ID | immediate、workers=1 | 同immediate语义，仅前向缓存；不接新deferred批量 |
| trf / lbfgsb | 原CPU解析J与既有候选缓存 | 初始/验证前向可复用；优化残差/J主路径仍是原CPU导数模块，无GPU J |
| needle / 结构合成 | 原作者循环、预算、归档与队列 | 尚未接入；仅交付下列纯评估接口供作者调用 |

八法的显式包装已经做定向行为对照，但没有替换GUI/队列调用位置。旧immediate同模式、301点×4层、48次候选计算的1.59倍，与新deferred同模式、1001点×12层、48次计算的3.51倍，是各自独立服务基准；不表示现有窗口自动加速，也不比较两种DE整体轨迹。

## 给针插作者的最小调用契约

```python
import numpy as np
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions

# frozen_candidate: 本候选拓扑的完整、摘要已校验快照；设计保存值必须合法正厚。
# 主循环先按本批候选数预留自己的评估token；不在后端改预算或best。
backend = FrozenFilmEvaluator(frozen_candidate, settings,
    options=PerformanceOptions(backend="cpu", cpu_workers=1))
ids = ["stable-layer-a", "stable-layer-b"]
spectra = backend.evaluate_thickness_batch(
    ids, np.array([[70., 90.], [75., 95.]], dtype=np.float64), cancel=cancel_event)
# wavelength_nm:[W]；R/T/A:[C,W]，行次序等于提案次序。
# 主循环复核cancel/代次，再按原指标评分并原子归档完整候选；不只归档代理分数。

# 同拓扑零厚临时探针；不保存为设计，不用行号替代ID。
probe = backend.evaluate_thickness_batch(
    ids, [[70., 0.]], cancel=cancel_event,
    zero_thickness_layer_ids=("stable-layer-b",))
```

`frozen_candidate`的类型/摘要语义沿用现有`coat_sim.frozen_film_design`，包含完整设计、冻结材料与sha256，不能从live目录补材料。同一实例只允许厚度改变；插层/删层/换材料/换网格/角度/偏振须准备新的合法拓扑快照和实例。零厚探针的base快照仍需合法正厚，随后通过显式ID临时取零；非相干零厚探针会拒绝，不能偷换模型。

返回精度为float64/complex128、功率分数R/T/A。失败`ValueError`、取消`InterruptedError`，没有半谱；外层负责记账。实例调用串行化，内部NumPy可按配置有限并行。需要每候选终态和代次标记时使用`execute_batch`封装；主循环只消费`archive_eligible`结果，并在发布时再次检查自身取消/代次。

实际设备AMD Radeon，已有torch2.4.1+cpu，实测backend为CPU；CUDA源码目标FP64/complex128尚未实机验收，J仍CPU受限域。没有安装驱动或换电脑。本交接冻结数值代码与原有调用点，后续接入由相应文件作者另行完成。
