# 从零膜系合成 GPU 前向：阶段验收报告

日期：2026-10-20。当前最新版 HEAD `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`。N0 GPU 前向已接入核心、typed job、CLI 和现有 Qt Quick 页面；CUDA/XPU 有真实执行证据。三周期整任务对照中 CUDA 比 CPU8 慢约6.8%，因此保留 CPU 默认值。八层吸收补充对照及用户追加的实际60层多光谱测试均已完成；60层四次完整流程、前向首调/稳态、两端取消和逐点复核见[60层报告](film-sixty-layer-multispectral-report-20261020.md)。本阶段未提交、推送或发布 release。

[实施契约](film-synthesis-gpu-forward-contract.md)和离线[算法14y](algorithms.html#film-synthesis-gpu-forward-20261020)先于实现建立。原始证据根目录为 `artifacts/film_synthesis_20261020/gpu-forward-20261020/`；本报告中的相对 artifact 路径均相对于该目录。

## 实现及数值边界

复用 `FrozenFilmEvaluator` / `TorchCoherentBatch`，没有另写 TMM。同拓扑、位置和材料的不同有限厚度 growth 候选使用 GPU 前向，父进程保持原顺序消费和归档；候选被选中进入 polish 后，才补算一次 CPU 解析 Jacobian。needle、删层、换材、厚度精修和独立审计继续使用 CPU。没有实现 GPU Jacobian；本阶段的混合路径也避免了未选 growth 原始候选的全层导数，时间差不能全部归因于 GPU 硬件。

新增 `FilmSynthesisRequest.forward_backend`（默认 `cpu`，支持明确的 `cpu/cuda/rocm/xpu`）及可选 `forward_device` 绑定，复用现有 CLI/助手/持久 typed job 序列化。验证请求不导入 Torch；实际执行前核对 runtime、设备、Torch 版本和 float64/complex128。绑定失败保持 `prebound_unverified`，不冒称成功。显式 GPU 不可用、绑定不匹配或既有 evaluator 回退 CPU 时明确失败，不静默冒称 GPU。

严格检查源 n/k、域和未知 k 显式假设后才进入 legacy 性能采样器；已知吸收保持原值。相同指标、相同 n/k、相同显式预算和 CPU 独立验收用于比较。有限采样不是连续证书，受控合成材料不是实测材料。没有恢复低额默认搜索预算。

有限评价预算使用 capacity=1，避免投机消耗尾项；无评价上限时仍限制单批和缓存内存（默认64候选、R/T/A估算64 MiB、LRU4），这些不是总工作量预算。GPU 已算但未消费尾项计入 actual/discarded，不进入 best；partial chunk 取消时总工作量为 null，同时记录已知下界，不猜测为零。既有心跳、取消、无响应检测及所拥有进程清理保留。

核心最终冻结为 `owner/core-freeze-epoch-fixed.json`，SHA256 `aa1cce1cb554cde998b8d582c12a95fe89e7a396a693b3e011281253146c47f0`。它包含五个生产源文件及核心测试；早期 `core-freeze.json` 保留，不覆盖。最后修复了验证完成后、下一次调用入口取消时错误沿用上次 execution/verification 计数的 epoch 竞态，红绿证据为 `owner/stale-epoch-red.*` / `stale-epoch-green.*`。

## 三周期完整 CLI 对照

输入为 `inputs/three-cycle-4001-cli.json`：450–900 nm、4001点、0/30/60°非偏振，三种已知 k=0 的合成候选材料、八个 growth fraction、显式三周期、评价上限 null、从裸基底开始。CPU8/CUDA8 在同一隔离 CUDA 13 解释器、同一最终核心源码下各运行两次；只有前向 backend/device 不同。没有总墙时上限；每2秒心跳、每秒采样进程树 RSS，600秒无响应触发协作取消，宽限后仅清理任务自己拥有的进程。监控退出检测约有1秒粒度，下面用两位小数报告。

| 端点 | 两次完整墙时（秒） | 中位（秒） |
|---|---|---:|
| CPU8 | 66.04 / 66.04 | 66.04 |
| CUDA8，RTX 4070 Ti SUPER | 71.04 / 70.04 | 70.54 |

CUDA 中位增加4.50秒、约6.82%，本案例没有端到端加速。此前8层/24候选前向微基准约11.7倍的稳态比例不能代替这一结果；旧 CPU1/8 harness 的1.612倍收益也不能拿不同计时边界的86.92秒与这里的66.04秒直接比较。

四次最终处方、完整训练评分、CPU 验收评分及目标判断逐值相同：最好3层，404次逻辑评价，3次验证、失败0；归一化最坏验收差距 `0.7157434558852453`，最坏点512.190755 nm / 30°，目标未达到，状态 `not_found/cycle_limit`。这只是显式三周期结果，不构成不可行证明。每次独立审计132次物理调用、288135波长点。

CPU完整语义SHA为 `a9017df8296de0a1e72a1ecbf9e712ecad2b7b279e45f39c9d9b5b23ec64c5d4`，与此前CPU路径精确一致；CUDA为 `0347c67f0ff2653d1919783ce1c0edc981c3c126b769c430d0ddc98ba08fe0be`。CUDA早期有一个额外 incumbent 和一个1 ULP误差差异，因此不声称 CPU/GPU 整条轨迹位相等；重复 CUDA 两次一致，最终质量完全一致。根任务另从四个原始结果离线规范化完整处方、训练分数、验收分数及通过标志，四份quality SHA均为 `f032e40b9b9a9f4538f58c451e90b211ddabc6429f50eb1a6b160b9eb94cd947`，见 `three-cycle-cpu8-cuda8-r2/root-final-quality-comparison.json`，没有重复数值计算。

| 实际工作（每次） | CPU8 | CUDA8 |
|---|---:|---:|
| 训练 evaluator invocations | 401 | 404 |
| 训练物理调用 / 波长点 | 1203 / 4813203 | 1212 / 4849212 |
| CPU worker已消费任务 | 204 | 60 |
| GPU已提交/完成/消费候选 | 0 | 145 / 145 / 145 |
| GPU丢弃 / unknown候选 | 0 / 0 | 0 / 0 |
| GPU训练物理调用 / 波长点 | 0 / 0 | 435 / 1740435 |
| CPU解析补算候选 / 物理调用 / 波长点 | 0 / 0 / 0 | 3 / 9 / 36009 |
| 额外数值门 CPU 调用 / 波长点 | 0 / 0 | 168 / 672168 |
| 额外数值门 GPU 调用 / 波长点 | 0 / 0 | 168 / 672168 |

训练、数值门和独立审计分开计量。CUDA列的1212次是混合训练总量，不是CPU工作量；解析补算已经包含于训练量，不能再累加。各次 worker、launcher 和 GPU 路径均完成清理，无取消或超时。

CUDA诊断两次中位：evaluator准备0.15746秒、厚度上传0.02312秒、resident stack上传0.07557秒、GPU计算含同步0.23328秒、下载0.01698秒。这些是局部且部分跨层级的计时，不能求和冒充端到端拆分。整任务还包括导入/context初始化、CPU解析计算、数值门、审计、归档、JSON和进程收尾，不能把所有未归属时间推断为某一种开销。

原始请求、结果、完整checkpoint、心跳、内存采样日志、stdout/stderr在 `three-cycle-cpu8-cuda8-r2/`。该runner的内存日志实际均为unavailable，不能提供有效RSS峰值；CUDA allocator峰值当时也未采集，资源数据缺失如实保留。`report.json`、`offline-audit.json` 和 `source-guard-epoch-fixed.json` 保留来源：每次 runner 当时记录四项运行源hash，启动前人工核验最终六项核心freeze、结束后六项均匹配；没有全137项核心源的事前快照，因此不冒称全目录前后守护。之后的补充多层runner已扩展到全137项核心、CLI和harness守护。

## 八层吸收 seed 到九层的完整补充对照

冻结输入及实际门禁见契约：synthetic glass/low/high，high明确k=0.002，450–900 nm/4001点、0/30/60°非偏振，两周期。CPU8/CUDA8各一次，分别226.11/224.11秒，单次约0.88%差异与监控检测粒度接近，不足以支持稳定排名。不是实测材料或论文复现。

两端最终9层、731次逻辑评价、验证3次、失败0，以cycle_limit正常结束，目标false；训练和验收最好最大归一化差距均 `0.5841904831180833`，weighted RMS `0.42279303335852036`，最坏R约0.41580851688@450nm/60°。完整处方、12003条训练误差和96101条验证误差的逐点R精确一致，最大绝对差0。审计233物理调用/288236点。

第二周期实际trace含216个9层growth_trial，GPU batch_records也含27个九层批次、216候选，满足多层路径门禁。GPU总前向241候选/723条件调用/2892723点；CPU解析refresh2次/6调用/24006点；数值门CPU、GPU各276调用/1104276点。CPU8总训练2184调用/8738184点，CUDA混合总量以原始performance为准，不能把逻辑评价数当CPU工作。两端均清理完成，无取消。

`absorbing-8layer-k002-cpu8-cuda8-r1/offline-audit.json`（SHA256 `2a53256c0b2bd79f2b3e2c41d5cfe1b7537a36682ca986ea4c79e38a55666d4f`）记录140个核心/CLI/harness路径事前事后hash完全一致。RSS每秒日志实际全部unavailable，CUDA allocator峰值亦未采，不能据此声称低内存。单独八层取消未启动，按用户新要求优先60层多光谱，并在其运行前验证资源采样器。

## 真实设备和取消控制

真实 CUDA CLI 已从 N=0 解出已知 AR：1层、1周期、7逻辑评价、独立审计通过，`completed/validated_targets_met`，exit0。见 `ar-cli-cuda-smoke-r1/`；约3秒仅为功能烟测，不作速度证据。

核心真实 CUDA 控制分别验证 bare、known AR、缓存验证计数、已计算未消费尾项，以及1024点 chunk 中途取消。known AR 训练6 invocations（CPU needle1、解析补算1、GPU4），6次物理调用/54点；数值门CPU/GPU各5调用/45点。缓存重用不重复计算数值门；尾项取消丢弃2个候选，保留完整已提交 best；partial chunk 中总量 null、已知下界1次调用/11点、unknown候选1。证据为 `owner/{bare-accounting,known-ar-accounting,cache-verification,cancel-tail,partial-chunk}-final.json`。最终 epoch 修复另有5项真实 CUDA 红绿控制，普通结果不变。

## 界面、现有入口和实际多核闭环

现有页面增加独立合成前向设备选项，CPU默认；不把调度位置 `ComputeTarget` 的 CPU deviceId 冒充算法前向 GPU。探测使用当前执行解释器的异步子进程，Qt线程不导入 Torch；helper拥有探测generation、单worker executor及子进程，controller拥有选择草稿/冻结请求。Windows先创建挂起子进程、加入已有Job后恢复，避免venv launcher孙进程逃逸；取消在worker中轮询并回收所拥有进程树，不阻塞UI。15秒只用于设备探测基础设施，不是搜索预算。

首版 GPU 只支持本地执行；正常和 prepared-angle 请求在实际remote路由解析后、准入前共用拒绝边界。CPU remote保持支持。历史摘要读取 `performance.forward.actual_backend` 和实际设备身份，未知值不由requested推断。

实际双worker回归由 `Controller.setSynthesisCpuWorkers(2)` 构建/冻结当前 typed job，再执行真实 `execute_job`；4001点、三角度、多候选触发既有调度阈值。结果实际2个worker PID、`headless_process`、submitted=completed=consumed、unknown0、cleanup=true，历史摘要显示实际2。它证明 UI 构建请求确实调用多核，不只是字段 roundtrip；不把它扩大成原生 Start/远程调度整链测试。

原项目解释器的实际helper探测6.85秒完成，Qt父进程 `sys.modules` 中 Torch 始终未导入；Intel Graphics XPU可用，而CUDA/ROCm明确返回当前Torch构建不支持。见 `ui-live-forward-probe.json`。Temp CUDA环境仅用于获准CLI试验，没有自动切换GUI解释器。

真实 controller 探测→选择XPU→冻结请求→当前CLI执行→历史摘要也通过。`owner/actual-xpu-smoke/20261020T080851Z-09031b00/report.json` 记录 Intel Graphics、XPU、Torch2.10.0+xpu、`prebound_verified`，known AR目标达到、exit0，摘要“实际 XPU (xpu:0 Intel(R) Graphics)”。它使用临时合成材料库，明确是本地CLI闭环，不是scheduler transport或性能测试。

## 验证、静态检查与限制

核心分阶段分别有68项既有auto/audit回归、34项evaluator/pool/cancel回归，以及epoch修复后19项定向回归；显式GPU条件的跳过分别记录在各日志，不能合并成同一次全套。隔离CUDA环境没有pytest，因此实际CUDA以artifact driver执行，不冒称为pytest运行。

界面最终13项定向测试通过（含remote normal/prepared拒绝、CPU remote准入、实际标签、真实双worker、真实探测进程树取消），29项既有queue/coordinator/config回归通过；两组不与此前重叠轮次相加。最终JUnit为 `owner/gpu-forward-ui-focused-final.xml`。最终只读gpt-6.1-sol核心/界面窄审均无阻断；审阅者未重复跑数值或冒称独立整套测试。

全量静态覆盖985个源文件，`artifacts/static-analysis/gpu-forward-20261020-r1-final/report.json`：fatal0、官方exit1，40项既有相对baseline差异。全部诊断fingerprint多重集与CPU/取消最终R5逐项相同，本阶段新增0、消失0。`static-final-comparison.json`记录报告SHA；`static-windows-transport-gpu-r1.json`证明baseline、工具、锁文件、Ruff/mypy配置全部未变。仅artifact适配Windows长argv传输（Ruff同配置按文件分批、mypy原生argfile保留完整单次import图），未削减范围或改忽略规则。该结果不是仓库全静态清零。

ROCm接口有明确可用性/失败路径，但本机无对应硬件，未作真实验收。GPU Jacobian、GPU独立审计及远程GPU不在本阶段支持范围；XPU有实际功能闭环，尚无整任务性能排名。当前CUDA整任务没有加速证据，CPU继续默认。

指定外部模型复审缺口单列：本阶段 DeepSeek/Seed 复审未完成，没有复用旧阶段源码披露授权。内部gpt-6.1-sol审查不等同指定外部模型审查。没有当前环境、权限或安装阻塞；驱动、安全设置和凭据未改。

## 用户追加的60层多光谱：完成

实际Ge/BaF2双15对、ZnS基片，60层始终正厚度；两红外窗各1001训练点、0/30/60°、s/p、8个R/T共同目标，64固定候选及同CPU解析TRF，另2001点/窗CPU留出。两对顺序CPU→CUDA和CUDA→CPU；CPU8完整75.831/77.965秒、CUDA45.389/46.506秒，中位76.898/45.948秒、比值1.6736。它是固定60层起点的受控评价/精修流程，不是N0生长到60层；不同批处理/IPC路径也在计时中。

四个64×24024观测矩阵全部有限，最大CPU/CUDA绝差3.3085e-14、2e-12门通过；四份完整局部结果和最终60层处方精确相同，都选候选33，3轮/5实际局部训练评价（上限3/6），每次实际70训练候选等价+1留出。最终最坏差距0.9886784054023782，目标未达；不称达标AR或不可行证明。首调CPU串行vector/CUDA2.777/2.418秒，resident中位2.608/0.330秒，显式12上下文微基准不代替生产LRU4或完整流程。

实测完整RSS峰值CPU最高0.853GiB、CUDA1.001GiB；CPU取消另达5.917GiB，单列。CUDA框架allocated/reserved峰值22.12/40MiB，micro44.80/64MiB；整卡共享峰1155/1179MiB，基线896MiB。两端取消保留完整60层候选、无补审计并正常关闭；CUDA实际64个已计算仅3消费、61丢弃，未证明kernel抢占。

精确命令、完整处方/矩阵SHA、逐目标未达差距、源守护、首个CPU脚本失败及修正历史、原始与反序独立报告、光谱图均在[60层报告](film-sixty-layer-multispectral-report-20261020.md)。未修改产品默认预算或GPU默认选择，未推送/发布。


## 软件交付收尾更新（2026-10-20）

用户已取消本项目 Seed 复审门禁；算法研究继续分轨推进，不等待研究全部结束才交付软件。DeepSeek 无项目数据连通已实际返回 READY；当前冻结源码复审的两次启动被自动审批在进程开始前拒绝，原因是缺少对这组非公开文件发送给 `opencode-go/deepseek-v4.1-flash` 的明确披露授权；未发送文件、未绕过重试，具体授权问题待用户答复。此前正文中的 Seed 未完成为历史状态，不再是当前门禁。

可选 CUDA 13 GUI 的真实窗口、异步探测/选择及 typed 请求冻结已通过 R2 有界检查；原 XPU 环境和 CPU 默认保留，新增本地快捷方式与用法见[GUI指南](film-synthesis-gui-guide.md#cuda-13-gui-启动可选)。该检查没有重跑算法矩阵。软件提交/推送与研究资料同步、安装包发布分别处理；当前交付状态以收尾记录为准。
