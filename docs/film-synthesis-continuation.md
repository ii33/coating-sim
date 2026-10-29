# 自动合成阶段与结构续接核心契约

更新：2026-10-28；实现基线 `3368edcbf297300af30169ef6b89a9f6413e4645`。

## 状态所有者与阶段

`synthesize_film_design` 唯一拥有物理评价、预算、取消、训练/审计档案和阶段调度。UI 只持有冻结请求、返回结果及搜索状态，不修改状态内部字段。核心不操作编辑器，不直接保存用户设计。

默认 hybrid 顺序为：本轮结构筛选及实质局部优化 → 固定拓扑完整精修与独立审计 → 在不恶化最佳指标的条件下尝试减薄 → 结束本轮。未达标时精修最差归一化违规；达标后仍走最差安全余量局部阶段。目标未达到不再强制自动扩展下一轮结构，用户主动点击继续才推进原前沿。准备求解若形成数值零层，先结束该求解、完成光谱等价清理，再对清理后的层 ID/材料/层数独立确认收敛；不会把原变量数的收敛标志借给删层后的设计。`local_margin.canonical_preparation` 单独记录准备求解、清理前后层数、实际耗时和评价数；`local_margin.fixed_layer_ids` 保存最终固定拓扑会话的真实起点。若第二次又需改拓扑，明确结束为未完成，不继续减薄。算法定义、单位、限制与来源见 [算法图册14bb](algorithms.html#film-margin-continuation-20261026)。

`candidate_design`、`training_score`、`validation_score` 始终绑定最终返回候选。新增 `local_optimum` 保存减薄前设计、训练/独立审计、物理哈希、最差有符号余量和局部终止原因；它可能比最终候选更厚、余量更大。它不是自动应用授权。历史冠军优于本次新结构时，继续会话可返回历史冠军，但会明确记录选择和新结构局部结果。

## 最小 UI 接口

收敛身份分开记录为 `solver_terminal`、`best_feasible_trial`、`returned_best` 和 `audited_selected` 物理哈希。求解器成功只能赋予同一终态候选；较好的线搜索试探不能借用其驻点标签。只有最终固定拓扑的求解终态本身完成对应的独立审计且身份一致，才可给它数值局部收敛标记。达标后的余量分支仍要求原门限训练和独审均通过；未达标的可行性精修分支可以数值收敛，但其 `targets_met=false`、逐条件违规及独审结果必须保留。历史冠军或较好的非终态试探不能借用该标记。终态审计拒绝时保留已审计来源，标为 `audit_rejected` / `audited_fallback_before_incomplete_local_stage` 并停止，不进入默认减薄。

若最佳完整可行试探的最差有符号余量确实优于 solver 终态，返回该试探供原目标独立审计；审计通过后保留为最佳可行解，标记 `best_feasible_not_stationary` 和 `audited_best_feasible_before_incomplete_local_stage`，不宣告局部收敛、不开始减薄。只在归一化余量改善超过 `1e-10 * max(1, |best_margin|, |terminal_margin|)` 时区分二者，以免舍入差异丢掉真实终态的收敛身份；这是本项目选择约定，不放宽任何原目标训练或审计阈值。每次独立审计晋升也更新实时最佳显示，避免局部阶段仍显示旧的厚度优先训练冠军。

407 集成后的现有优化窗口增加“继续搜索”和结构搜索 / 局部优化 / 减薄 / 本轮完成状态。一次任务默认完成局部优化、减薄后停止，用户点击才开始下一轮。候选审计区分别列出减薄前局部结果及最终返回候选；局部的归一化安全余量不会冒充最终候选的余量。

界面仅在原文档 `workflow_settings.optimization.synthesis.search_reference` 保存任务引用及编辑器设置指纹，真正的前沿保存在原调度目录 `best-candidate.search-state.json`。查看、保存、撤销和兼容的膜厚/拓扑编辑不清空引用；保存膜系并关闭重开后，从原任务的不可变 spec/source binding 与状态文件恢复。当前编辑设计重新冻结并作为首 seed，原候选重新评分。目标/角度/验收、候选材料/吸收假设、原生厚度/层数约束或搜索参数改变时分别显示需要重建的原因，旧任务文件保留。改变预算、CPU、显示精度不重建前沿。

继续提交仍使用原作业协调器、排队取消、来源修订和 pending edit epoch 检查，自动写回沿用原撤销事务并刷新光谱和约束层列表。未完成、输入摘要不匹配、未知引用/状态版本、截断 JSON、缺失状态和冻结材料变化都明确报错；不退化成拿旧候选重新随机搜索。极早取消但未生成新 sidecar 时，可恢复该新任务冻结请求内继承的 checkpoint。

实时显示控制器归优化窗口所有。复用窗口切换来源时只解除旧来源订阅、清空旧帧并用新 generation/作业摘要重新绑定，不销毁显示控制器；真正关闭窗口时才停止显示执行器。新一轮尚无完整帧时显示等待状态，不复用旧一轮摘要充当本轮结果。

```python
from coat_sim.core.film_synthesis_continuation import continue_synthesis_request

next_request = continue_synthesis_request(
    result.frozen_request, result.search_checkpoint,
    current_design=editor_snapshot,
    design_revision=current_revision,
    max_evaluations=new_budget, max_seconds=new_seconds,
)
```

`current_design` 可省略。提供时先冻结校验、规范化膜厚、检查材料闭包和原约束，作为首个重新评价 seed。允许的新覆盖项仅为 `max_cycles/max_evaluations/max_seconds/design_revision`。修改目标/材料/约束/结构搜索参数会导致兼容性拒绝；调用方须保留旧状态，显式选择新的 `search_checkpoint=None` 重建请求，不能悄悄删除旧档案。

查看、保存和编辑历史记录均不调用重置。继续创建一个新队列作业，其新预算包含当前 seed、旧池重新评分、恢复后新结构和审计的真实工作。旧冠军不能使作业在结构探索前成功返回。一次继续包含一个既有源筛选片和一个有界新结构局部会话；不要求新结构优于历史冠军才结束。

最终候选继续走既有候选应用入口、冻结来源/作业身份和当前设计修订校验；自动写回仅在该既有保护通过后执行。撤销沿用原编辑器命令；核心不会覆盖未保存草稿，也不会因返回 `local_optimum` 自动应用它。UI 在应用失败或修订变化时保留结果供查看。

减薄不再设置内部固定总评价数、时长或求解次数上限。单次 SLSQP 保留16次迭代作为局部数值保护；达到该保护但仍有双质量审计通过的实质变薄时，继续同一拓扑求解。无有效改善、数值收敛、求解失败、资源保护、用户显式预算及取消均有真实 `stop_reason`；不会把32次评价量子当成减薄已收敛。尺度化厚度改善阈值沿用 `relative_improvement_tolerance * max(initial_total_nm, 1 nm)`，不增加用户目标或参数。中断只保留此前已通过训练和独审质量保护的冠军，不补做停止后的光学评价。

达标余量阶段同样没有固定总迭代或求解次数限制。单次256迭代是可重复的 SLSQP 工作分片；触限且最差归一化余量改善超过 `relative_improvement_tolerance * max(1, abs(before), abs(after))` 时，独审通过后在相同拓扑继续。`local_margin.solver_calls` 分别保存各分片真实评价、耗时和物理身份；无改善明确记 `scaled_stagnation`，保持 `local_converged=false`，默认不减薄。原目标、正式验收及共享预算不变。减薄的 `_Stopped(time_limit)` 与取消路径均冻结同一已审冠军对，截止后不追加审计，不返回中途未审训练试探替代它。

完整 minimax 的 `solver_terminal` 记录真实求解终点；终点物理非法时保留其坐标、t、hash 及 `physical_valid=false`，另以 `retained_terminal` 记录上一已接受候选。顶层 `terminal_*`、`optimizer_t` 和 terminal callback 都对应这个保留候选及自己的 t；`returned_best.is_solver_terminal` 只与真实终点比较，回退点不能借用原终点角色或驻点证据。

显式 legacy 路线仅复用既有充分轮停滞计数：全部当轮审计反馈修复后，同时检查训练最好与同一已审候选的最差归一化误差；任一实质改善均重置计数。它不声明梯度收敛、不进入无驻点依据的减薄，也未新增 legacy DE 内部状态恢复。真正结构前沿续接仍为现有 hybrid 接口。

## 持久化与恢复

`FilmSynthesisResult.search_checkpoint` 是带版本、指纹和 SHA256 的 JSON 值。独立回调 `search_checkpoint_callback(state)` 在已提交候选及终止边界发布深拷贝；异常原样传播并清理执行器。调度器复用现有原子 JSON writer，在最佳审计文件旁保存 `<stem>.search-state.json`；即使没有达标候选也保存结构进度。原最佳候选 checkpoint 的含义不变。

`execute_job(..., write_json)` 保持原候选观察者契约，只接收已审计设计。搜索 sidecar 默认单独调用现有原子 JSON writer；需要定制存储时可传 `search_state_writer=`，不向旧候选回调混入不同结构的数据。进度引用写入文档时只合并 `search_reference`，不保存临时变量行或改写其它编辑设置。

CLI：`--checkpoint best.json` 同时写 `best.search-state.json`；`--continue-search best.search-state.json` 与新请求配合。恢复输入不能与任何输出路径相同。`load_search_state(path)` 只读取最多64 MiB的 UTF-8 JSON；版本、形状、校验和、随机算法、冻结指纹和候选物理闭包均校验。截断、未知版本、不兼容状态给出错误且不覆盖原文件；无需 pickle 或反序列化 Python 对象。

状态覆盖 hybrid 实际8个工程源，包含消费游标、活动源顺序、轮转位置、待评原/随机候选、紧凑实际设计池、档案插入/父候选身份及轮转、已试哈希、根 seed 和下一旋转 seed、模板算法/NumPy版本、审计反馈。源生成器从确定性游标重建，跳过步骤不做物理评价。旧池评分与审计不复用。已展开的随机待评候选直接评分，不再生成第二个随机 extra。

既有 DE 群体只在一次旋转片内生存；局部求解器也在阶段边界结束。这里不序列化这些求解器的内部状态，也不承诺任意物理调用处的逐位精确求解器续算。恢复时保留真实候选与源前沿、推进下一旋转 seed，避免从根随机搜索重启。墙钟预算与并行派发可改变最终采用前缀，真实计数始终按本次作业报告。

## 结果和错误边界

`state=completed` 表示本轮按正常规则完成，`targets_met` 仅表示实际返回候选通过原门限训练及独立审计，两者分离。未达标完整局部会话可返回 `completed / targets_met=false`，以 `local_session_converged_targets_unmet` 或 `local_session_stalled_targets_unmet` 说明数值收敛或无有效改善；后者不是梯度驻点证明。终态已收敛但返回另一条更优非驻点候选时，使用 `local_session_terminal_converged_best_retained_targets_unmet`，不薄化或借用驻点标签。训练通过而独审拒绝则使用 `local_session_audit_rejected_targets_unmet`，不冒称停滞。预算、取消和数值失败不能改名为收敛。局部终态审计拒绝或求解未完成时，若回退候选的原目标训练与独立审计均通过，仍保留该达标结果；同时保留真实局部终止原因、`local_converged=false` 和回退身份，默认不减薄。界面区分“已审计回退候选”与真正的减薄前局部结果。

评价/周期预算耗尽时，若已有同时通过正式训练及独立审计的冠军，保留历史的 `completed / targets_met=true` 达标语义，并保留实际 `evaluation_limit / cycle_limit` 终止原因；`local_margin.local_converged=false` 和 `budget_limited=true` 明确说明局部阶段未完成，不执行减薄。这不把候选达标冒充局部收敛。续接仍要求完成本轮新结构工作，不能因旧冠军返回成功；取消、截止、审计拒绝和求解失败保留各自状态。

`structure_proposal` 消息继续保持原始 provenance 字段契约。提案、局部试探、审计和阶段切换的物理身份单列为可省略的 `FilmSynthesisTrace.physical_hash`；旧记录缺省为 `None`，不向材料/模板 provenance 混入字段。

跨进程回归比较的是同一 checkpoint 在进程内与新子进程恢复后的完整提案序列、预算、游标、待评候选、随机推进和反馈；与不中断运行只比较初始 BASE/extra 两个提案，不声称完整未中断轨迹逐位等价，也不恢复 SLSQP 内部迭代。

重复提案或 `source_draw_quantum` 结束一个有限片但源仍活动时，终止为 `continuation_quantum_no_new_candidate`，保存游标并允许用户继续。只有所有源及待评队列确已耗尽才报告 `continuation_frontier_exhausted`。

`performance.search.hybrid.first_feasible/local_margin/thickness_reduction` 保存阶段墙钟和真实评价、训练物理调用/波长点及独立审计调用/点数；未知底层物理计数保留 `None`。`continuation_session` 记录选中新分支、局部终止、原审计结论和历史/新候选的返回选择。

局部收敛、迭代保护、数值失败、资源保护、评价限制、时间截止与取消分别报告。达标后余量阶段未收敛不继续减薄。未达标分支仅在完整局部终态及对应独审成立后，尝试不恶化当前最佳指标的减薄；无可接受减薄候选时保留局部结果并结束，不等待目标达标或继续扩结构。资源拒绝或没有有效工作时结束当前片/会话，不能声称全局无解。取消/截止不开始额外验证，只有匹配实际返回候选的完整审计可以附加。新局部结果达不到原目标时可返回已审计历史冠军；本次终止为 `continuation_local_session_complete`，不声称新结构达标或优于历史结果。

当前请求协议为 v6；`search_checkpoint` 和 `enable_local_margin_refinement` 仍为可省略字段，省略时分别为 `None/True`。目标进展交接已移除，v5及更早冻结执行输入须明确重建，磁盘历史不自动迁移。候选 warm restart 仍是独立操作，不能显示为结构进度恢复。
