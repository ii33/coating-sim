# MCP 本地接口

2026-10-11 入口解耦：22 个现有具体工具已委托无 GUI/LLM 的共享能力层，工具名及看板娘权限集合不扩展。[能力矩阵](entrypoint-capabilities.md) 区分可用/受限/缺失；[服务契约](entrypoint-service-contract.md) 说明实时生成目录、JSON CLI、错误码、部分文件副作用与队列取消边界。共享工具的 `ToolError` 保留人类可读错误及机器错误字段，看板娘网关据此沿用与 CLI 相同的 code/message/effect；其余工具保留原错误规则。

## 参数与效果提示（2026-10-18）

所有注册工具在 SDK 类型转换、未知字段移除和业务执行之前，按实际发布的输入 JSON Schema 校验。顶层参数对象统一 `additionalProperties=false`：不支持的 `project_target`、`project_id`、`root` 或拼写错误必须拒绝，不能静默丢弃后写入默认数据根。布尔字段只接受 JSON `true`/`false`，整数字段不接受字符串、布尔或浮点值；整个请求须为有限 JSON，任何深度的 NaN/Infinity 都会被拒绝。校验遵守嵌套 `$ref`、联合类型、范围与枚举；`dict[str, Any]` 领域内容中模式允许的扩展键仍保留给领域校验。共享能力及膜系/外观 typed 校验器继续执行。统一的前置参数错误使用 `invalid_arguments`、`effect=none` 机器标记；其他业务错误仍沿用各工具已有合同。

`list_tools` 同时发布 MCP annotations。共享操作从核心 `POLICIES` 推导效果，其余适配器在 `coat_sim/core/mcp_contracts.py` 显式分类。`read_calculation_result`（可导出）、持久估算和开发诊断中带写入分支的工具不标为无条件只读。写入能力保守标记可能破坏、非幂等；提示不授予权限，不替代路径、覆盖、项目身份、模式放行或原生审核。新增工具必须显式分类；不能仅凭 `read_` 或 `estimate_` 名字推断安全。

## 独立调度服务（协议 v1，2026-10-05）

MCP 的 stdio 只负责工具调用，计算统一提交到独立本机 scheduler；没有进程内计算队列。`calculation_job_status`、`cancel_calculation_job`、`calculation_job_result`、`find_calculation_job` 与 GUI/CLI 使用相同任务 ID/状态。结果返回 artifact ID/大小/SHA-256，数组通过下载接口显式读取；MCP 进程退出不取消已接收任务。授权/估算策略仍由现有应用层管理。启动停止及协议见[独立调度服务](independent-scheduler.md)。


文档更新：2026-10-18。

服务使用本机标准输入输出（stdio）。安装项目 requirements.txt 后，客户端用项目 Python 启动绝对路径 coat_sim/mcp/server.py；MCP 进程不导入桌面 GUI 模块，标准输出仅供 MCP 协议使用。

~~~json
{
  "command": "C:\\a\\wk\\coat\\deposition_sim\\.conda\\python.exe",
  "args": [
    "C:\\a\\wk\\coat\\deposition_sim\\coat_sim\\mcp\\server.py"
  ]
}
~~~

路径按本机安装位置填写。所有自动化方案、依赖、结果和 PDF 路径均受默认 CoatService 约束，只能位于项目目录内。桌面文件选择器允许人工选择其他目录；这不会扩大 MCP 或 UI 控制接口的自动化写入范围。

## 设置页中的 MCP 接入

设置页分别提供“外部 MCP 服务”和“本软件对外 MCP”。两者使用现有 Python MCP SDK，不依赖浏览器或系统专用界面。

### 看板娘连接外部服务

新增/编辑服务时选择 stdio、Streamable HTTP 或旧版 SSE；stdio 的命令与字符串 JSON 参数数组分开填写，不通过 shell 拼接。HTTP 填服务的完整端点，远程地址要求 HTTPS，本机地址可用 HTTP。可设置工作目录、5–600 秒工具超时和一个凭据名称：stdio 为环境变量名，HTTP 为头名称；Authorization 自动补 Bearer 前缀。凭据保存在系统安全库，项目配置只存凭据修订标识。

新服务默认停用，连接开关和工具调用许可分别设置；“允许发送或访问项目内容”也默认关闭。保存后下一轮任务生效，不自动连接。显式“检查连接”在后台启动所填程序或连接服务，读取工具目录后关闭连接，不执行业务工具，并可取消。

主助手只为本次需求按需使用 `external_tools`：空参数只列已启用服务和状态，不连接；指定 `server_id` 只连接该服务并返回简短工具目录；再指定 `tool_name` 才取得单个完整参数模式。讨论模式仅提供此目录读取；操作/攻关按当前授权使用 `external_action(server_id, tool_name, arguments)` 调用。外部工具与 `project_tools/project_action` 分开，连接在助手后台进程内复用，配置变化或后台退出时关闭。连接失败不在同一轮自动重试；调用中断的效果标为未知，须先核对服务状态。

服务只接收显式调用参数，不自动转发历史、附件、项目根目录或模型 API 密钥。未开放项目内容许可时，助手不能复制项目数值/文字到外部服务；项目路径检测是补充检查，无法识别所有复制内容。外部本地程序以当前账户运行，该许可约定不等于操作系统沙箱；外部工具返回和描述不改变项目权限。当前入口为工具调用，不提供资源/提示模板浏览或 OAuth 登录界面。

### 本软件供其他助手调用

“复制 stdio 客户端配置”提供当前环境 Python 和 MCP 服务脚本的绝对路径；Windows 的协议进程使用 python.exe，保持桌面 pythonw.exe 的启动方式。stdio 由外部客户端管理，无需先在设置启动服务，原有设计、计算、结果和路径校验继续生效。

“启动本机 HTTP”显式启动 `127.0.0.1:<端口>/mcp` 的 Streamable HTTP 服务，端口默认 8766，可选 1024–65535，不监听其他网卡。每次启动生成独立临时 Bearer 凭据；“复制 HTTP 客户端配置”显式复制包含该次凭据的配置，凭据不展示在普通状态或项目 JSON 中。停止服务或退出管理进程时清理该凭据；重启后应重新复制。HTTP 客户端配置需所用助手支持 url + headers。活动作业仍应按原编号核对终态，不能因服务停止就重放提交。

实现：`coat_sim/agent/external_mcp.py`（外部连接）、`coat_sim/mcp/desktop_endpoint.py`（回环服务）、`coat_sim/ui/settings.py` 与 `SettingsWindow.qml`（配置）、`coat_sim/ui/mcp_endpoint_process.py`（隐藏后台进程）。本次未连接实际外部服务器或运行 HTTP 验收。

## 内置助手的实际执行入口

MCP 直接接入仍可调用下列业务工具。内置助手的业务发现与执行入口为：read_software_manual 读取软件边界，project_tools(tool_name="") 按需列工具或查询一个完整 input_schema，project_action(tool_name, arguments) 执行当前模式允许的实际 MCP 工具；read_archived_tool_result 分页读取归档，ask_user_question 按会话条件提供。工具目录只列本轮允许的接口，工具模式同时兼容 MCP 的 snake_case 与旧 camelCase；顶层入口数量不代表完整业务能力数量。

讨论（内部兼容值 chat，/plan 与 /chat 均选择此模式）只读当前界面、设计与结果，进行校验、分析和规划；不写入、开关窗口、登记估算、提交/取消、导出、执行脚本、调用外部 action 或恢复旧批准。操作/攻关可按当前授权执行并委派领域助手；project_action 仍通过提交去重账本、估算与模式放行、取消和路径校验，不能因工具目录查询失败就重放保存或计算。UI 模式或代际变更后，旧请求不能继续写入。输入上限、输出预算、容量与全局额度分别检查；K/M 只换显示单位，不改变协议中的整数 Token。详见[讨论与持久放行合同](assistant-compute-discussion-contract.md)及[算法能力合同](assistant-algorithm-capability-contract.md)。

## 工具清单

业务工具按下表分类，具体接口以实时 list_tools 为准；源码版额外注册 5 个受门禁限制的诊断入口，实际调用须本地开发开关已启用，发行构建不开放。

| 用途 | 工具 |
| --- | --- |
| data 数据文件 | list_data_files、read_data_file、write_data_file、make_data_directory、move_data_path、delete_data_path |
| 沙箱脚本与图表 | python_sandbox_status、run_python_script、submit_python_script |
| 软件说明书 | read_software_manual（index/all/章节，固定只读资源） |
| 修正板 CAD / 加工图 | import_mask_dxf、export_mask_dxf、export_mask_drawing |
| 输入契约 | input_schema（source、substrate、mask、machine、process、settings、mask_optimization） |
| 设计文件和依赖 | read_design、validate_design、save_design、resolve_design_dependencies |
| 演示工作流 | create_default_workflow：一次创建源、基片、机器、工艺，并可提交正常后台作业 |
| 几何预览 | preview_design、source_quadrature、convert_gear_geometry、sample_substrate_outline、sample_substrate_surface、planet_center_occlusion_ring |
| 兼容计算入口（同样估算、模式放行、排队） | simulate_single_source、simulate_process_recipe、correction_board_weights、correction_board_field、selected_point_weights |
| 后台计算 | submit_single_source、submit_process_recipe、submit_mask_weights、submit_mask_field、submit_point_weights、submit_python_script、calculation_job_status、wait_calculation_job、find_calculation_job、cancel_calculation_job、calculation_job_result |
| 计算估算、模式放行与队列 | estimate_compute_task、request_compute_confirmation、calculation_queue_snapshot |
| 修正板优化 | mask_optimization_groups、estimate_mask_optimization、submit_mask_optimization |
| 结果及辅助 | read_calculation_result、export_pdf、calculation_history、estimate_calculation、convert_time_fields |
| 工程及窗口 | project_list、project_open、project_editor_snapshot、list_windows、open_window、read_window、update_window、window_action、close_window |

## 设计文件

input_schema 返回 Pydantic 生成的 JSON Schema。模型在 coat_sim/core/schemas.py；settings 可只传覆盖字段，其他字段采用默认值。源文件保存源形状和角分布，源在机器中的位置由机器文件对应的源位次保存。

save_design 校验文件自身和级联依赖，overwrite 必填；false 阻止覆盖已有文件。保存目录须已存在。validate_design 返回 valid、规范化 data 和 errors，可只传 kind/path 校验已保存文件；data/path 同时提供则校验草稿，并以 path 定位相对依赖。工艺 mean/center 的 monitor_slot 可省略或 null，monitor 基准必须提供从 1 开始的监测器位次。preview_design 接受已有 design_path 或未保存 data，且只能指定其中一项；未保存机器/工艺的相对依赖通过 intended_path 指定将来的文件位置。

五类设计可通过 input_schema → validate_design → preview_design → save_design 创建。明确 standalone CLI 编辑器仍按各自旧合同接受 data、fields 或 pointers 后用 window_action 保存。工程 runtime 的受控编辑包括原三膜系 film_design、film_simulation、film_optimization，以及新增 source、substrate、machine、mask、process、materials、main、ray_trace、film_appearance 九模块，仅限实时 capabilities/schema 声明的单字段更新或单个有界原 GUI 动作。写入需要原样精确六字段 ProjectTarget（client_id、client_generation、project_id、session_generation、root、project_file）、当前 instance_id，以及同模块最新 snapshot_id 作为 expected_snapshot_id；队列/结果与讨论模式只读。新九模块不新增保存、资源切换或计算入口，原三膜系显式 save 合同不变。详见[受控编辑合同](project-editor-operations-contract.md)。

### 简单演示任务

`create_default_workflow(client_request_id, name="演示方案", target_thickness_nm=100, rate_nm_s=1, grid_size=81, time_steps=null, time_step_s=null, backend="自动", fast_cycle=true, submit=true)` 创建单点源、水平行星盘、平面圆形基片和单层平均厚度工艺。固定机器默认为 120/36 齿、模数 4 mm、盘心距 312 mm、基片高度 1000 mm；源位置 (300,0,0) mm、基片半径 150 mm；主盘 15 rpm，无修正板和监测器。所有默认值是本项目演示约定，未经过设备标定，不代表生产加工方案。

返回 defaults、临时资产 paths、manifest_file、job/estimate_id/state；讨论模式不调用此工具；操作模式申请十秒倒计时放行，攻关模式按即时策略放行，未放行时返回 awaiting_confirmation。无助手来源的独立外部 MCP 请求须经桌面人工确认。相同请求 ID 和参数恢复原文件及作业仍须符合当前授权；改变参数或修改过原文件则报冲突，不覆盖。此流程由 `coat_sim/workflow.py` 的 SQLite 检查点引擎编排；run 和逐节点事件保存在 `artifacts/workflow_runs.sqlite3`，崩溃后可在授权有效时用相同请求 ID 与参数核对恢复。submit=false 仅创建临时设计，同样不是讨论只读工具。grid_size 可 21–181；未指定步数/步长时按实际工艺时长换算 0.025 s 步长，不能同时指定二者。高级结构使用普通设计接口。按模式放行后自动排队，queued 不表示成功；后台完成事件到达后读取终态和结果。新结果顶层 settings 表示实际生效参数，同时保留 requested_settings；多层源参数以逐层 effective_settings 为准。

## 软件手册与修正板 CAD

`read_software_manual(section="index", max_chars=12000)` 读取发行版固定资源，返回章节 keys；传某章节或 `all` 取正文，最多 24000 字符并标记截断。它不接受文件路径、不扩大文件权限。主助手和执行助手都接收简短能力总览，需要细节时按章节查询。

`import_mask_dxf(path, units="auto", curve_tolerance_mm=0.05, join_tolerance_mm=0.01, base_mask=None)` 只读取 data 内 DXF，返回规范化修正板 `data`、`warnings` 和 `provenance` 转换证据，不保存设计。单位可 auto/mm/inch，base_mask 仅保留现有板高、描述和备注。校验转换结果后用 save_design 保存。

`export_mask_dxf(data, path, overwrite=false)` 导出 data 内毫米单位二维 DXF；`export_mask_drawing(data, path, options=None, overwrite=false)` 生成 PDF 加工参考图。两者默认禁止覆盖。drawing options：paper=A4/A3、title、material、thickness_mm、tolerance_mm、notes、revision、source、curve_tolerance_mm；材料/板厚/公差仅作图纸信息，不改变零厚度遮挡模型。桌面文件选择器可显式选其他目录，MCP 的 CAD 路径严格限定 data。

支持一个外轮廓及孔洞，直线链、多段线、圆弧、椭圆和样条按公差转多边形；非平面、相交/相切/嵌套、轮廓歧义、顶点超限均报错。DXF 保留机架 XY；CAD Z 为 0，机器安装 z_mm 单独保存。PDF 的孔洞包围尺寸与坐标表不冒充精确圆孔直径。不提供 DWG、3D、刀路或制造审核，算法见[CAD 图解](algorithms.html#mask-cad)。

## 窗口与控件

dev.09 的工程窗口使用显式 `project_target`：先 `project_list` 取得完整六字段对象（client_id、client_generation、project_id、session_generation、root、project_file），或用 `project_open(project_file)` 在既有 MCP 授权根内打开指定 `.coatproj`。打开复用独立 runtime 并核验原所有者，进程启动本身不算成功；不从焦点推断工程、不接受端点/token。目标失效后刷新身份，禁止回退其他所有者或创建第二个 writer。完整接口见[显式工程窗口契约](mcp-project-window-targets.md)。

带 target 的 `list_windows/open_window/read_window/close_window` 仅作用于该会话，模块 key 使用现有 PROJECT_MODULES。read_window 只返回窗口/身份状态与 navigation capabilities，不是编辑器快照；open_window 是显式导航，不能借此传入 design_path/data/fields 替换当前草稿。只读 project_editor_snapshot 读取当前已存在 owner 的真实草稿/配置；队列/结果只提供只读状态或已载入视图缓存。分页携带 expected_snapshot_id，不可用时不能改读保存版冒充当前草稿。

工程编辑白名单包括原三膜系与新增 source、substrate、machine、mask、process、materials、main、ray_trace、film_appearance 九模块。精确字段、参数、单位及动作以实时 editor_capabilities 中 editable_fields、field_schema、action_arguments、action_schemas 为准。update 恰有一个字段；action 是一个有界原 GUI 操作，不接受任意 JSON pointer、方法或整体文档替换。写前传原样精确六字段 ProjectTarget（client_id、client_generation、project_id、session_generation、root、project_file）、当前 instance_id，以及同模块最新 snapshot_id 作为 expected_snapshot_id；身份、窗口、owner 或快照不匹配即拒绝，写后刷新 snapshot。

沿用原 GUI 的校验、草稿、dirty/revision、撤销和暂时无效文本语义；原有轻量预览与工作量估计照常。错误/陈旧/超白名单请求不得部分修改状态，未提交原生输入缓冲或待处理模态/对话框阻断写入，接口不代替 GUI 提交或确认。编辑入口不自动保存、开窗、切换资源、导入/导出、替换文档或选择设备/backend，不隐式建立计算放行回执、提交/取消计算或应用候选。新九模块不新增 save；原三膜系仅保留各自原合同明确声明的显式 save。新增范围不支持材料选择、光学数据集编辑/替换及其打开中的嵌套模态；film_optimization 原有受限 set_structure_material_selected 仍按原白名单保留。队列/结果与讨论模式保持只读，计算任务估算、真实模式放行、提交和取消继续走独立合同。详细矩阵见[受控编辑合同](project-editor-operations-contract.md)。headless project_manage 仍受 dirty/editor-owned save 限制，不能作为万能替代。

不带 target 时，普通启动器只公开 launcher/settings/about；业务窗口仅供明确 `COAT_UI_PID` 选择的 standalone CLI host，不能隐式选择工程。无运行全局界面时启动真实轻量启动器。以下文档/控件字段与动作仅描述该 global/explicit standalone 合同。

打开期间返回 state=loading。用 read_window 读取状态；在 state=ready、pending_parameters=false 且没有 initialization_error 后执行后续操作。首次启动的参数会在界面就绪后自动应用。参数应用失败会出现在 initialization_error 中，不会自动保存草稿。

open_window 和 update_window 接受：

- design_path：加载本项目内的设计文件；main 加载工艺文件。
- data：填入完整且已校验的源、基片、机器、修正板或工艺设计。
- intended_path：未保存草稿的未来路径，用于相对依赖解析。
- fields：编辑当前窗口的输入框、选择项或视图控件。
- pointers：机器/修正板文档的 JSON pointer → 新值，例如 /sources/0/x_mm。
- force：显式允许替换有未保存修改的文档，默认 false。

read_window 返回 fields、document、path、dirty、valid、status、editable_fields、editable_pointers（机器/修正板）、controls、actions 与 action_arguments。只有白名单内的属性和既有语义动作可用，不提供任意表达式或任意 QObject 方法调用。数值、几何和依赖校验继续由原有核心服务执行；普通输入编辑可保留暂时无效的草稿，保存时必须通过校验。

| 窗口 | fields / 文档编辑 |
| --- | --- |
| main | carrier_rpm、carrier_phase_deg、planet_phase_deg、time_steps、time_step_s、grid_size、backend、fast_cycle、fast_cycle_residual_percent；design_path 加载工艺 |
| source | device_type、kind、source_exponent 和现有形状尺寸字段 |
| substrate | name、outline_kind、surface_kind、radius_mm、width_mm、height_mm、roc_mm、conic_k、a4、a6、a8；轮廓顶点用完整 data 或点动作 |
| process | name、machine_file、substrate_file；步骤用完整 data 或步骤动作 |
| machine / mask | 完整 data 或 pointers；mask 另有 workpiece_diameter_mm |
| settings | 字体、缩放、助手开关、模型、API 地址、按模型的最大输入/输出与容量、Token 限额、时段长度、图标尺寸；凭证只返回是否已配置 |
| launcher / about | 查看窗口和模块信息 |

修正板还公开 QML 控件：tool_index、shift_x_mm、shift_y_mm、scale_x、scale_y、mask_slot、source_slot、angular_radius_mm、patch_radius_mm、auto_weight_y、weight_y_min、weight_y_max、heat_weight_mode。基片公开 tool_index、selected_point_index、point_x_mm、point_y_mm。控件当前值及范围见 controls；选项索引从 0 开始，源/板位次从 1 开始。修改选择项会触发原有 activated 处理；Y 范围会重绘；角向半径会触发现有延时后台分析。

window_action 只接受 read_window 给出的动作：设计新建/保存、基片点操作、工艺步骤操作、机器依赖及列表操作、修正板变换、设置保存/模型探测及取消等。模型调用 calculate、start_optimization、compute_weights、compute_field、compute_point 会返回 compute_confirmation_required，须走计算估算与模式放行入口。唯一 calculate 兼容例外是未传 project_target 的独立 film_simulation 窗口；该路径不具备 scheduler job_id/取消合同。人工在 GUI 点击计算按钮仍使用原有行为。动作参数见 action_arguments。save 和 export_pdf 要求显式 overwrite 布尔值；新建或替换脏文档要求 force=true。

带 target 的 close_window 进入原生工程窗口关闭流程；force 不跳过草稿、最后窗口 preflight 或工程 Save/Discard/Cancel review，accepted 也不表示异步 review 已完成。轻量 launcher 关闭只影响其本进程及全局窗口，不关闭工程 runtime 或共享助手。explicit standalone 的 dirty/busy/加载窗口仍保留原 force=true 合同；CAD 导入/导出计入 busy。

工程源/基片跟随各自独立 runtime 的明确 session/route，不能用旧隔离模块全局状态替代。explicit standalone CLI 保留原模块进程和明确 COAT_UI_PID 合同。MCP 工程目标始终经共享 registry 的 nonce、PID creation、双 generation、root 和 descriptor 核验后转发。

控制接口仅监听 127.0.0.1，使用每次启动生成的随机认证 token，发现文件位于 artifacts/runtime/ui-<pid>.json。端点和 token 属于运行状态，不应纳入版本管理。所有 UI 读写在所属 Qt 线程执行。

## 计算估算、模式放行与后台队列

所有模型提交的沉积仿真、修正板优化、遮挡权重/热图/选点分析和 Python 沙箱脚本统一走以下流程；估算不会运行物理计算或脚本。不要求看板娘在普通聊天中预测 Token 消耗。

1. `estimate_compute_task(kind, parameters)` 验证参数和依赖，返回 estimate_id、短摘要、work_index/operations、estimated_s、basis、sample_count，并绑定参数和所有引用文件的 SHA-256。
2. `request_compute_confirmation(estimate_id)` 根据 UI 持久记录的真实请求模式及当前会话模式/代际进入放行策略；内置运行时注入真实 session_id/request_id，忽略模型传入的模式判断。讨论（chat）不登记估算或请求放行，即使旧短任务估算不超过60秒也不能批准或提交；操作（operate）倒计时10秒，取消可阻止放行；攻关（research）按即时策略放行。外部 MCP 无助手会话来源时使用独立 manual 人工确认策略，不等同于讨论模式。
3. 人工确认、可信倒计时期满或攻关即时策略后自动提交；参数或文件改变须重新估算；原请求中止、会话删除/请求丢失或回执过期时不能入队。
4. 继续会话，后台完成事件回到原会话并只读核对终态与结果。只有原操作/攻关请求的 UI 模式/代际仍有效且导出已获授权时，程序才继续既有工作流导出节点；讨论不推进工作流，模型无需循环等待。

| kind | parameters 主要字段 |
| --- | --- |
| single_source | settings、backend、machine_path/source_path、source_slot、substrate_path、mask_paths |
| process | settings、process_path、backend、fast_cycle、fast_cycle_residual_percent；time_step_s 可替代 settings.time_steps |
| mask_optimization | settings、process_path、options；time_step_s 可替代 settings.time_steps |
| mask_weights | settings、machine_path、source_slot、mask_slot、mask_data（草稿可选）、substrate_path、radii_mm |
| mask_field | 同径向分析，半径字段为 ring_radii_mm |
| point_weights | settings、machine_path、source_slot、substrate_path、mask_z_mm、point_xy_mm、patch_radius_mm、radii_mm |
| python_script | code、files、timeout_s |

estimated_s=null 表示无可靠同类历史，不能转述为零秒。遮挡分析给出相位、工件点、源采样点与射线边测试上界，尚无墙钟标定。任意脚本计算量/耗时未知，只提供 1–300 s 执行超时上限。优化估算含各组完整工艺候选计算、必要的各组原网格验证以及最后全工艺复算。

所有 submit_* 计算工具支持 estimate_id、client_request_id（1–512 字符）。省略回执仅返回 awaiting_confirmation；未通过真实模式放行策略的回执不能提交。旧名 simulate_*、correction_board_*、selected_point_weights、run_python_script 共用同一后台入口，返回回执或作业摘要，兼容调用者不能再期待同步数组。stdio/HTTP 不暴露 confirmed=true、模式覆盖或批准工具；外部客户端默认须取得本地桌面人工确认。直接人工 GUI 点击保持原按钮行为。

回执固定 request_mode、request_generation、release_policy、release_at（操作请求时刻+10秒，UTC ISO）。未批准的 pending 卡片不因等待时间自动消失；真实请求中止或删除时失效。切换讨论或请求代际过期时，未入队 pending/approved 回执保留为 blocked pending，取消倒计时与旧批准；切回操作不能恢复，须新建明确请求。首次可信放行写 approved_at 与 expires_at（批准后15分钟，本项目约定），重复批准不刷新期限，旧 approved 过期须重新估算。重启不重置有效倒计时。桌面可信计时入口 CoatService.release_due_compute_estimate(estimate_id) 再核对持久政策与时间，早调用拒绝；讨论模式不能通过定时、手动确认或旧回执批准，最终入队也重查模式/代际。接口不注册为 MCP 工具。授权期限只约束入队前，已经 submitted 的作业不因切讨论自动取消，也不会因运行超过15分钟被中断。

### 队列与通知

`calculation_queue_snapshot(after_event=0, limit=100)` 返回 jobs、confirmations、events、cursor、latest_event_id。队列仅显示摘要、estimated_s、expected_finish_at 和状态，不追踪进度；活动任务按入队顺序优先完整显示，历史填剩余名额。身份字段包含 job_id/kind/estimate_id/origin，时间戳为 UTC ISO。

同一项目使用 artifacts/runtime/compute-tasks.sqlite3 共享队列，最多 16 个活动任务，按入队顺序串行执行。执行线程持有可续期跨进程租约，无法续期时协作取消。预计结束时间累加前序剩余估计；前序耗时未知，后续预计结束也为 null，并非承诺的完成期限。

每个作业最多一次 enqueued 和一次 completed 事件，数据库唯一键去重。首次连接用 latest_event_id 跳过旧通知；之后客户端持有自己的 cursor 分页消费。浮标/托盘气泡使用这些事件。已有详细作业状态和优化进度接口独立保留，队列控件不显示进度。

### 状态、取消和恢复

| state | 含义 |
| --- | --- |
| awaiting_confirmation / pending | 仅估算或等待模式放行（人工或倒计时） |
| queued | 已放行，等待执行 |
| running | 实际正在执行 |
| succeeded | 完成，result 为结果/文件摘要 |
| failed | 失败，error 给出原因 |
| cancelled | 已确认取消 |

calculation_job_status 即时查询；calculation_job_result 只在成功后返回摘要。wait_calculation_job 保留给程序客户端，最长 60 s；内置助手提交后不循环调用 wait/status，由后台结束事件唤回。cancel_calculation_job 是协作取消，cancel_requested=true 只是收到请求，终态才表明实际结束。跨进程取消标记由所属线程在核心检查点处理；沙箱取消等待隔离进程真正结束。

同 client_request_id 和相同输入返回原回执/作业，输入不同拒绝；批准后的作业身份绑定 estimate_id，避免桌面与模型使用不同标识重复启动。find_calculation_job 只核对效果，不重放。作业 JSON 原子保存于 artifacts/runtime/jobs，并同步紧凑 SQLite 状态。

确认所属进程退出或 PID 被复用，遗留 queued/running 标 failed/interrupted，发结束事件，不自动重算；无法确认进程身份时保守保留。服务停止、桌面重启或回复丢失不构成重算授权。

工作流等待人工/倒计时放行时停在 submit 节点；攻关模式立即创建持久作业。只有已通过策略的持久作业才能推进。演示恢复：原 client_request_id 与参数再次 create_default_workflow；设计恢复：advance_design_workflow(workflow_run_id)。完成事件按 estimate_id 关联原工作流和会话，不凭同轮文本猜测身份。

## 结果与限制

两类仿真将完整场和坐标保存到 artifacts/results/mcp-<run_id>.npz，回复包含 run_id、文件路径、尺寸、耗时和标量元信息。内存缓存最多 4 次结果。read_calculation_result 默认返回保存的输入和摘要，include_grid=true 返回完整坐标/网格（非有限采样以 null 表示）。export_pdf 可按 run_id 从 NPZ 恢复，支持服务重启后的导出。

工艺 fast_cycle / fast_cycle_residual_percent 使用现有快速周期算法及阈值，物理算法及条件见[算法说明](algorithms.html)。本次窗口控制和作业调度未更改物理算法。

planet_center_occlusion_ring 是固定机器 XY、单位 mm 的源中心到行星盘中心射线截线，本身不是通量权重，也不保证盘心对应最大径向遮挡。correction_board_weights 的 center_is_radial_peak 只比较平面盘当前离散采样，曲面时为 null。

径向分析最多 101 个半径；角向分析为 1–8 个半径；历史查询最多 100 条。UI 单次消息限制 2 MiB。数值请求经 Pydantic 形状检查和核心物理/依赖校验；无效输入返回明确 MCP 工具错误，分析中的不可计算采样保留长度并以 JSON null 返回。

## 计算输入快照

新的异步作业在跨进程查重成功后、入队前，将已校验的设计 JSON 依赖图复制到 artifacts/runtime/job-inputs/<uuid>/，重写机器和工艺引用为同图内的相对路径。每个原绝对路径与设计种类只复制一次，所有原路径和副本均受 CoatService 项目路径边界约束。完成后再校验整个复制图，并将 JSON 标记为只读。相同 client_request_id 重试返回原作业，不重新生成或覆盖快照。

作业的 input_snapshot_file 指向 manifest.json，记录原绝对路径、设计种类、副本路径和实际调用参数。计算、计算工作量与结果中的机器/工艺元信息均使用同一复制图；NPZ 元信息也保存快照清单，成功后保留输入文件以便追溯。对原设计文件的后续编辑不影响已入队计算。两个 simulate_* 兼容入队入口也在计算前使用同一快照机制。

快照保证已复制图的自洽及计算/导出输入一致；原文件可在复制过程中被编辑，不保证原文件编辑者的多文件原子保存。


## 逐层修正板选择

工艺步骤的 `mask_slots` 是机器已安装修正板的 **0 基位次列表**：`[0]` 板1、`[1]` 板2、`[0,1]` 双板、`[]` 不启用修正板。省略时读取全部已安装板；保存后写成明确列表。源与监测器的位次仍从1开始。重复或不存在的板位次阻止保存。快速算法的周期场以 `(source_slot, mask_slots)` 作为缓存键，因此同源但板组合不同的层分别积分。

## 修正板轮廓优化

先用 mask_optimization_groups(process_path) 只读查看实际启用关系。source_slot 从1开始，优化 mask_slots 从0开始；遮挡分析 mask_slot 从1开始，不能混用。默认各源对应一个独立板；兼容旧字段 allow_two_masks=true 允许一个源的任意数量板联合优化。板被不同源共同启用时拒绝独立组优化，须调整实际启用关系。source_slot 可限定源；target_map 必须明确一个源。

总报告 best_verified 是全工艺均匀性，group_reports 保存各组评分，target_map_group_scores 保存单源目标图评分，不拿单源目标计算全工艺 RMSE。

`input_schema("mask_optimization")` 返回 `MaskOptimizationOptions` 的严格模式。先读取完整工艺，再用 `estimate_mask_optimization(settings, process_path, options)` 取得对应设备/算法历史估时。无历史时返回 `estimated_s=null`；历史估计按评分预算累加并计入原网格复算，轮廓变化可能影响耗时。

`submit_mask_optimization` 沿用作业去重、快照、等待、取消及结果恢复接口。`client_request_id` 重试返回原作业；参数不同会报冲突。运行中 `calculation_job_status` 的 `progress` 含 `stage`、`evaluations`、`max_evaluations`、`best_score`、`elapsed_s`、`estimated_remaining_s`。估时由已完成评分的实测中位用时逐步更新。

| 参数 | 默认/含义 |
| --- | --- |
| `mask_slots` | 工艺中启用的全部板；0基，不设板数上限，按机器实际安装数验界 |
| `mask_bounds` | 各板原外轮廓的XY包络；可指定每板 `slot,x_min_mm,x_max_mm,y_min_mm,y_max_mm` |
| `objective.kind` | `uniformity`：累计膜厚CV；`target_map`：与目标图的归一化RMSE |
| `objective.target_field_nm` | 目标图模式必填，二维网格与原始网格一致，单位nm；无效区可null |
| `backend` | 自动；也可CPU、GPU、DirectML |
| `fast_cycle` / `fast_cycle_residual_percent` | false / 5；精确或快速算法及尾段阈值 |
| `search_grid_size` | min(81,原始网格) |
| `max_evaluations` | 每组48次搜索评分，每组上限2000；含基线和初形扫描 |
| `seed_grid_points` / `control_points` | 每轴2个初形位置 / 8个径向控制点 |
| `min_radial_extent_mm` / `min_edge_length_mm` | 0 / 0，几何限制 |
| `max_area_fraction` | 1，板实体面积与设计包络面积之比 |
| `initial_radius_mm` | 短边的10%，按可行尺寸区间夹取 |

首版只优化固定板高的无孔外轮廓，选中有孔板时报错；未选板保持原结构。每次候选以完整工艺模型评分，计入有限源、双板、逐层板选择和真实相位。搜索后的最优轮廓按原始网格复算，额外至多一次仿真。粗网格改进百分比不保证原网格优于原板，不保证全局最优。最小径向/边长不是最小制造线宽，面积比例不是沉积效率；目前无制造校核和实测通量校准。

成功作业的 `result` 保留常规 `run_id/result_file`，附 `optimization_report`、`report_path` 与 `artifacts`。`data/tmp/optimizations/<uuid>/` 保存完整机器、基片、源、板和工艺副本及 `optimization-report.json`，原文件不覆盖。用新 `process_path` 继续仿真或在工艺窗口打开；`run_id` 可读取完整膜厚结果、导出PDF。历史记录只计入最后一次膜厚计算的用时，优化总用时单列在报告中，避免影响一般仿真的设备估时。

原理、图解和参考文献见[算法文档09c](algorithms.html#mask-optimization)。工艺编辑器也提供参数设置、取消和实时进度入口；目标膜厚图模式目前通过服务/MCP使用。

## data 文件与沙箱 Python

五类设计可包含 description/notes，均为最多20000字的文字，不参与计算。新设计默认目录为 data/sources、substrates、masks、machines、processes；MCP设计服务原有项目范围不变。下列通用文件工具严格限制于 data，禁止操作根目录或通过链接越界。

| 工具 | 参数与返回 |
| --- | --- |
| list_data_files | path默认data，recursive默认false，limit默认200/最大1000；返回entries/truncated，不跟随链接 |
| read_data_file | path、offset=0、max_chars=16000/最大64000；UTF-8分页，媒体返回引用；PNG/JPEG/WebP/GIF在8MiB内返回原生MCP图片供视觉模型读取 |
| write_data_file | path/content，overwrite默认false；encoding=text或base64；原子替换，单文件32MiB |
| make_data_directory | path，仅data内 |
| move_data_path | source/destination，不覆盖已有目标、不移入自身 |
| delete_data_path | path，recursive默认false；不能删除data根目录，拒绝含链接的递归删除 |
| python_sandbox_status | 不执行代码；报告私有环境存在情况、Windows要求、限额、脚本输入约定；available不代表已实测隔离 |
| run_python_script | code、files（最多30个data文件）与timeout_s=60（1–300）；返回stdout/stderr、exit_code、elapsed_s、files、warnings、isolation |

脚本使用标准库和Pygal，读取 INPUTS（Path列表）或 INPUT_FILES（data引用→输入副本Path），写 OUTPUT_DIR。辅助模块提供 from coat_plot import chart, save_chart, read_csv。SVG去脚本、外部引用和非被动元素；只收集允许扩展名、普通文件及受限体积。脚本与execution.json保留在data/tmp/assistant/运行编号下，files列出可以预览的产物。

独立运行时在artifacts/sandbox-runtime：自含可信CPython base＋venv，不授权原.conda；运行器每次创建新AppContainer配置及SID，自动清理配置和SID ACL；固定registryRead仅允许必要HKLM只读且受用户DACL交集限制，无网络capability、无环境凭据，仅runtime读取/执行和本轮workspace读写。挂起创建，确认AppContainer令牌及独立LPAC授权边界并加入Job Object后才恢复；失败不降级。硬限512MiB/1进程、输出1MiB，时限轮询；输入/产物合计各32MiB、产物最多40个，workspace轮询64MiB/1000项。取消等待作业进程被杀死，所属宿主退出由kill-on-close结束。Windows OS资源仍是运行所需基础，隔离不是虚拟机。本机真实Pygal绘图与定向文件/网络/子进程/HKCU/测试凭据拒绝已验证（artifacts/debug/lpac-boundary-report.json）；_ctypes导入仍可能受系统限制，未增加额外能力或普通进程回退。本机真实Pygal绘图与定向文件/网络/子进程/HKCU/测试凭据拒绝已验证（artifacts/debug/lpac-boundary-report.json）；_ctypes导入仍可能受系统限制，未增加额外能力或普通进程回退。

示例（不自动执行）：

```python
from coat_plot import chart, save_chart, read_csv
rows = read_csv(INPUTS[0])
figure = chart("line", title="膜厚剖面")
figure.x_labels = [row["radius_mm"] for row in rows]
figure.add("膜厚 / nm", [float(row["thickness_nm"]) for row in rows])
save_chart(figure, "thickness.svg")
```

可信环境安装：项目Python运行tools/setup_sandbox.py。没有环境或OS隔离失败时返回错误，不能通过其他工具普通执行相同脚本。安装已完成的本机环境不纳入Git，重新部署需准备。具体实现与离线图解见[算法说明](algorithms.html#sandbox-visualization)。

已有仿真可用 read_calculation_result(run_id, export_data=true) 将原坐标轴与当前膜厚场原样导出为 data/results/chart-data JSON，返回 files 引用供沙箱分析。字段包含 x/y（mm）、field、field_name/field_unit，无抽样或统计变更；非有限值写null，不覆盖已有文件，单文件仍限32MiB。默认export_data=false，不产生文件；include_grid独立决定是否把大数组送给模型，图表任务应优先文件引用。


## 开发诊断与人设接口

源码版注册 developer_status、developer_checks、developer_persona、developer_access、developer_files；本地设置保存 developer_enabled=true 后下一轮可调用，无需额外启动标记。每次敏感调用再次验证本地开关、COAT_RELEASE_BUILD!=1，且非冻结/Nuitka发行构建。助手不能自行开启本地开发门禁。

| 接口 | 参数及边界 |
| --- | --- |
| developer_status | 读取开发门禁状态、固定能力及临时只读授权状态 |
| developer_access(action, duration_s) | status/grant/revoke；本机只读访问授权默认900秒，可设1–3600秒；仅当前MCP进程，不获取OS管理员身份 |
| developer_files(path, action, offset, limit) | 已授权开发访问下 stat/list/read 任意当前OS用户可读路径；目录每页最多200项，文件默认32768/最多65536字节；UTF-8或base64，next_offset继续；不能写入/执行 |
| developer_checks(check) | environment：依赖版本；syntax：固定项目源码 AST；unit-tests：既有 tests 中 test*.py 的 unittest，最长60秒，无自由命令参数 |
| developer_persona(action, text, expected_sha256) | read 返回固定 agent.md 的正文/指纹；update 仅该文件、非空最多32000 字节，SHA冲突拒绝，先备份后原子写入，下一轮任务生效 |

开发人设不改变后端安全权限。普通业务写入、文件授权和脚本 LPAC 边界继续生效。缓存用量明细保留 provider usage 的可空值；无统计不显示为0，读取/写入不从输入＋输出额度扣除。会话命中率仅聚合有明确缓存读取计数的输入量，兼容服务计数口径不一致时不编造百分比。

## 外部看板娘调试接口

此接口独立于 MCP，用于源码版桌面助手的真实对话测试。设置中保存开发开关后生效；发行构建不启动服务，每个调用重新核对门禁。认证信息记录在 `artifacts/runtime/assistant-debug-<pid>.json`；客户端读取该文件，向回环 HTTP 地址发送随机令牌，认证失败拒绝访问。不要公开运行时认证文件。

`coat_sim.ui.assistant_debug.AssistantDebugClient(target_pid=...)` 提供 `request(operation, **arguments)`：

| 操作 | 参数与语义 |
| --- | --- |
| status | 状态、当前请求编号、模型、上下文；不返回密钥 |
| submit | prompt（1–8000 字符）、唯一 client_message_id；mode、reasoning、new_session、queue；默认忙时拒绝 |
| steer | prompt、client_message_id；加入当前任务，不意味着取消 |
| withdraw | message_id；排队立即撤回，插话须等后台 ACK，已应用拒绝 |
| cancel | request_id；只中止正在运行的指定任务 |
| transcript | session_id 可选、after_id=0、limit=20（最多50）；完整文字分页 |
| events | session_id 可选、after_id=0、limit=100（最多200）；回合、工具、用量等持久事件分页 |

`submit` 调试预算默认 `max_output_tokens=4096`、`request_limit=12`、`total_tokens_limit=60000`，仅覆盖该轮，共享给主/子/整理代理；上限分别8192、24、200000。实报在请求结束结算，下一次请求前再检查累计额度，不承诺服务计数绝不超过估算预留。正常设置和全局配额继续适用。回执去重仅保留当前进程最近128个调用记录，同ID不同参数拒绝，跨重启须核对持久会话和作业。

命令行只读示例：`.conda/python.exe -X utf8 -m coat_sim.ui.assistant_debug status --pid <pid>`。编程调用示例见[看板娘说明](assistant.md#源码开发诊断)。这不是任意执行命令或获取操作系统管理员权限的接口。

## 固定工程工作流

- `create_design_workflow(client_request_id, specification)`：typed完整依赖组合，真实提交作业；返回workflow_run_id/job_id和已保存文件。
- `design_workflow_status(workflow_run_id, after_event=0)`：查检查点与增量事件。
- `advance_design_workflow(workflow_run_id)`：已获继续授权后推进暂停/失败的流程；不会另建重复作业。
- `cancel_design_workflow(workflow_run_id)`：取消流程及其所属后台作业。

独立客户端查询作业完成后推进导出；板娘运行时程序自动等待/推进。源/监测位次1基，板位次0基；同请求ID内容不能更改，导出恢复核对完整摘要。

## 开发问题报告

源码开发模式提供 `developer_report_issue(title, detail, stage, tool, evidence_reference, recovery)` 和 `developer_issue_reports(refresh=false, limit=10, consumer_id="mcp", unread_only=true, acknowledge=true, details=false, offset=0)`。前者只记录实际反馈或直接观察；后者默认按读取者返回未读问题的短摘要，并仅将本次返回的版本标记已读，完整账本保留。新出现或恢复的问题会重新成为未读。只查看不标记可设 `acknowledge=false`，历史查询可设 `unread_only=false`，需要详情再设 `details=true`；连续读取并标记未读批次时保持 `offset=0`。`refresh=true` 重新生成账本的 Markdown/JSON 文件；`developer_status.issue_reports` 显示门禁、可读报告与写入失败。发行禁用。自动日志和补报同样脱敏，不覆盖 SESSION_ISSUES.md，不执行推测的恢复建议。


## 批量膜系与固定可视化

工艺可包含最多 10000 层。批量展开、单位换算与重复膜层由程序完成；主助手无需逐层拼接完整工序对象。

| 工具 | 输入与用途 | 默认返回 |
|---|---|---|
| input_schema | kind="film_stack"，读取生成/导入五种模式的契约 | 精确 JSON Schema |
| build_film_stack | specification、path、overwrite=false；支持 explicit/periodic/equal/quarter_wave | 路径、层数、总物理厚度、名义时长、警告 |
| preview_film_stack_import | path；sheet、header_row=1、has_header=true、delimiter、encoding="utf-8-sig" | 列名、最多 12 行预览与格式提示 |
| import_film_stack | path、specification、output_path；specification.mode="import"，明确 columns、thickness_unit、order 和材料映射 | 已保存工艺的摘要与文件 |
| read_process_layers | path、offset=0、limit=20（最多 100） | 一页工序，不含整份层表 |
| update_design_fields | kind、path、patches（最多 64 个）；output_path 可另存副本 | 更改字段及保存路径 |
| visualize_calculation_result | run_id；charts、radial_bins=30、contour_levels=8、radial_center_mm=[0.0,0.0] | 本地 PNG/CSV 文件引用与说明 |

材料映射包含 material、1 基 source_slot、rate_nm_s、target_mode；监测器基准必须提供 monitor_slot。修正板 mask_slots 使用从0开始的非负整数位次，[] 不使用，省略或 null 沿用机器配置。周期生成使用 period_layers/repeat_count/prefix_layers/suffix_layers；等厚规则使用 sequence/layer_count/total_thickness_nm；四分之一波候选使用 sequence/layer_count/reference_wavelength_nm 及 materials[].refractive_index。

导入目前支持 CSV、TSV、TXT、XLSX、JSON，以及兼容的 TFCalc/Macleod 导出物理厚度文本表；没有核实专有原生格式。多工作表 XLSX 须明确 sheet，公式单元格须先粘贴为值。单位为 nm/um/angstrom，层序须明确 substrate_to_air/air_to_substrate；不自动将 QWOT 或光学厚度解释为物理厚度。文件上限 16 MiB。图片/PDF 可由多模态模型提取带不确定标记的显式层表，再调用生成器；没有内置 OCR。

四分之一波候选仅用 d=lambda/(4n)，不验证反射率、透过率、带宽、吸收或制造可行性。任意口头光学指标不能直接视为本软件已完成光谱优化。

字段补丁使用 JSON Pointer，例如 {"op":"replace","path":"/steps/0/target_nm","value":120}。支持 add/replace/remove；数组索引 0 基。保存到其他路径时依赖引用按原文件位置解析并重新生成。覆盖已有文件须 overwrite=true，仍遵循当前用户授权。

固定图表支持 heatmap、contour、radial、layer_durations；默认生成四类，径向默认 30 个环、均值及最小/最大范围。完整原网格在本地读取，不进入模型上下文，不调用脚本沙箱。同一运行与选项可复用经 SHA256 核对的自有输出。

### 摘要、分页与完整快照

read_design 对工艺默认返回 summary，需完整文件时显式 detail="full"；通常使用 read_process_layers 查看局部。save_design 的工艺回执返回摘要；validate_design 默认不回传整份 data，显式 include_data=true 才返回。

read_calculation_result 默认 detail="summary"。configuration 返回配置，layers 按 offset/limit（默认 20、最多 100）读取工序结果并省略重复 effective_settings；full 明确获取完整元信息。include_grid=true 是显式大网格请求；export_data=true 将标准绘图数据写成本地文件。完整配置、机器、工艺和全部逐层统计仍保留在计算 NPZ 快照中，摘要不改变物理计算和审计原文。

create_design_workflow 的 specification 可用 film_stack 代替 steps，二者须且仅须提供一个。film_stack 是生成器参数，其 name/machine_file/substrate_file 由工作流绑定；export_charts=true 启用固定图表导出。现有工艺只改局部参数时优先字段补丁，再按已有路径提交计算。

板娘运行时会将超大的工具返回及已结束调用参数完整归档到 artifacts/assistant/tool_results，给模型摘要与精确读取指针。read_archived_tool_result 是板娘运行时入口（不是本软件通用 MCP 业务工具），支持 JSON Pointer、分页及 mode="schema"；工具契约保留约束，不以深度截断替代。默认模型请求与累计 Token 成本限额按任务模式区分，主助手与子助手共用；达到边界保留会话和已提交作业，明确要求继续才能恢复。详见算法文档 13a-16。

### 临时设计、仿真资产与摘要数据库（2026-10-01）

新建对话资产先放 `data/tmp`。`save_design`、`build_film_stack`、`import_film_stack` 新建时若请求正式目录，服务将其转入临时目录并重写依赖引用；回执的 `path` 才是真实路径，`requested_path` 仅为原建议。已有用户文件仍可在明确 `overwrite=true` 时更新，不搬动旧文件。新工作流存于 `data/tmp/workflows`，工作流导出存于 `data/tmp/workflow-output`；已有工作流继续沿原清单恢复。

| 工具 | 行为 |
|---|---|
| prepare_temporary_design | kind、data、name_hint；在临时目录创建设计，返回名称建议和真实路径。机器/工艺依赖须提供当前有效路径。 |
| save_generated_design | kind、path、name、confirmed、overwrite=false；先询问用户保存意愿及几个词的设计摘要名称，再传 confirmed=true。复制完整依赖图到正式 data 分类目录并重写引用，临时源保留。 |
| save_simulation_asset | run_id、name、confirmed、path=null、overwrite=false；保存为一份 `.coatasset`，默认 `data/assets/{name}.coatasset`。 |
| read_simulation_asset | path、detail=summary、offset=0、limit=20（最多100）；可选 configuration/layers/images/full，默认不传输数值网格。 |
| simulation_summaries | limit=20、offset=0；读取跨 GUI/MCP 的仿真尝试摘要（成功、失败或取消），每个 run_id 一条。 |

每次成功仿真在 `data/tmp/simulations/{run_id}` 保存压缩数值网格、完整实际配置、结果元信息及标准热图。计算回执与 `read_calculation_result` 的 `files` 包含热图：板娘应先引用该产物，只有用户要求额外图表或不同绘图选项时才调用可视化工具。热图与后续标准图复用同一目录和绘图清单。网格导出也留在该临时目录。

资产包是带清单及 SHA256 校验的 ZIP 容器，包含 `result.npz`、`configuration.json`、`result-metadata.json`、`summary.json` 与已有图像/绘图表格。读取不执行文件、不直接解压到工作目录；拒绝路径穿越、重复成员、加密内容与容量超限。包上限512 MiB，展开总量1 GiB，最多64个成员。程序客户端可调用 `CoatService.load_simulation_asset(path)` 获取本地 NumPy 场及配置；助手使用摘要/分页接口。保存不删除临时源，也不自动删除数据库记录。

`data/simulation_history.sqlite3` 按 run_id 记录仿真尝试。成功摘要包含请求/实际设备、算法、用时、工艺与标量统计及已保存包的位置；失败和取消只记录真实状态、请求参数、工艺引用、用时与错误，不填造膜厚指标。后台作业异常退出也留下失败摘要。原计算估时 JSONL 历史保留，仅收成功记录，两者用途不同。

`export_pdf(run_id,path,overwrite,sections=null)` 默认仅选择 `heatmap`、`thickness`、`uniformity`，即热图、厚度和均匀性。可选 `computation`、`settings`、`machine`、`process`、`layers`；未选择的内容不写入PDF，工序统计不重复展开每层实际设置。

助手普通工艺仿真及新建工作流默认自动设备、快速算法和0.025 s步长；自动设备依次尝试GPU、DirectML、CPU。明确给出的 `settings.time_steps` 原样保留；否则按工艺真实总时长换算步数，仍受200000步上限约束。`time_step_s` 与显式 `time_steps` 不可同时提供；选择精确算法需显式 `fast_cycle=false`。纯底层 Settings 与已保存参数没有被全局替换。


## 膜系设计接口（schema v1，2026-10-06）

独立膜系工具：`film_design_schema`、`film_materials`、`validate_film_design`、`load_film_design`、`save_film_design`、`expand_film_expression`、`simulate_film_design`。具体参数以实际工具schema为准。材料列表返回用途与光学常数组元数据，完整nk数组不进入普通目录回复。

膜系光谱按用户要求暂不提交scheduler；MCP直接调用纯计算服务，将完整数组、设置、来源/摘要落盘到指定data/results文件，返回短摘要与产物引用。未知k须显式无损假设，超波段拒绝，非相干吸收基片明确不支持。首版单次波长点×(总膜层+3)上限1000000。文件路径继续受CoatService限制，保存/导出默认不覆盖；GUI人工文件选择策略不扩大自动化权限。此路径不改变沉积调度、既有模式约定或工具调用去重。

窗口类型新增 `film_design` 与 `film_simulation`；窗口操作经白名单adapter校验，未保存草稿替换需force。GUI计算通过后台线程执行，忙状态禁重复提交，参数改变使旧结果过期；主线程只编辑和绘图。

实际 `simulate_film_design(data, settings=null, output_path=null, overwrite=false)` 返回 `ok`、`summary`、`artifact_paths`、`warnings`、`input_sha256`、`artifact_payload_sha256`。默认路径 `data/results/film_<uuid>.json`；指定路径必须是该目录直属 `.json` 文件，同名 CSV 包含完整光谱八列。摘要的 mean 是本征 R/T/A 采样点算术平均，不是光源加权积分。完整 JSON 保存设计、规范化设置、结果和数据来源。输入摘要只覆盖实际设计/设置/来源，产物摘要另覆盖完整载荷；摘要不再次读取材料库。

保存返回 `{ok,path,id,name}`。overwrite 必须为 bool；默认原子不覆盖。CSV 已保存而 JSON 发布失败时，ToolError 明确包含 CSV 路径并保留文件，两个文件不是联合事务。schema/materials/validate/load/expand 为 AI 只读工具；save 属 design 执行、simulate 属 simulation 执行，均无膜系 submit/queue。详见[实际服务与接口契约](film-design-validation.md)。
