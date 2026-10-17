# 主项目目录边界

本仓库维护 coating-sim 主程序。安装器由独立的
[`ii33/coating-sim-installer`](https://github.com/ii33/coating-sim-installer)
维护；依赖消费合同见[安装与依赖](../installation.md)。

## 根目录保留内容

| 文件 | 用途与保留原因 |
| --- | --- |
| `README.md`、`README.en.md`、`LICENSE` | 双语入口与许可 |
| `AGENTS.md` | 当前仓库维护约定 |
| `agent.md` | 助手运行时读取的提示词资源，不是历史交接记录 |
| `launcher.py`、`launcher.pyw` | 正式入口及应用内无终端适配；独立安装器调用 `launcher.py` |
| `requirements.txt`、`requirements-test.txt` | 运行依赖与外观渲染依赖；后者也含默认外观功能需要的 Mitsuba/DrJit |
| `requirements-analysis.txt`、`requirements-analysis-lock.txt` | 静态分析与可复现依赖约束 |
| `requirements-sandbox.txt`、`requirements-remote-service.txt`、`environment.yml` | 沙箱、远端服务、Conda 环境规范 |
| `pytest.ini`、`mypy.ini`、`ruff.toml` | 测试与静态分析配置 |
| `.gitignore`、`.gitattributes` | 本地状态隔离、源码格式与发布边界 |

## 目录用途

- `coat_sim/`：应用、Qt Quick UI、核心算法与运行时资源。
- `config/`、`data/`：运行配置和资源；用户生成状态按 `.gitignore` 排除，不能按目录名整体删除。
- `docs/`：当前文档；`docs/development/handoffs/` 是注明日期的历史交接记录。
- `example/`：包含 `project.coatproj` 的完整合成演示工程及构建/验证脚本。
- `examples/`：单独的机器、源、基片、工艺等示例文档；与 `example/` 用途不同。
- `legacy/`：冻结 Tk UI 源码；算法说明仍引用原实现，`test_mask_designer.py` 仍测试其中的曲线函数。
- `scripts/`、`tools/`：在用的诊断、性能、回归、资源审计及桌面宿主工具。`tools/setup_sandbox.py` 为应用沙箱功能准备环境，属于主程序。
- `tests/`、`.github/`：回归与 CI。
- `artifacts/`、`.dependency-manager/`：忽略的本地验证/安装状态。安装器可能正在写日志，清理源码不移动其活跃目录。
- `tasks/`：只有按 `.gitignore` 排除的本地资源审计工作区可留在此处；已跟踪交接记录已归档至文档目录。

## 2026-10-17 清理记录

已删除硬编码 `.conda` 且不定位工作目录的 `start.bat`、`dev.bat`。删除仅用于一次性创建/修补外部
`other/ai` 项目的六个脚本：`extract_ai.py`、`finalize_ai_extraction.py`、
`fix_ai_paths.py`、`fix_ai_integration.py`、`fix_ai_verification.py`、`write_ai_docs.py`。
应用、测试和 CI 没有调用它们；资源审计工具保留这些历史文件名的只读识别能力，
以便它们被重新带入时仍报告，绝不执行。

`tools/diagnose.py` 保留作手动导入/语法诊断，已移除“由 start.bat 自动调用”的过时说明。
两份 2026-10-11 交接文档内容原样归档，生成验收哈希的工具使用新路径；
既有历史验收记录不重写。删除的跟踪文件均可由 Git 历史恢复，未删除本地环境、
用户工程或运行日志。

### 统一正式入口

继续删除重复的 `start-dev.bat`、`start-dev.ps1`、`start.sh`、`start-cloud.sh`，
以及对应包装测试 `tests/test_start_scripts.py`。源码直接使用项目 `.venv` 解释器运行
`launcher.py`；独立安装器生成的脚本与快捷方式使用相同协议。Windows 开发命令使用
PowerShell 7+，无需另存启动脚本。仓库外已有的固定启动文件不属于此次删除范围。

删除失效的 `legacy/launcher_gpu.py`、`legacy/launcher_tk.py` 与配套
`legacy/launcher.qml`；它们指向旧独立模块和已不存在的背景目录，不再属于当前
工程启动流程。`legacy/` 中算法说明和曲线测试仍依赖的实现保持原样。
`launcher.pyw`、`tools/launch_desktop.pyw` 仍被应用内重新打开、任务栏与桌面宿主
使用，继续保留；`tools/setup_sandbox.py` 是现有 LPAC 功能的可信环境准备工具，
没有被独立安装器替代。

旧云包装器专用 `tools/check_cloud_runtime.py` 及其四个预检测试同时移除；实际通知音降级测试继续保留。依赖检查与部署由独立安装器维护。
