# 离线膜系基准诊断契约

`tools/film_benchmark_diagnostics.py` 仅读取已有报告，不调用膜系搜索、评分、仿真或训练；不联网。它输出**观察事实**而不是失败机理结论，不把预算停止解释为物理不可行、局部最优或初始化差。

```sh
python tools/film_benchmark_diagnostics.py \
  --cases /path/to/report/case_results.csv \
  --summary /path/to/report/summary.json \
  --output-dir /path/to/private-output
# 可选：仅按需读取具体单次结果（可重复 --result）
# --result candidate=/path/to/case/result.json
```

输入为 UTF-8（接受 BOM）的配对 CSV：必需 `sample_id,split,category,baseline_status,candidate_status`，其余字段可缺。其余字段采用报告的 `baseline_` / `candidate_` 前缀，如 `stop_reason`, `stop_signal_observed`, `elapsed_seconds`, `evaluations`, `audit_complete`, `layers`, `total_thickness_nm`, `minimum_thickness_nm`, `numerical_boundaries_and_errors`, `blocked_reasons`。后两者为 JSON 数组（或 null）；非空无效 JSON、其他类型、空样本标识或 split、重复样本标识/split 均明确报错，不静默忽略。可选 summary 中 `input_provenance.{version}_{split}` 和 `source_links.{version}` 原样记录数据及代码版本；缺失为 null。单 case JSON 通过 `sample_id`、`split` 和显式版本精确匹配，读取 `research_budget.seed` 与 `best_design.front_layers/back_layers`；不递归扫描大型结果集。它不暗示有多 seed 实验。

输出 `case_details.json`、`summary.json`。前者每 case/版本一条，保留原始审核状态、停止信号、评估数、耗时、结果层数、总/最小厚度、显式阻断、采样目标及完整目标细节。无字段用 null；`objectives` 为 null 表示目标边界未提供，空数组表示边界已提供但无识别的 pointwise/mean 项。最佳逐层结构仅在可选结果 JSON 中存在时输出。seed 和多 seed 证据缺失为 null。

## 观察标签

标签允许重叠，未出现某标签不意味着反证；未知证据不得当作通过。规则均为本项目描述约定，未经外部验证：

* `budget_stop`：停止原因或信号明确是 `wall_budget`/`evaluation_budget`。既可以与违规也可以与名义达标共存。
* `runtime_error`：状态为 `error`、`failed`、`exception`、`runtime_error`、`harness_error`、`watchdog_timeout`，或明确 `watchdog_timeout=true` 或有明确 error 字段；普通 `cancelled` 不自动认定为异常。
* `constraint_violation`：报告至少一项 `targets_met=false`；未返回目标或未完成审核不自动算违规。
* `missing_material`：显式阻断种类以 `missing_source_k`、`missing_source_n`、`missing_material` 开头或为 `no_single_group_n_coverage`；`unsupported_metric`：显式阻断种类包含 `unsupported_metric`。其他阻断原样保留，不强行归类。
* `audit_incomplete`：内部 `audit_complete=false`。独立审核状态单独保留；空字段为未知，不等同未完成。
* `gradient_stagnation` 与 `structure_stagnation` 固定为 `unknown`。单次 progress 快照不构成轨迹；现版本未建立可靠轨迹判定，因此即使输入含 trace 也不判定。不得推断局部最优。

`objectives` 记录采样最坏实际值、上下限、目标、角度和波长、来源 `max_normalized_error` 与源细节。`raw_violation_at_reported_worst` 是在来源所报最坏点的原指标量纲偏离：范围为 `max(0, lower-actual, actual-upper)`（缺一侧时只用另一侧）；`value` 等值为 `abs(actual-target)`，缺所需值或未知模式则 null。严格边界上零数值偏差仍可能违反严格不等式；以来源 `targets_met` 为准。最大值、点位、归一化语义沿用输入，不声称从该点重算了全谱全角最大值；不同目标和单位不可直接取最大合并。源字段 `validation_max_error` 原样另列。

## 描述统计与限制

`statistics` 按版本 × split × category 分组，包含 `case_count`、`cases_with_objectives` 和每个标签的 `label_counts`。标签数分母是该组全部 case，缺证据没有被归入其他标签；数值可见数单列，分母不可混淆。`objective_statistics` 按版本 × split × metric × reduction 分组，同一 case 即使有多个相同类型目标仍只计一次；其分母仅为存在该类型目标的 case，因此不含无目标记录的阻断案例；标签仍是 case 级，不能据此将标签归因于该单个 metric。计数可重叠，不是成功率或概率。`proposed_experiments` 与事实分离，当前为空；本工具不训练、不调参、不利用测试集选择模型。单轮有限采样和本机耗时均非连续光谱证明、制造资格或显著性结论。
