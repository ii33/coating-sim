# 经典结构起点与局部优化核心契约

日期：2026-10-19。这是自动膜系合成的第二阶段，仅增加经典起点局部路线，不改变第一阶段针插＋演化。接口真值为 `coat_sim/core/film_classic_local.py`、`coat_sim/film_classic_local_cli.py` 和对应测试。公式和离线图见 [算法图解14r](algorithms.html#film-classic-local-20261019)。

## 输入、枚举和适用范围

`FilmClassicLocalRequest` 组合第一阶段 `FilmSynthesisRequest` 与明确的 `wavelengths_nm`、`pair_counts`、`template_ids`、可选 `max_local_iterations`。共享请求提供裸基片环境、现有目标、显式材料、冻结快照、制造约束、容差及独立密网格；共享 `max_cycles` 属于针插演化，此路线拒绝非空值，不能暗中改为局部迭代预算。其他针插专用数值探索参数在本路线不参与计算。

未选 `template_ids` 时使用当前内置目录全部5类：单层QW、双层Q/Q、三层Q/H/Q、`(HL)^N` Bragg反射器、`(HL)^N C (LH)^N` 半波缺陷腔。调用方必须给出设计波长；选周期模板时必须给出层对数，不推断目标所需对数，不把生成器默认8对当成搜索范围。非周期模板的pairs为0，不按请求的多个对数重复生成。

`iter_classic_seed_specs` 按模板顺序、设计波长、层对数、所有材料角色笛卡尔积惰性枚举。相同材料可重复用于不同角色；腔层C独立遍历，不默认只取H。H/L排序、材料透明性和波段有效性由现有 `film_design_templates.build_preview` 实际判定，拒绝项也返回其spec与原因。候选ID由明确规格内容确定，不依赖随机UUID。`classic_seed_count` 返回请求内计划数：Σ模板 (波长数 × 材料数^角色数 × 周期模板对数数)。它是覆盖计数，不是计算上限。

仅覆盖请求的这5类现有模板和所选波长、对数、材料，不代表全部经典结构或全部膜系空间。现有生成器的 `MAX_PAIRS=64` 兼容边界保留，不新增默认候选数或计算量上限。种子若违反显式层数、单层或总厚度约束，明确标为不适用；本阶段不会静默投影起点而仍称其为原经典结构。

模板中心公式要求法向、透明、正实n，`nH>nL` 等条件；小于等于既有 `1e-9` 透明阈值的已知k保留原值。未知k不假设透明；已知k超阈值不清零。生成后局部评分使用真实 n(λ)/k(λ) 和全部请求角度、偏振与上下限，不将中心解析公式冒充目标波段性能。模板不适用与物理不可行是不同结论。

常量入射端口仅为调用现有模板生成器构造临时常量记录，它精确对应environment已声明的n；不是伪造膜材或基片。评分前恢复原常量端口，冻结快照不增加该临时记录。材料型端口直接复用其冻结真实记录。

## 局部优化与状态所有权

`freeze_film_classic_local_request(service, request, *, cancel_event=None)` 复用第一阶段冻结与严格光学域检查。worker可仅重放冻结输入，不读可变目录。`optimize_film_design_from_classics(service, request, *, cancel_event=None, progress_callback=None)` 是同步核心，本次调用独占计数、当前候选和候选列表，复制输入后工作，不写材料库、session或用户设计文件。

每个经典种子先完整评分并保留基线，然后用与第一阶段相同的纯结构评分/Jacobian、可行优先加权RMS排序与独立密网格验证接口。局部搜索只调整当前结构的物理厚度；不针插、不全局扰动、不变换材料。精确零厚度行可删除，仍不引入新材料或新层。

局部方法为按当前设计波长四分之一波厚度缩放的解析梯度投影/回溯：初始归一化步长0.25，接受改善后最多扩展到0.5，拒绝后减半；步长阈值和相对改进阈值沿用共享请求，默认分别 `1e-6`、`1e-9`。投影到非负厚度、显式单层上限及显式最小非零厚度集合 `{0}∪[m,dmax]`，超过显式总厚度则缩步；没有默认最小制造厚度。每个完整候选评分后立即保留更优结果，后续精修或预算不足不能丢掉已评分结果。这些具体规则为本项目约定，不是全局收敛保证。

## 预算、结果与取消

共享 `max_evaluations` 在本路线表示整次调用所有候选共用的完整网格预算，默认None。每个正在优化的候选预留1次最终独立验证；剩余不足2次时不启动新候选。`max_local_iterations` 为每候选显式局部步数，默认None。默认不设置候选数、总评估数或局部步数硬上限；收敛/停滞阈值仍会终止局部搜索。

`FilmClassicLocalCandidate` 分别保存规格、原始种子、种子训练评分、最好局部候选、训练与密网格逐条件误差、逐项目标状态、适用条件、局限、计数和原因。`FilmClassicObjectiveStatus` 的训练/验证布尔值为None表示尚未评估。只有训练和密网格的最大逐条件归一化违反量均通过共享容差，候选才可 `completed/targets_met=True`；权重不能放宽某个条件的达标门。

候选状态有 `completed`、`not_found`、`inapplicable`、`failed`、`cancelled`。全程不自动覆盖用户设计。`FilmClassicLocalResult` 返回全部已尝试候选、独立排序的ID列表、计划数、已尝试数、`coverage_complete`、`any_targets_met` 和冻结请求。完整枚举且找到达标项为completed；完整但未找到为not_found；预算使枚举不完整为partial。所有适用项均数值失败时整体failed，不冒充正常“尚未找到”；全部模板不适用则 `no_applicable_seed`，不推断任意膜系不可行。

整体cancelled仍可保留之前已提交的达标候选；`any_targets_met` 只描述这些已独立验证的候选，不表示枚举已完成或取消项达标。`coverage_complete` 只表示规格遍历完整，不证明连续域或全局搜索覆盖。局部迭代限到达后仍对最好候选复核。

进度包含阶段、当前候选ID、计划/已尝试数、总完整评估数、局部步数和最好误差。无限预算下不伪造预计剩余时间或计算完成百分比。取消边界沿用第一阶段：冻结数值采样、完整评估前后和物理分块之间轮询，已开始的helper返回后才中断；`validation_complete` 回调后再检查取消，然后提交候选。`candidate_complete` 是该候选提交后通知，可停止后续候选；整体终态回调是提交后通知，不追溯撤销已提交结果。

## 冻结CLI与文件契约

`film_classic_local_request_to_payload/from_payload` 使用schema 1、kind `coat_sim.film_classic_local_request`，严格拒绝未知顶层字段及错误版本类型，复用共享请求的数据类型。新JSON是计算请求/结果封套，不修改`.cfilm`或材料格式。坐标仍为基片向外的层表，真空波长nm、厚度nm、角度°、R/T为0–1。

`python -m coat_sim.film_classic_local_cli --request FROZEN.json --output NEW_RESULT.json` 可运行。请求文件省略时读stdin；输出文件省略时完整JSON到stdout；进度逐行JSON到stderr。CLI强制完整冻结快照，不接目录扫描或GUI，SIGINT设置协作取消。`--output`用排他新建，已存在时明确失败，不覆盖设计或旧结果。返回码：正常完成/尚未找到/预算部分完成为0，计算/输入失败为2，取消为130；结果state和coverage必须同时检查。

完整请求示例和真实CLI运行结果保存在 `artifacts/film_classic_local/`；该例使用明确标注的解析夹具，不能冒充真实材料。`frozen_request` 可用于重放，实际函数签名及测试为接口真值。

## 验证与后续

小型对照见 [阶段报告](film-classic-local-stage-report-20261019.md)。包括全部角色/独立C枚举、经典起点与局部后结果对照、显式1/2/3对Bragg结构、真实固定RII Franta MgF2/SiO2色散与原k、节点间超标、两类端口、预算、取消、失败和CLI文件隔离。有限采样与局部法不证明连续波段达标、全局最优、制造资格或商业软件性能。

经典起点加全局、局部与全局交替、GUI/队列整合仍为后续阶段；首阶段针插＋演化继续保留。
