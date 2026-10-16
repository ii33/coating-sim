# 远程计算服务集成证据（2026-10-10）

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

复核者 实际派生两个 `gpt-6-luna` 工作包：`remote_contract` 负责便携输入契约与后续有效域修复，`remote_client` 负责显式客户端传输。复核者 编写服务端/worker 适配，集成并亲审协议、根目录隔离、摘要、取消和持久化。旧 `scheduler/client.py` P1 修复保持原样，SHA-256 为 `45f711dc62869d49d5bfeffc20ce1b4a61a7b122c35901759b440cb3a51016fe`。

## 追加：进程创建及首次实例写入失败的清理

同一独立审查者确认共享 venv 竞态主体闭合后，指出 `3aaedfad...` 控制器的两处未保护路径：登记占用后创建子进程抛错会留下占用；首次实例记录写入失败可能留下活动子进程。复核者 复用原显式 实现者 的 `remote_scripts` 完成窄修复，亲审异常时的 child 所有权、恢复记录、终止/等待证明及占用释放顺序。只改控制器和故障保护测试；复核者 另将原部署测试的模拟 child 修正为 wait 后实际报告退出，并补直接 launcher 日志关闭故障测试。没有新增命令或改变计算/传输功能。

保护顺序为：先登记占用；创建失败且没有返回 child 时释放占用；Popen 返回后发生异常则携带原始 child；首次主记录写失败时先保存位于 venv 注册文件旁的有界恢复记录，再按自有 child 或已核验身份尝试清理；确认 child 退出后删除主/备用记录，最后释放原 state 路径的占用。身份变化、退出未确认、终止/等待或记录删除失败时保留/恢复记录与占用，明确报错。空身份恢复记录不授权未来按 PID 终止；主记录缺失且占用仍在时也禁止重复创建 child。现有 stop 能读取可验证的备用记录并释放原 state 的占用。记录不含环境变量或 token 值。

实现者 最终带运行护栏的 **29 项纯模拟测试通过，28.15 s**。复核者 独立最终四模块运行 **83 passed、1 deselected，40.14 s**：故障/安装保护 30、既有部署脚本 24、HTTP 开关 13、远程客户端 16。默认护栏拒绝实际 Popen/run、信号、socket 和 Windows 原生进程句柄调用；测试显式 mock 覆盖，未运行真实服务。明确排除实际 venv/PID 子进程测试，未运行 lifecycle/server 真实进程模块。

实现者 前一轮 **25 passed、3 failed** 是故障注入按全局第一次 JSON 写入误击占用 registry，而非预期的首次主实例记录；已改成按主实例路径定向注入。该失败轮原始输出和摘要与最终日志均保存在 start-cleanup-review（历史验证记录，未随公开源码发布），未被覆盖。29/83 与此前 73 项有重叠，不相加。该清单包含 复核者 最终源码、原始 stdout/stderr、JUnit 和测试护栏摘要。

没有重试 PowerShell/Bash/CIM 拒绝，也没有启动真实 wrapper、服务、pip/venv、网络或变更权限。若主记录及备用持久化均持续失败，代码会明确报告 PID、备用路径与无法持久化身份，保留占用供人工处理；这一全部存储失败分支仅完成源码审查，未冒充运行验收。既有 Windows/Linux 真实生命周期、全新依赖、异机/TLS 部署边界保持不变。

## 追加：独立审查后的安装互斥修复

远程环境安装需与启动及运行实例互斥，检查入口锁顺序、跨根共用环境、失败清理和旧实例边界；不改变计算公式。

`install/start/stop/health` 按规范化后的绝对 venv 路径共用外层独占锁，生命周期操作再取得 state-dir 锁。锁和 registry 都位于 venv 同级，避免锁创建误判新环境。有限大小的版本化 registry 记录完整/不完整跟踪和各实例 state 路径；启动前写入、flush/fsync 并原子替换，重复 start 补回登记，已核验 stop 只释放自身。任何占用、旧 venv 无记录、损坏或不完整记录均在 venv/pip 子进程前拒绝 install，且不查询操作系统进程。无法确认启动清理身份时保留占用；Linux 权限/解析错误也不再被当成进程已退出。已核验的 legacy stop 可以完成，但保留不完整跟踪继续阻断安装。

实现者 新增 **20 项纯模拟测试通过**。复核者 独立运行最终四模块，结果 **73 passed、1 deselected，24.93 s**：安装保护 20、原部署脚本 24、HTTP 开关 13、远程客户端 16。明确排除会创建真实 venv/子进程的 `test_direct_base_interpreter_keeps_the_private_venv_context_and_pid`；没有运行真实 lifecycle 或 server 模块。本轮额外测试插件默认拒绝实际 Popen/run、进程信号、socket 创建和 Windows 原生进程句柄调用，明确 mock 才能覆盖该拒绝，全部通过。两个运行的数量有重叠，不能相加为不同测试总数。

原始 stdout/stderr、JUnit、测试护栏、实现者 原始日志及最终源码摘要见 install-guard-review（历史验证记录，未随公开源码发布）。实现者 的首次尝试因 pytest 搜索路径不完整未加载；其后在新的私有目录补齐路径通过，未用失败尝试假称成功。历史 118 项真实 loopback 服务运行和 54 项脚本运行仍分别保留，此轮不扩大它们的实际验收范围。

没有运行真实 pip/venv、受控服务、CIM、PowerShell/Bash 包装器或网络连接；没有重试此前拒绝、变更权限或配置实际凭据。Windows 受控启停、Bash/Linux 部署、全新依赖安装及异机/TLS 仍未验收。registry 仅覆盖控制器管理的实例；使用该环境的手动/前台进程须由操作员自行协调。无新增 init/status 命令，没有 commit/push/部署。

## 追加：显式关闭 TLS 的实际结果

用户随后明确授权“允许关闭 TLS”。复核者 复用原显式 `gpt-6-luna` 的 `remote_client` 工作包完成服务端/客户端 `allow_insecure_http` 和 13 项 mock 测试，亲审后补充真实 loopback HTTP 提交/计算/下载测试。服务端默认仍要求 TLS + bearer；新开关与 TLS 路径及旧 loopback 模式互斥，客户端须独立显式选择 HTTP 且不得同时设置 CA/HTTPS/旧 loopback 开关。新模式保留强制 bearer 认证，启动前警告 token 和数据未经 TLS 加密；没有配置真实持久凭据。

这一追加运行 **118 passed，48.82 s**：上一轮 104 项全部保留，新 HTTP mock 测试 13 项，以及实际 authenticated HTTP 服务测试 1 项。实际新模式验证 ready、正常 bearer、缺失/错误 bearer 的 401、probe 不可用、冻结光追提交、严格结果 schema、字节数和 SHA-256 下载。外部 host 只以 mock 验证配置；所有真实 socket 只监听 `127.0.0.1`。七个自有服务进程均退出 0，启动 warning 未泄露临时 token。

原始 stdout/stderr、JUnit、服务/请求/worker 证据及本轮源码摘要保存在 http-opt-in-118（历史验证记录，未随公开源码发布），共 33 个原始文件。这里的 118 项不包括后来追加的部署脚本，脚本验证另行记录。没有启动 LAN/公网服务，也没有改现有服务配置、系统服务、防火墙、ACL 或认证设置。旧 `scheduler/client.py` P1 摘要保持不变。

## 追加：部署脚本交付与实际验证范围

复核者 实际另派显式 `gpt-6-luna` 的 `remote_scripts` 工作包，完成 Windows/Shell 包装器、共享 Python 控制器和定向测试。实现者 的初轮 17 项通过原始日志另存；复核者 亲自审查并修正 endpoint 路径/身份匹配、Windows venv redirector、严格 health 值、等待句柄所需 `SYNCHRONIZE` 权限，以及失败启动的自有进程清理/不确定记录保留边界。

最终本轮定向运行 **54 passed，14.15 s**，分别为部署控制器 **25 项**、HTTP 开关 **13 项**、远程客户端 **16 项**。这 54 项与此前 118 项有重叠，不合计为一轮完整测试。源码、JUnit、stdout/stderr、标准 venv 上下文及控制器参数证据见 deployment-scripts（历史验证记录，未随公开源码发布）。Windows 私有 venv 的真实解释器 PID 与 Popen PID 一致，`sys.prefix`/site 指向所选中文路径 venv，用户 site 被关闭；这项不使用 CIM 或网络。控制器直接入口的 help/validate/dry-run、CA 冲突和缺 token 行为独立验证；未安装或下载依赖。

已交付 `scripts/remote-compute-service.ps1`、`scripts/remote-compute-service.sh`、`scripts/remote_compute_service.py`，以及 TLS/显式 HTTP 配置示例。`install` 只向私有 venv 安装 reduced requirements。后台 `start` 只有匹配 PID/instance 的 bearer ready health 才宣称成功；操作锁、进程创建身份、完整命令/绝对 data-root 和 endpoint 限制 `health/stop`。Windows stop 通过创建时间核验的进程句柄强制终止，明确要求下次启动检查中断恢复；Linux 使用 pidfd 发送信号。token 仅从已有环境变量读取，不写配置、实例文件或命令行。

**未通过的环境边界单独保留：**复核者 查询本轮自有 Python PID 的 `Get-CimInstance Win32_Process` 收到 `Access to a CIM resource was not available to the client`；Bash 路径 `C:/a/pro/msys/ucrt64/bin/bash` 的 `-n` 进程启动被拒访问；独立 Windows PowerShell `powershell.exe -NoProfile -NonInteractive -File ... --help` 收到 `AuthorizationManager` / `UnauthorizedAccess`。停止这些调用后，没有切换身份 API、Bash 路径或更改/绕过执行策略。拒绝信息是工具输出转录，未假称为保存的原始子进程日志。

历史阶段未完成全新环境依赖安装、跨主机/TLS 与完整原生服务生命周期验收；静态审阅和工具报告不能替代真实执行。

本轮没有运行外部监听，没有改用户现有 app/服务、真实 DB、QSettings、权限、安全设置或持久凭据；没有 commit/push/发布。GUI 远程地址选择仍在本轮范围之外。

## 先前服务及有效域基线结果

追加 HTTP 开关前的一次运行 **104 passed，49.76 s**，八个受影响模块全部通过。新增四个远程/有效域模块 **64/64 通过**；相关几何/CLI/队列/作业回归 **40/40 通过**。未出现权限拒绝，也未改权限或绕过拒绝。

历史夹具曾遗漏 manifest 坐标、单位、dtype 与 field_nm，修复后增加真实 NPZ 解析、缺字段负例及旧包兼容测试；生产校验和期望没有降级。

| 模块 | 通过 | 失败 |
|---|---:|---:|
| test_remote_server | 7 | 0 |
| test_remote_contract | 17 | 0 |
| test_remote_client | 16 | 0 |
| test_raytrace_valid_domain | 24 | 0 |
| test_geometric_raytrace | 16 | 0 |
| test_ray_trace_cli | 2 | 0 |
| test_ray_trace_queue_dispatch | 4 | 0 |
| test_ray_trace_job | 18 | 0 |

最终原始完整 stdout/stderr、JUnit、每个服务的启动/关闭日志、请求阶段日志、worker.log/outcome/checkpoint 保存在 accepted-104（历史验证记录，未随公开源码发布）。记录包含 UTC 时间、实际 loopback URL、PID、instance、每次请求阶段、HTTP 响应或异常 traceback；关闭前记录 endpoint 身份，关闭后记录退出码。六个服务进程均退出 0。请求记录不含认证头或 token。所有服务/client 根目录和 TEMP/TMP/basetemp 均为本次 repo 内私有目录。清单包含测试时生产源码摘要和所有原始证据摘要。

## 完整变更清单

这些条目只描述本任务修改；共享工作树的其他改动没有回滚或覆盖。

| 文件 | 本任务变更 / 执行事实 |
|---|---|
| [remote_contract.py](../coat_sim/scheduler/remote_contract.py) | 新增：实现者 实现冻结输入/版本/依赖/严格类型契约，复核者 核查并集成 |
| [remote_client.py](../coat_sim/scheduler/remote_client.py) | 新增：实现者 实现显式 URL 传输初稿；复核者 接管修正能力路由、严格 envelope、二进制限额与摘要/安全文件名 |
| [remote_server.py](../coat_sim/scheduler/remote_server.py) | 新增：复核者 实现独立 HTTP/TLS/auth 服务、blob、资产注册、共享状态适配与有界请求 |
| [remote_worker.py](../coat_sim/scheduler/remote_worker.py) | 新增：复核者 的独立 worker bootstrap，进程内选择膜系业务服务 |
| [geometric_raytrace.py](../coat_sim/core/geometric_raytrace.py) | 共享文件局部：实现者 扩展 `_grid_data/_sample_field` 节点/单元 mask，复核者 亲审；不改 UI/result schema |
| [ray_trace_job.py](../coat_sim/core/ray_trace_job.py) | 共享文件局部：实现者 重建轮廓域、校验有效厚度、处理跨单元边界和取消 |
| [test_remote_contract.py](../tests/test_remote_contract.py) | 新增：实现者 契约验证，复核者 集成 |
| [test_remote_client.py](../tests/test_remote_client.py) | 新增：实现者 基础传输测试，复核者 增严格响应、TLS/redirect 和摘要保护 |
| [test_remote_server.py](../tests/test_remote_server.py) | 新增：复核者 真实双进程、分离根、重启、取消、8 MiB、沉积 A→B 光追和真实 checkpoint writer |
| test_raytrace_valid_domain.py（历史引用目标，当前公开源码不包含此文件） | 新增：实现者 定向物理边界；复核者 独立开矩形裁剪 oracle |
| test_ray_trace_job.py（历史引用目标，当前公开源码不包含此文件） | 补两处完整夹具，增真实解析负例及合法旧包 v1/v2 兼容测试 |
| [algorithms.html](algorithms.html#raytrace-valid-domain) | 共享文件局部：复核者 增 §15a、目录、日期、公式/伪代码、内联 SVG、版本/适用域/项目规则；保留其他章节 |
| [remote-compute-service.md](remote-compute-service.md) | 新增：API、所有者、错误/异步/取消、输入版本、部署/连接和限制 |
| [geometric-raytrace.md](geometric-raytrace.md) | 独立审查后授权的文档修正：删除远程传输仍为未来工作的过时表述，链接已实现服务并保持 GUI 本地边界 |
| [remote-compute-service-evidence-20261010.md](remote-compute-service-evidence-20261010.md) | 本验收映射、变更清单及原始证据入口 |
| remote-compute-service.example.json（历史引用目标，当前公开源码不包含此文件） | 新增：无真实凭据的部署配置占位示例 |
| [requirements-remote-service.txt](../requirements-remote-service.txt) | 新增：CPU 服务依赖，含业务 schema 校验用 Pydantic；不含 Qt/助手/MCP |

该阶段未修改本地客户端、daemon/worker/jobs 或 UI/launcher/result schema。

## 已亲自核对的功能

- 无 GUI 服务和真实计算 worker 在单独进程运行，服务工作目录/数据根与客户端缓存根隔离。测试启动记录 `gui_modules_loaded:false`。
- 固定 job ID 同内容提交幂等，不同内容返回冲突；客户端销毁后可按 URL/job ID 重连。服务重启保留排队作业，正在运行的作业明确记为 interrupted，事件游标返回 reset。
- 未带正确认证的请求被拒绝；非 loopback 的明文配置在监听和创建运行目录前拒绝。客户端 TLS 默认验证 CA/主机名并禁止携带凭据跟随重定向。未创建证书或真实凭据，未实测外部 TLS 连接。
- 8 MiB 二进制真实下载、字节数与 SHA-256 校验通过；损坏缓存触发重新下载。测试产物 SHA-256 为 `0c77bc0a0795a93612d45256897456d0fcb24f151c44c150d07ecd03f4ef5168`。
- 排队和运行中的网络取消通过，远程客户端 stop 被禁用。真实膜系算法在首个完整候选后的取消钩子触发，实际 worker checkpoint writer 原子写入并 replay 验证；服务恢复后仍可取到候选。这一项是进程内算法/持久化组合验证，未声称完成了计时网络取消真实 optimizer 的测试。
- 合成工艺快照冻结后删除原始 `.csrc/.csub/.cmac/.cproc` 文件，服务 A 仍完成沉积。下载 `.coatasset` 后上传/注册到独立服务 B，B 按注册 ID + 摘要执行带膜光追，不共享 A 的文件路径或原始 artifact ID。
- 闭环实际步骤为 `step-00001-9b144687fc683555`，光线采到中心逐层厚度 `0.05050510447483223 nm`，与原资产一致；R/T/A 与现有 `coherent_rta` 同输入一致，missed 为 0，返回结果经严格 schema 验证。光追 JSON SHA-256 为 `0c596b73debd978c5382055462e9b2643d896691c6af37df89cfe6b34a72315d`。
- 有效域测试覆盖圆片中心、边界、域外、节点孔洞、全四角有效但跨凹轮廓/曲线缺口的单元、域内 NaN/Inf/负值、mask 类型/shape，以及轮廓遍历中取消。复核者 加入固定 seed 的独立开矩形线段裁剪 oracle，核对 30 个合成多边形的所有网格单元。规则和保守边缘损失已写入离线算法文档 §15a。

| 验收边界 | 直接对应测试 |
|---|---|
| 配置 fail-closed、认证、显式地址/隔离根、8 MiB 摘要与缓存修复 | `test_configuration_fails_closed_before_listening`、`test_real_remote_disconnect_cancel_integrity_and_auth`、`test_remote_client.py` |
| 幂等、断线重连、队列重启、instance/cursor reset、取消 | `test_real_remote_disconnect_cancel_integrity_and_auth`、`test_real_restart_preserves_queue_and_resets_events` |
| 真实裸界面、完整结果 schema、真实沉积上传跨根带膜光追、膜系优化 | `test_real_geometric_job_and_schema_checked_result`、`test_real_portable_deposition_bundle_and_film_optimizer` |
| 真实候选 checkpoint writer、replay 验证、取消结果持久化 | `test_verified_cancelled_film_candidate_retains_remote_result_after_restart`（进程内取消钩子） |
| 节点/单元有效域、边界/孔洞/凹缺口、坏值与取消 | `test_raytrace_valid_domain.py`；其中 `test_polygon_cells_match_independent_open_rectangle_clipping` 是独立几何 oracle |
| 严格 manifest、不冒充逐层旧包、当前实际资产版本 | `test_corrected_captured_fixture_passes_real_numeric_asset_validation`、`test_real_numeric_reader_rejects_missing_captured_field_contract`、`test_legacy_total_only_asset_is_readable_but_cannot_supply_layer_maps`；实际资产清单（历史验证记录，未随公开源码发布） |

## 两种历史问题的区分

早先受限沙箱出现的 WinError 10013 位于客户端 connect 阶段；获准的本机隔离测试随后能连接并取得实际 HTTP 响应，没有修改防火墙或监听到其他网络。另一次提交等待超时是服务器 HTTP 线程首次加载 NumPy native extension，15 s 和 30 s 的原始堆栈均停在 `numpy._core.multiarray` / `create_module`。其完整原始日志单独保存在 numpy-request-thread-timeout.log（历史验证记录，未随公开源码发布）。服务现在在启动主线程加载 NumPy，ready 在此之后发布；后续真实提交已通过。两种问题不能合并为一个“网络不通”的结论。

最初 40 项远程测试通过时，圆片逐层 NaN 闭环仍是预期 `algorithm_failed`，因此当时不构成带膜光追成功证据。该阶段的原始日志和摘要单独保存在 baseline-40（历史验证记录，未随公开源码发布），未用后续成功覆盖失败事实。本次修复后的闭环是另一次独立运行。

## 复现与剩余边界

历史定向协议和物理边界复核不替代真实服务集成、全新依赖环境、异机或 TLS 部署验证。

源代码环境中的本机试运行命令为：

```powershell
& .conda/python.exe -m coat_sim.scheduler.remote_server --data-root C:/service-data/coat --host 127.0.0.1 --port 8765 --insecure-loopback
```

CPU 服务依赖为 NumPy、Matplotlib、Pillow、Pydantic，安装列表与现有项目版本下界一致。外部部署配置只提供证书路径与 token 环境变量名称占位；当前没有设置真实证书/凭据。支持的生产 executor 白名单是 `process`、`process_recipe`、`single_source`、`mask_optimization`、`film_optimization`、`mask_weights`、`mask_field`、`point_weights`、`geometric_raytrace`。`probe` 仅用于显式 loopback 测试。`python_script`、任意可执行脚本、未注册 executor 和独立的 `film_simulation` 作业不在此服务 API 白名单；膜系计算通过现有 typed optimizer 执行。非 CPU 后端仍依赖原有可选运行时，未在本次远程测试中验收。

入口、配置、错误码、状态所有者、异步/取消边界见 [remote-compute-service.md](remote-compute-service.md)，部署占位配置见 remote-compute-service.example.json（历史引用目标，当前公开源码不包含此文件），CPU 依赖见 [requirements-remote-service.txt](../requirements-remote-service.txt)。可用以下模式复现；目录每次必须唯一：

```powershell
$repo = (Get-Location).Path
$runRoot = Join-Path $repo ('tests/tmp_remote_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runRoot | Out-Null
$env:TEMP = $runRoot
$env:TMP = $runRoot
$env:PYTHONPATH = $repo + ';' + (Join-Path $repo 'build/qa_tools_pytest')
& .conda/python.exe -m pytest -q -p no:cacheprovider `
  tests/test_remote_server.py tests/test_remote_contract.py tests/test_remote_client.py `
  tests/test_raytrace_valid_domain.py tests/test_geometric_raytrace.py `
  tests/test_ray_trace_cli.py tests/test_ray_trace_queue_dispatch.py tests/test_ray_trace_job.py `
  --basetemp (Join-Path $runRoot 'pytest') --junitxml (Join-Path $runRoot 'results.xml')
```

当前 Qt 页面仍使用既有本地客户端，尚未增加远程地址选择 UI；显式连接入口是 `remote_client.make_remote_client`，现有 GUI 不会因为服务启动而自动切换。v1 是单操作员可信队列，未实现多租户角色、计算 CPU/内存/时长配额、结果保留/GC 或断点续传；传输失败从头重试。新的 reduced requirements 未在全新主机验证。只验证新建私有根及同根恢复，未追查用户真实旧 DB 的历史缺表问题。未部署到其他主机、未开启公网/LAN、未更改系统服务/ACL/防火墙或持久凭据。远程部署需明确目标主机与配置并另行验证。
