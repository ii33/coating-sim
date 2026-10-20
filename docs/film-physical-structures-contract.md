# 可检查、可导出的物理结构库

日期：2026-10-20。实现：`coat_sim/core/film_physical_structures.py`；接口与回归真值：`tests/test_film_physical_structures.py`。算法图解见 [algorithms.html](algorithms.html)。这是当前设计 schema 上的纯计算构造器，不新造薄膜文件格式。

## 输入、输出和状态所有权

```python
generate_physical_structure(
    environment: Mapping[str, Any],
    spec: FilmStructureSpec,
    frozen_material_snapshot: Mapping[str, Any],
) -> FilmPhysicalStructureResult
```

`environment` 是合法的裸基片设计：无 front/back 层、single 基片、front 入射、`use_k=True`、`assume_lossless=False`。完整保留基片、介质、参考波长和其他环境参数。所有输出层均为物理 nm，不依赖环境参考波长解释厚度。当前构造范围是单面前镀层；不自动改写背面或设备相干策略。

`spec` 是 frozen dataclass，含必填 `mechanism`、显式角色 `bindings`、按顺序排列的 `sections`；可选 `name='physical structure'`、`validation_wavelengths_nm=()`。`FilmStructureMaterialBinding(role, material_id, optical_set_id)` 明确固定薄膜材料与数据集。每个角色必须唯一，允许多个角色引用同一数据集。输入不绑定实时材料服务；快照必须提供 `materials` 数组，其材料及 optical_sets 结构沿用现有冻结设计。构造器复制数据，不调用服务、GUI、网络或文件写入。

返回 `FilmPhysicalStructureResult(candidate_design, metadata)`：

- `candidate_design` 通过现有 `validate_design`，每个真实单层含 material_id、optical_set_id、物理厚度和确定性稳定 ID。`front_layers`、所有 sections 和 metadata.layers 的顺序统一为 **基片→入射介质（substrate_outward）**。直接光学求解器若要求入射顺序，调用者需反转一次；既有设计服务已负责这一转换。
- `metadata.parameters` 是完整可检查参数；`layers` 是逐层处方，含序号、角色、来源数据集、厚度、构造波长与当时的 n/k，另含局部 Bragg／ratio 或 rugate cell/fill 参数；`sections` 给展开范围。metadata 包含材料快照摘要、来源字段与 n/k 各自有效域、已检查波长、引用和明确近似。
- 返回值的 frozen 外壳不表示内部 Mapping 不可改。调用者持有独立副本；修改结果不会污染输入或后续回放。相同环境、参数及材料快照产生相同处方和 ID。

调用者唯一拥有会话状态、种子候选集合、搜索预算及保存动作；构造器不会应用候选或宣称性能达标。该入口同步计算、无 worker/异步提交，不产生晚到结果。取消边界由调用方在构造前后管理；入口内部至多展开现有 schema 的 4096 层，不引入用户未要求的优化预算。

输入、材料、域或数值不满足契约时抛 `ValueError`，没有可应用的部分结果。拒绝未知 k、非被动 k、缺数据、外推、不满足 nH>nL 的角色及无效结构。所有实际使用材料（含基片、材料型入射介质）在全部构造波长和显式 validation_wavelengths_nm 上检查；入射介质要求 k=0。这不是任意连续谱段的有效域证明，后续求解必须检查自己的完整目标与验证网格。

## 参数与序列化

`structure_spec_from_mapping` 严格解析 JSON-compatible 对象，未知字段和 section type 都拒绝；`structure_spec_to_mapping` 可逆序列化。dataclass 类型/字段为接口真值。`physical_structure_catalog()` 提供每种 section 的参数、必填状态、默认值、来源及验收能力。

根 `mechanism` 为 `multistack`、`coupled_cavity`、`chirped`、`digital_rugate` 或 `composite`。前四项要求至少出现对应 section；所有机制都允许组合匹配、阻断或其他 section。标签不代表该光谱用途一定可实现，也不把结构搜索限制为固定五个经典配方。

| section type / dataclass | 参数与默认 |
| --- | --- |
| `layer` / FilmOpticalLayerSpec | 必填 role、wavelength_nm；optical_thickness_waves=.25；thickness_nm=None。给物理厚度时直接用该厚度，否则 d=λ×waves/n(λ)。匹配层、buffer、独立腔可各自指定材料、厚度和参考 λ。 |
| `bragg` / FilmBraggStackSection | 必填 wavelength_nm；pairs=8；H/L角色默认 H、L；first_role='L' 表示基片侧先L；high_low_optical_ratio=1；thickness_scale=1；terminal_role=None；terminal_optical_thickness_waves=.25。端层可构成 (HL)^m H 等奇数镜，其厚度独立，不跟随 pairs 的 thickness_scale。 |
| `coupled_cavity` / FilmCoupledCavitySection | 必填 substrate_mirror、cavities、coupling_mirrors、incident_mirror。端镜和耦合镜各为独立 Bragg section；N 个独立 layer 腔要求 N−1 个耦合镜。没有隐式镜像或自动调谐。允许组合独立阻断 section。 |
| `chirped` / FilmChirpedSection | 必填 start/end_wavelength_nm；pairs=16（≥2）；H/L/first_role 如 Bragg；ratio_start=ratio_end=1；spacing='linear' 或 'log'；thickness_scale=1。周期变化与 H/L 光学比例变化是独立自由度，不把两面镜叫双啁啾。 |
| `digital_rugate` / FilmDigitalRugateSection | 必填 wavelength_nm、period_nm；periods=8；samples_per_period=16（≥2）；mean_n=None→(nH+nL)/2；modulation_n=None→.45(nH−nL)；phase_rad=0；apodization='hann' 或 'none'；H/L角色独立。全部产物是真实 H/L 单层。 |

上表数量、线性/log插值、默认比例、Hann包络、.45振幅及 midpoint/symmetric cell 均为 **本项目约定**，不是论文处方或制造建议。层数只受现有设计 schema 资源上限约束；不额外施加 pairs=64、最小层厚、总厚、搜索次数等默认限制。

```json
{
  "mechanism": "multistack",
  "name": "two-stack with explicit matching",
  "bindings": [
    {"role": "H", "material_id": "selected-high", "optical_set_id": "selected-nk"},
    {"role": "L", "material_id": "selected-low", "optical_set_id": "selected-nk"}
  ],
  "sections": [
    {"type": "layer", "role": "L", "wavelength_nm": 4000, "thickness_nm": 100},
    {"type": "bragg", "wavelength_nm": 6000, "pairs": 8},
    {"type": "layer", "role": "H", "wavelength_nm": 7500, "optical_thickness_waves": 0.17},
    {"type": "bragg", "wavelength_nm": 10000, "pairs": 10, "high_low_optical_ratio": 0.8}
  ],
  "validation_wavelengths_nm": [3000, 5000, 8000, 12000]
}
```

这里的材料 ID 是占位符，需要真实且覆盖所需范围的用户数据；例子只是参数结构，不声称得到双窗口 AR 或双通带。

## 公式、坐标与适用近似

真空波长、沿层法向 z 的物理厚度都用 nm；z 从基片侧向入射侧增长。折射率采用已有求解器的被动 n+i k 约定。构造公式用法向实部 n 定一次厚度，后续完整谱计算保持物理厚度并使用各材料原始色散 n(λ)、k(λ)。吸收保留并进入后续 R/T，但四分之一波与相位谐振的理想解析预期只适用于无损或弱损耗条件。

Bragg 比例 r=OT_H/OT_L，令 OT_H=λs·r/[2(1+r)]、OT_L=λs/[2(1+r)]，其中 s=thickness_scale，d_j=OT_j/n_j。r=s=1 即 QW。多段堆栈／匹配按显式顺序拼接后作为一个整体相干结构求解，不能把单段光谱相乘。多腔沿 z 顺序展开为：端镜—腔0—耦合镜0—腔1—…—端镜。各腔 d 或 n d/λ 独立；半波且镜像互逆的理想单腔在对称无损端口中心 T=1，不意味着任意多腔自动平顶。

啁啾 t_j=j/(N−1)，λ_j=(1−t)λ_a+tλ_b 或 λ_j=exp[(1−t)ln λ_a+t ln λ_b]；r_j=(1−t)r_a+t r_b，再用局部 Bragg 公式展开每对。零中心变化和零比例变化严格退化周期堆栈。这个生成器提供结构自由度，不是 1997 论文完整解析双啁啾综合器或相位优化器。

数字 rugate 明确分三层含义：

1. 连续数学目标：n_t(z)=mean+amplitude·a(z)·sin(2πz/P+φ)，a=1 或 sin²(πz/Z)。只定义法向、参考波长上的标量实部目标。
2. 数值离散：每周期 M 个 midpoint cell，Δz=P/M。这个采样数是剖面构造分辨率，不自动等于工艺最小厚度。
3. 真实数字层：f_H=(n_t²−n_L²)/(n_H²−n_L²)，每 cell 输出 H(fΔz/2)、L((1−f)Δz)、H(fΔz/2)，零厚片省略。**不**新建 n_t 或“混合材料”记录，不线性插值混合 n/k；真实层保留绑定的 H/L 数据集。

上述实介电常数填比是法向、深亚波长 cell、弱损耗条件下的近似；细层周期产生各向异性，不能宣称所有角度/偏振、全谱、吸收混合或有限分片都与数学剖面等价。调幅0时连续目标退化均匀 n，但有限数字 H/L 仍是层序，只在加密薄cell极限趋于该均匀层。导出的片层可极薄，未验证沉积连续性、有效介质域、最小厚度、粗糙度、应力、互扩散与工艺可制造性。

## 三种目标必须分别调用

- `dual_window_ar_objectives(max_reflectance=..., samples_per_window=41, ...)`：3–5、8–12 μm 两个 R 上限，间隙不加目标；不能代替双通带含阻带。
- `dual_passband_objectives(min_transmittance=..., max_stop_transmittance=..., stopbands_nm=..., samples_per_band=41, ...)`：相同两窗的 T 下限及用户显式阻带 T 上限。阻带须有序且与通带完全分离，包含端点的重叠拒绝；间隙与两侧阻断的边界及过渡宽度由用户明确。不会默认把两个不同中心带通串联当成双通带。
- `six_line_ar_objectives(max_reflectance=..., wavelengths_nm=(532,1064,2100,3900,4800,9500), ...)`：六个独立单波长 R 上限，无点间连续范围保证。

它们共用现有 `FilmOptimizationObjective`，角度/偏振按现有契约传入，不更改求解器指标。当前只支持 R/T 验收；phase、GD、GDD 明确 unsupported。生成可用于相位探索的结构不表示已支持相位验收。

## 来源与证据强度

研究依据固定在 [ii33/coating-research-library@01980d6e6a9b57d5de2c5aa989394d0b368a67c6](https://github.com/ii33/coating-research-library/tree/01980d6e6a9b57d5de2c5aa989394d0b368a67c6)，只读核对 [经典机制研究](https://github.com/ii33/coating-research-library/blob/01980d6e6a9b57d5de2c5aa989394d0b368a67c6/manual_studies/2026-10-19_multispectral_ir_structures/research.md)、[近期证据边界](https://github.com/ii33/coating-research-library/blob/01980d6e6a9b57d5de2c5aa989394d0b368a67c6/manual_studies/2026-10-19_recent_frontiers/README.md)及[六线全文核查](https://github.com/ii33/coating-research-library/blob/01980d6e6a9b57d5de2c5aa989394d0b368a67c6/manual_studies/2026-10-19_fulltext_followup/six-spectral-review.md)。没有下载作者程序、模型权重或复制论文处方。

机制参考链接：

- 多谱区组合：[Li / Dobrowolski 1993](https://doi.org/10.1364/AO.32.002969)。这里实现一般显式段组合，未声称实现该论文全部等效层综合方法。
- 多腔：[Baumeister 2001](https://doi.org/10.1364/OE.9.000652)。这里验证无损解析目标，不声称复现 21 腔 482 层或实测 IR 器件。
- 双啁啾概念：[1997 原文](https://doi.org/10.1364/OL.22.000831)。只借鉴周期和耦合独立控制的机制；当前未验相位。
- 数字 H/L：[Southwell 1985](https://doi.org/10.1364/AO.24.000457)；渐消机制：[Southwell 1989](https://doi.org/10.1364/AO.28.005091)。本文的 midpoint、对称cell与默认 Hann 形式属本项目约定，未找到其特定组合的外部依据，仅为本项目实现。
- 六离散线证据：[Zhang 2019](https://doi.org/10.1016/j.surfcoat.2018.12.049)。全文没有完整29层厚度及 n/k，不能称为其精确复现；本模块六线波长是目标示例，并不继承该双面器件的实测性能。

## 验证与边界

独立 `coherent_rta` 前向验证包括：单堆栈中心导纳解析式；多堆栈／匹配参数；对称单半波腔中心 T=1 与邻域抑制；奇数耦合镜双腔分裂峰及独立阻断；线性/log局部Bragg序列；零啁啾退化；数字 rugate 对4096片连续标量oracle的8/16/32分片加密收敛；零调幅对均匀 slab 的收敛；真实 H/L 导出、确定性回放、未知k与独立k域拒绝；三种目标分别命名且点／带／阻带不混淆。测试材料为明确标注的合成常数，不作为真实 IR 材料验证。

边界：不保证平顶、低纹波、连续全谱收敛、相位镜性能或可制造性；未实现非平面/各向异性材料、温变/相变、金属岛状生长或神经逆设计；不宣称文献最终膜系的精确复现。真实器件仍需完整谱域 n/k、工艺和温度、基片厚度与背面、角度偏振、相干及制造误差验证。

## 现有参数侧栏接线（2026-10-20）

当前GUI在既有参数生成器加入多堆/匹配、多腔/独立耦合镜、啁啾、数字rugate与组合段表单；`coat_sim/core/film_physical_generator.py`是参数目录与类型转换适配，实际物理展开仍只调用本契约的核心。角色绑定使用当前项目材料服务，结果保持基片→入射侧逐层nm次序。预览沿用`FilmDesignEditSession`候选与provider/revision/epoch守卫，确认替换为未保存草稿及一次撤销，不自动优化或保存。未知k、域不足、不支持的设计环境显式拒绝并清除旧预览。具体接口所有权见[GUI契约](film-synthesis-gui-contract.md)，用户入口见[GUI指南](film-synthesis-gui-guide.md)。该接线没有实现相位/GD/GDD新求解器，也不把Ge研究邻域列为产品模式。
