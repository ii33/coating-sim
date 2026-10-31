# 膜系资源评估核心契约

日期：2026-10-31。实现：`coat_sim/core/film_resource_estimation.py`、`film_resource_estimation_bounds.py`；图解及公式：[`algorithms.html` 14be](algorithms.html#film-resource-estimation-20261030)。这是数值筛查与实际候选审计，不是训练预测器或主优化器。

## 输入与唯一所有者

`FilmResourceEstimateRequest` 为冻结 dataclass：`synthesis_request: FilmSynthesisRequest`、`candidate_designs=()`、`audit_candidates=False`、`layer_search_limit=128`、`max_seconds=None`、`search_budget_reference=None`、`probe_max_evaluations=0`、`probe_seeds=(0,1)`。默认快速筛查不合成、不审计候选；显式开启验证可配置每种子逻辑评价窗口（UI 初始256）和候选审计。

选面仍指物理 front/back；环境仅清空所选面，固定对面保留。完整当前膜系放在 candidate_designs；原始指标、材料与工艺由已有请求契约验证。候选池不能为空。显式产品 seed 中的材料可按原产品语义保留；仅供观察/审计的池外材料冻源但不进入允许材料集合，不能成为任务约束内推荐。

`freeze_resource_estimate_request(service, request, cancel_event=None)` 深拷贝并冻结活动光路、允许候选及审计候选所需源。传入已有快照时只验证和复用；若缺必要审计源则拒绝，禁止从实时目录偷偷补齐。`resource_estimate_source_digest(frozen_request)` 是 UI/worker 共用源身份：绑定环境、原指标、选面、允许材料、显式种子、材料快照、工艺与参考搜索帽、审计候选物理身份；不含评估计算窗口。UI在完成时从新鲜当前输入重新冻结并比较该摘要，过期结果不当作当前结果。

评估会在自己的独立请求副本里去掉 `max_layers/max_total_thickness_nm`，仅记录为 `search_budget_reference={max_layers:int|None,max_total_thickness_nm:float|None}`，不改调用者请求。真实工艺 `minimum_nonzero_thickness_nm/max_layer_thickness_nm` 保留；没有工艺输入不补硬限制。总搜索帽仅供比较，超帽结构仍可被本次无总帽评估审计，报告 `within_search_budgets=false`，不能暗示原优化帽内已有解。

核心同步入口 `estimate_film_resources(service, request, cancel_event=None, progress_callback=None)` 拥有本次状态；它不提交、保存或应用膜系，默认不调用合成搜索；仅 probe_max_evaluations>0 时显式调用原生合成及其独立审计，成本可能接近自动合成，不能称快速预测。UI应在工作线程调用，不另算一套物理结果。回调在该工作线程同步执行，返回 `FilmResourceEstimateProgress(stage,message,completed_units,physical_evaluations,wavelength_points)`。

## 算法与证据

1. L0复用冻结与严格源域，默认未知k拒绝；显式未知k=0保留已知k且标条件。透明外媒下只对相同波长/角度/偏振及一致均值测度检查目标交集和R+T约束，不跨自由窗口或偏振混算。
2. 厚度数值LP只使用正带宽、包含正入射的 pointwise HR 约束；无损常n单前面半空间理想化。只接受常n表的理想化诊断，不把有限实测表宣称全频常数证书。有限辅助背景为外界/候选n与中点。系数先用(n−a)/sqrt(a)缩放再平方，np.errstate将下溢/上溢/无效范围转成numerical_unknown；LP用C/max(C)归一后还原原对偶并检查success、shape、finite、usage与目标值。结构零行的浮点微正B也只报numerical_unknown，不从其或solver infeasible推物理无有限厚度。仅精确R=1正宽窗保留所声明无损常n有限模型的解析诊断；顶层仍不判生产不可行。返回是浮点数值screening，不声称外向区间认证。
3. DP只用 pointwise HR；每个实际采样条件须全部允许介质无损、正实传播导纳。可以逐波长使用色散n。吸收/临界/倏逝条件跳过并列出；不得删除材料后仍称原问题不可行。单面MVP外的双面DP/厚度LP不适用。
4. `layer_search_limit=128` 仅为默认数值枚举窗口，UI必须可调，允许0；不是优化max_layers或永久层数上限。超窗口返回value_layers=None、excluded_through与exceeds_search_window；不显示“最多128层”。
5. 双面只改一面时，对无损传播基片且自动解析非相干的整件pointwise透射，用真实固定膜块TMM计算固定面透射上包络。功率互易被动性给 Twhole≤Tfixed。相干、吸收基片、mean-only、两面可改时不套该门。
6. 所有界都只是忽略部分工艺/厚度/跨波长耦合的必要放松，`claim_level=model_numerical_screening`，不叫certified，不把conflict当生产不可行。
7. 当前/显式候选经现有 `audit_frozen_synthesis_candidate` 的完整密网格、错位、局部极值审计。只有原始nominal全部通过（不以solver容差替代）、工艺及允许材料符合、完整审计完成的结构进入Pareto。总层/物理nm厚度按所选面合计；固定层/基片不混入新增镀膜成本，inactive single-back草稿不查材料。
8. 推荐压缩节点也保留candidate_index、source_digest、sampled_witness证据等级、continuous_certificate=false、conditional_on_assumption和完整absorption_assumption及within_search_budgets；UI推荐卡必须显著显示有限审计与未知k假设，不能脱离原报告变成无条件预算。推荐仅为真实Pareto节点列表，不把某解最少层与另一解最薄厚度拼角点。没有见证就unknown和None。未知k假设在每个候选报告中保留 `conditional_on_assumption/absorption_assumption`；名义数值通过不是实测吸收验证。

快速筛查不提供未经标定的通用层数／厚度区间；条件必要下界不等于足够预算。没有可靠估计时保持未知。work.assessment_mode 为 fast_screen（合成和候选审计均关）或 verification；源验证、DP/LP 与固定面筛查仍有输入相关成本。

## 结果与停止边界

结果 dataclass 支持 asdict：state=completed/cancelled/deadline/failed，input_status=ready/invalid，estimate_state恒not_calibrated，feasibility_state=sampled_witness_found/unknown，source_digest/input_digest，selected_surfaces，search_budget_reference，bounds，screenings，candidate_reports，pareto_witnesses，recommended_budget，work，message，continuous_certificate恒false。

bounds每项含kind/applicability/reason/value_nm/value_layers/excluded_through/search_limit/claim_level/conditional_on_assumption/details。screenings含kind/objective_indices/state/reason/message/claim_level/details；conflict只是模型数值筛查。候选含candidate_index/state/targets_met/solver_acceptable/within_constraints/within_material_pool/within_search_budgets/layer_count/total_thickness_nm/surfaces/max_normalized_error/physical_hash/audit/absorption_assumption/message。surfaces含各物理面的active、layer_count与total_thickness_nm；未参与光路且无可用源的草稿厚度为None。

总ledger从L0冻结开始，包含source_freeze_attempts（已开始的源冻结验证次数）/source_freeze_passes（已完整完成的验证工作单元）、source_samples（冻结后本模块严格源采样数）、dp_conditions_started/完成dp_conditions、逐步累计dp_transitions、实际lp_solves、固定面物理调用、audits_started/完成audits_completed、physical_evaluations、wavelength_points、elapsed_seconds。物理计数是已完成返回的调用；审计返回前取消仍由retained callback计入，不丢已完成工作。源冻结与audit内部源验证以完整工作单元计，不冒充逐采样全局计数。

取消/时限在源采样、DP迭代和原审计检查点响应。未完成审计不提升见证；已完成的见证即使后续取消也保留。已开始的NumPy/LP/TMM调用不保证硬中断，墙钟可能越界，应报告真实时间。输入错误返回failed/invalid及具体message；后阶段错误仍保留前次完整证据，不制造预算。回调错误不等同物理不可行。无自动重试或后续优化提交。

## 来源与验证边界

物理依据：[Kats 2026 Eq1/Eq10](https://arxiv.org/pdf/2608.26633)、[Byrnes的功率归一化及非相干模型](https://arxiv.org/abs/1603.02720)。LP多背景组织、DP图松弛、枚举窗口、源身份/档案/调度是本项目约定，未找到外部依据，仅为本项目实现。测试包含Fresnel/Brewster、17层QW中心高反但宽带失败、固定面对面.96与相干T=1反例、mean门控、源假设、选面、工艺/搜索帽分离、取消与Pareto。没有训练预算预测器、真实材料标定或连续谱证书。
