---
name: result-visualization
description: 对已有沉积计算结果创建程序生成的离线图表，或复用已有图表文件；覆盖可视化、膜厚图、热图、等高线和径向分布请求。
---

讨论模式（内部兼容值chat）仅允许读取、校验、分析和计划；以下写入、窗口操作、绘图生成、脚本、估算登记、计算、取消、导出和工作流推进仅在操作/攻关及现有授权范围内适用。不得以技能指令绕过执行白名单。

# 结果可视化

用户提到“可视化”、膜厚图、热图、等高线、径向分布或层时长图时使用本技能。先按需查看 `results` 说明与 `visualize_calculation_result` 工具契约，不能把请求改成自由编写绘图脚本。

仿真成功会自动保存标准热图并将引用放入结果 `files[].heatmap`；优先使用该文件或已有结果 `files`，避免重复生成。仅当用户要求等高线、径向图或其他额外视图时调用 `visualize_calculation_result(run_id, charts=[...])`，固定程序生成 PNG/CSV。不要用 LLM 脚本，也不要把完整大网格读入上下文。默认 summary 只含标量摘要；按明确需要用 `read_calculation_result(detail='summary'|'configuration'|'layers'|'full', offset, limit)` 读取，优先分页、避免 full。

用户已有自己拥有的结果图文件时可直接复用。说明图的 run_id、类型和实际文件引用；不从颜色反推精确数值，也不把可视化当作指标验证。

