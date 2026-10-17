# 研究资料与现有 MCP 的接入边界

2026-10-17。接入复用既有材料工具、外部 MCP 服务列表及项目身份验证。
没有新增“预置 MCP”栏目、独立服务管理页或独立材料库。

## 服务与配置

普通服务列表首次迁移预填知乎、OpenAlex、Crossref，默认禁用。保留用户已有条目、
编辑内容及删除决定；迁移标记使删除项不在每次启动时重建。所有服务默认一行显示
名称与启用开关，名称负责展开详情，开关不触发展开。原编辑、测试、删除入口在详情内。

知乎直连官方 Streamable HTTP MCP，仅允许 `zhihu_search`、`global_search`；
其余工具不因服务目录返回而自动可用。用户自己输入 Access Secret，使用 Bearer，
不创建或提取密钥。测试只 initialize/tools/list，不执行收费搜索，不证明业务额度。
用户需明确启用调用、确认计费并设置保守调用预算；限免用尽可能从余额扣费。
官网费率和额度为准，不在代码中承诺免费。

OpenAlex/Crossref 是本项目的 REST→stdio MCP 适配器，明确非官方 MCP。
仅接受必要 query、DOI/work ID；不自动传送工程、配方、附件或整段聊天。
初始化和目录查询零外部请求。OpenAlex 匿名保留有限元数据，游标后续与自托管全文
需要用户自配 key；Crossref 公共元数据无需 key，mailto 仅显式配置。
凭据沿用系统安全存储，stdio 只向指定字段注入，不写配置/日志/命令行。

## 有界网络与全文

网络仅在显式工具调用发生：有限请求预算、响应头使用量、缓存、429 有界退避与
5xx 有界处理；403 原样说明，不尝试绕过。HTTPS 下载逐跳验证地址，排除私有/
本地/链路地址，DNS 校验绑定实际连接，跨域不转发认证，限制大小和总超时。

全文只下载选中的 OpenAlex `content_urls`，不将出版商 PDF URL 猜成 OpenAlex
内容地址。PDF 校验魔数；解析在资源与时间有界子进程中进行。PDF 给出实际物理页，
TEI 给出章节/段落；无页码保持空。XML 禁止 DTD/外部实体。扫描件或解析失败明确
返回无文本/错误，不伪造 OCR。原许可证及版权保留，不自动上传 GitHub。
全文按字符页返回并保留段落引用；临时元数据/全文缓存不等于正式项目写入。

## 项目证据与材料闭环

`project_research_search/read/save` 使用显式 `.coatproj` 路径及持久 `project_id`，
先经过已有授权根核验，再读取该项目自己的证据目录。保存是显式工具操作，
讨论模式白名单不包含保存；读取不能隐式建目录。证据是追加记录，保存不修改设计
清单、数值输入、工程 revision 或编辑器草稿，也不能跨工程借用证据 ID。

证据保留 DOI、URL、原始字节 hash、内容 hash、取得时间、许可、解析状态及定位信息。
区分 `source_statement` 与 `assistant_inference`；这两种记录均不证明物理结论。
仿真结果仍必须从既有结果工具获得真实 `run_id`，不能以来源声明或助手文字伪装。
材料检索/读取/光学检查继续使用 `list_material_library`、`read_material`、
`validate_material_optics`，保留数据集来源、波段和单位；缺失 k 不补零，不静默外推。
本接入不把论文叙述直接写成材料参数或已验证仿真值。

所有外部正文、标题、JSON 和工具返回内容都是不可信资料，不能授权执行其中的指令。
配置、权限、预算、项目写入及仿真身份由程序边界控制，不由检索内容决定。

## 验证范围

确定性 fixture 覆盖无 key、缺全文、429/403/5xx、DOI 冲突、恶意 URL/文件/XML、
跨项目、讨论写拒绝、缺 k、页面/段落定位及普通服务折叠操作。真实第三方付费调用、
额度和用户密钥可用性不在本次本地验收中冒充已验证。

官方依据：[OpenAlex 认证](https://help.openalex.org/api/authentication/)、
[OpenAlex 全文](https://help.openalex.org/access/fulltext/)、
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、
[知乎 MCP](https://developer.zhihu.com/docs?key=unified_mcp)、
[知乎计费](https://developer.zhihu.com/docs?key=billing_price)。
