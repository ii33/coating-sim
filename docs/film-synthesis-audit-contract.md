# 自动合成的独立光谱审计契约

当前更新（2026-11-01）：合成审计支持真实双面/背面入射设计；选面与未选固定面契约见[算法 14bd](algorithms.html#film-synthesis-selected-surfaces-20261030)。波段 mean/pointwise 两组、版本、raw coverage 与严格判定以[双约束契约](film-band-criteria-contract.md)为准。旧 flat 目标与旧协议不迁移。

日期：2026-10-20。实现真值：`coat_sim/core/film_synthesis_audit.py`。算法与离线图解同步见 `algorithms.html` 14t。

`audit_frozen_synthesis_candidate(request, design, *, cancel_event=None, progress_callback=None, dense_max_error_limit=None, ...)` 接收已冻结的 FilmSynthesisRequest 和完整合法层序，返回与统一结构评价器相同的 design/score/responses 以及 audit 工作量与覆盖元数据。它不读取实时材料目录、不改设计或请求、不拥有优化档案。合成调用是工作状态、训练最好解和已审计最好解的唯一所有者。无异步回写；进度回调在调用线程执行。

先计算既有验证倍数定义的固定密网格，再计算错位中点，逐目标/角度在两组联合点上寻找功率极大与极小的三点括区。局部有界极值搜索使用 λ=a+(b−a)t，t∈[0,1]，而非大数值绝对波长坐标。范围目标的上/下界与定值目标均检查相应功率极值。谱线只检查自身，不自动扩展成窗口。端点总是保留。逐点误差复用统一物理目标引擎。

score.max_error 和 errors 包含额外审计点；weighted_rms_error 始终取固定密网格，不因自适应重复采样改变目标权重。各目标单独报告最坏点、归一化违规、是否达标与覆盖范围。extra point 不计入固定网格积分。逻辑候选审计计一次 validation evaluation；audit.physical_evaluations 单独记录内部统一评价器调用数，wavelength_points 记录实际波长×角度条件数，不将它们与候选次数混淆。

同一目标、角度、波长、偏振的重复逐点条件出现不同数值时，若该点属于密网格则保留密网格见证；其他额外点重复时保留归一化违规更大的见证。覆盖中的该点实际值同步为被保留的条件值，并与独立的密网格 coverage 严格一致。额外采样不能覆盖并降低密网格最差违规。固定密网格 mean 与 RMS 不随额外采样重新积分；这是数值证据归并的本项目约定。

内部可选 `dense_max_error_limit` 只用于合成续接池恢复时、已有本轮完整独审冠军且所有启用目标无 mean 的情形；该限制必须是有限非负数。新密网格最大违规严格大于限制加 `max(1e-8, 1e-6·abs(limit))` 时抛出 `AuditCandidateDominated` 表示候选当前被支配，错位点和极值尚未计算。调用方不得把它当完整独审、成功/达标或物理数值失败；tie、近阈值和潜在优胜者继续完整独审，有 mean 的请求不可使用限制。新密网格用冻结源真实计算，其逻辑审计、物理调用和波长点仍按工作计数；中断/取消优先，未采样材料源域、窄峰和剩余目标点未被验证。筛除减少稀疏审计反馈，后续搜索可能不同，不承诺全面审计轨迹等价、速度或达标。完整审计是默认路径；非续接调用不得把部分筛除误报为审计结果。该规则为本项目约定，未找到外部依据，仅为本项目实现。

取消在物理调用前后和局部极值迭代之间检查，未完成审计不进入已审计档案。调用失败向上抛错，调用方保留先前完整档案。进度回调仅报告实际活动，执行入口另提供墙钟心跳。单次已经开始的物理 helper 不强制中断。

默认极值坐标容差 1e−10，数值迭代上限 500；这是局部求解收敛设置，不是整个结构搜索的计算预算。至少验证密网格和错位网格；不加入任意墙钟截止。高 Q 窄峰仍可能藏在未括住的区间，材料插值、相干和有限网格均有适用域；continuous_certificate 永远为 false。未知 k、域外波长和数值失败不能判为达标。

方法来源：SciPy minimize_scalar 官方文档 https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.minimize_scalar.html ；正确局部尺度和有限审计局限见固定研究库 01980d6e6a9b57d5de2c5aa989394d0b368a67c6 的 manual_studies/2026-10-19_exploration_round2/README.md。括区、错位点、RMS保留与归档组织为本项目约定，未找到外部依据，仅为本项目实现。

逐目标worst字段按最大归一化违规选点。若所有范围点都在界内，裁剪后违规均为0，最坏违规存在并列，metadata代表点不等同于最大/最小功率点；报告实际功率极值应遍历完整errors或responses。该并列语义不放宽验收。

## Active optical references and supported incidence (2026-10-20 correction)

The audit uses `film_design_service.active_material_references(design)`, the same reference selector as ordinary frozen forward simulation. For a single-sided substrate, only substrate, front layers and a material incident port are active. Inactive back-layer drafts and exit-medium fields are preserved but never looked up or sampled, including missing, unknown-k and out-of-domain draft references. A double-sided forward path activates both layer stacks and both material ports; front/back incidence exchanges the physical ports in the ordinary forward engine.

As of 2026-10-30, synthesis and its independent audit retain the actual complete optical geometry: a single-sided front-incidence substrate, or a double-sided finite substrate illuminated from either physical side. `selected_surfaces` selects mutable coatings, not separate optical objectives; fixed opposite-side rows and both external ports remain in every double-sided audit. The production `FilmDesignService._simulate_chunk` is the sole forward R/T truth, with unchanged coherent/incoherent substrate handling, exact grids and original objective reduction. A single-sided substrate still rejects back incidence and back selection. Fixed-stack identity is checked at synthesis, continuation, result and apply boundaries; the standalone single-front audit retains its existing inactive-draft source semantics. See [algorithm 14bd](algorithms.html#film-synthesis-selected-surfaces-20261030) and the [core contract](film-auto-synthesis-contract.md).

Every active source must be frozen and provide explicit finite positive n and finite passive known k at each actually audited wavelength. Missing active materials or optical sets, invalid source models, unknown k and coverage failures raise contextual `ValueError` naming material ID, optical-set ID and target intervals; sampled failures also name the wavelength. Compatibility zero defaults are never accepted as source evidence. Audit control flow, fixed-grid RMS measure, dense/offset/extremum sampling, extrema settings and cancellation ownership remain unchanged.

Extremum callbacks bind each bracket's objective, angle, endpoints, sign and scalar cache explicitly. Spectrum pairs use strict equal-length zipping. Type checking should use the official SciPy stubs (`https://github.com/scipy/scipy-stubs`); no import-ignore or dynamic import is added to hide missing analysis dependencies.

## Explicit unknown-k approximation (2026-10-20)

The former unconditional unknown-k rejection above remains the default. A frozen `environment.assume_lossless=true` explicitly permits k=0 only for an optical source with no k data. At every dense, offset or extremum point, invalid n or an existing k table outside its domain still raises; known finite passive k retains its value. The audit uses the same frozen policy as training/TMM and records `absorption_assumption`, including original source IDs and actual used affected references. Finite targets_met under this policy is conditional model evidence, not measured absorption validation; continuous_certificate stays false. No sampler density, RMS measure, minimizer, cancellation or budget rule changes. See [the policy contract](film-synthesis-unknown-k-contract.md).
