# CPU波长分块补充验收（2026-10-17）

已补齐单候选多线程CPU基线。新比较中，CPU与Intel均通过同一个CoherentBatchEvaluator.evaluate包装、同一FP64/complex128模型与64MiB预算，按CPU→XPU/XPU→CPU交替顺序进行7次稳态计时。前次单候选0.0575秒CPU实际上只有1个worker，不能代表最优多核CPU。

本次仍是60层、10000波长×1角度37°=10000条R目标/候选，非偏振为s/p平均，候选数另外为1或64。种子19、候选矩阵、400–800nm范围与前次相同。OMP/MKL/OpenBLAS为1，不进行候选池×波长池嵌套。

## 最小实现

FrozenCoherentStack.wavelength_slice返回连续波长的只读共享视图，不复制完整冻结堆栈、不重算材料/角度/界面系数。每个波长的层递推、临界层解析处理及偏振平均保持完整，线程结束按输入波长顺序拼接，再由调用方统一算RMS/MSE，不能对线程内RMS求平均。

PerformanceOptions新增cpu_parallel_axis=auto/candidates/wavelengths。默认auto：单候选选波长轴，多候选保留候选轴；默认cpu_workers仍1，不安装本机4/8线程或任何性能阈值。两种轴均允许显式扫描，线程数受可用CPU、独立维度长度、总scratch和保留结果预算约束。视图按worker数缓存在本evaluator中；每次调用只用一层线程池，创建/拼接均计时。kernel版本v4使旧计时profile失效，新profile须匹配请求轴。

## CPU扫描（三次稳态中位，秒）

| 候选数 | 分块轴 | 线程 | 秒 | 平均占用CPU核 |
|---:|---|---:|---:|---:|
| 1 | candidates | 1 | 0.053520 | 1.06 |
| 1 | wavelengths | 2 | 0.033507 | 2.03 |
| 1 | wavelengths | 4 | 0.029299 | 2.46 |
| 1 | wavelengths | 8 | 0.061960 | 2.44 |
| 1 | wavelengths | 12 | 0.084280 | 2.65 |
| 1 | wavelengths | 16 | 0.109126 | 2.84 |
| 1 | wavelengths | 20 | 0.066533 | 1.41 |
| 64 | candidates | 1 | 4.941428 | 0.99 |
| 64 | candidates | 2 | 2.491982 | 1.94 |
| 64 | candidates | 4 | 1.244202 | 3.65 |
| 64 | candidates | 8 | 0.680621 | 5.82 |
| 64 | candidates | 12 | 0.900009 | 5.98 |
| 64 | candidates | 16 | 1.206323 | 5.08 |
| 64 | candidates | 20 | 1.347923 | 5.58 |
| 64 | wavelengths | 2 | 2.704368 | 1.90 |
| 64 | wavelengths | 4 | 1.197716 | 3.61 |
| 64 | wavelengths | 8 | 0.588264 | 5.62 |
| 64 | wavelengths | 12 | 0.856394 | 4.88 |
| 64 | wavelengths | 16 | 1.068735 | 3.97 |
| 64 | wavelengths | 20 | 1.362246 | 3.36 |

单候选波长4线程优于1线程，8/12/16/20线程反而可能因调度、小数组/缓存和共享资源开销更慢；未采集内核级硬件计数，具体原因不作已证实归因。多候选的候选轴8线程仍是合理基线，本轮波长轴8线程略优，所以确认阶段取其最低扫描中位配置；这不构成所有机器/规模都应默认改用波长轴的证据。

## 相同包装、相同稳态口径确认

双方先完成一次共同API调用，再交替执行7次共同API调用；包括验证、线程池创建/合并，或GPU候选上传、计算同步、下载和调用内分配。GPU固定堆栈驻留上传、首次导入/门禁成本在冷启动单列。GPU调用若回退为CPU，基准断言即失败。

保留了首轮和最终源码确认两轮记录，主表采用最后一轮，不挑最低耗时。首轮单候选CPU/XPU为0.028837/0.042000秒，64候选为0.528102/1.203651秒；可见一定轮间波动，两轮结论一致。

| 候选数 | CPU轴/线程 | CPU秒 | Intel秒 | CPU平均占用核 | 对比最大绝对误差 |
|---:|---|---:|---:|---:|---:|
| 1 | wavelengths/4 | 0.034309 | 0.043818 | 2.46 | 1.54e-14 |
| 64 | wavelengths/8 | 0.512157 | 1.183195 | 6.04 | 4.27e-14 |

单候选CPU/Intel时间比为0.783，补齐CPU多线程后前次单候选胜负反转。64候选CPU/Intel时间比为0.433。结果仅适用于本机当前环境和此代表性模型，不能承诺其他Intel/CPU配置同样加速。

确认计时CPU单候选的进程CPU时间接近多个核，已不再是单候选worker=1；64候选也有并行CPU时间证据。当前XPU实现仍为13候选×10000波长一块、64候选五块，FP64/complex128和Intel xpu:0不变。GPU前向段包含eager提交及同步等待，未将其冒称纯硬件计算时间。

## 独立新进程冷启动

每个CPU/XPU×1/64候选组合各在独立原生进程测第一次共同API调用。XPU冷调用包括torch导入、上下文、驻留上传及堆栈数值门禁，CPU包括波长视图/线程池首次建立。保留已有驱动磁盘缓存，不清系统缓存。

| 候选数 | 后端 | 首次API秒 | 导入准备至首结果秒 | 峰值进程MiB |
|---:|---|---:|---:|---:|
| 1 | cpu | 0.031221 | 0.462089 | 140.2 |
| 1 | xpu | 2.146584 | 2.540442 | 722.8 |
| 64 | cpu | 0.428726 | 0.840269 | 157.2 |
| 64 | xpu | 3.067696 | 3.459068 | 764.7 |

## 测试、边界与整合

71 passed in 3.33s。新增覆盖：不可变共享视图、不整除的波长分块顺序、s/p/非偏振、吸收/临界混合波长、单/多候选、全谱归约、总scratch、保留结果预算退回串行、调用前取消与工作线程内部取消。60层/10000目标原生扫描和确认均正常返回，谱逐点rtol/atol2e-12通过。

默认auto不意味着自动寻找最优线程/轴，不在窗口打开时启动基准，不写入校准、不扩展通用调度器。显式线程请求在单候选时开始使用波长线程，可能因线程太多变慢；默认1线程不受影响。角度不拆分，偏振归约顺序不变。

前次完整优化异构快12.2%的数据仅相对旧的候选轴CPU基线；此次没有重跑完整优化/异构，不能把旧比例直接沿用为对新CPU最优轴的优势。此补充严格给出评估器层面的公平CPU/Intel实测。功耗/温度及其他物理硬件仍未验收。

补丁基于上次独立v3交付增量，只涉及film_optics_batch.py、film_performance.py、算法文档、新增定向测试和可复跑基准，共5文件。主仓库当前的亲和性探测等父任务修改不在本补丁中，不整文件替换、不改安装器、不推送。采用git apply --check验证当前整合目标，实际整合由主任务完成。

原生执行环境曾出现一次exec-server transport disconnected，未重复启动计算；恢复只读检查后确认原扫描正常完成，退出码0。此为工具连接瞬断，未被记成硬件失败或基准超时。

## 复跑

在安装原项目和已有可选XPU依赖的原生环境，OMP/MKL/OpenBLAS设1，顺序执行：

```powershell
& $python scripts/benchmark_film_wavelength_workers.py --output-dir evidence/wavelength --phase scan
& $python scripts/benchmark_film_wavelength_workers.py --output-dir evidence/wavelength --phase confirm
# 以下每条用新进程；backend分别cpu/xpu，candidates分别1/64。
& $python scripts/benchmark_film_wavelength_workers.py --output-dir evidence/wavelength --phase cold --backend cpu --candidates 1
```

本机启动/日志脚本为launch_wavelength.py和run-wavelength-confirm.ps1。既有原生隔离环境被只读复用，未改用户环境/驱动/安全设置或用户应用。
