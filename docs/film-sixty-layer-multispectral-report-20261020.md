# 实际60层多光谱：CPU多核 / CUDA 13受控对照

日期：2026-10-20。状态：四次完整流程（两次反序配对）、两次前向诊断和两次取消均已完成，四份CPU/CUDA完整60层最终结果精确相同；独立离线逐点复核通过。该文件不把预测资源或只读审查当作数值验收结果。N0核心/界面已完成的三层、八层整任务对照另见[GPU阶段报告](film-synthesis-gpu-forward-report-20261020.md)。

用户指定“60层多光谱”。下列具体波段、材料、采样、目标和优化限制是本项目本次测试选择。使用现有物理结构生成、固定拓扑评价、解析TRF和留出验收；是实际60层起点和始终60层的评价/精修，不是从N=0长成60层，也不是论文精确复现。

| 冻结项 | 本次参数 |
|---|---|
| 真实物理层数 | 60；Ge/BaF2两段各15对，中心4/10µm；ZnS基片 |
| 基础处方厚度 | 总49225.871283nm；单层238.359136–1761.804087nm |
| 连续目标窗 | 3000–5000nm、8000–12000nm，共2窗 |
| 训练波长点 | 每窗1001，两个窗不补间隙目标 |
| 角度 / 偏振 | 0°/30°/60°；s/p，共6组 |
| 共同目标 | 各窗、各偏振R≤0.0099且T≥0.9001，8个objectives |
| 条件与计分点 | 每候选12012独立波长-角度-偏振点；当前R/T分别评价，共24次objective-angle调用/24024计分点 |
| 候选矩阵 | 固定64×60；PCG64 seed20261020，第0行为基础处方，其余为[0.8,1.2]比例厚度扰动 |
| CPU端 / GPU端 | 真实8进程OrderedEvaluationExecutor / 同序SynthesisForward CUDA；候选阶段均不算Jacobian |
| 精修 | 两端同CPU解析TRF、全60变量、正bounds=基础厚度的[0.5,1.5]；最多3轮/6次训练评价 |
| 留出 | 两端同CPU每窗2001点，非连续证书 |
| 精度 | float64/complex128，保留既有CPU/GPU数值门；实际结果另做2e-12逐点对照 |

源n/k复用既有 `exploration_v3/ge_source_joint_seeded_direct.request.json`。Ge为Amotchkina e-beam薄膜，BaF2/ZnS为Querry块材代理，各源条件不同，没有目标制造工艺匹配证据；全部来源归属和源hash保留。不改材料库或凭据，不将未知k当零，不回退到k=0来通过测试。所有训练网格strict源域预检通过，已知k保留。

证据根目录：`artifacts/film_synthesis_20261020/gpu-forward-20261020/sixty-layer-multispectral/`。`prepared-input.json`包含完整60层处方、物理结构参数、冻结n/k及全部typed请求字段，SHA256 `8dcd65634be022a17ec39a54c17dd73f1fce7eb999b405f34dc793971af92190`；`candidate-thicknesses.npy` SHA256 `a40a70ed0dfb5641db0585f880d8b80a1e2ce2eb3aef789e069ffcdb50a9bbd5`。完整约束见[实施契约](film-synthesis-gpu-forward-contract.md)。

正式完整流程各用新子进程，先完整CPU baseline，再64候选、原序选best、CPU解析精修及同CPU留出。基线另1次，TRF≤6次训练、≤1次留出，最多72个完整候选评价等价（实际物理调用另记）。评价枚举由harness硬限制64，底层request cap=None只为允许同批执行；没有无穷生长。

前向诊断另开新子进程，CPU串行vector batch与CUDA分别首调+3次稳态，保留12个固定条件上下文。这是显式resident诊断，与生产LRU4不同，不能用来代替CPU8或N0完整流程收益。正式端到端不提前运行微基准预热。

运行前资源预测：批量R/T/A约35.19MiB、全残差Jacobian约11.0MiB，额外评分对象/IPC/临时数组/Torch runtime另计，预计总进程树RAM<8GiB、框架显存<1GiB。主机实际31.48GiB RAM、当时16.27GiB可用；父子真实RSS预检通过。以上预测不代表实测峰值。正式采集RSS、native working-set、Torch allocated/reserved及整卡nvidia-smi显存，整卡值包含显示/其他进程，不能等同任务独占内存。

取消用例与完整计时分开：先提交真实完整60层baseline，再在64候选真实计算进展后外部取消；保留完整best，半成品/未消费尾项不提交，取消不追加验收。仅清理自有进程树。心跳只表示活性，数值进展单独用于600秒无响应检测；资源与墙时基础设施保护触发原因均写入manifest，不作物理不可行结论。



首次CPU r1为保留的失败历史：实际完整60层baseline通过、64批开始，在首个compact worker结果导出时因误读已裁去的responses字段停止，未形成有效paired结果。新脚本统一从完整score.errors.actual提取原24024点并核顺序，无额外TMM，SHA256 `0f9cb8a938d74e13bf295ff6e77e3556c017a9b97f4f2fa128e2448367fea919`；CPU r2及后续CUDA共用此修正。生产源、材料/矩阵和预算未变，旧进程均已退出。失败10.88秒不计有效性能比较。

## 完整流程实测

CPU为Core Ultra7 265K，真实8个headless worker PID，未回退串行；GPU实际为RTX 4070 Ti SUPER，UUID `d3d20d99-597c-5de6-ecec-487f20bde100`，Torch2.10.0+cu130、runtime CUDA13.0、float64/complex128。两端同隔离解释器/依赖、同修正child、同输入及矩阵，没有改原XPU环境或产品默认预算。

| 完整流程时间（秒） | CPU8 | CUDA |
|---|---:|---:|
| 外部spawn至观察退出 | 75.831 | 45.389 |
| child数值流程 | 74.324 | 43.233 |
| 额外完整CPU基线 | 4.033 | 4.005 |
| 64候选评价/原序消费/导出 | 38.328 | 8.588 |
| 共同CPU解析TRF及留出 | 31.591 | 30.319 |

首对外部时间比1.6707、CUDA减少约40.14%。随后以CUDA→CPU反转顺序，按同一冻结输入/预算/代码再跑一对：CUDA46.506秒、CPU8 77.965秒。两端各两次外部中位数为CPU8 **76.898秒**、CUDA **45.948秒**，比值**1.6736**、CUDA墙时减少约40.25%。相反顺序的两对均观察到收益；样本每端两次，结论限于此固定60层受控流水线。计时包括不同批处理/IPC、既有数值门、CPU评分、观测顺序校验及文件导出，不是纯GPU硬件比值，也不代表GUI/N0从零长到60层的时间。spawn计时与资源预检/首次整卡基线查询分开；退出检测含监控采样和nvidia-smi查询粒度。正式流程先运行，未先用微基准预热。

四次实际局部精修均为3轮、5次训练评价，`iteration_limit`，另1次留出；实际为baseline1+枚举64+局部5=70次完整训练网格候选等价，另留出1。框架state=completed表示流程完成，targets_met=false。处方始终60层，最终总厚48067.169293nm。

| 实际物理工作 | CPU8 | CUDA流程 |
|---|---:|---:|
| 完整基线CPU调用/点 | 24 / 24024 | 24 / 24024 |
| 64候选调用/点 | CPU1536 / 1537536 | GPU1536 / 1537536 |
| 局部CPU解析调用/点 | 96 / 96096 | 96 / 96096 |
| 局部CPU普通forward（含留出）调用/点 | 48 / 72048 | 48 / 72048 |
| 额外数值门CPU调用/点 | 0 / 0 | 36 / 36036 |
| 额外数值门GPU调用/点 | 0 / 0 | 36 / 36036 |

局部48次普通forward中含24次2001点留出、24次1001点训练；96次解析调用均为训练。分项计量，不把数值门或审计重复加到训练，也不把跨进程累计compute/IPC秒数相加当墙时。CPU候选池提交/返回/消费64、丢弃0、unknown0、cleanup=true；GPU也是64/64/64，丢弃0、unknown0、closed=true。

## 同指标数值结果

独立读两端全部64×24024 R/T观测矩阵，全部有限，`rtol=atol=2e-12`通过，最大绝对差 `3.3084646133829665e-14`；前向微基准数组也通过同门，最大差相同。该离线数组仅含目标R/T，R/T/A的既有运行时数值门另有metadata，未冒称离线额外验证A。

四次都选择候选33，最终60层处方、完整局部结果（含全部训练/验收逐点行）精确相同。四个64×24024数组均对首CPU通过同门；完整local-result的canonical SHA均为 `d9001f0526ffe249bd81d8c2d561f7da2d292c9d28ba2e288dfb6cd8a1fcbcf0`。根任务完整quality SHA为 `7bf55d896f1e5edc81eeb12bcc95f566a67d55e49028802218cf6784cb828c57`。最终最大归一化差距 `0.9886784054023782`、训练weighted RMS `0.9136407640367898`，目标未达到。最坏R=0.9985784054023782，位置10168nm、60°、s；上限为0.0099。本处方作为大负载测试起点，不能称达标宽双窗AR设计；显式3轮结果也不是不可行证明。

| 2001点/窗、三角度联合验收 | 偏振 | 最大R | 最低T |
|---|---|---:|---:|
| 3–5 µm | s | 0.995680756 | 5.122e-23 |
| 3–5 µm | p | 0.982852197 | 2.126e-20 |
| 8–12 µm | s | 0.998578405 | 5.592e-17 |
| 8–12 µm | p | 0.997391120 | 2.657e-14 |

共同门为R≤0.0099、T≥0.9001，上述四组均未通过。所有结论限于有限模型采样，源工艺不同且未作实验验证。图为已保存验收值的离线绘图，无新增TMM：

![60层双窗口最终有限采样R/T](../artifacts/film_synthesis_20261020/gpu-forward-20261020/sixty-layer-multispectral/final-audited-spectra.png)

## 首调和稳态前向诊断

| 同64候选/24条件的纯前向（秒） | 首调 | 后三次 | 稳态中位 |
|---|---:|---|---:|
| CPU串行vector batch | 2.777406 | 2.607347 / 2.618198 / 2.608474 | 2.608474 |
| CUDA | 2.418281 | 0.326469 / 0.330419 / 0.347191 | 0.330419 |

稳态比约7.89倍。首调含该进程实际Torch/context/准备和数值门；重复显式保留12条件上下文，属于resident诊断。它不是生产LRU4的重复工作轨迹，也不是CPU8完整流程比值。每次24个objective-angle调用×64候选×1001点=1537536目标观测点，R/T重复物理计算如实保留；4次工作与完整流程分开。

## 实测资源与取消

| 案例 | 采样进程树RSS峰值 GiB | Torch allocated/reserved峰值 MiB | 整卡显存峰值 MiB |
|---|---:|---:|---:|
| CPU8完整（两次最大） | 0.853 | 未初始化CUDA | 896 |
| CUDA完整（两次最大） | 1.001 | 22.12 / 40.00 | 1155 |
| CPU串行forward | 0.208 | 未初始化CUDA | 896 |
| CUDA resident forward | 0.865 | 44.80 / 64.00 | 1179 |
| CPU8取消 | 5.917 | 未初始化CUDA | 896 |
| CUDA取消 | 0.879 | 22.12 / 40.00 | 1155 |

整卡运行前基线均896MiB/总16376MiB，包含DWM和其他进程，不是任务独占显存；Torch数值为框架allocator。RSS为实际采样最大值，不是所有瞬时峰值保证。CPU取消阶段显著高于完整run，另有逐进程native working-set高水总和约9.07GiB；这些进程各自高水不一定同时发生，不能叫同时RSS峰值。预估资源与实测分开，没有用完整run较低峰值掩盖取消阶段。

两例取消均先核完整60层baseline，再实际64批进展，外部取消文件触发，最后state=cancelled、保留完整60层候选2、未做精修/验证/额外审计，targets的未知审计状态为null/false。CPU实际提交/返回16任务，消费8、丢弃8，后8任务取消，实际240物理调用/240240点，unknown0；executor清理完成、无active worker。CUDA已计算64个完整候选（1536调用/1537536点），父流程消费3、丢弃61、unknown0、closed=true。该CUDA案例说明已完成批量结果的协作取消和尾项处理，没有证明可中断运行中的kernel；核心partial-chunk取消另见GPU阶段控制。

从请求所在监控轮开始到观察退出，两端均约1.08秒，含该轮采样/SMI查询及退出检测粒度；不冒称精确UI输入延迟。两端均未触发强制清理、资源阈值或无响应阈值。

## 证据和当前边界

八个有效case均exit0、实际设备匹配，156项源/输入守护前后无变化（其中139个core递归Python文件包含2个RII vendor工具；实际生产core137项）。完整输入/矩阵、stdout/stderr、逐阶段event/独立心跳、实际PID、RSS/native高水、整卡显存、allocator、checkpoint及完整逐目标行均保留。初次CPU r1失败单列，不计有效paired性能；修正只导出已算评分，没有改物理核心。

综合机器可读报告 `sixty-layer-final-report.json` SHA256 `1b2ade6cf3be89141c73701210ab86c36a88e78cf6e6374a056c1fe810cc7cfc`；反序重复另存 `repeat-report-r2.json`，SHA256 `3c9781b63e72d1b552ff10c5562ab57d1218ce3603066f24e7a1da9fb4af67a2`，没有覆盖首份报告。根任务独立离线复核为 `root-final-quality-comparison.json` 和 `root-repeat-quality-comparison.json`。逐case目录分别为 `cpu8-pipeline-r2`、`cuda-pipeline-r1`、`cpu-forward-r1`、`cuda-forward-r1`、`cpu8-cancel-r1`、`cuda-cancel-r1`，反序重复为 `cuda-pipeline-r2`、`cpu8-pipeline-r3`。固定处方/矩阵和child/monitor命令记录在runner-contract，重现时输出必须用新目录；CUDA解释器为获准Temp隔离环境。

本阶段只在当前最新版实现，未合并旧仓库、未推送或发布release。GPU Jacobian、远程GPU、ROCm硬件实测和指定外部模型审查尚未完成；本案例没有当前环境或权限阻塞。CPU仍为产品默认：小型三周期N0案例CUDA慢6.82%，八层配对差异不足1%，本60层固定拓扑才观察到明显描述性收益，不设置未经校准的自动GPU路由。

当前源码入口已只读核验：`artifacts/film_synthesis_20261020/bounds-input-20261020/CoatingSim-current-input-fix.lnk` 指向本目录 `.venv/Scripts/pythonw.exe`，参数为本目录 `launcher.py`，工作目录也是当前项目；目标存在。原环境XPU实际GUI→CLI闭环已通过，Temp CUDA环境没有替换产品解释器。README和GUI指南保留入口。HEAD仍为 `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`；当前未发布新安装包。

本阶段实际模型分工为复杂核心/只读审查gpt-6.1-sol、常规接口/监控Luna，没有使用GPT-6-sol或Astra。指定DeepSeek/Seed外部复审本阶段未执行，未复用旧范围源码披露授权；这一缺口与已完成的内部审查、实际数值验收分开记录。


## 软件交付收尾更新（2026-10-20）

用户已取消本项目 Seed 门禁；算法研究继续分轨推进，不等待研究全部结束才交付软件。已有功能在本地提交 c4d0e1ad7d2732fb6618658704e3960e9ec12392；后续限定收口修改单独提交，实际推送状态以交付记录为准，没有发布安装包。

在用户明确授权两份冻结包后，DeepSeek 实际源码复审均完成，core 381.64秒、GUI 312.00秒，exit0，无工具调用。模型提出的意见经完整源码和反例核实，不能把其严重度标签直接当已证实缺陷。必要修复为：物理 RuntimeError 进入既有失败终态；回调/检查点写失败保持原异常并清理资源；验证回退元数据保留已发生工作；unknown候选在批次所有者处仅计一次；界面区分无审计未知状态，清理未提交的失败作业引用，并明确历史只读、无候选终态和裸片基线。训练/独审双门真值、应用资格、默认预算及取消最好候选规则未变。修后没有再次外发新源码，不声称由外部模型执行了测试。

新增核心7项与GUI6项红绿验证通过；同一次联合回归127 passed、3 skipped，63.71秒，生产源码前后稳定。三项skip均要求显式受支持GPU runtime；另在独立CUDA13环境对修后源码实际跑通单层已知AR控制与缓存门复用：1层112.268279878nm，520–580nm有限审计最大R约0.000341136，目标R≤0.001，state=completed、validation_complete=true。该控制使用明确k=0的教学材料，不替代真实材料双窗结论；计时未用于速度比较。60层大矩阵没有重跑，之前性能/内存数字保留原冻结来源。

可选CUDA GUI窗口、探测/选择与typed请求冻结的R2实测保留；当前界面更改另由真实Qt定向回归覆盖。原XPU环境和CPU默认保留，启动/选择步骤见[GUI指南](film-synthesis-gui-guide.md#cuda-13-gui-启动可选)。软件推送、研究资料同步和安装包发布分开处理。

### 显式 CUDA 三项回归补齐（2026-10-20）

原环境联合回归的127 passed、3 skipped是保留的历史结果。随后在隔离CUDA13解释器中设置既有测试开关 `COAT_SIM_TEST_SYNTHESIS_GPU=cuda`，原样执行此前跳过的三项：`test_real_gpu_complete_synthesis_and_cpu_audit`、`test_real_gpu_cancel_entry_never_reuses_previous_work[after_gate]`、`[cached_entry]`。结果 **3 passed、0 skipped，3.78秒**（pytest时间；外部测试驱动5.80秒，不用于性能比较）。Torch 2.10.0+cu130、CUDA runtime13.0、RTX4070 Ti SUPER，实际完整合成使用CUDA并通过CPU独审；两个取消边界保留已发生数值门工作，未重复累计前次调用。测试源码和生产源码前后哈希一致，未修改或放宽断言。

首个启动仅因隔离环境缺pytest退出，没有执行测试；按requirements-test.txt约束补齐与原环境相同的pytest9.1.1后重跑。隔离环境106个包离线兼容检查通过，原XPU环境、默认CPU及项目依赖文件不变。原始JUnit、完整实际工作日志、心跳和源码哈希位于本地 `delivery-closeout-20261020/gpu-skips-r2`；失败启动位于r1，未覆盖。未重跑60层矩阵，也没有新增软件缺陷、外部模型调用或性能结论。
