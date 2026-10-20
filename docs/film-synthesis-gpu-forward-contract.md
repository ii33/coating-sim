# 从零合成复用GPU前向：跨模块契约

日期：2026-10-20。连续于CPU多核/取消应用阶段；先写边界再实现。只在当前最新版工作目录修改，复用已有FrozenFilmEvaluator、TorchCoherentBatch、同一目标聚合器和CPU解析导数/独立审计，不重写TMM，不改变默认总量预算。本阶段必须以实际合成整轮作验收，孤立前向微基准不作为完成条件。

## 请求、入口及状态所有者

FilmSynthesisRequest增加forward_backend，缺省cpu；首版只接受cpu/cuda/rocm/xpu四个明确运行时，不把未校准auto解释为GPU。旧wire缺省仍CPU。增加可选forward_device；CPU为None。若已有异步探针绑定，记录runtime、index、uuid（运行时未提供可为null）、name、torch_version、dtype=float64/complex128，执行端在使用前比对；不提供绑定时明确采用所选runtime的默认设备，并在结果记录实际身份，不冒称预绑定。冻结/校验请求不导入Torch或执行设备探针。

GUI另有从零合成前向设备选择，CPU默认；CPU worker数独立保留。既有ComputeTarget继续拥有调度服务/位置/作业路由，不能将其CPU deviceId直接改成GPU前向选择。探针沿已有异步executor/独立子进程模式，用当前执行解释器探测实际Torch前向runtime及float64/complex128，不用CuPy或Mitsuba可用性代替；Qt线程不import Torch。独立小型UI helper唯一拥有探针generation、加载/空/失败状态、自有single-worker executor及探针子进程；Controller唯一拥有选中后端/绑定草稿和冻结请求。helper仅报告带generation的可用性，不能改变调度位置或算法状态。关闭/取消只回收自己启动的probe进程，不在UI线程等待超时。配置和历史保留选择，但观察到的result.performance才是执行事实。

CLI、助手和typed持久作业沿现有冻结JSON传递这两个字段，不使用可变环境变量覆盖冻结请求。UI首阶段GPU前向仅允许匹配本地执行服务；远端尚无相应能力/绑定广告时明确拒绝该GPU配置，CPU远端路线不变。原项目环境是TorchXPU；Temp隔离CUDA13解释器用于已授权CLI/整轮测试，不自动替换GUI环境或重定向现有调度服务。

## 算法边界及误差

GPU仅加速同一工作支、插入位置、材料、层序和目标/角度下不同有限厚度的growth forward组，以及合法纯forward评价。每组展开真实正厚度处方，以已有FrozenFilmEvaluator.evaluate_thickness_batch计算R/T/A，再进入同一目标评分聚合。不同材料/位置/层序不得串组；层ID、原提案消费顺序、原排名/预算/归档/审计边界仍由父进程唯一拥有。

needle需要CPU解析Jacobian，保留原路径；删除、换材、精修和审计保留CPU。GPU growth raw不计算无消费者的全层导数；该候选被选中进入polish时，对同一处方惰性补一次既有CPU解析评价。此为同一候选的derivative refresh，不新造逻辑候选评价，额外真实物理调用/波长点单独且累计计量。不得以有限差分替代解析Jacobian。默认cpu执行路径和CPU多核原顺序保持原样。

所有实际GPU波段在进入legacy性能评价器前，用既有strict source采样验证活动材料n/k、有效域及显式未知k假设；未知k未经显式假设不得变零，已知吸收保留。复用现有stack-local CPU/GPU 2e-12数值门和chunk取消机制；数学公式沿既有TMM，不修改精度。最终审计仍原CPU同指标验收，GPU浮点微差按公开容差评估，不要求CPU/GPU位级轨迹相等，不以GPU速度替代质量。

## 异步、取消、预算与失败

显式有限评价预算的growth保留逐项执行，不预支在途尾项；没有总量限额时也只做有界同组预计算。已计算未消费尾项计actual/discarded，不进入最好档案或取消快照。每chunk前后协作检查取消；已经发出的GPU kernel不能抢占，父进程完成前不提交半次结果。保留既有CLI/队列心跳、取消、无响应监测、worker回收及终态只交付一次。有限缓存仅控制内存，不新增搜索cycle/evaluation/time默认上限。

显式GPU不可用、绑定不符、精度门失败或不支持的物理模型须给出具体原因及实际backend，不能把已有性能API的CPU fallback冒充GPU执行。不得隐藏已经发生的CPU验证/fallback工作。GPU不保证小任务或每种结构都有收益；整轮未加速就报告实际差距，不宣布不可行。

## 计量及验收

performance保留原CPU进程池诊断，另外增加forward诊断：requested/actual backend、runtime、实际device/dtype、批数、提交/完成/消费/丢弃候选、逐目标/角度物理调用和波长点、CPU解析refresh工作、stack-local CPU/GPU验证工作、准备/上传/计算/下载耗时和错误/fallback事实。一次batch不是一次候选物理评价；一次GPU验证的CPU/GPU额外调用不能消失。异步/跨进程区间不重叠相加冒称墙时。

先测试已知有解AR，再测试同输入多层/三周期、含吸收案例；至少一次实际CUDA整轮和当前UI可用XPU整轮/入口证明。保持同一目标、n/k、采样和显式预算；记录最好指标、层数、端到端、实际工作、心跳和取消。CPU基线与GPU用相同新源码，处方/分数差异及最终CPU审计逐项报告；不能拿旧CPU源码的墙时拼为新内核收益。界面测试覆盖配置/冻结/queued spec实际字段和观察到的执行设备，CLI/助手不是孤立脚本。

本方案分组、惰性解析、状态及计量规则均为本项目约定；未找到外部依据，仅为本项目实现。物理方法与GPU核来源见现有算法文档及已有代码，不声称新外部算法优势。ROCm接口沿既有后端能力，当前机器无对应硬件，不能列为已实测。

## 前向探针与轻量校验API（实现前冻结）

`film_synthesis_forward.validate_forward_device(value, backend)`仅检查上述封闭绑定schema，不import Torch；`resolve_forward_device(backend, binding=None)`仅在worker/probe执行真实设备身份及FP64/complex128验证，返回实际绑定。无binding的观察标记runtime_default，提供绑定且匹配为prebound_verified。`live_probe(backend)`返回可用绑定列表。

UI启动当前执行解释器的`python -m coat_sim.core.film_synthesis_forward --probe --backend cuda|rocm|xpu`，只接收前缀`__COAT_FILM_SYNTHESIS_FORWARD__=`后的单行JSON `{ok, backend, devices, error}`，并严格核对schema、请求runtime和generation。沿既有异步probe的15秒基础设施超时，明确显示失败/超时并可刷新；该超时不用于求解器或搜索预算。子进程属于本次探针，取消/关闭只回收自己所有的工作，不终止其他任务。

## 吸收多层整任务对照（执行前冻结）

冻结输入为 `inputs/absorbing-8layer-k002-2cycle.json`，SHA256 `a42eef6fc8159cd5e7312bd08143856b81bc26b4abf5431828e54a616049fb9d`。这是受控合成材料，不能表述为实测材料或论文复现：350/550/900 nm 的 glass n=1.52/1.50/1.49，low n=1.47/1.45/1.44、k=0，high n=2.12/2.10/2.09、k=0.002。八层 low/high 交替，厚度65、67、…、79 nm，总厚576 nm；450–900 nm、4001点、0/30/60°非偏振，R下限0.999999。

显式 max_cycles=2、max_layers=9、max_evaluations=null，use_k=true；不设置总墙时预算，保留心跳、无响应检测和取消。当前分支轮转先处理裸基底、第二周期处理seed；只跑一周期不能证明多层GPU路径。以同一源码/解释器分别跑CPU8、CUDA8各一次，先作为功能及质量对照，不凭单次墙时宣称稳定排名。必须从实际 batch_records 确认 prepared_topology_layers≥9，并核对第二周期 growth_trial 的 layer_count≥9，才算覆盖八层到九层的GPU有限厚度增层；若seed精修删层导致未覆盖，明确报告门禁未满足。名义growth候选为24+216，实际约束裁剪和额外CPU导数/精修/审计工作以结果计数为准。

独立CLI取消案例另建输出，在心跳证实多层growth路径已开始后创建取消文件；检查完整best/checkpoint、任务cancelled状态、同处方验收事实、当次epoch计数和所拥有进程清理。取消案例不并入完整CPU/CUDA质量或速度配对。以上输入、门禁和测试安排均为本项目约定。

## 用户追加：实际60层多光谱有界对照

用户指定的是“60层多光谱”；下列具体材料、阈值、采样和预算为本次项目测试选择，不冒称用户指定或文献性能规格。使用当前已有真实源n/k快照 `exploration_v3/ge_source_joint_seeded_direct.request.json`（文件SHA256 ad954b80b3ce9ddc4e094dfb21d16f85b92d7a45067ac7cfa97115812cb504dc，冻结材料快照SHA256 `c24ef496b7cdcc23538643098a58f48909b80fae7d7ef2b21fc3cfc65ac54d21`），源表和归属只读保留，不写用户材料库、不将未知k设零。Ge薄膜、BaF2/NaCl和ZnS块材代理与目标制造工艺未匹配，数值测试不等于实物验证或论文复现。

现有物理结构生成器构造两个各15对的4/10µm膜堆，共60个真实非零厚度层，ZnS基片。正式输入需落盘完整逐层处方及64×60确定性厚度候选矩阵并记录SHA；所有候选及精修保持相同材料序列和实际60层，厚度变量上下限为起点的0.5/1.5倍，严格正。它是固定拓扑60层受控流水线，不能称从N=0自动长成60层。N0种子入口会先执行大量替材/精修，有限预算可能在60层growth前耗尽，故本次使用当前合法固定拓扑评价与优化入口，并分别报告前向、解析精修和整体流程。

共同目标为3000–5000及8000–12000nm双窗，每窗1001训练点；0/30/60°，s/p两偏振，R≤0.0099且T≥0.9001，共8个objectives。每候选12012个去重波长-角度-偏振点；现逐目标实现分别计算R/T，实际24个objective-angle前向调用、24024个计分点。64候选为1537536个逐目标计分点，不能与独立条件点混为一谈。目标为本项目测试约定，有限采样不是连续证书。

CPU候选评价复用现有 `OrderedEvaluationExecutor` 8个真实headless进程，明确with_jacobian=False；CUDA候选评价复用 `SynthesisForward` / `FrozenFilmEvaluator`，同矩阵、同评分、同原序选best，float64/complex128并保留既有CPU/GPU数值门。64总候选由测试harness硬限制；核心request评价上限为None仅用于避免其有限预算强制batch1，并不表示测试无限运行。选中处方进入现有TRF，60个解析CPU厚度变量，max_iterations=3、max_evaluations=6；按实际迭代/评价及停止原因报告，不保证恰好跑满。最终两端均使用相同CPU密采样验收（每窗2001点），不把GPU前向称作GPU Jacobian。

正式端到端各用新子进程，包含实际候选评分/原序选择/精修/CPU验收及收尾；微基准另用新子进程记录同64候选矩阵的首调与3次稳态，明确其中CPU serial batch不是CPU8完整流水线。GPU现有LRU4不能容纳本例12个去重条件，不能只因重复调用就声称全resident warm；实际prepare/upload/compute/download和验证工作如实记录，不将重叠区间求和冒充墙时分解。

资源预估：R/T/A输出约35.2MiB，60变量最终Jacobian约11.0MiB，额外Python评分对象、CPU8 IPC、解析临时及Torch runtime另计，预测总进程树RAM<8GiB、框架显存<1GiB，不冒称已实测。运行前GlobalMemoryStatusEx实际可用16.27GiB/总31.48GiB；已确认原Windows进程树RSS函数对父/子PID取到非零数据。采样实际进程树RSS和native working-set峰值，CUDA child记录allocated/reserved峰值；框架显存不等于整卡占用。旧三/八层采样失效原因是artifact父脚本未把仓库根目录放入sys.path，import tools失败；旧空日志保留，不补造峰值。

独立60层取消用例须先保存完整60层baseline/best，再从真实批量候选计算边界触发取消；检查保留的完整候选、当次工作/尾项unknown边界、cancelled状态和自有进程清理，不在取消后补审计。候选数及TRF工作有界；保留心跳、无响应检测，资源保护阈值和具体触发原因写入manifest，基础设施中断不作物理不可行结论。所有本次矩阵、预算、资源估算和验收门均为本项目约定。

60层输入预检已冻结：`sixty-layer-multispectral/prepared-input.json` SHA256 `8dcd65634be022a17ec39a54c17dd73f1fce7eb999b405f34dc793971af92190`；`candidate-thicknesses.npy` SHA256 `a40a70ed0dfb5641db0585f880d8b80a1e2ce2eb3aef789e069ffcdb50a9bbd5`。实际使用Ge/BaF2两个15对段、ZnS基片，60层，总厚49225.871283nm，单层238.359136–1761.804087nm。PCG64固定seed20261020生成其余63个[0.8,1.2]厚度比例候选，第0行为基础处方；随机矩阵是本项目固定测试输入，不是新优化算法。生成器/全训练网格strict源n/k预检已执行，Torch未导入、TMM及优化调用0，材料记录未变。

60层执行前最终窄审ACK：child `run_case.py` SHA256 `42d221d208e87655f7e6f285c847cbab747071b4801c1e7d7a0710534d77be80`；parent `monitor_sixty_layer.py` SHA256 `fc61aac55f5643078d6bb168eba4a100a4221c3a4956674a2ee6b324b3cb55fd`。正式两端完整流程优先，微基准和CPU/CUDA独立取消随后顺序执行；每例独立新目录及子进程。基线1+64枚举+local训练≤6=训练≤71，另密网holdout≤1，总≤72候选完整网格评价等价；数值门额外工作单列。实际pipeline GPU仍用生产LRU4，micro显式保留12上下文作为resident诊断，不能代替生产缓存性能。监控以实际execution/metadata识别后端，CPU8门须8个真实worker，数值事件与心跳分开；预检、整卡基线查询和spawn至退出墙时独立。只读ACK不是数值通过，正式后另查完整64×24024输出有限/2e-12容差及实际资源/取消。此段冻结后不在正式运行中改动合同或harness。

60层首个CPU r1因artifact错误读取compact worker已裁去的responses字段退出，日志、实际60层baseline、部分批量及资源数据保留，不计有效配对；27个观察到的自有PID均已退出。已用同一共享评分的完整score.errors.actual无额外TMM提取24024点并检查顺序，child新SHA256 `0f9cb8a938d74e13bf295ff6e77e3556c017a9b97f4f2fa128e2448367fea919`，只读差异终审ACK；monitor SHA不变。旧child/runner manifest另存before-score-projection，生产、输入、候选矩阵、精度/选择和预算未变。新CPU r2/CUDA共用修正child重新完整运行，合同此后再次冻结。


## 外部意见核实后的错误与计量收口（2026-10-20）

实际物理训练/独审的RuntimeError沿用既有failed终态，分别给numerical_failure或validation_failed原因。调用者progress/snapshot观察者错误（包括检查点写入失败）不是物理失败：在资源清理后原样传播同一个异常，停止本次搜索，不吞错继续；CLI/worker沿现有上层错误出口报告失败。本项目约定，未找到外部依据，仅为本项目实现。

通用evaluator发生CPU回退时保留本次已发生验证的metadata与CPU/GPU完成计数。显式GPU合成仍要求实际GPU后端；本次统计修复不允许静默回退通过。失败批次的unknown_candidates只由批次拥有者按N个候选登记一次；verification的工作量另外累计，不再额外把一个验证事件计为候选。正常评分、逻辑预算、物理精度及取消选择不变。新回归为tests/test_film_synthesis_deepseek_closeout.py；真实CUDA控制和独立核实、原始红绿证据保存在本地delivery-closeout-20261020。
