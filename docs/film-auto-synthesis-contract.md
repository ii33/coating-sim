# 自动膜系合成核心契约

日期：2026-10-21。当前真值为 `coat_sim/core/film_auto_synthesis.py` 类型与签名。图解、公式、默认与来源见 [14t](algorithms.html#film-synthesis-audit-20261020)；独审见[审计契约](film-synthesis-audit-contract.md)；可组合起点见[物理结构契约](film-physical-structures-contract.md)；统一JSON、CLI与助手准备边界见[执行契约](film-synthesis-execution-contract.md)。2026-10-19报告仅作此前阶段记录，不代替当前文件和测试。

## 输入、物理边界与唯一状态所有者

`FilmSynthesisRequest.environment` 必须是合法空front/back层的单面、前向裸片；`objectives`为既有R/T值/上下限目标，候选材料显式给出。`seed_designs=()`可选，允许任意真实合法层序而不限五经典模板；起点不称从零发现，裸片工作分支始终保留。起点与环境光学边界、参考及吸收设置一致，nm/µm/QW冻结后转物理nm，材料可独立于允许插入/替换的候选池。来源revision只做回溯，不读写用户session。运行级ID保留集合包含全部起点与已分配提案，删层/丢候选不释放；新层冲突时稳定加后缀，旧层绝不重编号。审计checkpoint中的design可显式加入seed_designs恢复，仍重新冻结和审计；恢复不等于读取旧计算分数直接判合格。

坐标z=0在基片侧，层表按基片向入射排列；真空波长与物理厚度nm、角度°、R/T为0–1。支持现有解析相干核与半无限基片的R/T，非一般各向异性/散射模型，不支持phase/GD/GDD目标。前表面R/T不自动等于双面有厚基片整片。

`freeze_film_synthesis_request(service,request,*,cancel_event=None)`深拷贝、冻结基片/介质/候选与起点材料，重放已有快照时不读live目录。显式采用source k策略，要求每个实际目标、验证波段、尺度与QW参考点有已知k；不把未知或域外k补0，不外推n/k。失败点名material/optical_set、首次缺失波长和目标区间。已知教学k=0与真实来源k保持不同身份；真实工艺适用性不能由公式名称推断。所有结构变更后完整重算。

`synthesize_film_design(service,request,*,cancel_event=None,progress_callback=None,snapshot_callback=None)`为同步纯核心。本次调用独占请求副本、各工作分支、训练best与audited_best，材料库、文件与编辑会话不变。回调在调用线程执行，不获得写回用户设计权限；异步外壳负责自己唯一取消事件、代际与请求身份。

## 搜索和排序

真正N=0即使裸界面dR/dd为0，也尝试合法正有限厚度出生。首目标中点λref的quarter=λref/(4nref)，增长(.5,1,2)quarter并按显式厚度上限和总厚剩余空间夹限，去重后真实评分。负灵敏度针插与有限出生共存，微小针改善不能封锁有限生长。针层内拆分保持宿主总厚，所有非零行满足制造约束。

精修使用解析加权SSE梯度和quarter尺度投影回溯，实际接受/归档按真实最大标准化违规优先、固定网格RMS其次；全部满足acceptable容差后优先较少层、更小总厚，再比RMS/最大违规。只改变本合成入口的排序，不改旧local/global算法契约。

每层步长按所有目标/验证采样及材料n/k结点的复传播相位控制到π/4，再真实重算；投影若破该cap则缩步。它是数值约定，不保证高Q功率变化小。显式删层均重算；换材试原物理厚和保参考光学厚度dnew=dold*nold/nnew两个合法提案，raw改善才精修，未覆盖所有先变差再精修邻域。工作生长可暂时变差，不能覆盖更优已审计解。

全部训练可行候选都有独审机会，不因训练制造排名而丢失；同一完整处方hash缓存审计。循环由已审计候选的训练/审计双门决定完成，训练过关但独审漏峰不直接停机。暂未把审计反例波长自动增补到训练梯度目标，困难窄峰仍可缓慢或反复失败。

## 参数、工作量与退出

最小非零厚、单层/总厚、层数、周期、完整候选评价数均默认None；现有schema4096层资源边界继续有效，产品不新增默认低额预算。max_evaluations若显式给定至少2，逻辑候选评价、零针、精修、删换和审计共用计数，保留最终审计空间。一次audit计一次逻辑候选评价，内部完整统一评价器调用与波长×角度条件量通过audit_physical_evaluations、audit_wavelength_points单列，不混称单波长算量。

默认每层3内点+全部界面；needle_seed_fraction=.001；local_step_tolerance=1e-6；relative_improvement_tolerance=1e-9；stagnation_cycles=2仅报告平台，不是默认终止；验证倍数4。极值局部迭代设置见审计契约，均为数值设置不是总搜索预算。制造m投影到{0}∪[m,dmax]，m/2为最近点界；未设m不按任意薄层阈值删除，精确零行移除且重算。

## 结果、进度、取消与失败

结果含对应同一candidate_design的training_score/validation_score、逐条件误差、audit每目标最坏位置和域、计数、trace、来源revision与冻结请求。最终候选优先返回已审计最好档案；取消时仍可返回此前完整审计，validation_complete不等于targets_met。本轮正常数值结束与正式达标分开：completed不要求targets_met=True；targets_met仅在同一候选的原门限训练和独立审计均通过时为True。未达标局部收敛/无有效改善可正常结束本轮，保留实际误差和真实停止原因；acceptable_normalized_error仍只是求解可接受尺度，不放宽名义目标。权重小不放宽最大违规门。

进度给stage/cycle/evaluations/layers/best最大违规，snapshot_callback(candidate,training_score,validation_score,audit)仅在更优完整独审后调用。CLI外壳提供真实活动与独立墙钟心跳，不伪造无限预算百分比。文件输出由外壳负责排他/原子语义。

取消在材料采样、物理调用前后、波长分块、局部步骤和回调后协作检查，已开始的helper返回后才响应；未完成评分不得归档。validation_complete通知后仍检查取消，再提交终态；终态通知后取消不回溯撤销。shape/control非法开始前ValueError；材料域、数值或审计失败为failed；显式限制耗尽而未达标为not_found，不是不可行。无完整评分时候选为空；候选不自动应用。

## 验证边界

有限密网格、错位点和局部极值复核不是连续证书，continuous_certificate=false；仍使用同一物理内核，不是独立实验或独立求解器证明。离散谱线不扩为窗口，只验证规定角度/偏振。研究两轮的KKT正交评分、额外相位辅助与复响应多样性没有稳定优势，未包装为新验证算法。阶段证据和未达到目标的差距见2026-10-20阶段记录；无生产工艺、实机校准、量产良率或商业软件排名结论。

## Cycle-0 unified explicit budgets (2026-10-21)

`max_cycles` counts outer search iterations; it does not disable the initial structural priors/global stage or mean one candidate evaluation. `max_evaluations` remains the parent-owned logical evaluation count, including audit; training admission reserves one audit slot. Complete worker batches require sufficient remaining slots; speculative physical work retains separate consumed/discarded/unknown accounting.

`max_seconds` is optional, positive and finite, measured in seconds with a monotonic clock from entry into the core synthesis call. `None` remains unlimited, including old JSON/configuration defaults. The same cooperative stop signal covers cycle-0 freezing/phase preparation, proposal generation, local/global search, physical chunks, and parent worker startup/waits. Queue/adaptor preparation before core entry is outside this search clock. Indivisible calls and owned-worker cleanup may exceed the deadline; this is not hard real-time preemption.

An expired time budget yields `not_found` / `time_limit`, not a user cancellation or infeasibility certificate. No new final audit starts after expiry. Retain only a complete parent-committed training candidate and an already complete audit of that exact design; never promote late/unconsumed worker outcomes. Explicit cancellation keeps its established semantics. See [algorithm 14ab](algorithms.html#film-synthesis-run-budgets-20261021).

## 默认统一混合路线（2026-10-22）

默认 search_strategy=hybrid，legacy 仅显式重放历史独立路由。结构族按需求层级广筛，完整实际设计进入共享多样性/Pareto档案，再轮转局部/低维DE、解析批针、全厚联合SSE、实际删层/同源合并与全原约束SLSQP。详见算法14ak；原网格、默认1nm、材料来源和独立验收不变。

objective_weighting=equal_window 默认将每窗口条件归一并保留用户目标权重；point_count 必须显式选择。该选项只改变探索merit，严格max验收仍检查用户原全部约束。真实SSE终态续接下一轮，即使max暂时恶化；训练严格冠军和已审计冠军分开保存。档案默认32项保护二者，删层赢家含实际设计、父代和操作而不只存分数。全SLSQP不使用稀疏活动集，不信任epigraph t直接验收。

needle_batch_size=6、needle_position_refinement_rounds=1、needle_pre_local=true（新建默认，可显式关闭）均为数值探索设置。针绑定原宿主ID/分数与原界面，实际构造序决定Jacobian列序；数学零/近零与最终制造厚度分开。只相邻同material_id+optical_set_id可合并，bulk不参与。max_layers仍None，不存在70/104目标层数或隐性上限。

每轮调度片及已完成/未完成、实际调用量和预算停止写入performance.search.hybrid；旧全局前缀不再独占预算。max_working_set_bytes默认0=自动，冻结时取主机/cgroup可用内存的50%；未知/无效/异常测量透明回退1.5GiB并记录basis。正整数仍是显式字节限额，None/JSON null仍是关闭，GUI提供三种明确模式，CLI通过--request同一字段配置。派生resource_guard_resolution版本1保存requested/effective/available与依据；冻结重放不重新测量，清空该解析字段可显式重新采样，改变请求限额会清除旧解析。旧正整数/null请求语义不变。performance.search.hybrid含requested/effective限额、解析元数据、resource_limited及每个跳过阶段；时间耗尽且发生资源跳过须同时披露time_limit/resource_limited，未完成独审不能称完整合成验证通过。该估计只约束混合阶段工作准入，不是进程硬内存限额；冻结、初始裸基片评分和保存结果仍有基本开销。超出是透明资源跳过，不是物理不可行。phase1只新增可独立调用基础模块和独立敏感性CLI，phase2才启用此默认路线和正式窗口/作业接线。

混合首版按父调度器顺序评价单个候选/导数探测；保留cpu_workers请求和现有legacy完整候选批并行能力，不把请求的工作进程数冒充已使用数。混合路线的实际后端、有效工作进程与真实调用计数以performance为准，不宣称其针扫描已经多进程并行。生产GUI渲染与普通优化设备选择没有为测试降级。
