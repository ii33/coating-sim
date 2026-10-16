# 膜系高性能评估契约（2026-10-12）

> 最新补充（2026-10-12，云端主机）：批量 DE 已接入正式 worker；支持显式 CUDA / ROCm / XPU 后端路由，需 FP64/complex128 运算探测及 CPU 光谱对照。AMD/Intel 未实机验收；原 GPU 硬件测量不等于新版完整作业验收。默认仍保守 CPU/串行。见 [当前契约](film-optimization-hpc-contract.md) 和 首阶段实测（历史验证记录，未随公开源码发布）。下文早期阶段状态保留为历史记录。

状态：CPU 服务级阶段已实现并实测；GPU 尚未完成 CUDA 实机验收。完整证据见 阶段包（历史验证记录，未随公开源码发布）。

2026-10-12 独立保存/重放候选见 [第三阶段契约与结果](film-performance-stage3-candidates.md)。重放复制候选慢3.25%，不推荐合并；有界checkpoint单次编码的当前真实worker写入阶段约快25.2%，总worker仅1.024x，默认关闭补丁未应用，不能据此宣称整体明显加速。

**部署状态：本轮没有默认自动加速。** 新包装和独立批量DE均需显式调用；八法、multistart、needle边界及最小示例见 [精确交接](film-performance-handoff.md)。共享作者后续已加入默认关闭的 worker 缓存开关，真实入口验收由其负责；服务/隔离候选基准不表示现有优化窗口已验收加速。最新剖析、测量版本和后续目标见 [有限 worker 性能剖析](film-performance-profile.md)。

## 文件所有权与阶段

2026-10-12 第二阶段仅新增固定拓扑 worker adapter、独立测试和接入前真实 worker 基准，数值模块维持阶段一指纹冻结。具体共享文件/签名/进度白名单/sidecar 契约见 [第二阶段接入清单](film-performance-stage2-integration.md)，完整数字见 第二阶段报告（历史验证记录，未随公开源码发布）。接入补丁由共享文件唯一作者应用；隔离候选验证不代表当前生产队列已启用。解析 J、旧 DE immediate、原预算、checkpoint 重放和 single_local_worker 均保留。20 个适配器回归用例已取得通过结果；当前 worker 端到端收益小且有变慢场景，没有支持默认自动加速门槛的充分证据，不以阶段一纯前向或显式包装基准代替。

高性能任务 `01a12d16-9282-70e4-b2ac-679ef61600ff` 拥有新增 `film_optics_batch.py`、`film_optics_gpu.py`、`film_evaluation_executor.py`、`film_performance.py`，各自测试、专属基准脚本和报告。复核者 主持；实现者 分别实现冻结 CPU 数值与有界执行器。阶段一冻结光学输入并测量 CPU 路径，阶段二限定 FP64 GPU 对照和显式回退，阶段三服务入口及受测阈值，阶段四回归与算法文档。


## 输入、输出与唯一状态所有者

冻结评估输入是明确真空波长 nm、入射到出口的 `n+i*k`、横向波矢、偏振及候选物理厚度 nm，数组为独立只读 FP64/complex128 副本。候选批量结果 shape 为 `[candidate,wavelength,3]`，最后一维是功率分数 R/T/A。偏振平均发生在功率上。没有材料目录、UI session 或当前设计的隐式依赖。

主调用方拥有候选提案顺序、seed、代次、评估额度和最佳档案。执行器只集中预留调用方交给它的额度并返回完整终态；不得独立改已有优化器预算或档案。结构调用方已经预留 token 时无需使用第二份额度账本。按提案顺序消费结果；平局保留先提案者。取消或旧代次结果不允许发布到当前最佳档案。

新批量 DE 服务使用单代结束才更新的 `deferred`，元数据明确独立版本，与旧 `immediate` 的轨迹不同。同 seed、同冻结数据、同配置、同模式可复现。已有八种优化器默认保持原行为。解析 Jacobian 仍为 CPU 路径；不存在全 GPU 梯度能力声明。

## 资源与错误边界

CPU 并行至多四个线程，并限制在途数。纯 NumPy 光谱计算不调用 BLAS；泛用评价函数需显式确认可并行，否则串行。批量候选与波长按 scratch 预算分块；结果保留大小另受限。没有无限作业队列、全核默认或全局线程环境变量修改。

仅选已有 PyTorch CUDA 的 complex128/float64；DirectML 不提供本路径要求的精度，不能换成 float32。精确临界层、未实现非相干/背面模型及任何能力失败均使用完整 CPU 参考入口并给回退原因。不改变网格、材料、单位或精度。GPU 失败不等于 GPU 验证成功；本机自动选择须由真实计算对照及端到端耗时校准支持，否则 CPU。

调用方停止提交后，已开始候选到计算分块边界检查取消；未开始预留可释放，已开始失败仍计账。只返回完整候选谱，禁止发布半谱。执行器不拥有磁盘归档或覆盖旧 best 的权限。

参考：[SciPy DE 官方文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.differential_evolution.html)、[PyTorch complex 官方文档](https://docs.pytorch.org/docs/stable/complex_numbers.html)。线程/内存上限、GPU 临界回退和自动阈值是本项目约定，须由本机测量验证。

## 可调用的稳定入口

```python
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions
evaluator = FrozenFilmEvaluator(frozen_snapshot, settings,
    options=PerformanceOptions(backend="auto", cpu_workers=1,
        chunk_bytes=64*1024**2, max_result_bytes=256*1024**2,
        max_prepared_bytes=256*1024**2, calibration=measured_profile))
response = evaluator.evaluate_thickness_batch(
    ["stable-layer-a", "stable-layer-b"], [[70., 90.], [75., 95.]], cancel=cancel_event)
# response: wavelength_nm[W], R/T/A[C,W], metadata(真实后端/回退理由/精度/冻结摘要)
```

实例调用串行化，内部纯 NumPy 批量可开至多四线程；不与共享 GPU 初始化/验证争用。`settings` 返回独立副本，波长与 prepared arrays 不能恢复写权限。材料采样沿用 source_only_zero_defaults 与请求中的 n 外推策略，QW 的参考 n 也使用同一策略。零厚只可通过 `zero_thickness_layer_ids` 明确标临时探针，不能成为合法保存设计。

现有八种方法入口为 `optimize_film_design_accelerated(service, request, *, options=None, cancel_event=None, progress_callback=None, checkpoint_callback=None)`，结果仍是原 `FilmOptimizationResult`，前向 prepared LRU 容量为2，解析 J 保留 CPU。该入口不改变旧请求格式、不写 session、不提交新作业。

新批量 DE 为 `run_batch_de(evaluate_batch, initial, lower, upper, *, max_iterations, max_evaluations, seed=0, population_size=12, max_batch_size=16, cancel_event=None, checkpoint_callback=None)`，厚度为物理 nm；纯评价函数返回同长 `BatchEvaluation(key=(worst,rms), surrogate=..., value=完整载荷)`。一批最多64候选，完整载荷归档和 checkpoint 各自独立快照；基线、开始后的失败/合作取消均计真实额度，取消载荷不归档。`mode="deferred-batch-v1"`，seed及实际预算写在结果中。它是独立入口，未接管结构循环或现有队列。

通用独立候选执行器的签名是 `execute_batch(candidates, evaluator, *, options=None, budget=None, cancel_event=None, current_generation=None, estimate_result_bytes=None)`，evaluator 接收 `(CandidateInput, cancel_event)`。结果按输入顺序，检查 `archive_eligible` 和取消代次后才可由所有者发布。启用结果上限时，必须给每候选保留结果大小上界；不透明 payload 的大小不能硬保证。

CLI：`python -m coat_sim.film_performance_cli capability` 或 `evaluate`。后者仅从标准输入接受冻结 JSON 与性能参数，返回完整 R/T/A；输入16 MiB、原始输出8 MiB上限，超限须减少候选数或使用 Python 服务，不降采样。没有 live 材料访问、文件/凭据/驱动/队列权限。

## 本机测量与能力清单

实际主机为 Ryzen 7 4800U（8核16线程），RAM 16,499,134,464 B；AMD Radeon，驱动31.0.21921.1000；已有 torch2.4.1+cpu。它不是历史4070TiSuper/32GB主机。NumPy2.5.3，FP64/complex128；没有 CuPy、threadpoolctl；纯光谱核没有 BLAS调用。首次 PyTorch探测超时保留为失败证据，随后通过 vendor build 声明及独立probe确认CPU-only，不把超时当CUDA通过。

| 同候选、同网格、同精度组 | 完整服务逐候选中位秒 | 冻结批量1线程秒 | 冻结批量4线程秒 | 自动选择 |
| --- | ---: | ---: | ---: | --- |
| 8×31点×4层 | 0.02099 | 0.001013 | 0.006915 | 1线程 |
| 12×1001点×12层 | 0.21194 | 0.04011 | 0.05241 | 1线程；2线程收益不稳定 |
| 8×50001点×18层 | 4.43625 | 2.78902 | 1.62877 | 已测精确区域4线程 |

这些速度比较包含冻结复用和减少重复服务报告的收益，不是只改workers。旧 NumPy纯核另测：中组中位0.08145秒、大组4.38079秒。全部候选 R/T/A 最大绝对差0。大组首次准备+4线程前向2.44581秒；三次预热中位1.62877秒。进程累计峰值RAM468,885,504 B（约447MiB），非每组独立峰值；GPU传输/显存没有可测数据。全量50001点解析J按原域分5块、2个变量，中位17.845秒；它仍明显比前向慢，不能称全GPU。

原immediate DE同48候选预算的预热中位：原服务0.57951秒、缓存入口0.36421秒，1.59倍。新的deferred DE同48候选、1001点×12层：原服务0.48000秒、批量0.13669秒，3.51倍；提案轨迹与最佳厚度完全一致。首次SciPy加载单列，不用它夸大优化加速比。原始逐次数字与运行脚本见阶段包。

自动配置只匹配运行时摘要、核版本、候选数、点数、层数、偏振和scratch的已测区域；至少3次、数值通过、比单线程中位耗时低20%才开并行。区域外CPU单线程，未给GPU阈值。本机合成基准未排他占用机器，因此保留逐次波动，不外推到其它CPU、材料或生产测量。

运行脚本：`scripts/probe_film_performance.py`（45秒受控自有子进程）、`scripts/benchmark_film_performance.py --output <独立报告目录>`、`scripts/benchmark_film_batch_de.py <独立报告目录>`。`--gpu`仅在有现有CUDA依赖的目标环境显式尝试；失败会记录真实CPU回退。没有安装新GPU依赖或改变系统设置。
