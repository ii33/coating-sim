---
name: film-stack
description: 用于创建或从表格导入多层光学膜系工艺；涵盖膜系、厚度表、60层及四分之一波长序列。模型只映射已确认的参数，层序展开由程序完成。
---

讨论模式（内部兼容值chat）仅允许读取、校验、分析和计划；以下写入、窗口操作、绘图生成、脚本、估算登记、计算、取消、导出和工作流推进仅在操作/攻关及现有授权范围内适用。不得以技能指令绕过执行白名单。

# 膜系工艺

用户提到膜系、厚度表、60层或多层薄膜时使用本技能。先区分沉积层表工艺与独立光学膜系：前者读 `read_software_manual(section="film_stack")`，后者读 film_design；优化/算法选择再按需读 algorithm_selection、film_optimization 与 film_optimization_parameters。然后按当前任务一次查询相关工具契约，不反复读取 schema/catalog，不用空参数试错猜工具。

## 新建和导入

- MCP 工具：`build_film_stack(specification, path, overwrite)`；批量显式层、周期（`period_layers`/`repeat_count`/`prefix_layers`/`suffix_layers）、等厚序列（`sequence`/`layer_count`/`total_thickness_nm`）或四分之一波长序列（`sequence`/`layer_count`/`reference_wavelength_nm`），折射率从 `materials[].refractive_index` 读取。程序展开，最多 10000 层。
- `create_design_workflow` 可提供 `film_stack` 代替 `steps`，两者二选一。已有 60 层方案需要局部修改时，优先 `update_design_fields` 的 JSON Pointer patches，不重发整份膜系。
- 支持 CSV/TSV/TXT/XLSX/JSON 层表。XLSX 多工作表须明确 sheet；公式单元格先粘贴为值。TFCalc/Macleod 文件仅在导出为文本后导入，不声称支持未知原生格式。导入前 `preview_film_stack_import` 只看短预览，再 `import_film_stack`；用 `input_schema('film_stack')` 获取字段要求。nm、um、angstrom及输入层序（基片→空气或相反）必须明确。不自动 OCR；多模态附件可辅助整理带不确定标记的层表，需核对后再生成。
- 口述光学指标时先确认波长、各材料折射率 n、层序和控制假设。四分之一波长只按法向、透明材料、实数 n 生成候选厚度，不验证反射率或带宽，不宣称满足光谱指标。

## 查看和修改

- `read_process_layers(path, offset=0, limit=20)` 分页查看；limit 最大 100。
- `update_design_fields(kind, path, patches, output_path?, overwrite?)` 支持最多 64 个、0 基 JSON Pointer 的 add/replace/remove 操作；优先针对旧工艺做最小字段修改。
- `preview_film_stack_import` 的预览短且不替代校验；导入后用返回状态/现有校验工具核对。

层材料及工艺字段与当前 process 契约一致。模型负责将用户意图映射为参数，重复层展开、保存与有限图表由程序完成；不逐层手写超长 JSON。



独立 .cfilm 光学设计总层数上限4096，与此沉积层表10000层上限不同。独立模块已计算光谱，但表格展开本身仍不验证光学目标；厚度优化与结构插层/生长分别使用当前开放入口，不能用厚度MCP请求伪装结构搜索。
