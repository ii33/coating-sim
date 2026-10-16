# CoatSim 便携版（Windows x64）

便携版是 Windows x64 可启动运行时 ZIP，不是 `.coatasset` 设计/计算数据包，也不是维护者构建的离线安装器自解压 EXE。将完整 ZIP 解压到可写目录后，双击包根目录的 `Launch-CoatSim.cmd`。

## 首次启动与移动目录

ZIP 内的 `runtime-template.zip` 保存未绑定目标路径的 Conda 运行时。首次启动器使用 Windows 10/11 自带的 `tar.exe` 将其展开到 `runtimes/runtime-initial`，随后运行一次随包提供的 `conda-unpack` 路径修正，并把相对运行时名称写入 `active_runtime.txt`。启动器只使用包内 Python，不安装 Python、Conda、Qt，不联网，也不改系统 PATH 或安全设置。

移动整个已解压的便携目录后，启动器从旧运行时启动标准库 bootstrap。bootstrap 比较记录的目录位置；位置改变时，从 `runtime-template.zip` 在 `runtimes/` 下生成新的运行时，在新位置执行路径修正，再原子更新 `active_runtime.txt`。旧运行时和模板归档会保留，直到新位置的路径修正完成；`app/data`、`app/config`、`app/history`、`app/artifacts` 不参与替换。一次启动只尝试一次恢复，不会循环重启或自动联网安装。

首次展开运行时约需 2.4 GB。搬迁恢复期间旧运行时、新运行时和原始归档会暂时并存，请预留额外空间。修复失败时启动器保持旧指针、写入 `app/artifacts/logs/portable-startup.log` 并显示错误；请保留完整目录和 `runtime-template.zip`，不要移动或删除 `app/data`、`config`、`history`、`artifacts`。

## 包含内容与边界

ZIP 包含完整项目 Python 源码、PySide6/Qt/QML 与当前 CPU 运行依赖、图标和字体、材料光学目录、CIE 数据、离线算法文档和第三方许可记录。环境缓存、PDB、Conda 元数据中的机器缓存路径、branded host、应用数据库、日志、历史、附件、凭据和用户设置不打包。可写数据目录由首次启动创建。

CPU GUI 和仿真在本机离线运行。DirectML 是可选 GPU 后端，仍要求兼容的 DirectX 12 硬件及显卡厂商驱动。CUDA/CuPy、外部 LLM/MCP 服务、账号/API 密钥、第三方 CLI 和网络服务不包含在包内。

启动器不扫描或导入所有依赖，也不检查整套材料数据；启动时只做路径修正并启动 GUI。构建交付时会在独立目录运行一次功能冒烟验证。当前构建宿主并非全新 Windows 虚拟机，因此不能据此声称完成全新系统验收。

## 构建与校验

2026-10-14 本轮审计的源码基线（`bc2fccc7`）不含 `tools/build_portable_bundle.py` 及对应的 portable bootstrap、preflight、delta helper，也没有可核验的运行时 ZIP 和配套发行 manifest/SHA 文件。下面的命令保留为 2026-10-07 的历史构建参考，不能从该源码快照直接重建便携运行时。须先恢复并审核这些构建/启动入口，再在 64 位 Windows 用真实运行包验收首次启动、目录迁移、失败时保留旧运行时指针和数据，以及启动日志恢复。Linux 上的文档/资源隔离验证 ZIP 只证明选定文件的布局与字节一致，不能替代上述 Windows 运行验收。

构建器从 `.conda` 原环境打包，工具 wheel 只安装在 `build/portable_tools_condapack_20261007/`，不会更改原环境或用户级 Miniconda。完整 ZIP 和外置 manifest、SHA-256 文件位于 `dist/`。可在本项目根目录运行：

```powershell
$env:PYTHONPATH = "build\portable_tools_condapack_20261007\site;$($env:CONDA_EXE | Split-Path -Parent | Split-Path -Parent)\Lib\site-packages"
.\.conda\python.exe tools\build_portable_bundle.py --conda-pack-site build\portable_tools_condapack_20261007\site
```

输出目录或 ZIP 已存在时，构建器会拒绝覆盖。SHA-256 请以 `dist/*.sha256` 和外置 manifest 为准。
