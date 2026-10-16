# 项目数据目录

看板娘的文件读写与脚本运行以本目录为授权边界。目录联接或符号链接不能用于访问项目外部路径。

- `sources/`：蒸发源设计。
- `substrates/`：基片设计。
- `masks/`：修正板设计。
- `machines/`：机器结构。
- `processes/`：工艺方案。
- `results/`：导出结果；`results/optimizations/` 保存优化候选及完整依赖文件。
- `attachments/`：通过对话文件选择器显式导入的外部附件副本。

所有设计文件可包含可选的 `description`（描述）与 `notes`（备注），各自最多 20000 字，不参与物理计算。编辑器新文件默认保存在对应子目录；桌面文件选择器仍允许用户明确选择任意目录。已有设计不会自动搬迁。

外部文件需经用户明确选择后导入；看板娘不能利用引用或脚本绕过此目录边界。

## 光学常数参考库

`optical_catalog.json` 是随项目提供的文献参考光学常数目录，由内置材料的精确 ID 与用途类别关联。程序只把各条目的 `optical_constants` 追加到对应的只读内置材料，不修改用户材料文件、手工常数、默认源模型或研究数据快照；重复导入相同常数组会跳过，遇到同 ID 内容冲突会停止并保留现有内容。

来源条件与逐组出处保存在 `optical_catalog_sources/sourced_optical_constants_20261006_v1/`，包括原始 README、完整 provenance、来源审计和验证摘要。Refractiveindex.info 数据库固定于 commit `c5c2f188e848453def5970e347399d653df2ffc2`，按 CC0-1.0 提供；许可文本随目录保存在 `refractiveindex_LICENSE`。这批数据是文献/作者参考值，不代表本机设备校准，也不会自动设为生产默认值。
