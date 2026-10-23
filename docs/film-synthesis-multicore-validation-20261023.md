# 混合自动合成多核交付验证

日期：2026-10-23。此阶段基于独立的原门限验收修复；未复制旧分支文件，重叠文件先核基线字节相同，再仅整合本任务新增差异。用户原材料、目标、1 nm默认网格及名义门限不变，没有硬编码研究处方。

## 实际能力及边界

- 正式GUI、CLI、job/checkpoint与MCP连接同一冻结 `parallel_batch_size`，AUTO按请求/实际CPU解析一次，没有人为4进程上限。串并比较固定同一逻辑批次，仅改变执行宽度。
- 工程先验/全局独立候选和同轮解析零厚针探针使用真实持久工作进程。材料上下文每进程只准备一次，非共享内存。源序采纳、全部厚度联合精修、后续轮次依赖及独立验收仍串行；独立敏感性本阶段没有新增并行声明。
- 实际并发受到可用CPU、候选数、阶段/总预算、冻结内存及通信估计共同限制。结果区分别显示请求、真正派发过工作的进程和拥有的PID记录；9个ready、实际派发2或0不会显示为9个工作进程。
- 内存估计包括父数值工作集、紧凑档案、保留响应/Jacobian、空闲工作进程基线与活动工作集。只是准入代理，不是RSS或OS配额；局部时限不是收敛。启动、取消、回退、未收到的未知工作及清理分别记录。
- 旧持久JobSpec缺新执行字段时，原冻结身份保留，解析Q/内存记录单列；材料、目标、原网格、种子层ID及源规范的精确门禁不放宽。GUI或CLI覆盖新调度配置不回写输入文件。

## 已完成的最终验证

| 验证 | 真实终态 |
|---|---|
| 明确排除本机服务端点的完整安全光学算法组 | 1203通过，4跳过，18 subtests通过；退出0 |
| 正式入口、GUI、敏感性全7模块、重放、资源与字体/生命周期组合 | 322通过；退出0；与算法组有重叠，不合计 |
| 新混合多核专用模块 | 30通过，另独审内存/诊断回归通过；已纳入上面的安全算法组 |
| 官方全源静态 | 1076文件；新增0、消除基线债0、语法fatal0；未修改基线 |
| 完整测试收集审计 | 7123项收集成功；退出0，不冒充执行结果 |

请求19的生产阈值测试，在本云真实可用CPU为9时得到9个真实工作PID，逐PID收到正CPU时间及完成任务，串并物理语义完全相同，结束后无活动worker且清理完成。其原始4097点、48层种子/单层族混合批次达到生产冷启动阈值200000，没有降低阈值；显式3 GiB护栏。该有界单窗口并行验证不是四窗从零合成达标，也不是19倍速度证明。测试按实际平台cap提供足额逻辑预算，20核主机请求19的验证须在该主机实际执行后另行报告，不以本云9核结果代替。

GUI默认hybrid经真实控制器→冻结→typed JobSpec→直接正式worker执行，原4001点×3角度首批达到生产阈值，使用2真实PID，随后协作取消；已完成4项均据实消费，未达标/取消状态与清理保持正确。旧legacy专属阶段回归显式选择legacy，原全部断言保留。

## 可复现命令

在项目现有已安装依赖的Python环境中执行，数值库导入前将 `OPENBLAS_NUM_THREADS`、`OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`NUMEXPR_NUM_THREADS` 设为1，避免进程数乘BLAS线程数超订阅。

```text
python -m pytest -q -s tests/test_film_hybrid_parallel.py::test_real_request_19_auto_uses_actual_platform_cap_without_lowering_work_threshold
python -m pytest -q tests/test_film_hybrid_parallel.py tests/test_film_hybrid_parallel_review_regressions.py tests/test_film_hybrid_job_identity.py
python -m pytest -q tests/test_film_hybrid_parallel_surfaces.py tests/test_film_hybrid_parallel_gui_flow.py
python -m pytest -q tests/test_film_sensitivity.py tests/test_film_sensitivity_cli.py tests/test_film_sensitivity_jobs.py tests/test_film_sensitivity_quick_controller.py tests/test_film_sensitivity_nominal_acceptance.py tests/test_film_sensitivity_review.py tests/test_film_sensitivity_integration_review.py
python tools/run_static_analysis.py check
```

首条命令打印请求/真实cap、PID、逐PID CPU、任务计数、内存估计和清理；测试本身同时校验固定冻结逻辑批次的串并一致。取消、有限针位置/Jac列一致、失败回退及未知工作回归在专用模块中，不用修改生产阈值来宣称性能。

## 未通过和未覆盖的事项

- 该云完整scheduler/GUI服务端点IPC不在有效本地通过证据内。全算法tier曾在无关Cub MCP入口写只读应用状态目录处失败；没有改权限或绕过已拒的socket限制，改用上述明确安全组。正式完整tiers交由准确发布SHA的CI及受控主机验证，尚未完成不能写全绿。
- 原四窗1200秒正式冷启动仍未达标，研究89层已知处方的名义通过不是软件冷启动证明；本次没有重跑该长测或将其改标成功。
- 1.63×仅为12个独立48层1001点Jac任务的热评价池计时，含启动与完整流程不同，也没有线程池A/B证据。没有承诺整个混合流程线性加速。
- 独立敏感性提供真实线性分析与有限误差目标优化，不是蒙特卡洛良率、完整联合相关误差集合或制造通过。普通名义验收不自动加余量，最终应用仍须确认并检查来源未改变。


## Windows微任务CPU证据修正

受控Windows验收已完成原生19工作进程专项：19个真实PID均有正CPU增量，31项任务全完成，固定冻结批次串并一致并退出清理；3 GiB护栏的最高估计约2.64 GiB。17.90秒是该有界任务耗时，不是加速比。

另一个6-worker调度用例原来仅31个波长点，单次真实评价0.85–2.73ms；49项任务全部完成/消费、unknown为0，但部分PID诚实记录CPU增量0。独立进程CPU API诊断观察到15.625ms的计时量化，并非PID、单位或累加错误。此补丁仅把该用例的物理网格提高到4097点，保留每PID正CPU、实际进程数、同冻结批次串并一致及清理的全部断言。不改生产计时器、不改成纳秒接口、不降200000生产阈值、不伪造非零；也不改变默认1 nm采样或用户目标。新fixture仍须在该Windows主机复验，不能用云端结果替代。
