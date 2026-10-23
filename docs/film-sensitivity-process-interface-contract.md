# 膜厚敏感性过程模型接口合同

日期：2026-10-23。本文是 CLI、MCP 与持久化敏感性 job 之间的接口合同；核心请求/结果类型及其闭合反序列化器仍是字段和数值校验的权威。算法增量见[保留原名义规格的有限场景稳健优化合同](film-robust-optimization-contract.md)。这里不重复算法定义，也不以 JSON Schema 替代运行时校验。

## 适用范围与向后兼容

过程误差和保名义请求只属于独立 `coat_sim.film_sensitivity_request`。普通膜厚优化 v1 继续拒绝敏感性字段，不自动升级或忽略未知字段。

旧请求未写新字段时，按原行为解释：`optimization_policy="robustness_only"`、`error_amplitude_semantics="bounded_amplitude"`。这两个缺省值及零误差项在规范化请求身份中省略，以保持既有 canonical 身份与回放兼容。任何新增请求字段均经正式闭合 deserializer 校验；不能仅凭 MCP 暴露的 JSON Schema 接受请求。

## 请求字段

敏感性请求可显式设置：

- `optimization_policy`：`robustness_only`（旧行为，目标为敏感性/鲁棒性）或 `nominal_preserving`（只在保持原始名义约束下寻找鲁棒改进）。
- `error_amplitude_semantics`：`bounded_amplitude`（兼容旧的有界扰动解释）或 `standard_deviation`（每个幅度作为一倍标准差解释）。
- `error_model`：`independent`、`common_mode`、`material_common_mode` 或 `linear_drift`。模型定义与适用条件见算法合同和核心实现。
- `errors`：逐层误差项。每项有 `layer_id`，并可提供旧字段 `magnitude`/`unit` 或新字段 `absolute_nm`、`relative_fraction`。误差上界幅度为 `(unit=absolute_nm ? magnitude : 0) + absolute_nm + ((unit=relative_fraction ? magnitude : 0) + relative_fraction) × 名义厚度`；各项线性相加，不作 RSS 合成。旧单位 `absolute_nm` 与 `relative_fraction` 保持有效。
- `scenarios`（显式有限场景请求）中的系数由调用方给定并随冻结请求保存，不得将调用方未给出的误差、工艺默认值或系数静默补入请求。

显式场景生成 API 使用必填 `distribution`：`bounded_uniform` 或 `gaussian`，并接收样本数、seed、误差模型及可选前缀。生成结果必须把每个实际系数写入 scenario。`gaussian` 系数可超出 ±1 而不裁剪，并且请求必须使用 `error_amplitude_semantics="standard_deviation"`；有界均匀分布不得被描述成连续概率/制造良率保证。

`nominal_preserving` 仅接受 `finite_scenario_worst` 或 `finite_scenario_expected` 鲁棒性目标；`linear_sensitivity_norm` 作为优化目标与该策略组合时明确拒绝；`linear_drift` 是受支持的误差模型，不是该线性目标。原有名义上下界是硬约束，不添加 margin，也不放宽用户目标。冻结初态不满足名义硬约束时结果为 `failed`，终止原因为 `initial_nominal_infeasible`。若求解器末次试探未通过精确原名义验收，终态为 `partial`、原因 `constrained_nominal_rejected`；结果只保留此前完整评分且原名义可行的 incumbent，不将该不合格末点重标为可应用候选，也不以求解器成功报告完成。保留候选的名义通过状态仍独立按真实分数报告。

## CLI

CLI 继续只接受完整独立敏感性 request JSON（`--request PATH` 或 stdin），要求完整冻结材料快照；它不查询 live catalog，不提供另一套绕过冻结/校验的参数接口。输出是完整敏感性 result JSON（stdout，或新建 `--output` 文件；拒绝覆盖）。进度事件仍走 stderr。

进程退出码：`0` 表示 result `completed`，`3` 表示已计算但不完整/未满足严格验收的 `partial`，`2` 表示输入/冻结失败或 result `failed`，`130` 表示协作取消后的 `cancelled`。JSON result 保留实际状态、终止原因和完整最佳已评分候选；失败或取消不因曾有候选而升级为成功。

## MCP 与冻结 job 边界

`plan_film_sensitivity` 是只读规划：完整调用正式敏感性 deserializer 和冻结路径，返回规范化 request/估算，不创建估算确认、job 或调用 worker。`submit_film_sensitivity` 仍沿用已有估算、client request 去重、显式计算释放/确认及 scheduler 提交流程；它不是新造的直调计算入口。两个工具的 schema/docstring 应发现上述新增枚举、误差字段和幅度语义。

释放后的唯一业务执行路径是 `kind="film_sensitivity"` 的持久化 job。`film_sensitivity_jobs.build_job_payload` 冻结服务材料与完整请求，job spec 中的 payload 必须包含并保留新策略、幅度解释、误差模型、误差项和显式场景系数；`execute_job` 对该冻结 payload 实际执行核心计算、产出 checkpoint/result。禁止在 MCP/CLI 入口旁路 job 身份、材料/设计/目标绑定、client request 身份规则、终态权威或原子报告写入门禁。

结果和 checkpoint 回放必须先与完整 job spec/payload 身份核对，再进行物理重放。改变上述任一有效新字段或场景系数不得重放成另一请求的结果；合法但错绑的结果必须拒绝。新增缺省字段及规范化为空的零误差项不得使既有 canonical 请求身份失配。

## 必需验证边界

接口测试须覆盖旧 canonical/普通 v1 兼容；schema 与正式 deserializer；CLI 新 JSON 请求的真实分析与至少一个真实有限场景 `nominal_preserving` 优化及结果 round-trip；冻结 MCP plan/submit 到真实持久化 job worker 的同一 payload 计算、回放身份匹配/错绑拒绝；初态不可行、严格名义拒绝的 partial/incumbent、预算终止与取消。已存在的身份、去重、释放确认、终态与原子报告测试为既有门禁，新增测试不得替代或弱化它们。

没有通过 MCP 传输层实际执行的验证必须标明范围；单测调用规划/业务函数不能声称已验证 MCP transport。任何 socket/服务端点探测或绕过均不属于本合同。
