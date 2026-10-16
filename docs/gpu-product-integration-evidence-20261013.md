# GPU 外观渲染产品接入：2026-10-13 验证记录

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

## 结论

产品现有外观窗口和本地共享 `appearance_render` 队列已接入显式选择的
`gpu_cuda/mixed` 后端。真实 Windows 11 / RTX 4070 Ti SUPER 上，保存的
HR54 圆形基片资产经**产品共享队列**完整提交、执行、校验和结果注册：
54 层、161 节点逐层场、半径 150 mm、基片厚度 12 mm、405–830 nm 的
86 个波长，64×48、4 spp 的受控冒烟任务终态为 `succeeded`，
`rendered_unqualified`，所有 fatal 光学计数均为零。队列最后关闭了本次
私有 daemon，原项目源码和大资产未修改。

这是**低分辨率产品流程冒烟**，不能外推为 512×384、64 spp 的产品性能验收，
也不是颜色/成像物理准确度认证。此前两张高分辨率正视图由独立已验证研究脚本生成，
不能替代当前产品 UI/队列的高分辨率验收。新统一主机选择器在本记录时仍在集成，
其实际 UI 操作未在 Windows 原生环境 复验。AMD/Intel GPU 后端未实现。

## 实机队列证据

- 主机：SAKUYA，Windows 11，Mitsuba 3.9.0，Dr.Jit 1.4.0，
  `cuda_ad_rgb`，RTX 4070 Ti SUPER。
- 任务 ID：`e0714430e5204a03bbbf37bbe368b61a`；终态 `succeeded`；
  已注册结果索引 SHA-256：
  `42dc6903294c62d98bb20c2bb6313cd4c9d06ab8b8013d401a4447c2c6410614`。
- 渲染全流程 55.944 s，其中第一次 GPU 调用 50.720 s。证据标为
  `cold_first_call`；未证明 JIT 缓存未命中，不称为确定冷编译时间。
- 冻结封装 37,326,817 B，JobSpec 37,326,907 B，
  实际紧凑提交消息的重建字节数 37,327,027 B。
  真实 HTTP body 未另行留存；该重建采用已接受的同一 spec、job ID 和 client 编码。
- 输出 `input-job.json` 36,842,226 B，完整字段与资产冻结 job 相等；
  86 个波长记录完整，fatal 分类均为 0。注册结果索引、manifest 和 PNG
  分别经 SHA-256 与长度核对。
- 补丁包 SHA-256：
  `fb80aecda6b8a3d97bd8513b85a4c555f2a70d4825ecf712c7d458babc68ce04`。
  对隔离副本 9 个源码文件先检查原哈希（新 limits 模块须原不存在），
  再应用并核对新哈希；无盲目覆盖。完整原始队列验收 JSON SHA-256：
  `d3e29cd5e310da5e4705c04c3f29fc186e89f769cd291187ace4b7c475f8d94d`。

先前 sandbox 内的 localhost daemon 启动/通信任务超时发生在提交前，
未产生 GPU 队列任务。随后同一源码在获准的隔离执行环境和新的私有 root
中成功；没有更改原项目 ACL、系统安全限制或安装软件。

## 数据和预算门禁

冻结适配器继续保留真实逐层厚度、材料光学数据、光源和两面层序，圆形边界
按已声明的 `captured_node_mean_with_local_nearest_fallback` 预览约定。
公开控制请求、UI worker 输入和冻结子进程输入的上限为 16,000,000 UTF-8 B；
完整场的冻结封装和共享 JobSpec 的上限为 60,000,000 B；本地 RPC 仍保持
64 MiB 的真实传输 body 门禁。超限拒绝，不能裁剪场、改用名义厚度或静默降低
波长/像素/spp。结果和 GPU job copy 在读取前检查大小；冻结资产、job、队列 spec
和结果索引仍以内容摘要绑定。

原 GPU `input-job.json` 美化输出会把 54 层大场膨胀到约 92 MB，导致后续
结果读取门禁拒绝。生产写出改为**无损紧凑 JSON**，字段和数值未改变；
对应真实资产云端 job copy 从 91,976,858 B 降至 36,705,127 B，
读回字段及 job SHA-256 一致。

## 云端验证和边界

另一份真实 HR54 161 网格 v7 资产（SHA-256
`c192a7e211614573d53ba0494772a9c3d76af772720f53d5a7ea4505d91d4337`）
在云端经独立冻结子进程、JobSpec 解析和真实 SQLite SchedulerStore 提交：
封装 37,185,831 B，JobSpec 37,185,921 B，紧凑本地提交消息 37,186,028 B；
公开 `estimate_appearance` 与 `submit_appearance` 的 prepared/确认门也成功。
这些检查不启动 CUDA，不能充当硬件渲染证据。

预算补丁后的云端回归：`tests/test_appearance_*.py` 为 105 通过、1 跳过、
66 subtests 通过；GPU 原型/可选采样预算/Qt 界面/光线追迹工作区组合为
59 通过、3 跳过；scheduler service/main/compute confirmation 组合为
32 通过、30 subtests 通过。测试运行在主机选择器新增改动之前；
随后统一主机下拉冻结版本的外观 GPU Qt、local worker、预算、工作区与
compute target 组合再测为 38 通过、10 subtests 通过。共享下拉提供主动
CUDA 检测按钮；探测成功前 CUDA 设备保持不可提交，后续可明确选择，
原本的 QProcess 真设备探测没有移到 GUI 线程。含新 UI 和队列的完整
同进程测试由后续冻结快照另行验收；新增主机 UI 仍未在 Windows 原生环境 实机操作。

UI 局部 GPU 子进程此前在 Windows 原生环境 完成 86 波长、混合精度、零 fatal 的同类小图，
首次波长边界取消返回 3、无结果 wrapper。此记录未证明共享队列硬件取消、
完整分辨率产品运行或跨品牌 GPU。完整输出仍标记 `rendered_unqualified`。

## 实现与复现入口

核心：`coat_sim/appearance/gpu.py`、`coat_sim/appearance/limits.py`、
`coat_sim/core/appearance_compute.py`、`appearance_freeze_worker.py`、
`appearance_execution.py`、`appearance_result.py`、
`coat_sim/scheduler/{client,daemon,jobs,worker}.py`，以及
`coat_sim/ui/{appearance,appearance_worker}.py`。
研究 CLI `tools/validation/gpu_appearance_preview.py` 只是生产 GPU 核的兼容入口。

隔离硬件冒烟脚本为 `tools/validation/product_gpu_worker_smoke.py` 与
`tools/validation/product_gpu_queue_smoke.py`，必须提供原资产哈希绑定的
`--source-job`、`--source-asset`、私有 `--project-root`，队列脚本还需指明
现有 `--runtime-python` 并预先为私有 root 准备指向该 runtime 的可撤销链接。
脚本不得对原项目根目录写入资产或运行输出。参见
`docs/appearance-render-contract.md` 和 `docs/gpu-preview-contract.md` 的物理适用边界。
