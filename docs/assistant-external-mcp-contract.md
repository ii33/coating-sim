# 外部 MCP：连接、取消和不确定效果契约

日期：2026-10-14。范围：`ExternalMcpPool` 的独立外部连接边界；不改变本机业务 MCP、数值算法或外部服务器权限。

## 输入、状态所有者和门禁

接口真值为 `coat_sim/agent/external_mcp.py` 的 `validate_server`、`ExternalMcpPool.catalog(server_id, tool_name)`、`execute(server_id, tool_name, arguments)` 与契约测试。配置只保存非密钥字段。当前 worker 的 pool 是连接、工具目录、连接错误与未知效果屏障的唯一状态所有者。

- `enabled` 和 `allow_tools` 必须同时为严格的 `True`，才可建立工具目录连接或派发调用。默认目录只列服务器，不连接。锁内再次检查当前配置，不能用等待锁前的旧授权连接。
- 未授权项目数据时，已知项目路径参数在建立连接或调用前拒绝。这个启发式检查不识别任意复制内容，不是 DLP，也不是操作系统沙箱。外部本地程序仍以当前账户权限运行。
- 客户端 roots 为 `[]`；不会自动发送历史、附件、项目根、模型密钥。stdio 只继承明确的运行环境白名单及该服务明确配置的凭据。显式工具参数仍由调用者负责按当前用户请求和数据授权限定。
- 配置删除、停用或变更关闭旧连接。连接建立期间配置变化时，旧连接清理且派发前返回 `effect=none`，不会给新配置缓存旧连接错误。派发前再次核对同一配置，不能把旧目标的身份用于新连接。执行与关闭使用同一服务器锁；取消等待关闭不证明服务器业务已取消。
- active 客户端绑定其实际创建时的非密钥配置快照。`sync` 公开新配置但仍在等待锁时，排在清理之前的 execute/catalog 必须核对客户端快照，关闭旧连接后才可连接当前目标；不能复用旧目标的工具目录、HTTP 头或凭据修订。关闭旧连接期间配置再次改变时，拒绝建立已过时的替代连接。清理取得锁后再核对快照，不关闭另一操作刚建立的当前目标连接。目录元数据与返回工具来自同一已连接快照；拿到连接后到读取这些数据之间没有异步让步。
- 确定未派发的连接错误只属于其原配置。配置替换会清除同 ID 的旧连接错误，允许显式新目标连接；这不是重试已经派发的 unknown 调用。真实 HTTP 断线 / timeout 后，SDK close 可能重抛先前 transport 例外；本地撤权仍完成连接 / 目录 / 快照移除并尝试关闭 transport。清理阶段普通例外不阻止撤权，`CancelledError` 仍上抛；unknown 屏障保持不变。

## 错误、取消及重放

参数、门禁、目录和连接建立失败发生在调用派发前，返回 `effect=none`。实际 `tools/call` 派发后，业务 `isError`、响应解码失败、超时、连接丢失均保留 `effect=unknown`；外部返回不能证明未发生效果。任务取消继续抛出 `asyncio.CancelledError`，同时记录未知效果屏障。

屏障身份为 `(server_id, connection_target_sha256, tool_name, arguments_sha256)`。连接目标包括 transport、command、args、cwd 和 url；显示名称、权限开关和凭据修订不会解除原目标屏障。参数按排序键、紧凑分隔符的有限数值 JSON 编码；仅保留 SHA-256，不保存参数或密钥原文。无效 JSON 参数在派发前拒绝。对相同目标、工具和参数，新的用户轮次、对象键顺序变化、停用后恢复或重新连接都不能自动重放。

被阻止的新调用返回 `effect=none`、`prior_effect=unknown` 和 `dispatch_blocked=true`，明确本次未再次派发，之前效果仍未知。不同工具或实际不同参数代表独立调用，不被一概锁死；但调用者不能靠改请求 ID/参数掩盖同一未知业务效果，通用客户端不宣称可识别这种业务等价性。相同 server ID 改为不同连接目标时身份不同；若恢复旧目标，旧屏障仍生效。其他工具可用于显式只读状态核对，但不能推导屏障已经解除。`begin_turn()` 只允许下一用户轮次重试未派发的连接错误，不能清空未知效果。未知身份加当前派发中身份最多占用 1024 个位置；跨服务器并发也必须先预留位置，成功完成后释放，达到上限后新调用派发前拒绝，不驱逐旧屏障。参数在首次连接等待前深拷贝冻结，路径检查、身份摘要和实际派发使用同一快照。

当前没有通用外部效果核对或人工解除屏障协议。worker 退出会丢失内存屏障；因此这不是持久化 exactly-once 保证，重启后也必须先人工核对远端状态，不能据重启自动重放。

## 限定验收

`tests/test_external_mcp_stdio_contract.py` 使用本地临时 stdio fixture 和实际已安装的 FastMCP Client/MCP 协议。它验证双门禁零启动、按需目录、精确参数/空 roots/环境隔离、前置路径拒绝、业务错误、子进程失联、派发后取消、不跨轮清除屏障、配置撤销、连接关闭与子进程退出。

`tests/test_external_mcp_http_contract.py` 的 38 项使用真实 `127.0.0.1` TCP listener、短命 uvicorn 子进程、已安装 MCP 原生 HTTP/SSE 服务端和实际 FastMCP Client。覆盖门禁零请求、按需 schema / 显式参数、空 roots、前置拒绝、业务错误、真实断线、5 秒 timeout、派发后取消、不重放、目录等待 / 已有 active 连接的配置竞态、URL / 合成 HTTP 头与凭据修订变化、参数冻结、容量和确定未派发的拒连重试。子进程白名单环境是 fixture 的测试卫生，不宣称生产 HTTP 客户端对远程操作系统进行环境隔离。

`tests/test_external_mcp_active_config_contract.py` 的 9 项另使用真实 stdio peer 覆盖 active 替换 / 缓存目录竞态、配置替换清旧错误、关闭期间再换配置、未变配置 sync 不等待在途调用，以及真实关闭后注入 client/transport 普通例外 / `CancelledError` 的本地清理边界。后四项合成 close fault 不冒充真实网络故障；HTTP 断线 / timeout 回收缺陷另有实际网络失败和修复后证据。

这些验收不调用真实第三方服务、模型 provider、系统真实凭据或付费 API；不证明外部程序可信、不能访问用户文件或所有媒体/token 协议均兼容。AI-03 可推进本地 stdio、HTTP 和 SSE 的限定传输阶段；真实第三方服务、TLS / SSO 与重启后持久化恢复仍需独立授权与证据。本轮边界与报告（历史验证记录，未随公开源码发布） 明确列出命令、当前源码哈希、失败与修复结果。

当前安装的 SDK 自动选择 modern stdio 协议，其服务端不能发起 roots/list back-channel；fixture 明确返回 `roots_back_channel=false`，不把未取得响应伪装为端到端 roots 回执。空 roots 的保证另由实际客户端配置的回调直接验证；工具参数、环境和进程生命周期则通过真实子进程往返验证。
