# 看板娘上下文预算契约

状态：契约版本 1；容量证据核验日期 2026-10-11。

## 用途与边界

本契约规定设置页、助手运行时和离线模型目录共同使用的上下文预算。它只计算本地配置，不访问供应商、账号凭据或模型接口。供应商公布的容量描述不代表账号已获该模型权限，也不表示实际吞吐或效果经过测试。

设置页在现有“服务商与连接”设置中按服务商 API 地址及模型 ID 精确选择预算。唯一可编辑项为上下文窗口、预留输出、压缩预留、压缩后保留历史、自动压缩阈值；前四项以 K Token 显示，阈值以百分比显示，1 K = 1000 Token。内部和持久化使用整数 Token。压缩保留历史是最近原始历史的 K Token 目标，不从可用输入容量再次扣除；工具调用及返回按完整消息边界保留，边界对齐可使实际保留量略高于目标。

模型容量与硬限来自离线 `provider_catalog_sources.json`，只匹配规范化的 scheme、host、完整 path，以及区分大小写的完整模型 ID。目录版本、核验日期、官方来源与近似说明随记录保存。没有精确记录时保持自动/未知；不得从相似型号、前缀、其他供应商、套餐端点或模型列表推断容量。模型列表刷新不会写入或覆盖预算设置。

## 输入与输出

纯预算函数接受：

- `C`：模型总上下文窗口，未知时为 0；
- `I`：独立最大输入硬限，未知时为 0；
- `Omax`：独立最大输出硬限，未知时为 0；
- `O`：本次请求预留的输出 Token；
- `R`：从适用输入上限中预留给压缩的 Token；
- `H`：压缩后保留原始历史的 Token 目标；
- `p`：自动压缩阈值百分比，范围 1–100。

已知总上下文提供的输入边界为 `C − O`。有效输入容量为：

```text
B = max(0, min(known(C − O), known(I)) − R)
```

`min` 只采用已知边界；一项未知时采用另一项。若两项均未知，结果包含 `known=false`，有效容量显示“未知”。自动压缩的已知模型触发量为 `floor(B × p / 100)`，向下取整以避免越过阈值。运行时为未知模型保留 64 K Token 的本项目保守调度回退；界面须同时标明这是运行回退，不能把它显示成模型容量或有效硬限。

输出预算 `O` 不得超过已知 `Omax`；有效输入不得超过已知独立输入限 `I` 或扣除预留输出后的 `C − O`。`C` 不得高于已知模型上下文窗口。未知上限保持未知，不将 `C` 伪造为 `I + Omax`。持久化配置违反这些约束时，运行时返回明确预算错误且不发起模型请求；不得以 `max(1, ...)` 或静默钳位掩盖无效配置。

主助手与子代理的普通请求在必要的安全压缩之后，须以同一纯函数结果检查估算输入不超过已知 `B`。若近期用户消息、未读内容、工具调用/返回配对或附件仍使请求超出 `B`，应暂停并报告预算错误，保留历史和附件；不得为了凑预算丢弃未读内容或拆开工具配对。压缩摘要请求可以使用单独且较小的摘要生成预算，不按普通请求的 `B` 拒绝；但其输入和输出仍受已知 `C`、`I`、`Omax` 硬限约束。64 K 未知模型回退只决定压缩调度，不作为拒绝大请求的模型硬限。运行时不得暗中把用户预留输出改成 `min(O, C/2)`；无效的 `C`/`O` 组合须明确报错。

## 状态、持久化与模型切换

`SettingsBridge` 持有现有助手设置对象，是预算设置的唯一状态所有者；它沿用现有设置文件和保存边界。模型覆盖按规范化 API 地址与精确模型 ID 保存。模型切换只重新读取静态目录默认和对应覆盖，不修改旧模型记录。刷新模型列表或重启应用不覆盖手工设置。

当前地址/模型的“重置为模型默认”只删除新版五项手工覆盖，立即从离线目录和项目默认规则重新计算该模型设置。旧版最大输入限是独立本地硬限，迁移后保留并作为隐藏附加约束；重置不删除它。旧版最大输出预算、上下文窗口和阈值分别迁移到预留输出、上下文窗口和阈值字段。原始旧值及迁移版本标记保留供恢复审计，但版本标记存在后不能再将旧字段重复当作隐式覆盖。超出已核验硬限的旧值不丢弃，界面标错并要求修正；运行时拒绝使用该记录。
非法旧 `max_input_tokens`（非整数、bool、负数或大于 2,000,000 Token）仅在用户显式执行重置修复时归档原值到 `legacy_values.invalid_max_input_tokens`，并把活动隐藏输入硬限置为 0（未知）；有效旧输入限制继续保留。普通迁移不会自动清零或改写该值。首次迁移或 v2 迁移未完成、且 `assistant_model_limits_legacy` 尚不存在时，迁移器将原始 `assistant_model_limits` 整张映射深拷贝保存到该备份；因键规范化而跳过的空白、非法或过长模型键仍可从整张映射恢复，既有备份不覆盖。`legacy_collisions` 保留冲突并继续触发预检拒绝，须人工恢复或移除；五字段 reset 不清除这些冲突。

五项空白或旧值 0 表示自动。自动值优先使用已核验模型配置；没有可靠默认时标成未知。默认输出、压缩预留、历史目标和阈值均为本项目启发式，不声称经过实测最优。设 `A` 为已知 `C`、`I` 中较小的原始容量；若仅有其一则使用该值，若两者都未知则使用仅供调度的 64,000 Token 回退。设 `Bbase` 为扣除输出预留后、但尚未扣除压缩预留的已知适用容量 `min(known(C − O), known(I))`；无已知边界时用于自动值的调度回退为 64,000 Token。默认值按整数 Token 计算：

```text
O = min(known(Omax), 32,768, max(1, floor(A / 16)))
R = min(8,192, max(1,024, floor(Bbase / 50)), floor(Bbase / 4))
B = max(0, min(known(C − O), known(I)) − R)
T = floor(B * p / 100)
target = min(floor(B * 45 / 100), max(0, T - 1))
H = min(32,768, floor(B / 5), max(0, target - 1))
summaryO = min(O, R, max(0, target - H), 8,192)
p = 70
```

这里 `target_tokens` 是压缩后整份请求输入的目标；`H` 是保留的最近原文 Token 目标，二者不能混用。摘要请求的输出硬预算为 `summaryO`，因此 R 有直接用途，且摘要输出受 O、R、剩余目标空间和 8,192 Token 调度上限共同限制。未知容量时，调度基础取 64,000 Token 后仍按相同 R 公式扣除；例如 R=1,280 时 B=62,720、p=70 时触发量为 43,904。该回退仍不是模型硬输入限。

未知 `Omax` 不作为 `O` 的硬限输入；只有已知硬限参与 `min`。已知容量很小时，默认 `O`、`R`、`H` 还需缩到满足正向输入空间、`H < B` 且 `H < floor(B × p / 100)` 的最大有效整数值；不可满足时报告约束错误，不能暗中改成 1 Token。可见 K 值以 1 K = 1000 Token 保守向下取整，精确 Token 整数仍保存在配置中。32,768 Token 显示为 32.768 K，8,192 Token 显示为 8.192 K。

保存时如五项相互矛盾，设置层拒绝该次保存并指出冲突，不覆盖上一次有效值。取消、关闭或切换模型沿用现有设置页编辑/保存流程；预算计算和模型默认加载是同步纯函数，不产生后台工作，也没有额外取消边界。重置只影响当前地址/模型的五项覆盖。

## 已核验容量来源

下列值仅用于 `provider_catalog_sources.json` 中同一完整地址和同一模型 ID。来源中的 K/M 未明确采用十进制时按十进制保守换算，并在记录中标注。独立硬限未知的项保持 0/未知。

| 目录端点类别 | 精确模型 | 已核验值（Token） | 官方来源 |
| --- | --- | --- | --- |
| DeepSeek 官方 | `deepseek-flash`, `deepseek-v4-pro` | C=1,000,000；Omax=384,000 | [DeepSeek 定价与模型限制](https://api-docs.deepseek.com/quick_start/pricing/) |
| OpenAI `/v1` | `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna` | C=1,050,000；Omax=128,000 | [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| 智谱 BigModel 标准 API | `glm-5.3`, `glm-5.3-flash`, `glm-5.2` | C=1,000,000；Omax=128,000 | [官方模型概览](https://docs.bigmodel.cn/cn/guide/start/model-overview) |
| 阿里云 Model Studio 北京、新加坡 | `qwen3.8-max`, `qwen3.7-plus`, `qwen3.8-flash` | C=1,000,000；I=983,616；Omax=131,072 | [Qwen 3.8 Max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Qwen 3.7 Plus](https://help.aliyun.com/zh/model-studio/qwen3-7-plus)、[Qwen 3.8 Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) |
| Gemini API OpenAI 兼容端点 | `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-pro-preview`, `gemini-3-flash-preview` | I=1,048,576；Omax=65,536；C 未知 | [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[3.5 Flash Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)、[3.1 Pro Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview)、[3 Flash Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview) ；兼容端点：[Gemini OpenAI-compatible API 文档](https://ai.google.dev/gemini-api/docs/openai) |
| MiniMax 中国站、国际站 | `MiniMax-M3` | C=1,000,000；Omax 未知 | [中国站 OpenAI 兼容 API](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[国际站 OpenAI 兼容 API](https://platform.minimax.io/docs/api-reference/text-openai-api) |
| MiniMax 中国站、国际站 | `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`, `MiniMax-M2.5` | C=204,800；Omax 未知 | 同上 |
| Moonshot 中国站、国际站 | `kimi-k3` | C=1,000,000；Omax 未知 | [中国站模型概览](https://platform.kimi.com/docs/api/models-overview)、[国际站模型概览](https://platform.kimi.ai/docs/api/models-overview) |
| Moonshot 中国站、国际站 | `kimi-k2.7-code`, `kimi-k2.6` | C=256,000；Omax 未知 | 同上 |
| GroqCloud | `openai/gpt-oss-120b`, `openai/gpt-oss-20b` | C=131,072；Omax=65,536 | [Groq 模型限制](https://console.groq.com/docs/models) |
| GroqCloud | `qwen/qwen3.8-27b` | C=131,072；Omax=16,384 | 同上 |
| GroqCloud | `llama-3.1-8b-instant` | C=131,072；Omax=131,072 | 同上 |
| GroqCloud | `llama-3.3-70b-versatile` | C=131,072；Omax=32,768 | 同上 |
| Mistral API | `mistral-medium-3-5`, `mistral-small-2603` | C=256,000；Omax 未知 | [Medium 3.5](https://docs.mistral.ai/models/mistral-medium-3-5-26-04)、[Small 4](https://docs.mistral.ai/models/mistral-small-4-0-26-03) |
| Mistral API | `mistral-large-4` | C=1,000,000；Omax 未知 | [Large 4](https://docs.mistral.ai/models/mistral-large-4-0) |
| xAI API | `grok-4.7`, `grok-4.6` | C=500,000；Omax 无独立文本上限 | [xAI Release Notes](https://docs.x.ai/developers/release-notes) |
| xAI API | `grok-4.3` | C=1,000,000；Omax 未知 | [Grok 4.3 模型页](https://docs.x.ai/developers/models/grok-4.3) |

gpt-6.1-sol 的 [官方模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol)注明工具调用需使用 Responses API；Chat Completions 仅支持无工具调用。本表的静态容量证据不代表当前客户端的工具调用协议已验证。

其余目录模型、聚合器以及不同套餐或协议地址暂不填容量，直至有对应端点的官方证据。Groq 目录的 context 记录不改变套餐可用性；模型容量与账户权益是不同信息。各项记录的共同版本为 `context_profile_version=1`、核验日期 `2026-10-11`；模型匹配规则为完整规范化端点 + 大小写敏感精确 ID。

## 实现与验证位置

- 静态证据目录和端点匹配：`coat_sim/ui/provider_catalog_sources.json`、`coat_sim/ui/provider_catalog.py`。
- 规范化、纯预算计算及运行时输出限制：`coat_sim/agent/modelclient.py`、`coat_sim/agent/runtime.py`。
- 唯一持久状态和 QML 设置契约：`coat_sim/ui/settings.py`、`coat_sim/ui/qml/SettingsWindow.qml`。
- 回归覆盖：`tests/test_model_context_budget.py`, `tests/test_context_budget_runtime.py`、`tests/test_settings_context_profiles.py`、`tests/test_settings_context_profiles_qt.py`。

