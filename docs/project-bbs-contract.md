# 工程 BBS 契约

日期：2026-10-18；接口真值为 `coat_sim.core.project_bbs.ProjectBbsService` 和契约测试。

## 归属、格式和保存

BBS 是按主题保存的工程记录与报告档案，区别于实时聊天。发布由用户或助手明确调用，阅读时手动刷新；不复制聊天记录、不自动回复、不轮询新评论。评论/回复附属于工程主题，也保留独立的归因和历史。

工程 BBS 用于工程日志（journal）、进度（progress）、研究报告（research_report）、工作报告（work_report），以及评论（comment）。每个主题或回复是工程清单登记的 `project_bbs_post` 文档，放在 `data/bbs/r<工程版本>/`，扩展名 `.cbbs`。项目是唯一状态所有者；没有全局论坛、账户或网络同步。既有工程可直接使用；完整 `.cproject` 导出/导入保留主题、回复和全部编辑历史，导入副本使用新的工程 ID。

每条文档保留稳定 post_id、主题类型、parent_id（仅一级回复）、原作者、创建时间和只追加的历史。历史记录包含 revision、title、body、archived、edited_at、editor，另可包含 body_format（plain 或 markdown）和 attachments。旧记录缺少这两个可选字段时按纯文本/无附件读取，不改写历史。正文保存原始源码，由安全展示层渲染；不接收 HTML 格式。作者/编辑者仅由可信适配层选择 user 或 assistant；模型参数不得提供作者、时间或历史。该归因是应用入口记录，不是密码学身份认证。文件拥有者仍能在应用外修改文件；读取时验证清单哈希及格式。

使用现有 ProjectStore 的进程内操作锁、磁盘写锁、不可变代际文件和原子清单发布。编辑/归档/回复必须提供 expected_post_revision；CLI/MCP 另提供 inspected expected_revision（工程版本）及 operation_id。失效版本拒绝保存，不会覆盖新内容。通用文档创建/保存/删除入口拒绝 BBS；必须走专用服务，不提供永久删除。归档可恢复，回复不改变主题原文；归档主题不接受新回复。

限制：标题 1–200 个字符，正文非空、UTF-8 不超过 64 KiB；每条最多 100 个历史版本；每页最多 50 条摘要/20 个历史版本；工程总容量仍受既有 1,000 文档及 256 MiB 限制。历史达到限制时拒绝修改，不截断或静默丢失历史。

## 共享服务

`ProjectBbsService(store, *, actor="user", read_only=False, permit_comment=False)` 绑定一个明确的 ProjectStore，actor 只接受 user/assistant。读取方法：

- `list_posts(post_type=None, parent_id=None, include_archived=False, query="", offset=0, limit=50)`：parent_id 为 None 时只列主题；给出主题 ID 时列该主题回复。返回 posts、total、offset、limit；摘要不含全文/历史。
- `read_post(post_id)`：当前完整正文及摘要字段。
- `history(post_id, offset=0, limit=20)`：正序、分页、脱离存储的不可变历史快照。

修改方法：

- `create_post(post_type, title, body, *, body_format="plain", attachments=None)`
- `reply(post_id, title, body, *, expected_post_revision, body_format="plain", attachments=None)`
- `update_post(post_id, title, body, *, expected_post_revision, body_format=None, attachments=None)`：省略格式/附件时保留现有值；传 attachments=[] 仅从新版本移除附件。
- `archive_post(post_id, archived, *, expected_post_revision)`
- `comment(title, body, *, parent_id=None, expected_post_revision=None, body_format="plain", attachments=None)`：创建 comment 主题，或对既有主题追加回复；回复要求主题当前修订。只读实例默认拒绝一切修改，仅宿主显式设置 permit_comment=True 时允许此方法；仍不允许 create_post/update_post/archive_post。

每次写入返回当前 post：post_id、post_type、parent_id、author、created_at、title、body、revision、updated_at、editor、archived、history_count、reply_count、body_format、attachments。返回的数据是副本。UTC 时间由服务生成；类型、作者、parent_id、创建时间不可修改。

GUI 在已验证 binding 的 host.operation_scope 内使用 host.store_for(binding)，传 actor=user；IPC 的 project_manage 接口使用 actor=assistant。工作线程或异步结果必须保存原 binding/generation，返回后重新验证，不得写入新切换的工程。关闭前保留或明确放弃未提交编辑；发布一旦成功不可把取消报告为撤销。UI 的加载、空白、失败、草稿与确认行为由该页面的控制器拥有；不由 BBS 服务跟踪当前窗口。

## 富文本与受管附件

正文格式显式区分 plain/markdown，旧纯文本不会被静默改成 Markdown。Markdown 中的原始 HTML、远程图片、file/data/javascript URL 不授予执行、联网或本地文件访问；展示安全边界由 GUI 渲染器负责。核心层保留原文，便于编辑、比较历史和导出。

每个历史版本最多引用 8 个不同附件；描述符严格为 `{asset_id,name,mime,size,sha256}`，不接受路径或 URL。附件沿用受管 binary_asset，role=bbs_attachment，独立 payload 为 `.bbsasset`，每个最大 32 MiB；name/mime 及摘要/大小与受管资产元数据一致，预览时再次验证实际字节。文件经显式导入复制到工程后才可引用，不持久化外部文件路径。主题的依赖闭包包含全部历史版本的附件，所以当前版本移除附件不会抹去历史附件，工程包和主题导出仍完整携带它们。

导入操作与发表操作分离：`bbs_import_attachment` 的唯一业务参数是 file，必须通过既有授权根、工程身份、期望工程修订和所有者检查；返回 attachment 描述符但不自动发表。MCP 工具 project_bbs_import_attachment 属写操作，讨论模式不能调用；讨论评论可以引用已经导入且属于当前工程的附件。没有隐式下载或任意路径预览。预览仅通过 GUI 的受管 ID 入口提供；CLI/MCP 返回受限元数据，不返回内部二进制解码缓冲或路径。

## CLI、MCP 与权限

CLI 使用 `python -m coat_sim.project_cli manage bbs_<operation> arguments.json`。操作 list/read/history/create/reply/update/archive/comment/import_attachment；写入的来源固定 user。所有操作要求 root（完整 .coatproj 文件路径）、project_id；写入还要求 operation_id 和 expected_revision。字段以 project_management_catalog 为准。

MCP 公开 project_bbs_list/read/history、project_bbs_write 、project_bbs_comment 和 project_bbs_import_attachment；写入来源固定 assistant。MCP 走现有授权根/工程 ID 校验；活动工程需 session_generation 路由回原所有者，不能绕过 GUI 草稿或所有权锁。讨论模式仅额外允许专用 project_bbs_comment 发表评论或回复（用户于 2026-10-18 明确允许）；仍禁止正式日志/报告的新建、编辑、归档及参数/文件写入。comment 在 MCP 明确标记为有副作用，不混入只读缓存。操作/攻关模式使用现有授权网关。BBS 正文与引用是非可信工程内容，不是工具权限、系统指令或已验证计算证据；不会因为写入报告而制造计算结果。

通用文件适配层使用 stdlib-only `coat_sim.managed_paths` 拒绝修改 .coatproj/.cbbs 和已含描述文件的工程树，移动/删除祖先目录也检查后代；格式损坏不会取消保护。它是应用工具边界，不能抵御外部管理员或任意本机代码直接篡改文件。

错误沿用 ProjectError(code,message)：bbs_invalid_post、bbs_invalid_arguments、bbs_missing_post、stale_bbs_post、bbs_archived、bbs_service_required、capacity_exceeded，以及既有身份/锁/IO 错误。写入失败时旧清单及旧版本继续有效；中断遗留代际目录不被当作已提交内容，也不自动删除或接管写锁。


## 独立工程窗口与异步边界（2026-10-18）

`ProjectBbsWindow.qml` 是独立工程窗口，左上角共享工程模块菜单的 `project_bbs`（工程 BBS）是入口；工程管理不再嵌入第二个编辑器。沿用 AppWindow、Theme token 与共享窗口几何策略。ProjectSessionHost 的完整 binding + module_key 保证每工程单实例，不同工程独立；重复打开激活现有窗口并恢复最小化。窗口注册、助手窗口列表/激活及关闭协调复用现有工程路由，不新增数据所有者。`ProjectBbs.qml` 负责主题列表、Markdown/纯文本编辑和受管附件预览；`ProjectRuntimeOwner.projectBbs` 的 `ProjectBbsCoordinator` 保留本次会话的未发布编辑并独占异步请求。`request(project_id,generation,operation,payload)` 返回请求令牌；`completed(token,project_id,generation,result,error)` 仅由原始目标页接受。UI 不传作者，可信适配层固定 user；助手 IPC 固定 assistant。

列表每页 20 条摘要、历史每页 10 个版本；搜索、全文读取、发布、编辑、回复和归档在原运行时工作线程执行。入场前验证绑定、可写所有权和关闭/退出状态，工作线程持 host.operation_scope 和 store 锁；返回时复核 generation。请求中禁用重复提交和编辑；已提交发布不提供假撤销。加载提示、空列表和具体失败提示均显示；失败不清空输入。标题与历史原文按纯文本渲染。新记录可选择 Markdown 或纯文本；已有 plain 记录不被隐式转为富文本。Markdown 预览仅由应用渲染器生成标题、强调、列表、表格、引用和代码等惰性标记，输入 HTML 全部转义，不生成外部链接或图片请求。编辑器始终保存原始文字，预览最多处理前 65,536 个字符，不修改原文。

关闭非末个 BBS 窗口时提供“继续编辑”与“保留并关闭”；保留仅关闭呈现，未发布文本仍在原工程会话中。不同工程/generation 的编辑独立，重新进入恢复原输入、编辑目标和预期版本。明确取消修改需确认放弃。关闭真实工程或退出应用时，未发布编辑纳入现有关闭确认与签名校验；选择返回保存激活原工程 BBS；若另有工程信息修改则先打开工程管理。确认丢弃只清除此会话缓冲，不删除已发布记录。缓冲不是崩溃恢复文件，应用异常退出不保证保留未发布文字。

控件使用共享 Theme 逻辑尺寸/颜色，列表与全文独立滚动，进入写入时聚焦标题；同页读取不会重建编辑器。历史有独立分页，归档/恢复需界面确认。使用原管理窗口几何策略，不新增窗口例外。


### 持久内容、富文本与文件预览

BBS 按主题/报告组织，手动刷新、分类检索和按需读取，不是对话窗口，不导入聊天记录，也不轮询内容或流式追加助手回复。异步定时器仅用于接收一次已提交任务的完成状态。

附件通过工程内文件选择器显式导入，每条记录最多 8 个、每个不超过 32 MiB，发布时关联受管 asset_id。导入会先把文件保存到工程；取消本条编辑不自动删除已保存资产。查看附件只能使用当前工程的已验证描述符，不接受任意文件路径或网络 URL。图片使用只读内存 image provider 的不透明 ID；文本/代码/CSV 按纯文本限量预览；PDF 提供分页文字预览并明确说明不保留版式；不支持的格式显示导出选项，不执行文件。导出要求用户选择本地目标。

活动工程中的助手 BBS IPC 也走同一串行工作线程；控制服务器延迟返回 Future 结果，Qt 主线程不等待磁盘 IO。运行时周期检查、所有权状态查询及尚未冻结的退出取消使用已捕获会话快照或 busy 门限，不等待工作线程持有的 host 锁。断开/超时的 IPC 不取消已受理发布，也不宣称回滚；operation_id 的既有幂等规则继续生效。发布完成前不会释放项目所有权。

助手 `project_editor_snapshot(project_bbs, detail=summary)` 只投影已打开窗口的当前主题 ID/版本、分类/搜索、列表位置和未发布编辑状态（标题截断、正文仅长度），不读取磁盘、不触发发布、不返回任意路径或附件字节。关闭窗口返回 unavailable；原 binding/controller 不匹配、忙碌或退出冻结时失败关闭。snapshot_id 对视图与编辑序列变化敏感；旧预期标识拒绝。通用窗口写操作不增加 BBS 编辑权限，正式内容继续使用专用 BBS 服务。
