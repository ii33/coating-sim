# 优化目标上下限输入修复阶段报告

日期：2026-10-20；当前HEAD `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`，在真实当前未提交工作上定向修复，未合并旧仓库/旧副本，未推送或发布。

目标上下限无法编辑的阻断已修复。原始输入在逐键编辑、Enter和失焦后保留；只在完成编辑后告警。非法草稿不能用旧值或默认值启动计算。支持有效0、相等界限的精确目标、百分比、小数、目标切换及有效配置保存重开。

## 根因与改动

1. Controller原先每次提交单侧即检查整对上下限，倒置后pending key又阻止另一侧修正；现按目标ID持有成组原始草稿，允许两侧独立编辑和修复。
2. QML忽略setter失败而清除编辑标志，公共ThemeField随后失焦回填旧值；现保留草稿/错误并在开始、快捷开始、冻结请求和导出入口拒绝非法或未完成输入。
3. 公共数值展示默认格式会改写长小数/尾零；新增默认关闭的preserveNumericDisplayText，仅当前目标/波长范围采用原文显示，不扩改其他页面。
4. 失焦提交重建目标列表会吞掉首次目标点击；列表按钮改为不抢焦点，并在按下时同步提交及切换，没有用延时或重复点击规避。
5. 同类波长上下限接入草稿机制，目标范围与波长范围警告隔离；移除角度步长的输入期DoubleValidator，保留现有提交时采样校验。采样算法及评分公式不变。旧采样测试仅同步两条与用户既定规则冲突的断言。

## 验收

- 独立真实Qt 13项通过：39.47秒，1100×760 offscreen；涵盖双向调整、全选替换、空/1e/负值/101、Enter/失焦、原文12.34567与12.7500、两组非法范围分别修复、切目标、实际.cfilm保存重开、0及相等界限冻结。
- 主线程Windows原生相同13项通过：40.40秒测试时间（驱动41.24秒），7个相关源码/测试运行前后SHA一致，保存33张真实窗口截图。人工查看了有效尾零和非法原文截图；后者保留-0.25、警告且开始禁用。
- 两类真实GUI Start→typed scheduler→completed/targets_met：输入0–12.5%，磁盘冻结上限0.125，普通与N0均成功。独立作业ID为2628ded4871542d0b781318a9e6621f1、7863e4d4662f458cbeab57274e6212d4。550nm单点宽范围仅用于验证入口输送，不宣称减反设计或真实材料性能达标。
- 既有target_sampling及numeric_format共45项通过（26.25秒），相关Python Ruff通过。依赖跟随mypy仍有全仓既有诊断，未宣称全库静态门禁通过，也未改baseline/ignore/配置。

## 交互入口与证据

当前源码入口：`artifacts/film_synthesis_20261020/bounds-input-20261020/CoatingSim-current-input-fix.lnk`，目标为现有`.venv/Scripts/pythonw.exe`和当前`launcher.py`，无需产品pwsh依赖。已有应用进程需重新打开入口才会加载本次源码。

主要证据：同目录native-final.json/log、native/junit.xml、bounds-valid-trailing-zero.png、bounds-invalid-preserved.png；独立证据位于`artifacts/film-optimization-bounds-edit-20261020/independent-review/`，含真实job冻结参数、红绿日志和来源SHA。输入契约见[草稿契约](film-optimization-input-draft-contract.md)。

本报告只验收输入修复。取消自动应用的核心已单独完成，typed调度与GUI仍在接线；CPU性能计时和GPU评估待这些修改重冻后继续。保留已有工作，不自动保存用户文件。
