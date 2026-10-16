# 独立计算服务 1.0.1 修复候选证据

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

日期：2026-10-11。此轮只在已冻结服务包上叠入指定的两份已审修复，不从当前全仓库获取核心快照。1.0.0 ZIP 和 [原发行证据](remote-service-distribution-evidence-20261010.md) 保留；本轮仍是候选，不代表实际远程部署完成。

## 来源与固定边界

原 headless ZIP SHA-256 为 `f6fac0d64f35ba874a100564799f33dd3fa7a99ab16db7d8e0020184d20478d8`，原 GUI 为 `5d76d45fca87e21d7ed1205eec7c74395f91a5ff7501e1c24e389569c2aea324`。两者未被覆盖。新 headless 为 `build/remote-service/coat-sim-remote-service-1.0.1.zip`，SHA-256 `30ed1f7751e88b09842031d5d73d597a91778454be4a9865569a1117b1c5bf51`。

仅两项来源替换：

| 源文件 | 1.0.1 指定 SHA-256 |
| --- | --- |
| `coat_sim/core/geometric_raytrace.py` | `2f94afe5255713e48389a3712141035d6b9a536ae25f6c8a8e3e0e4735a24dc0` |
| `coat_sim/scheduler/worker.py` | `61637e8a75831962c54fdb569c42bd8e456782616db89d25252e6da240ac4d7b` |

复核者 从 ZIP 逐字节核查：新包 79 个文件，其中 78 个列在普通文件清单，77 份源快照、71 个内部模块；只有上述两项及 `distribution-manifest.json` 改变，其余 75 份源快照完全相同，README、配置和依赖文件也相同。worker 新导入的结果校验器已在原包中，没有新增第三方或动态导入。来源比较和全部原始记录见 归档清单（历史验证记录，未随公开源码发布）。

原包与新包均不含 `ray_trace_cli.py`，因此另行修复的 CLI SIGINT 问题没有加入这个发行范围，也未在这里重复验收。算法语义由原修复在 [算法 §15](algorithms.html#geometric-raytrace) 维护，本轮没有另建算法分支。

本包仍为 **工艺 schema v6**，不支持正在审查的膜系工艺 v7。API v1、executor envelope schema v1 与工艺版本是不同层级。`process_profiles.py` 和远程输入契约字节未变；新包执行时也核对工艺版本常量为 6。v7 的远程错误仍是 `invalid_design_input`，尚无专用内层不支持版本码，原 v7 拒绝证据保留。不能仅修改版本数字当作转换。

## 构建与发布核查

复用原显式 实现者 的 `server_distribution`，实现固定 `--apply-reviewed-raytrace-fixes` 入口。它校验 f6fa 原包全量摘要，仅读取两份指定修复；模块集合、源/文件路径集合、所有未改字节以及外部/动态导入都需与基线匹配。manifest provenance 记录基包、两项旧/新 SHA 和依赖审计。发布前再检查基包和补丁来源；目标已存在时拒绝覆盖。Windows 同目录 `os.rename` 实现新候选发布，非 Windows 明确拒绝这个发布入口，不调用 `os.link`。

实现者 的补丁入口定向运行 **4 passed、9 deselected，71.87 s**，新 headless CLI 构建及隔离 `--qa` 通过。复核者 亲审固定哈希、依赖、发布顺序和版本路由，再独立进行包内计算；没有以代理自评代替下面的结果。

GUI 的基包只接受明确 pin 的 f6fa/1.0.0 与 30ed/1.0.1，包内版本及输出版本必须匹配。新增 pin/版本/防覆盖定向运行 **2 passed、13 deselected，25.53 s**，GUI 构建及隔离 `--qa` 通过。两个定向运行分别保留日志，不与旧完整测试数相加。

最终 GUI 为 `build/remote-service/coat-sim-remote-service-gui-1.0.1.zip`，SHA-256 `7278d9953f6892af9b8530df83a564fdc70e785042fbd3b47548c3c36fd1979d`，包内基包 SHA 为 30ed…、版本均为 1.0.1。复核者 全量摘要及来源比较通过：96 个文件、95 个普通清单条目、93 份源快照；77 份计算服务来源与新 headless 相同，16 份 GUI 与原 5d76… 及当前源码完全相同。新 GUI 相对旧 GUI 也仅改变两修复文件及 manifest。全部摘要由清单和 候选状态（历史验证记录，未随公开源码发布） 记录。

复核者 从最终 7278… ZIP 的独立字节快照解包，真实 Qt 页面渲染、QTest 键盘/按钮/确认、草稿失败保留、观察取消与迟到结果、焦点/滚动恢复、实际 app 初始视口/边框/底部布局在 100% 与 200% 下均通过。200% offscreen 工作区 400×400，客户区 398×398、边框 400×400；内容尺寸一致，表单非零、底部操作区在客户区内。另覆盖浅/深色、560×480 最小布局及字体缩放 150%。复核者 查看结构截图；服务操作仍使用可控替身，没有真实 managed 启停。此环境字体库为空，文字为方框，`text_visual_qa=false`，不声称文字视觉验收通过。

## 新包的真实离线 worker

`tools/verify_remote_service_distribution.py --offline-inline-only` 先保存新 ZIP 原字节，再安全解包，仅用包内模块和私有数据根运行真实 ray/film worker；不调用 socket 或沉积 NPZ 保存。普通光追 succeeded，550 nm 裸界面 R=0.04000000000000001、T=0.9600000000000002；膜系优化 succeeded 且保留有效最佳候选。排队取消、重建持久状态、幂等回放与 JSON SHA 校验均通过。膜系 4 次评估预算不表示达到优化目标。

`tools/verify_remote_service_patch_distribution.py` 从同一 30ed ZIP 的独立解包目录运行四个真实光追 worker：非偏振、s、p 的零权重请求各 succeeded，三个零能量事件的 R/T/A 都是有效系数且和为 1；near-critical 请求 succeeded，保留向前透射及正透射能量。结果全部通过包内严格 validator、字节数和 SHA 核对。

临界样本为 n1=1.5、n2=1、dx=0.6666666666666633。真实 worker 的非偏振 T=`5.875667782317897×10⁻⁷`；另外从同一包纯 trace 核对 s 为 `3.6157961240505593×10⁻⁷`、p 为 `8.135539440585235×10⁻⁷`。与独立裸界面 Fresnel 表达式的绝对差分别不超过 `4×10⁻¹⁶`；此前约 `3.62×10⁻⁷` 的复现对应 s 偏振。该数值检查不代表全计算域精度验收。

无效完整结果的故障注入只替换 trace 返回值，在 worker 的新增 validator 处拒绝，结果目录没有文件；这是发布边界测试，未模拟生产写入成功。原 1.0.0 包的纯 trace 对照实际复现零权重 `event_rta_balance` 拒绝和近临界 `nonforward_transmission` / T=0，说明回归覆盖原问题。

首轮对照工具误从 `schemas` 导入版本常量；修正为 `process_profiles` 后通过。首轮新包回归的四个 worker 已完成，但最后故障注入误传序列化字典；修正为 `JobSpec.parse` 后在新私有根完整运行通过。两个工具失败轮的原始日志另行保留，不计作通过，也没有修改生产源码补救测试。

## 延续的限制

没有重试此前 WinError 10013 的 HTTP 连接、WinError 5 的沉积 `os.link` 保存、Qt 分离式 validate 或系统字体路径解析，也没有执行受拒绝的 PowerShell/Bash/CIM 操作。没有变更权限、生产原子保存、凭据或认证配置。

此次离线成功不覆盖 HTTP/TLS 下载、运行中取消、真实 GUI managed start/stop/health、原生文件选择器、异机部署、Linux、全新依赖安装或冻结 EXE。GUI offscreen 字体库为空，文字视觉验收仍未完成。没有部署 LAN/公网或其他主机、改系统服务/防火墙/ACL、生成真实持久凭据、commit/push/reset。

使用说明：[发行与安装](remote-service-distribution.md)、[GUI 状态与取消契约](remote-service-config-gui.md)。
