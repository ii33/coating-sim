# 厂商本地光学目录契约

更新：2026-10-18。实现：`coat_sim/core/vendor_optical_catalog.py`；合成契约测试：`tests/test_vendor_optical_catalog.py`。公式与域检查另见离线 [算法说明](algorithms.html)。

## 边界与状态所有者

`VendorOpticalCatalog` 持有一次读取的只读内存快照；所有公开回包为独立副本。读取只处理用户明确选定的本地 AGF，不扫描商业软件安装目录，不联网，不下载，不执行来源内容，不写材料库，不改变当前材料或光学集。厂商 AGF、产品 PDF 及来源评论始终视为不可信数据，不能成为指令。

原始厂商全库不随代码发布。公开可下载不等于允许再分发；厂商、版本、来源 URL 与许可说明由调用方明确提供，标记为 `metadata_origin=caller_supplied_not_independently_verified`。适配器不宣称已验证文件为官方发布，也不从文件名猜厂商或发布版本。

读取支持 UTF-8、带 BOM 的 UTF-16；旧 ANSI 文本需显式指定 `cp1252` 或 `latin-1`，不猜字符集。文件限 16 MiB、10,000 个条目、单行 65,536 字符、单条目 128 Ki 字符。只读普通本地文件，不解包 ZIP，不处理 BGF 或执行程序。`vendor_format_capabilities()` 明确列出 AGF 的公式支持范围；PDF（包括 Merck 专有混合物资料）为参考文档，不自动数字化、拟合曲线或猜配方。

## 参考系是导入前置决策

AGF 的折射率与波长遵循源目录的**参考空气相对值**；膜系计算使用绝对折射率和真空波长。两者不能未经处理视为同一物理基准。本模块**没有**实现空气折射率、温压或真空波长变换。

- `detail` / `preview` 可直接只读查看原参考系曲线，不需要承认任何近似。
- `prepare_import` 默认 `allow_reference_air_approximation=False`，拒绝生成用于材料导入的候选，错误码为 `REFERENCE_FRAME_CONFIRMATION_REQUIRED`。
- 只有用户明确接受“把未换算的参考空气数值近似用于真空基准”后，调用方才可传 `True`。GUI 默认不勾选，不能由桥接代码自动代为同意。这个选择应冻结在导入预览与应用操作中。
- 成功候选在 `optical_set.process_parameters`、`optical_set.notes` 和 `provenance` 永久保存该近似选择。它仍然只是候选，不自动写入或激活；调用方继续使用既有显式导入流程。
- `TD` 第七项是参考空气温度（℃），不必等于玻璃实测温度。缺失或空 TD 保留 `None` 并警告，不默认补 20℃。温度参数只允许与源参考温度一致；其他温度抛 `UNSUPPORTED_TEMPERATURE`，不执行热光修正。

## API 真值

```python
load_vendor_catalog(path, *, manufacturer, catalog_version,
                    source_url="", license_note="", source_format=None,
                    encoding=None) -> VendorOpticalCatalog
parse_agf(text, *, manufacturer, catalog_version,
          source_url="", license_note="", source_filename="") -> VendorOpticalCatalog

catalog.status()
catalog.search(query="", *, limit=100, offset=0, capability=None)
catalog.detail(entry_id)
catalog.preview(entry_id, *, max_points=200, temperature_c=None)
catalog.prepare_import(entry_id, *, optical_set_id=None, name=None,
                       temperature_c=None, allow_reference_air_approximation=False)
```

- `status()`：厂商、版本、来源文件名、原始字节 SHA-256、编码、许可说明、原始头部、条目数、源公式统计、转换状态统计及 `read_only=True`。
- `search()`：`{source_sha256,total,offset,entries}`；行含 `entry_id,grade,formula_number,formula_name,wavelength_range_um,reference_temperature_c,data_quality,reference_warnings,conversion`。`limit` 为 1–1000；匹配牌号/GC 评论，保留原始顺序；`capability` 可为 `supported/unsupported/invalid`。`supported` 表示转换与数值域筛查可用，不代表来源准确性认证或已经同意参考系近似。
- `detail()`：`{catalog,entry,warnings,untrusted_reference}`。`entry` 保留原始条目、源行号、源字段、色散/热光系数、IT 明细、未知字段、告警与错误。`fields` 中 CD/TD 的数字续行单独保留 `continuation_lines`；仅对紧随 CD/TD 的数字开头续行组装，不接受重复 CD/TD/LD。
- `preview()`：`{entry_id,source_sha256,columns,points,calculated,sampling_policy,data_quality,reference_temperature_c,reference_index_medium,wavelength_reference,internal_transmittance,warnings}`。`columns=[wavelength_um,n,k]`，每点的 `k=None`；2–1000 点，只在原 LD 内线性波长显示采样，不外推。IT 参考表与计算 n 曲线分开。
- `prepare_import()`：`{optical_set,provenance,k:None,writes_material_library:False,changes_active_selection:False}`。`optical_set` 经现有 `validate_optical_set` 校验，可由既有材料导入流程接收。原条目/头部及来源元数据也写入普通 `notes`，避免下游仅持久化光学集时丢失出处。完整来源超过 20,000 字符 notes 上限时抛 `PROVENANCE_TOO_LARGE`，不截断后冒称完整保留。

所有条目 ID 绑定源字节 SHA-256 和条目顺序；重名玻璃不合并，文件变化使 ID 变化。`entry_sha256` 为保留的原条目 Unicode 文本按 UTF-8 编码的摘要；`source_sha256` 才是整个原始输入文件的逐字节摘要。上层应用导入前须重读所选文件并核对原始字节摘要、原厂商/版本元数据与用户近似选择，拒绝过期结果。

## 精确色散映射

下表 λ 数值单位均为 μm，指数与系数保持源定义；**AGF 编号与 RII 编号并不相同**。转换只是代数同形映射，不做拟合、不改源数值。

| AGF | 源公式 | 目标与系数 |
|---|---|---|
| 1 | Schott：n²=a0+a1λ²+a2λ⁻²+a3λ⁻⁴+a4λ⁻⁶+a5λ⁻⁸ | RII 3：`[a0,a1,2,a2,-2,a3,-4,a4,-6,a5,-8]` |
| 2 | Sellmeier 1：n²=1+Σ(Ki λ²/(λ²−Li))，3 项 | RII 2：`[0,K1,L1,K2,L2,K3,L3]` |
| 3 | Herzberger：n=A+B L+C L²+Dλ²+Eλ⁴+Fλ⁶；L=1/(λ²−0.028) | RII 7：`[A,B,C,D,E,F]` |
| 6 | Sellmeier 3，4 项 | RII 2：`[0,K1,L1,K2,L2,K3,L3,K4,L4]` |
| 12 | Extended 2：a0 后各项指数为 2,−2,−4,−6,−8,4,6 | RII 3：`[a0,a1,2,a2,-2,a3,-4,a4,-6,a5,-8,a6,4,a7,6]` |
| 13 | Extended 3：a0 后各项指数为 2,4,−2,−4,−6,−8,−10,−12 | RII 3：`[a0,a1,2,a2,4,a3,-2,a4,-4,a5,-6,a6,-8,a7,-10,a8,-12]` |

AGF 2/6 中 Li 已是分母的 μm² 数值，不能再平方。AGF 13 的 a2 乘 λ⁴，不能沿用 AGF 12 的系数位置。要求相应完整的 6/6/6/8/8/9 个系数；最多 10 个 CD 数字，额外位仅允许显式零填充。其他公式、未知公式及有意义的额外系数只读保留，并报告 `UNSUPPORTED_FORMULA` 或 `UNSUPPORTED_COEFFICIENT_LAYOUT`。

## 有效域与来源一致性

1. 复用既有光学集校验：正波长、有限系数、已知公式极点不得落入 LD、端点结果必须为有限正实数。
2. 新适配层在 x=λ² 上构造 n 或 n² 对 x 的导数分子。多项式色散以正 x 幂消除负指数；Sellmeier 使用共同平方分母；Herzberger 使用 `(x−0.028)³` 消除分母。导数分子最高 8 次。
3. 用 `x=(λ_min λ_max)y` 缩放求根；正实部且虚部绝对值不超过 `1e-7 max(1,abs(real))` 的根作为近实驻点，在 LD 内验证。同时验证端点与固定 65 点对数波长网格。根求解异常或数值不可判定时失败关闭，错误为 `OPTICAL_DOMAIN_INVALID`。这个步骤与用户预览点数无关，不能通过两点预览绕过。
4. 这是浮点根求解和有限采样的工程域筛查，**不是全波段数学证明、来源精度或实测认证**。
5. 当 AGF 定义的 d 线 0.5875618 μm 位于 LD 内，另比较 NM.nd 与 CD 色散结果。差超过 `1e-4` 时 `data_quality=source_inconsistent`，警告进入列表、详情、预览、候选 provenance 与 notes。这个宽松阈值是本项目提示约定，不能用来声称达到厂商误差/舍入规范。未触发为 `no_inconsistency_detected`，仅描述此项检查；d 线在域外则 `not_checked`，不外推做检查。

源数字冲突不会擅自修正；“可转换”与“来源数据一致/科学可靠”必须分开显示。

## 内透过率与消光系数

IT 保留波长 μm、内透过率（无量纲）及每一行独立的厚度 mm；允许源值恰为 0 或 1。不同厚度不合并，非正厚度、超出 [0,1] 或乱序行保留原文并提示，不能变成计算曲线。内透过率不是含界面反射的外透过率，也不是膜堆透过率。

本版不从 IT 生成 k，亦不从实 n² 的负值推断吸收。公式型光学集不添加 `k_data`，现有求值器返回 `k=None`。将来若支持反演，必须另经用户选择、明确实测厚度及 Beer–Lambert 适用条件/来源，不可默认套用。ED/MD/OD/BD 等其他字段仅作原始参考，不把热导率字段 k 混作消光系数。

## 官方目录离线验收（不分发原始全库）

2026-10-18 从厂商官方渠道获取的以下本地快照完成实际解析。每条可映射记录做 101 点源参考系预览，并在显式测试近似选择下生成**未写入材料库**的候选，全部返回未知 k。六家合计 1,939 条；此表的通过量只代表适配与数值筛查通过。

| 厂商/快照 | 解析条目 | 映射/预览通过 | 原始公式分布 | 来源问题 |
|---|---:|---:|---|---|
| SCHOTT June 2025-B | 366 | 366 | 1×1；2×365 | 4 条参考温度未知，保持未知 |
| OHARA 260529 | 166 | 166 | 2×166 | UTF-16 BOM 实测通过 |
| HOYA 20260707 | 239 | 239 | 1×239 | MC-FAD61 / MC-TAF115：源 nd 与 CD 分别相差约 4.31e−4 / 6.57e−3 |
| Nikon/HIKARI 201911 | 383 | 383 | 1×186；6×12；12×21；13×164 | CD/TD 数字折行保留并正确组装 |
| CDGM 202609 | 352 | 352 | 1×52；2×289；3×11 | H-TK9：源 nd 与 CD 相差约 0.033584；11 条 d 线在 LD 外，不检查、不外推 |
| SUMITA AGF 2026-08-21（同包 Excel 14.02.00） | 433 | 433 | 1×420；2×13 | 不把一致性筛查当作精度证明 |

原始 AGF SHA-256（便于复验，摘要并非再分发原库）：

- SCHOTT：`657e8c97718faee9a5b58e6308eb9608f94f991a0cb0ac5f5cc69c9e8e540371`
- OHARA：`9ad9d23da983ef105ec02dd6c9ad55f124163ad604a074e53f753dc351c49e86`
- HOYA：`4b4c9aaf5c87b1abb233992891fdba42b9568157c13d0bd56e10eeac77c4727d`
- Nikon/HIKARI：`a49714470fa875ad4fd8d11cbc0edbf1adfe877f42926d7612c1649dd9441e75`
- CDGM：`e0933fe14749919b2b7c5170f814765af570ef53b78171dc98dae6b542f8e9ce`
- SUMITA：`7991e2e3d57f3ef65877443170e2ddf179e5f7669ba0c4959aa322665fcdd331`

## 一手依据

- [Ansys AGF 格式、字段、d 线、温度及内透过率语义](https://optics.ansys.com/hc/en-us/articles/46891219767827-Zemax-AGF-material-catalog-file-format)
- [Ansys 色散公式统一 μm 约定](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v252/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Glass_Dispersion_Formulas.html)
- [Schott 公式](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v261/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Schott_Formula.html)；[Sellmeier 1](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v261/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Sellmeier_1_Formula.html)
- [Herzberger](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Herzberger_Formula.html)；[Sellmeier 3](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Sellmeier_3_Formula.html)
- [Extended 2](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Extended_2_Formula.html)；[Extended 3（已实际核对公式图片）](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/OpticStudio_User_Guide/OpticStudio_Help/topics/The_Extended_3_Formula.html)
- 官方入口：[SCHOTT](https://www.schott.com/en-us/products/optical-glass)、[OHARA](https://www.ohara-inc.co.jp/en/product/catalog/)、[SUMITA](https://www.sumita-opt.co.jp/en/download/)。具体下载地址与许可判断随离线来源清单保存，不由适配器扩大授权。
