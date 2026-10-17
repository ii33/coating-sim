# 看板娘使用说明

2026-10-11 入口边界梳理见 [能力矩阵](entrypoint-capabilities.md) 与 [共享服务契约](entrypoint-service-contract.md)。看板娘继续使用当前 ProjectGateway 权限、模式、去重和未知副作用账本；选定 MCP 工具复用纯服务层，不通过操纵窗口调用核心算法。共享目录不会扩大本轮允许工具，也不启用膜系优化、外观渲染或研究库的新权限。

更新日期：2026-10-11。

看板娘使用设置中的 OpenAI 兼容服务，通过本项目接口读取设计、计算膜厚和生成离线图表。它不连接真实镀膜设备。工作规则见 [agent.md](../agent.md)，算法与近似见[离线算法说明](algorithms.html)。

## 启用与设置

从启动器打开“设置”，填写并保存服务商地址、当前模型、密钥，再开启看板娘。地址可保存、加载、删除；API 密钥通过 keyring 存于系统安全凭据库，并按规范化 API 地址绑定，切换地址不复用其他地址的密钥。无安全凭据后端时保存明确报错，不降级为明文。

网络客户端保留系统代理配置，依赖包含 SOCKS 所需 `socksio`。启动异常会记录脱敏后的具体原因；缺少代理组件与缺少整个助手运行时会分别提示，不通过关闭代理绕开配置。

When the assistant is enabled, window open/reopen reads the address-bound credential and model-cache snapshot on a worker, then schedules one delayed model-list check if the saved key exists and the cache is stale. When disabled, this bootstrap waits until the advanced settings category is opened. The first visible frame does not wait for keyring or cache-file I/O.

### 设置页凭据与模型快照异步契约

- SettingsBridge owns the active settings, bootstrap generation and UI status. ModelDiscoveryBus owns shared model/capability cache entries and probe reservations. QML controls own only unsaved drafts.
- A worker reads keyring state and the model-cache file. It returns key presence, a credential fingerprint, validated cache entries, an error flag, and the secret only to private Python bridge memory for the authorized GET. The secret is never exposed to QML or logs.
- Opening an enabled settings window, changing the API address, or saving a credential schedules a delayed recheck. Old results are discarded when their generation or address is stale; closing the window invalidates pending UI delivery and cancels an active probe.
- Automatic discovery requires the assistant to be enabled, a saved key, an eligible provider profile, and stale model metadata. It sends at most one GET to the provider model-list endpoint; it never probes reasoning options or sends candidate completion POSTs.
- Manual Refresh models bypasses the cache TTL. If the endpoint does not provide a usable model list, the existing bounded candidate checks may send completion POSTs and incur provider charges.

推理探测先用非法值确认服务确实校验参数，再枚举原生值。界面直接显示所有确认过的原生档位，不再压成低/中/高三档；原生档位已成立时不额外拼接 thinking 开关。只支持 `thinking.type` 时显示“开启思考/关闭思考”。服务器接受参数不证明推理质量变化；忽略参数或证据不足时仅保留自动。详情显示各请求证据和用量。

探测可取消，每个请求最多 0.032 K 输出 Token、15 秒，总计最多 120 秒。手动模型刷新先发送 GET；模型目录端点没有返回可用列表时，可能会按上文策略发送有界候选短聊天请求，可能计费。推理测试仅由用户点击“测试推理档位（消耗额度）”发起，最多 14 次 POST（含兼容重试），可能计费。鉴权、权限、限流错误会停止测试。每次 POST 使用全局 Token 配额：预留估计输入和输出上限，再按服务实报结算；用量未知时保守记账，明确拒绝且无用量的请求不扣估计量。0.032 K Token 不保证足够生成可见文字。详见[探测流程](algorithms.html#assistant-model-probing)。

对话、附件和必要上下文会发送到所选服务。上下文设置按服务商规范化 API 地址与区分大小写的完整模型 ID 保存：上下文窗口 C、输出预留 O、压缩预留 R、压缩后保留历史 H、自动压缩阈值 p；C/O/R/H 以 K 编辑（1 K = 1000 Token），p 为百分比，内部保存整数 Token。空白或 0 表示自动；目录没有精确模型记录时容量保持未知。“模型上下文与压缩”预算分组默认折叠，展开后显示五项设置及只读的有效输入 B/触发 T 预览；刷新模型列表不覆盖预算。旧版最大输入作为隐藏附加硬限保留。显式重置当前模型预算时保留有效的旧版独立 `max_input`；仅当它不是 0–2,000,000 范围内的整数（布尔值不算整数）时，才将原始值及 `migration_error` 归档到 `legacy_values`、把活动 `max_input` 置为 0，并清除活动迁移错误，以解除五个字段无法修复的隐藏错误。首次迁移或 v2 迁移未完成、且 `assistant_model_limits_legacy` 尚不存在时，迁移器将原始 `assistant_model_limits` 整张映射深拷贝保存到该备份；因键规范化而跳过的空白、非法或过长模型键仍可从整张映射恢复，既有备份不覆盖。`legacy_collisions` 保留冲突并继续触发预检拒绝，须人工恢复或移除；五字段 reset 不清除这些冲突。请求前校验已知上下文、最大输入和最大输出硬限；未知容量时 64 K 仅作运行调度回退，不作为硬限。常规主助手和子代理请求使用用户预留输出 O，不静默减半；必要时先安全压缩，压缩后估算输入须在已知 B 内，否则保留历史并报告预算错误。摘要可使用更小的输出上限，但不改变界面触发阈值。详细字段和公式见[上下文预算契约](assistant-context-budget.md)。设置中的 Token 字段按类别可用宽度排成 3、2 或 1 组；窄窗和大字号时在组内上下排列，保留 K 精度和保存行为。
字号立即应用；DPI 缩放需重新打开程序。悬浮图标可拖动到桌面，点击一次显示并激活普通对话窗口，再次点击隐藏；隐藏不停止活动任务。新会话先显示本地自我介绍，不请求模型。

## 对话布局

- 模式和推理强度紧凑排列在输入框下方。
- 用户与看板娘名字高亮，板娘使用正式资源目录中的头像。
- 只有服务返回可见思考文字时显示思考项，默认三行，点击展开/折叠；省去“看板娘 · 思考”标题行，思考文字采用较低亮度，保留复制入口。无推理或未返回文字时不生成空卡片，不解码加密内容。
- 同一任务按模型回合交错展示思考和正文；输入、思考、正文可分别完整复制，复制按钮仅在对应区域鼠标悬停或键盘聚焦时显示，另可复制整轮或整个会话，不受当前页面可见范围限制。
- 主助手、领域子代理和上下文整理默认要求使用系统界面语言，包括服务可显示的思考文字；用户明确指定语言时优先按用户要求。模型服务是否遵循仍由其实际输出决定。
- 流式文字在当前轮显示，工具状态放在当前轮内；轮末显示用时、本轮输入/输出 Token，以及估算标记。
- 消息模型保持稳定，不随每个输出片段重建。停在底部自动跟随；手动查看历史时暂停跟随，回到底部自动恢复。隐藏窗口/切换应用不会被输出强制激活。
- 会话列表可新建、切换、删除。删除消息与恢复入口，不删除已消耗用量、生成文件或底层运行审计。清空开启新上下文。

交流语气可在设置中选择“严肃 / 标准 / 轻快 / 亲密”，默认标准。严肃稳重克制，标准自然有温度；轻快档明显轻松俏皮，可接情绪、玩笑和日常闲聊，吐槽针对事情且不拿用户处境开玩笑，遇到刻意撒娇卖萌可轻松带过；亲密档表达更亲近，情绪回应、闲聊、温和吐槽和小抱怨更多，也可自然配合无害的小要求。亲密表达不虚构真实关系或承诺；涉及真实能力、授权或安全限制时仍说明边界。用户当轮明确的语言、语气、称呼、篇幅或 emoji 偏好优先于档位默认。档位只影响面向用户的表达，不改变事实判断、单位、权限、工具选择或执行策略。

## 输出中继续输入

输出期间输入框和附件增删仍可用。Enter 或“排队”保存当前输入及附件，在本轮正常结束后依次发送；它不会停止正在进行的任务。待发送数可打开紧凑列表，逐条删除。队列属于当前运行进程，不承诺跨重启恢复。

待发送消息可点“撤回”恢复到输入框，并合并附件、保留已有草稿。未应用插话可申请撤回，收到后台确认后恢复；已经应用到模型的插话不能撤回，不会因此停止当前任务。附件合计超限时保留原消息并提示处理。

“插话”把消息加入当前任务，在下一次主助手模型决策点纳入请求；已开始的模型输出或工具操作可先完成，不另起并发任务，也不取消当前任务。插话在当前轮内以高亮用户名显示，并标出接收/应用状态。结束前未吸收的插话转为下一轮待发送。

“停止”单独取消当前任务；停止或出错会暂停已有待发送项，需点击“继续队列”显式恢复。再次直接发送的新消息正常开始新一轮，不自动恢复已停止的任务。强制结束后台进程会终结该次运行，重启后不复活旧任务或重放未知工具副作用；已有工程作业仍应按原编号查询。

讨论（plan mode，内部兼容值chat）只读操作界面、设计和结果，做校验、分析和计划，不执行创建、写入、编辑、开关窗口、估算登记、提交/取消、导出或脚本；外部服务也只提供目录读取。操作用于执行方案与仿真，攻关用于规划和迭代。推理独立选择自动或当前模型确认过的原生档位；未知模型仅开放自动。主助手通过 read_software_manual、project_tools 与 project_action 发现并调用当前模式允许的业务工具，另有归档分页和必要提问入口。操作/攻关可按需委派领域助手。执行前核对UI-owned模式与请求代际；切到讨论或代际变化后，旧运行不能继续写入/放行，切回操作也须新的明确请求。模式不扩大其他授权或路径权限。

工具目录支持一次获取多个工具与设计格式，本轮复用目录，避免逐个重复查询。以下新建、保存、估算、提交、绘图和导出说明仅适用于操作/攻关及已有授权，讨论只读。对话中新生成的设计先暂存于 `data/tmp`，依赖文件一并暂存；只有用户决定正式保存时，才询问名称，可先建议简短设计摘要。`prepare_temporary_design` 准备临时设计，`save_generated_design(kind,path,name,confirmed)` 在用户确认后复制依赖图到正式 kind 目录。`create_default_workflow` 的演示设计也暂存于 `data/tmp/workflows`；默认值标为演示约定。高计算量任务先由 `estimate_compute_task` 估算，再由本机界面依据可信UI模式处理放行：讨论阻断，操作倒计时，攻关按策略放行；模型没有批准入口。确认后写入持久串行队列并立即返回，当前会话继续。桌面队列展示实际任务状态、摘要和可用耗时估算。完成事件绑定原会话，程序独立读取真实终态与结果摘要，助手据此只读汇报，不重复提交；讨论不继续工作流或导出。普通参数/校验错误返回具体字段和恢复建议；执行状态不明的写入须先核对文件或作业，不能盲目重放。

每次仿真尝试（成功、失败或取消）都会在 `data/simulation_history.sqlite3` 留一条摘要。成功时程序自动将热图、NPZ、实际配置和摘要放入 `data/tmp/simulations/<run_id>`；失败或取消只记录请求、状态和错误，不虚构厚度指标。使用 `read_calculation_result` 读取成功结果摘要与现有文件引用；优先复用 `files[].heatmap` 或结果 `files` 中已有图，仅在需要等高线或径向图等额外视图时调用可视化工具。正式保存仿真资产时先询问用户确认及名称，再调用 `save_simulation_asset`；资产为 `.coatasset`，可在仿真界面从 `data/assets` 打开，也可通过 `read_simulation_asset` 分视图读取。

英文思考不保证更快或更省 Token；这里按系统语言要求模型输出可显示的思考，并在每次请求重申。未增加翻译模型调用，也不改写模型原始思考。每轮消耗是所有请求的累计输入和输出，上下文环是最近主助手请求的占用，两者不是同一数值。

## MCP 设置

设置页提供两类入口：配置看板娘连接的外部 MCP 服务，或复制本软件供其他助手使用的 MCP 配置。外部服务支持 stdio、Streamable HTTP 和旧版 SSE；新服务默认停用，连接、工具调用和项目内容许可分别控制。保存后下一轮生效；连接检查由用户显式启动，可取消，不执行业务工具。凭据保存在系统安全库，不写明文项目配置。

本软件默认使用 stdio，复制的配置包含当前环境和服务脚本的绝对路径。可手动启动仅绑定本机的 HTTP 服务，再显式复制该次临时认证配置；默认不监听端口。外部服务接收显式调用参数，不自动接收整段历史、附件或模型密钥。本地外部程序仍以当前账户运行，不能把 MCP 许可误当成脚本沙箱。详细配置、协议、生命周期与范围见 [MCP 接入说明](mcp.md#设置页中的-mcp-接入)。

## 富文本输出与图片光标

流式输出按完整段落保留稳定视图，尚未结束的尾段先显示可选择的原始文字；代码围栏、表格、公式和 SVG 保持整体，闭合后再转为富文本。Markdown 净化、公式和 SVG 转换在单独后台线程完成，结果返回界面时只更新对应段落，当前输出优先于历史格式转换。界面合并约 120 ms 内的输出变化，滚动跟随合并约 48 ms 内的布局变化；这些是界面调度设置，不影响模型速度或计算算法。

已完成的输出和步骤不因后续 Token 重建，复制按钮仍取得完整原文。查看历史时保持原有手动滚动规则，只有位于底端时跟随新增内容。很大的单个表格或代码块完成转换后仍需 Qt 排版，未据此承诺所有长消息都无卡顿。

鼠标悬停在可放大预览的本地图片链接或有效图片卡片时显示放大镜；离开图片后恢复文字或普通链接光标。光标由跨平台 Qt 绘制，不拦截文本选择、滚轮和点击事件。

## 本地文件与多模态

点击附件按钮选择文件，或使用 @{data/.../文件名} 引用 data 文件。输入框支持 Ctrl+V / Cmd+V 粘贴图片，右键“粘贴（含图片）”同样可用；无图片时保留普通文本粘贴、复制、剪切和全选。剪贴板像素转换为 PNG，复制到 data/attachments 后作为普通附件预览、排队或插话；不执行剪贴板 HTML、脚本或地址。剪贴板图片上限 16 M 像素（16×1024²），每条消息最多 8 个附件、合计 32 MiB。外部选择文件也会复制到 data/attachments，模型不获原外部目录访问权限；UTF-8 文字附件最多 64000 字，更大数据由工具分段读取。

支持图片、音频和 PDF 输入，具体取决于模型与兼容服务。当前 Chat 适配不直接传本地视频，需使用提取的图片或音轨；SVG作为文本输入。服务不支持的媒体会报错，不静默丢弃。模型返回的兼容图片/音频保存为 data/tmp/assistant 附件；也支持工具生成的图表和文件。

/audio 可为下一条请求要求兼容服务返回音频（WAV），/text 恢复文字输出；需模型和服务支持 Chat 音频输出。当前实现收到完整音频后展示，不流式播放。没有媒体输出接口的模型仍可生成受控脚本图表。

Markdown 表格、代码、文件/网页链接及白名单彩色文字可直接显示。\(...\)、\[...\]、$...$、$$...$$ 和 math/latex/tex 代码围栏中的公式用离线 Mathtext 子集渲染；它不是完整 LaTeX，不支持的语法保留文字。授权本地图片及被动 SVG 可在对话中显示并点击预览；SVG 净化后用 QtSvg 转为 PNG。

内置预览限定四类：图片/SVG、普通文字、Markdown/HTML 富文本和 CSV/TSV 表格。文字最多读取 512 KiB，表格最多 500 行 × 50 列，超出时明确提示。富文本净化后展示，不执行脚本或远程资源；远程图片只提供链接。PDF、音视频及其他格式交给系统默认程序，失败会提示。附件预览仅接受 data 内或显式导入的文件；不会加载 Qt PDF、多媒体或浏览器插件。各平台的外部格式支持取决于本机文件关联。

JSON、日志和代码使用固定的普通文本预览，保持原始文字，不先经过富文本再转换格式。Markdown 报告的相对文件/图片引用以该报告所在目录为基准，目标仍须是 data 内现存的授权文件；`data/...` 引用保持项目内含义。SVG 使用被动预览，同路径图像更新后按修改时间和大小刷新。脚本产物可能带收集器编号前缀，应使用工具实际返回的 `files[].reference`，不能猜测原脚本中的文件名。

自动产出卡片按现存文件的规范路径识别；重复保存更新同一张卡片，同一会话只在最后引用它的任务中显示当前卡片。不同目录的同名文件仍分别展示，旧消息正文及用户附件保留。不存在、未授权的文件，省略路径和错误文字不生成卡片；代码示例与错误引用段不参与自动识别。卡片表示当前文件，不是每次修改的历史版本。

## 常用命令

输入 @ 显示文件与类别补全：@文件、@机器、@工艺、@源、@基片、@修正板；选定文件以 @{data/...} 插入。输入 / 显示本地命令补全。

| 命令 | 用途 |
| --- | --- |
| /help | 查看命令 |
| /new、/clear | 新会话、清空当前上下文 |
| /stop | 停止任务 |
| /chat、/operate、/research | 切换模式 |
| /settings、/attach | 打开设置、选择附件 |
| /audio、/text | 下一请求的音频/文字输出选择 |

本地命令不消耗模型 Token，实际行为以界面帮助为准。

## 用户数据与脚本图表

[data](../data/README.md) 是看板娘的完整用户数据读写范围。对话新生成设计和仿真产物默认暂存于 `data/tmp`；用户确认正式保存后，设计进入 sources、substrates、masks、machines 或 processes 目录，仿真结果保存为 `.coatasset`。设计支持 description（描述）与 notes（备注），不参与物理计算。原位置文件不自动迁移，桌面保存仍可选择其他目录。

已有仿真优先复用结果 `files[].heatmap` 或其他现成文件；仅需额外等高线、径向图等视图时才用固定的 `visualize_calculation_result` 接口生成 PNG/CSV。自定义数据分析和非标准图表才使用 [Pygal](https://www.pygal.org/en/stable/) 与 run_python_script 生成离线 SVG，Python 标准库处理 CSV/JSON 等。执行环境与项目原 Python 隔离。可信环境准备：

```powershell
.conda\python.exe tools\setup_sandbox.py
```

沙箱直接启动私有 `base/python.exe -I -S -B -X utf8`；Windows venv 的 `Scripts/python.exe` 是会再创建解释器进程的转发器，与单进程限额冲突，因此只保留 venv 的独立包目录供可信引导程序加载。引导程序固定模块路径为私有标准库/DLLs、私有 venv 包和本次工作目录，不执行 `.pth` 或原环境启动钩子。已存在的完整私有环境无需重装；项目目录移动后也按当前相对结构定位。

`python_sandbox_status` 只做静态文件检查，会显示实际缺失路径；`available=true` 不代表已试运行 LPAC。启动失败保留 `runtime_files`、`lpac_create_process` 或 `lpac_runtime_load` 阶段及 Windows/NTSTATUS 错误，区分缺文件、模块加载、策略或启动条件。不能仅凭“系统找不到指定文件”断言未安装。本次已在本机真实LPAC中完成Python/Pygal绘图及定向越界拒绝检查；状态查询本身仍不执行试运行，launch_verified=false仅描述该查询。证据见artifacts/debug/lpac-boundary-report.json，不代表任意扩展库或其他平台已验证。

脚本没有 API 密钥、网络能力或原项目 Python 环境权限。Windows LPAC 每次创建独立配置并清理，仅授权本次任务输入副本、输出目录及只读私有运行时；固定registryRead仅用于必要HKLM只读，仍受用户DACL限制，不能读HKCU或宿主凭据；脚本不能直接访问完整 data。仅显式列出的 data 输入复制进沙箱，安全输出收集到 data/results。Windows 隔离或私有环境不满足时工具报错，禁止普通 Python 回退。

本机Pygal、CSV/JSON图表已验证；_ctypes曾发生DLL初始化失败，未为它放宽隔离，依赖其他本机扩展的脚本仍可能受限。

默认限额：60 s，可设 1–300 s；512 MiB内存、1个进程；最多30个输入/32 MiB；最多40个输出/32 MiB；stdout+stderr最多1 MiB，工作目录大小轮询上限64 MiB。隔离由操作系统执行；工作目录体积检测为轮询，不是磁盘硬配额。限额与技术边界见[算法说明](algorithms.html#sandbox-visualization)。

## 用量与持续任务

对话浮窗默认 860×720 逻辑像素，创建及显示时按屏幕可用区域限制高度和位置，扣除桌面任务栏，不重复乘系统 DPI。

底部环只显示最近主助手请求的输入占用，不被子代理或摘要的小请求覆盖。未有请求显示“—”；实际占用大于 0 而不足 1% 时显示一位小数或“＜1%”，容量未知不显示虚构百分比。点击查看消息、工具、指令、媒体与开销估算，悬停保留真实 K 数值。服务返回 usage 后总输入采用实报，分类仍是估算。环不等于本轮总消耗：轮末 Token 累计主助手、执行助手、分析和摘要请求。

自动整理在估计占用达到 T 时，于安全消息边界压缩运行历史：已知 B 时 T=floor(B×p/100)；B 未知时只用 max(0, 64000−R) 作为调度预算再计算 T。总目标 target=min(floor(B_sched×45/100), max(0, T−1))；H 是压缩后保留的近期原文目标，不从 B 再扣一次。摘要生成输出上限为 min(O, R, target−H, 8192)，必须大于 0。未知有效容量在界面保持“未知”；运行时可用 64 K 调度回退计算触发与目标，但不会用它拒绝请求。工具调用和返回保持配对，近期未读消息、附件及必要的文件和作业编号都参与安全压缩；若压缩后仍超过已知 B，保留原历史并报告预算错误。显示的对话、已接收流式文字和账单不会因压缩删除；上下文环可能变小，不表示删除会话；仅显式新会话或清空命令开启新的会话上下文。

统计自动采用 K/M（1 M = 1000000 Token），小于 1 K 的数保留到 0.001 K。会话与每轮显示服务实报缓存读取/创建量；无字段、旧记录或估算请求显示未提供，不当成 0。命中率分母只含明确报告缓存读取的请求输入；明确为 0 也计报告，缓存统计不从输入总量扣除。

每日和滚动时段限额为输入＋输出，0 表示不限，所有会话/模型共用。请求前按档位预留生成预算，不足则不发送。缺少服务计数或请求中断时保守估算并标记，不是精确账单。媒体估算不是供应商分词器，详见[预算公式](algorithms.html#assistant-budget)。

后台进程持续处理任务。停止讨论仅停止当前模型任务，不取消已有后台作业；操作/攻关的显式停止按原请求和仍有效的模式代际清理所属任务，协作取消以最终状态确认。关闭启动器保留已打开工程；关闭工程窗口按各自未保存审查处理，退出全部须通过所有工程确认。高计算量任务先估算，按UI拥有的持久会话模式/代际放行：讨论不登记/提交/放行；操作模式10秒倒计时后按策略放行，可取消；攻关模式默认放行。外部MCP没有真实会话来源时默认人工确认，模型不能自报模式或批准；放行后持久串行入队并立即返回，助手继续对话。队列展示真实任务状态、摘要和可用耗时估算。终态事件在原会话由程序独立只读读取并汇报。Pydantic AI + Harness按模式提供执行循环、委派、计划、上下文整理与完整步骤快照。已停止或强制终结的轮次不会因下一次聊天自动重启；后续请求使用保留的会话信息正常继续对话，不重放未知副作用、不恢复 GPU 内核。后台作业用原编号查询，不因缺少回复就重复提交。

模型请求取消了固定 90 秒总截止：仍在收到服务器数据的长输出可继续。连接上限 20 秒，连续 300 秒没有网络数据才按读取超时报错（写入 30 秒、连接池等待 20 秒）；超时与取消保留已接收内容。活动 Token 预留每 30 秒续约，不改变开始时间和账单日；900 秒无续约的未知请求仍保守估算。

浮标右键菜单提供计算队列、提示音开关和隐藏入口；隐藏后显示系统托盘图标，托盘菜单与浮标菜单相同。队列只显示摘要、预计用时和预计完成时间，不提供进度控件。桌面使用跨平台 Python 入口 `launcher.pyw`；Windows 用项目 `pythonw.exe` 打开，Linux 桌面快捷方式设置 `Terminal=false`，界面统一使用随项目提供的 Noto CJK 字体（OFL 许可）。源码与安装器启动统一使用项目解释器运行 `launcher.py`；无终端任务栏适配继续由 `launcher.pyw` 提供。诊断保留在 `artifacts/logs/desktop.log`（每类日志最多 2 MiB，保留两个轮转备份；完整策略见 `global-assistant-process-contract.md`），后台协议进程使用隐藏的 python.exe 与管道，不用 pythonw 替代通信进程。

## 软件能力边界

[离线软件说明书](software_manual.html) 在关于与帮助中可阅读，助手可用 `read_software_manual` 按章节查询。每次任务都会提供能力总览与不支持项；创建机器前核对参数契约与现有文件，不列出软件没有实现的机型供选择。当前只支持固定太阳轮理想行星盘蒸发几何、膜厚/遮挡分析和有限轮廓优化；不提供任意整机机械设计、溅射/气体输运、设备控制或光学面型仿真。超范围设想必须明确写“不适用于本软件”。

板娘默认自然、简洁地交流，先给结果和可执行动作；不能因语气轻松省略单位、计算边界或失败信息。

界面、模型探测、凭据、二维 CAD 和 PDF 图纸采用 Python/Qt 等跨平台库。现有脚本沙箱仍为 Windows LPAC 后端；其他系统缺少安全后端时明确禁用脚本，绝不直接执行未隔离代码。DirectML 是 Windows 可选计算后端，不是界面依赖。

接口、路径与权限详见[MCP 文档](mcp.md)。


## 源码开发诊断

源码版在设置中启用开发模式后，启动器提供经过认证的本地看板娘调试接口，可直接提交消息、插话、撤回、取消指定任务，分页读取对话与事件。它调用真实桌面助手后台，不是模拟回复；不提供密钥或通用终端。接口只绑定回环地址，发行构建禁用，每个请求再次核对开发开关。

核心服务错误通过 `ServiceError` 提供稳定的 `service.code`、单次 `occurrence_id` 与来源位置；错误被格式化或序列化时尽力写入脱敏 SQLite `data/tmp/diagnostics/errors.sqlite3`，最多保留 2000 条。此机制覆盖核心服务错误记录路径，不代表整个项目所有异常都已统一捕获。开发模式另有环境问题账本：助手遇到实际启动、依赖、DLL、文件、权限或服务连接问题时自动记录，不额外请求模型。权威记录位于 `data/tmp/ENVIRONMENT_ISSUES.sqlite3`，最近 200 项投影为 `ENVIRONMENT_ISSUES.json` 和 `ENVIRONMENT_ISSUES.md`，供板娘按需查询。重复问题累计次数，保留首次/末次时间、会话/请求、工具阶段、脱敏错误和恢复信息；它记录观察到的错误，不把模型猜测当作已确认根因。脚本调用不存在的 API 单列为用法问题，一般设计校验、缺少业务参数或文件重名不记为环境故障。

日志不包含 API 密钥、完整提示词、脚本、正常输出或调用参数；记录失败不覆盖原工具错误。关闭开发开关或发行构建不启用记录，不扩大文件或沙箱权限。现有 `data/tmp/SESSION_ISSUES.md` 保留，自动记录不覆盖会话总结。

```python
from coat_sim.ui.assistant_debug import AssistantDebugClient
client = AssistantDebugClient()  # 多实例时传 target_pid
print(client.request("status"))
reply = client.request("submit", client_message_id="debug-demo-001",
    prompt="设计一个机器和工艺，跑一下仿真", new_session=True,
    mode="chat", reasoning="low", max_output_tokens=4096,
    request_limit=12, total_tokens_limit=60000)
print(reply)
print(client.request("events", after_id=0, limit=100))
print(client.request("transcript", after_id=0, limit=20))
```

推理档位应使用当前服务已确认的原生值。调试预算由主助手、子代理与整理代理共用，默认单次最多 4096 输出 Token、12 次请求、累计 60 K Token；实报用量在请求结束结算，估算不足时可能先超出累计预算，再阻止下一次请求。相同 `client_message_id`、相同参数在同一调试服务进程中返回原调用回执；变更参数报冲突。进程重启后须先查询会话、文件与作业，不能依赖内存回执防重复。见 [MCP 与本地调试](mcp.md)。

源码运行可直接在设置中启用“开发模式”，默认关闭；保存后下一轮生效，无需额外命令行开关。launcher.pyw --development 和 COAT_DEVELOPMENT=1 保留为源码兼容入口。COAT_RELEASE_BUILD=1、冻结打包或 Nuitka 构建强制关闭开发能力，不能由助手或设置页绕过。

开发诊断提供固定环境检查、Python 语法检查和既有 tests 的 unittest 测试（60 秒上限）；没有通用终端、任意检查参数或任意脚本执行入口。运行实际测试仍应符合用户当前授权。修改人设只允许固定 agent.md：先读取 SHA256，更新时核对相同指纹并备份，下一轮任务生效。

developer_access 提供会话内只读路径授权的 grant / revoke / status；developer_files 对已授权且操作系统允许读取的路径提供分页 read / list / stat。它们用于开发排障，不获得管理员权限、不写入或执行外部文件；授权不扩展 Python 沙箱输入范围。未开启开发诊断的运行及发行版不开放这些接口，人设和语气也不能绕过后端权限。

## 桌面窗口图标

运行中的 Qt 窗口和应用使用正式资源 [logolite.png](../coat_sim/ui/assets/icons/logolite.png)，Windows 任务栏重新启动图标使用同目录的 [logolite.ico](../coat_sim/ui/assets/icons/logolite.ico)。ICO 包含 16、20、24、32、48、64、128、256 像素八种尺寸，由原图保持比例高质量缩放生成；原 PNG 未修改。桌面的启动器快捷方式仍使用 launcher.ico。other 只是临时交接目录，运行时读取正式资源。

Windows 适配位于 coat_sim/ui/taskbar.py：启动时设置 AppUserModelID=com.lyx.coat-sim，普通窗口的属性库依次设置重新启动命令、ICO 路径、显示名，最后设置同一 AppUserModelID。先填写相关属性再设置 ID 的顺序遵循 [Microsoft AppUserModelID 说明](https://learn.microsoft.com/en-us/windows/win32/shell/appids)与 [System.AppUserModel.ID](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-appusermodel-id)；任务栏图标路径格式见 [RelaunchIconResource 官方说明](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-appusermodel-relaunchiconresource)。其他平台继续使用 Qt 图标与应用身份。

源码方式新固定的任务栏入口可通过 pythonw.exe 与 launcher.pyw 回到启动器；发行程序指向自身。已经固定的旧 Python 解释器快捷方式不会由本功能自动改写，需要取消旧固定后重新固定项目窗口。修改后应重启新进程；本轮检查了源码、结构布局和 ICO 条目，未在实际桌面任务栏验证，不据此承诺各系统的图标缓存立即更新。

## 固定工程工作流

多源、多层及修正板组合任务可一次提交 `create_design_workflow` 的结构化方案；程序保存依赖并校验，计算估算按真实会话模式放行后持久入队并立即返回。助手继续会话；完成事件在原会话由程序独立读取终态、结果，并执行已授权的后续导出。运行时不在同一工具调用中等待计算终态。`design_workflow_status` 读状态和事件；经用户授权继续时用 `advance_design_workflow`，取消用 `cancel_design_workflow`。具体假设、采样与工程指标仍须明确，流程通过不代表生产工艺已验证。见[工作流说明](workflows.md)。

开发启动参数 `--development` 或 `COAT_DEVELOPMENT=1` 在源码版可开启能力，设置页显示实际状态；发行门禁优先。自动日志现在包含无法归为环境故障的实际工具能力异常，标明非环境问题。板娘可用 `developer_report_issue` 补报实际观察，`developer_issue_reports(refresh=true)` 生成并返回可点击 Markdown/JSON 文件；`developer_status.issue_reports` 显示日志可读文件与写入失败原因。原 SESSION_ISSUES.md 保留。

沙箱 `coat_plot` 提供热图 `heatmap`、等高线 `contour`、径向桶统计 `radial_profile` 和均值＋极值带 `profile_chart`，保留已有 Pygal 图型。能力与输入格式可由 `python_sandbox_status` 或 `coat_plot.capabilities()` 查询。x/y 为一维坐标，values[y][x]；默认 mm/nm，其他数据应改标签。无效点不补值，径向等样本平均不代表非均匀网格面积平均。

## 膜系工艺与结果图

助手可用 `build_film_stack` 从显式层、周期层、等厚序列或四分之一波长候选程序化生成最多10000层；`create_design_workflow` 可用 `film_stack` 替代 `steps`。厚度表可先短预览再导入；确认 nm/um/angstrom、层序、XLSX工作表，公式先粘贴为值。多模态读取只辅助提取并标记不确定项，不自动OCR。TFCalc/Macleod需先导出文本。60层已有工艺的局部修改用字段patch，不重传全部层。

四分之一波长厚度仅按法向、透明材料和实数折射率n形成候选，不验证反射率/带宽或光学指标。处理口述目标前确认波长、材料n、层序与控制假设。现有计算结果用 `visualize_calculation_result` 生成固定PNG/CSV；默认仅读标量摘要，按需指定结果detail与分页，避免完整网格进入对话。`export_charts=true` 可将图表纳入固定设计工作流导出。技能分流见 `coat_sim/agent/skills/film-stack/SKILL.md` 与 `coat_sim/agent/skills/result-visualization/SKILL.md`。


运行时对讨论、操作、攻关模式按每任务请求数与累计输入量限额，并提供恢复流程；限额不取消已经提交的后台作业。当前文档不据此声称已经实测性能。




## 待决选项与报告未读视图（2026-10-02）

主助手可调用 `ask_user_question` 提问：2–3个短选项，推荐项在第一位，每项给一句利弊，同时接受补充输入。关键问题一直等待用户回答；可默认决定的次要问题可设10–30秒倒计时，默认15秒。开始填写补充或点选选项会暂停倒计时，需手动提交。自动选择明确标为倒计时默认，不冒充真人回答或提升权限；高计算量放行另走前述模式策略。每任务最多3次弹窗，次要提问间隔至少30秒；优先合并问题，能根据已知约束决定的小事直接处理。

选项窗口关闭只收起待决项，可从浮标或托盘的“查看待决事项”再次打开。会话不在前台时通过气泡提醒，点击进入选项；一般任务成功、失败或被停止也在会话不活跃时提醒，点击回到对话。提示音沿用右键开关。计算队列窗口仅展示任务摘要、预计用时/完成时间和状态，不提供放行审批。图片产物采用144px宽/72px高的小预览；其余产物独立排列为紧凑文件名，没有文件图标和大卡片。

环境问题的数据库保留全部去重证据，Markdown/JSON是最近200类的重新生成视图，不是把整份文件重复追加。`developer_issue_reports` 默认返回读取者未读的10条短摘要（错误最多400字符）并标记返回版本已读；不同会话/读取者独立，同一问题再次发生或新增恢复观察会重新变为未读。下一页继续用offset=0。`acknowledge=false`只查看，`unread_only=false`回看旧记录，`details=true`读详细证据，`refresh=true`重新生成文件。已读不等于已解决，历史不会清空；工具仍会返回原报告引用，助手应按需读取，避免将累计全文反复放入上下文。

修正板优化的结果保存路径已修正：仿真审计快照用run_id，队列作业用job_id；结果保存不再错误调用作业状态更新。历史失败不会自动重跑，已有NPZ仍可用原run_id查询结果。此处只说明代码修复和现存文件，不宣称已重新执行优化验收。


## 任务管理器进程身份

Windows 桌面入口 `launcher.pyw` 会在本地生成并使用 `.conda/沉积仿真.exe`：由同目录 pythonw.exe 复制，只更新副本图标和版本资源。FileDescription、ProductName 和进程映像名分别为“沉积仿真”、“沉积仿真”和“沉积仿真.exe”；正式 logolite.ico 写入 EXE。独立蒸发源、基片窗口分别使用“蒸发源.exe”和“基片.exe”，独立启动其他模块时按模块名称生成宿主。角色为白名单，源解释器、DLL、许可和 Qt/Python 技术栈保持原样。构建器为 `tools/build_desktop_host.py`，环境或图标变化后重新生成。资源已读回核对；运行中的旧进程须关闭重开才能切换宿主。此开发副本未签名；发行应在最终资源写入后走项目自己的打包签名流程。macOS/Linux 保持原桌面入口和 Qt 应用身份。

窗口标题使用各窗口自己的名称，不再追加统一的软件名称后缀。设置中的应用身份保持 `coat-sim`；显式空的 Qt `applicationDisplayName` 关闭平台标题拼接。独立模块具有不同的 Windows AppUserModelID，共享进程中的窗口仍属于同一个应用进程。[Qt 标题拼接实现](https://github.com/qt/qtbase/blob/6.8/src/gui/kernel/qplatformwindow.cpp)、[Microsoft 应用身份与分组说明](https://learn.microsoft.com/en-us/windows/win32/shell/appids)。

### 全部窗口闲置内存观察（2026-10-02）

用户打开全部窗口后，只读枚举确认 11 个可见窗口属于 3 个进程：启动器及多数界面共用主进程，蒸发源和基片各一个进程。一次采样的私有工作集如下（MiB = 1024² 字节，非持续监测）：

| 进程 | 私有工作集 / MiB |
| --- | ---: |
| 主界面进程 | 812.4 |
| 蒸发源 | 245.8 |
| 基片 | 248.4 |
| 合计 | 1306.6 |

该读数与任务管理器约 1.3 GB 的空闲占用相符；工作集总和（包含共享页）约 1656.4 MiB，是另一种统计口径。已加载模块中未发现 Torch、DirectML 或 CUDA 计算库；AMD 图形驱动、Qt 和 NumPy 已加载。GPU 计数器同时报告主进程约 775.8 MiB 的图形提交量，但提交量不等于驻留量，也不能直接与私有工作集相加。一次快照不足以证明内存泄漏或确定具体堆分配来源；后续优化应测量窗口开关后的增长趋势、隐藏窗口的图形资源保留、字体/图像缓存和窗口销毁时的释放。[Qt 图形资源释放说明](https://doc.qt.io/qt-6/qquickwindow.html#releaseResources)。

## 对话历史滚动与会话切换（2026-10-11）

- `AssistantBridge` 与 `ConversationModel` 仍是消息数据和异步会话读取的状态所有者；聊天窗口的 `ListView.contentY` 与 `followOutput` 是当前视口状态所有者。窗口在内存中按会话保存可见行锚点、视口偏移和是否跟随末尾，不写入会话数据库。
- 既有普通已完成行缓存最多保留 512 条；仅当消息及元数据完全相同且仍属于原会话时复用。已加载会话投影另按下述三会话缓存保留文档和步骤模型；正文变化只更新对应文档，流式尾部沿用局部刷新路径。
- 历史滚动条固定使用 48 个逻辑像素的滑块，按 ListView 当前内容范围与 `contentY` 映射位置；轨道点击翻页，拖动滑块定位，键盘、滚轮和辅助技术动作仍操作同一个历史列表。虚拟化造成内容高度更新时合并同步，不在滚动过程中混用实测末行高度与估算总高度。
- 切换会话前先保存当前可见行锚点；返回会话时等目标行布局后恢复锚点和偏移。原会话处于跟随末尾状态时继续跟随，新会话默认显示最新内容。
- 阅读位置恢复只操作当前稳定消息键与视口偏移。目标行第一次实例化不等于布局完成；局部文档块高度在异步排版后变化时，恢复状态应继续维持该偏移，直到有界布局收敛或用户的新滚动、会话切换、隐藏/关闭取消恢复。不能通过放宽偏移误差或等待全历史排版实现恢复。
- 恢复过程中合并目标行 y/height 与总内容高度变化的纠偏；仅既有 80ms 定时采样确认相邻几何稳定且偏移误差不超过 1 个逻辑像素时结束，最多 8 次定时采样和 128 次即时纠偏。复用池中的委托或消息键不符的委托不能提供恢复坐标。这些阈值为本项目 UI 参数约定。
- 恢复尚未完成时，用户主动滚轮、拖动、键盘滚动或跳至底部会取消待恢复锚点；旧异步会话读取结果仍由 generation 丢弃。
- 看板娘入口对隐藏或最小化的聊天窗口执行显示、恢复、置前及激活请求；聊天窗口已是前台活动窗口时再次点击才隐藏。窗口不设置持久置顶标志；最终是否能切到前台仍遵从操作系统窗口管理策略。
- 长历史（超过 48 条存储消息）首次显示最新 24 条；只有向上接近已加载历史的顶部，或显式请求较旧历史，才构建下一批最多 32 条。`AssistantBridge` 拥有当前会话消息快照、已加载范围和读取/投影代次；`ConversationModel` 拥有已发布行。QML 拥有视口、焦点、选择范围、消息键锚点及视口偏移，不持有另一份消息数据真值。
- `hasOlderHistory: bool` 表示存在尚未投影的旧消息；`historyProjectionPending: bool` 只表示当前一批正在加载，不因仍有旧消息而持续为真。`loadOlderHistory() -> bool` 仅接受当前会话下一批请求，已加载完、正在加载、关闭或请求无效时返回假，不重复插入。最新消息和当前输出始终进入尾部，正常刷新不重新投影未加载的旧历史。
- `historyLoading: bool` 与 `historyError: str` 通过 `historyProjectionChanged` 通知首次读取和分页的加载/失败状态；`retryHistoryLoad() -> bool` 仅重试仍属于当前会话的失败读取或批次。成功清除错误，失败保留已经发布的消息和锚点，重复、过期或关闭后的重试返回假。
- `visibleTurnIndex(key: str) -> int` 只查询已发布模型，未找到返回 -1。`rememberHistoryAnchor(key: str)` 记录当前会话已发布行的稳定键，供聊天历史缓存保留包含当前阅读位置的连续已加载后缀；空字符串清除旧锚点，未发布的非空键不改变缓存边界，调用不触发加载或模型刷新。`historyProjectionBatchAboutToInsert(count: int)` 在前置插入前同步通知窗口捕获稳定消息键与偏移；既有 `historyProjectionBatchInserted()` 在插入后通知布局和滚动条同步。恢复锚点不得依赖插入后已改变的行号。用户新的滚轮、拖动、键盘或跳到底部操作优先于待执行恢复。
- 已加载视图缓存保留最近 3 个会话。通常保留最近 512 行；正在阅读旧历史时，扩展为锚点前最多 128 行至真实尾部的连续已加载后缀，故深锚点缓存可能超过 512 行。返回仍在缓存中的会话可恢复稳定键和偏移，不重新投影这些旧页；缓存逐出的会话重新显示最新一页，不将失效锚点的行号套到另一条消息上。这些缓存数量与范围是本项目 UI 参数约定，不是数据库保留上限。
- 首次读取显示加载状态；空会话显示原有本地介绍。向上加载时保留现有消息；失败显示可理解的错误并允许重试，不把失败当空数据，也不回退为全量历史解析。会话切换、清空和关闭使存储及投影代次失效，迟到批次不能改变新会话。富文本任务还校验文档实例及修订，防止同键文档重建后接受旧实例回调。
- `copyTurn(key)`、`copyConversation()` 保留完整原文、思考、步骤、插话和附件引用，包括未显示的旧消息；复制不取消分页、不更改已加载范围、不重置可见模型。选择按文档/块的稳定键保存，虚拟化卸载和历史前置插入后仍可恢复选择并复制。切换/清空会话清理旧会话的活动选择，避免内容混入。
- 附件归属从较新行向较旧行累计，同一附件仍由最新助手行显示。正文文档按可见行请求，富文本转换与 QTextDocument 排版限定在视口附近的块；已加载步骤可持有轻量文档模型。保留上述会话缓存和既有附件引用缓存上限，流式尾部更新不重新解析已完成历史。滚动条为窗口中固定的单一实例，48 个逻辑像素滑块及轨道、拖动、键盘、滚轮和辅助技术操作保持一致。
- ListView 委托进入复用池时保留轻量文档、块模型和已测量或估算的正高度占位，只停用块的 TextEdit Loader；不能把整个块模型清空后使委托高度塌缩。复用时按新消息键恢复选择和块高度，离屏排版控件不得因池内文档元数据仍在而保持活动。
- 最新 24 条、较旧最多 32 条、48 条触发阈值是本项目 UI 参数约定。历史读取仍使用既有会话消息快照；本次按需加载限定 UI 投影和排版，不改变数据库格式、模型上下文或 Token 压缩。加载、布局、转换、滚动及流式延迟使用纯合成夹具在同一环境前后比较，不从静态检查推断流畅度。

- 历史投影期间提交的用户消息、流式助手输出和完成或取消的回复合并到可见尾部；切换会话先使旧投影代次失效，再读取目标会话。
- `first_body_data_available_ms` 表示可见委托取得正文数据的时间；真实显示帧呈现和硬件滚动体验需另行测量。
- 本轮合成验收、源码冻结哈希、功能回归和性能边界见 聊天滚动验收记录（历史验证记录，未随公开源码发布）。最终三对原路径复测中，60000 字符大跨度滚动心跳 p95 为 86.474–94.812ms、max 为 111.043–117.531ms；富文本 240 条 p95 为 50.580–53.865ms，旧版为 43.141–60.019ms，中位仍高 6.998ms，观察范围不是统计学置信区间。独立连续 API 路径实际移动 4800px，138 个间隔 p95/max 为 25.508/36.291ms，无 ≥100ms 间隔，但可视槽逐帧轨迹有限。旧版流式有迟到绑定异常，原始数值、告警与早期较差结果全部保留。离屏软件心跳与 frameSwapped 通知不代表物理显示帧率，也不能把大范围跳转或短段低延迟解释为长对话全面流畅。原 22 项功能回归及最终重跑的证据链接同列于验收记录。

## 悬浮助手图标窗口契约（2026-10-11）

- `AssistantController` 持有浮标与聊天窗的 QML 窗口引用。浮标是独立顶层 `Qt.Tool` 窗口；实现不设置 `QWindow.parent`（其值为 `None`），并将 `transientParent` 显式设为空。launcher/业务窗不是浮标的父窗口或 transient parent，Windows 下预期 native `GW_OWNER` 为空。浮标保持在普通窗口之上且不接受输入焦点，展示或点击浮标不会把焦点从当前聊天窗抢走。
- QML 浮标左键调用 `assistant.toggleChat()`。`AssistantController` 是聊天窗显示状态的唯一所有者：聊天窗当前可见、非最小化且活动时隐藏；其余情况通过现有显示路径唤起聊天窗并请求激活。右键菜单仍使用 `Popup.Window` 且声明 `focus: true`；隔离探针确认菜单可见、Esc 能关闭菜单。
- 右键打开菜单后，在 onOpened 中延后请求仍可见的菜单内容窗（contentItem.Window.window）激活；菜单已关闭时不再请求激活，浮标窗口不会被激活。
- `DesktopAssistantBridge` 持有“隐藏浮标”状态；隐藏会调用 controller 隐藏浮标和聊天窗，再次显示只在助手已启用时显示浮标。禁用或隐藏不把浮标重挂到 launcher，也不改变未完成聊天任务的取消语义。
- `QQmlComponent` 装载/创建失败或头像文件缺失时，`_create_window` / `_ensure_windows` 抛出 `RuntimeError`；`reload_configuration()` 捕获它并在显示浮标前返回。`DesktopAssistantBridge.showAssistant()` 调用 `_ensure_windows()` 时没有本地捕获；launcher 的 `launchError` 只处理从 controller 挂载流程逸出的异常。浮标点击和显隐没有异步结果；关闭 controller 时先停止聊天 bridge、隐藏窗口并安排 `deleteLater()`，聊天任务取消仍由 bridge 的既有停止路径管理。

## 研究库只读检索（2026-10-11）

研究库通过 coat_sim/agent/research_library.py 的 ResearchLibraryProvider 接口接入。当前活动路由（2026-10-11）为 config/assistant_research_library.json：默认设置中的 assistant_research_library_root 为空时，worker 每个请求读取这个固定 UTF-8 JSON sidecar（最多 8192 字节）；非空显式 root 优先。pending sidecar 已在校验后通过同目录不覆盖重命名切换为 active。活动 root 固定为 C:\a\wk\coat\deposition_sim\snapshots\research-library\core-f1e4dc591946\files，仍使用原有 reparse 检查、resolve(strict=True) 和 canonical containment。元数据标记 initial_partial，commit f1e4dc591946a50d02576780812a5f06b60a87a5，100/2651 个文件纳入、2551 个 omitted；实际 worker 状态为 connected_read_only_partial。本次在已纳入的 100 个文件内执行 ITO bounded search：首次 direct provider 扫描因 time_limit 报告 incomplete=true/truncated_scan=true；后续 worker 扫描报告 incomplete=false/truncated_scan=false。这不扩大快照覆盖范围，coverage_note 仍说明剩余 2551 个 blob 不在搜索范围。

- provider 只搜索和读取 Markdown、MDX、TXT、RST 和 HTML 文档，文本按 UTF-8 解码（允许 UTF-8 BOM）。每次搜索最多读取 1000 个文件、5000 个目录、20000 个目录项或 32 MiB，另有 3 秒扫描预算；单文件最多 1 MiB。遇到预算上限时返回 `truncated_scan` 与 `truncation_reason`；`has_more` 只表示已扫描结果中还有下一页，不能据此推断未扫描区域。根目录不可读返回固定错误，子目录或单文件不可读会把扫描标记为不完整。
- provider 检查根路径及逐级路径中的 reparse point（含 Windows `st_file_attributes & 0x400`），并验证解析后仍位于根目录内；拒绝 UNC 路径。文件读取有字节硬上限，以覆盖 stat 后文件增长的情况。搜索和读取不写入本地目录。
- 标题最多 240 字符、章节最多 160 字符、文档版本最多 100 字符、原始 URL 最多 400 字符。只有文档中明确的 `source_url`、`original_url` 或 `Original URL` 字段可填充 `original_url`；超长 URL 省略，普通正文链接不作为权威来源。快照提交号单独通过 `snapshot_revision` 标示，不混作文档自身版本。sidecar schema 限定 `snapshot_kind ∈ {initial_partial, partial, complete}`、7–64 位十六进制 commit `revision`、非负文件计数（included 与 excluded 之和等于 repository）和最多 200 字符的 `coverage_note`；`complete` 必须包含全部仓库文件。
- 已验证快照为 commit `f1e4dc591946a50d02576780812a5f06b60a87a5`，材料化目录 `snapshots/research-library/core-f1e4dc591946`，研究正文仅为其 `files/` 下 100 篇完整文件（1,199,480 字节）；仓库共 2651 个 blob，另外 2551 个不在此摘录范围。根目录 `manifest.json`、`README_FOR_CONSUMER.md` 和 `SHA256SUMS.txt` 是传输元数据，不作为研究正文搜索。ZIP 为 661,914 字节，SHA-256 为 `155aeb4cd89a0c86b9bb41e3b8b35454dc334abe10d8ace736a6b2721e59cbfa`；manifest SHA-256 为 `529f8793e7212c4d097f9dedf208f6e91dffb1b635a656b58da292f34cab5f1f`。归档路径、类型、数量/展开大小、清单、每项正文 SHA-256 和 Git blob SHA-1 已检查，落盘正文又完成逐项 SHA-256 复核。现有证据见快照的 `MATERIALIZATION_EVIDENCE.md` 和 `artifacts/research-library/RESEARCH_ACCESS_REVIEW.md`。当前已验证真实部分库连接；不得描述为完整库，也未验证当前打开的 GUI 进程或真实 LLM 回答。
- worker 是每个任务 provider 状态的唯一所有者；sidecar/config 检查同步完成后开始已有 task 流程，未新增后台同步任务、持久索引或取消边界。研究工具只有在 provider 可用且 `allow_model_context=True` 时注册，用户已授权普通研究摘录用于看板娘回答；返回的文档文本是不可信参考内容，不能覆盖系统或用户指令。接口为 `search(query, offset=0, limit=6)` 与 `read(path, offset=0, max_chars=6000)`：search 返回有界 `results`、已观察的 `total_matches` 及扫描状态；read 返回 `content`、`source`、`offset`、`next_offset`（正文结束时为 null）和 `total_chars`。搜索最多返回 10 个 700 字符正文摘录，必要时另加最多 2 个省略号；读取每页最多 6000 字符。read 的 `offset`、`next_offset` 和 `max_chars` 都按 Python Unicode 字符位置/数量计算。

- provider 的扫描状态需与快照覆盖分开解释：`initial_partial` 表示快照未覆盖完整仓库；`incomplete` 与 `truncated_scan` 同义，表示本次扫描因预算、超过 1 MiB 的文档，或文件/目录路径解析与元数据检查的 OSError 而未完成。`skipped_documents` 仅计入因大小限制跳过的文件及固定原因 `skipped_reasons`（`document_too_large`、`read_grew_too_large`）；无法访问的路径不计作完整扫描，也不计入此大小限制计数。路径 containment 检查产生的 `ValueError` 仍按不安全或越界路径排除，不标记为不可读。`has_more` 只表示已观察到的匹配项还有下一页，不代表未扫描部分存在或不存在匹配项。
- 单文件打开、读取、resolve 或 lstat 的 OSError 通过 `truncation_reason=file_unreadable` 标记；遍历目录的 resolve、is_dir、scandir 等 OSError 通过 `directory_unreadable` 标记；root scandir OSError 仍返回 `library_unreadable`。这两类异常分支按已有 `truncation_reason or ...` 规则保留之前记录的原因；既有 `time_limit` 分支行为沿用。预期 containment `ValueError` 会安全排除路径，不会单独导致不完整。


## 当前工程界面与算法参考（2026-10-16）

启动器负责最近工程、新建/打开与全局设置。工程窗口列表打开膜系及沉积资源/输入模块；同工程共享当前膜系会话与修订，不同工程草稿隔离。project_editor_snapshot 通过精确六字段目标只读捕获当前各已存在 owner 的真实状态，返回 instance_id 和 snapshot_id；队列/结果仅提供只读状态或已载入视图缓存。不可用不能用保存版冒充，分页携带 expected_snapshot_id 防混修订。

操作/攻关可对原三膜系窗口沿用原白名单，也可对新增 source、substrate、machine、mask、process、materials、main、ray_trace、film_appearance 九模块按实时 editor_capabilities 做单字段更新或一个 schema 约束的有界动作。每次写前提交原样精确六字段 ProjectTarget（client_id、client_generation、project_id、session_generation、root、project_file）、当前 instance_id，以及同模块最新 snapshot_id 作为 expected_snapshot_id，写后刷新快照。沿用原 GUI 的校验、草稿、dirty/revision、撤销和暂时无效文本语义；原有轻量预览与工作量估计照常。错误/陈旧/超白名单请求不得部分修改状态，未提交原生输入缓冲或待处理模态/对话框阻断写入，接口不代替 GUI 提交或确认。编辑入口不自动保存、开窗、切换资源、导入/导出、替换文档或选择设备/backend，不隐式建立计算放行回执、提交/取消计算或应用候选。新九模块不新增 save；原三膜系仅保留各自原合同明确声明的显式 save。新增范围不支持材料选择、光学数据集编辑/替换及其打开中的嵌套模态；film_optimization 原有受限 set_structure_material_selected 仍按原白名单保留。队列/结果与讨论模式保持只读，计算任务估算、真实模式放行、提交和取消继续走独立合同。详见[受控编辑合同](project-editor-operations-contract.md)。

算法选型和参数解释按需读取手册 algorithm_selection、film_optimization、film_optimization_parameters。厚度与结构请求、沉积与光谱算法、设备资格分别判断；当前结构GUI不代表typed厚度MCP已接入结构提交。详见[能力与只读执行合同](assistant-algorithm-capability-contract.md)。
