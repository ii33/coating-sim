# CPU多核与GPU评估阶段报告

日期：2026-10-20。当前目录HEAD为`0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`，在真实未提交源码上实施；没有提交、推送或release。[输入框修复](film-optimization-input-report-20261020.md)单独交付，不计作性能成果。[取消应用阶段](film-optimization-cancel-apply-report-20261020.md)已完成核心、接口及原生按钮验收，最终静态收口独立记录。

## 已实现与冻结身份

从零合成的独立针探针与合法有限厚度增层候选使用run级headless进程池。父进程保持原候选顺序、排名、逻辑预算、归档、审计和停止条件；换材、删层、局部精修和审计仍串行。小任务/有限预算不合格批次保留串行；基础设施故障可以在原消费位置回退，物理错误不隐藏。实际Windows worker/launcher身份、Job Object回收、取消和线程/管道清理均有定向检查。

核心SHA：`a0fbe6366052e740727408e80e99ffdd17b78efa65b4bf2d653cdb8103dc8ace`；并行后端SHA：`286ca0328e00a9b923c7b0aebd3a5cabf7617517803d34cd08f858f5977ba0da`。矩阵manifest保留逐项当前源码SHA、HEAD、Python、硬件及参数；24次运行源码守卫均未变化，未使用历史实现。机器为Intel Core Ultra 7 265K、20核/20逻辑处理器、31.48 GiB内存；每worker数学线程为1。

## 实际硬件三次重复

空栈，450–900 nm/4001波长，0/30/60度，3种合成材料×8种增层厚度，24候选，显式1周期、评价次数预算null。仅改变cpu_workers；完整处方、分数、审计、选择轨迹和逻辑评价次数精确一致。

| worker | 端到端中位秒 | 串行时间比 | 最大采样进程树MiB |
|---|---:|---:|---:|
| 1 | 15.221 | 1.000 | 469.0 |
| 2 | 13.646 | 1.115 | 485.0 |
| 4 | 12.424 | 1.225 | 653.9 |
| 8 | 11.778 | 1.292 | 969.8 |

8 worker墙时减少约22.62%，没有线性倍增承诺。每次29次逻辑评价、84次训练物理调用、336084训练波长点；并行消费24、丢弃0，终态清理完成。峰值为0.1秒采样近似，历史单进程峰值不可相加为同时峰值。端到端包含启动、导入、初始化、IPC、审计、结果序列化及清理。

该固定工作量案例未达到R>=99.9999%宽带目标；最好1层，最坏审计R为0.19423525859，最大归一化差距0.80576374141。周期结束不构成物理不可行证明。另12次已知有解对照全部通过520–580 nm、R<=0.1%目标：1层匹配材料、厚112.268279878 nm，最坏审计R=0.000341135525442。小案例均按启发式串行，中位约0.78–0.80秒，不据采样抖动宣称加速。两案例为显式k=0的合成oracle，不冒充真实材料复现。

## 复现与原始文件

从当前仓库根目录使用当前venv；输出目录必须尚不存在：

```text
.venv/Scripts/python.exe tools/benchmark_film_synthesis_parallel.py --workers 1,2,4,8 --repeats 3 --case ar --output NEW_AR_DIRECTORY
.venv/Scripts/python.exe tools/benchmark_film_synthesis_parallel.py --workers 1,2,4,8 --repeats 3 --case throughput --sample-interval 0.1 --wavelength-samples 4001 --output NEW_THROUGHPUT_DIRECTORY
```

原始证据在`artifacts/film_synthesis_20261020/parallel-cpu-20261020/`：`ar-matrix-r1/`、`throughput-matrix-r1/`各含manifest/report/JUnit，以及逐run请求、结果、心跳/进度、内存采样、stdout/stderr和可用审计checkpoint；`performance-summary.json`汇总。正式计时期间暂停竞争测试并短时冻结源码。此前受影响回归246项+5个subtests通过，1/2/4一致性、有限预算、故障和真实Windows进程树清理有独立证据；新取消后端另有30项定向及2项既有CLI/worker回归通过，不混称为同一次全套。

## 取消与GPU边界

真实4 worker/16001波长任务在完整非空incumbent后取消，保留完整评分候选，取消触发到退出2.543秒，所有worker清理完成。未独立审计保持null/false；取消不追加审计。见`cancel-check-r2-evidence.json`。r1脚本错误地要求首次审计之前已有审计checkpoint，失败证据保留，按新契约修正了fixture，未改求解器迎合检查。

原项目Torch为2.10.0+xpu，Intel Graphics XPU live可用，该解释器未包含Torch CUDA。用户随后批准新建独立CUDA 13测试环境；官方2.10.0+cu130已在Temp隔离环境安装并完成RTX 4070 Ti SUPER真实设备小算，原XPU解释器未改。下段CPU/XPU数字来自此前探针，新的同口径CUDA对照另行记录。float64/complex128小算与现有前向batch精度通过。8层含吸收、24候选、4001波长的FrozenFilmEvaluator CPU/XPU R/T/A最大差约4.61e-15。非隔离观察的稳态中位CPU0.10992秒、XPU0.02856秒；XPU首调1.71123秒，CPU0.11059秒。首调包括runtime/context初始化；该数字不作为稳定收益结论。原始报告、脚本和JSON在`gpu-probe/`。N0的Jacobian、结构评分及串行审计仍为CPU路径，尚未证明整轮N0端到端收益，该探针阶段尚未开放产品GPU选择；后续N0集成已完成，实际整任务结果见[GPU阶段报告](film-synthesis-gpu-forward-report-20261020.md)，精度未降低。

## 瓶颈拆分：诊断计时，不代替正式速度矩阵

复用同一throughput输入和回调，在内存对当前synthesize内部同步函数加perf_counter计时，按同线程栈扣除已跟踪子区间。生产与harness源文件未改；38项实际headless源码前后/跨run稳定，两次语义SHA与原矩阵一致。分区之和分别等于真正synthesis墙时12.164/8.668秒，无重叠相加。此前cProfile原始累计归属不自洽，已保留原pstats与限制，下面不使用那些累计值。

| 不重复synthesis墙区间 | 1 worker秒 | 8 worker秒 |
|---|---:|---:|
| 裸栈评分 | 0.126 | 0.125 |
| 串行针探针3项 | 0.346 | 0.347 |
| growth评分/原序消费24项 | 3.581 | 0.250 |
| 串行holdout审计 | 2.764 | 2.548 |
| checkpoint JSON回调 | 0.856 | 0.820 |
| 审计归档其余工作 | 2.846 | 2.286 |
| 普通归档其余工作 | 0.308 | 0.240 |
| polish控制（本案未做多层精修） | 0.000 | 0.000 |
| 换材（本案未调用） | 0.000 | 0.000 |
| 其余准备/等待/关闭等 | 1.336 | 2.051 |

8 worker的growth 0.250秒是父进程evaluate原序消费/提交区间，不能解释成24项总计算；批次准备/部分等待落在其余2.051秒。该次8 worker的原始执行诊断为startup0.278秒、worker init0.000879秒、IPC累计2.098秒/27.17MB、worker计算跨进程和2.716秒；它们可重叠，不能再加到上表。pool启动不是此案例的主要开销。

synthesis返回后，转换、hash、JSON及结束回调另用2.095/1.872秒，其中result dump约0.815/0.792秒。checkpoint约46.3MB、result约49.1MB。审计归档其余工作包括候选/分数deepcopy、签名及通知，不能把整段冒称为纯deepcopy。此案例的主要剩余开销是串行审计、归档和完整证据序列化；局部polish仅空栈控制一次，尚不代表真实多层精修。原始完整区间、计时脚本与SHA见`profiling/phase-timing/`。

三周期代表性负载也已完成：4001波长、相同材料与目标、workers1/8各两次，评价总预算仍null，仅显式周期为3。1 worker墙时139.125/141.075秒，中位140.100秒；8 worker为88.165/85.665秒，中位86.915秒，串行/并行时间比1.612。采样进程树峰值分别437.5/957.8 MiB。每次404次逻辑评价、1203次训练物理调用、4,813,203个训练波长点；8 worker提交/消费204、丢弃0，全部清理完成。轨迹含4次refinement、5次deletion、12次material_replacement、63次needle_probe和144次growth_trial，不再以空栈polish代表多层精修。

四次完整处方、评分、审计与选择轨迹语义SHA均为`a9017df8296de0a1e72a1ecbf9e712ecad2b7b279e45f39c9d9b5b23ec64c5d4`。最好3层、归一化最坏差距0.7157434558852453，目标仍未达到；该有限周期结果不构成不可行证明。所有非UI coat_sim源码和两项harness/验收源码守卫稳定；期间独立UI取消状态文案曾修改，manifest如实记录，headless运行不导入UI。证据为`three-cycle-matrix-r1/`，复现命令：

```text
.venv/Scripts/python.exe artifacts/film_synthesis_20261020/parallel-cpu-20261020/run_representative.py --workers 1,8 --repeats 2 --wavelength-samples 4001 --output NEW_DIRECTORY
```

新性能/取消阶段未复用旧k/GUI阶段外部模型披露批准，尚无本阶段DeepSeek/Seed复审结果；已完成gpt-6.1-sol内部窄审，不能据此声称指定外部模型审查完成。

## 与沉积、几何光追、外观后端的边界

膜系Torch构建不代表整个项目的GPU能力。当前`core/simulator.py`自动沉积后端依次为CuPy CUDA、Torch DirectML、NumPy CPU；显式后端失败报错，自动候选失败才尝试下一项。`core/geometric_raytrace.py`独立几何光追为NumPy/CPU TMM。外观及其渲染光追由`appearance/gpu.py`的Mitsuba/Dr.Jit `cuda_ad_rgb`执行，CUDA失败不回退CPU，GUI使用独立`build/mitsuba_appearance_env/Scripts/python.exe`；当前该预期环境不存在，不能由此否定此前运行版本的4070渲染记录。

父任务提供的历史验收线索：10月15日沉积为CuPy 14.2/4070 Ti SUPER，显式和自动3/5/17掩膜与CPU一致且无回退，当时无Torch/torch_directml；10月14日外观为Mitsuba 3.9.0/Dr.Jit 1.4.0 CUDA，4096 spp对照约CPU173秒、CUDA4–4.5秒含初始化/JIT，当时图像精度验收尚未通过。这是历史记录归属，不是今日重新实测或当前启动设备确认。

## CUDA 13隔离环境及同处方前向实测

已按用户批准在`C:/Users/youai/AppData/Local/Temp/coating-cuda-eval-20261020`新建venv，Python3.13.16、NumPy2.5.4、SciPy1.18.1与原项目一致。安装官方`torch2.10.0+cu130`，运行时CUDA13.0；[PyTorch官方历史版本页](https://pytorch.org/get-started/previous-versions/)列出对应cu130安装入口。Toolkit13.2和驱动报告的CUDA13.4不是wheel运行时版本。实际设备为4070 Ti SUPER、capability8.9；float64/complex128在cuda:0分配/运算/同步成功。原项目Torch version.py前后SHA一致`D21716E2AD86C02E02B76A38B4E607755390BF4DD16BBF24DAB7ACFEA7F0EDE3`；驱动、系统CUDA与原XPU环境没有改。

暂停竞争测试后，现有FrozenFilmEvaluator使用同一8层含吸收处方、24候选、4001波长、400–800 nm、37°非偏振、semi_infinite、返回host R/T/A。每个端点先首调，再3次稳态；CPU/CUDA在隔离cu130解释器，CPU/XPU在原解释器，分别保留配对CPU基线。

| 端点 | 首调ms | 稳态中位ms |
|---|---:|---:|
| CPU（cu130环境配对） | 106.846 | 107.044 |
| CUDA RTX4070 Ti SUPER | 1613.264 | 9.184 |
| CPU（原XPU环境配对） | 108.904 | 112.483 |
| Intel XPU | 2092.366 | 27.781 |

CUDA三次稳态9.184/9.246/8.853 ms；XPU27.514/28.149/27.781 ms。均实际float64/complex128、无fallback。CUDA-vs-CPU最大R/T/A绝对差分别8.88e-16/2.33e-15/2.11e-15；XPU为2.05e-15/4.55e-15/4.61e-15，全部通过rtol=atol=2e-12。首调包括lazy Torch导入、设备/context、上传及既有CPU/GPU自检，因此不能将稳态比值冒称首轮或整轮N0收益。

稳态诊断中位（CUDA/XPU，ms）：厚度H2D0.106/0.191，前向含同步7.693/25.752，D2H0.270/0.544；resident材料上传1.212/1.520 ms是每evaluator首次成本，metadata重复携带不表示每轮重传。分项和不等于端点墙时，尚有CPU准备/调度/校验；不跨进程重复相加。CUDA/XPU峰值allocated约21.28 MiB，仅为框架观测，不是全部显存。

解析Jacobian另外调用真实`simulate_with_thickness_jacobian`，8变量、4001波长，R/T导数形状4001×8且全部finite；R/T与CPU batch逐值一致。首调150.369 ms、稳态140.589/133.929/130.955 ms，中位133.929 ms。该端点仍CPU；现有CUDA API没有解析Jacobian，本次没有用有限差分代替，该微基准不证明N0已接通；后续真实N0接入及完整对照另见[GPU阶段报告](film-synthesis-gpu-forward-report-20261020.md)。原始命令、安装报告、版本、元数据、首调/逐次稳态和SHA见`gpu-probe/README.md`及各JSON，汇总见`performance-summary.json`。

当前原项目venv另外已有Mitsuba3.9.0/Dr.Jit1.4.0。执行现有`python -m coat_sim.appearance.gpu --probe`已真实分配/求值/同步CUDA，返回available=true、gpu_cuda、cuda_ad_rgb，exit0（记录了不影响此CUDA探针的LLVM初始化提示）。这直接表明Torch XPU与Mitsuba CUDA可在同一解释器共存。日志为`gpu-probe/appearance-current-venv-probe.log`；仅微型能力探针，不是完整图像或精度验收。原venv无CuPy/torch_directml，而GUI外观要求的独立环境目录仍缺失；现有env包与GUI入口约定分别记录，未擅自改路由或安装它们。


## 软件交付收尾更新（2026-10-20）

用户已取消本项目 Seed 门禁；算法研究继续分轨推进，不等待研究全部结束才交付软件。已有功能在本地提交 c4d0e1ad7d2732fb6618658704e3960e9ec12392；后续限定收口修改单独提交，实际推送状态以交付记录为准，没有发布安装包。

在用户明确授权两份冻结包后，DeepSeek 实际源码复审均完成，core 381.64秒、GUI 312.00秒，exit0，无工具调用。模型提出的意见经完整源码和反例核实，不能把其严重度标签直接当已证实缺陷。必要修复为：物理 RuntimeError 进入既有失败终态；回调/检查点写失败保持原异常并清理资源；验证回退元数据保留已发生工作；unknown候选在批次所有者处仅计一次；界面区分无审计未知状态，清理未提交的失败作业引用，并明确历史只读、无候选终态和裸片基线。训练/独审双门真值、应用资格、默认预算及取消最好候选规则未变。修后没有再次外发新源码，不声称由外部模型执行了测试。

新增核心7项与GUI6项红绿验证通过；同一次联合回归127 passed、3 skipped，63.71秒，生产源码前后稳定。三项skip均要求显式受支持GPU runtime；另在独立CUDA13环境对修后源码实际跑通单层已知AR控制与缓存门复用：1层112.268279878nm，520–580nm有限审计最大R约0.000341136，目标R≤0.001，state=completed、validation_complete=true。该控制使用明确k=0的教学材料，不替代真实材料双窗结论；计时未用于速度比较。60层大矩阵没有重跑，之前性能/内存数字保留原冻结来源。

可选CUDA GUI窗口、探测/选择与typed请求冻结的R2实测保留；当前界面更改另由真实Qt定向回归覆盖。原XPU环境和CPU默认保留，启动/选择步骤见[GUI指南](film-synthesis-gui-guide.md#cuda-13-gui-启动可选)。软件推送、研究资料同步和安装包发布分开处理。
