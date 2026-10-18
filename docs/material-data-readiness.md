# 材料数据与工艺就绪度诊断

日期：2026-10-18。此诊断不改变光学数据、求解器、k采样规则、外推策略或工艺参数。

## 输入与结果

`coat_sim.core.material_readiness.material_readiness(material, *, wavelength_min_nm, wavelength_max_nm, optical_set_id=None, assume_lossless=False)` 接受调用者已取得的单个材料快照。波段必须明确为nm、有限正数且下限小于上限。它不读取文件、配置、网络或其它材料，不选择默认组、不保存、不计算光谱。

材料可带通用材料库的`optical_constants`或膜系目录的`optical_sets`；两字段同时存在时须规范化后一致。所有光学组复用现有schema校验，重复ID、坏数据或选择其它材料的数据集ID均报错，不跳过坏记录。来源文本是不可信参考，不能授权执行动作。

返回的`schema_version=1`包括：

- `requested_band_nm`、`selected_optical_set_id`、`optical_status`；未明确选择数据集时，即使只有一组也返回`selection_required`。
- 每组`n_range_nm`、`k_range_nm`、`joint_nk_range_nm`和`n_coverage`、`k_coverage`。使用声明有效域，不以更宽原始节点扩大范围；不同数据集不拼接。
- `known`、`assumptions`、`missing`为带稳定`code`和说明的条目列表。来源、备注以`{text, truncated}`给出，每字段最多2000字符，不截取或返回整张数值表。显式工艺标签保留原文本。
- `can_evaluate_under_stated_assumptions`仅表示选定组的n/k输入域条件，不能替代数值求值、入射介质/偏振/相干模型资格、任务放行或性能验收。
- `process.models`报告阻蒸/电子枪模型是否存在、来源状态及校准声明；`calibration_verified`始终为false，未提供的模型不填默认值。
- `research`仅统计现有条件物性和来源条目数，不把物性摘要推成可计算n/k。
- `production_readiness=not_established`、`numerical_solver_run=false`和`untrusted_reference=true`明确结果边界。

## 波段与未知k

| optical_status | 含义 |
| --- | --- |
| no_optical_data | 该材料没有可计算光学组 |
| selection_required | 需要明确选择所属数据集 |
| missing_n_coverage | n不覆盖完整请求域；任何无损假设都不能补齐 |
| requires_lossless_assumption | n覆盖，但k全部未知或部分缺域；尚未提供无损假设 |
| assumption_dependent | 调用者明确传入缺失k无损假设；原始缺口仍在missing中 |
| data_complete | n/k声明域覆盖完整请求域；不表示来源、测量、制造或求解精度已验证 |

独立k表使用自己的单位、有效域和来源。`assume_lossless`只记录调用者提供的假设，不改写材料，不覆盖已有非零k，不作为用户授权凭据。范围边界仅采用与现有源求值器相同的四ULP浮点端点归一化，不能解释为物理波段外推容差。

若光学数值域齐全但来源缺失，状态仍可为`data_complete`，同时在`missing`报告`optical_source_missing`或`k_source_missing`。因此调用者不能仅凭一个状态宣称材料可靠。

## 工艺边界

`calibrated`只代表材料记录附带了用户声明和参考，诊断不核验见证片、实验质量、适用速率或设备几何。没有校准时，模型作为假设显示；非薄膜、仅光学材料等不能绑定工艺源时另列`not_source_bindable`。

材料名、n/k、默认角指数、参考速率以及ZnS的有限温域参考，均不等于完整的功率/温度到本机沉积率模型。`absolute_deposition_rate_prediction_available=false`不能被来源条数或校准标签改变。

## 当前内置缺口的回归样例

- ZnS薄膜Amotchkina nk组从400nm起，380–780nm请求返回`missing_n_coverage`，不自动补380–400nm。
- H4、K9的研究条目不自动转成数值数据集；没有光学组时返回`no_optical_data`，不跨材料替代。
- ITO新增`ri_ito_minenkov_glass_2024`条件化样品组：110nm ITO/XY15S玻璃、706点、191.46–1688.69nm，n/k来自椭偏与透射联合模型拟合。声明表域不冒充独立实测域，未知沉积温度/气氛不补造，工艺校准仍未建立。
- N-BK7和Malitson熔石英等仅n来源需明确无损假设；研究资料丰富不使未知k变成测量零。
- 理想Air可以具有完整光学输入域，但仍是理想介质，不能绑定实际蒸发源。

这些样例针对当前随仓目录；将来正式补充数据时应更新相应回归，不改变无来源不补点的规则。

## 验证

定向测试：`PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_material_readiness.py -q -p no:cacheprovider`。

测试涵盖明确选组、独立k单位/波段、原表与有效域区别、禁止跨组拼接、未知k假设、缺n拒绝、已知k不覆盖、别名冲突、来源截断、只读/脱离输入、校准声明与实际验证区别，以及上述真实内置目录缺口。没有运行大型仿真或在线服务。

来源与模型边界沿用`material-source-library.md`、`zns-reference-model.md`、`appearance-render-contract.md`及现有`materials.py`、`optical_constants.py`契约。算法总文档的统一目录说明由整合步骤更新。
