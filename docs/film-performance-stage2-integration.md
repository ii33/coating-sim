# 固定拓扑 worker 前向缓存接入清单（2026-10-12）

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

后续阶段：2026-10-12 当前主机已增加显式 deferred_batch 正式作业通路，见 [HPC 契约](film-optimization-hpc-contract.md)。下述接入前说明及性能数字保留为历史记录，不代表该后续实现已通过同等测量。旧 serial 求解轨迹不变。

状态：历史最小接入清单。本阶段新增adapter和测试，既有数值模块保持阶段一指纹冻结。

2026-10-12 后续观察：共享作者已加入 `enable_forward_cache` / `COAT_SIM_FILM_FORWARD_CACHE=1` 默认关闭的显式接入，其进度/sidecar 细节与下述历史建议补丁已有差异，源码接口为真值。下述隔离候选数字不能追认新接入；真实入口复验由唯一作者负责。[有限 worker 性能剖析](film-performance-profile.md) 保留测量版本、启动与稳态范围、瓶颈和后续目标。

## 单作者需要修改的边界

1. `coat_sim/core/film_optimization_job.py::execute_film_optimization_job`：增加可选 `performance_callback=None` 关键字，结构请求分支完全保留；只在固定拓扑分支 `_service_with_frozen_materials` 之后调用新 `create_worker_forward_service(frozen_service, frozen_snapshot, performance_callback=performance_callback)`。仍调用原 `optimize_film_design`，不换求解器、不重启搜索、不加第二份预算。结果仍交原 `serialize_result`，schema v1不增加字段。
2. `coat_sim/scheduler/worker.py::_film_optimization`：本作业内收集 `performance` dict，传 `performance_callback` 给上述函数；`publish_progress` 用同一个现有 `report` 添加六个性能标量。性能字段不加入 replay-verified result/checkpoint。完整终态诊断用原原子写工具保存独立 sidecar 并记录 worker 日志；不用新队列、UI窗口或独立worker。
3. GUI/MCP/CLI 都继续原共享提交服务→`film_optimization`队列→上述worker，默认固定拓扑任务会因此缓存前向。没有新增GUI控件和请求格式字段。TRF/L-BFGS-B 的解析J仍是原CPU受限域；multistart起点仍串行。旧DE仍 immediate；新deferred本阶段不替换它，不借旧方法ID接入。

新module API（高性能作者拥有）：

具体最小差异见 film-performance-stage2-owner.patch（历史引用目标，当前公开源码不包含此文件），本任务没有直接应用共享文件。该补丁已归一化为标准 unified diff，不再需要 `--recount`。

补充边界：`coat_sim/core/compute_tasks.py::_compact_progress` 会删除未知性能字段，接口应只放行六个标量：文本 `performance_adapter/backend/dtype` 与数字 `performance_forward_calls/cache_hits/fallback_calls`。沿用现有长度与 finite 检查，不放行数组、候选设计或嵌套字典。

终态详情用原 `write_json` 原子写到 checkpoint 同目录的 `film-performance.json` 并记 worker 日志；写诊断失败只警告。严格 v1 result/checkpoint 不添字段，旧 best 不被诊断覆盖。成功、失败、取消均保存当前快照；结构分支不创建 adapter，也不写此 sidecar。进度沿用原 250 ms 节流，终态详情以 sidecar/日志为准。

独立测试脚本 `scripts/film_performance_stage2_probe.py` 在内存中加载该补丁的三个文件，原文件只读；`scripts/benchmark_film_worker_cache.py` 通过原始/候选真实 worker、原优化器和重放验证比较同 seed/预算结果。该方式是接入前候选验证，不表示生产队列已经接入。共享作者应用后仍需通过原入口复验。

```python
from .film_optimization_forward_cache import create_worker_forward_service
frozen_service = create_worker_forward_service(
    frozen_service, frozen_snapshot, performance_callback=performance_callback)
```

`options=None`的生产默认是CPU单线程；容量2 prepared LRU、既有波长/角度/偏振与FP64不变。非相干/资源域外/拓扑或光学设置变化/自定义service在纯计算前回原冻结CPU服务，并记录原因。失败仍按原优化器候选边界计账、取消照原baseline/validation行为。不能在cache失败后重跑整个优化；不得访问live材料。

## 验证和发布边界

接入前候选功能已验证：原 17 个适配器回归和追加单波长三域共 20 个用例取得通过；独立 实现者 只读复核为 CPU 范围 READY。真实 worker 小/中/大组、双面非相干、取消和追加实际 DE 搜索均已完成，报告见 第二阶段证据（历史验证记录，未随公开源码发布）。预热比值分别为 1.077x、1.011x、1.043x，追加搜索为 1.128x；没有达到稳定 20% 端到端耗时降低的证据，不建议据此宣称默认队列明显加速。

冻结 adapter SHA256：`8c318ce5f1b33f035884819cb893f355311628576b5cafa2b033413b5e6223d5`。标准补丁对当前文件的 `git apply --check` 已通过。测量后其他作者更新了 `compute_tasks.py` 的其他业务逻辑，进度函数核对保持原实现；原测量 source SHA 与并发漂移均记录在证据中，不能以新全文件 hash 追认受测。唯一作者可审阅接入，应用后须用真实提交入口复验。

接入后必须通过真实 `execute_film_optimization_job` 与 `scheduler.worker._film_optimization`：八法、同seed/预算/key/schema，固定小中大组、取消、CPU fallback、材料目录改变后仍只用冻结材料、checkpoint重放。数字基准拆冷加载与预热，不能以独立API结果冒称原worker收益。

只新增adapter与其测试的阶段包不能声明“现有窗口已加速”。需队列单作者确认修改落盘后再做原调用点回归。与针插窗口的共享函数不得并写；高性能作者给具体最小补丁，队列作者应用并回执。
