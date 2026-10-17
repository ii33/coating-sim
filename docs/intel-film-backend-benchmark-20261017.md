# Intel 膜系计算后端实测与交付（2026-10-17）

已修复实际 XPU TMM 超时，保留 FP64/complex128；核显单独执行并不普遍优于充分多线程 CPU。独立候选异构可行，但完整优化收益随规模和子集线程数变化且有冷启动、内存成本，暂不默认启用。

源码从发布 `23bba330dd461c98a54b493bb7584fec35e57473` 的 Git archive 独立导出。主仓库现有 `3fd62b9315c34a95dea5b1d1bc5cb3e06a2c7fbf / dev.20261017.03` 保留。本次整合基于3fd62b9，并保留既有助手/日志/入口清理。未修改安装器、驱动、安全设置或用户运行中的应用，未推送发布。测试全部在 sakuya 原生 Windows / PowerShell 7 中运行。

硬件为 Intel Core Ultra 7 265K（20核/20线程），Intel Graphics 8086:7D67，显示驱动32.0.101.9033。XPU运行时设备、版本、FP64能力和线程环境逐轮写入JSON；原始DXGI LUID对照沿用前次硬件证据。

## 实际超时定位及方案

逐个 ATen 算子完成后同步 XPU：复数 exp/mul/div 与 abs 已完成，`aten.pow.Tensor_Scalar` 输入 float64 [3,81]、指数 2 未返回；60 秒诊断超时后只终止该诊断进程树。原先 90/180 秒的失败不能算通过。证据：`tmm-trace.stdout.log`、`tmm-trace.stderr.log`、`tmm-trace.process.json`。这定位到实际触发算子，不宣称已确定 Intel/PyTorch 内部驱动缺陷。

将 `abs(z) ** 2` 改为 `m=abs(z); m*m`，同一个 FP64 幅值自乘；未改复数递推、临界层规则或精度。16 候选×81 波长×2 层完成时间约 3.606 秒（含首次上下文/算子初始化与逐算子诊断），最大绝对误差 9.99e-16；正常预热后毫秒级。证据：`tmm-fixed.stdout.log`。未采用大规模重写或实虚分离，因为最小等价修复已稳定通过。

DirectML 显式选 Intel index 1 / privateuseone:1，默认 index 0 是 NVIDIA。float64 基础exp探测返回正确且保持dtype/device，但这不足以证明完整FP64膜系算子链或排除内部回退；complex128 转换在隔离诊断进程中原生终止，退出码 3221226505，stderr 为 unsupported ComplexDouble。见 `directml-fp64-gate.*`、`directml-tmm-gate.*`。因此 DirectML 不能直接执行当前复数TMM内核；实虚分离的FP64 DirectML仍需另行逐算子精度、回退和性能评估，不能仅凭本次复数失败宣称其一切FP64不可用，也不以FP32跑分替代精度验收。

## 公平性与测量口径

同一被动弱吸收色散堆栈、37°、非偏振、400–800 nm、种子19，CPU和GPU均走冻结矢量化服务。CPU扫描 1/2/4/8/12/16/全部可用20线程，保留全部核心结果并对比最快线程数；NumPy递推无BLAS，OMP/MKL/OpenBLAS均为1。CPU产品默认仍1，显式最大值按进程可用CPU检测，不写死20。

候选/波长分块由既有总scratch及结果预算约束，未减少光谱或产品优化工作量。批处理单模式及独立冷进程单模式scratch64MiB；异构CPU/XPU各32MiB、总量64MiB。同进程基准的完整优化分支为复用对象，CPU单模式仍为32MiB、GPU为64MiB；因此完整优化冷进程表才是单模式总预算一致的证据。异构只并发独立候选，按原输入顺序拼接；优化代际依赖仍串行。异构CPU子集的最优线程数单独扫描；不能机械照搬整批CPU最优线程数。

首次调用（GPU另有数值核验、异构另有一次预调用）单列，随后计时重复3次；交叉点与异构子集复测7次。这是首次使用后的重复计时，并非每点均有额外3次预热。计时包括上传、前向同步、下载；resident上传及首次调用单列。eager模式显式图编译0秒；驱动JIT、首次内核初始化不能分离，均计入first-use，不能把0误写成驱动无编译成本。冷启动是新Python进程，保留现有驱动磁盘缓存，不清理系统缓存。

## 批处理结果

下表秒数为预热中位数。CPU列为线程扫描最优；括号是工作线程数。异构为该轮所测候选比例/线程组合最优，非全局最优承诺。

| 轮次 | 层/波长/候选 | CPU秒(线程) | Intel秒 | 异构秒 | CPU/异构 |
|---|---:|---:|---:|---:|---:|
| bench-small | 2/81/16 | 0.000305 (1) | 0.001382 | 0.001532 | 0.199 |
| bench-small | 2/81/128 | 0.001432 (1) | 0.001754 | 0.001652 | 0.867 |
| bench-small | 2/1001/16 | 0.002228 (2) | 0.002245 | 0.003776 | 0.590 |
| bench-small | 2/1001/128 | 0.013605 (4) | 0.011821 | 0.008010 | 1.698 |
| bench-small | 60/81/16 | 0.006831 (1) | 0.025114 | 0.027396 | 0.249 |
| bench-small | 60/81/128 | 0.022657 (2) | 0.031594 | 0.036277 | 0.625 |
| bench-small | 60/1001/16 | 0.033291 (4) | 0.038901 | 0.063757 | 0.522 |
| bench-small | 60/1001/128 | 0.136036 (8) | 0.231313 | 0.200037 | 0.680 |
| bench-large | 120/81/16 | 0.010091 (2) | 0.061165 | 0.070498 | 0.143 |
| bench-large | 120/81/128 | 0.046331 (2) | 0.061546 | 0.066199 | 0.700 |
| bench-large | 120/81/512 | 0.099685 (4) | 0.175074 | 0.128773 | 0.774 |
| bench-large | 120/1001/16 | 0.061080 (4) | 0.078031 | 0.122840 | 0.497 |
| bench-large | 120/1001/128 | 0.228075 (8) | 0.470258 | 0.379680 | 0.601 |
| bench-large | 120/1001/512 | 1.056599 (8) | 1.831354 | 1.267110 | 0.834 |
| bench-cross | 2/1001/128 | 0.013104 (4) | 0.011909 | 0.009187 | 1.426 |
| bench-cross | 2/1001/512 | 0.044123 (8) | 0.047680 | 0.050900 | 0.867 |
| bench-cooperation | 60/1001/64 | 0.078416 (4) | 0.123822 | 0.063934 | 1.227 |

60/1001/64 的七次复测中，最佳异构为 CPU48候选/4线程 + GPU16候选：0.063934秒，相对 CPU0.078416秒约1.227倍吞吐（约18.5%耗时下降）。同轮完整优化只快约5%，不能把单批收益直接当成完整优化收益。120/1001/512 的CPU为1.0566秒、GPU1.8314秒、所测最佳异构1.2671秒，证明共享带宽、调度/功耗竞争下异构会更慢。未测功耗传感器，具体物理成因仅是可能解释，不是已证实归因。同进程矩阵的峰值工作集是累计进程峰值，且GPU初始化先于CPU扫描，不能把它当作CPU独立常驻开销；模式内存比较以独立冷进程表为准。

## 完整优化与冷启动

Deferred DE，同种子19、种群64、3代、256次真实评估、相同目标误差/停止条件。所有记录的proposal trace SHA256和最优厚度一致，best key相差小于2e-12，failed=0，均正常iteration_limit完成。60层的固定小预算未收敛到零误差，其目标误差只作模式一致性比较，不宣称设计任务已求解。

| 层 | 模式 | CPU线程 | 冷优化秒 | 预热优化中位秒 | 峰值进程MiB |
|---:|---|---:|---:|---:|---:|
| 2 | cpu | 4 | 0.393573 | 0.041955 | 84.2 |
| 2 | gpu | 4 | 1.818952 | 0.030649 | 654.4 |
| 2 | hybrid CPU50% | 4 | 2.265215 | 0.028881 | 638.2 |
| 60 | cpu | 4 | 0.564748 | 0.279629 | 105.6 |
| 60 | gpu | 4 | 2.610845 | 0.527500 | 667.2 |
| 60 | hybrid CPU50% | 4 | 2.295016 | 0.299822 | 664.8 |
| 60 | hybrid CPU75% | 4 | 2.266504 | 0.238497 | 663.2 |

60层新进程CPU最优线程4、预热0.279629秒；全20线程约0.84秒，更多线程会因调度/内存竞争更慢。核显预热0.527500秒；CPU75%异构0.238497秒，CPU/异构比1.172，冷优化2.267秒对CPU0.565秒且进程内存约665MiB对106MiB。另一次同进程复测CPU0.330692秒、GPU0.546588秒、50/50异构0.314982秒，收益只有约5%。因此建议保留CPU默认，异构仅在重复、完整任务实测有效且会复用上下文时显式使用。

75/25完整优化的三次预热耗时为0.2385/0.2359/0.2904秒，第三次比该CPU基线还慢；中位收益不能隐去运行波动，当前不足以安装通用自动选择策略。

## 数值、设备、资源证据

定向回归54项通过。原生18组（s/p/非偏振、裸界面、吸收、倏逝、60层高反、120层弱吸收、1e5–1e6nm厚层、零厚度）最大绝对误差 2.45e-14，均保持 rtol/atol2e-12。1KiB/64KiB小scratch分块及取消门禁通过；精确临界层显式CPU回退。见 `precision-native.json`、`compute-tests-final.*`。

所有前向ATen输出设备审计为xpu:0，未出现CPU张量算子、标量pow或精度转换；Intel设备属性为8086:7D67、FP64=1、torch2.10.0+xpu，显示驱动32.0.101.9033。审计无法窥探驱动内部实现，但下列PID+Intel LUID的Compute引擎活动进一步证明Intel实际执行。

- bench-cross：Intel LUID 00000000:0000eac0，有效Compute采样3，峰值47.58%，共享显存峰值84.4MiB；剔除超出0–100%的状态码0异常值0个，保留原始JSON。
- bench-cooperation：Intel LUID 00000000:0000eac0，有效Compute采样10，峰值96.92%，共享显存峰值70.0MiB；剔除超出0–100%的状态码0异常值2个，保留原始JSON。

系统PDH采样较稀疏且跨CPU/GPU/异构阶段，不能用全程平均值代替每个毫秒批次的EU利用率。CIM GPU性能类首次查询未及时返回，已只清理任务自建监视进程；改用PDH得到可用样本。跨适配器枚举可能创建极小NVIDIA内存声明，不能当作其计算参与；前向设备及活跃Compute LUID均明确为Intel。CPU进程时间/占用核数、峰值工作集、torch峰值分配在各JSON。未获得每模式功耗/温度数据，不以模型推测替代实测。

60/1001/64的GPU前向段0.122019秒，候选上传0.000325秒，结果下载0.000542秒。前向段包含Python eager提交与XPU同步等待，不能当作纯硬件内核时间；传输不是该规模主要耗时。逐层、双偏振的多次eager调度是明确的进一步优化方向，但融合/偏振批维仍需独立数值和多机验收。

## 其他电脑与能力边界

这些是sakuya实测，其他Intel型号、旧驱动、低内存、笔记本功耗限制均未物理验收。CPU核数限制和小scratch仅为受限模拟。保留FP64能力门禁、堆栈局部数值核验、临界/近临界CPU路径、取消和现有内存预算；不对无FP64的Intel设备降精度冒充支持。DirectML/FP32属于另一个需独立误差评估的未来方案。

不新增窗口启动昂贵基准，不落地自动异构分配，不生成或安装GPU自动配置。既有auto无匹配测量保持CPU；kernel版本升级使旧profile失效；安装指纹含torch/numpy/scipy、可用CPU、Windows显卡/驱动声明，创建evaluator时读取一次避免每批开销。受限读取驱动时不允许跨进程复用。默认CPU线程1，用户可显式选择当前进程允许的线程，实际仍受候选数和总预算约束。跨平台驱动身份进一步完善、可取消短校准UI和多机验收属于后续工作。

## 复跑

在已安装numpy/scipy/torch XPU的独立原生测试环境运行；不要替换用户环境或驱动。先用小矩阵，再按已完成结果扩大。脚本不安装依赖、不写产品设置。PowerShell7示例：

```powershell
$env:OMP_NUM_THREADS='1'; $env:MKL_NUM_THREADS='1'; $env:OPENBLAS_NUM_THREADS='1'
# $python 指向已有隔离XPU环境。CPU冷测试只需要numpy/scipy。
& $python scripts/validate_intel_film.py --device-index 0 --output precision.json
& $python scripts/benchmark_intel_film.py --device-index 0 --output matrix.json --layers 2 60 --waves 81 1001 --batches 16 128 --optimization
& $python scripts/benchmark_intel_film.py --device-index 0 --output large.json --layers 120 --waves 81 1001 --batches 16 128 512 --optimization
& $python scripts/benchmark_intel_film.py --output cooperation.json --layers 60 --waves 1001 --batches 64 --workers 1 2 4 8 20 --hybrid-workers 1 2 4 8 --repetitions 7 --optimization
# 上行20仅重现sakuya证据；别的机器省略--workers让脚本扫描实际可用CPU，或提供合法数量。
& $python scripts/benchmark_film_cold_backend.py --mode cpu --workers 4 --output cold-cpu.json
& $python scripts/benchmark_film_cold_backend.py --mode gpu --workers 4 --output cold-gpu.json
& $python scripts/benchmark_film_cold_backend.py --mode hybrid --workers 4 --cpu-fraction .75 --output cold-hybrid.json
```

当前任务的`supervise.py`提供独立进程超时和只清理自建进程树；`.process.json`以returncode0且timeout=false为成功。各正常基准均已满足。超时仅为诊断保护，不是产品计算量上限；超时、原生崩溃和监视器失败均保留为失败证据。

官方背景：[PyTorch2.10 Intel GPU](https://docs.pytorch.org/docs/2.10/notes/get_start_xpu.html)、[torch.abs](https://docs.pytorch.org/docs/2.10/generated/torch.abs.html)。算子停顿及速度结论来自本次本机原始证据，不借官方支持列表承诺该桌面核显或所有用户电脑的加速。


## 产品接入边界

本次是数值后端与显式服务/基准接入，**没有把窗口 CPU 选择改成 XPU**。后端可通过 `PerformanceOptions(backend="xpu")` 显式调用；异构仅基准工具实现，产品不接受 `backend="hybrid"`，没有自动分配策略。

当前具体缺口：

1. `ComputeTarget.refresh()` 对膜系只发布 CPU，`client_for_submit()` 拒绝非 CPU；没有可绑定的 XPU index/UUID 行，也没有有期限的用户环境 FP64 探测。
2. `FilmOptimizationQuickController._build_request()` 沿用 `execution_mode="serial"`，没有批量执行选择。`deferred_batch` 仅支持固定拓扑差分进化，其代际更新语义有别于旧 immediate，不能因用户选 GPU 自动偷偷切算法或执行模式。
3. 冻结作业没有携带本地 XPU 物理设备绑定和回退策略；worker 批量路径仍靠既有环境变量选择后端。若只增加下拉框，CPU 选择/旧后台/环境变量可能与实际执行不一致，因此不足以安全接通。
4. 必须补队列能力版本、冻结的运行时/index/UUID 核对、用户可见的 CPU 回退策略、实际后端与原因持久化、取消及过期结果回归，并在当前安装解释器具备相应 XPU 依赖时验收。不得自动安装/替换用户环境。

本次明确报告这些缺口，保持 CPU 默认与所有既有窗口行为。后端 GPU 失败、临界或未支持模型会返回 CPU，并在服务结果 `metadata.backend` / `requested_backend` / `fallback_reason` 中标明；CPU 结果不是 GPU 通过证据。XPU 不报告 FP64 时在创建复数张量前拒绝；驱动身份不可读或缺失时不复用跨进程 GPU 校准。


## 补充：60层、10000离散目标（同日）

这里是 **10000波长 × 1角度（37°）=10000条R目标/候选**，不是10000个候选，也不因返回R/T/A而把残差数计作30000。候选参数向量为60个层厚变量；批量候选数另列。旧“10000候选”假设已作废，未执行该全量模式。

同一弱吸收色散模型、400–800nm、非偏振、种子19，CPU按1/4/8/12/16/20线程扫描，64候选选出8线程；单候选实际只用1个CPU工作线程。异构独立校准为CPU4线程处理43候选、Intel处理21候选。线程数与比例仅为这次测量，不成为产品配置。

| 模式 | 单候选预热秒 | 64候选预热秒 | 完整256次评估预热中位秒 | 冷优化秒 |
|---|---:|---:|---:|---:|
| CPU | 0.057506 | 0.603084 | 2.307644 | 2.724026 |
| Intel XPU | 0.038916 | 1.250357 | 4.791552 | 7.702974 |
| CPU+Intel | 不适用 | 0.472037 | 2.026303 | 4.753890 |

每模式独立新进程，批处理预热后计时7次；完整deferred-DE每模式1轮冷、3轮预热，种群64、3代/256次评估、目标/边界/停止条件一致。64候选CPU/异构吞吐比1.278，完整优化为1.139；核显单候选较快、64候选及完整优化反而较慢。不能把单候选或单批收益当作全过程收益。GPU导入/初始化/精度门禁在完整冷优化中计入，批次first-call不等于从程序启动的冷延迟。

最大光谱绝对误差约4.27e-14，保持rtol/atol2e-12。所有模式proposal trace和最优厚度一致、failed=0，正常iteration_limit结束；固定短预算不意味着60维设计已收敛。设备仍为同一8086:7D67及Intel Compute LUID，其他硬件与功耗没有新增验收。详细原始证据在本次私有验收目录的`TARGETS-10000-REPORT.md`及`targets-60-10000/*.json`；未将机器专用启动脚本纳入产品。本节归纳已有测量，不新增自动异构策略，也不改变上述产品接入缺口。
