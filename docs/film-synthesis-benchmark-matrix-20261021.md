# 薄膜合成验收基准矩阵（冻结矩阵 v1.1，2026-10-21）

本文件冻结多类别验收请求的范围与默认门槛，供后续按冻结材料快照运行。v1.1定义保留；下方v2记录后来完成的参考资格与默认链实验状态。本文所有目标窗、门槛、过渡保护带、预算和层数均为**本项目约定**，不是行业标准。状态 `must_pass_qualification_pending` 表示尚无经本项目独立 TMM 核验的可达处方；它可以用于探索性运行，但不得把搜索未命中当成算法失败或把参考目标称为已证可达。只有独立 TMM 核验的参考才具备可达性证据；若材料含未知 k 的显式零吸收假设，状态须保留 `conditional_on_unknown_k_zero`，只按条件结果报告，不计作无条件通过。

## 冻结共通条件

- 波长和厚度单位均为 nm；外部材料数据库原始单位需在生成冻结快照时换算并记录。入射介质采用项目数学模型 `builtin_ideal_air / ideal_air_n1_k0`。角度从表面法线量起。除分光项外，s、p 偏振分别验收并以较差者判定；单列 unpolarized 是 `(Rs+Rp)/2` 或 `(Ts+Tp)/2`，不可代替 s/p。
- 目标端点均包含。`guard_nm` 是目标窗外禁止计入目标的保护区；过渡区没有性能门槛。密集审计应在目标窗及保护区每 1 nm 取样，粗搜索网格每 5 nm；带边和保护区边界必须显式采样。另运行偏移审计：以 0.5 nm 半步长错开密集网格，并对候选极值波长及其 ±0.5 nm、±1 nm 邻点加密。以审计全局最坏点判定。上述点距是本项目约定。
- 各窗以对应偏振的最差波长验收，`min_R` / `min_T` 为下界，`max_R` / `max_T` 为上界。AR 默认以反射上限验收；只有显式声明基底、基片厚度、背面处理和吸收假设后才加 T 下限，因为单面反射与有限基片透射不是同一量。未给出的约束不隐含要求。反射/透射损耗和能量守恒按吸收材料的复折射率模型计算。
- 材料必须由冻结 catalog 的精确 `material_id + optical_set_id` 标识，记来源、n/k 域及其单位、数据哈希。只选每个目标域内 n、k 均有数据的光学集；未知 k 不可默认为 0。n-only 材料只有显式 `assume_lossless=true` 的条件基准可用，结果标 `conditional_on_assumption`，不得计为无条件通过。数据域以外禁止外推。若可用材料域不能覆盖要求，标 `blocked_material_domain`，不可悄悄缩小光谱目标。
- 公开矩阵不含用户私有项目路径或完整材料表。仓库 optical catalog 当前记录的 n/k 同域（单位 μm）如下：SiO2 `ri_sio2_rodriguez_de_marcos` 0.0299714–1.51066；MgF2 `ri_mgf2_rodriguez_de_marcos` 0.0299919–2.00146；ZnS `ri_zns_amotchkina_nk` 0.4–1.0；TiO2 `ri_tio2_jolivet_amorphous` 0.20664–0.82656；Ge 膜 `ri_ge_amotchkina_film` 0.4–12；YbF3 `ri_ybf3_amotchkina_nk` 9.0168–12。可见光/近红外候选优先从 SiO2、MgF2、ZnS 选；TiO2 只覆盖至 826.56 nm。NIR 使用 ZnS、Ge 膜；IR 候选 Ge、YbF3。以上只是数据域，不代表已验证适合任意层设计；基底同样须核对 n/k 域及吸收状态。对应来源：SiO2 [Rodríguez-de Marcos et al., 2016](https://doi.org/10.1364/OME.6.003622)，MgF2 [Rodríguez-de Marcos et al., 2017](https://doi.org/10.1364/OME.7.000989)，TiO2 [Jolivet et al., 2023](https://doi.org/10.1016/j.apsusc.2022.155214)，ZnS/Ge/YbF3 [Amotchkina et al., 2020](https://doi.org/10.1364/AO.59.000A40)。目录把数值指向固定 RII 数据库 commit；本项目保存的逐材料来源、原始数值哈希和网页 URL 以 `data/optical_catalog.json` 为准。基底材料也需确认 n/k 域及吸收状态。禁止仅凭论文光谱图声称精确复现。
- 可制造性缺省：`max_layers=32`，`minimum_nonzero_thickness_nm=10`，`max_layer_thickness_nm=500`，`max_total_thickness_nm=5000`；易/中/难依次限制到 12/20/32 层、单层 250/350/500 nm、总厚度 1800/3000/5000 nm。允许材料清单限定为对应候选集。除非某基准单独声明，所有层均须正厚度，不允许隐式基底/背面膜。
- 种子固定为 0、1、2。每种难度每个类别每个种子预算 120 秒墙钟时间；每个类别/难度合计上限 360 秒，完整 11 个 case×3 难度矩阵为 11880 秒预算。`active_workers` 记录期望值和每 1–6 秒实际活跃数量；另记录 CPU 核秒（parent、worker 各自及合计）、wall time、峰值活动 worker、退出/取消/超时和每种子的目标最坏值。不得以 19 请求值代替实际活跃数。易/中/难均为工程探索级别，不意味着可达性已证。
- 可达参考目前均需按下表状态处理。每类至少一个代表基准已定义，但在提供具体层厚参考处方并由独立 TMM 对训练网格、密集网格、offset 网格、极值点完成核验前均为 `must_pass_qualification_pending`。难搜索附加基准只比较预算内找到的最好值，不作必须达标门槛。

## v1.1 修订记录

- 将 HR+AR 混合多光谱两个 AR 窗从 `min T ≥ 0.95` 改为明确的 `max R ≤ 0.02`，HR 窗仍为 `min R ≥ 0.99`。
- IR 目标窗固定为 3–5 μm 与 8–12 μm；材料 n/k 域不覆盖时标为 blocked，不裁剪或静默缩小目标。
- 追加两个解析参考资格样例（单层 AR 与周期 HR），不替代任何冻结 v1 case。详细层厚、具名请求和 TMM 证据哈希见 [参考资格记录](../benchmarks/film_synthesis/20261021/qualification/reference-qualification-v1.1-public.json)。

## 基准目标矩阵

| 类别 | 代表目标（nm；包含端点） | 过渡保护带/窗间区（nm） | 入射条件 | 硬门槛 | 资格状态 | 难探索扩展 |
|---|---|---|---|---|---|---|
| 增透 V 膜 | 540–560 AR | 500–539、561–600 | 0°；s/p | max R ≤ 0.002 | verified_reachable_conditional_on_unknown_k_zero | 450–650 AR，max R ≤ 0.01 |
| 增透 W 膜 | 445–455、645–655 AR | 456–644、400–444、656–700 | 0°；s/p | 两 AR 窗 max R ≤ 0.005 | qualified_source_nk（v2新增处方） | 430–500、600–680 双宽窗，max R ≤ 0.01；需扩展材料域时标阻塞 |
| 宽带透射/增透 | 450–650 AR | 400–449、651–700 | 0°；s/p | max R ≤ 0.01；若另设 T 下界，须声明基底与有限厚度/背面模型 | qualified_source_nk（v2新增处方） | 400–700 AR，max R ≤ 0.02 |
| 超宽带透射（可见光） | 400–900 AR | 350–399、901–1000 | 0°；s/p | max R ≤ 0.02；若另设 T 下界，须声明基底与有限厚度/背面模型 | must_pass_qualification_pending | 350–1000 AR，max R ≤ 0.03 |
| 高反 HR | 600–900 HR | 550–599、901–950 | 0°、40°、50°；s/p 各自判定 | min R ≥ 0.99；原已冻结 HR case 作为单独基准保留 | 代表扩展 must_pass_qualification_pending；原 HR case 依已冻结协议 | 600–1000，0/40/50°，min R ≥ 0.98 |
| 宽带/超宽带高反 | 400–700 HR | 350–399、701–750 | 0°、30°；s/p | min R ≥ 0.98 | must_pass_qualification_pending | 400–1000 HR，min R ≥ 0.95 |
| 带通 | 500–600 pass；400–450 与 650–700 block | 451–499、601–649 | 0°；s/p | pass min T ≥ 0.95；两 block 窗 min R ≥ 0.95 | must_pass_qualification_pending | NIR 1200–1400 pass、1000–1150 与 1450–1600 block；需材料域覆盖 |
| 分光（50/50） | 450–650 分光窗 | 400–449、651–700 | 45°；s/p | 两偏振分别 `0.45 ≤ R ≤ 0.55`，并独立记录 T/A 能量余项 | must_pass_qualification_pending | 30–60°角锥最差点仍满足 0.40–0.60 |
| 偏振分光（探索） | 450–500、600–650 两个分色窗 | 501–599、400–449、651–700 | 45°；s/p | 450–500 窗 `min(Tp) ≥ 0.95`、`min(Rs) ≥ 0.90`；600–650 窗 `min(Ts) ≥ 0.95`、`min(Rp) ≥ 0.90` | exploratory_only | 只报告预算内最佳值；差分量作为派生审计统计，不作为优化目标 |
| HR+AR 混合多光谱 | 450–500 AR；650–750 HR；850–950 AR | 501–649、751–849 | 0°、30°；s/p | 两 AR 窗 max R ≤ 0.02；HR 窗 min R ≥ 0.99 | must_pass_qualification_pending | 400–1000 三个以上分离窗，扩展难度只报告最好结果 |
| IR 多光谱（exploratory_only） | 固定 3–5 μm 与 8–12 μm 两个目标域；不按材料域裁剪目标 | 窗间区 5–8 μm | 0°；s/p | 3–5 μm 设 AR max R ≤0.10，8–12 μm 设 HR min R ≥0.95；任一材料 nk 域不足即标 `blocked_material_domain`，保留原目标窗且不外推 | exploratory_only / blocked_material_domain | 仅当材料域完整时运行；不得外推或令未知 k=0 |

“宽带/超宽带”名称用于区分本项目用例覆盖范围，不宣称行业统一定义。普通分光表示分光窗反射率在 45–55%；偏振分光是独立探索项，分别约束目标偏振的透射及另一偏振的反射。透射差分仅作派生审计统计。HR+AR 指同一膜系的多波段功能组合。V/W 表示可见光透射谱中一个/两个高透窗口。

## 固定审计与报告字段

独立 TMM 审计必须由合成搜索外部的求值路径执行，并输出模型版本、代码提交标识、冻结请求与材料快照哈希、网格点生成规则、每个目标窗最坏值及其波长/角度/偏振、保护带边界、offset 审计和极值邻点。出现两种路径结果不符时，基准状态为 `audit_mismatch`。

每次 run 至少报告：`case_id`、`difficulty`、seed、请求/材料快照哈希、实际 layer stack、来源与 nk 域、unknown-k 处理、搜索版本、forward backend、wall budget 与实耗、requested/active workers 时间序列、CPU 核秒、训练与独立审计结果、`targets_met`、reference 状态、失败/取消/超时原因。参考待证和条件材料结果须显著标注，不能混入硬验收通过率。

## 来源与可达性说明

光学常数应沿用仓库 `data/optical_catalog.json` 中每个光学集自身的 source、notes 和域；目录目前标识其溯源数据库为 [refractiveindex.info 数据仓库](https://github.com/polyanskiy/refractiveindex.info-database)，commit `c5c2f188e848453def5970e347399d653df2ffc2`，CC0-1.0。该目录声明是 sourced reference，不是机器校准。生成逐次冻结快照时应记录材料条目所附论文/数据库页面的 DOI 或 URL；本矩阵未把仅有谱图的论文当精确复现证据。除 V-AR 外，冻结矩阵中的资格 case 仍为 `must_pass_qualification_pending`，直至有对应的可达参考处方通过 TMM 审计。V-AR 的 540–560 nm 原目标由具名 YbF3 n-only 处方在 `assume_lossless=true` 条件下通过，属于条件资格，不是无条件吸收验证。v1.1 附加的窄线单层 AR 与 680–720 nm 周期 HR 参考标为 `verified_reachable_added_case`，不替换或放宽其他冻结矩阵目标。除数据来源外，所有指标为本项目约定。


## v2 可执行代表矩阵与阶段结果

冻结时间 `2026-10-21T02:16:13.625911+00:00`；[具名请求 JSON](../benchmarks/film_synthesis/20261021/representative-suite-v2.json) SHA256 `61d2c533164f425409db83ba748c0215c45b24a498c819f8e90720f6e84fdad3`。这份v2固定实际材料和一个约束档次供默认链实验；v1.1较难宽带目标保持原定义，新增较窄代表不替代它们。

别名完整映射以JSON为准：ZnS-A=Amotchkina n/k，ZnS-D=Debenham n-only，YbF3-n=Amotchkina n-only，YbF3-nk=Amotchkina 9.0168–12μm n/k，MgF2-R/SiO2-R=Rodríguez-de Marcos n/k，TiO2-J=Jolivet n/k，Ge-A=Amotchkina n/k。各基底按单面半无限光学基底模型；膜态数据作基底模型不宣称块体实测。Air n=1。仅V与原用户HR显式允许未知k=0；已知k均保留。

| ID | 基底；候选膜料 | 目标（nm；功率0–1） | 角度/偏振 | 过渡区nm（无性能要求） | 层数/单层nm/总厚nm | 参考资格 |
|---|---|---|---|---|---|---|
| ar_v_visible | ZnS-A；YbF3-n,ZnS-A | 540–560 R≤0.002 | [0]° s,p | 500–540；560–600 | 20 / 350 / 3000 | qualified_conditional_unknown_k_zero |
| ar_w_visible | ZnS-A；MgF2-R,ZnS-A | 445–455 R≤0.005; 645–655 R≤0.005 | [0]° s,p | 400–445；455–645；655–700 | 20 / 350 / 3000 | qualified_source_nk |
| ar_broad_visible | ZnS-A；MgF2-R,ZnS-A | 450–650 R≤0.01 | [0]° s,p | 400–450；650–700 | 20 / 350 / 3000 | qualified_source_nk |
| ar_ultrabroad_visible | ZnS-A；MgF2-R,SiO2-R,ZnS-A | 400–900 R≤0.02 | [0]° s,p | 未另设 | 32 / 500 / 5000 | pending |
| hr_narrow_visible | SiO2-R；MgF2-R,ZnS-A | 680–720 R≥0.99≤1 | [0]° s,p | 650–680；720–750 | 20 / 350 / 3000 | qualified_source_nk |
| hr_user_600_900 | ZnS-D(n-only)；ZnS-A,YbF3-n | 600–900 R≥0.99≤1 | [40, 50]° unpolarized | 550–600；900–950 | 无用户上限 / 无用户上限 / 无用户上限 | confirmed_three_seeds_pre_explicit_strict_field |
| hr_broad_visible | SiO2-R；MgF2-R,ZnS-A | 400–700 R≥0.98≤1 | [0, 30]° s,p | 未另设 | 32 / 500 / 5000 | pending |
| hr_ultrabroad_visible | SiO2-R；MgF2-R,ZnS-A | 400–1000 R≥0.95≤1 | [0, 30]° s,p | 未另设 | 64 / 1000 / 12000 | pending |
| splitter_narrow_normal | SiO2-R；MgF2-R,ZnS-A | 540–560 R≥0.45≤0.55; 540–560 T≥0.43≤1 | [0]° s,p | 500–540；560–600 | 32 / 500 / 5000 | qualified_source_nk |
| bpf_narrow_visible | SiO2-R；MgF2-R,TiO2-J | 549.75–550.25 T≥0.95≤1; 500–520 R≥0.95≤1; 580–600 R≥0.95≤1 | [0]° s,p | 520–549.75；550.25–580 | 32 / 500 / 5000 | pending_reference_optimization_source_variant_v2b |
| mixed_hr_ar_visible | SiO2-R；MgF2-R,ZnS-A | 680–720 R≥0.95≤1; 520–540 R≤0.02 | [0]° s,p | 540–680 | 32 / 500 / 5000 | exploratory_production_audited_success_dense_request; fresh_frozen_suite_pending |
| ar_nir | Ge-A；MgF2-R,Ge-A | 1200–1600 R≤0.03 | [0]° s,p | 未另设 | 32 / 1000 / 10000 | pending |
| hr_ir_known_domain | Ge-A；Ge-A,YbF3-nk | 9200–10800 R≥0.95≤1 | [0]° s,p | 未另设 | 64 / 3000 / 60000 | pending |
| bpf_broad_v1 | SiO2-R；MgF2-R,ZnS-A,TiO2-J | 500–600 T≥0.95≤1; 400–450 R≥0.95≤1; 650–700 R≥0.95≤1 | [0]° s,p | 450–500；600–650 | 32 / 500 / 5000 | pending |
| splitter_broad_v1 | SiO2-R；MgF2-R,ZnS-A | 450–650 R≥0.45≤0.55 | [45]° s,p | 400–450；650–700 | 32 / 500 / 5000 | pending |
| mixed_multispectral_v1 | SiO2-R；MgF2-R,ZnS-A | 450–500 R≤0.02; 650–750 R≥0.99≤1; 850–950 R≤0.02 | [0, 30]° s,p | 500–650；750–850 | 32 / 500 / 5000 | pending |

全套种子0/1/2，每case每seed墙钟120秒、2候选worker、每进程1数学线程、BelowNormal优先级。默认请求无处方。层最小正厚度10nm（原用户HR保留null），未指定的cycle/evaluation预算为null。训练最大5nm；审计细分5份，最大1nm并有0.5步错位及极值；窄BPF训练0.1nm/审计0.02nm。只按目标窗判定，过渡区没有隐含门槛。产品默认网格细分仍为4，新基准显式取5；旧参考若曾按4运行，不得写成1nm，追加更密审计另记。

原用户HR为40/50° unpolarized，严格R>0.99，仅该case显式严格索引0；不能以它替代v1的0/40/50° s/p更难目标。v2其他范围为含端点，基准acceptable_normalized_error=0，不允许归一化容差掩盖原始门槛。

新增资格证据：[AR V/W/broad](../benchmarks/film_synthesis/20261021/qualification/ar-qualification-v2.json)、[splitter/BPF](../benchmarks/film_synthesis/20261021/qualification/filter-splitter-qualification-v2.json)、[mixed默认探索](../benchmarks/film_synthesis/20261021/qualification/de-production-case-v2-public.json)。目前V/W/broad、窄HR、窄splitter有可达参考；mixed已有不同密请求的生产默认探索成功，但v2新请求仍须冻结后实验。BPF仍未资格通过，其最佳扫描Tpass=.94227、Rstop=.94949分别差.00773与.00051，后续优化不能据此宣称不可达。超宽AR、宽/超宽HR、NIR/IR与v1宽滤波目标仍是困难探索/资格待定。

每轮输出参考资格、默认三种子命中率/首次审计时间、最差原始值及位置、层数总厚、真实工作量、worker CPU增量、心跳与取消/清理；不能以平均loss、参考处方命中或单次最好值代替验收。源哈希随每轮保存，代码修改后不沿用旧确认性签署。

## v2 参考资格补充（后于请求冻结）

窄带通TiO2-J/MgF2-R变体已通过：9层对称处方、通带549.75–550.25nm最小T=0.950087，阻带500–520/580–600nm最小R=0.950223/0.950307；正式审计通带0.02nm/错位0.01nm，阻带1nm/错位0.5nm、6个极值，全部s/p目标满足。splitter亦追加1nm审计通过。请求冻结时的pending元数据保留，不据结果倒改目标或层数约束；最终参考证据以qualification文件为准。

自然DE覆盖另有更紧的独立混合请求（680–720nm R≥0.99，520–540nm R≤0.001，0°s/p，32层/单层10–500nm/总5000nm），不是将旧混合case改门槛：默认空栈在19.608秒以14层审计通过，HR最小0.9901305574，AR最大0.0009996181。实际执行三参数结构DE初代加4变异代，共120提案全部消费、2workers，无回退/残留；训练物理1548次/465948波长点，审计162次/48158波长点。随后的全维厚度DE未执行，不能据此宣称覆盖。记录保存前一较宽松mixed探索结果，保持源版本和实验资格分开。

## v2.1 运行前材料身份纠正

预检发现v2原用户HR基底的Debenham光学集误绑定到ZnS膜料ID，导致该case冻结失败；未运行该套件的优化。保留原冻结文件，另冻结[representative-suite-v2.1.json](../benchmarks/film_synthesis/20261021/representative-suite-v2.1.json)，SHA256 `612c88b4d830a44dc1de9eb4009dc99d467dae170084e5b668dcd65526d0c627`，将基底ID改为真实保存请求的`builtin_research_ZnS_substrate`。全部目标、预算、厚度/层数约束及其他15例绑定不变。正式运行以v2.1为准，不能把错误绑定的v2当已验证请求。


## v3 超宽参考与带通默认结构阶段（2026-10-21）

前表保留冻结时点的元数据；当前资格以追加证据为准，冻结请求/阈值不回写。[ultrabroad-qualification-v3.json](../benchmarks/film_synthesis/20261021/qualification/ultrabroad-qualification-v3.json) SHA256 `472d61ea02666036b07f79e0057f720d79288e14a9b5d14a2d1b902316136d6d` 新增400–900nm超宽AR可达参考：ZnS-A基底、MgF2-R/ZnS-A 7层，总厚162.271868nm，0°s/p最差R=.01999958957600149@400nm；1nm密网格、0.5nm错位及6个极值审计通过。它是独立可达处方，不注入默认搜索。最小厚度因浮点误差投影到精确10.0nm后重算通过；不同的10.01nm投影失败，也保留记录。

宽HR400–700nm R≥.98（0/30°s/p，32层/10–500nm/总5000nm）与超宽HR400–1000nm R≥.95（同角偏振，64层/10–1000nm/总12000nm）各预定300秒、单worker/单数学线程、BelowNormal。分别3036次/120次候选搜索后最好采样minR=.8237731498/.6443311，均未产生通过原训练门槛的处方，故没有正式审计资格。两项仍待参考与默认链确认；这不是不可达证明，也不能与资格已过的窄HR混算通过率。

带通原默认的三次120秒0/3控制已保留；新增单腔/耦合腔/端匹配候选后，在同一冻结目标及制造约束下三次均由11层原始自动结构直接通过独立审计，minT=.9554999478，两阻带minR=.9825435537/.9869735420。实际DE及局部精修均未运行，不能将这个成功称为DE性能验证。生长耗尽仍须继续未消费提案/连续搜索的回归已补齐；该阶段同源334项核心/运行器测试通过，完整项目CI未运行。

400–900nm超宽AR原默认同预算三种子仍未达R≤.02，保留其失败作为下一轮薄匹配结构扩展的对照，不因已知7层参考而冒称默认算法通过。


## 阶段23当前结果（冻结请求不改）

AR薄匹配家族已接通默认GUI构造/冻结/持久任务/执行器：超宽AR400–900nm R≤.02由阶段22的120秒0/3改善为同预算3/3，7层、最差R=.0199800093793；真实结构DE4个变异代与局部精修，实际串行。其余七类各3/3，共8类24个有限审计成功。最终源码`feacc127b4ee4a5841338c2e1596bbc18792679e4e5c02571d2620072662cbc7`、355项统一回归通过，完整逐项结果见[阶段23](../benchmarks/film_synthesis/20261021/results/representative-v23-matching-final.json)与[报告](film-synthesis-extension-results-20261021.md)。

冻结矩阵hr_user_600_900的零容差/审计倍数5变体seed0仍失败：训练>.99但密审计minR=.9899932589247@657.5762nm/50°非偏振，120秒取消，不能计入8类成功。原保存请求使用容差.001/倍数2，最终同源19workers严格原始R>.99回放通过；两者分开报告。宽/超宽HR参考搜索、NIR/IR、宽带通/宽分光/复杂混合仍未完成资格，保持原门槛与待验证状态。本地实验已收束，后续超宽AR结构研究由父端云主机承担。
