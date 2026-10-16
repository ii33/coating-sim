# 助手模型协议与媒体请求契约

日期：2026-10-12。对应审计 **AI-01、AI-02**。本轮实现请求前能力门禁，继续使用现有 Chat Completions 适配器；没有实现或验收 Responses 路由。运行结果和交付 hash 另见本任务验证记录。

## 接口与状态所有者

`coat_sim.agent.provider_protocol.validate_request(...)` 检查显式 API 地址、精确模型 ID、本次有效推理参数、完整消息、工具定义和模型设置。代码签名、错误类型和契约测试为接口真值。检查是无副作用操作，不调用模型、读取凭据、建立会话、修改历史或自动转换协议。

`BudgetAndTools.wrap_model_request` 在预留额度和调用模型 handler 前检查。摘要、主助手和执行助手均经过这一请求边界；工具目录发现并不授权执行。`MediaChatModel` 保留现有 Chat 消息、函数工具、工具返回、Ark opaque reasoning、缓存用量和媒体 delta 接口。

唯一对话/配额状态所有者仍为 `AssistantStore` 和现有运行时；工具副作用与后台作业仍归 Gateway/ledger 与 scheduler 管理。能力检查不替代预算、参数校验、文件权限或后台计算放行。

## 精确协议规则

已核实规则只用于规范化后精确匹配的官方 `https://api.openai.com/v1` 与大小写敏感的完整模型 ID。完整 Chat URL 可归一化为该 base URL；其他 host、套餐、代理、聚合器和不同路径不继承此规则。未知组合维持现有兼容行为，其能力和账号权益仍为未验证。

| 模型/请求 | 当前行为 |
| --- | --- |
| `gpt-6-astra`、`gpt-6.1-sol`，无函数工具的文本或已支持媒体请求 | 保留 Chat Completions。 |
| 上述两个模型携带函数工具、结构化输出工具或需继续的工具消息 | 请求前拒绝，提示 `responses_required`；不得静默丢工具或改模型。 |
| `gpt-6-sol`、`gpt-6-luna`，函数工具且本次有效 `reasoning_effort=none` | 保留 Chat 函数工具请求。 |
| 上述两个模型，工具调用配合其他推理参数/供应商默认值 | 请求前拒绝，提示需 Responses；不得将默认推理当作 none。 |
| 原生供应商内建工具 | 本项目普通函数工具与供应商内建工具是两个独立类别；不得从内建工具仅支持 Responses 推导全部供应商函数工具均不支持 Chat。 |

这四个精确组合的推理档位以官方记录为准，不能由大小写变化、前缀猜测或过期探测覆盖；其他组合保留原有有效探测机制。Astra 和 6.1 Sol 不接受 none/minimal；6 Sol 和 Luna 接受 none。推理非 none 时不向这些模型发送 `temperature`、`top_p`、`top_logprobs` 和 Chat `logprobs`。该规则只约束请求参数，不证明当前账号权限或推理效果。

`ProviderProtocolError` 带稳定 `code`、`model` 和可选 `required_protocol`。`responses_required` 表示当前客户端协议不满足本次请求；`unsupported_modality` 表示该精确模型/当前适配路径不支持媒体请求；`request_settings_conflict` 表示原始请求体覆盖与已解析的推理/工具/消息设置冲突。这些错误都不是账号拒绝、模型不存在、预算耗尽或服务故障。错误不包含 API key、完整媒体数据或聊天内容。禁止自动跨供应商重试、改 endpoint、切模型或降级功能。

检查以最终合并后的设置为准，包括输出音频开关。`extra_body` 不能绕过 typed 工具定义、改写已核对的模型或消息；即使 tools 为空或 null，也不能静默覆盖工具列表。非 none 推理的受限采样字段从标准设置及原始请求体同时清除，其他供应商保留原兼容行为。工具 JSON 中的 MIME 描述是文本元数据；仅原生媒体内容参与模态检查。

## 附件和多模态

现有 `build_prompt` 保留每轮最多 8 个附件、合计 32 MiB、文本文件最多 64,000 字及授权 data 路径约束。图片和 PDF 使用 `BinaryContent`；文本文件由应用读取后作为文本发送。视频维持显式拒绝。模型不支持音频输入/输出时，在发送前拒绝音频，不能因开关开启而假设支持。

Chat 图片使用 `image_url` 数据 URL；PDF 使用 `file` 内容块中的 `file_data` 和文件名。官方 Chat 文件输入支持 PDF，因此不能将“Responses 支持更多文件种类”误解为 Chat 不支持 PDF。测试只验证 SDK 生成的请求形状和本地响应消费，不声称供应商已实际接受文件。

媒体 token 是本项目预留估算：图片预留 2,048 Token；其他二进制媒体至少 2,048 Token，并按每 KiB 8 Token 预留。该值用于本地预算，不是供应商 tokenizer，也不是 PDF 页数或音频时长的精确成本。响应使用服务器实际 usage；没有 usage 的中断保留估算标记，不能报告实际为零。

## 预算与取消

本轮沿用 [上下文预算契约](assistant-context-budget.md) 的纯预算计算，不改变容量、输出预留、压缩规则或数值语义。超预算时不发新请求，保留未读消息、附件和完整工具调用/返回配对；未知模型的 64 K 回退仅用于压缩调度，不作为硬输入限。

本地模拟流覆盖首 token 前与收到文本后取消。取消要传播到当前模型请求，已收到文本仍可由现有运行时保留；供应商是否已产生计费不能由取消本地连接判断。停止助手不自动取消已有 scheduler 作业。MCP mutation 已派发后的取消必须记录 `effect=unknown` 并封锁同参数重放；只读取消保持无 mutation 屏障。作业取消回执不等于终态。

## 使用与验收

当前使用 OpenAI Astra/6.1 Sol 的助手工具请求会明确停止并提示 Responses 要求。需要 Chat 函数工具时，只能选择当前已支持该组合的模型与推理设置；对 6 Sol/Luna 需显式 none。应用不会代用户更换模型或降低推理设置。普通无工具 Chat 的可调用性不代表带工具的助手已可用。

本地验收使用合成消息、临时状态、测试密钥和 `httpx2.MockTransport`，不连接付费模型、不读取真实凭据/聊天。运行现有上下文预算和 provider 回归以及新 `tests/test_ai_provider_protocol_contract.py`，实际通过/失败/未测必须分别记录。Responses 流事件、Responses 附件/工具返回、真实 endpoint、账号 entitlement、供应商精确 token 数和桌面端到端流程均不属于本轮已验收范围。

未来实现 Responses 时，应独立覆盖完整消息/工具返回、原生媒体、流式事件、取消、用量和恢复边界；不能仅替换 SDK model 类后宣称已完成协议路由。

## 官方依据

- [GPT-6 API 使用指南](https://developers.openai.com/api/docs/guides/latest-model)：各精确模型的工具协议、推理档位和参数限制。
- [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)、[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)、[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)：模型特定协议与输入模态。
- [OpenAI 文件输入](https://developers.openai.com/api/docs/guides/file-inputs)、[图像与视觉](https://developers.openai.com/api/docs/guides/images-vision)：Chat PDF 与图片内容形状。

附件数量、大小、文本长度、媒体 Token 预留及暂停/恢复编排均为 **本项目约定**。本轮不改变物理计算、几何、采样、优化或结果统计算法；算法文档与主手册由文档组维护。
