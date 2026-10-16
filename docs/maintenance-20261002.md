# 2026-10-02 修复记录

## 原生崩溃调查

现有桌面日志先记录 Windows 异常 `0x80010012`，随后在输出全线程栈期间再次记录访问冲突。Windows 错误报告确认此次崩溃模块为 Python 3.12 的 `python312.dll`，异常 `0xc0000005`，偏移 `0x2ea93d`；静态导出地址表显示该位置靠近 Python 的栈输出辅助函数。日志包含的 Python 源码行号可能来自已载入的旧版本，不能直接映射到当前工作树。

这是诊断器可能二次崩溃的证据，尚不能确定最初异常来自哪个原生调用，也不能证明上下文接近容量直接造成原生访问冲突。`UiControlServer` 已使用 Qt 排队信号在界面线程派发，未发现此路径直接跨线程操作窗口。

Windows 默认使用常规异常日志和系统 Windows Error Reporting；不再让 faulthandler 对每个非排除 SEH 异常遍历全部线程。`COAT_NATIVE_TRACE=1` 可显式开启当前线程的原生诊断。窗口操作新增开始/结束记录，只记录动作、窗口种类、耗时和是否成功，排除文件正文、对话、密钥和参数。原生异常处理行为可核对 [CPython 3.12 faulthandler 源码](https://github.com/python/cpython/blob/3.12/Modules/faulthandler.c)。本轮没有复现崩溃；该修复属于有证据的诊断加固，不能承诺所有原生崩溃已消除。

## 修正板优化

旧报告中搜索和验证同为81网格：中途评分对应3500 nm单源贡献（CV约0.2869%），最终对应7000 nm全膜系（CV约1.6714%）。另一源及启用板的贡献在收尾叠加，这解释了评分不可直接比较。机器板引用只有文件字段，轮廓采用固定机架坐标，排除了保存时再次应用placement的疑点。

均匀性优化现在统一评价全膜系。搜索前四名及原板在原网格验证，最终未改善保留原方案；目标图模式保持源目标图评分并另列全膜系CV。周期三次径向样条从搜索阶段生成轮廓，评分与保存使用同一离散形状。队列分别显示实测阶段、评分次数、搜索最佳与验证最佳；验证计算量纳入估算。

## 上下文和桌面

运行上下文采用有效输入预算下的摘要＋近期原文策略，70%触发、45%目标、30%近期保留。手动 `/compact` 和用量面板入口保留显示历史及任务身份。后台合并流事件、增量更新当前轮，历史和统计读取移至后台；思考展开采用纯文本分块布局。参数和限制见[离线算法文档](algorithms.html#assistant-context)。

形状与工艺详情由固定程序产物提供，不要求模型展开层表或自己写几何脚本。关于页人物下缘与内容框按固定图像比例锚定；悬浮右键菜单避开图标。提示音为本项目合成的气泡声，无需外部素材，仍可关闭。正文最小字号及深色思考对比度提高。

本轮只进行了源码、接口、文档和差异静态检查；未新增或运行测试、真实模型请求、仿真或GUI复现。性能提升幅度与原生崩溃复现仍需实际运行确认。

## 云端同步兼容检查

云端现有环境只有 PySide6-Essentials。为避免新增气泡音效阻断启动，仅在 QtMultimedia 模块缺失时回退既有系统提示音，并明确输出降级警告；模块内部依赖损坏和其他导入错误仍正常暴露。Addons 可用时继续播放原气泡音效，requirements.txt 保留 Addons 声明。Linux 启动预检对缺失的音效扩展给出警告，其余依赖缺失或已装版本不匹配仍失败。本次不自动安装依赖，不改变配置或重启正在运行的界面。

悬浮图标右键菜单改为调用实际拥有 `menuPosition` 的 `desktop` 桥；原 `assistant` 桥没有该槽，offscreen 右键事件可复现 TypeError。修正仅改变绑定对象，保留菜单避让算法与所有操作。

同步验证：52 项离线单测（原42项＋10项音效/启动/绑定回归）、36项队列检查、20项新增逻辑 tiny 回归通过；Python 语法、运行模块导入、Linux 启动预检与 dry-run 通过。关于、聊天、悬浮和有进度数据的队列窗口可 offscreen 渲染；右键菜单事件通过。聊天富文本布局的12条绑定循环警告在旧QML对照中同样出现，保留为既有问题；未宣称真实桌面操作、音频输出、模型连接、优化效果或崩溃复现已验证。


## Floating assistant menu timing and About image (23:15 UTC)

- Kept the existing desktop bridge, screen-edge positioning and menu actions.
  The right-click path now waits for `MouseArea.onClicked` and one queued turn
  (`Qt.callLater`) before opening the popup window. This avoids transferring the
  mouse grab during the initiating button press/release. Menu focus is explicit.
  Left-click chat and system dragging retain their existing paths.
- The About/Help character moves down by 8 logical pixels. Its image-relative
  elbow anchors, responsive dimensions and page layout are unchanged.
- Seven new offline UI regressions cover press/release ordering, persistence,
  focus, Escape and repeated opening, action clicks, left clicks, left drags,
  screen-edge placement and the About layout at four window sizes. The old QML
  fails the new press/release assertion as expected. All 59 repository tests
  passed with the three intentionally omitted legacy test inputs supplied from
  the exact base commit in a separate test-only directory. No omitted legacy
  file was restored into the deployed application. Running the cloud copy's
  discovery without those inputs still fails the pre-existing legacy import.
- On a real Linux X11 desktop, isolated windows built from both the base and
  candidate QML displayed the menu. The candidate was manually checked for
  right-click opening, Escape dismissal, reopening, action selection, left-click
  dispatch and actual left-button dragging. The About image was visually checked
  after the offset. Tests use stubs and never call a model, read saved account
  credentials, submit computations or write user settings.
- The user-reported Windows failure was **not reproduced or verified on Windows**.
  This is a narrowly scoped input-timing compatibility candidate; Windows closure
  still requires checking the updated version in that environment. The Linux
  result does not establish the Windows root cause. Existing app processes must
  be closed normally after preserving unsaved work, then reopened to load QML.
- Qt emitted its known Python-wrapper shutdown ResourceWarning in the successful
  offline suite. The isolated X11 desktop also lacked the optional desktop portal.
  Neither warning caused an observed test or menu failure. No real application
  window was restarted; only the independently created QA windows were closed.

Reference: [Qt Menu context-menu event handling](https://doc.qt.io/qt-6/qml-qtquick-controls-menu.html#context-menus).
