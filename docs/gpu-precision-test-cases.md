# GPU / FP32 膜系精度验收用例

本文是候选 GPU/FP32 光学内核的测试计划。比较基准为 `coat_sim/core/film_optics.py` 的 float64 / complex128 被动散射递推；它保留吸收、evanescent exit 和内部临界层解析极限。不要把图像渲染载体的 FP32 和膜系求解精度混为一谈。

## 当前生产路径与既有门限

- `coat_sim/core/film_optics_gpu.py` 目前是 **FP64/complex128** PyTorch GPU 实现，不是 FP32 膜系核。`verify_gpu_dtype()` 用复数指数探针，CPU 对照 `rtol=atol=1e-13`；栈级 `TorchCoherentBatch.verify()` 的当前门限为 `rtol=atol=2e-12`。
- GPU 现行支持域策略：精确内部 `q=0` 回 CPU；若任何介质满足 `|q| < 1e-10 * max(1, |n|)`，也回 CPU。这个 FP64 策略不能原样当作 FP32 的临界保护阈值。
- `coat_sim/appearance/render.py` 的 Mitsuba `scalar_rgb` 和输出数组是 float32，但 `_rta()` 每个表面命中仍调用 `coherent_rta()` CPU 光学解算。局部方向分量已是 float32；之后 `acos` / 角度转弧度在 Python float 中计算。因此仍有 **输入角度被 FP32 方向量化** 的边界风险。
- 渲染 BSDF 当前要求每个命中 `R/T/A` 在容许的被动范围内，并且 `|R+T+A-1| <= 2e-7`；被动值范围检查为最小值不得低于 `-1e-9`、最大值不得高于 `1+1e-8`。
- 现有 CPU 回归很紧：独立矩阵对照典型断言 `2e-12`；批量/标量一致通常 `4e-14` 到 `1e-12`；GPU 栈级 `2e-12`。这些是当前实现的生产/回归门限，不能据此宣称未来 FP32 已达标。

## 小而有覆盖的验收矩阵

| ID | 输入与覆盖 | 可直接复用的测试/数据 | 必须检查 |
|---|---|---|---|
| Z0 空膜系、双向与偏振 | 0 内层；Air↔n=1.5；正入射；各测 s、p、unpolarized；方向互换 | `tests/test_film_optics.py::test_bare_fresnel_and_absorbing_exit_definition`，`coherent_rta(550, [1, 1.5], [], ...)` | Fresnel R/T；A=0；`unpolarized=(s+p)/2`。反向计算注意端口顺序及同一 beta 的定义。 |
| Z1 真实材料 60 层 | 30 组交替 ZnS/YbF₃；lambda=550 nm；每层 `d=lambda/(4*n(lambda))`；使用 `data/optical_catalog.json` 的 `ri_zns_amotchkina_n` 和 `ri_ybf3_amotchkina_n`，按既有材料评估器取值；**仅为透明夹具而显式选择 `use_k=False`**；全 s/p/unpolarized，正入射和中等斜入射；也测反向 | 两个材料 ID：`builtin_zns_literature_20261004`、`builtin_research_YbF3_film`；配方角色绑定例子见 `tests/test_film_design_reference_bridge.py::test_real_qoq_mapping_is_private_until_apply_then_undo_restores_owner`。60层批量基线可复用 `tests/test_film_optics.py::test_wavelength_batch_matches_scalar_for_30_60_120_layer_stacks` | 60 层不能只测正入射或单一厚度；正反序要反转层序和厚度；报告每个偏振误差、全谱最大误差及共振/极值位置。n-only 且 `use_k=False` 是显式本次假设，不代表来源测得 k=0。 |
| Z2 吸收与极端厚度 | 现成中等吸收角度用例及厚吸收体；s/p/unpolarized；前后入射分别检查；吸收层与吸收出口定义分开 | `tests/test_film_optics.py::test_absorbing_45_degree_oracle`（[1, 2.1+0.08j, 1.52]、100 nm、45°）；`test_very_thick_absorber_has_no_overflow`；`tests/test_film_optics_batch.py::test_absorbing_extreme_absorption_and_absorbing_exit` | A 必须真实为正且不被归一化抹掉；厚吸收下不溢出；区分“有限膜层 A”和“进入吸收半空间的 T”。对比全部原始输出，不要只验总和。 |
| Z3 反向传播/顺序 | 60层栈 `n,d` 同时反序，分别让两侧作入射端；用共同实 beta 比较互易关系；另覆盖项目渲染中基片向外层序接口 | `tests/test_film_optics_batch.py::test_scalar_matches_single_wave_batch_for_sixty_layer_exact_angle_cases` 的 `ordinary/reversed_incidence`、厚度反序；`render.coherent_stack_rta()` | 端口和膜层顺序不可混用；同一角度在不同入射介质下通常不是同一 beta。对同一 beta 的可传播端口检查功率通量归一后的透射互易关系；R 允许因非对称栈而随方向变化。 |
| Z4 内层临界、TIR 与邻域 | 直接 beta 夹具 `[1.5, 1.0, 1.5]`，厚度 0/100/270 nm；beta=`nextafter(1,0)`, `1`, `nextafter(1,+inf)`；另以 1.5→1.0 端口角度扫过临界角；全偏振 | `tests/test_film_optics.py::test_exact_critical_internal_layer_analytic_limit`；`tests/test_film_optics_batch.py::test_critical_q_zero_and_evanescent_exit_fallback`、`test_scalar_matches_single_wave_batch_for_sixty_layer_exact_angle_cases` 中 critical/TIR 栈 | 临界 q=0 解析极限有限；TIR 端口 `T=0`、`R=1`、无负 A；临界上下两侧分类正确。覆盖内部临界层与出口 evanescent 两种不同情况。FP32 实现须有经验证的宽保护带/FP64 回退，不能照抄现行 FP64 `1e-10` q 阈值。 |
| Z5 float32 角度量化和 grazing | n=1.5→1.0 的裸界面，`theta_c=asin(1/1.5)=41.810314895778596°`；把 theta 存为 float32，测试两个邻接 float32 角点；再测 89.999° grazing（渲染上限） | `tests/test_film_optics.py::test_brewster_tir_and_unpolarized_average` 已覆盖 Brewster 和 60° TIR；`render._snell_transmitted_cosine()` 的 `8*eps64` 传播阈值 | 数值实测：`float32(theta_c)=41.8103141784668°`，紧邻上点 `41.81031799316406°`。对 s 偏振，前一点 CPU T≈`5.9843e-4`，后一点严格 T=0。必须分别记录原始 double 角、实际 FP32 方向分量/重建角、量化后的 CPU 参考及 GPU 输出；不允许只以量化后输入的 CPU 对照掩盖物理输入变化。grazing 下 R/T/A 仍有限且守恒。 |
| Z6 窄共振与 Brewster 极小值 | 对 Z1 60层膜系加一个明确的相位缺陷/腔层厚度，或从候选设计中挑一个窄带共振；以 double 密集扫描先定位共振中心与 FWHM，再比较中心及半高两侧的邻近 float32 角度；另对 `[1,1.5]` 的 p 偏振 Brewster 角及 float32 邻点测试 | 参考做法可沿用 `tests/test_film_optics.py::test_quarter_wave_ar_hr`、`test_brewster_tir_and_unpolarized_average`；并复用 Z1 的真实材料评估 | 报告峰/谷高度、中心波长/角度偏移（用 FWHM 归一）、半高宽变化、全谱 `max_abs_error`；不能只测粗网格点或峰外的两三个厚度。零点附近以绝对误差计，不用相对误差放大舍入噪声。 |

建议把矩阵拆成 CI 的快速固定集（Z0、Z2、Z4、Z5）与较重的 60 层/谱扫描集（Z1、Z3、Z6）；每项仍要覆盖 s、p、unpolarized。`unpolarized` 必须与同输入下 `(s+p)/2` 的功率分数逐项一致，不能平均复振幅。

## 输入量化与计算误差要分账

每个角度边界用例至少保存三种计算：

1. FP64 reference 对原始 double 输入：测量 FP32 输入量化造成的端到端变化。
2. FP64 reference 对**已量化成 FP32 再升回 float64** 的相同输入：隔离输入量化后的内核算术误差。
3. GPU 候选在与第2项完全相同的输入：与第2项比较得到解算误差。

临界角用浮点 `nextafter` 构造两侧，而非仅写十进制 `theta_c±1e-6`。内部层临界另以 beta 直接构造，避免 angle→sin 转换混淆分支。窄共振以峰中心/半高宽作尺度；角度 ULP 远小于绘图采样步长时，仍要构造落在相邻 float32 量化值的输入。

## 汇报指标与拒收条件

每次测试保留未裁剪、未归一化的原始 GPU `R/T/A`，报告：

- 所有极化、方向、候选厚度和波长上的 `max_abs_error(R/T/A)`；对不接近零的通道再报告 `max_relative_error`。近零 R/T/A 只用绝对误差。
- `max |R+T+A-1|`、各通道最小值/最大值、NaN/Inf 数量；分别报告能量闭合和对 CPU reference 的误差。
- 临界/TIR 的 q 分支、传输硬零断言、临界两侧分类；窄峰中心偏移/FWHM/高度变化；反向透射互易检查。
- 输入量化误差与 FP32 运算误差分开报告；任何角度/波长/厚度输入的舍入都不能被算子误差指标隐去。

拒收：发生临界/TIR 侧错误；漏掉/明显移动窄共振；R/T/A 非有限或越界；正吸收被裁成 0；仅靠裁剪/归一化通过功率守恒；用单个绝对/相对误差或 `R+T+A` 代替全部物理分支检查。现有仓库没有已批准的 FP32 膜系误差预算，因此不能把新阈值伪称生产容差；先在 GPU/FP32 候选和实际材料上跑完上述集，再由产品精度需求确定各指标数值门限。

## 容易“错误通过”的现有检查

- `A` 由 `1-R-T` 计算时，`R+T+A≈1` 基本是代数恒等式；单独通过 balance 不证明 R/T 算对。若先 clamp A/R/T 再检查 balance，还可能把越界或负吸收藏掉。
- `TorchCoherentBatch.verify()` 只检查当前候选中的前两行厚度和一行全零厚度；这是运行时轻量探针，不覆盖临界、反向、材料损耗和窄共振，不能替代 Z0–Z6 离线回归。
- 只测 unpolarized 可能掩盖 s/p 单通道错误；只测正向可能掩盖层序反转；只检查输出总和可能掩盖错误的端口 flux 归一化。
- 当前测试中的 60 层随机 stack 是很好的稳定性/回归输入，但不是 ZnS/YbF₃ 的真实数据验收；真实材料测试须绑定数据集 ID、波长单位、k 策略和层序，避免把样例常数误作已验证材料常数。
