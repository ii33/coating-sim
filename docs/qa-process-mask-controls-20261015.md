# 工艺入口与修正板浮动按钮定向验证

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

日期：2026-10-15。

## 范围与行为

- 工艺页暂时隐藏“批量膜层”按钮；`FilmStackController`、`FilmStackDialog` 和既有接口保留。
- 机器、基片文件按钮统一显示“浏览”；相邻编辑器入口按当前引用显示“新建”或“编辑”，无障碍名称仍包含机器/基片对象。路径解析和路由不变。
- “从膜系导入”在缺机器时仍可点击，打开既有审阅对话框并提示先浏览或新建机器、配置有效源。无效机器、无有效源和已变化的资产包也给出明确失败；不发送膜系读取请求，不替换工艺。
- 每次请求清掉上次候选、映射和错误，补齐机器后可以重新取得膜系、显式选面与源位并导入。导入不自动保存。
- 修正板仅移除局部 `ViewportToolbarButton` 的投影矩形和文字描边，保留填色、边框、焦点、选中和禁用反馈。不修改通用按钮或其它页面阴影。
- 顶部单行及溢出适配由共享 `ResponsiveOperationBar` 实现，本次定向验证使用已接入该组件的工艺与修正板页面。

## 最终结果

解释器：项目独立 QA 虚拟环境中的 Python，PySide6 6.12.0，Linux offscreen / software Quick / software RHI。

最终选择执行 10 个测试，10/10 通过，10.22 秒。日志：`artifacts/process-mask-controls-20261015/final-focused.log`。

`tests/test_process_editor_navigation_qt.py` 的 7 项选择覆盖：

1. 隐藏批量入口但保留 controller；机器/基片紧凑标签。
2. 无机器真实鼠标点击、可见错误、连续两次 Esc 关闭/重试；步骤、dirty 和文件不变。补机器后冻结合成膜系、显式映射并成功替换步骤；再次缺机器会清掉旧候选。
3. 缺失机器文件/未配置源的 bridge 失败信号、无修改和恢复。
4. 点击入口前复查被删除的源依赖，源文件恢复后可重试。
5. 已变化资产包拒绝隐式 reload，保持原工艺。
6. 编辑器路由及发送前保存决策。
7. 920×680 工艺窄窗与机器页布局、滚动和实际操作。

`tests/test_mask_viewport.py` 的 3 项选择覆盖：浅/深色按钮均无投影与文字描边、空白浮层区域仍可平移、各工具在多个缩放倍数下可点击且不越界。

Fixture 使用自身临时设计与合成光学记录；显式等待工艺 controller、释放窗口/组件/引擎/bridge、恢复字体和路径补丁，再清理临时目录。源文件故障注入在 finally 中恢复。Qt 设置和网络沿用测试守卫隔离。

截图位于同一 artifacts 目录：`process-import-missing-machine.png`、`process-import-valid-review.png`、`ProcessWindow-*-light.png` / `*-dark.png`、`mask-toolbar-flat-light.png` / `mask-toolbar-flat-dark.png`。已人工查看缺机器提示、窄窗及修正板浅/深色平面按钮。

仅做以上定向验证；未运行全量套件或全库静态检查，未声称完成原生桌面、多显示器或真实 GPU 验收。未改物理算法、文件格式或数值语义，未提交或推送。
