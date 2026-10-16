# CIE 照明体与薄膜光谱后处理小包

版本：2026-10-06。只交付可嵌入数据、参考后处理和测试；未修改任何产品或薄膜求解器。

## 首发集合

- **CIE 标准照明体 D65**：代表约 6500 K 相关色温的平均昼光。官方表，300–830 nm、1 nm、531 点。
- **CIE 标准照明体 D50**：代表约 5000 K 相关色温的昼光；ISO/CIE 11664-2:2022 已将其纳入标准照明体。官方表，300–830 nm、1 nm、531 点。
- **CIE 标准照明体 A**：代表钨丝灯照明，与约 2856 K 的普朗克辐射分布对应。本包直接使用官方表，不用四舍五入后的色温自行重建。300–830 nm、1 nm、531 点。
- **E 等能参考**：按“每单位波长的相对辐射量恒定”生成 S(λ)=100。本包声明计算域 360–830 nm、1 nm、471 点。它不是下载的官方数据集，也不冒称 ISO/CIE 11664-2 的标准照明体。该计算域不是可见光边界的严格物理定义。

A、D65、D50 的现行标准身份和定义可核对 [CIE 的 ISO/CIE 11664-2:2022 页面](https://www.cie.co.at/publications/colorimetry-part-2-cie-standard-illuminants-0)。E 的定义依据 [CIE e-ILV 17-23-023](https://cie.co.at/eilvterm/17-23-023)。D50、D65 必须用各自标准 SPD，不能以相同色温的黑体代替，也不能拿 AM1.5 太阳光谱代替。CCT 不是光谱的唯一标识。

另附两项官方支持数据：光视效率 V(λ)，以及 CIE 1931 2° 配色函数 x̄/ȳ/z̄；均为 360–830 nm、1 nm、471 点。观察者不是光源，不应放进光源选择框。

## 集成入口

- `data/cie_illuminants.json`：自包含目录、数值、定义域、出处、DOI、许可、已知异常。可直接 JSON.parse 后交给模块。
- `data/{A,D65,D50,E}_1nm.csv`：有列头的独立光谱文件。
- `data/V_1nm.csv`、`data/XYZ1931_2_1nm.csv`：光度/色度支持数据。
- `upstream/`：从 CIE 直接下载的未改动 CSV 与对应 JSON metadata，保留原字节和换行。
- `src/illuminants.mjs`：无依赖 ECMAScript module；只后处理，不含薄膜求解器。
- `NOTICE_DATA.md` 与 `LICENSE_DATA_CC_BY_SA_4_0.txt`：必须随数据重分发保留。
- `reports/upstream_verification.json`：原始哈希、官方抽样行/列总和核验、已知元数据异常。
- `reports/independent_reference.json`：独立 Python 从原 CSV 计算的回归参考；其薄膜谱为明确标注的合成样例。
- `reports/node_tests.tap`：24 项测试全部通过。

示例（Node.js 或由打包器导入等价 JSON；浏览器模块本身不依赖 Node）：

```js
import { weightedRta, gridFromStep } from './src/illuminants.mjs';
const catalog = await (await fetch('./data/cie_illuminants.json')).json();
const grid = gridFromStep(360, 830, 1); // 471 points, inclusive endpoints
// rta comes from the existing solver, evaluated on aligned standard-air coordinates.
const summary = weightedRta(catalog, rta, {
  illuminantId: 'D65', bandNm: [360, 830], weighting: 'photopic'
});
// summary.fractions.R/T/A are weighted fractions, NOT replacement R(λ)/T(λ)/A(λ).
```

`rta` 契约：`{wavelengthBasis:'standard_air', wavelengthsNm:[...], R:[...], T:[...], A:[...]}`；R/T/A 是 0–1 功率比例，不是百分数，不是场振幅，也不是光密度。各数组长度必须相同，波长严格递增，数值有限。本参考接口针对常规被动、弹性光学、无损入射介质、定义一致的功率平衡域，校验 R+T+A≈1。吸收入射介质、增益、荧光、非线性、散射未计入通道等问题需要先明确模型，不能通过裁剪 RTA 或补零掩盖。

## 物理与指标边界

1. 单色 R(λ)、T(λ)、A(λ) 由膜层 n/k、厚度、入射角、偏振、基底和相干模型决定。固定这些条件时，更换 A/D65/D50/E **不能改变单色结构响应**。光源只进入后处理。源选择也不自动改变偏振、角度分布或厚基底相干性。关于薄膜功率定义、吸收介质与相干/非相干处理可参见 [Byrnes, Multilayer optical calculations](https://arxiv.org/abs/1603.02720)。
2. 相对入射谱为 S(λ)，则相对反射/透射/吸收谱分别为 S·R、S·T、S·A。这些“输出相对功率谱”可以随光源变化，但不是 RTA 曲线本身。原始 SPD 归一到 S(560 nm)=100；没有绝对辐照度/功率标定，不能给出 W、W/m²、lux 或流明。
3. 对明确波段 B，辐射功率加权透射率：`T_e,B = ∫B S(λ)T(λ)dλ / ∫B S(λ)dλ`。R/A 同理，使用同一分母、波段和求积权重时三者之和仍为 1。
4. 明视觉加权透射率：`T_v,B = ∫B S(λ)V(λ)T(λ)dλ / ∫B S(λ)V(λ)dλ`。R 同理；A 的结果应称“V(λ) 加权吸收比例”，不要混淆成材料中真正存在的“被吸收流明”。它与能量加权结果通常不同。固定灰滤片在两种权重下得到同样的常数比例。
5. 光视效率使用 [CIE 官方 V(λ) 数据](https://cie.co.at/datatable/cie-spectral-luminous-efficiency-photopic-vision)。普通能量比不加 V；光子数权重另含 λ/(hc)，本模块未提供。普通明视觉指标不能冒称暗视觉、昼夜节律或任意观察者指标。
6. 如以后计算颜色：`X=k∫S·Q·x̄dλ`，Y/Z 类似，`k=100/∫S·ȳdλ`，Q 为 R 或 T，且白点/观察者/积分域必须同口径。附带 XYZ 数据用于独立白点核对；本包未实现色适应、RGB、Lab 或显示色管理。不要把反射/透射输出各自强制峰值归一化而丢掉亮度。

上述积分表达是明确的后处理契约。`weightedRta` 使用节点合并、最多 1 nm 间隔的复合梯形求积，是工程数值实现，不代表声称符合全部 CIE 色度计量/测量带宽标准。白点回归另以 1 nm 等间隔求和检查。

## 插值、归一化与缺域

- D50/D65/V/XYZ metadata 指定线性插值。本包在相邻原数据间线性插值；避免高阶样条过冲/负光谱，不外推。
- **A 例外**：当前官方 metadata 指定 `useRelatedFormula`。本包优先整数 nm 的官方样值；用户要求非整数波长时只提供带 warning 的线性工程近似，不冒称标准公式求值。若产品要严格公式模式，应另取得并核验标准公式及其固定常量，不用“2856 K + 最新物理常数”擅自替代。默认整数 1 nm 采样无需这一近似。
- 保留原 SPD；绘图可选 `raw`、`at560`（560 nm=1）、`peak`（当前查询节点峰值=1）、`area`（当前查询域梯形积分=1）。面积归一化后数值单位是 nm⁻¹。图表要标明归一方式及域；它们均不是绝对光功率标定。任何统一正比例缩放都会在加权比的分子/分母相消。
- CIE metadata 的表外约定为 `zero`，这不说明真实光源在表外没有辐射。本包为了避免把缺失波段误当无光，默认采用更严格的 **报错**。不对缺失 RTA/n/k 补零，不自动缩窄积分域，不计算分母为零的比例。
- 请求波段必须同时被光源、RTA 和所需 V/XYZ 覆盖。对于不完整谱，只有明确改选并显示新波段后才可得到“该波段内指标”。例如 400–700 nm 的结果不能无说明地命名为全波段明视觉透射率。
- 不完整域仍可以显示原单色 RTA；只禁用不具覆盖条件的光源加权指标。材料 n/k 覆盖性由原求解器负责，不能因为 CIE 表有数值而自动扩充材料范围。
- 官方表使用标准空气中的波长坐标；很多 TMM 求解器输入真空波长。严格计算要依据明确空气折射率模型，将同一物理波长坐标对齐后再求值：λ_vac=n_air·λ_air。若转换 SPD 的每波长密度，还需一致处理 Jacobian；最简单是保留 CIE 空气波长积分轴，只在调用求解器时转换查询波长。模块要求显式 `standard_air` 标记，拒绝把真空轴悄悄改名。研究/快览若忽略这一区别，必须明确标成近似，不能声称严格对齐。

## 1 nm 与点数建议

闭区间等间隔网格：`N=(λmax−λmin)/Δλ+1`，反算 `Δλ=(λmax−λmin)/(N−1)`。

- 300–830 nm，1 nm：531 点
- 360–830 nm，1 nm：471 点
- 380–780 nm，1 nm：401 点
- 400–700 nm，1 nm：301 点；300 点实际间隔约 1.003344 nm

范围与步长不整除时，本包报错，让调用者显式调整端点或选择点数，不悄悄改变“1 nm”的含义。建议 UI 以范围和步长为主、联动显示点数；历史点数输入保留时同时显示实际步长。

**1 nm 是本包数据网格，不是所有膜系足够准确的保证。** 厚层、高 Q 腔、陡边等可能需要更细的求解网格/自适应采样。比较 1、0.5、0.25 nm 的 RTA 特征和积分结果收敛性，按产品误差预算决定。将 5 nm 结果插到 1 nm 无法恢复丢失的窄峰；本模块默认拒绝间隔大于 1 nm 的 RTA，只有显式 `allowCoarseRta:true` 才近似插值并给出 warning。已有粗 RTA 导出文件应促使重新运行求解器，而非把“插值后的 1 nm”当成独立计算分辨率。

## 实测来源异常与处理

此次直接取得的五个官方 CSV，MD5 和 SHA256 **全部与对应当前 metadata 一致**，所有 metadata 抽样行和列总和也一致。但：

- A 网页仍显示旧 MD5 `6c33a1c3caff2d774f0f2025f3001a56`；当前文件及 v2 metadata 一致为 `ed0e4effb55d82b950c0912b6278a9d1`。网页描述引 CIE 015:2018，metadata 已引 ISO/CIE 11664-2:2022。使用匹配的当前“文件+metadata”快照，保留差异。
- V 网页 MD5 为 `272297df28120479b5a1ffb9a0388b02`，当前文件与 metadata 一致为 `f389958555461a7d9a7562145e8ca9c0`。本次逐行核对 V 与 CIE 1931 ȳ 完全相同。
- D65 metadata 的波长列起点为 300，但 SPD 列误差/冲突项写 360；实际 CSV、531 行、官方校验和/列总和、标准介绍共同支持 300–830 nm。本包据此采用实际表域并列出 warning；未修改原 metadata，也未声称 CIE 已发布勘误。

以上为当前一手内容的不一致记录，不是下载失败或本包手填修补。详细证据与完整来源 URL 在数据目录和核验报告。

## 运行与验收

无需安装第三方包；Python 3.12 与 Node 24 已验收。

```sh
python build_data.py
python tests/independent_reference.py
node --test --test-reporter=tap tests/illuminants.test.mjs
```

24/24 通过：原值/样本数、V 与 ȳ、点数换算、非整数插值和 A 提示、归一化、域外/NaN/零权重拒绝、中性片不变性、源切换不修改冻结的 RTA、光度与能量差异、能量守恒、粗采样提示、波长基准检查、白点 smoke check、JS 与独立原 CSV Python 求积一致（容差 2×10⁻¹⁴）。这些是本包校验，不替代产品集成测试或计量认证。
