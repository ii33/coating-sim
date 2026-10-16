# 材料光学常数导入包（2026-10-06，v1）

本包纳入15个材料角色、23个常数组：16个薄膜组、7个基片组。所有数值来自refractiveindex.info官方仓库或NIST原论文数值表；没有从资料概览页拼造测量谱，也没有写入产品文件。

## 入口

- `材料光学常数目录.md`：中文清单、参考镀膜条件、有效波段和每组完整来源。
- `data/optical_catalog.json`：机器导入主入口，按精确材料角色路由。仅将每项的`optical_constants`写入对应材料。
- `data/provenance.json`：每个常数组对应的结构化条件、原始来源、派生方式、许可与哈希。
- `data/ri_*.json`：当前产品`validate_optical_set`可直接接受的单组JSON。
- `csv/ri_*.csv`：严格两列或三列CSV；单位µm。CSV导入时应同时保留对应JSON的短名、notes与source。
- `raw/`：本轮实际纳入的原始数值文件。YAML均按固定官方仓库commit获取。
- `quarantine/not_imported/`：研究时取得但明确未导入的候选，不能批量导入。
- `verification/`：当前产品schema、材料角色、域拒绝、单位、unknown k、CSV往返和公式采样数值检查结果。

## 产品接入契约

1. 主入口包含`schema_version/dataset_id/materials`。每个`materials`项有`material_id`（研究材料精确ID）、`builtin_material_id`、`category`、`optical_constants`。
2. 精确匹配材料ID和角色，禁止按同化学式合并膜材与基片。ZnS薄膜目前的内置ID为`builtin_zns_literature_20261004`，其余已提供完整内置ID。
3. 只追加这些光学组；不要覆盖用户手工组、用户材料文件、材料源模型、原有`research_data`快照或其他物性。
4. 常数组`notes`、`source`不是可选装饰。它们包含薄膜/块材、实际工况、未知项、来源、许可证、有效域限制与不确定度边界，导入时须保留。
5. 所有查询先检查`wavelength_min/max`。JSON保留完整原表时，原表的支持节点可能超出本轮计算域；它们不能扩大计算域。CSV已裁为有效域并按原表内插端点。
6. 7组仅n数据保持k未知（表格为null；解析模型不含k字段）。如果某计算器需要完整n/k，应明确请求所需吸收假设或拒绝该计算，不允许导入时默默补零。
7. 同源n/k范围不同者分别提供“仅n”和“n-k交集”组。两组的边界按provenance保存，不跨来源或工艺拼接。
8. 数据是文献/作者参考，非本机校准；不要自动启用某组为默认生产材料常数。
9. 建议产品算法文档说明：来源式1的C是共振波长平方、式2的C已是µm²；非单位偏移式以有界采样导入，禁止硬套为偏移为1的三项Sellmeier。

## 关键审核结果

- Ge/Burnett官方YAML标为2–14µm、温度295K，但原论文Eq.2/Table3拟合的是空气波长与相对空气n，有效域2.25–14µm。该YAML未导入。已改用原文Table1的17个λvac/nvac测点，22.000°C。测点的标准不确定度仅适用于原节点；当前产品线性插值的误差并未由此得到保证。
- Amotchkina的作者表/公式延伸到14或15µm，论文表征窗为0.4–12µm；本包保守把本轮计算上限限制为12µm（本身只到11µm的Ge基片仍为11µm）。
- ZnS/Amotchkina的k只提供0.4–1.0µm；ZnSe为0.4–0.888µm；YbF3实际表节点为9.0168–13.975µm，本包交集止于12µm。不能把末点k=0扩展到无数据区域。
- Al2O3/Rodriguez de Marcos原YAML书目把2025文献误写成2016/错误卷刊，并含远超实测58.4nm–43µm范围的拟合延伸。整组暂不导入，以ALD120°C/20nm和室温溅射组替代，不修改原始档案。
- Malitson仅保留原1965论文0.21–3.71µm区间，不把后续6.7µm验证合并为“1965原始测量”。原YAML页码1205–1208，出版者列1205–1209；provenance已注明此书目差异。
- K9/H-K9L/N-BK7没有直接互换；本轮未给K9强配N-BK7。H4商品配方与ITO掺杂/制膜条件未形成足够核定数据，本轮保留缺口。
- 来源没明确空气/真空参照的组，按来源约定留存并标注未知，不能冒称高精度绝对n标定。Burnett特例已核真空基准。

## 可重现验证

运行：

`python validate_package.py /absolute/path/to/deposition_sim`

验证器只读产品并输出本包的验证报告，不修改产品。标准库可完成产品schema与CSV等检查；安装PyYAML时还验证原表逐值同一性。

重建：

`python build_package.py`

重建需要PyYAML，读取本包固定原件，输出JSON、CSV和中文目录；不访问网络，不修改产品。非原生解析式采用有界自适应采样，在每个输出小区间的31个独立分点验证n的线性插值数值误差低于1e-7。该检查不是连续全域误差的数学证明，更不是材料测量不确定度。

当前已通过23组严格光学schema、15个目标材料profile、23个CSV往返、nm/µm端点等价、域外拒绝、7组unknown k、所有原表逐值同一性，以及6组派生表的采样精度检查。未在本研究包内执行GUI或产品持久化集成测试。

## 来源与许可

官方仓库：https://github.com/polyanskiy/refractiveindex.info-database

固定commit：c5c2f188e848453def5970e347399d653df2ffc2（2026-09-04）。数据库按CC0 1.0提供；完整许可证随包保存于`sources/refractiveindex_LICENSE`。数据库说明和各组原论文归属分别保留。CC0不自动涵盖原论文全文和图像，本包不再分发那些图文。

数据库引用：M. N. Polyanskiy, Refractiveindex.info database of optical constants, Scientific Data 11, 94 (2024). https://doi.org/10.1038/s41597-023-02898-2

NIST原表另列为直接论文数值事实转录，非伪称来自CC0数据库；原文链接、页表定位和原节点标准不确定度见`raw/Ge_Burnett2016_Table1_vacuum.csv`与provenance。

检索限制：部分出版者全文受订阅限制，部分作者机构PDF抓取超时；只采用可核的官方数值文件、作者说明、可见原论文/官方表格或来源明确元数据。未推测不可见的工况，未绕过访问限制。
