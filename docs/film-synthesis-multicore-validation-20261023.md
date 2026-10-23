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

另一个6-worker调度用例原来仅31个波长点，单次真实评价0.85–2.73ms；49项任务全部完成/消费、unknown为0，但部分PID诚实记录CPU增量0。独立进程CPU API诊断观察到15.625ms的计时量化，并非PID、单位或累加错误。此补丁仅把该用例的物理网格提高到4097点，保留每PID正CPU、实际进程数、同冻结批次串并一致及清理的全部断言。不改生产计时器、不改成纳秒接口、不降200000生产阈值、不伪造非零；也不改变默认1 nm采样或用户目标。受控Windows主机已在17e0c4d上复验通过：6个真实PID的CPU增量0.09375–0.21875秒，48任务完成，原11项断言、串并一致和OS进程退出均通过；这些是实机结果，不以云端结果替代。

## 同阶段集成回归修正

原完整CI的quick组发现两项兼容/统计问题，未撤销失败记录或放宽验收：旧示例目标不应被静默增加显式false的严格合成字段，现仅将false规范化为缺省，true仍保存、非布尔仍拒绝，原完整配置相等及实际优化断言保留；基准runner正式拒绝的响应只保留诊断，不再从其自报终态/分数/清理得出成功统计，新增身份、畸形响应及套件计数负例。后者的完整模块33项通过，原正式物理和完整网格门禁不变。完整发布SHA的CI须另读真实终态，不因本地定向通过而称所有tiers已绿。

## 冠军清理的真实核心修复

完整CI另外发现默认hybrid在原12评价预算下返回两个同源半QW层，旧GUI的单层与保存/漂移断言失败。两层的物理分数和名义通过本来都是真实的；问题是有限生长刚归档冠军即遇阶段预算，探索终态仍旧，压缩只处理探索端，尚未生成、评分可合法合并的冠军。未将测试改legacy、未增加旧预算、未删除原断言。

当前改为先检查真实冠军，终审前再对后续冠军作有界检查；完整原谱重新计算及谱等价核查后才按原排名归档，新层表仍须自己的独审。原12评价请求现在仍11次总评价、一次真实合并重算，返回111.78861788617887nm单层并独审通过；真实探索终态不被冠军重置。新增10项直接正式JobSpec回归通过，覆盖制造厚限、不同optical_set、独审留槽、资源护栏、取消、墙钟截止及观察者异常。清理完成、实际被选为冠军、独审三者分别报告，详见[冠军清理合同](film-synthesis-champion-cleanup-contract.md)。

有权限运行现有本地scheduler端点的环境可复验原GUI节点：

```text
python -m pytest -q tests/test_film_synthesis_terminal_cleanup.py
python -m pytest -q tests/test_film_synthesis_gui_review.py::test_qml_n0_grows_real_single_layer_and_freezes_current_targets_and_materials tests/test_film_synthesis_gui_review.py::test_project_qml_synthesis_new_material_closure_survives_save_or_drift_undo
```

前一命令不创建scheduler服务，不能代替后一命令的GUI/保存/漂移验收；受限云环境没有重试被拒的端点。准确发布SHA的所有CI tiers仍须分别读取实际终态。

受控Windows主机已在c880a569上完成上述冠军修复验收：10项新边界和3项真实GUI/IPC共13项通过；三个原队列案例均返回单层、11次评价，原12次预算及GUI断言未修改。此为实机正式路径结果，不以直接core测试替代；完整CI仍须按准确SHA分别核对终态。
