# 膜系算法当前落地清单（只读核对）

源码目录：`C:\Users\youai\Documents\coating-dev\coating-sim`；本地 `git rev-parse HEAD` 显示 `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`。只读检查源码与契约；未跑测试、改生产代码或调用外部服务。

> 证据定位：下表出现的行号是本次只读读取快照中的定位点，并非最终冻结行号；稳定定位应以仓库相对路径、函数/类符号及下列源码 SHA-256 为准。读取时间：2026-10-20 03:21:09 UTC。

| 项目 | 当前层级 | 源码证据与限制 |
|---|---|---|
| N=0 增层、删层、换材、厚度精修 | **GUI + CLI + MCP 正式执行**。既有优化窗 synthesis 模式以裸片作为工作分支；当前设计仅在用户显式勾选时作为附加 seed。 | GUI 入口/worker：`coat_sim/ui/film_optimization_quick_controller.py:2570-2587,3734-3788,3946-3950`；请求适配：`coat_sim/ui/film_synthesis_quick_adapter.py:42-54,64-68`；核心裸片/seed：`coat_sim/core/film_auto_synthesis.py:637-668`，needle与有限增层：`:687-765`，厚度投影/精修/删层：`:507-588`，换材：`:590-625`；CLI：`coat_sim/film_synthesis_cli.py:65-83`；MCP计划和提交：`coat_sim/mcp/server.py:1187-1209`。有限增层默认试 `0.5/1/2` 个参考四分之一波厚度；另做极薄针插灵敏度探测，不是任意厚度完备搜索（request默认值 `coat_sim/core/film_auto_synthesis.py:50-56`）。 |
| 经典结构起步局部路线 | **核心 + 冻结 JSON CLI**；当前 GUI/MCP 没有这条路由。枚举模板/材料/波长/周期并逐个局部梯度精修、验收。 | `coat_sim/core/film_classic_local.py:142-166,249-346`；`coat_sim/film_classic_local_cli.py:1-36`。GUI模式白名单只有 `fixed_topology/structure/synthesis`：`coat_sim/ui/film_optimization_quick_controller.py:2570-2574`。 |
| 经典结构起步全局路线 | **核心 + 冻结 JSON CLI**；当前 GUI/MCP 没有这条路由。对枚举出的模板结构评分后，在固定拓扑厚度边界内做差分进化；需用户给有限厚度界限。 | `coat_sim/core/film_classic_global.py:165-215,274-306`；`coat_sim/film_classic_global_cli.py:1-36`。 |
| 经典“局部后接全局”路线 | **没有实现为串联执行器**。global枚举结构后直接把生成的模板候选作为DE起点；它没有先调用 local 的精修入口。 | global路径 `coat_sim/core/film_classic_global.py:279-305`；local精修入口 `coat_sim/core/film_classic_local.py:249-346`。不应把两条独立CLI说成现有复合工作流。 |
| 物理结构模板 | **GUI构造器可用**；CLI/MCP合成请求也可将结构spec扩成seed。**生成器不是搜索**：它展开一份真实层序处方，不排名、不优化。 | GUI catalog/表单入口 `coat_sim/ui/film_quick_controller.py:903-927,940-965`、`coat_sim/ui/qml/FilmDesignGeneratorPanel.qml:279`；适配 `coat_sim/core/film_physical_generator.py:195-216`；核心生成器 `coat_sim/core/film_physical_structures.py:405-484`；合成seed展开 `coat_sim/core/film_synthesis_io.py:96-109`。支持 multistack/coupled cavity/chirped/digital rugate/composite；构造本身仅按离散波长检查，验收限R/T，不保证制造或论文复现。 |
| 匹配过渡层/半波层包 | **可在GUI模板里手工表达；无专用自动匹配搜索**。通用layer支持显式光学厚度（默认0.25波），Bragg段可指定末端层；可以描述一份过渡或腔层处方，但不会自动发现/优化该层包。 | `coat_sim/core/film_physical_structures.py:29-46,78-83,409-411`。 |
| Beam/lookahead、多样性选择 | **当前合成主路径未实现**。多个用户显式seed作为轮转分支；每轮needle/有限增长采取贪心接受/择优，没有beam宽度、lookahead或多样性距离/选择器。 | `coat_sim/core/film_auto_synthesis.py:661-670,672-766`；经典多候选枚举不等于多样性算法。 |
| Phase引导 / phase homotopy | **phase目标和homotopy未实现**。有一项按离散复传播常数限制厚度步长的数值上限；它不是相位误差优化目标。 | 数值step cap `coat_sim/core/film_auto_synthesis.py:299-342,544-550`；物理结构声明只支持R/T且phase/GD/GDD unsupported：`coat_sim/core/film_physical_structures.py:476-483`。 |
| Projection repair | **合成路径已实现局部厚度投影**：厚度投影到非负、单层上限及`{0} ∪ [minimum,…]`，每个候选重新评分；不是通用任意制造约束修复器。 | `coat_sim/core/film_auto_synthesis.py:507-514,549-563`；其余可行性约束另检 `:277-282`。 |
| 自适应采样审计 | **GUI/CLI/MCP合成候选共用有限独审**：密网格、偏移点、从相邻样点括出的局部谱极值；明确 `continuous_certificate=False`。不是连续波段证明，也不会把训练网格改成通用自适应优化采样。 | `coat_sim/core/film_synthesis_audit.py:87-121,152-227`；合成档案触发 `coat_sim/core/film_auto_synthesis.py:469-483,664-669,769-785`。 |
| 角度鲁棒性 | **可优化/审计用户指定的有限角度集合**，经GUI/CLI/MCP入口；**不提供连续角域保证**，样点间不推断通过。 | 角度目标与输入校验 `coat_sim/core/film_optimization.py:41-46,409-415`；audit逐objective/angle `coat_sim/core/film_synthesis_audit.py:96-116,152-171,221-227`。 |
| 正交评分与KKT退化 | 当前字典序依次看最大归一化违规、加权RMS、达标后的层数/厚度成本；**没有独立KKT退化检测/证明**。 | `_synthesis_candidate_rank` `coat_sim/core/film_auto_synthesis.py:285-296`；局部收敛/停步 `:516-575`。 |
| Complex diversity | **当前主路径未实现**；无法从当前源码确认有稳定优势。显式seed多分支并不等于complex-domain diversity。 | 当前请求/分支工作流 `coat_sim/core/film_auto_synthesis.py:29-58,661-766`；未发现complex-diversity算子或执行入口。 |

主要正式用户入口是优化窗口内 synthesis 与物理结构构造器。助手 `plan_film_synthesis` 只准备冻结请求；实际MCP运行需 `submit_film_synthesis` 确认并进入持久队列。经典local/global仍是core/CLI入口。物理模板输出可作为seed，但“生成处方”和“搜索、评分、验收”是不同阶段。

## 本次检查的源码指纹与定位符号

| 仓库相对路径 | 关键符号（供最终冻结后复核） | 本次读取 SHA-256 |
|---|---|---|
| `coat_sim/core/film_auto_synthesis.py` | `FilmSynthesisRequest`, `_synthesis_candidate_rank`, `_phase_step_limits_nm`, `synthesize_film_design`, `polish`, `replace_materials`, `legal_growth_thickness` | `95DB8246D5D19D8F82AC855D9174A35A7ACC2ED1D3FEC0AF315AEC7F345B00AB` |
| `coat_sim/core/film_synthesis_audit.py` | `audit_frozen_synthesis_candidate` | `93D47EF02830F49E87F178CF9ED806CDBEF7B18DD208EB1A495A3EB21EF6CC59` |
| `coat_sim/core/film_synthesis_io.py` | `prepare_film_synthesis_payload` | `169FB063D464BCCAC01916890E3BE57432A92347BF959E854373221119A47BB4` |
| `coat_sim/core/film_classic_local.py` | `classic_seed_count`, `iter_classic_seed_specs`, `optimize_film_design_from_classics` | `39DDA9A1DFD1369317D63817EFF797F314B5CB93419A60E74DA1520BD622A917` |
| `coat_sim/core/film_classic_global.py` | `search_film_design_from_classics`, nested `evolve` | `6A8BECBF6EE30FCF8C2BD7C387E77BC8FF297B685DB37A0E8A5E6B74BD55E02B` |
| `coat_sim/core/film_physical_structures.py` | `FilmOpticalLayerSpec`, `FilmBraggStackSection`, `physical_structure_catalog`, `generate_physical_structure` | `E9CE902B700BFB942F8C74F7A55B328E2FD2F59749F9DA96579F04D4CAF36A84` |
| `coat_sim/core/film_physical_generator.py` | `MECHANISMS`, `build_physical_structure_preview` | `7B126215898764864996C3182BD225494624AF8C4A7C16BF43A4835E85AC7C4D` |
| `coat_sim/core/film_optimization.py` | `FilmOptimizationObjective`, objective validation/evaluation | `2D79A2909BE187BDF066696BC2231970333A9615C83CCF944C767422819568D8` |
| `coat_sim/ui/film_optimization_quick_controller.py` | `setSearchMode`, `_build_synthesis_request`, `inspectSynthesisQueueResult`, synthesis task routing | `5C193C453B429F1ADB90D0235156749D65577A9EA4F08DDCB37A70A1F8156B7F` |
| `coat_sim/ui/film_synthesis_quick_adapter.py` | `build_synthesis_request`, `synthesis_spec`, `synthesis_result_view` | `284E4F2AD21762CA05B54E6601FCB60411B8711B0376D1DFACACDDDFF684401A` |
| `coat_sim/ui/film_quick_controller.py` | `physicalStructureCatalog`, `previewPhysicalStructure` | `9F520FE41399F92D8E560DCA1DB84E39C7C5435AF3D1B1963DC4429E2B86C344` |
| `coat_sim/ui/qml/FilmDesignGeneratorPanel.qml` | `FilmPhysicalGeneratorForm` panel inclusion | `1D106246AC608843CAD92A162FBE0F57578AE15296A0603F7909F3B7E055D52C` |
| `coat_sim/film_synthesis_cli.py` | `main`, `_execute` | `9C90AD5A13B68B93EAB7E994F14BF7E6CB8650F044B99D1F1C2122A81D0C26A8` |
| `coat_sim/film_classic_local_cli.py` | `main` | `546AEAD6EBEF1F6FA85FBE9E39FD01C1C24F3532D3E7FE9149CB2894A02EC233` |
| `coat_sim/film_classic_global_cli.py` | `main` | `74A6AFE1DE2B9F56BDE5E9DDD179B12ADFF1D6A85C15C490445C7E4AE8B5C750` |
| `coat_sim/mcp/server.py` | `plan_film_synthesis`, `submit_film_synthesis` | `0704420556E01BADC27BA3419BB313BF71F8A60A3F4CA89B7272BBC5730E03E8` |
