# 快速资源预测契约（受限域 v1）

日期：2026-10-31。理论说明见[研究库](https://github.com/ii33/coating-research-library/blob/main/08_stack_optimization_algorithms/predictive_design_theory_20261031/README.md)。各适用域及独立核验结果见算法文档；不表示通用膜系预测已经完成。

## 边界与所有者

`film_resource_prediction.predict_film_resources(service, request, cancel_event=None, progress_callback=None)` 只允许冻源、输入与适用域验证、固定特征计算、加载已验证离线参数和轻量推断。不调用自动合成、优化器、针插、删层、候选独立审计或必要界求解。没有模型、超出域或失败时禁止自动回退上述功能。

复用 `FilmResourceEstimateRequest` 与冻源身份契约；忽略并显式关闭请求的审计/探测开关。当前候选与seed堆栈不是从空白结构预测的输入，不纳入快预测冻源；目标、候选材料、基片和工艺保留。以固定9点源检查冻结记录，再恢复完整原始目标和来源身份，不按优化采样数执行密扫描。`source_digest` 与资源控制器的当前源复核一致。共享输入类型不授权跨越在线求解边界。GUI 工作线程持有运行与取消；当前材料或目标变化时旧结果仍标过期。

## 输出

复用 `FilmResourceEstimateResult`，追加可空 `prediction`。`estimate_state` 区分 estimated、out_of_domain、not_calibrated。仅 estimated 带数值，其他状态不伪造预算；输入错误为 failed/invalid，模型配置错误不指责有效用户输入。未校准不算已完成预测。

`prediction` 同时携带 model_id、reference_profile、layer_count、total_thickness_nm、joint_interval、coverage_note、applicability_note、claim_level。N/D 来自同一联合模型；误差区域外接区间的每个角点不一定有可行膜系。`feasibility_state` 仍 unknown，预测数值不是审计见证，不写回处方或变更约束。

工作量中的物理光谱、合成与候选审计次数为0；源冻结次数及端到端时间据实记录。独立展示的推断时间不能替代完整在线路径耗时。取消不返回可应用设计，快速预测本来也不是处方。

## 首批研究域与模型

常数无损域覆盖AR/HR；真实完整n/k数值源指纹注册域目前仅覆盖400–700nm指定SiO2/MgF2薄膜、TiO2 amorphous/Franta 与N-BK7/Malitson基片的AR。未知k仅用户明确允许时按0，已知k不改写。两域均限n0=1、两个候选材料、单前面半无限基片、正入射、单窗口非严格逐点R、无额外层厚制造限制。平均值、双面、多窗口、角度与工艺限制不静默近似。块材熔石英作为膜料未过区间宽度门，不在支持域。

模型参数由离线参考族独立审计数据决定，当前仅接受固定21特征二阶岭回归；在线不拟合、不以近邻查找代替中心公式。相近残差仅用于误差尺度查表。误差尺度可由训练组外残差的局部统计确定，分位数由独立材料校准组计算；盲测组不得调参。未达到准确性、区间宽度、覆盖和延迟要求的参数文件不进入生产数据目录。

测试中的小型假参数只验证调度和语义，不是预测准确率证据。生产参数另有分组盲测和真实n/k外部核验；实际GUI与源变更/取消另测。部署数据位于core/resources，随标准资源映射分发；缺失或未通过验证的参数明确拒绝，不回退合成。
