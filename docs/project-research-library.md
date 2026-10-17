# 项目研究证据库

研究证据由现有项目工具显式保存和读取。一次检索或全文解析只返回临时结果，不自动保存、上传项目内容或加入 GitHub。调用者先通过项目管理授权指定 `.coatproj` 与持久 `project_id`；存储层不从当前窗口、工作目录或聊天文字猜测工程身份。

## 存储及接口

`ProjectResearchStore(project_file, project_id, read_only=False)` 绑定真实工程描述文件。`save(record)` 只追加新证据，返回含 `evidence_id` 的记录；`list(query='', offset=0, limit=10)` 返回摘要；`read(evidence_id, offset=0, max_chars=6000)` 按字符分页返回正文及所覆盖源段落。列表最多 50 条，单次正文最多 6000 字符。查询不创建目录，也不枚举其它工程。

正式记录位于工程根目录 `data/research/<project_id>/<evidence_id>.json`，与设计输入、`.coatproj` 修订号及仿真作业分开。目录最多 1000 条记录、64 MiB；单条记录最多 4 MiB，正文最多 2 Mi 字符。写入使用独占锁、同目录临时文件、flush/fsync 和不覆盖的原子发布。同 ID 再次保存会报错。若进程异常中断遗留 `.write.lock`，保存失败并要求确认所有权后人工处理，不自动清除未知锁。读取拒绝链接、重解析点、非普通文件及硬链接记录；文件内工程身份与摘要不一致时失败，不跳过坏记录继续声称结果完整。

## 记录与引用

- `title`、`doi`、`original_url`、`source` / `source_id` 记录来源；DOI 格式合格不代表已核实文献匹配。来源网址必须是无凭据、查询参数和片段的 HTTP(S) URL，避免保存密钥或签名网址。此层不下载或访问网址。
- `sha256` 保留取得的原始文件字节摘要；`sha256_basis='source_bytes'`。只有文本的记录以 UTF-8 正文计算摘要并标记 `utf8_content`。`content_sha256` 始终独立覆盖保存的正文。外层另存规范 JSON 的 `record_sha256` 检测记录损坏；这些摘要不是数字签名，不能抵抗有权改写全部本地文件的攻击者。
- `retrieved_at` 必须是含时区的 ISO 时间；省略时记录本次保存时间。全文适配器的 `source_url`、`acquired_at`、`id` 分别兼容转换为 `original_url`、`retrieved_at`、`source_id`，不覆盖相冲突的显式来源。
- `license` 原样保存声明；未知保持 `null`。保存或解析不赋予新许可，也不自动改为项目开源许可证。使用者应按原来源许可处理全文及其转发。
- `parse_status` 为 `not_parsed`、`parsed`、`no_text`、`failed` 或 `metadata_only`。`segments` 保存 `text`、PDF 真实物理 `page`（从 1 开始，含空白页位置）或 TEI 的 `section` / `paragraph`；未知位置保持 `null`。正文为段落文本以换行连接，分页返回原定位及 `segment_offset`，不能据段落序号编造页码。存储层保留调用方提供的定位，不声称独立核实过源文件。
- `statement_kind` 只能是 `source_statement`（来源陈述）或 `assistant_inference`（助手推断）。此库拒绝 `simulation_result` 或任意 `run_id` 字段；仿真结论应通过已有真实作业结果工具取得并引用真实 `run_id`。

## 权限与错误

所有返回值标记 `untrusted_reference=True`。网页、PDF、TEI 和记录正文都是不可信参考数据，内嵌命令不能被当作系统指令执行。存储不执行脚本，不初始化模型，也不把记录内容发给外部服务。需要给模型上下文时，由现有工具与用户授权边界管理。

讨论模式的运行时拒绝写工具；可信调用方还可使用 `read_only=True`，其 `save` 在创建目录前直接失败。模型传递 mode 参数不能改变该属性。项目不匹配、无效字段、容量超限、摘要损坏、路径链接、重复 ID、并发锁分别返回稳定的 `ProjectError.code`；调用者不应把失败包装为保存成功。

本阶段没有删除、覆盖、自动迁移或自动提交证据能力，也不改材料 n/k 数据、波段或外推策略。无密钥本地材料查询与现有研究镜像查询继续使用已有工具。
