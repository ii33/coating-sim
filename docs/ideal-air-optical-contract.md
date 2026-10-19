# 理想 Air 光学数据契约

更新：2026-10-19
范围：内置光学占位数据集 `builtin_ideal_air` / `ideal_air_n1_k0`

## 用途与身份

`builtin_ideal_air` 是材料库中的内置光学介质占位记录，`roles` 为 `["medium"]`，`optical_only` 为 `true`。它不是研究材料目录中的材料条目，不含 `research_data`，也没有电阻舟或电子束蒸发源模型；代码把两个工艺源模型保留为 `null`。因此该记录只表达 Film Design 所需的光学边界近似，不提供实体基片、蒸发源或工艺性能参数。

其光学数据集稳定 ID 为 `ideal_air_n1_k0`，类型为 `tabulated`，材料目录分类为 `other`。Film Quick 在内置 Air 记录和该数据集均存在时，将它作为新设计入射与出射介质的默认绑定；picker 及参考配方继续按材料 ID 与其所属数据集 ID 解析，不通过材料名或特殊合成数据绕过归属检查。相关行为见 [`film-design-quick-migration.md`](film-design-quick-migration.md) 与 `coat_sim/core/film_design_session.py`、`coat_sim/core/film_design_reference_recipes.py`。

### 项目起始默认与全局 Air 的关系

新建项目膜系草稿的入射/出射介质默认绑定来自**项目起始默认（packaged project starter）**，而不是用户可变的全局材料库。该默认由 `coat_sim/core/project_material_defaults.py` 从只读打包种子（`builtin_material_profiles` 生成的 `builtin_ideal_air` / `ideal_air_n1_k0`）读取，与全局库无关：

- 全局库为空、被覆盖或被墓碑删除时，新项目/新膜系仍绑定 n=1、k=0 的打包种子 Air，不会因此缺失或改变数值。
- 项目默认优先于全局库；全局被修改的 Air 不会降低项目默认的 n/k。
- 该起始默认只存在于项目内存视图；它**不写入全局材料库**，因此不会复活被全局删除的 Air，也不会覆盖用户对全局 Air 的编辑。
- 首次保存膜系时，Air 作为被测设计显式引用的材料被捕获进项目自身的 `optical_material` 文档；此后项目在全局库不可用时仍能读取自己的闭包。

全局材料库中的 `builtin_ideal_air` 仍是可编辑/可恢复的普通内置记录（见下文“实现与数据边界”）；上述起始默认只改变新项目草稿的取值来源，不改变该内置记录本身的身份或分类。

## 模型、单位与求值域

本项目定义的数学工程模型为：

\[
N(\lambda)=n(\lambda)+i k(\lambda)=1+0i
\]

其中折射率实部 `n=1`、消光系数 `k=0` 均无量纲；没有几何坐标或方向参数。目录以波长单位 `µm` 储存两端点 `0.001` 与 `1000`，两端的 n/k 值相同，沿用通用表格线性插值路径表示恒定模型。查询 API 接受显式的 `nm` 或 `um` 单位；例如 550 nm 查询为 0.55 µm。

`0.001–1000 µm`（1 nm–1 mm）只是本项目选择的软件序列化及求值域，不是实测空气的有效波段，也不构成该波段内真实空气精度的声明。直接求值默认拒绝超出数据集有效域的波长；不会因模型平坦而自动外推。若调用者显式采用通用端点策略，那是调用策略，不扩展本模型的物理适用范围。

## 来源与适用边界

这是本项目定义的无色散、无吸收工程近似；未找到外部依据，仅为本项目实现。它不是实测空气、厂商数据、论文数据或 refractiveindex.info（RII）数据。它不描述压力、温度、湿度、空气色散或吸收，也不推断未知 k。理想模型中的 `k=0` 是该模型的明示值，不会改写其他材料数据中未知的 `k=null`。

`data/optical_catalog.json` 的根级来源和 CC0-1.0 说明对应已有的 RII 来源批次；混合目录中的 `project_ideal_air_20261012_v1` 与 `ideal_air_n1_k0` 使用独立的项目来源说明，不继承 RII 的来源身份或许可说明。目录条目中的端点也不是测量样本。

## 实现与数据边界

- `data/optical_catalog.json` 保存 Air 数据条目与模型来源说明。
- `coat_sim/core/materials.py::builtin_material_profiles` 将其作为 `builtin_ideal_air` 的一个普通光学常数集接入；`_optical_catalog_by_builtin_id` 校验内置材料 ID、角色分类与数据集。
- `coat_sim/core/optical_constants.py::validate_optical_set`、`evaluate_optical_set` 和 `prepare_optical_set` 使用通用表格验证、线性插值和有效域检查。
- `coat_sim/core/film_materials.py::FilmMaterialLibrary.evaluate` 从所选材料自身的 `optical_sets` 中查找所选数据集，再按 nm 求值；未知或不属于该材料的数据集 ID 会报错。
- `coat_sim/core/project_material_defaults.py` 从打包只读种子向每个项目提供起始默认（理想 Air），`coat_sim/core/project_film_service.py::ProjectFilmService` 以“项目记录 > 打包起始默认 > 全局目录”的优先级合成，并在保存膜系时把被引用材料捕获为项目 `optical_material`。该路径不写全局库。
- `coat_sim/core/material_business.py::MaterialLibrary` 的复制、内置覆盖与恢复仍使用通用材料 schema。复制会生成用户记录并保留其数据快照；覆盖写入 `.overrides/<material-id>.json`；恢复移除对应覆盖并返回打包的内置原件。没有为 Air 增加 picker、导入或存储豁免。

以上是软件数据契约，不是对实际空气光学性质的验证。算法文档中的对应条目及示意图见 [`algorithms.html` 的 14-1 节](algorithms.html#film-ideal-air-model)。

## 定向验证

`tests/test_ideal_air_optical_contract.py` 对内置记录身份、模型字段、来源边界、端点/域外求值、材料与数据集归属，以及普通复制/覆盖/恢复路径做离线契约检查。`tests/test_project_film_air_defaults.py` 覆盖项目起始默认：空/被编辑/被删除的全局 Air 下新项目仍绑定打包 Air、全局不被复活、保存后项目自持 n=[1,1]/k=[0,0] 闭包、无全局目录下重开可读。该测试验证目录和通用代码路径，不构成真实空气的实验校准。
