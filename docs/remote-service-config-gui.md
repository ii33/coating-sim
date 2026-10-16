# 独立服务端配置工具契约与布局

接口与布局日期：2026-10-10。布局经主线程批准，源码和定向测试已完成。1.0.1 只更换经过固定 pin 核查的计算基包，GUI 来源字节保持不变；新包证据见 [1.0.1 修复记录](remote-service-patch-evidence-20261011.md)，原包证据保留在 [1.0.0 交付记录](remote-service-distribution-evidence-20261010.md)。

## 页面与依赖

默认窗口为 720×660 逻辑像素，最小 560×480；`app.py` 在显示前将最小尺寸和初始几何裁入当前屏幕工作区，使用共享 `WindowGeometryManager` 恢复几何；显示后及换屏时，平台提供边框尺寸后再次裁入，极小工作区相应降低最小尺寸。这是配置工具对共享 1100×760 默认尺寸的明确例外。Theme 尺寸为逻辑尺寸，不再乘 DPR。

固定顶部显示配置文件、观测状态、经过时间和错误；中间表单滚动；底部操作始终可见。可用宽度至少 640 且字体缩放不超过 1.25 时，地址和端口以完整的“标签＋输入框”成对双列排列，否则单列。路径占完整一行，标签可换行，浏览按钮有固定宽度上限，输入框可横向编辑并提供完整路径提示。TLS 证书、私钥及客户端 CA 在 TLS 模式显示；HTTP 模式仍要求认证。日志默认折叠，展开后高度有界。小窗口和字体放大时底部操作重排。

工具只引入共享主题、几何管理器和 ThemeButton/ThemeField/ThemeLabel 的明确依赖闭包，不引入产品设置页或产品主窗口。`requirements-remote-service-gui.txt` 单独声明 `PySide6-Essentials>=6.8,<7`；GUI 与计算服务环境分开维护。首次打开不查询、不启动服务。

## 接口与唯一状态所有者

启动入口明确选择 Qt Quick Controls Basic 样式，保证共享 Theme 控件的自定义背景与内容项可用，不依赖平台原生样式的定制能力。

同步 QML 构造完成后再准备并显示窗口；几何管理器由 engine 持有。窗口内容尺寸明确绑定逻辑窗口尺寸，原生尺寸交付后同步 Quick 视口，防止首次显示或工作区裁入时表单保持旧尺寸。启动验收先请求 offscreen 渲染帧，再同时检查客户区、边框、内容尺寸、表单非零尺寸和底部按钮边界。

接口真值位于 `coat_sim/remote_service_gui/controller.py` 的 `RemoteServiceConfig`、`RemoteServiceController`、`QProcessRunner`，以及 `operation_runner.py` 的请求/回执结构。GUI 不复制服务生命周期逻辑，仍调用发行包内唯一的 `scripts/remote_compute_service.py`。

`RemoteServiceController` 唯一持有编辑草稿、原始端口文本、dirty、配置路径、观测状态、当前操作代次及未确认操作状态。`QProcessRunner.finished(int generation, int exit_code, str stdout, str stderr)` 返回结果；`operationFinished` 和 `stateChanged` 驱动页面。状态包含 unknown、unstarted、starting、healthy、health_failed；校验成功不会暗示服务正在运行，超时只产生 unknown。

服务 JSON 只包含既有服务端字段。严格解析拒绝未知字段与类型强制转换，保留已支持的连接数、blob 大小及配额等限制。客户端 CA、服务虚拟环境及控制器状态目录属于 GUI 运行参数，保存到本工具的绝对路径 IniFormat 设置文件，按配置路径分组，不扩展服务 JSON。路径浏览使用本地 QUrl；手工编辑保留原始输入，保存成功后才按发行根目录基准规范化相对路径。数据根目录适用域仍由共享服务校验器判断。

端口文本允许暂时为空或不完整，但只有 ASCII 数字、长度 1–5、数值 1–65535 才允许保存或提交。无效文本不会静默使用此前的有效端口。加载失败保留草稿；原子 JSON 写入失败保留输入及 dirty；GUI INI 保存失败明确说明 JSON 已写入但运行参数未保存，仍保持 dirty。覆盖已有文件、丢弃未保存草稿、忙碌或状态未确认时关闭都需要确认。保存/校验/操作结束及拒绝对话框后恢复表单焦点与滚动位置。

## 异步、取消和回执

同一窗口仅观察一个活动操作。提交时冻结已保存的服务配置，给出递增 generation，并禁用配置编辑。`QProcess.startDetached` 启动发行包内 `operation_runner.py`；该适配器导入同一共享控制器执行 validate/start/stop/health。GUI 关闭不会通过 QProcess 析构终止控制器或计算服务。

请求是私有临时目录中的有界 JSON，只允许 controller、config、action、venv、state_dir、ca_file、receipt 七个字符串字段；仅允许四种 action。适配器限制请求文件名、目录前缀、同目录配置和回执、发行包内控制器路径，并拒绝符号链接。回执严格为 `{exit_code: int, stdout: str, stderr: str}`；输出各有 16 KiB 上限，完整回执不超过 64 KiB。写入使用同目录临时文件、fsync 和 os.replace。只在有效回执被确认后清理自己固定名称的文件；确认失败保留证据。

共享控制器的启动阶段等待 20 秒，健康检查 5 秒。GUI 观察上限为启动 35 秒、其他操作 60 秒；每秒更新经过时间，每 200 ms 查看回执。观察超时停止计时器并显示“状态未知，操作可能仍在执行”，不终止服务操作。未确认操作期间禁用编辑和后续管理操作，允许显式健康检查；失败或成功的健康结果不能自行消除此前未确认的操作。下一次显式操作可读取保留的有效迟到回执并核对原动作，随后再提交新操作，没有无期限后台等待。

“取消等待”只停止 GUI 观察并使旧代次结果失效。忙碌关闭确认说明“已提交操作继续完成，不停止计算服务”；确认后停止观察并退出 GUI。停止服务仅由明确的“停止”动作交给共享控制器核对进程身份。未知 PID、CIM 或平台拒绝直接呈现，不换路径或手段绕过。

认证值只由外部环境提供，页面、JSON 和 INI 只保存环境变量名称。适配器和 GUI 对继承环境中已知 token 值进行遮盖，并限制日志大小；跨写入边界的秘密片段也先合并遮盖再截断。本阶段没有生成或配置真实持久凭据。

## 验证范围

安全定向测试采用私有绝对 INI、fakekeyring、合成配置，覆盖严格输入、保存/失败、代次、超时回执、关闭、日志遮盖和真实 QML 对话框。包内 Qt 验收使用 offscreen/software 渲染，实际 QTest 键盘和按钮操作，服务动作使用可控替身；包内真实 app 启动仅检查几何，不执行服务操作。原生文件选择器交互不在此验收范围。

真实分离式 validate 曾在请求目录 `Path.resolve(strict=True)` 被 WinError 5 拒绝，保持停止，未冒充通过。真实 managed start/stop/health 还受既有 CIM/PowerShell/Bash 拒绝限制。offscreen 环境字体数据库为空；读取现有系统字体的路径解析也被 WinError 5 拒绝并停止，因此截图仅用于布局、控件与颜色检查，文字视觉验收未完成。完整证据与限制记录在 [交付记录](remote-service-distribution-evidence-20261010.md)。

依据：[共享 Qt Quick 页面与控件规范](quick-theme-controls.md)、[主题接口契约](quick-theme-contract.md)、[窗口几何策略](window-geometry-policy.md)、[服务生命周期契约](remote-compute-service.md)。本次不改变计算公式或文件数值语义。
