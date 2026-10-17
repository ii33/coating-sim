# 安装入口与项目依赖合同

安装器独立维护于 [GitHub: ii33/coating-sim-installer](https://github.com/ii33/coating-sim-installer)
及 [CNB 镜像](https://cnb.cool/nonereason/coating-sim-installer)。
Windows 单文件 EXE、引导、依赖部署、代理/镜像、更新检查、快捷方式和安装器构建说明，
统一使用该项目的 [Release](https://github.com/ii33/coating-sim-installer/releases)、
[中文说明](https://github.com/ii33/coating-sim-installer/blob/main/README.md)及
[English guide](https://github.com/ii33/coating-sim-installer/blob/main/README.en.md)。
主项目不再携带安装器实现或安装脚本。应用源码镜像仍为
[CNB coating-sim](https://cnb.cool/nonereason/coating-sim)。

## 已发布 alpha.1 的消费边界

已核对安装器 `v0.1.0-alpha.1` / `8010c9e29afe129f61e7d4d3eb1784a48e0eb459`，
GitHub 与 CNB tag 指向相同提交。该 EXE 内置安装逻辑及 Python/Git 清单，
不读取目标源码中的 `tools/dependency_manager`、安装引导或占位 manifest。
目标项目只提供声明和应用资源；安装器不作为应用运行依赖。

| 项目侧保留接口 | 原因 |
|---|---|
| `requirements.txt` | 默认 desktop 的科学计算、Qt、助手及互操作依赖 |
| `requirements-test.txt` | appearance 从此取 Mitsuba/DrJit；开发模式另取 pytest/mpmath，不能按文件名误删 |
| `requirements-analysis.txt`、`requirements-analysis-lock.txt` | 开发模式直接 Ruff/mypy 声明，以及项目静态分析复现 |
| `requirements-sandbox.txt` | 高级 CLI sandbox 组及应用沙箱环境 |
| `requirements-remote-service.txt`、`environment.yml` | 主程序远端服务部署与环境规范，保留原用途 |
| `coat_sim/`、`launcher.py` | 部署识别、应用启动及计算设备实际数值探测 |
| `coat_sim/version.txt` | UTF-8、不超过 128 字节的既有开发版本格式 |
| `coat_sim/ui/assets/icons/launcher.ico` | 启动入口计划即使不创建快捷方式也校验此资源 |
| `tools/run_tests.py` | 开发部署后的 quick 测试入口 |

alpha.1 的根 requirements 采用既有简单包名/版本声明。不要改成仅有 pyproject，
也不要在这些消费接口中引入任意 pip 参数、URL 或 markers；更改语法须先协同升级安装器。
Python/Git 下载清单、安装实现及发行许可记录由独立仓库维护，不在主项目保留副本。
接口证据：[catalog](https://github.com/ii33/coating-sim-installer/blob/8010c9e29afe129f61e7d4d3eb1784a48e0eb459/tools/dependency_manager/manager.py)、
[launch entries](https://github.com/ii33/coating-sim-installer/blob/8010c9e29afe129f61e7d4d3eb1784a48e0eb459/tools/dependency_manager/launchers.py)、
[updates](https://github.com/ii33/coating-sim-installer/blob/8010c9e29afe129f61e7d4d3eb1784a48e0eb459/tools/dependency_manager/updates.py)。

## 主项目保留的资源与运行工具

`tools/distribution_resources.py` 仅暂存和校验应用源码、静态资料、文档及资源 ZIP，
排除本地私有设计和可变数据库；`tools/audit_distribution_graph.py` 调用它做资源审计。
它不下载 Python/Git、不安装依赖、不生成安装 EXE，也不导入独立安装器。
正式启动入口 `launcher.py`、应用内使用的 `launcher.pyw` 桌面适配、桌面宿主构建、
工程资产包及远端计算部署工具继续归主项目所有。重复的启动包装脚本及旧 UI 启动器已移除。
历史便携运行时文档只记录当时的构建方式，不是当前可用安装入口。应用计算设备能力仍由 `coat_sim` 运行时探测，不由安装推荐替代。
