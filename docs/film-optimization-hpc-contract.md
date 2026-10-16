# 正式优化作业的批量执行契约

2026-10-12。第一阶段：固定拓扑厚度优化，保留原串行默认。

- 请求增加 execution_mode（serial 默认；deferred_batch 显式开启）及 batch_size（1–64，默认16）。仅 differential_evolution 允许 deferred_batch；请求/检查点保留模式，不能把延迟更新声称为原即时更新轨迹。
- 工作进程接收可选 PerformanceOptions。auto 无匹配的本机测量配置时保守使用 CPU；不得把 sakuya 的阈值当作所有机器的阈值。GPU 继续使用 float64/complex128、数值预检及明确 CPU 回退。
- 优化器拥有评价预算、最优候选和结果状态；批量内核只计算固定快照的光谱。完整基线总是先评价，包括预先取消；其重复项不再算一次。批次开始前预约全部评价名额；中断批次不提交部分候选。
- 每批按条件计算光谱并留在进程内的 NumPy 数组；不将每个候选经 JSON 往返。仅新最优候选、终态和诊断进入现有队列通道。评分与稠密网格复核沿用原实现。
- 预计算缓存有界；每批候选数同时受光谱结果缓冲预算限制。Python 对象开销并非严格内存上限。超域/非标准模拟服务必须明确报错，不可无声套用另一模型。
- 不改变膜系物理、单位、偏振、材料快照、R/T目标、接受阈值。延迟 DE 不保证和即时 DE 相同最优解；需比较预算、得分与耗时，不只比较内核吞吐。

验收包括序列化往返、无效模式拒绝、真实作业入口、标量评分复核、预算尾批、预取消/批中取消、失败保留最优、CPU/GPU 回退和固定种子重复。GPU 真机耗时需在有 GPU 的执行端另外测量。

## 启用与诊断

构造 FilmOptimizationRequest 时显式传入 execution_mode="deferred_batch"、solver="differential_evolution"、batch_size=16，再经原 freeze_job_payload/共享计算队列提交。旧请求省略新增字段，仍默认 serial。

直接作业 API 的 performance_options 接受 PerformanceOptions；原 worker 的 COAT_SIM_FILM_BACKEND=auto|cpu|gpu 与 COAT_SIM_FILM_CPU_WORKERS=1..4 只作用于显式批量请求。auto 无本机校准时 CPU；初版 worker 不读取外部校准文件。具体设备与回退原因记录到原 job 目录 film-performance.json。序列化、重放验证、原子检查点保存机制继续保留，不能用绕过验证获得速度。

当前界面尚未增加模式开关，默认请求不会自动变成批量任务。结构针插与梯度法不自动改用此模式。第一阶段不承诺所有工作量加速。

## 串行路径也生效的评分准备

优化器为固定目标缓存 PreparedConditionScore 和 PreparedRmsWeights：尺度/权重校验及 frexp 分解、RMS 权重分母只准备一次；候选值仍逐项验证，RMS 保持原 frexp/ldexp/fsum 顺序，不改为普通平方求和。该准备作用于原 evaluate 路径，旧 score_condition/weighted_rms_error 保留为数值参考。验证要求最终结果与全部检查点哈希一致，不能以改变接受规则换取速度。

## Cross-vendor FP64 backend contract (2026-10-12)

`PerformanceOptions.backend` and `COAT_SIM_FILM_BACKEND` accept `cuda`, `rocm`,
`xpu` in addition to existing `auto`, `cpu`, `gpu`. Explicit vendor selection
never silently uses another vendor: unavailable runtime, unsupported FP64/complex128
operators or failed stack-local CPU comparison fall back to the CPU reference and
record the requested backend and reason. Generic `gpu` discovers CUDA/ROCm/XPU;
`auto` remains CPU without a matching measured profile. ROCm uses torch's CUDA API
but is identified by `torch.version.hip`, never rejected as a CPU-only CUDA wheel.

Device operations, synchronization and memory reporting use the selected runtime.
The same optical kernel and FP64/complex128 precision are retained. A real complex
operation probe and stack-local spectral parity gate precede selecting GPU output.
DirectML is not advertised as supporting this complex128 optical kernel; existing
real-valued deposition DirectML support is separate. Unsupported consumer devices
must report CPU fallback, not silently reduce optical precision. No driver/package
installation is performed by device discovery. Hardware acceptance remains separate
from mock dispatch tests; AMD/Intel hardware is not available on the current host.

References: https://docs.pytorch.org/docs/stable/notes/hip.html and
https://docs.pytorch.org/docs/stable/xpu.html .

A failed GPU initialization or numerical gate is latched for the lifetime of the
frozen evaluator: subsequent batches use CPU without repeatedly initializing a
broken device. A new evaluator/job retries. Cancellation is propagated and does
not become a GPU failure. Optional CLI `capability --live-probe --backend xpu`
performs a real device dtype probe; without `--live-probe` capability only reads
installed build declarations and does not initialize a device. Probe success is
not full job or performance validation.

### 使用与验收边界

- NVIDIA：`COAT_SIM_FILM_BACKEND=cuda`，安装环境需已有可用的官方 CUDA PyTorch。
- AMD：`COAT_SIM_FILM_BACKEND=rocm`，环境需已有适配型号/系统的官方 ROCm PyTorch；不承诺所有 Radeon 或 Windows 组合。
- Intel：`COAT_SIM_FILM_BACKEND=xpu`，环境需已有 XPU PyTorch，且设备能够执行所需双精度复数算子；不能把所有 Arc 都声明为可加速。
- 通用 `gpu` 发现当前可用运行时；默认 `auto` 没有匹配实测标定仍使用 CPU。
- 以上环境变量用于已显式请求 `execution_mode=deferred_batch` 的膜系优化作业；不是新增 UI 开关，也不改变沉积仿真设备选项。
- 沉积仿真已有 DirectML 实数 float32 路径，可用于其支持的 Windows GPU。膜系的双精度复数路径没有借用该近似。
- 官方 DirectML 数据类型列有 FLOAT64，但类型存在不等于本项目所需复数算子可用： https://learn.microsoft.com/en-us/windows/win32/api/directml/ne-directml-dml_tensor_data_type 。
