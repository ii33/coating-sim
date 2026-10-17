# 旧版 Windows 便携运行时记录

本文只记录 2026-10-07 旧版便携运行时的布局和迁移设计，供识别已有包使用。当前仓库不再提供这些启动和构建脚本，也没有可核验的对应运行时 ZIP；本文不是当前安装或启动教程。安装部署统一使用独立的 [coating-sim-installer](https://github.com/ii33/coating-sim-installer)（[CNB 镜像](https://cnb.cool/nonereason/coating-sim-installer)），正式应用入口为项目解释器运行 `launcher.py`。项目侧边界见[安装与依赖合同](installation.md)。便携运行时 ZIP 与 `.coatasset` 设计/计算数据包不同。

## 首次启动与移动目录

ZIP 内的 `runtime-template.zip` 保存未绑定目标路径的 Conda 运行时。首次启动器使用 Windows 10/11 自带的 `tar.exe` 将其展开到 `runtimes/runtime-initial`，随后运行一次随包提供的 `conda-unpack` 路径修正，并把相对运行时名称写入 `active_runtime.txt`。启动器只使用包内 Python，不安装 Python、Conda、Qt，不联网，也不改系统 PATH 或安全设置。

移动整个已解压的便携目录后，启动器从旧运行时启动标准库 bootstrap。bootstrap 比较记录的目录位置；位置改变时，从 `runtime-template.zip` 在 `runtimes/` 下生成新的运行时，在新位置执行路径修正，再原子更新 `active_runtime.txt`。旧运行时和模板归档会保留，直到新位置的路径修正完成；`app/data`、`app/config`、`app/history`、`app/artifacts` 不参与替换。一次启动只尝试一次恢复，不会循环重启或自动联网安装。

首次展开运行时约需 2.4 GB。搬迁恢复期间旧运行时、新运行时和原始归档会暂时并存，请预留额外空间。修复失败时启动器保持旧指针、写入 `app/artifacts/logs/portable-startup.log` 并显示错误；请保留完整目录和 `runtime-template.zip`，不要移动或删除 `app/data`、`config`、`history`、`artifacts`。

## 包含内容与边界

ZIP 包含完整项目 Python 源码、PySide6/Qt/QML 与当前 CPU 运行依赖、图标和字体、材料光学目录、CIE 数据、离线算法文档和第三方许可记录。环境缓存、PDB、Conda 元数据中的机器缓存路径、branded host、应用数据库、日志、历史、附件、凭据和用户设置不打包。可写数据目录由首次启动创建。

CPU GUI 和仿真在本机离线运行。DirectML 是可选 GPU 后端，仍要求兼容的 DirectX 12 硬件及显卡厂商驱动。CUDA/CuPy、外部 LLM/MCP 服务、账号/API 密钥、第三方 CLI 和网络服务不包含在包内。

启动器不扫描或导入所有依赖，也不检查整套材料数据；启动时只做路径修正并启动 GUI。构建交付时会在独立目录运行一次功能冒烟验证。当前构建宿主并非全新 Windows 虚拟机，因此不能据此声称完成全新系统验收。

## 历史验证边界

2026-10-14 审计基线 `bc2fccc7` 已不含旧便携构建器、bootstrap、preflight
和 delta helper，也没有配套发行 manifest/SHA 文件。旧命令已移除，不能从当前
源码按本文重建或启动便携运行时。已有包的行为须以该包实际内容和对应版本说明为准。
Linux 文档/资源隔离 ZIP 仅验证选定文件的布局和字节一致性，不代表 Windows
首次启动、目录迁移、失败恢复或完整部署验收。
