# 隔离 Dr.Jit GPU 膜系精度与性能探针

日期：2026-10-13。实现仅在 `tools/validation/`；不修改生产 `render.py`、默认后端或 FP64 资格策略。此文是可执行探针契约；总体验收矩阵见 [gpu-precision-test-cases.md](gpu-precision-test-cases.md)，算法总览入口仍为 [algorithms.html](algorithms.html)。

## 输入、输出与资格范围

`TmmBatch` 的每批输入：

- 波长 `wavelength_nm[N]`，正有限 FP64，单位 nm
- 复折射率 `indices[N,L+2]`，`n+i*k`、`n>0,k>=0`，排列从入射端到出射端
- 有限内层厚度 `thickness_nm[L]`，非负 FP64，单位 nm；同批共享一组厚度
- 真实横向波矢比 `beta[N]=k_parallel/k_vacuum`，入射端透明且 `abs(beta)<n0`
- 偏振 `s`、`p` 或 `unpolarized`

输出为原始功率分数 `R/T/A[N,3]`、无效解掩码和内部精确 `q=0` 掩码。`T` 是进入出射半空间的通量；`A` 仅为有限膜层吸收。反向传播必须同时反转介质和厚度，并重新明确入射端；吸收入射端仍不支持。共同 beta 的互易用例与“共同角度”的双向渲染用例分开。

它是 TMM 内核实验，不是完整 Mitsuba BSDF，也不验证几何、空间插值、采样、光谱积分或图像一致性。没有任何自动生产启用。现有 PyTorch FP64 GPU 验证通过不能代替这个 Dr.Jit 探针通过。

## 同物理公式和设备执行

令 `q_j=sqrt(n_j²-beta²)`，取 `Im(q)>=0`、若虚部为零则实部非负的被动分支，时间约定 `exp(-i*omega*t)`。传播只用衰减的 `P=exp(i*2*pi*q*d/lambda)`；不形成增长的反向指数。

普通层按生产散射递推：

- s：`r_ij=(q_i-q_j)/(q_i+q_j)`，`t_ij=2*q_i/(q_i+q_j)`
- p：分母 `D=n_j²*q_i+n_i²*q_j`；`r_ij=(n_j²*q_i-n_i²*q_j)/D`，`t_ij=2*n_i*n_j*q_i/D`
- 从出射端递推：`Dstack=1+r_ij*r*P²`；`t=t_ij*P*t/Dstack`；`r=(r_ij+r*P²)/Dstack`
- s 通量因子 `Re(q)`；p 为 `Re(q*conj(n)/n)`；`R=abs(r)²`，`T=abs(t)²*max(0,flux_exit)/flux_incident`

内部精确 `q=0` 使用生产 `_critical_one` 的归一化 E/H 解析极限。s 导纳为 `q`；p 为 `n²/q`。每层衰减传播矩阵对角 `(1+P²)/2`，非临界非对角 `(1-P²)/(2*y)`、`(1-P²)*y/2`；临界 s 上项 `-i*2*pi*d/lambda`、下项零，临界 p 上项零、下项 `-i*n²*2*pi*d/lambda`。每层按 E/H 最大模归一，独立跟踪衰减幅度因子；p 出口 `q=0` 用有限场极限。GPU 用设备掩码合并两条路径，安全分母只防止未选中分支制造 NaN；选中分支真退化会被明确标为无效。

```text
incident port -> layer 1 -> ... -> layer L -> exit port
                     P_j only decays
regular path:       <- scattering recurrence <-
internal q=0 path:  <- normalized E/H limit   <-
per-ray mask selects the correct original R/T/A
```

图示：所有设备 lane 同时处理不同波长/入射条件，只有固定层数和偏振在 Python 中循环；没有逐光线 Python 解算。保守实现同时追踪普通和临界分支，计时包含这部分开销，不代表后续优化 BSDF 的上限。

使用明确 `drjit.cuda.Float64/Complex2f64` 和 `drjit.cuda.Float/Complex2f`；LLVM 对照用同一套公式、同精度的 `drjit.llvm` 类型。Mitsuba variant 仅记录，绝不通过切 variant 来宣称光学计算在 GPU。计算图追踪和求值全程显式 `JitFlag.FastMath=False`，浮点常数 pi 按所选精度存储并记录；FP32 常数舍入计入解算误差。只匹配生产 `abs(A)<5e-13` 的残差置零，不 clip R/T/A、不归一化吸收。

## 指标与固定候选判定

以下均为本项目约定，尚不是获批的 FP32 生产误差预算，不引用外部来源暗示已认证：

- 每个 R/T/A：最大、p99、RMS **绝对误差**，不使用近零相对误差
- 候选最大绝对差：FP64 `2e-9`，FP32 `2e-5`；这是初筛值，不能自动放宽，也不能用它宣布与旧 FP64 等精度
- 另外逐项报告既有栈回归门限 `abs(delta)<=2e-12+2e-12*abs(reference)` 的通过/失败，独立于上述宽候选门限
- 非有限值/无效解必须为零；量化后非法入射行必须为零
- 独立报告生产 BSDF 门限：R/T/A 最低 `-1e-9`、最高 `1+1e-8`、功率和绝对差 `2e-7`
- 参考出口零通量时，设备 T 必须硬零；偏振平均和共同 beta 反向透射逐项报告
- 最大误差失败行附具体波长、beta、原始参考和设备结果。完整原始数组保存到同名 `.raw.npz`，JSON 记录文件 SHA256 与键

`A=1-R-T`，因此 `R+T+A≈1` 是构造关系，不是独立物理准确性证据。即使 balance 通过，只要原始 R/T/A 越界、逐项 CPU 误差失败、TIR 分支错误或非有限，仍拒收。候选容差未通过是有效实验结果；运行/模式/设备 launch 失败才返回非零运行退出码。缺失必需覆盖不能宣布完整资格通过。

误差明确分账：

1. 原始 FP64 物理输入的 CPU 解 vs 设备解：端到端总误差
2. n/k、波长、厚度、beta 量化到所选精度后再升 FP64 的 CPU 解 vs 设备解：相同量化输入的算术误差
3. 量化输入 CPU 解 vs 原始 CPU 解：输入量化误差；非法行单独计数而不删去失败
4. 原始方向余弦量化 FP32 后，**先升 FP64 再 acos/cap89.999**，仍用 CPU FP64 解：单独方向量化诊断

第4项没有把先 FP32 acos 后升 FP64 冒充等价计算。直接 FP32 beta 在 89.999°附近可能舍入为 n0、令入射通量为零，探针会显示失败，不另改角度 cap 来掩盖。

## 覆盖与已知未覆盖

合成固定集：405–830 nm、86 个波长；0/60层，双向、全偏振；一般角度 0/30/60/80/89/89.999°；透明/吸收膜、吸收出口、强吸收厚层下溢；内部精确 q=0、相邻双临界层、零厚度、出口临界/TIR；beta nextafter 上下侧；60层普通偏置临界邻域；共同 beta 反向互易；Brewster 精确角和 FP32 两侧邻点。1.5→1.0 裸界面临界角另明确覆盖 FP32 度值 41.8103141784668、41.81031799316406，以及 double 临界角 41.810314895778596。前一点 s 的 CPU T≈5.9843e-4，后一点 T=0。

`--appearance-job` 接受 **validate_job 直接接受的 schema_version=1 JSON**。封装 spec/frozen request 须在隔离目录显式导出其中 job 后再传入，工具不会猜格式或静默换成合成 n/k。调用现有验证/材料插值，保留真实 n/k、显式 lossless 假设、数据源与原输入 SHA256。每个有膜面从共同有效的捕获网格节点选最近几何中心、最小总厚度、最大总厚度三点，逐点明确坐标/层ID/全部厚度；按实际 substrate→outward 层序测试两侧入射，并加可访问的透明内层/出口临界邻域。无膜背面仍测试真实裸界面。三点可能重合；它们不能代替全空间误差界。

当前离线参考已经显示一项不可隐藏的边界风险：透明 `[1.5,1,1.5]`、p偏振、beta在1两侧double nextafter、100/270nm层时，生产CPU参考本身出现A约 `-1.86e-9/-1.63e-9` 的消去残差，越过 `-1e-9` 生产下限；balance仍约 `4.44e-16`。JSON单独计数CPU参考的生产门槛失败，不将这种情况改为“通过”或静默放宽门槛。它是近临界数值保护仍需审查的证据，不能归因成单独GPU错误。

Z6 的**窄共振密扫定位、中心/FWHM/峰谷高度变化**本版未覆盖；86 点粗谱不能排除漏峰。Brewster 小值已覆盖，但不因此宣称 Z6 完整。真实 ZnS/YbF3 60层资格必须提供冻结真实 job，保留其 ZnS n-k 与 YbF3 n-only 的明确策略；合成60层和透明四分之一波夹具都不能替代。

## 公平性能实验与证据

默认 batch 为 256、8192、65536；重复5次，可配置，没有墙钟计算预算。0/60层普通非偏振样本用于计时，排除 exact critical 和超过80°输入，避免生产 CPU 标量临界 fallback 扭曲普通内核速度比。

对同一 batch 输入 hash：

- CPU 必须调用当前生产 **NumPy 波长批量** `coherent_rta`，不与旧逐光线 Python scalar 计时混比
- 驻留输入 GPU/LLVM时间包含新追踪、eval、同步；每次释放旧结果，必须有新的 JIT launch。另报上传→解算→下载→同步的端到端时间
- 所有计时边界均 `dr.sync_thread()`，设备 history 记录真正 CUDA/LLVM 后端、JIT source/hash、f64/f32 token、执行时间与编译/缓存状态
- NumPy/GPU墙钟速度比明确叫“实现吞吐比”，不能叫GPU纯算力倍数；同一图、同精度 LLVM/CUDA 的 device timer 比另列
- 记录首次用例含上传/冷追踪/下载总耗时、是否观察到真实 backend 编译；benchmark另记录清除内存cache后的首次求值。清内存cache不清磁盘cache，若 cache_hit=true 则不能称冷编译
- 冷启动可用全新临时 HOME/CUDA_CACHE_PATH 启动一次，避免已有用户缓存污染首次用例；history证据仍是最终判据，工具不删除用户的缓存

不会将内核加速比套用整单渲染52秒基线，或宣称variant切换等于真实GPU BSDF加速。记录硬件、驱动、Python/DrJit/Mitsuba、当前variant、CPU线程环境与实际DrJit线程数。远程测量须在既有CPU验收完成后独占资源。

## 运行与阶段验证

本机无DrJit不安装，只做离线参考和方程检查：

```bash
python -m pytest tests/test_gpu_tmm_probe.py -q
python tools/validation/gpu_tmm_probe.py --reference-only --no-benchmark --output /tmp/tmm-reference.json
```

远端已有 Python3.12/Mitsuba3.9/DrJit1.4/CUDA，先做最小真实launch/精度检查，再完整suite；均在仓库根目录使用已存在的远程虚拟环境 Python：

```bash
python tools/validation/gpu_tmm_probe.py --case zero_layer_exit_critical_exact --backends cuda --no-benchmark --output /tmp/tmm-cuda-smoke.json
python -m pytest tests/test_gpu_tmm_probe.py --run-gpu -q
```

完整精度+公平性能+真实冻结job示例：

```bash
fresh_home=$(mktemp -d /tmp/drjit-tmm-home.XXXXXX)
HOME="$fresh_home" CUDA_CACHE_PATH="$fresh_home/cuda-cache" python tools/validation/gpu_tmm_probe.py --appearance-job /absolute/path/actual-appearance-job.json --backends cuda llvm --precisions float64 float32 --llvm-threads 20 --batch-sizes 256 8192 65536 --repeats 5 --output /tmp/tmm-gpu-full.json
```

JSON与同名 `.raw.npz` 必须都保留。输出已存在会拒绝覆盖。`--no-synthetic` 可只跑真实job，`--case` 可重复选择名字包含片段的用例，`--no-benchmark` 只跑精度。只打印一个JSON摘要；完整详细证据在输出文件。reference-only 的成功不意味着CUDA通过；离线 NumPy equation adapter 仅检查公式移植，真实GPU测试按 `real_gpu` 门禁默认跳过。

## 参考

- 公式真值与单位/通量/临界语义：[生产 film_optics.py](../coat_sim/core/film_optics.py)，本探针未另改物理模型
- 薄膜被动复折射率、偏振通量与问题域：[Byrnes, Multilayer optical calculations, arXiv:1603.02720](https://arxiv.org/abs/1603.02720)
- 数据并行追踪/JIT：[Jakob 等, Dr.Jit, 2022](https://arxiv.org/abs/2202.01284)
- 明确复数与CUDA类型：[Dr.Jit array types](https://drjit.readthedocs.io/en/latest/types.html#complex-numbers)
- FastMath开关、eval/sync及真实kernel history/device timers：[Dr.Jit 官方API](https://drjit.readthedocs.io/en/latest/reference.html)

候选误差门限、抽样点、batch大小、重复次数均为本项目约定；外部来源不证明这些选择满足产品资格要求。

## 2026-10-14: close-camera clipping and scalar eta regression

A synthetic sakuya comparison exposed a scene-construction defect: the scene uses
metres, but the perspective sensor inherited Mitsuba's default `near_clip=0.01`.
For a camera 10 mm in front of the substrate, primary rays therefore started on
the front interface. Scalar and CUDA intersection rounding could put the ray on
different sides. The native dielectric reproduced the discrepancy, separating it
from the custom thin-film BSDF and the TMM kernel.

The shared CPU/GPU scene now sets `near_clip=1e-6` m explicitly. This is a one-
micrometre near plane for the current millimetre-scale exterior appearance camera;
it is not a claim to support cameras on a surface or sub-micrometre close-ups.
The scalar BSDF also now returns `bs.eta = n_transmitted / n_incident`, as Mitsuba
expects for path throughput/Russian roulette. Its existing radiance weight uses
the inverse ratio squared and is unchanged. This independent eta defect did not
explain the main scene discrepancy.

Diagnostic evidence (8x6, 1024 spp, 500 nm, seeds 3 and 703, circular radius 2 mm,
12 mm substrate, eight synthetic layers): native uncoated scalar/CUDA NRMSE was
0.016771 / 0.017921 after explicit clipping; coated with corrected scalar eta was
0.016710 / 0.018495. Before clipping, seed 3 was 0.578504 native and 0.782348 coated.
Primary rays started in air and all 12 lens pixel centres first hit the front disk.
Remaining error is comparable to this small experiment's seed variation; full-
spectrum and larger-image numerical acceptance remain pending. These diagnostic
runs did not change the sakuya production source tree.

Offline regression coverage: `tests/test_appearance_camera_eta.py` exercises both
circle/rectangle shared sensor construction and scalar reflection/transmission
eta/weights from both sides in radiance/importance modes. It uses a fake Mitsuba
API and is not a substitute for actual GPU tests.

References: [Mitsuba sensor parameters](https://mitsuba.readthedocs.io/en/stable/src/generated/plugins_sensors.html),
[Mitsuba BSDF contract](https://github.com/mitsuba-renderer/mitsuba3/blob/v3.9.0/include/mitsuba/render/bsdf.h).

The subsequent full-spectrum diagnostic (400–700 nm at 10 nm spacing, otherwise
same 8x6/1024 spp scene) yielded XYZ NRMSE 0.011497 and 0.011781 for seeds 3/703,
versus the original 0.740756 and 0.738645. CPU/CUDA mean Y was 14.95487/15.01097
and 15.05894/15.00656 respectively. Complete process times were 43.11/3.91 s and
70.79/3.68 s; these small synthetic runs are not a general performance claim.
All 62 GPU wavelength diagnostics passed. CPU/CUDA within-backend two-seed
variation was 0.007871/0.005240; the two-seed mean-image residual was 0.010385.
Full-spectrum acceptance is therefore still pending a sampling-convergence check;
the major clipping defect is fixed, but these results do not establish numerical
equivalence across renderers. Production sakuya source remained hash-identical.

A bounded follow-up increased sampling to 4096 spp for the same 8x6 full-spectrum
scene and seeds. Paired XYZ NRMSE was 0.009737/0.009534; CPU/CUDA seed variation
fell to 0.003878/0.002754, while the two-seed mean-image residual remained 0.009270.
The sampling noise approximately halved, but the mean residual improved only
about 11%. These two seeds do not establish statistical significance or renderer
acceptance, and the remaining difference cannot simply be assumed to be noise.
Complete process times including initialization/JIT were CPU 173.71/172.84 s
and CUDA 4.01/4.52 s. All four processes exited normally; all 970 production
source hashes remained unchanged. The bounded diagnostic finished, with no
larger rendering matrix started. Raw report, summary and reproduction script are
in sakuya's `gpu-spectral-convergence-4096-26c501ed` diagnostic directory, outside
the production checkout. This is synthetic diagnostic evidence, not a benchmark
for a production-size image or an optical imaging qualification.
