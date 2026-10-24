# 波段平均值 / 绝对值（逐点）契约

更新：2026-10-24。数学、单位、非均匀梯形 q、解析梯度和图解见 [算法 14at](algorithms.html#film-band-criteria-20261024)。本页为当前接口边界，不提供旧格式兼容。

每个 `FilmOptimizationObjective` 只有一个波段身份，共享 wavelength_min_nm / wavelength_max_nm / wavelength_samples / angles_deg / polarization / metric / weight / normalization_scale。`metric` 只支持 R/T。`mean` 与 `pointwise` 都为 `FilmObjectiveConstraint(enabled, mode, target_value, lower_bound, upper_bound, strict_lower, strict_upper)`。默认逐点开、均值关；两组都关拒绝。value 必须清除 range 两界；range 必须清除 target。各组阈值独立，禁用组不进 loss/J/nominal/候选拒收。GUI 的“绝对值”意为逐点约束，与敏感性 absolute 尺度无关。

## 输入、状态与错误

优化 controller 是唯一编辑草稿所有者；任务冻结后 job request 是计算输入所有者。每组保留自己的启用状态、上下限和未完成输入。禁用组的 `bounds_drafts` 只在编辑配置保存，不进入物理请求。Tab / Shift+Tab、切换选中波段和保存重开不得清空另一界或重建整个编辑 Repeater。启用组未完成、非有限、两组已知矛盾、正宽单点或缺端点都在提交前报错；不会在材料未知源域补值。

结果按 mean/pointwise 分列启用、名义通过、solver acceptable、最差角。mean 条件 wavelength_nm=null，保留 band endpoints；局部谱线仍从完整 raw_coverage 展示，不伪造中心波长。mean-only 局部违规仅诊断，不暗加违规比例或逐点门槛。两角分别判定，不能跨角平均。

## 数值与审计

每个 band/angle 汇齐完整 raw spectrum/J 后统一 reducer，跨 chunk 仅拼接完整数据。每 band 质量 W，角 A，启用 K，每组 M=W/(A*K)；pointwise 按非均匀梯形 q 积分。RMS 是 sqrt(sum(loss)/sum(W))。即使仅逐点，端点和 RMS 权重也采用新定义，搜索轨迹可改变，原目标阈值不变。最大误差、nominal 和 guard 不乘质量。

严格名义通过与求解容差分开。独审均值只用预定完整积分 grid；offset/extrema 额外点不混入 mean。完整 raw coverage 与 active condition count 独立校验；历史结果独立重放完整光谱，不只验证一个均值标量。有限网格和极值搜索不构成连续证书。

## 冻结、接口与取消

当前 synthesis request/result/job/checkpoint 协议为 3（冻结的 `enable_thickness_reduction` 布尔选项）；ordinary / structure / classic request/result/job 协议为 2；ideal sensitivity request/result 为 3，error table 为 2；独立 sensitivity job/checkpoint 外壳仍为 1，内层身份严格绑定新版本；优化编辑配置为 2。旧 flat mode/target/bounds 与请求级 strict_*_objective_indices 均拒绝，无迁移或 fallback。CLI / MCP / GUI / scheduler 使用同一嵌套输入与接收证据，不修改旧科学处方或冻结研究文件。

取消、时间和物理评估预算仍覆盖原计算阶段。未独审的取消最佳解只能显示已完成 training 事实，不能宣称独审通过。开关实时预览不新增物理 forward/J，关闭组不恢复已禁用约束。既有可选模板随机化与 seed 冻结保持原义。

理想敏感性始终保留原逐 λ/角固定 baseline guard。freeze 将所有 enabled 名义 bound sides 的并集固定成 guard_directions（lower/upper/both）；结果/重放使用冻结值，不从后来 UI 重算。F 与尺度不变。独立误差参数表仍计算逐点误差；关闭逐点 target 时原始点的 nominal pass 为 null，归约名义条件另列。参数表不能应用为理想候选。


## 公开载荷的 JSON 原生数组（2026-10-24）

ordinary / structure 的 request、candidate、checkpoint、result 在公开序列化边界直接返回 JSON 原生数组。`angles_deg` 使用 `objective_to_payload()`；`raw_coverage` 与 `validation_raw_coverage` 的 `wavelength_nm`、`actual_values` 使用 `coverage_to_payload()` 转为独立 list。因此本地返回值与 JSON 编码、HTTP 传输、持久化和重启读取后的值保持完全相等，不依赖整份载荷的二次 JSON 编解码。

计算归约器、内部档案和 observer 继续共享不可变的 tuple coverage；只在公开边界建立列表，不增加 TMM / Jacobian 求值。数值、严格边界标志、原始采样覆盖、冻结材料及请求身份保持原意；接收端 `coverage_from_payload()` 恢复不可变证据，原有形状校验与物理回放门禁保持有效。
