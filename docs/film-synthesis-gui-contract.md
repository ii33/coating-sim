# 从零自动合成与物理结构GUI接线契约

日期：2026-10-20。本轮在既有膜系优化页和膜系设计参数侧栏接入现有v3核心，不创建窗口，不改变TMM、目标定义或结构构造公式。v3的62项冻结及514测试属于历史核心阶段；本轮单独记录实际改动与GUI验收。数据结构/签名与契约测试为接口真值，以下解释状态边界。

## 优化页输入和唯一状态所有者

现有`FilmOptimizationQuickController`继续拥有当前目标、材料/数据集选择、输入草稿、候选展示及应用意图。现有`FilmOptimizationRunCoordinator`继续独占窗口的一次队列尝试、worker观察、busy/取消/关闭生命周期；scheduler继续独占任务及持久心跳，不增加第二套队列。项目`ProjectComputeWorkspace`及`ComputeTarget`捕获当前工程root、generation和operation，不能借用助手全局配置root替代当前工程。

新增从零自动合成模式使用当前会话的物理边界作为裸栈环境，明确从N=0起步；当前处方只在用户显式选择时作为附加seed。输入包含当前逐项目标、用户选择的真实材料/optical_set及可选约束。空的总轮数/评价预算为None，不继承旧模式的有限默认预算；所有明确约束继续生效。单面front适用边界及真实n/k域检查由现有核心承担，不静默把双面、back入射或未知k转成受支持条件。准备/失败不修改来源设计。

队列输入通过现有film_synthesis适配冻结，任务协议保存求解需要的设计/目标/活动材料快照及来源revision/editEpoch/项目generation。GUI在同次提交准备中另捕获候选将保留的可编辑环境/工作流完整材料闭包，保留原provider完整记录；交集物理数据必须与活动快照精确一致。该GUI闭包不加入核心请求或历史队列协议，非活动材料仅保存，不新增n/k求值或暗设k=0。提交后编辑不会改变已排队输入；旧revision、来源关闭、项目关闭/重开、另一轮attempt或取消竞态的迟到结果不能回写。准备和提交错误保留有效输入并显示错误；加载、没有材料/目标及失败状态分别显示，重复提交禁用。

## 结果、取消与应用

既有进度和结果区显示阶段、实际工作量、层数、最好指标、心跳/停滞状态及完整审计候选层表。任务完成状态与`best_candidate_audit`物理合格事实分离；取消任务不改写已审计候选的真实分数，尚无完整审计的层表与分数明确标记为训练候选。取消沿用协作式队列协议，不终止用户进程，心跳/无响应诊断不自动取消。

项目内“查看候选采样光谱”显示本页任务冻结的目标采样点，不声称完整连续谱。正常完成的手动应用沿用确认流程；用户显式取消当前尝试，或工程队列接受同一活动作业的取消时，按[取消应用契约](film-optimization-cancel-apply-contract.md)自动应用一次完整合法最好候选，不补长审计，不自动保存。Controller校验operation、job、origin、工程generation、session、revision、editEpoch和使用的冻结材料来源；冲突保留候选且不覆盖新编辑。关闭、销毁、detach、shutdown、未请求的取消及历史浏览不产生应用意图。来源会话原子持有应用与一次撤销；相同候选不改dirty/revision/undo，仍请求一次当前版本光谱刷新或复用。未审计候选可以显示训练事实和层表，但不得标为已审计或目标达标。

当前运行的项目候选另存和应用携带提交时另存的完整可编辑材料闭包，不从后来修改的实时材料值补齐。应用按完整可编辑文档引用闭包校验；缺少冻结记录时明确拒绝并指出缺失材料。历史队列不携带GUI额外闭包；其活动快照不足以满足完整可编辑引用时明确拒绝另存，保留查看层表和候选目标采样图，不从实时目录补值。项目首次保存沿用原文档保存协议，撤销恢复原设计及材料provider，并保留应用后合法的其它工作流材料变更。独立 `.cfilm` 只保存材料引用而不嵌入快照；若当前目录中的相同材料/数据集内容已漂移或缺失，拒绝此另存并提示采用项目候选保存。

## 参数生成器输入、输出与会话

现有`FilmDesignGeneratorPanel`提供当前核心支持的多堆/匹配、多腔/耦合镜、啁啾、数字rugate及组合段参数。角色绑定来自当前工程材料/optical_set；用户通过表单编辑真实构造参数并预览逐层物理nm处方，不用研究脚本或文献常数替代当前材料。参数和角色变化使旧预览失效。核心构造函数保持GUI独立，不保存或应用结果，不自动开始搜索。

`FilmDesignEditSession`继续唯一拥有设计和最后成功预览候选副本、材料provider身份与revision；controller继续守卫pendingEditEpoch。新物理预览沿用现有模板/参考设计的候选防篡改、过期材料拒绝、确认替换为未保存草稿及一步撤销规则。错误或取消保留原设计；应用保留文档身份和文件句柄，保存/覆盖/关闭确认均走原流程。适用边界不支持时明确错误，不静默清空有效背面或更换入射方向。

加载、空材料、失败状态分别呈现。侧栏折叠/切换保留参数、选择、焦点与滚动；输入变化清预览；延续现有Theme逻辑尺寸、无额外DPR倍率及窗口几何。没有新的窗口几何例外。生成器不把Ge有限增层+TRF研究邻域当成产品模式。

## 验收与版本边界

定向回归覆盖旧模式兼容、新N=0输入/真实队列、进度取消/候选事实、来源及工程过期守卫；生成器覆盖真实材料表单、逐层处方、应用撤销及未知k/域错误。隔离创建自有Qt窗口完成真实鼠标/键盘交互并检查可见布局，不重启或关闭用户窗口；无长研究搜索、软件提交推送或release。最终具体实现位置、签名与测试证据随实现同步更新；本轮产物集中`artifacts/film_synthesis_20261020/gui-integration-20261020/`。

## 2026-10-20 实机输入与用料表单修复契约

目标角度的已提交值与已登记的组草稿归优化controller所有；编辑控件保留可见文本缓冲，并通过`noteObjectiveAngleDraft(minimum, maximum, precision)`显式登记整组原文，不反射读取QML。逐字输入、全选替换和失焦/Enter提交时，不得用上一次数值或0覆盖未完成文本。角度下限等于上限是合法单角度，包括0°和非零；空白、未完成数字或非法范围要显示错误并阻止提交，不修改为另一角度。保存/重新打开配置保留合法实际值，未完成草稿不得伪装为可运行配置；实际队列request而非控件外观作为验收真值。

从零合成候选材料使用动态行，每行有稳定界面ID、工程material_id和optical_set_id。材料变化只重选该材料真实可用数据集；增删按稳定行ID处理，不能把下方行的选择或焦点串给前一行。空列表及不存在/已删除引用显式报错。controller唯一拥有行数据，QML只呈现和提交编辑；当前工程材料服务与资源稳定标识为来源，不以显示名称或数组位置作为身份。配置往返与已有配置兼容按实现类型/契约测试核验，不改变核心冻结wire。

物理结构表单面向用户称膜料及用途，不要求三种不同材料。常见高/低折射率两膜料即可使用；多个用途可指向相同材料/数据集，核心真实nH>nL等物理限制保持。只向核心提交当前结构确实引用的用料；具体腔层或附加段需要独立膜料时在该字段说明，可复用现有材料。显示真空nm、物理nm、光学厚度n·d/λ、弧度等原有单位，不新增物理公式或未知k放宽。

配置适配真值：`synthesis.material_rows`保存有序`{id, material_id, optical_set_id}`行，`id`仅为稳定界面行ID；材料和数据集ID沿用资源ID。缺此可选字段的旧配置从原显式选中材料对迁移；旧structure模式选择器保持。行顺序与ID在现有设计优化配置保存/打开时往返，不加入物理请求wire。实际冻结请求仅含合法material_id/optical_set_id候选对；重复对、空行、缺失数据集、空列表显式拒绝，删除后迟到的旧行操作返回失败。

`confirmObjectiveAngleRange`整组校验并原子提交下限/上限/精度；`exportOptimizationConfiguration`、请求构建与关闭守卫先消费已登记草稿，合法则提交，非法则拒绝并保留原文。QML的Enter、失焦和既有开始快捷入口同样走该组提交；精度输入保留科学计数法支持。未改变核心角度网格、目标/权重或停止条件。

## 2026-10-20 用户显式选择未知 k 无损近似

本次新增独立策略默认关闭；勾选只将未知 k 按0计算，保留已知非零吸收及严格n/波长域。它随请求冻结并贯穿计算、审计、保存和结果标注，不改材料源。输入/输出、唯一状态所有者、异步及取消/错误边界见[未知k策略契约](film-synthesis-unknown-k-contract.md)。本修订取代上文“任何未知k均拒绝”的无条件描述：默认仍拒绝，只有本项明确选择获得带标注的无损近似。

复审确认修复：旧显式points角度未明确确认时，切换/新增/复制/删除目标先消费当前草稿；未完成则保留目标及原文，给出确认提示。配置导出除角度组提交外也拒绝其他已登记的数值输入错误，自动持久化失败保留前一配置。结构化材料行仍是可编辑草稿，允许保存空/未选/重复暂态以恢复编辑；行错误可见，实际冻结/开始拒绝，无“导入即全部物理有效”的承诺。

## Unknown checkpoint work and rejected assumption edits

Recovered historical synthesis envelopes may contain null for unsaved cumulative counters, together with explicit work_counters metadata. They retain a valid independently audited candidate; the GUI must preserve the raw envelope, display unknown work explicitly and keep inspection/save available under existing frozen-material rules. Any integer placeholder needed only by legacy plot/result presentation is not a new statistical authority and must not replace null in exported payloads. The unknown-k control follows the same controller busy/shutdown/pending-edit guard, and restores its property binding if a setter rejection races a toggle.


## 质量未知状态与无效作业引用收口（2026-10-20）

Controller的只读conditionErrorsEmptySummary基于已绑定typed synthesis payload的候选审计事实提供两处空条件列表文案：无完整独审为未知，不以空列表推断全部通过。任务targets_met仍是任务训练/独审综合真值，候选事实与任务取消状态分开；Apply资格及来源保护不放宽。角度准备启动失败或准备后的提交提前拒绝时，清理该未提交attempt的project operation与prepared job ID，不将它们带入下一次操作。

无候选cancelled/not_found显示明确终态；历史结果保持只读保护并显示历史身份，不把人为只读标记说成原设计已修改。裸片基线图例绑定结果自身的合成身份，避免之后修改算法草稿改变旧结果含义。Controller继续是状态唯一所有者，QML只读呈现。新回归为tests/test_film_synthesis_deepseek_closeout_gui.py。
