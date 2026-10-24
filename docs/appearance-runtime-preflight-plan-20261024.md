# 外观专用运行时与预检的最小设计（待执行）

状态：2026-10-24 设计稿。基于 `dev.20261024.03` / `f41901bfdc709c67f53131acedd10f40c4c1537e`。本次仅修复独立的沉积探针退出码缺陷；未创建、安装或变更任何运行环境，未修改独立安装器。用户随后要求安装器提供自包含的完整硬件运行时，同时包括适用的 CUDA 与 DirectML；该后续实现和新测试目录内的部署由独立任务负责，本提交不宣称已完成运行时改造或实际外观队列验收。

## 保持的边界

- 本地外观继续只接受项目根、源码根下既有的 `build/mitsuba_appearance_env`，顺序和去重方式与当前 `ui.appearance._worker_python`、`scheduler.daemon._appearance_gpu_python` 一致。
- 不增加 PATH、注册表、全局包、其他工作树或缓存目录搜索；不把应用 `.venv` 作为隐式回退；不创建 junction、symlink 或伪解释器掩盖缺项。显式 GPU 请求失败不能发布 CPU 成功结果。
- 专用环境隔离 renderer 的 NumPy/native extensions/DLL 与应用 Qt/Torch。沿用生产 worker 的环境清理、源路径和输出校验，不复制或移动现有 venv。
- 唯一状态所有者：安装器拥有显式批准的依赖部署；项目运行时解析器拥有执行路径判定；GUI 只呈现可用性快照；scheduler/worker 拥有作业及终态。输入冻结成功不是运行时就绪或任务成功。
- 不涉及膜系 `raw_coverage`、请求/结果序列化、物理算法或数值阈值。

## 部署方案与改动范围

只读审计已验证：现有官方通用安装器可为明确的 `--venv <项目>/build/mitsuba_appearance_env`、`--python <现有完整 base Python>`、`--groups appearance`、`--profile runtime` 生成新环境计划。`backend=cpu` 在该计划中只是不加入沉积 CuPy/DirectML，并不删除 Mitsuba 的 CUDA variant。当前可复用的正式 base 是 CPython 3.13.16；计划没有覆盖应用 `.venv` 或 base 的动作。

该计划尚未执行 binary-only resolver、下载或安装，不能标为 renderer ready。实际执行前需展示新目标、完整解析包清单/大小/哈希、环境影响和计划身份；批准后只在新环境执行。现装等价七根依赖闭包约 392.6 MiB，仅为文件占用参考，不包括 pip/venv、wheel cache、临时发布、回滚和 JIT cache。没有已核验完整离线 wheelhouse 时需联网解析及下载。

当前一键 `appearance` 仅把依赖安装到应用 `.venv`，未创建产品要求的专用目录；文档所称 `requirements-appearance-win-cp312.lock` 也不存在。后续安装器应补明确的专用环境 provisioning/repair 和完整 ABI 锁清单，保留原应用环境。不能把缺失的旧 lock 当作已实现，也不通过宽泛版本兼容或隐式解释器回退修复。

项目侧最小改动建议是抽取上述两个重复解析器为一个只读解析入口，保留原固定候选及现有调用边界；GUI 和 daemon 使用同一执行路径。该抽取及安装器修改均未在本次实现。

## 项目内预检方案

以下步骤是待部署后的检查顺序，尚未执行，也不是新增的可调用 CLI：

1. 使用生产解析器确定唯一解释器，记录 Python/version/prefix、包版本和源提交等非敏感身份。路径缺失即报告 runtime missing，不创建目录或尝试其他解释器。
2. 在该解释器、生产 worker 环境清理规则下启动 headless 子进程，导入 NumPy、Pydantic、Mitsuba、DrJit，并执行 `from coat_sim.core.schemas import design_data`，核对 ABI 锁清单。检查不应靠加载应用 PySide6 或混合 site-packages 成功。
3. CPU 执行现有小 scalar 渲染探针；GPU 执行现有 `coat_sim.appearance.gpu --probe`，要求真实 CUDA 分配、`cuda_ad_rgb`、子进程正常 exit 0 和有效 JSON。包 metadata/variant 名称存在不能代替执行检测。沿用已有界限，不盲增超时。
4. 分别呈现“包部署完成”“headless 预检通过”“CUDA 分配通过”；所有状态仅在被检测解释器、提交和设备范围内有效。失败/超时/退出异常保留具体原因，未检查的状态保持未验证。
5. 安装批准和上述预检成功后，在独立任务根复制当前随附示例，执行真实沉积生成资产，再运行 8×6、2 波长、4 spp 的 CPU/GPU 外观队列。核对实际 backend/precision、进度、取消边界、终态、注册输出哈希、数组有限性、PNG 尺寸和非零像素。不得把 CLI 渲染或输入冻结成功计作队列通过。
6. 只停止本次根目录下可验证身份的 scheduler/worker；真实桌面 API 禁用期间，桌面点击和动画继续标为未测。

预检应只读运行时，不自动修复、下载、创建环境、改驱动/安全设置或读取认证信息。运行时导入失败与硬件不可用分开报告。GPU 低采样输出继续保留未资格化状态，不修改精度或物理阈值。

## 拟定回归

- 解析器只使用两个原有固定候选；缺失、不完整环境与错误解释器有明确失败，不能搜索到应用/全局 Python 后变为可用。
- 标记成功后 exit 7、未捕获异常、超时、不完整 JSON 均不可标为可用。沉积探针的这组真实子进程回归在本次实现；外观既有正常退出判定继续保留。
- 新专用环境必须同时通过资产 schema headless 导入和 renderer 探针；仅装了 Mitsuba/DrJit metadata 的环境不能报告完整就绪。
- 安装器 appearance 默认流程最终解析到的解释器必须与 GUI/队列相同；对齐后才能报告部署完成。安装器代码和跨仓测试由其维护线程另行实施。
- 实际队列回归记录真实执行身份和资产/图像证据，不以 mock 或假路径作为环境已部署的证据。
