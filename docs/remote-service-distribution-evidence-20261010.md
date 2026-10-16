# 独立服务发行与 GUI 候选验收记录

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

日期：2026-10-10。本轮交付是候选包，不能标为计算核全面通过。远程协议与早期服务验证另见 [远程计算服务集成证据](remote-compute-service-evidence-20261010.md)。

复核者 实际派生两个 `gpt-6-luna`：server_distribution 负责构建器/打包测试，service_config_gui 负责配置 GUI/状态测试；复核者 亲审依赖、协议边界、来源字节、回执与取消，补齐 GUI 输入、视口、样式、首次布局并独立验收。没有用子代理自评替代最终证据。

## 候选与基线

- 无 GUI ZIP：`build/remote-service/coat-sim-remote-service-1.0.0.zip`，SHA-256 `f6fac0d64f35ba874a100564799f33dd3fa7a99ab16db7d8e0020184d20478d8`。
- GUI ZIP：`build/remote-service/coat-sim-remote-service-gui-1.0.0.zip`，最终 SHA-256 `5d76d45fca87e21d7ed1205eec7c74395f91a5ff7501e1c24e389569c2aea324`。旧 `60fcc23e308e0cd5635768380c1eb2354112a0268c461fd60c6bca143031b9ba` 已被替代，不能作为最终验收包；全量文件摘要及来源比较见 归档清单（历史验证记录，未随公开源码发布） 和 候选状态（历史验证记录，未随公开源码发布）。
- Headless 含 79 个 ZIP 文件、71 个闭包模块、77 份源快照；GUI 含 96 个 ZIP 文件、80 个模块（其中 9 个 GUI 专属）、93 份源快照及 5 个 QML/JS 资源。包不含 Python/第三方运行时、产品主 UI、助手、MCP 或开发测试。
- GUI 包的全部 77 份服务端源快照与 f6fa 基线逐字节相同。当前开发树的工艺 v7/schema 半升级没有混入包；算法只在原仓库维护，不创建独立算法分支。
- P1 `coat_sim/scheduler/client.py` SHA 保持 `45f711dc62869d49d5bfeffc20ce1b4a61a7b122c35901759b440cb3a51016fe`；共享控制器 SHA 保持 `1239020782b33d2f898a7f99af2fe0e3127a185b586af8e68b93dd751473bfd3`。

## 已完成验证

构建器完整 **9 passed，182.33 s**，涵盖独立解包闭包/导入、两次构建可复现性、全部摘要、远程禁止脚本/origin、每个可选导入位置、源/QML 快照变更拒绝、冻结基线叠加、CLI 参数与路由、发布前来源变化保留旧 ZIP。该运行先于最后的 GUI 启动修复；构建器此后没有修改。最终 GUI 重建另跑隔离 `--qa`，包含包内帮助、直接无网络配置校验、全部导入和私有 INI/fakekeyring 下的 app 启动。它没有执行 GUI 分离式 validate 或真实 managed 服务动作。

复核者 最终 GUI 定向源码运行 **12 passed、1 deselected，10.97 s**，覆盖严格配置、无效端口文本阻止旧值提交、原始路径输入/保存失败、覆盖/关闭、超时/迟到回执、代次、提交异常及日志遮盖。唯一 deselected 是此前明确被拒绝的真实分离式 validate，没有重试。

包内 Qt 工具 `tools/verify_remote_service_gui_distribution.py` 先冻结 ZIP 原字节，安全解包后仅加载包内 `coat_sim`，使用绝对私有 INI、fakekeyring、offscreen/software 渲染。QTest 实际执行键盘清空/恢复端口、保存覆盖拒绝/接受、validate/start/stop/health 按钮、失败保留草稿、日志展开/收起、dirty/busy 关闭确认、取消观察/忽略迟到结果、焦点和滚动恢复；服务动作使用可控替身，不代表真实启停。

独立 app 启动采用正常 Qt 对象，只在构造入口接入私有检查器，不提交服务操作。100% 与 200% 缩放均核对客户区、边框、Quick 内容尺寸、表单非零尺寸和底部操作区。200% offscreen 工作区为 400×400 逻辑像素，客户区裁入 398×398，边框为 400×400；先请求渲染帧再检查布局。复核者 亲自查看结构截图。Basic 样式下不再有原生定制告警或隐式宽度循环。

文字视觉验收未完成：此环境的 offscreen 字体数据库为空，截图文字为方框；读取系统现有字体的路径解析又被 WinError 5 拒绝。没有安装/复制/打包字体、换路径或绕过读取。截图仅支持结构、边界、颜色及控件检查。

## 解包来源的真实 worker

命令：

```text
python tools/verify_remote_service_distribution.py build/remote-service/coat-sim-remote-service-1.0.0.zip --evidence-dir tests/<unique-directory> --offline-inline-only
```

从 f6fa ZIP 解包来源启动真实 scheduler/ray/film worker，禁止 socket、无产品 UI/助手，数据根位于隔离区：裸平面、550 nm 的 ray succeeded，R=0.04000000000000001、T=0.9600000000000002，结果 SHA `cd7e5ded5e0a5623f65c191099840205a1384c589dc5abba33769fc13fdba113`；膜系优化 succeeded 并保留可读取最佳候选，4 次评估预算、质量值 0.0955190827406278，不声称达到目标；排队取消、重建持久状态、幂等回放及 JSON 摘要一致。原始证据已归档至清单的 offline-inline 项。

这些事实不覆盖 HTTP/TLS 下载、运行中取消或沉积 NPZ 保存，不等价于新的远程双进程闭环。

## 工艺格式兼容

**本包仅含工艺 schema v6，不含新膜系工艺 v7。** API v1 / executor envelope schema v1 是另一层版本。复核者 从冻结包直接验证 v7 在计算前被拒绝，核心消息为“工艺配置仅支持第 6 版”，远程输入码为 `invalid_design_input`；该边界目前掩去内层详细错误，没有专用 `unsupported_design_schema_version` 码。外层不支持版本已明确返回 `unsupported_api_version` 和 `unsupported_executor_schema_version`，且在读取 blob 前拒绝。见归档 compatibility 项。

因此 process v7 请求在此包上的 `invalid_design_input` 应明确理解为格式不支持，不能当作普通计算失败重试；不要仅修改版本数字当作转换。后续细化错误码须在冻结基线另行修改、验收并发布新包。

## 已知光追 P2

历史独立审查记录以下三项，未在该阶段重新运行复现：

1. 零权重样本可接受，但事件 R/T/A 全为 0，结果 validator 会拒绝。
2. `sin_t >= 1 - 1e-14` 把略低临界角误判为无透射；n1=1.5、n2=1、dx=0.6666666666666633 时真实 T 约 3.62×10⁻⁷。
3. CLI 写入阶段 SIGINT 只置取消标志，可能写完仍返回成功。

这三项约束当时两份候选包；修复后需重新验证。

## 保留的环境拒绝与限制

真实 loopback 双服务客户端连接被 WinError 10013 拒绝；两条自有服务通过 stdin 关闭、退出 0。该轮早期 ZIP 随构建变动，失败证据保留当时文件摘要，不能充当最终 ZIP 通过证据。另一次禁 socket 的沉积 worker 完成积分后，生产 NPZ 原子保存 `os.link` 遇 WinError 5，作业 failed / `result_save_failed`。没有修改安全写入、权限或以 fixture shim 宣称真实保存通过。

真实 Qt 分离式 validate 在请求目录 `Path.resolve(strict=True)` 被 WinError 5 拒绝；实现者 当时 stdout/stderr 未保存独立文件，归档明确标为代理报告转录，保留测试配置来源。系统字体读取拒绝同样标为 复核者 工具输出转录，不冒称原始进程 stderr。既有 CIM、PowerShell wrapper 和 Bash 执行拒绝仍保留，未以 GUI 或发行包绕过。

未部署其他主机、未监听 LAN/公网、未更改系统服务/防火墙/ACL、未生成或配置真实持久凭据。TLS 握手、真实 GUI managed start/stop/health、原生文件选择器、Linux、全新依赖安装及冻结 EXE 均未验收。源码环境为已有 Windows Python 3.12.14 与依赖。

失败 Qt 轮、最终 stdout/stderr、截图和 SHA 归档分开保留；先前几何-only 通过不足以证明页面布局，最终工具已补内容和底部边界断言。没有 commit/push/reset，未覆盖其他线程的脏源码。

使用说明：[发行与安装](remote-service-distribution.md)、[GUI 状态及取消边界](remote-service-config-gui.md)。
