# 桌面 Intel XPU 延迟批量优化 v1

CPU 与旧串行执行保持默认。用户显式选择固定拓扑差分进化，勾选“延迟批量评估”，再按需检测当前解释器中的 Intel XPU 并选择设备。不会因选择 GPU 偷偷切换算法、安装依赖或启用异构。

首个产品范围：本机、单目标条件、一个固定角度、前表面、单面半无限基片、固定拓扑厚度优化；R/T 值或区间目标、s/p/非偏振沿用现有评分。结构搜索、多角度、多目标、背面/有限厚基片不提供 XPU。精确/近临界或 GPU 能力、数值门禁、设备身份失败直接终止，不回落 CPU 冒充 GPU。CPU 评分、首次堆栈数值门禁中的 CPU 对照、候选重放和独立稠密验证仍保留；这些显式验证不是失败回退。

`FilmOptimizationRequest.execution_device` 是可选、冻结的执行声明。缺省字段保持旧请求字节与行为；显式 CPU 批量声明使用 `film_cpu_v1`，覆盖继承环境变量。XPU 声明包含种类、运行时索引、设备 UUID、设备名与 torch 版本。材料、设计、目标、种子和执行声明一同进入原作业指纹及结果 request；不从实时目录重新读取材料。旧后台不声明 `film_xpu_execution=1` 时拒绝这种请求，不能静默投给旧 worker。

设备列表由 ComputeTarget 拥有，显式探测在有期限的自有子进程进行。旧代次结果丢弃，不自动改变已选设备。worker 在其自身进程核对 UUID/运行时版本并选择精确索引，严格包装已有 FrozenFilmEvaluator/TorchCoherentBatch；前向包装本身不修改 film_performance.py 的算法；同次整合独立交付的 CPU 波长并行补丁。批量前向每次必须实际返回 XPU FP64；错误不执行 CPU 替代前向。

取消沿用队列取消事件与 GPU 分块检查；只发布完整候选。进度及有界 sidecar 记录实际后端、UUID、dtype和批量数。错误保留已完成候选；完成、取消、源设计变化、窗口关闭及结果应用继续受原 operation/project generation 和 CPU 重放门禁控制，不自动覆盖当前编辑。每次新窗口的批量选择默认关闭，保存的设备偏好仍须重新检测和显式启用模式。

延迟 DE 按代更新，与旧 immediate 模式轨迹不同。新增 UI 不改变波长/角度采样、误差容限、预算、物理单位或材料外推策略。实现约定及限制没有外部通用性能保证；本机硬件测量见 intel-film-backend-benchmark-20261017.md。

显式 CPU 批量声明还冻结 cpu_workers（默认1）与 cpu_parallel_axis（auto/candidates/wavelengths）。线程不超过当前进程可用 CPU；auto 只为单候选选波长轴、多候选选候选轴，不扫描最佳线程数。线程池不嵌套、全谱拼接后统一评分，保留 scratch/结果预算与取消。界面设置仅对 CPU 批量前向有效，不改变评分/独立验证线程策略，选择 XPU 时禁用。新窗口的执行选项不从业务配置自动恢复。

日志统一为项目目录 artifacts/logs/，每通道2MiB、两份轮转备份；新探测子进程只保留协议 stdout，stderr 使用 compute-worker 有界日志。film-performance.json 是单份覆盖的作业结果元数据，不是追加日志。

端到端入口：tests/test_film_xpu_desktop.py（--run-gpu，COAT_SIM_TEST_XPU=1，已有原生 XPU 解释器）。覆盖真实窗口控制器→有期限探测→私有真实队列→worker→实际 XPU 元数据→CPU 重放/验证→应用，以及取消、编辑后拒绝覆盖。无GPU默认回归见 test_film_xpu_execution.py。

新公平基线中，同一包装、7次交替稳态、60层/10000波长×1角度：单候选 CPU 波长4线程0.03431秒、Intel0.04382秒；64候选 CPU 波长8线程0.51216秒、Intel1.18319秒。本机 CPU 更快。旧异构快12.2%仅相对旧候选轴基线，不能用于新 CPU 波长并行；异构仍只有实验基准。详见 cpu-wavelength-workers-20261017.md。

新异构闭环已完成：60层/10000目标/256评估，独占进程五轮热态中位 CPU2.385s、Intel4.835s、异构2.269s（约4.8%）；冷启动2.291/7.689/4.875s。捕获未知 Python 约1核瞬时活动，4.8%不构成稳定收益保证；旧12.2%作废。异构不自动启用，不再追加调参，见[闭环报告](hybrid-closure-20261017.md)。
