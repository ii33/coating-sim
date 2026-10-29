# 助手从零合成接线

本接口复用现行 `FilmSynthesisRequest`、冻结编解码、`film_synthesis`
worker 和原目标独立审计，不新增优化算法或预算。接口真值为 MCP 注册签名、
角色 allowlist、请求编解码及测试。

`film_synthesis_schema()` 返回现行请求模板和单位。模板默认值来自产品请求；
环境、实际基片、目标及候选材料/光学数据集必须由当前项目或用户明确指定。
R/T 和上下限使用比例，0.004 表示 0.4%，膜厚/波长为 nm。先用现有
`plan_film_synthesis` 只读冻结，再经 `estimate_compute_task` 和
`submit_film_synthesis` 的真实会话确认、幂等身份及持久队列运行。

`film_synthesis_job(job_id)` 从配置的计算根读取精确作业，并绑定该作业
冻结输入摘要；不猜当前焦点、PID 或文件路径。调度状态、核心终态、目标通过、
取消请求和错误分开返回。当前最佳摘要来自同一终态候选，或 worker 原子提交的
完整审计检查点，携带物理 hash、来源和审计身份。只读查询不重新做物理审计，
`replayed_by_this_read=false`，不凭“任务成功”或空误差列表推断达标。
无候选、无搜索状态与运行中分别明确表示；损坏或错源记录明确报错。

`plan_film_synthesis_continuation(job_id, request_id, limits?, current_design?)`
仅对已结束作业准备续搜请求。原始冻结输入、结构来源游标、已尝试集合、
待搜索结构及随机状态通过现行 checkpoint 校验器；不重建初始搜索、不复用
旧得分/审计或求解器迭代。可选兼容编辑设计成为重新求值的种子。
limits 仅含 max_cycles/max_evaluations/max_seconds：省略保留原显式值，
null 明确无上限；没有另一份助手内部工作预算。调用只准备请求，之后仍须
原估算/确认/提交路径。运行中不创建并行续搜。源检查点不改写。

继续搜索不依赖任何目标进展交接策略。旧策略移除不等于删除真实 checkpoint。

当前执行请求/结果/队列使用 schema6，不带目标进展交接字段。v5及更早冻结
执行输入明确要求重建；本批不改写或迁移磁盘历史。该格式边界不是旧请求
可直接续算的承诺。当前格式的 checkpoint 仍由原校验器保留/验证。

保存的 UI 配置中两个退役字段仅作为不执行的旧数据读取，并显示移除说明，
以保留其余显式开关/边界。不存在旧策略类、setter、求解分支或执行入口；
读取不会回写或迁移磁盘配置，用户后续正常编辑/保存才导出当前设置。

当前窗口的 optimization.submission 在 synthesis 模式返回同一参数 envelope，
executor=film_synthesis、requires_plan_freeze=true。快照只读取现有 owner，
不在 GUI 只读锁内访问材料目录/冻结材料；plan 再按明确计算根冻结。
工具的 execution_project_root 必须与已选工程身份一致，不从焦点猜根。
编辑操作沿原 instance_id/expected_snapshot_id 守卫，增加窄的合成参数、
候选材料稳定行ID、基片材料/数据集动作；不开放任意方法或重计算动作。

这些准备、查询工具属于读取能力；讨论模式仍不能估算登记、提交、取消或写入。
提交沿现有运行时注入的 client_request_id 去重；回执丢失先 find，不改 ID 重发。
取消使用现有 cancel_calculation_job，回执不等于已取消。

结果应用维持原生候选审查和工程/来源版本守卫。现行通用 window_action 不开放
apply_candidate，因为它可能触发计算；本批不以适配为由绕过该确认边界，
也不写设计文件、自动保存、启动真实任务或修改用户工程。
