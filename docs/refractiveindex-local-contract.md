# 本地 RefractiveIndex.info 参考数据库契约

实现真值：`coat_sim/core/refractiveindex_local.py`。此库是独立、版本固定的本地文献参考数据，不是全局材料库或工程材料库。搜索、详情、预览和转换均不写材料，不联网，不启动下载。

## 状态所有者与目录

Current selection (2026-10-19): `default_store_root()` resolves the trusted
process override first, then an existing user installation, then the fixed
read-only shipped snapshot at `core/resources/refractiveindex`. No lookup creates
directories, writes indexes or downloads anything. An explicit missing or damaged
override does not silently fall back. The original Windows LOCALAPPDATA / POSIX
XDG_DATA_HOME fallback remains the writable user-store location.

`writable_store_root()` keeps explicit CLI build output in the user store;
`store_root_for_snapshot(snapshot_id)` preserves exact packaged IDs after a later
user installation. The model has no root argument. Physical snapshot storage is
internal to the [unified assistant library](unified-offline-library-contract.md),
with no independent database page or extra path setup. The existing CC0 snapshot
was copied byte-for-byte, preserving hashes, rights and operator-declared
upstream identity. Existing user installations and research are retained.

Internal storage structure:

```
<store_root>/CURRENT.json
<store_root>/versions/<snapshot_id>/manifest.json
<store_root>/versions/<snapshot_id>/index.sqlite3
<store_root>/versions/<snapshot_id>/source/LICENSE
<store_root>/versions/<snapshot_id>/source/README.md
<store_root>/versions/<snapshot_id>/source/database/...
```

`RefractiveIndexLocalStore(store_root, snapshot_id=None)` 在构造时固定一个快照。未指定版本时读取当时 CURRENT；以后 CURRENT 更新不改变旧 reader。版本格式为 `40位上游SHA-16位源内容摘要-v1`。没有 CURRENT 时只返回 missing 状态，不创建目录、SQLite 文件或默认库。显式指定不存在/损坏的版本报错，不能自动退回其它版本。

## 显式构建与原子激活

```python
build_from_directory(source_dir, store_root, *, upstream_commit,
                     source_url=None, activate=True, cancel_check=None)
```

1. 输入为已下载的本地官方格式快照；`upstream_commit` 是操作者显式声明的40位小写 Git SHA。可选 source_url 仅接受对应官方固定提交 tree URL。
2. 只复制 LICENSE、可选 README.md 和 database 子树。上游脚本、测试、工作流、Git 元数据不复制、不执行。LICENSE 须以 CC0 1.0 Universal 开始。
3. 源与 store 不得重叠；拒绝符号链接、非普通文件和 catalog 路径穿越。store 的 versions/CURRENT 也不接受符号链接。
4. 在 store/versions 内构建 staging，逐文件计算 SHA-256，保留原始字节；使用 SQLite 建立独立索引。实际读取累计大小、单文件大小和文件数都受限，不只依赖复制前 stat。
5. 先完成分类引用完整性、SQLite integrity、manifest 全字段/计数/摘要和可读状态验证，再把 staging 原子 rename 为不可变版本；最后原子替换 CURRENT。旧版本不删除。
6. 取消检查在复制、索引循环和激活前执行。回调返回 true 时抛 `CANCELLED`。取消/失败不改变旧 CURRENT；已完成但未激活的版本可以保留。`activate=False` 不改 CURRENT。
7. 重复构建相同源内容与提交可复用同一健康版本；已有版本损坏时明确失败，不覆盖它、不激活它。

构建返回有限 JSON：status() 内容加 `activated`。构建不自动下载、不配置凭据、不安装依赖。PyYAML 是正式依赖，缺失时报告 `DEPENDENCY_MISSING`。现有 `python -m coat_sim.refractiveindex_cli build` 是显式构建入口，避免另一套重复脚本。

“上游提交声明”与“本地内容校验”严格分开：manifest 的 `upstream_commit_verified` 恒为 false，`verification` 说明只做本地哈希检查。一个任意目录不能因操作者填写官方 SHA 就被标为已经网络核验。官方包下载及逐 Git blob 验证证据可以另行提供，不伪造签名或认证。

## 原始内容与索引边界

- 原始 YAML、目录、REFERENCES、COMMENTS、CONDITIONS、PROPERTIES、about.yml、LICENSE 均保留在 source 镜像。
- 可检索条目定义为 database/data 下拥有 DATA 的 YAML 文件。每个规范化源相对路径只建一条索引；未被 catalog 引用的 DATA 文件也可搜索，但 `cataloged=false`、classifications 为空。
- `entry_id = 'rii_' + SHA256(规范化相对路径UTF-8)[:24]`。它不是 shelf/book/page；官方目录确有重复导航元组。快照身份和源 SHA-256 分别返回，不能仅以 entry_id 判断来源版本。
- classifications 数组保留所有分类出现、顺序、DIVIDER、原始 data 路径。安全地规范化重复斜杠不修改保存的原 catalog 字节。
- 不把 about、工作流、分类文件数冒充光学数据集数。status 分别返回源镜像文件数、字节数、分类出现次数、索引条目数、nk/n2、cataloged/uncatalogued 与 conversion 状态计数。
- catalog-n2 是非线性折射率系数。RII 色散公式里的 n² 是线性折射率的平方，二者不是同一数据类型；n2 也不是消光系数 k。

## 只读 API

### status()

缺库：`{status:'missing', snapshot_id:null, counts:{}, read_only:true}`。

就绪：返回 `status:'ready'`、snapshot_id、upstream_commit、source_url、upstream_commit_verified、verification、license、counts、read_only。

counts 包含 source_files/source_bytes/catalog_occurrences/indexed_entries/nk/n2/cataloged/uncatalogued/conversion_status。

### search(query='', *, catalog='nk', shelf=None, book=None, capability=None, cataloged=None, offset=0, limit=50)

- catalog 为 nk/n2/all；capability 为 supported/selection_required/unsupported/invalid 或 None。
- query 最多200字符，空白分词后对本地名称、路径、分类、引用、注释、条件和物性作不区分大小写的包含匹配。所有 token 均须匹配；没有外部搜索、SQL 拼接值或正则执行。
- shelf/book 是官方分类标识，单项最多200字符；filter 组合在同一个分类出现中匹配。cataloged 是严格 bool 或 None。
- offset 为0–1,000,000；limit 为1–100。稳定按 source_path 排序，返回 snapshot_id/query/total/offset/limit/has_more/entries。
- entries 是去重源记录：entry_id/name/source_path/catalog/cataloged/classifications/data_types/conversion/source_sha256。

### get_entry(entry_id, *, raw_limit=20000)

返回搜索字段、固定来源链接、license、原始条件/物性、DATA 块摘要、原始 YAML 的限长纯文本。raw_limit 为0–100,000；raw_yaml 含 text/truncated/total_chars。references/comments 各最多20,000字符，并有各自 truncated 布尔标记。完整字节仍保留在 source 文件。

blocks 摘要包括 index/type/range_um/row_count/coefficients；非法或未知格式附 error。原 REFERENCES/COMMENTS 中的 HTML、Markdown、链接和命令形文字仅作为不可信资料，不执行。conditions 中的拼写错误不静默改正，不凭名称补温度、基底或沉积条件。

### preview(entry_id, *, block_indices=None, max_points=200)

max_points 为2–1,000；block_indices 为 None 或1–64个互异合法索引，空数组无效。

每个块单独返回 index/type/quantity/status/columns/points/source_point_count/display_point_count/sampled/calculated/range_um/error。未知类型可查详情但预览标 unsupported；格式或计算失败标 invalid。

- 表格仅按原始行顺序选择显示点：超过上限时取 `floor(i*(N-1)/(M-1))`，保留首尾；不排序、不平均、不清理重复点。该规则只是本项目显示抽样，不声称有保峰误差界。
- 公式在声明域内按线性波长显示网格调用现有 optical_constants 求值，标 calculated=true。数值为模型输出，不是新测量。
- n-only、k-only、n2 表可按原始数值预览。可展示不等于能转换成完整 nk；n2 数值单位保留源解释，不自动当作普通折射率。
- 预览抽样不得作为导入数据。转换总是重新读取哈希验证后的完整原始 DATA。

### convert_entry(entry_id, *, optical_set_id, name=None, block_selection=None)

只返回 `{optical_set, provenance, conditions, missing, assumptions, warnings, production_validation, untrusted_reference}`。不落盘、不创建材料、不更换用户默认光学组。

- provenance 含 snapshot_id/entry_id/source_sha256/source_path/upstream_commit/source_url/license/block_selection/excluded_blocks。
- block_selection 为 None 时使用全部 DATA 块；多个 n 表示必须明确选择。显式排除块会返回警告并在候选 notes 中保留排除索引。
- 原样支持 tabulated n、tabulated nk、formula1–9、公式+独立 k；同网格独立 n/k 表可精确合并，异网格不能自动重采样。公式系数波长约定固定 μm。
- 最终由 `validate_optical_set` 验证。单点 n、不递增/重复波长、负 n/k、不支持系数长度、域内极点和未知格式不能因为“官方库”身份跳过现有数值约束。
- 缺 k 始终保留 None，不自动填零，不把缺 k 隐藏在“可计算”标签下。n2/k-only 不伪装成完整 nk。多个来源不自动拼接、外推、裁域或修复。
- source/notes 保留固定来源、引用、条件和块选择；超过现有20,000字符字段限制时明确 `PROVENANCE_TOO_LARGE`，不静默丢证据。
- `production_validation='not_established'`：有光学 n/k 不代表具有可迁移沉积标定、实际膜密度或工程适用性。

## conversion 状态含义

- supported：默认全部块能无损投影并通过本项目光学数据输入验证。包括 n-only，不保证 k 已知、不保证生产有效性。
- selection_required：默认组合不能直接导入，但存在可以明确选择的单块投影。例如多个 n 表示或异网格 n/k 中单独选 n。选择导致丢弃其它块时必须可见。
- unsupported：没有当前可用投影，如非线性 n2、k-only、未知类型。
- invalid：当前光学契约拒绝该源数据的格式/数值，如重复波长或域内极点。原始记录仍留在参考库。

conversion 附 reason_codes/reason/choices；choices 记录 block_selection、has_n、has_k。条件适用性不由上述状态自动推断。

## 安全、错误和限制

`RiiLocalError(ValueError)` 具有稳定 code/details。常见 code：STORE_MISSING、INVALID_STORE、INVALID_QUERY、INVALID_ENTRY_ID、ENTRY_NOT_FOUND、SOURCE_INTEGRITY_ERROR、SOURCE_READ_FAILED、INVALID_YAML、UNSAFE_SOURCE_PATH、UNSAFE_STORE_PATH、SOURCE_LIMIT、BLOCK_SELECTION_REQUIRED、INDEPENDENT_TABLE_GRIDS、MISSING_N、NONLINEAR_N2_REFERENCE_ONLY、UNSUPPORTED_DATA_TYPE、OPTICAL_VALIDATION_FAILED、CANCELLED。

- 单文件16MiB，镜像实际总量256MiB，最多10,000文件；数值表最多100,000行，在解析第100,001行之前拒绝。
- UTF-8 YAML；基于 PyYAML SafeLoader 的受限加载器，拒绝 alias/anchor、显式 tag、重复/non-string mapping key、非有限数；最多32层、200,000个解析事件、64个DATA块。没有手写宽松 YAML 解析器。
- 每次读取数据检查对应 SHA-256；SQLite 也核对固定快照哈希，使用 mode=ro、immutable=1、query_only 和 trusted_schema=OFF，不生成 journal/WAL，不动态载入扩展。
- 不执行上游脚本，不把不可信文献当工具指令，不自动访问来源链接。只有显式构建写参考库；材料导入仍由现有带作用域的材料库审核/冲突保护流程完成。
- v1 不实现在线更新器或自动追踪 main。版本切换保留旧库；不提供隐式垃圾清理。

## 实测固定快照和验证

来源：[官方固定提交](https://github.com/polyanskiy/refractiveindex.info-database/commit/c5c2f188e848453def5970e347399d653df2ffc2)。数据库许可：[CC0 1.0](https://raw.githubusercontent.com/polyanskiy/refractiveindex.info-database/c5c2f188e848453def5970e347399d653df2ffc2/LICENSE)；数据描述论文：[Polyanskiy 2024](https://doi.org/10.1038/s41597-023-02898-2)。具体组的原论文引用须继续保留；CC0 不自动授权下载或分发其链接论文全文和图片。

2026-10-18的本地完整构建实测：上游4,198文件中镜像4,192个数据/说明/许可文件、47,738,456字节；3,816个分类出现，去重3,720个DATA条目（nk3,480、非线性n2 240），其中2个不在catalog中。默认整组投影 supported 3,353、selection_required 51、invalid 55、unsupported 261。3353包含2个未分类文件；不能与仅统计catalog引用文件的3351混淆。

这些规模属于该固定快照，不是永久常数。测试使用小型合成fixture，不把45MiB全库缓存加入Git。完整官方库的下载、逐 Git blob 校验和离线索引/分页/预览证据保存于独立交付目录。验证命令：

```
python -m pytest tests/test_refractiveindex_local.py -q
python -m mypy --follow-imports=silent coat_sim/core/refractiveindex_local.py tests/test_refractiveindex_local.py
```

界面状态/取消/导入确认见 `refractiveindex-browser-contract.md`；MCP/CLI 参数和输出边界见 `refractiveindex-tools-contract.md`。
