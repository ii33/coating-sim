# 真实均匀层 AR 结构研究落地阶段（2026-10-21）

状态：本地集成与可迁移云包已验收，云端确认性搜索待父端执行。当前HEAD为4d5071c，既有v23为未提交算法工作，禁止从旧副本覆盖或合并；本阶段保留全部无关工作、旧结果与失败。

## 范围与文件分工

- `coat_sim/core/film_synthesis_ar_clusters.py`：无GUI、无评分的非QW双材料、一/二/三簇、总相位预算/层数扫描、两端薄匹配及可选第三低n外层；纯低维参数映射。
- `coat_sim/core/film_auto_synthesis.py`：默认纯AR从初始阶段交错非簇/簇结构，父入口统一评估/预算/档案/审计，低维全局先于梯度精修，后续释放逐层；显式`v23_matching`对照。
- `tools/export_film_synthesis_cloud.py`与`tools/run_film_synthesis_cloud.py`：公共冻结材料/请求与纯算法依赖包、可监测/可取消运行、源与请求哈希、原始最差值及两种均值统计。
- 定向单元/物理/默认GUI持久链/云包测试；同步离线`algorithms.html`、执行契约和README。

所有簇均展开真实可沉积的各向同性均匀层，完整膜系联合TMM；没有理想连续n、孔隙、蛾眼、RCWA或新的混合材料选项。论文均值不等于本项目成绩，经验参数和调度均属本项目约定。

## 预先冻结的验证协议

确认性主例沿用`benchmarks/film_synthesis/20261021/representative-suite-v2.1.json`的`ar_ultrabroad_visible`：Air入射，ZnS Amotchkina源n/k半无限基底模型；允许MgF2 Rodriguez-de Marcos、SiO2 Rodriguez-de Marcos、ZnS Amotchkina，已知k全部保留，无未知k取零。400–900nm、0°s/p各点R≤.02，最薄10nm、最多32层、单层≤500nm、总物理厚≤5000nm。5nm训练，独立1nm密网格/半步错位/括区极值，均为有限证据。

同一新源码两模式`v23_matching`/`clustered`，种子0/1/2、每次120秒、2候选worker、每进程单数学线程。各run保存首次匹配审计达标时间、完整处方/层数/总厚、最差R及波长角偏振、逻辑/物理/波长点计数、真实worker/CPU采样范围、心跳/取消/清理、依赖/源/请求哈希。不能将云墙钟与旧本机v23秒数直接相减称加速；主对照必须同主机同预算两模式。

V/W/宽AR、窄HR、窄分光、窄带通、HR+AR混合共其余7代表case也冻结同原材料/目标/约束/三个种子120秒，以原始最差条件验收不回退。V膜显式未知k=0的条件资格不升级为实测吸收验证。未达标保留差距，不改阈值或删角偏振。

平均λ指标为各固定dense网格上∫R(λ)dλ/(λmax−λmin)，平均logλ为∫R(λ)dlnλ/ln(λmax/λmin)，按各角度/偏振/目标分别列；离散谱线为该点值。均值只作附加描述，不代替最差门槛，极值追加点不混入积分。

## 本地与云端边界

本地仅跑必要回归、默认GUI请求→冻结→JobSpec→纯执行器的有界契约、包导入与短smoke；短smoke不得计达标率或不可达。完整120秒多种子、结构消融与更大探索留父端云主机执行。迁移最小需求为CPU Python及已记录兼容NumPy/SciPy/Pydantic、当前纯核心依赖闭包、公共冻结请求及光学快照、运行器与哈希manifest；无Qt、用户工程、凭据、材料库写回或pwsh运行依赖。

## 交付与审批边界

本地只整合并提供可审查文件清单及原始验证证据，不提交、推送或发布。既有GPU4d5071c普通推送曾被拒且无本次具体新批准，仍不重试。新算法提交若以后推送，会携带该未推送祖先，不能仅按文件分开就声称发布历史隔离。

## 本地冻结交付证据

同一最终源码26模块399项通过（75.52秒），28条既有PurePath.is_reserved弃用告警；定向Ruff通过，核心与云运行器独立复审接受。默认GUI请求→冻结→JobSpec→core契约确认两结构族及K=1/2/3候选、真实低维DE先于梯度、取消保留候选；这不是原生窗口交互或新结构光学性能验收。

云包103源文件、8公共冻结请求、两模式配对预检通过。原导出包ZIP SHA256 `5c9aa5a338dbc9ce8f502420f2a9cdf5dcc28eb1c774bbd99a695416b149dcc9`，bundle-source SHA256 `da0b39f10a46228acdaad202683b9116475f591113985125e39054d7246380db`。云运行器每1秒写心跳，30秒无进度标stalled、120秒标unresponsive，均仅诊断；SIGINT/SIGTERM合作取消、恢复handler。首次达标要求候选/训练/审计三身份及source均匹配，R/T分别报告均值，混合指标无跨metric总均值。

5秒短smoke在5.029秒取消，300逻辑评估、保留7层候选，训练最差R=.0261718146，未完成审计，不计达标。取消中1次serial工作未知，总物理/波长计数保留null；已知598次/60398点，不以已知部分冒充总量，实际serial、无worker启动。独立两任务包内IPC另确认2个worker、4次物理调用/404点、unknown0及清理完成；不能据此声称该短搜索使用并行。完整证据见[本地验收JSON](../benchmarks/film_synthesis/20261021/results/ar-clusters-local-integration-public.json)。
