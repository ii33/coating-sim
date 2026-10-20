# 自动合成与物理结构GUI阶段验收

日期：2026-10-20。环境：sakuya，当前主目录`C:/Users/youai/Documents/coating-dev/coating-sim`；HEAD保持`0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`。本阶段在当前未提交工作上增量实施，没有旧版合并、提交、推送、release、依赖安装或长研究搜索，也未重启/关闭用户窗口。

## 可交互交付

- 优化页→“算法”→“从零自动合成”：从裸栈起步，当前设计仅提供实际环境；显式候选材料、当前目标、可选当前处方seed。空预算保持None，复用现有队列/CPU执行器、心跳、无响应诊断和协作取消。
- 膜系设计→“参数设计”→“物理结构与组合段”：多堆/匹配、多腔/独立端镜和耦合镜、局部Bragg/比例啁啾、真实H/L数字rugate、可排序组合段；真实材料/数据集表单、完整nm层表、确认替换未保存草稿与一步撤销。
- 优化结果呈现任务状态、同一候选的独立审计事实、逐目标最坏值和完整层表。项目“查看候选采样光谱”只聚焦本页冻结目标采样点，不替换其它光谱窗口，不称连续谱。完成不自动回写；历史、取消、失败和未达标候选不可应用。

操作见[GUI指南](film-synthesis-gui-guide.md)，状态/输入/异步边界见[GUI契约](film-synthesis-gui-contract.md)。本轮没有发布安装包；应从本目录现有启动器进入源码界面。

## 边界与修复

独立实际操作发现并修复：空栈项目材料角色过滤为空；相同n/k记录因`design_ready=None`与省略键的差异导致正常项目保存失败；活动求解快照不含合法非活动出口材料，导致完整文档应用被拒；真实项目宿主没有`open_window`，旧式预览调用无效。

当前GUI在提交时另冻结完整可编辑环境/工作流材料闭包，保留provider原始完整记录并精确核对与活动求解快照重叠的物理数据。核心wire、活动n/k审计和结构公式保持不变；非活动未知k只保留，不求值或暗设零。应用、项目保存和撤销使用该闭包，运行后同ID目录漂移不改候选值；撤销保留后来独立加入的工作流材料。项目原保存冲突检查未放宽。

历史队列没有GUI额外闭包，活动快照不足以覆盖完整文档引用时明确拒绝另存，仍可查看层表与采样图。独立`.cfilm`仅保存材料引用；目录数据漂移/缺失时拒绝另存。源revision、pending-edit epoch、项目关闭/重开和迟到/重复回调继续失败关闭。

## 当前同源验证

最终全部检查前后均核对同一962项源码SHA，无运行中源码改动：

| 检查 | 实际结果 | 原始证据 |
| --- | --- | --- |
| 34模块集成回归 | 464 passed、13 subtests，198.30s；0失败/0跳过；28项既有弃用警告 | [JUnit](../artifacts/film_synthesis_20261020/gui-integration-20261020/final-current/regression-verified-junit.xml)、[日志](../artifacts/film_synthesis_20261020/gui-integration-20261020/final-current/regression-verified.log) |
| 原生Windows真实Qt操作 | 14 passed，60.87s；1100×760；47张原始窗口截图 | [JUnit](../artifacts/film_synthesis_20261020/gui-integration-20261020/final-current/native-verified/junit.xml)、[截图目录](../artifacts/film_synthesis_20261020/gui-integration-20261020/final-current/native-verified/) |
| 独立复核 | 同14项offscreen，54.37s；Ruff通过；新独审文件mypy诊断0 | [独审日志](../artifacts/film_synthesis_20261020/gui-integration-20261020/independent-review/gui-review-typing-pytest.log) |
| 全仓锁定静态check | 962源；mypy3863、QML1671、Ruff1428；相对基线新增40/已解决222、语法致命0；exit1 | [官方完整报告](../artifacts/static-analysis/gui-20261020-locked-current/report.json) |

真实操作使用私有临时工程、真实本地scheduler和TMM/合成核，不mock数值或队列。覆盖鼠标/键盘模式选择、材料与目标/预算编辑、N=0已知单层、冻结spec、单点图和层表、心跳+已审计checkpoint后取消、任务与候选事实分离、实际Yes/Ok确认、保存/撤销、项目关闭重开、未知k/域错误和材料漂移拒绝、独立耦合腔字段。

已知有解GUI用例使用明确合成数据n=1.23/k=0，550nm、基片n=1.5，从空栈得到单层111.788617886nm，反射率约0.0018332%，满足R≤0.1%；1周期、5次候选评价。测试显式短预算只用于该解析验收，产品默认仍无总量预算。该结果不是真实红外材料或制造验证。

本轮39项新增类型诊断已按窄类型契约、Qt属性接口与明确None拒绝分支修复，未加ignore或修改基线。最终剩余40项诊断的源码逐字节等于GUI阶段开始：31项Windows/平台属性差异、8项未改源码既有问题、1项缺失`scipy.optimize`类型桩。全仓门禁仍未通过，不把本轮新增0写成全仓0。现有静态配置、lock、baseline与启动器7项控制文件均保持原SHA。

首轮验收驱动曾因Qt delegate重建后读取已销毁对象、旧测试helper路径缺失失败；只修驱动并保留日志，随后同断言通过。另一次静态驱动误注入pytest helper路径，产生16项旧文件import-untyped差异；保留该轮56项报告，最终锁定静态进程不注入该测试路径。没有用忽略、基线刷新或安装依赖掩盖差异。

## 实现与历史版本

核心GUI独立：`film_auto_synthesis.py`、`film_synthesis_audit.py`、`film_physical_structures.py`等数值核心在本阶段未改。新表单目录/闭包适配位于`core/film_physical_generator.py`；会话仍由`film_design_session.py`/`project_film_session.py`唯一拥有。优化controller、`film_synthesis_quick_adapter.py`与既有QML负责接线；compute_queue/project_compute_views与CPU本地/远端准入沿现有队列契约。

完整当前文件SHA和本轮差异见[GUI冻结清单](../artifacts/film_synthesis_20261020/gui-integration-20261020/gui-freeze-manifest.json)。历史v3清单保持原SHA `24635ad4ae35de298880d471f122c3013e818e88a30ce9e16da665cdd5dcd621`，不能作为新增GUI验收。其62项中本轮改变7项：`remote_contract.py`、`remote_server.py`、README、algorithms、入口能力、物理结构契约、执行契约；逐文件新旧SHA均在新清单中。

有限网格与局部极值不构成连续证书；本阶段未扩大单面front与真实n/k的支持域、未实现phase/GD/GDD，未把Ge有限增层+TRF研究脚本产品化，也未改变历史双窗真实IR强目标仍有差距的结论。SciPy桩安装此前自动审核拒绝，精确拒绝记录见[原始证据](../artifacts/film_synthesis_20261020/v3-static/scipy-stubs-denial.json)；本轮未重试，保留父任务待决。
