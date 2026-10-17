# 研究全文获取与解析

`coat_sim.agent.research_fulltext.fetch_fulltext` 仅获取用户明确选择的来源记录中声明的 PDF 或 TEI 链接，不自动补搜、不猜测下载地址，也不会写入项目研究库。记录需含 `content_urls.pdf` 或 `content_urls.grobid_xml`（也接受 `tei`）。`has_content` 明确为 false、缺少链接、缺少 OpenAlex 全文密钥时均明确返回不可用；不能从摘要或元数据伪造全文。正式保存由项目证据库的写权限及项目隔离机制另行控制。

OpenAlex 的 `content.openalex.org` 全文链接需要用户提供自己的密钥，仅通过 `Authorization: Bearer …` 发送给该主机。出版商链接不会收到 OpenAlex 密钥。共享安全 HTTP 客户端对每次请求和每次重定向校验公共 HTTPS 地址、绑定解析后的公共 IP，跨域移除认证，限制请求次数、超时及下载大小；403 不绕过。该功能可能消耗服务商额度，连接测试不自动获取全文。不要提交密钥、全文或私有配方到仓库。来源 URL 在结果中移除 query 和 fragment，避免持久化访问令牌。

原始下载上限为 16 MiB。OpenAlex `.grobid-xml` 按官方约定可能通过 `Content-Encoding: gzip` 返回压缩字节；TEI 支持该头，未声明但有 `1f 8b` 魔数也会识别。解压仅在受限解析工作进程中进行，解压后另有 16 MiB 硬上限，损坏的 gzip、压缩炸弹和未知编码明确拒绝。原始下载 SHA-256、字节数始终对应尚未解压的原始 HTTP 响应字节，另返回解码字节数；gzip PDF 暂不支持。PDF 必须以 `%PDF-` 开头，使用 BSD-3-Clause 许可的 `pypdf` 提取文本；未安装时返回 `pdf_parser_unavailable`。不依赖 GPL 解析器。下载数据送入单独的解析工作进程：源码运行使用 Python `-I -B` 和绝对脚本路径，冻结版本使用已注册的 `research-parser` 子进程角色，不把 EXE 当作 Python 解释器。环境不继承 API 密钥或项目上下文。冻结路径当前仅验证角色命令，不代表已构建或验收 EXE。输入只在自动删除的临时文件中短暂中转；输出在子进程编码和父进程读取两侧均限制为 4 MiB，超限只终止本次解析工作进程。解析墙钟上限为 15 秒、内存上限为 768 MiB、CPU 上限为 12 秒；无法安装 OS 资源限制时失败关闭。Windows 使用当前工作进程专属 Job Object，POSIX 使用资源限制。这里只隔离和限制文档解析，不能作为任意不可信代码运行的通用沙箱。

PDF 返回真实的 1 起始物理页码，包含空白页，最多 200 页；不把印刷页码、目录或推断位置冒充物理页码。没有文本层的扫描件返回 `no_text` / `no_text_layer_no_ocr`，不会假称已经 OCR。PDF 文本排序和公式、表格可能丢失版式，需要回看原文。

TEI 提取标准 `<TEI><text><body>` 正文中的 `p`，也限定支持 OpenAlex 官方说明的旧 GROBID `<html><body><tei><text><div>` 包装和 `text/div` 回退；局部元素名不区分大小写。普通 HTML 和其他任意包装仍拒绝。保留嵌套 `div/head` 章节路径和 1 起始段落序号，`page`、`page_count` 均为 null。既不从段落数量推算页码，也不把 TEI 的 `pb` 标签当已核实 PDF 页码。标准库 Expat 的 DTD、实体和外部实体回调全部拒绝声明，包括 UTF-16 文档；另有限制元素数量、嵌套深度和输出文本。没有正文段落会明确返回 `no_text`。这不是通用 HTML、JATS 或任意 XML 抽取器。压缩的 UTF-16 文档同样执行 DTD/实体拒绝策略。

每次成功下载保留原始字节 SHA-256、取得时间、字节数、原许可（未知保持 null）、来源 ID/DOI、脱敏 URL 和解析状态。所有文本标记为 `untrusted_reference=true`，属于来源陈述；不得把其中的指令当作工具授权、助手结论或带真实 run_id 的仿真结果。失败不会生成伪引用。单次解析最多 500,000 个字符（含重复返回的章节路径）、2,000 个 TEI 段落；单级章节标题最多 500 个字符，章节路径最多 1,000 个字符；超过限制明确失败，避免静默截断产生不完整证据。

参考：[OpenAlex 全文文档](https://help.openalex.org/access/fulltext/)、[pypdf 文本提取限制](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[pypdf 许可](https://github.com/py-pdf/pypdf/blob/main/LICENSE)、[Python XML 安全说明](https://docs.python.org/3/library/xml.html#xml-vulnerabilities)。确定性测试使用本地原创 PDF/TEI 和模拟 HTTP，无需真实账号、密钥或付费搜索。
