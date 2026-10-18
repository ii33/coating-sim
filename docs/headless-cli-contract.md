# 专用无界面 CLI 合同

更新：2026-10-18。物理计算与队列仍由共享服务负责，CLI 只负责参数转换、状态观察和结果取回。

## 沉积算法 CLI

示例：`python -m coat_sim.algorithm_cli --root /path/to/project --process data/processes/recipe.cproc --time-step-s 0.3 --detach`

- `--root` 明确选择项目及 scheduler 队列根目录；省略时保持安装源码根目录的历史默认。不会根据当前目录或任意工艺路径自动猜测项目。工艺及其依赖仍受服务根目录约束；先冻结并验证，再构造队列客户端。支持已有 v6/v7 工艺。
- 不提供 settings 文件时，网格默认 31，时间步数默认 120。提供 `--settings` 时，其 JSON 对象替代 CLI 网格默认，遗漏字段使用产品 Settings 默认；显式 `--time-steps` 覆盖文件中的同名值。
- `--time-step-s` 与显式 `--time-steps` 或 settings 对象中的 `time_steps` 不可同时出现，冲突在提交前返回错误。
- 步长表示请求的最大平均步长，沿用共享 `process_timing`：N = max(层数, ceil(T / 请求步长))，实际平均步长为 T/N；逐层积分仍使用各层时长/分配点数。例如 T=1 s、请求 0.3 s 时 N=4、实际平均 0.25 s。没有修改时间采样算法。
- 使用 `--time-step-s` 时，成功或 detach JSON 的 `timing` 对象包含 `time_steps`、`time_step_s`（实际平均值）、`total_duration_s`。其他入口输入时该字段为 null。它描述物理工艺全局采样，不是 fast-cycle 独立角分辨率保证。
- 输出目录必须不存在。业务和初始化错误输出一个 stdout JSON 对象（`error`、可用时 `output`），退出 2。参数解析错误保留 argparse 行为。已有目录不覆盖；初始状态成功发布后才尝试写失败状态，失败状态写入不能遮盖原始错误。所有正常/异常退出路径恢复先前 SIGINT 处理器。
- 同步模式观察队列终态、校验并下载产物。Ctrl-C 分离观察，不取消已提交计算；`--detach` 提交即退出。队列作业是状态的唯一所有者。输出/状态发布失败不撤回已经接受的任务。

## 冻结膜系批次 CLI

`python -m coat_sim.film_performance_cli evaluate` 接受 stdin JSON，stdout 恰有一个结果对象：成功 `ok:true`/退出 0，受控输入错误 `ok:false`/退出 2。

- 请求必须为对象，`frozen_snapshot` 必须为对象，再验证快照内容及摘要。null、数组、字符串、数字或缺失快照均返回 JSON 错误。
- 显式 `settings` 优先，否则读取快照设置；归一化的零宽单点 `step_nm=0,samples=1` 可直接回放。活动层和透明外部端口约束与参考服务一致。
- 仅使用冻结材料，不访问实时材料库。输入上限 16 MiB、内联原始结果上限 8 MiB。GPU 探测需 capability 的明确参数；这些回归只用小型 CPU 数据，不宣称实时 GPU 验收。

合同测试：`tests/test_audit_compute_cli_contracts.py`、`tests/test_offline_budget_entrypoints.py`。
