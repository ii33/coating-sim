# 沉积仿真均匀性侧栏

日期：2026-10-13。此变更只调整展示，不改变计算、结果文件、导出、队列或日志。

## 数据与状态所有者

`MainBridge` 继续独占工艺输入、任务、有效结果和失败详情。QML 只持有展开状态、滚动和焦点意图；常驻 rail 与侧栏不会创建第二份业务对象，收起不销毁控件、不清空结果、不取消计算。原 `_metrics`、面形摘要 getter、资产包和 PDF 数据仍保留，但右侧均匀性栏不显示机器、过程、性能和基片面形。

- `uniformityRows: QVariantList`：无有效结果时为空；有结果时固定七行，每行为 `{key: string, label: string, value: string}`。标签为“中文 (English)：”，值包含 `nm` 或 `%`。缺失、非法或不能恢复的数据明确显示“未提供”。
- `uniformityStateText: string` / `uniformityStateSeverity: string`：空结果给出计算下一步；正在计算时显示加载提示，已有结果继续显示；计算、绘图、任务提交、资产读取失败保留完整错误与仍有效的上次结果。取消为 `info`，失败为 `error`，执行中为 `busy`。状态由同一 bridge 的真实业务路径设置，不通过文字猜测。
- 旧 `uniformityMetrics` 和 `surfaceFigureMetrics` 接口不删除，右栏消费新的结构化显示接口，不解析已格式化的统计字符串。

## 数值定义与单位

接口真值见 `coat_sim/core/process_simulator.py::_field_metrics` 和 `coat_sim/ui/uniformity_presentation.py::format_uniformity_rows`。统计来自同一累计 `field_nm` 的有限采样点，均值和标准差均在舍入前计算。已有 CV 使用总体标准差（`np.std` 默认 `ddof=0`）/均值×100。

七行依次为平均、最小、最大膜厚，PV（峰谷差，`pv_nm`），相对 PV（`pv_percent`），相对均值的均方根偏差 RMS，以及相对 RMS。PV 为最大值减最小值，单位 nm；相对 PV 使用已有 `(max-min)/mean×100` 结果。RMS 是去除平均值后的不均匀性，不是厚度本身的均方根值，也不等于 PV。

现有结果没有独立 `rms_nm`。仅当原始 `mean_nm` 为正且有限、原始 `cv_percent` 非负且有限时，只读恢复 `RMS_nm = mean_nm × cv_percent / 100`；相对 RMS 使用同一原始 CV（%）。不从预览图重算，不解析已舍入的文字，不写回结果。均值非正时沿既有 CV 的有效域限制显示“未提供”。缺失相对 PV 也不从其他行猜测恢复。

例：有效厚度 `[1, 2, 3] nm`（忽略同场 NaN/Inf）给出 mean=2 nm、PV=2 nm、相对 PV=100%、原始 CV≈40.8248290464%；显示 RMS=0.816 nm、相对 RMS=40.825%。常数 200 nm 场显示 mean=200 nm、RMS=0 nm、相对 RMS=0%。展示最多三位小数，非零小量采用科学计数法而不显示成零。

## 布局、输入和生命周期

沿用膜系设计页面的 64 逻辑 px 功能 rail、主题选择色与小型折叠按钮。只有“均匀性”功能入口；展开后是标签/值对齐的紧凑信息栏。窄窗初始收起，用户仍可展开，绘图区与左侧输入保持正尺寸和相互分离。主题 token 为逻辑 px/pt，不再乘 DPR。

F4 在一般主窗区域切换侧栏；焦点在 ComboBox 及其编辑子项、或者下拉弹出层已打开时，保留 Qt 原生 F4/下拉行为。按钮可通过 Tab 和键盘激活。折叠前记录侧栏滚动与有效焦点，焦点在侧栏内时先移到 rail，展开通过代次保护的延迟回调恢复仍有效的焦点；轻量结果/状态刷新不会抢焦点或复位滚动。

窗口关闭、取消、排队、冻结输入快照、载入队列结果资产后继续计算，均沿原 controller 路径。异步旧任务不会覆盖最新失败状态。无新增文件写入、确认动作、后台采样或开窗重计算。

## 验收入口

- `tests/test_main_uniformity_presentation.py`：总体标准差定义、同域和原始精度、PV/RMS区分、缺失/非法/非正均值、常数场、微小值、只读与失败/旧代次状态。
- `tests/test_main_compact_sidebar_ui.py`：实际 QML 正常/最小窗/收起渲染、重复切换、滚动/焦点、原生 ComboBox F4、空/加载/失败状态、DPR 1/1.5 与明暗主题。
