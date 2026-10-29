# 膜系设计服务契约与验证范围

本文仅保留历史开发阶段的技术范围与验证边界，不提供私人执行记录。历史测试数量、性能数字与环境不代表当前公开提交的 CI 结果；当前验证应以公开仓库对应提交的实际运行结果为准。

## 实际服务入口

`coat_sim.core.film_design_service.FilmDesignService(root, restrict_to_root=True)` 不依赖 Qt。自动化保留项目根目录路径边界；桌面交互可使用 `restrict_to_root=False`。

| 入口 | 行为 |
| --- | --- |
| `new_design()` | 返回 schema v1 的可保存空草稿，材料引用尚未绑定 |
| `validate(design)` | 校验结构并返回规范化副本；不把草稿当作可计算设计 |
| `load(path)` / `save(path, design, overwrite=False)` | UTF-8 JSON 往返；保存默认不覆盖；覆盖参数必须为 bool |
| `material_options(purpose='film')` | 按 film/substrate 用途读取共享材料资料；用途选择独立 |
| `expand(expression, symbols)` | 安全解析 TFCalc 风格子集并返回基片向外的层数组 |
| `simulate(design, settings=None)` | 直接计算并返回完整光谱对象；无队列提交 |
| `export_csv(path, result, overwrite=False)` | 导出波长、本征 R/T/A 和四个相对光谱通道；默认原子不覆盖 |
| `freeze_design(design, settings=None)` | 捕获材料组全文、设计与设置，返回带 SHA-256 的摘要校验输入快照；不创建作业 |
| `simulate_frozen(root, payload)`（模块函数） | 验证快照类型、版本和摘要后计算；材料库后续变化不会改变快照结果 |

`FilmMaterialLibrary(root).set_usage(material_id, purpose, enabled)` 将用途覆盖保存至 `data/film_design/material_usage.json`。光学数据仍使用既有材料基础设施，不改沉积源绑定规则。

## CoatService 与 MCP/AI 契约

`CoatService(root=...).film_design_service` 延迟构造纯服务并保留原有路径权限。实际包装入口为 `new_film_design()`、`film_material_options(purpose='film')`、`validate_film_design(data)`、`load_film_design(path)`、`save_film_design(path,data,overwrite=False)`、`expand_film_expression(expression,symbols)`、`simulate_film_design(data,settings=None)` 与 `export_film_csv(path,result,overwrite=False)`。保存包装返回 `{path,id,name}`；底层 `FilmDesignService.save` 返回规范化设计副本。

MCP 的实际七个工具及返回契约：

| 工具 | 返回 |
| --- | --- |
| `film_design_schema()` | `schema`、可验证 `example`、额外语义 `constraints`；`validate_film_design` 是最终校验依据 |
| `film_materials(purpose='film')` | `materials` 元数据含数据组来源、波段和单位；不返回 n/k 数组 |
| `validate_film_design(data)` | `{ok,design}` |
| `load_film_design(path)` | `{path,design}` |
| `save_film_design(path,data,overwrite=False)` | `{ok,path,id,name}` |
| `expand_film_expression(expression,symbols)` | `{layers}` |
| `simulate_film_design(data,settings=None,output_path=None,overwrite=False)` | `{ok,summary,artifact_paths,warnings,input_sha256,artifact_payload_sha256}` |

仿真直接执行，默认发布 `data/results/film_<uuid>.json` 和同名 CSV；指定 output_path 必须是该目录直属 `.json` 文件。summary 含样本数、波段，以及本征 R/T/A 各自 min/mean/max；mean 是采样点算术平均，不是光源加权带积分。JSON 包含规范化 design/settings、完整 result 和两种摘要。`input_sha256` 只覆盖实际 result 的 design、规范化 settings、实际 provenance；`artifact_payload_sha256` 覆盖写入载荷（计算摘要前不包含其自身字段）。二者均用 UTF-8、sort_keys=True、ensure_ascii=False、allow_nan=False、紧凑 separators 的规范 JSON 计算 SHA-256。不为摘要重新读取材料目录。

MCP 将服务/路径/类型/数值/I/O 错误转为既有 `ToolError`：`ServiceError` 保留 `code: message`，Pydantic `ValidationError` 使用 `invalid_request`，普通 `ValueError`/`OSError` 保留消息文本，不虚构统一结构错误码。默认禁止覆盖，并在计算前检查目标冲突；并发创建目标由原子 no-clobber 保留已有文件。CSV 先发布，若后续 JSON 发布失败，错误明确返回已保存 CSV 路径，不删除可能被其他进程替换的文件。两文件不是联合事务。

AI 只读集合含 schema/materials/validate/load/expand 五项；save 属 design 执行，simulate 属 simulation 执行，均无 film submit/queue 工具。UI 类型含 `film_design` / `film_simulation`；仅膜系窗口 calculate 直接交由白名单 adapter，既有沉积 main calculate 的排队确认规则保留。

调用示例（已绑定材料的设计）：

```python
service = FilmDesignService(project_root)
design = service.load('examples/film_design_nbk7_plate.cfilm')
result = service.simulate(design, {
    'wavelength_min_nm': 400, 'wavelength_max_nm': 700,
    'samples': 301, 'angle_deg': 0, 'polarization': 'unpolarized',
})
service.export_csv('data/results/plate.csv', result)
```

## 设计 schema 与引用

`schema_version=1`，`layer_order='substrate_outward'`。正背面数组 `front_layers` / `back_layers` 都从各自基片面向外排列；计算时才转换传播次序，总计最多 4096 层。

每层包含唯一 `id`、`material_id`、`optical_set_id`、`thickness:{value,unit}`、`note`，并可保留 `symbol` 与表达式展开标签 `group`（`G1`/`G2`）。新增可选的 `interactive_group_id` 是另一独立字段：正安全整数表示分组号，字段缺失或 `null` 表示未分组；它沿用 schema v1 的可选字段兼容规则，保存/加载/复制和膜层编辑均保留。该字段仅供交互式膜厚分析管理组成员，光学仿真与优化器不会把它解释为物理参数；不得将既有表达式标签 `group` 改作该用途。层厚度单位为 nm/um/qw；qw 按选定常数组在 `reference_wavelength_nm` 处的实部 n 换算为物理厚度。不存在隐式常数组选择。

`substrate` 包含材料 ID、常数组 ID、厚度（nm/um/mm）、`sides:single|double` 和 `mode:semi_infinite|incoherent|coherent`。单面模式不使用背面数组，存储的背面草稿不因此删除。`first_surface:front|back` 决定首入射面；有限基片背入射会交换膜堆和两外部介质，半无限模式拒绝背入射。

`incident_medium` 与 `exit_medium` 使用透明常数 `{kind:'constant',n}` 或材料引用 `{kind:'material',material_id,optical_set_id}`。首版外部端口必须透明。`assume_lossless` 必须显式 bool：未知 k 默认报错，true 才采用 k=0 假设并记入警告。

`expression:{text,symbols,in_sync}` 保存表达式原稿和符号厚度定义；手工编辑膜层将同步标记置 false。支持单字母、正十进制数倍率（无科学计数法）、嵌套括号后 `^正整数` 与 G1/G2；不支持完整 TFCalc 语法或公式内单位后缀，不使用 eval。最大深度 64，禁止不产生膜层的重复组。

材料沿用离散 nk、Cauchy 与三项 Sellmeier，多组名称、备注、来源、波段及单位完整保留。只读新增 N-BK7 与 Malitson 熔融石英参考，均不伪造 k、不替换 K9 等既有资料。N-BK7 来源 n 相对空气，未换算至真空，结果明确警告。

## 计算输入、输出与错误

`settings` 接受 `wavelength_min_nm`、`wavelength_max_nm`、`samples`、`angle_deg`、`polarization:s|p|unpolarized`、`first_surface` 与 `light_source`。默认 400–700 nm、301 点、0°、非偏振、平坦光源；首入射面默认继承设计。采样范围为 2–10001，单次点数×(总膜层数+3)不超过 1000000。

光源可为 flat、blackbody（temperature_k）或 tabulated（wavelength_nm/intensity/source）。本征 R/T/A 与光源谱分别计算；光源为每波长相对谱密度、峰值归一化，非绝对辐射功率。

完整返回对象 `kind='coat_sim.film_spectrum'`、`schema_version=1`，包含 `design_id`、设计/设置副本、`wavelength_nm`、等长 `R/T/A`、`source_spectrum`、`reflected_spectrum`、`transmitted_spectrum`、`absorbed_spectrum`、`source_units`、`definition`、`warnings` 与 `provenance`。每份 provenance 保存实际选中光学组全文及 SHA-256；冻结计算另含 `input_snapshot_sha256`。

半无限模式的 T 是进入基片端口的通量，A 仅为膜层吸收。有限相干模式的 A 包括基片体吸收。透明厚基片非相干模式用往返强度级联，包含背面多次反射；分别级联 s/p 后再平均，不直接相加两面 R。首版拒绝吸收基片或倏逝内部端口的非相干计算，避免给出错误通量；有吸收的有限基片可选相干模式。

结构、无效选择、未知 k、超出 nk/光源有效波段、数值非有限值及预算问题报 `ValueError`，不发布伪造曲线。保存/导出的 I/O 失败可能报 `OSError` / `FileExistsError`；并发创建目标时仍保留原文件。GUI 使用既有 `UiControlError` 约定，并暴露 busy / error / result_stale；旧异步结果不得覆盖新参数，busy 或过期结果不可导出。

## 数值验证范围与限制

历史回归覆盖裸 Fresnel、无损与吸收守恒、AR/HR、斜入射 s/p、Brewster/TIR、强吸收、双面基底、材料数据切换、表达式错误、持久化及 GUI 编辑/计算/绘图。这是覆盖范围说明，不能当作当前提交的通过证明。

k 的现行项目约定：关闭 k 时计算为 0；启用 k 时，仅在其自身来源有效域内取值或线性插值，缺失或域外计算为 0，不外推。原始未知状态保留，计算默认值不写回材料。该约定不是材料实际无吸收的外部证明。n 的 endpoint_hold/error 策略、独立来源单位和数据有效性检查保持独立。

单面采用半无限基底；双面含裸后界面。厚片平均条纹是工程近似，不能据此判定真实测量的相干性，不替代激光、高分辨平行片测量或制造验证。采样不足应显式报告，不能插值伪造收敛。

QW 与物理厚度的关系为 QW=4 Re[n(λ0)]d/λ0；派生显示不覆盖保存的 nm/um 厚度。性能测试必须固定材料、采样、层数与 k 假设，区分目录冷启动、热复算、绘图与按钮总时间；不得把重复单材料结果推广到任意异质膜堆。

离屏 Qt 或 QTest 通过不代表 Windows 原生标题拖动、双击、多 DPI、所有主题或长序列生命周期均通过。实机验证需单独报告平台与确切范围。

参见 [算法说明](algorithms.html#film-k-defaults-20261007)、[软件手册](software_manual.html)、[测试说明](testing.md)。
