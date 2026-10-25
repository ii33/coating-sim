# 当前膜系制造修复的本地队列契约

更新：2026-10-25。实现接口为 `coat_sim/core/film_manufacturing_jobs.py`，几何与数值算子仍为 `film_synthesis_manufacturing.py`。本文只描述显式 GUI/本地 CPU 作业，不修改合成 v4 协议，不接入自动 hybrid，不默认运行或应用结果。

## 输入、冻结与唯一状态所有者

- 本地 executor 为 `film_manufacturing_repair`。现有 remote allowlist 不含该 executor，远程/GPU/并行 CPU 请求明确拒绝，不静默降级。
- `make_request_payload(service, synthesis, source_design, *, defect_layer_ids, reference_nm, refinement_evaluations_per_route, max_iterations_per_route, seconds_per_route, request_id="manufacturing-repair")` 在后台线程捕获源为真实 seed，通过既有 synthesis freeze 捕获材料、检查原波段 n/k 来源，并按既有参考波长实部 n 将 QW 转为物理 nm。源行 ID、材料引用和顺序保留；数值过程不会执行自动合成。
- 最终源指纹绑定转换后的物理 nm 源及冻结材料，不绑定编辑器原 QW 表示。GUI 还必须保留编辑器原始源/修订/材料闭包，应用前核对，没有该双重栅栏时应明确拒绝 QW 输入。
- request 为 schema 1、kind `coat_sim.film_manufacturing_request`，字段精确为 schema_version、kind、synthesis（完整既有 v4 synthesis request envelope）、source_design、source_fingerprint、defect_layer_ids、reference_nm、refinement_evaluations_per_route、max_iterations_per_route、seconds_per_route。引用厚度、选中 ID 及三类路线配额均显式输入；无 25 nm/57 层等隐藏缺省。
- `canonical_parameters(service,{"payload":request})` 仅接收完整冻结 payload；`build_job_payload(service,request)` 返回 schema 1、kind `coat_sim.film_manufacturing_job`、request。队列创建和 worker 复验不重读实时材料或解析另一搜索熵。
- `request_from_payload(request)` 与 `deserialize_job_payload(job)` 返回 `(ManufacturingRepairRequest, normalized_source_design)`。request/source 为后台验证后的只读上下文，不能从 UI 最新草稿重建作业输入。
- scheduler 独占 pending/running/terminal/cancel-request 状态；算子独占路线 trial/current/champion。GUI 只观察、取消及显式选择待应用方案，无 autoapply。

## 执行、预算与实际取消

`execute_job(parameters,cancel_event,report,checkpoint_path,write_json,*,external_retained_bytes=...)` 是持久 scheduler worker 的执行入口。parameters 只有 payload（上述 job）。执行源完整原训练，再执行独立 dense/offset/extrema 审计；只有二者完整时才启动 A 保留增厚/B 删除合并。沿用原始目标、角度、波长网格、均值口径、归一化与严格边界，求解器容差不作为名义通过。

源训练、源审计和所有路线完整候选/审计共同消耗 synthesis.max_evaluations（若设置）；synthesis.max_seconds（若设置）覆盖整个 worker 算法调用。每路秒数仍只覆盖该路线起始刷新、精修和审计，源无另一个隐含 5 s 配额。任一整次审计准入先预留其完整 dense/offset/extrema 上界，不以单块工作量替代整次预算。内存策略继承已冻结的 auto/explicit/off，绝不设置固定 512 MiB。

适配器的 live cancel bridge 把核心 `cancel_check` 接到既有 TMM evaluator/auditor 的 `is_set`。因此 route/global 截止、取消与共享停止在每个既有物理 wavelength chunk、审计 offset 和每个 extremum scalar 调用前生效，不只在提交回调前检查。部分调用不提交不完整分数；已完成训练先保留完整源快照。源审计未完整时允许只读返回训练源、audit_score=null、routes=[]，不冒充已审计源。

## 完整审计资源模型（项目约定）

每目标 i 的原采样数 s_i、加密倍数 m、角度数 a_i，令 d_i=1（离散线）或 (s_i-1)m+1。P_dense=Σ a_i d_i，P_offset=Σ a_i(d_i-1)，后者仅对非离散波段。启用 pointwise 的 value 目标搜索两符号，range 目标按实际上下界各一符号；mean-only 不制造 pointwise 目标。最保守地允许每个 dense+offset 内点为每符号形成 bracket，因此 scalar 调用上界为 Σ a_i signs_i max(0,2d_i-3) EXTREMUM_MAXITER。该上界可能很松，会明确提前资源停止，不降采样、不改目标、不声称运行速度。

完整审计工作集代理：128 MiB + 9×2048×(P_dense+P_offset+P_scalar) + 384×P_dense×(n+1) + 6×owned_bytes(frozen_synthesis,design)。九个完整记录所有者预留审计字典/标量 cache、返回完整证据、canonical 校验与 checkpoint JSON 投影；mean-only 仍支付完整 raw coverage。只占一个物理块的 TMM 数组不能代替所有同时存活记录。求解器继续用既有 `manufacturing_refinement_working_set_bytes` 模型。

worker 无 GUI cache、外部档案、远程/并行数组所有者。适配器显式按身份计入 worker parameters、冻结 job、work ledger、源设计和源训练/审计暂存，并从 worker 回调计入另外保留的 ParsedFrozenJobSpec（含其独立参数与 canonical JSON）及 queue_record，再由核心另计其 request/source、路线证据和求解器状态；没有把未知 archive/worker 所有权假定为零。只有确实无额外保留所有者的独立直接调用才可省略该回调；嵌入调用者必须声明其额外存活数据。source audit 还计入保留的完整训练源。模型为保守数值准入，不是 OS 硬内存隔离保证；JSON 传输和 worker 启动并不因此获得独立进程内存隔离。

work ledger 分开记录已准入完整调用、完成训练/审计、审计实际 evaluator 调用/点、完成 kernel F/FJ、完成训练点/film-layer×点、预留完整调用/kernel/点上界，以及峰值估算。完整 chunk 数沿用 evaluator 的 `min(10001, floor(1000000/(front+back+3)))` 分块，审计工作从既有 progress 的完成计数累加。中断后 `partial_physical_work_possible=true` 明确表示未完成调用可能已有未报告物理块；完成数不伪装完整实际总成本。

## 输出、验证与恢复

- result envelope 精确为 schema_version=1、kind=`coat_sim.film_manufacturing_result`、request、state（completed/failed/cancelled）、is_final=true、termination_reason、result、work。result 为核心 dataclass 的 JSON 投影；若源训练未完成则为 null。
- 完整训练与审计保留每波段/角度原始 errors、coverage、audit_coverage、objective_status、严格 nominal 与 solver_acceptable。`best_repaired` 与 `spectral_incumbent` 分开，后者必须保留更好的原已审计光谱。停止不是达标证明；空层路线没有伪造的最薄厚度。
- `result_matches_job(result,job)` 严格绑定完整冻结请求；`validate_result_payload(result,expected_job=job,replay=False)` 返回规范原始 JSON，验证原始条件与 raw evidence 的 canonical arithmetic、拓扑/边界、排名、计数、终止状态和来源身份。此检查也可能随证据大小较慢，应在后台线程执行。
- `replay=True` 会重新调用数值训练/独立审计，必须显式离开 Qt UI 线程；scheduler completion/cancel 不做数值 replay。既有已验证 context 供 UI 应用栅栏读取，不能为省事在 Qt 上调用 replay。
- 源完整独立审计后保存精确 job 绑定 checkpoint，终止结果再覆盖同一指针。崩溃恢复只验证已保存完整证据而不宣称新物理证明；未完成 worker 的接受取消不自动读取 checkpoint。`terminal_reply` 在 cancel-wins 时仅改变任务状态/原因，保留真实候选原证据，不补做审计。
- 错误不降级为另一作业或 generic simulation asset。持久队列通过专门 typed capability 提供只读查看；源训练-only 也只能作为未完成审计证据查看。应用仍须用户明确选择，并由 GUI 核对原编辑器源、修订、材料与冻结目标。

上述 admission/ledger/ownership 规则未找到外部依据，仅为本项目实现。现有数值接口参考：[SciPy bounded minimize_scalar](https://docs.scipy.org/doc/scipy/reference/optimize.minimize_scalar-bounded.html)，不作为本资源估算获得外部验证的依据。

### 组合终态输出的独立峰值阶段

`max_evaluations` 的单位严格是完整候选调用：一次完整独立审计只计一次，不把内部所有 dense/offset/scalar evaluator 或 kernel 上界扣成该配额。两路等候选配额不是相同物理工作量。work 与 estimate 都携带该单位说明；实际物理计数与完整资源上界另列。

单次审计的九个记录所有者不能冒充整个终态结果的峰值。完整核心结果最多保留 7 个不同 source/start/current/champion 对象，asdict 则展开为 9 个训练分数/角色设计（source + 六个路线角色 + best + incumbent），以及 5 个独立审计分数。适配器在每次光学准入（含源训练）之前，另算整个未来 A/B 输出的完整上界，并采用 max(数值阶段峰值, 组合输出阶段峰值 + 实际外部所有者)，不把两个不同时存活的数值/序列化阶段相加。

`_expanded_json_sizes` 不创建 asdict 或 JSON，逐字段按角色展开别名，分别计 P（解码后 Python 字典/列表/标量）与 J（ASCII JSON 字符串）。字典每项 64 bytes、列表每项 16 bytes 加空容器大小及额外字典 128/list 64 bytes 初始容量份额；标量按实际 Python 大小；Unicode 每码点最多 12 个 ASCII 转义字符。请求与设计的任意长字符串直接计量，不能用点数常数掩盖。固定 score 格式每条最多 16 字段的 condition 预留 4096 bytes、每原始点 512 bytes、每角度和每波段的 status 容器各 8192 bytes，审计另留 metadata。将完整审计最坏 coverage 带入 9 个训练/5 个审计的角色数，另计 17 个设计大小份额，涵盖 9 个角色设计、两个路线初始设计与 ID/bounds/诊断冗余，并给 envelope/work 预留 128 KiB。这些是明确的保守格式模型，未找到外部依据，仅为本项目实现。

组合输出阶段预留 128 MiB 运行时 + 4P + 2J + 两份冻结 request：原核心数据不超过一份角色展开 P，原 JSON payload 一份 P，校验时同时保留的重建 source/route 快照不超过一份 P，validator 最后返回的新 JSON payload 再一份 P；两份 J 覆盖编码/解码文字存活。scheduler 原 spec/canonical JSON/queue record 和适配器实际所有者另计，不拿源/路线角色的共享身份抵消 asdict 展开。`_envelope` 在任何大 asdict/JSON 分配前，再以真实组合角色进行非分配 P/J 核对，超出预先声明格式模型则拒绝，不能先分配结果再声称已准入。

非光学生命周期测试构造全部 7 个独立角色、best/incumbent 别名及完整 canonical 离散线证据，保留原结果、payload 和 validator 返回副本；tracemalloc 只观察 envelope 与完整结果校验阶段，不宣称包含整个进程或 OS 峰值。显式 chunk<1 检查与既有 evaluator 报同一错误，这是表示域校验而非新增层数 cap。
