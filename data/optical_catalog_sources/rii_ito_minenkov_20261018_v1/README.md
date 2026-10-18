# ITO Minenkov 2024 条件化光学常数

本批只向`builtin_research_ITO_film`追加`ri_ito_minenkov_glass_2024`一组可选择的光学常数，不改其他材料、研究物性快照、源模型或默认工艺。

## 样品和数据

- 样品：110nm ITO / XY15S玻璃，Xinyan Technology LTD商用样品。
- 数据：706个tabulated n/k配对点，原表0.19146–1.68869µm；逐个十进制值保留，不补点、不平滑、不外推。
- n/k由椭偏和透射联合光学模型拟合得到，不是逐点直接测量。模型/样品依赖性保留，不作通用ITO生产默认。
- 110nm是本样品光学模型中的膜厚，不应与供应商155±20nm的标称规格混用。
- 此商用样品的沉积温度、气氛、组成比例未核定；不借用同论文自制Si晶圆样品的溅射条件。
- 声明表域来自官方数值表。完整独立实测/拟合有效波段未核定，`provenance.json`保留null；不以该表域冒称全段实测，不把论文operando液体池的窗口套到本样品表。

原论文：Minenkov et al., *Monitoring the Electrochemical Failure of Indium Tin Oxide Electrodes via Operando Ellipsometry*, ACS Applied Materials & Interfaces 16, 9517–9531 (2024)，[DOI 10.1021/acsami.3c17923](https://pubs.acs.org/doi/10.1021/acsami.3c17923)。官方数据库注明数值由作者Manuela Schiek提供。

## 固定来源和许可

上游：`polyanskiy/refractiveindex.info-database`，固定commit `c5c2f188e848453def5970e347399d653df2ffc2`，与既有RII批次相同。

- [原始YAML](https://raw.githubusercontent.com/polyanskiy/refractiveindex.info-database/c5c2f188e848453def5970e347399d653df2ffc2/database/data/other/mixed%20crystals/In2O3-SnO2/nk/Minenkov-glass.yml)
- [固定许可](https://raw.githubusercontent.com/polyanskiy/refractiveindex.info-database/c5c2f188e848453def5970e347399d653df2ffc2/LICENSE)
- [数据库样品页](https://refractiveindex.info/?shelf=other&book=In2O3-SnO2&page=Minenkov-glass)

YAML文件头和随附许可均明确CC0-1.0，可分发该数据库数值记录。许可不自动覆盖原论文PDF或图片；本批不分发论文图文。

`raw/Minenkov-glass.yml`保留原始23346字节，SHA256为`2861e8774e0d8a95f485ea56721a70d239bf43ff7e11f48c1f60b12a551fad78`。`refractiveindex_LICENSE`保留原始6555字节，SHA256为`36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673`。原始文件没有从网页摘录重建。

## 文件与验证

- `package.json`：现有`tools/import_optical_catalog.py`接受的单组追加包。
- `provenance.json`：固定来源、哈希、原始说明/条件、单位、范围、未知项、许可和变换边界。
- `import_record.json`：追加前后数量，以及296fe33中未改动材料行/根元数据的规范JSON摘要。
- `raw/Minenkov-glass.yml`、`refractiveindex_LICENSE`：原始来源字节。

已有导入器的预览：`python -m tools.import_optical_catalog data/optical_catalog_sources/rii_ito_minenkov_20261018_v1/package.json`。当前目录已包含同组，重放应为幂等no-op；没有本批再导入其他候选的行为。

专门测试`tests/test_ito_optical_reference.py`核对原始字节、706行逐值一致、schema、有效域拒绝、精确材料/角色、来源条件、既有数据未变及幂等性。测试不访问网络、不运行大型仿真。

## 未补齐的部分

ZnS薄膜380–400nm仍无本轮可验收数据，不外推Amotchkina，不拼接Querry晶体或Ozaki模型。H4商用混合物和K9仍不作材料替代。上述缺口由材料就绪诊断显式返回。
