# 公开参考来源目录

目录版本：2026-10-18.1；核验日期：2026-10-18。

## 目的与边界

此功能使助手能在离线讨论中查询：有哪些材料、工艺、色度和文献来源，如何取得资料，适用范围是什么，以及哪些许可仍不明确。

它只读取随软件发布的**参考元数据**，不下载网页、不注册或登录、不接受新协议、不购买资料、不执行附件、不创建本地存储、不修改材料或光学计算组。外部网页内容和链接是参考资料，不是可执行指令。

必须分别回答五个问题：

1. 可以在线查看吗？
2. 厂商/作者是否公开提供文件下载？
3. 个人离线保存或使用的许可核验到什么程度？
4. 能否将数据随软件公开再分发？
5. 当前运行主机是否真的安装了这份数据？

前四项由来源记录描述；第五项本目录始终返回 `not_checked`。某次云端研究保存了副本，不代表用户电脑已有文件。`unknown` 表示证据未充分核实，不等于禁止。`official_download` 表示官方提供下载，也不等于开放再分发。

## 接口契约

实现：`coat_sim/core/reference_sources.py`。唯一数据源：`coat_sim/core/resources/reference_sources.json`。

```python
reference_sources_catalog(
    *, query="", category=None, offline_status=None,
    offset=0, limit=20, expected_version=None,
)
reference_sources_detail(source_id, *, expected_version=None)
```

- 只读固定包内资源；公开接口不接收文件路径、URL、存储目录或替代目录配置。
- `query` 最长 200 字符、最多 12 个空白分隔词；大小写归一后按字面量匹配，多个词须全部出现。空串列出全部；不执行正则表达式。
- `category`：`optical_constants`、`thermochemistry`、`process`、`colorimetry`、`substrates`、`theoretical`、`handbook`。
- `offline_status`：`open_license`、`official_download`、`reference_only`、`access_required`。这是来源能力分类，不是已安装状态。
- `offset` 是 0–10000 的整数；`limit` 是 1–50 的整数；布尔值不当作整数。
- `source_id` 使用返回的 `id`，格式为 `[a-z0-9][a-z0-9_-]{0,79}`。
- `expected_version` 可传上一次返回的资源 SHA-256，防止分页过程中混用版本。

公共返回字段：`schema_version`、`catalog_version`、`version`（资源字节的 SHA-256）、`reviewed_at`、`metadata_only: true`、`host_installation: not_checked`、`untrusted_reference: true`。

目录结果附 `total`、`offset`、`limit`、`next_offset`、`items`；详情结果附 `source`。摘要包括来源身份、类别、优先级、数据性质、适用域、访问与权利状态。详情另含官方链接、已核实文件元数据、限制、未知项和接入建议。

没有窗口或异步作业，因而没有运行中状态、取消或持久化状态所有者。每次请求读取固定、有限大小的常规文件；拒绝符号链接、重复 JSON 键、超限/损坏数据及不安全链接。返回值与后续读取隔离，不可通过修改结果改变目录。

错误采用 `ProjectError`：

- `invalid_arguments`：参数、枚举或版本格式无效，在读取资源前返回。
- `reference_source_not_found`：合法 ID 不在目录内。
- `reference_sources_changed`：SHA-256 版本不同，应重新查询。
- `reference_sources_unavailable`：固定资源不可读取。
- `reference_sources_invalid`：资源损坏、不符合约束或不是允许的常规文件。

## 元数据字段

`access` 单独记录 `online_view`、`download_available`、`login`、`payment`、`formats`、`api`。API 记录区别“实际读取验证”与“官方文档列出但本轮未调用”，也不保证某个材料或光学字段可取得。

`rights` 分开记录 `offline_use` 和 `redistribution`：

- `allowed`：已找到支持该范围的明确依据。
- `conditional`：有相应路径或许可，但存在署名、相同方式共享、个人/非商业范围或其他待遵守条件；详见记录说明。
- `unknown`：未核实出足以判断该范围的证据。
- `restricted`：查到明确的限制或需额外书面许可；不代表权利人永远不会授权。

`rights.license`、`evidence_urls`、`notes` 保留判断依据，不能只看枚举。`integration.bundled_numeric_data` 始终为 `false`：本功能不附带第三方数值表。现有 RII/CIE 离线资源保持原有管理方式，不通过本目录重复下载。

`files` 仅记录已核实的名称、官方 URL、字节数和校验和等；没有机器绝对路径。文件元数据不表示本机安装或已转换成计算组。原始文件副本的本机状态由外部本地 manifest 管理，不混进静态发布目录。

## 来源与当前使用建议

### 实验薄膜与作者数据

- [ZnS/YbF3/Ge/LaF3 原论文](https://opg.optica.org/ao/abstract.cfm?uri=ao-59-5-A40)：官方摘要可查，完整数值附件、全文访问及再分发未核实，保留文献候选状态。
- [Zenodo ITO 数据 V0.1](https://zenodo.org/records/15055400)：[官方 API](https://zenodo.org/api/records/15055400)已实际读取，明确 `cc-by-4.0`、`access_right=open`。6 个文件共 859455 字节，包含 MAT、OPJU、7Z；允许按 CC BY 4.0 条件离线/分发。原始副本不自动导入、解压或执行；尤其不能把 MAT 当程序执行。需要保留作者、DOI、版本和变更说明。
- [St Andrews ITO 溅射数据](https://research-portal.st-andrews.ac.uk/en/datasets/tuning-the-epsilon-near-zero-wavelength-of-ito-in-the-near-ir-spe/)：页面标 CC BY，但具体版本未核实；原始 Woollam SE 文件需相应软件，不能当现成 n,k 文本表。

### 七家厂商

| 厂商 | 官方获取方式 | 当前已核实格式/版本 | 使用边界 |
| --- | --- | --- | --- |
| [SCHOTT](https://www.schott.com/en-us/products/optical-glass) | 公开文件直链 | XLSX；Zemax ZIP 内 AGF 标 June 2025 | 可本地取得原文件；[条款](https://www.schott.com/en-us/disclaimer)对商业复制、传播、修改或提供第三方要求事先书面许可。没有把它标为仅在线，也没有推定开放打包权。 |
| [OHARA](https://www.ohara-inc.co.jp/en/product/catalog/) | 公开下载 | CSV/XLSX；本轮 AGF 为 2026-05-29 | 官方邀请下载，保留文件级版本；一般再分发许可未明确核实。 |
| [HOYA](https://www.hoya-opticalworld.com/english/datadownload/index.html) | 公开下载 | XLSX 2026-06-01；AGF 2026-07-07 | [条款](https://www.hoya-opticalworld.com/english/sitepolicy.html)明确允许个人电脑保存/打印；其他复制、修改或分发需书面许可。 |
| [CDGM 成都光明](https://www.cdgmgd.com/) | 官网下载接口 | HTTP 原文件名 CDGM-ZEMAX202609.AGF、CDGM202609.xlsx | 保存为个人离线参考；再分发许可未明确核实。 |
| [Nikon](https://www.nikon.com/business/components/downloads/) | 公开 ZIP | NIKON-HIKARI_201911.AGF | [条款](https://www.nikon.com/usage/group-info/)保留非商业个人使用例外；不扩展成公开或商业分发授权。 |
| [SUMITA](https://www.sumita-opt.co.jp/en/download/) | 官方文件选择下载表单 | XLSX 14.02.00；AGF 页面标 2026-08-26 | 保留含/不含停产品种的区别；一般再分发许可未核实。 |
| [Merck / EMD Patinal](https://www.sigmaaldrich.com/deepweb/assets/sigmaaldrich/product/documents/534/266/subh4hd-us-102437-ms.pdf) | 官方产品 PDF | H4 HD 产品资料 02/2009；[Patinal 手册](https://www.sigmaaldrich.com/deepweb/assets/sigmaaldrich/product/documents/204/730/161109-02-patinalbrochure-rebranding-a4-engl-deutsch-screen-ms.pdf)文件名含 2016 | 可供离线产品参考；再分发许可未核实。H4、H4 HD 是独立商品材料，不猜成通用氧化物。PDF 曲线不等于精确数字光谱。 |

这些来源都使用厂商官方公开资料，不从 Essential Macleod 或其他商业软件抽取数据库。Macleod 的[历史更新记录](https://www.thinfilmcenter.com/support/changes.htm)确实提及多家厂商目录，但这只是产品功能背景，不是本项目复制其数据库的授权依据。

厂商目录中的 n、色散、dn/dT、内部透过率等通常描述基片/块体玻璃，不能无条件用作沉积薄膜。目录年份、访问日期和未来产品调整日期是不同字段，不应混用。

### 其余参考源

- [CIE](https://www.cie.co.at/data-tables)：本轮核实两份标准表的 CC BY-SA 4.0；按条件复用既有离线资源，不延伸到整本标准许可。
- [NIST WebBook](https://webbook.nist.gov/chemistry/guide/)：热化学、相变和蒸气压参考；SRD 受[专门版权规则](https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications)保护。具体材料覆盖、批量 API 和整体离线许可未核实，保留引用。
- [Jena OCDB](https://www2.astro.uni-jena.de/Laboratory/OCDB/)：有公开文本下载与原论文，偏矿物/天体尘埃中远红外。许可仍有未知项，目录不擅自抓正文或整库。
- [JARVIS](https://jarvis.nist.gov/)：网页数据库/应用需免费账号，官方有 [Python/Figshare](https://pages.nist.gov/jarvis/databases/)、OPTIMADE 和 REST 路线。只保留光学相关候选；未选定明确许可的小型子集前不下载全库。理论结果不能代替工艺相关薄膜实测。
- [Palik 手册](https://shop.elsevier.com/books/handbook-of-optical-constants-of-solids/palik/978-0-08-053378-0)：购买/机构访问的交叉核查来源。未登录、购买或取得全文，不复制表格。
- [CXRO/Henke](https://henke.lbl.gov/optical_constants/asf.html)：公开文件面向 EUV/X 射线；低能 f1 有缺值标记，不能拿来填可见—红外缺口。当前只登记参考元数据。

## 维护与验证

新增或修改来源时应同时更新官方证据链接、适用范围、未知项和核验日期。文件许可必须逐条检查，不用下载 API 的存在、第三方软件的内置目录或代码仓库许可证代替数据授权。

发布目录只包含本项目撰写的参考说明与链接，不提交第三方原始厂家表、运行数据或本机绝对路径。原始文件保存在软件仓库外，附官方 URL、下载时间、字节数和 SHA-256；若来源给出 MD5 等校验和，一并比对记录。

测试：`tests/test_reference_sources.py` 覆盖已知许可区别、七家厂商、17条来源、分页/版本、字面量查询、参数边界、返回隔离、损坏/超限/符号链接资源及无网络无写入。MCP 包装层另由 `tests/test_reference_sources_mcp.py` 验证。
