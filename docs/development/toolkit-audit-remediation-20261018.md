# 可迁移工具集与审计整改记录（2026-10-18）

基线：9c7c900。内部版本：dev.20261018.01。本文件记录实现范围，不代表公开发布、真实 provider 或硬件验收。

## 后端迁移边界

工具定义、严格参数、结果封套、文件策略和状态模块在 `coat_sim.agent.toolkit`。宿主以 authority/cancelled/显式根目录和 provider 注入环境能力；现有后端通过 `adapter.py` 与 `pydantic_adapter.py` 接入。换 SDK 不应复制业务工具或去掉权限检查。

设置默认关闭，能力取显式全局列表、显式预设上限和已激活声明式预设的交集；空列表不代表全开。活动预设在本轮只能收窄，撤销预设不会扩大当前请求权限。persona/任务面板是有界非可信上下文，不能授权或改变程序模式。任务命名空间在同一授权工作区内用于组织，不是租户安全隔离。

已提供文件、图像、分块/合并/打包、任务和声明式预设工具。文本文件默认 1 MiB；文档处理上限 32 MiB、分块及清单 1 MiB；图片编码上限 8 MiB 并限制像素，模型回传沿用每轮 8 张/32 MiB。执行/安装/网络/SFTP/OCR 工具具备严格宿主接口，但桌面尚未注入这些真实 provider；不会自动安装依赖、关闭 TLS 或回退为无隔离本机执行。配置主机管理和远程搜索采用当前保守子集，差异见能力表。

## 审计项与对应回归

| 原审计编号 | 实现落点 | 主要回归 |
|---|---|---|
| AS-01/02/03 | tool_dispatch/runtime：编辑去重与提交去重分开、分析角色硬只读、动态界面读取不受静态重复缓存限制 | test_assistant_audit_regressions.py |
| MCP-01/03/04 | 全工具 SDK 前严格校验、保守 annotations、修订技能说明 | test_mcp_audit_contracts.py |
| MCP-02 | 外部 MCP 项目许可统一严格布尔语义 | test_external_mcp_project_permission.py |
| PL-1/PL-2 | ProjectStore 整体读改发布/快照锁、原代际校验、选择性导入保留原指纹 | test_project_audit_concurrency.py、test_project_transfer_publication_generation.py |
| PL-3 | 关闭日志失败区分已释放所有权，一次告警和显式确认，不改写新所有者日志 | test_project_runtime_contract.py、test_project_recovery.py |
| 计算 C1/C2/C3 | 参考/冻结支持域一致、只计算有效膜层、单波长设置可重复规范化 | test_audit_compute_cli_contracts.py |
| CLI-1/2/3/4 | 显式根目录、采样互斥、清理与 JSON 错误 | test_audit_compute_cli_contracts.py |
| C-01 | 本地/远程接入后台化、冻结设备绑定、稳定任务身份、未知接受/取消反馈 | test_film_optimization_remote_async_qt.py、test_remote_ui_async_qt.py 及配套队列测试 |
| C-02 | 无 GUI 依赖的统一资产到外观请求适配器 | test_appearance_asset_adapter_parity.py |
| QD-01/02 | 内容固定的公共参考资源清单、链接/特殊文件拒绝、暂存和 ZIP 发布边界 | test_distribution_resources.py |
| QD-03 | 引号含空格/转义/换行的秘密值完整脱敏 | test_assistant_diagnostics.py |
| QD-04 | README 指向 version.txt，保留历史版本记录 | 版本脚本检查及文档复核 |

## 尚需独立验证或后续阶段

- 原生 Windows 窗口行为、文件系统恶意并发、真实 GPU 和外部 provider/DSH 接通。
- 工具集只串行化本进程合作调用；外部进程文件并发仍需宿主隔离或更强事务协议。
- 全仓静态历史债务持续削减；不通过增加基线掩盖本次新错误。
- Windows/硬件 CI、生产依赖锁定矩阵、覆盖率/故障注入目标的持续建设。
- 真实研究资料目录仍由用户显式配置；通用工具工作区不自动替代研究库。

先通过离线契约与回归，再检查最终提交 CI；不把合成测试计数相加作为独立用例总数，不把此前 DSH 工具报告的 29/30 实测成绩用于本实现。

## 本批离线验收

合并定向回归：574 通过、4 跳过（86.49 秒）。最终静态门禁：827 个源文件，新增诊断 0、未清除的已解决条目 0、致命语法错误 0。只移除了 6 条经核验的历史豁免，没有添加或扩大豁免。历史诊断仍为 mypy 4138、qmllint 1832、Ruff 1479，不代表全仓无问题。
