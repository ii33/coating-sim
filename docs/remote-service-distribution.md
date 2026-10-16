# 独立计算服务发行包

日期：2026-10-11。状态：1.0.1 两文件光追修复候选，保留旧 1.0.0 包及原证据；仍不代表远程部署或全部运行路径通过。新包来源和验收见 [1.0.1 修复证据](remote-service-patch-evidence-20261011.md)。

发行包从同一套仓库源码构建，只携带远程 API、调度器、worker、计算核、格式读写和必要资源。构建时复制选中的源码到产物；算法仍只在仓库原位置维护。无 GUI 包不包含 QML、助手、吉祥物、桌面设计窗口和开发测试。数据根目录必须在发行包代码目录之外。

## 布局与接口

```text
coat-sim-remote-service-<version>/
  coat_sim/                选中的 headless 模块与必要资源
  scripts/                 单一部署控制器及启动入口
  config/                  TLS 与显式 HTTP 配置示例
  requirements-remote-service.txt
  distribution-manifest.json  版本、相对路径和 SHA-256 清单
  README.txt               发行包的安装、校验、启动说明
```

可选 GUI 变体在同一布局加入 `coat_sim/remote_service_gui/`、独立 GUI 入口、最小共享主题/几何/控件文件和单独的 Qt 依赖声明。它仍通过现有 `scripts/remote_compute_service.py` 管理服务。发行包不包含 Python 运行时；部署机需有受支持的 Python 与声明的依赖。本轮 Windows 隔离目录验收不代表 Linux 或冻结二进制验收。

构建入口为 `scripts/build_remote_service_distribution.py`，输入为 `--source-root`、`--output`、`--name`、`--version`。GUI 构建必须同时指定 `--include-gui` 与 `--base-service-archive`，先核对 headless ZIP 的固定 SHA 和全量清单，再只叠加明确 GUI 源文件；清单 provenance 记录基线和 GUI 快照。可接受基线明确固定为原 1.0.0 的 f6fa… 和经 复核者 核查的 1.0.1 的 30ed…，包内版本与输出版本必须匹配 pin。不能把并行 core 开发自动混入 GUI 包。

`--apply-reviewed-raytrace-fixes` 是固定的 1.0.1 补丁入口，与 GUI 开关互斥，必须指定原 f6fa 基包。它只从当前仓库读取两份已审哈希的 `core/geometric_raytrace.py` 和 `scheduler/worker.py`，其它内容均来自原 ZIP；全量清单、内部模块闭包、源和文件路径、外部/动态导入及所有未改文件字节必须匹配原基线。清单记录旧/新摘要，发布前复核基包和补丁来源；未包含的 CLI 不加入包。补丁和 1.0.1 GUI 只在 Windows 使用同目录 `os.rename` 拒绝覆盖已有目标，未支持的平台明确拒绝该发布入口，不使用 `os.link`。

输出为有固定相对布局的 ZIP 与内容摘要清单。构建器按 AST 导入闭包选取模块，显式加入控制器、配置示例和计算核 JSON 资源。每份源文件只读取一次作为快照，打包同一组字节；发布前再次检查源文件，若并行工作已修改源码则拒绝发布并保留原 ZIP。ZIP 条目按名称排序，时间戳固定。一般源码构建沿用同目录临时 ZIP 和 `os.replace`，上述固定修复候选使用拒绝覆盖的发布路径。清单不记录源仓库绝对路径、凭据或用户数据。

本地助手的限定延迟分支涉及两个被排除模块：`coat_sim.agent.sandbox` 仅用于本地 `python_script` 或本地文件指纹分支，`coat_sim.agent.storage` 仅用于本地助手 origin 校验。构建器校验每个导入位置；远程契约拒绝 `python_script`、origin 和相关本机文件参数。不能用一个全局可选模块名单隐藏新的可达依赖。源码导入闭包、动态导入和资源读取由明确清单及测试共同约束，不能把整个应用目录复制进包来掩盖缺失。

部署配置由现有控制器校验。认证只引用环境变量名；TLS 为默认配置。服务端与客户端各自显式允许 HTTP 后才可使用 HTTP，认证要求仍然保留。发行过程不配置真实凭据、不部署到其他主机、不修改系统服务或防火墙。

## 构建与安装

兼容性：本轮发行包固定在已验收的工艺 schema v6 计算核基线，**不包含正在开发的膜系工艺 v7**。API v1 和执行器 envelope schema v1 仍有效，它们与工艺文件版本是不同层级。不得把当前开发树的新工艺 v7 请求送入此包，或只改版本数字当作格式转换。

冻结协议对外层不支持版本返回 `unsupported_api_version` / `unsupported_executor_schema_version`。工艺 v7 会在计算前拒绝；核心校验器明确说明“工艺配置仅支持第 6 版”，但远程输入边界当前统一为 `invalid_design_input`，尚没有专用 `unsupported_design_schema_version` 错误码。提交的 process 记录为 v7 时，该错误表示本包格式不支持，不是普通计算失败。包内来源的无网络兼容验证见交付证据；后续细化内层错误码须在这一冻结基线上单独修改、验收与发新包，不能引入半升级核心。

在源码仓库构建；命令不安装依赖、不启动服务：

```text
python scripts/build_remote_service_distribution.py --apply-reviewed-raytrace-fixes --base-service-archive build/remote-service/coat-sim-remote-service-1.0.0.zip --version 1.0.1 --output build/remote-service/coat-sim-remote-service-1.0.1.zip --qa
python scripts/build_remote_service_distribution.py --include-gui --version 1.0.1 --base-service-archive build/remote-service/coat-sim-remote-service-1.0.1.zip --output build/remote-service/coat-sim-remote-service-gui-1.0.1.zip --qa
```

这两条命令用于首次生成候选，已有目标会拒绝覆盖；复现时指定新的输出文件名。已有 1.0.0/1.0.1 包不得被当前半升级开发树覆盖。本轮不使用一般 headless 构建入口从当前全仓库取快照；后续整体升级须先完成核心与契约验收。

部署机解压后进入 ZIP 顶层目录。Windows 源码验证环境是 Python 3.12.14，其它版本尚未验证。使用部署机已有的 Python 创建专用环境，并由获授权的部署操作安装依赖：

```text
py -3.12 -m venv .remote-service-venv
.remote-service-venv\Scripts\python.exe -m pip install -r requirements-remote-service.txt
.remote-service-venv\Scripts\python.exe -m coat_sim.scheduler.remote_server --help
.remote-service-venv\Scripts\python.exe scripts/remote_compute_service.py validate --config config/remote-compute-service.insecure-http.example.json --data-root D:/private/coat-service-data
```

Linux 无 GUI 路径使用 `python3 -m venv .remote-service-venv` 和 `.remote-service-venv/bin/python` 执行相同模块；本轮未在 Linux 运行。发行包是源码 ZIP，未内置 Python 或第三方依赖，也未生成冻结 EXE。GUI 变体另用 `.remote-config-gui-venv` 安装 `requirements-remote-service-gui.txt` 并运行 `scripts/remote_compute_config_gui.py --config <绝对配置路径>`；GUI 环境与计算服务环境分别维护。

默认 TLS 示例 `config/remote-compute-service.example.json` 使用证书/私钥路径占位符；显式 HTTP 示例仅用于获授权的可信网络或加密隧道。认证变量值由部署机外部环境提供，示例和 GUI 只存变量名。启动/健康检查/停止的命令和身份、占用锁、失败恢复边界沿用 [现有部署控制器契约](remote-compute-service.md)。

## 验收边界

原 1.0.0 包仍保留两个光追问题：零权重事件的 R/T/A 为 0，结果 validator 拒绝；近临界容差带会丢失可透射方向。1.0.1 仅叠入指定两文件修复，复核者 从新包的真实 worker 核查零权重三偏振与近临界透射，并通过故障注入确认无效完整结果在发布前拒绝。CLI SIGINT 修复不在此验证范围：原包与新包均不含 CLI，这一修复没有加入发行包或在这里验收。算法定义已由原修复同步至 [算法 §15](algorithms.html#geometric-raytrace)，发行过程不另建计算实现。

从独立解包目录运行入口的帮助、配置校验和所有支持执行域的导入，并检查模块来源；不使用原仓库 `PYTHONPATH`。headless ZIP 含 71 个导入闭包模块、77 个源文件快照、79 个文件。五项初始打包测试通过，包含可复现双构建、独立帮助/校验/导入、摘要检查和源变更拒绝。后续 GUI 与导入分支收紧测试另行记录。

复核者 验收工具 `tools/verify_remote_service_distribution.py` 先将被测 ZIP 的字节冻结到唯一证据目录，再检查安全解包路径并运行解包来源的独立子进程。默认流程用于两个隔离服务根目录上的真实 HTTP 闭环；本轮 loopback 连接被系统拒绝（WinError 10013），已停止该操作，两条自己创建的服务进程均正常退出。另一次无 socket 的沉积 worker 在结果保存时遇到生产 `os.link` 的 WinError 5，保留失败状态与日志，没有改原子保存、权限或使用 shim 宣称通过。

`--offline-inline-only` 是独立的未受影响验收路径，明确不尝试 HTTP 或沉积结果保存：解包后的真实 ray/film worker 成功，排队任务取消成功，重建服务对象后持久任务状态、幂等回放和 ray JSON 摘要一致。裸平面合成例在 550 nm 得到 R=0.04、T=0.96，膜系保留有效最佳候选（不声称达到目标）。这只验证队列、worker 和相关持久结果边界，不等价于网络下载或真实双进程远程闭环。

GUI 管理动作测试使用可控适配器。真实 detached validate 在请求目录 `Path.resolve(strict=True)` 被 WinError 5 拒绝，保持停止。offscreen 字体数据库为空，系统字体路径解析也被 WinError 5 拒绝并停止；截图仅支持布局和控件检查，文字视觉验收未完成。此前独立 PowerShell wrapper、CIM 进程身份查询和 Bash 检查的执行拒绝保持记录，不能用发行包或 GUI 绕过。未进行远程主机部署、TLS 握手、Linux 运行、全新依赖安装或冻结二进制验证。

详细 GUI 契约见 [配置工具契约与布局](remote-service-config-gui.md)，新包证据见 [1.0.1 修复记录](remote-service-patch-evidence-20261011.md)；原包与失败/通过轮保留在 [1.0.0 发行与 GUI 记录](remote-service-distribution-evidence-20261010.md)。
