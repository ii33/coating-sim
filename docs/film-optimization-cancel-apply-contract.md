# 取消优化时应用当前最好候选

日期：2026-10-20。适用于当前活动常规膜系优化及从零合成窗口；沿用现有工程会话、任务协调器、撤销栈和光谱窗口，不新增求解器或自动保存行为。用户明确要求取消时应用当前最好解，取代旧文档“取消候选只能查看/另存”的活动用户取消分支；失败、历史任务及关闭/销毁行为仍按既有边界处理。

## 输入、候选与唯一状态所有者

用户显式取消动作产生绑定当前operation ID及queue job ID的单次应用意图。当前优化窗口取消按钮，以及同一工程队列已确认接受的匹配活动作业取消，均可产生该意图；队列传输失败、非匹配/历史作业、关闭窗口、销毁、detach和shutdown不产生意图。协调器继续唯一拥有运行/取消/完成身份，Controller唯一拥有应用意图和一次性应用状态；核心唯一拥有最好候选。

“当前最好”是父进程在首次观察取消之前，按原顺序完整评分、完成已有物理/几何合法性检查并提交的最好可行候选。未消费的多进程结果、零厚针探针、半次评分及取消后到达值均不进入该档案。按现有同一评分排名选优，不因缺独立审计回退到较差旧候选。合法性不等同目标达标或连续光谱证书。

常规优化沿用已有完整训练best，取消跳过holdout；现有结构优化分支不得在返回本轮完整typed候选后又无取消信号重跑训练/holdout验证，取消采用便宜的结构/数据/输入绑定校验，正常完成仍保留原严格验证。合成单独维护完整训练候选档案；取消取其冻结快照，主candidate_design及training_score必须来自同一候选。只有该同一设计已有完整审计时才复用validation，否则validation_score=null、validation_complete=false，best_candidate_audit.available=false、targets_met=null。任务状态仍cancelled、任务targets_met=false。

新增合成result.candidate_provenance为可选附加映射，旧wire缺省为空：取消完整候选须selection=completed_training_at_cancel、training_complete=true、frozen_at_cancel=true、fully_scored_evaluations为真实完整可行评分次数、validation_matches_candidate准确标记是否存在同一候选审计。不用该字段绕过设计、有限数值、冻结材料与约束校验。无合法完整候选返回None，保留原设计并明确“未有完整有效候选”。


旧结构优化的取消桥保留 `candidate_verified=false`（尚未独立物理重放），不伪造验证事实；只在原开放的 `topology_audit.cancel_snapshot` 中记录 `source=worker_completed_reply`、`replay_performed=false`、`training_complete=true`、`candidate_available=true`。当前完整 typed 回包经封闭 schema、既有拓扑/制造约束、保存逐条件训练分数聚合及原作业绑定的便宜校验后才可进入此桥；普通完成与历史恢复的严格重放不变。`is_final` 保留其原有“算法终态”语义，不解释成已审计。

## 异步、取消及错误

取消不补充独立审计或物理重放来等待应用资格。正常取消采用可信当前worker已返回的完整typed候选；scheduler worker及daemon必须保留本轮已通过typed校验的取消回包，不得因cancel marker仍存在而丢弃，并不得用较早checkpoint覆盖。两类膜系任务及现有本地/远端调度分支均适用。完整回包除了自包含typed校验，还须与当前作业的request_id、规范化冻结请求/spec及队列input_digest绑定；自一致但属于另一输入的结果不可交付为本任务候选。绑定校验不得进行TMM或独立审计。取消没有现成完整候选时不启动checkpoint物理重放；只有显式崩溃/历史恢复继续原严格重放验证，且不冒充本轮主动取消的自动应用资格。检查点恢复依旧清楚区分原已审计候选及重放成本，不在取消响应路径偷偷运行长审计。无法快速确认候选有效/完整时保留原设计和可审查数据，说明原因。核心停止消费后先回收自己的worker/launcher/reader/writer，终态只交付一次。旧operation、迟到重复完成和新任务开始后的回调均不得自动应用或改变新任务状态。

应用前核验：当前operation及项目generation、对应原session身份、设计revision、未提交edit epoch、冻结材料闭包。源编辑、资源/session切换或材料冲突时，保留原完整候选及其来源供查看/另存/后续显式处理，显示具体冲突；自动流程不改变新编辑，也不重绑定旧结果。历史浏览从不触发自动应用。

## 原子应用、撤销与光谱

常规优化走已有applyOptimizedDesign的厚度/层ID规则；合成走applySynthesizedDesign的完整层序及冻结可编辑材料闭包。不得把合成塞进普通厚度ID映射。两条路径保持现有原子会话事务、dirty和一次undo，不保存工程/参考文件。已经与当前规范化设计完全相同的候选视为no-op，不制造额外dirty/revision/undo，状态说明保持当前设计，但仍请求一次同项目当前设计光谱刷新/复用；已有新鲜同版本谱可以合并，重复取消或终态不再请求。

成功提交后通过原工程已有单例光谱刷新路径计算当前提交revision；已有谱观察器与显式打开/计算须合并，避免重复启动。光谱异步结果继续由现有项目/session/谱generation守卫拒绝旧结果；设计应用成功但谱刷新失败单列报告，不回滚/伪造光谱。重复取消、重复终态、重复应用不添加第二次undo或第二次强制刷新。未达采样目标/未审计的取消候选在状态中明确标注，不伪装成功优化。

## 验收与文件分工

至少覆盖早取消无候选、已有合法最好后取消、未审计训练最好优于旧审计、真实多核在途/晚到结果、重复取消/完成、过期operation、源编辑/待提交字段/资源切换、无效候选、单次undo/无自动保存、设计与光谱同revision、相同候选no-op、匹配队列取消及关闭/shutdown不自动应用。

核心：film_auto_synthesis.py（与现有常规优化取消语义对齐），film_optimization_job.py仅取消返回后的重复验证边界；适配器/typed job：film_synthesis_quick_adapter.py、film_synthesis_jobs.py及必要IO校验，scheduler/worker.py、daemon.py、remote_server.py及result_capability.py只窄改上述typed取消回包保留、作业输入绑定/不重放边界，其他任务生命周期不变；GUI与队列接线：film_optimization_quick_controller.py、project_compute_views.py，必要时现有film_quick_controller.py定向合并谱刷新；独立测试另建文件。先协调文件所有者，保留当前其他改动。离线算法文档14x说明选择/计量规则。

上述取消提交、选择和自动应用规则均为“本项目约定”；未找到外部依据，仅为本项目实现。不是优化方法的外部收敛或连续验证结论。

## Source spectrum refresh coalescing (2026-10-20)

The simulation controller remains the single owner of calculation generation, current inputs, in-flight work and committed spectrum. Its existing no-argument requestCalculation slot keeps the manual force-calculation behavior used by the spectrum page and shortcuts. A dedicated no-argument requestSourceRefresh slot means “ensure the spectrum for the current source is available”: reuse matching in-flight work or a complete fresh committed result, otherwise calculate through the existing path. Reuse requires matching source/project/session generation, physical input signature, requested polarizations and unchanged optical-material input identity; it must never reuse a prior spectrum after a source/material change or a failed/incomplete calculation.

Only source-companion start requests in the existing launcher and project film host prefer this dedicated slot; legacy test/host controllers without the slot keep the current requestCalculation fallback. Source open/apply APIs and the explicit manual calculation action retain their signatures and manual force behavior inside the spectrum page. The pending-window start branch must use the same source-refresh route when it becomes ready.

A very fast real TMM may finish before Future.add_done_callback registration, making its completion callback run synchronously on the registering thread. The subsequent source-companion open intent therefore must coalesce a matching already-fresh result as well as a busy result. This is part of the same explicit cancel/apply request, not another numerical optimization or audit. Completion, cancellation and disposal keep their existing generation guards. A refresh failure is reported separately from an already committed design; no automatic save or rollback is added.

Material identity is bound to the inputs actually used by the worker. Project calculations derive their active optical identity from the frozen project material snapshot rather than a later live read. A completed result is reusable only when its existing material provenance agrees with that captured optical identity. A changed or unknown identity during busy source refresh queues one recomputation conservatively; it is never silently treated as equal. The source-only request rejects a replaced source session/service. No extra TMM or independent audit is performed for these checks.
