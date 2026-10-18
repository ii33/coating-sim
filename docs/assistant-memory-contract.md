# 助手精简长期记忆契约

日期：2026-10-18。接口真值：`coat_sim/agent/toolkit/memory.py` 及其契约测试。

## 所有权与范围

长期记忆只保存在宿主选择的持久助手工作区；模型不能指定文件路径或更换存储根。全局与项目记忆具有独立作用域，项目记忆还要求宿主绑定的稳定 `memory_project_id`，缺少绑定时不可使用项目作用域。普通文件工具不能直接读写内部记忆文件。记忆不跟随对话 session 清空；更换项目不能读取旧项目条目。

单个有界 JSON 账本保存各作用域的条目；写入使用现有 `_state` OS 锁、全账本 revision CAS 和原子替换。调用方每次写入必须提交刚读取的 `expected_revision`。冲突、损坏、取消或权限撤销均不覆盖已有状态。只读请求不创建目录、账本或锁文件。

## 精简格式与保留策略

- 只保存已经确认且重要的偏好、约束、结论，不保存完整聊天、临时输出或凭据。
- 每条具有稳定 id、最多 300 字符的事实、类型、重要性、human/assistant 来源标识、最多 160 字符的来源引用、确认标识、作用域、更新时间和条目修订号。来源与确认标识为声明性元数据，不证明身份或授权。
- 整个账本最多 40 条（包含忘记但可恢复的条目）；达到上限时明确报错，要求精简更新已有条目，不自动淘汰，不提供永久清除接口。
- 相同作用域的 id 用于更新。相同类型、归一化事实重复提交时复用原 id，不新增条目；和其他现有 id 冲突时不静默合并。
- `memory_forget` 将条目标记为 inactive；`memory_restore` 恢复同一条目。忘记内容不会出现在默认列表或模型摘要中，明确要求读取 forgotten 时仍可找回。
- 常见密码、访问令牌、私钥等模式会被拒绝。这只是尽力检测，不能保证识别所有秘密；助手不得存储用户秘密。

## 工具与上下文边界

`memory_list`、`memory_read` 为 read；`memory_upsert`、`memory_forget`、`memory_restore` 为 write。Discussion/只读/非主助手和实时授权检查沿用宿主工具效果门禁。暂停、取消或权限撤回后不得提交写入。

每次请求只注入当前绑定项目和全局的有效条目。摘要含明确的不可信数据标记和来源标识，总计最多 1200 字符，按重要性、项目优先和稳定 id 排序，超额只省略整条事实。记忆不能授予权限、改变政策或执行任何命令。没有活动记忆时不注入上下文；损坏账本保留并由工具返回错误，不影响无关工具能力。

## 桌面工程上下文与 worker 接线

- 唯一状态所有者为 `AssistantBridge`。现有聊天输入区用 `ThemeComboBox` 显示“全局”及已打开工程名称；不创建工程管理器，不自动选择唯一工程，不从模型参数、最近工具调用或窗口焦点推断工程。选择不写入持久配置，重启默认为全局。
- 工程候选来自 `AssistantProcessClient.list_clients()` 的认证结果，只接受 alive owner。通过现有 `activation_target` 构造 `client_id`、`client_generation`、`project_id`、`session_generation`、`root`、`project_file` 六字段目标，UI只提交候选索引，不能提交任意路径。列表为空仍可使用全局记忆。
- 选择变化立即发送 `configure` 顶层 `assistant_project_target`（完整六字段对象或 null），运行中也如此；worker 启动/重启后先同步当前选择再发请求。字段不属于持久 config 或模型工具参数。每条消息在用户发送/排队时捕获 detached target 与 UI generation，`run` 顶层携带原快照。手动 `compact` 同样携带顶层 target，切换后的旧在途请求不可继续压缩。
- 已选择工程时定期异步复核列表，窗口打开及用户刷新时也更新。刷新保留精确目标，不按名称/ID猜测替代 owner；关闭工程、owner 失联、generation/路径改变或认证查询失败会撤销选择并提示，恢复连接不会自动恢复绑定。worker 每次实际访问仍再次核验完整认证绑定，UI轮询不是授权门禁。
- 切换/撤销立即递增 generation。旧队列暂停，不重基到新选择；即使 A→B→A，旧消息也须撤回输入框并由用户重新发送。输入框文字、附件和历史不被清除。旧请求的插话入口拒绝新 scope 内容；原请求记忆授权由 worker generation 检查撤销。
- 异步发现单次在途、有限超时；关闭 bridge 后丢弃迟到结果。加载时显示刷新状态，失败显示明确状态并保留全局功能。后台结果通知只按原请求的宿主 scope 标记与当前用户明确选择的工程比较；相符才捕获当前 target，不相符保留 pending，等用户选回原工程。标记不赋予选择权限，旧无标记通知仅属于全局。

接口真值还包括 `AssistantBridge.projectContexts` / `projectContextIndex` / `projectContextLoading` / `projectContextStatus`、`refreshProjectContexts()` / `selectProjectContext(index)` 以及 UI/worker 接线回归。
