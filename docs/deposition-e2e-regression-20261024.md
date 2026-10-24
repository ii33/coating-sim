# 沉积、光追与外观回归：2026-10-24 / sakuya

## 结论与隔离

已确认并修复 `.02` 随附示例和生成器仍使用旧优化配置的问题。新版本严格接受优化编辑配置 v2，而示例仍含 v1，导致项目材料依赖解析和光谱/优化页面恢复失败。修复仅将公开合成示例保存为当前嵌套条件，保留逐点 R 范围 0–1.3%、500–600 nm / 11 点、全部光学和沉积参数、文档 ID 及路径；未增加运行时兼容、修改算法、放宽数值阈值或静态物理基线。

同时修复端到端验证脚本的失效启动器替身和空文件哈希检查，以及两个过期测试断言。真实桌面验收仍未完成，不能据下述离屏 Qt、真实进程或 PNG 结果宣称桌面点击和动画通过。

| 身份 | 值 |
|---|---|
| 主机 / 解释器 | sakuya；正式项目既有 `.venv/Scripts/python.exe`，Python 3.13.16 |
| 固定仓库初始身份 | `C:/Users/youai/Documents/coating-dev/coating-sim`；main / origin-main 本地引用均为 `2d9006a82ab94ec95da60111c833b7f2c7bbce90` |
| 固定仓库初始工作树 | 75 tracked 修改，14 个默认 porcelain untracked 条目；本任务未改其源文件 |
| `.01` 隔离目录 | `C:/Users/youai/AppData/Local/Temp/cs-dep-reg-20261024`；基线 `2d9006a` / `dev.20261024.01` |
| `.01` 测试修复记录 | `ec02055002513d27c4599930f7d03191791547f9`；仅 3 个测试/验证文件 |
| `.02` 隔离目录 | `C:/Users/youai/AppData/Local/Temp/cs-dep-reg02-20261024`；基线 `8f0cd958b912477fc6d62b2a8769d98c835fe3d5` / `dev.20261024.02`，父提交 `2d9006a` |
| 发布 | 本任务未推 main、GitHub、CNB 或安装器；未操作另一集成 worktree |

下文证据目录均为各自隔离目录中的 `artifacts/deposition-e2e-20261024/`。`.01` 已保存 `baseline-identity.json` 和覆盖 613 个证据文件的 `evidence-sha256.json`；后续 `.02` 运行使用另一目录。`.02` 修复前和修复后的报告分别保留，不能把带修复的工作树报告标成未修改的原始提交。

## 真实链路与数值证据

| 范围 | 结果 | 证据 |
|---|---|---|
| 实际沉积 scheduler / worker | 两版小任务成功，生成可验证结果及资产；修正板优化小案例成功，实际积分 dt=0.0125 s；真实结果保存失败被区分为 `result_save_failed`，后续任务仍成功 | 两版 `artifacts/scheduler-validation/real-checks.json`，脚本最终 exit 0，6 项检查 |
| MainBridge → scheduler → worker → 图像提供器 | 两版均通过；双源 + 板，21×21、317 有效点，均值约 2 nm，范围 0.990199–2.504478 nm；真实 21×21 预览 1640 个非零 RGBA 通道 | 两版 `project-check-*/report.json`、`deposition-preview.png`，进程 exit 0 |
| 重跑 / 结果刷新 / 持久化 | 网格 21→25，结果尺寸和 image revision 1→2 同步更新；保存配置后重开为网格 25；新项目包导出/导入得到新项目身份、相同输入闭包 | 同上；实际 API/进程，未以 mock 代替计算 |
| 算法中途取消 / 重跑 | `.01` 在 integrate=71.2%、`.02` 在 integrate=93.6% 接受取消，worker 终态 cancelled、无结果；之后 CPU 小任务成功 | `audit-deposition/gpu-cancel-*/gpu-cancel-evidence.json`；`audit-deposition02/gpu-cancel02-*/gpu-cancel02-evidence.json` |
| 单源 + 1 板 | 21×21 / 317 有效点，均值 24 nm，范围 23.7714296152–24.2354947465 nm；精细 474、周期 237 点，最大差 1.4211e-14 nm | `.01` `audit-deposition/actual-numeric-checks.json`；`.02` `audit-deposition02/numeric02-evidence.json` |
| 双源 + 2 板 | 均值 48 nm，范围 44.1253971457–52.9092270494 nm；精细 948、周期 474 点，最大差 2.1316e-14 nm；源贡献加和误差 0 | 同上；两版同输入文件哈希，所有对应厚度场逐值差严格 0 nm |
| 实际沉积资产 → 外观完整生产链路 | 复制随附示例 → process scheduler/worker → `.coatasset` → `appearance_context` → 固定冻结子进程 → appearance scheduler/worker → 输出索引和全部文件哈希验证，两版均成功 | `.01` `audit-render/new-e2e-*/e2e-final-report.json`；`.02` `audit-render02/actual-e2e02-comparison.json`，命令 exit 0 |
| 上述跨模块像素与数组 | 沉积 21×21 / 12 点，317 有效节点、226 非零，mean=70、min=0、max=99.08609930921882 nm；CPU 外观 8×6 / 2 波长 / 4 spp，谱/XYZ/RGB 全有限，PNG 112 非零通道、max=182、42 种 RGB | 两版真实沉积及逐层场、三份外观 float 数组严格相同；PNG SHA256=`09b5675b7a775ecd18403b5f551878886d47ecc05ee78446f2cc61e012e1aa55` |
| 几何光追 | 裸片/涂层片 2 界面、3 射线、2 波长；6 条全部命中探测器，能量闭合；裸片 T=0.9216，涂层 T 约 0.85832249 / 0.81464068；取消抛 InterruptedError | 两版 `audit-render*/raytrace-summary.json`；`.02` 与 `.01` 结果 JSON 完全相同，进程 exit 0 |
| 外观并行与取消 | CPU 1/2 workers 三份数组逐值相同；CPU / CUDA 都在完成首个真实波长后取消，不能发布完整 manifest/display；新目录重跑成功 | `.01` `audit-render/parallel-parity.json`、`render-cancel-summary.json` |

沉积厚度为目标均值归一化模型的输出，证明有限性、空间分布、周期分支和源加和，不能据此宣称绝对生长率已标定。含静止遮挡的公开示例预期存在 0 nm 节点，不应为追求全部正值而改阈值。上述周期一致性使用完整回归周期，不构成任意残余时长的统一精度证书。

## CPU / GPU 身份与限制

- CPU 外观实际使用 Mitsuba 3.9.0 `scalar_rgb`，DrJit 1.4.0；CUDA 探针实际分配 `cuda_ad_rgb` 设备。生产 GPU 清单为 NVIDIA RTX 4070 Ti SUPER、驱动 617.42、16376 MiB；精度 float32。LLVM 初始化警告保留在日志，scalar CPU 和 CUDA 均实际完成。
- 两版裸片和双层圆片 CUDA 小图均真实运行，所有每波生产 diagnostics 通过，致命非有限、能量、几何和方向诊断均为 0；数组有限、PNG 非零。4 spp 的生产 CPU 比较状态保留为 `rendered_compared_unqualified`，没有宣称高采样色准或 GPU/CPU 精度资格化。
- `.01` / `.02` CUDA PNG 逐 byte 相同。新增逐 bit 原始数组诊断观察到 spectrum 最大差 5.96e-8、XYZ 最大差 3.81e-6、linear_sRGB 最大差 1.53e-5；同版本同输入重复也有微差。相关 renderer 源文件和 job 输入哈希相同。初始严格比较失败和重复结果均保留在 `.02/audit-render02/gpu-repeat02-analysis.json`，未改产品阈值，不能将其直接定性为本次代码回归。
- **`.02` GPU 外观队列实际失败，不能算通过。** 同一真实沉积资产、8×6 / 2 波长 / 4 spp，GPU 冻结预检通过、提交成功，job `actual-example-appearance-gpu02` 最终 `failed / worker_start_failed`，协议进程 exit 1。错误原文为 `gpu_cuda appearance queue requires isolated build/mitsuba_appearance_env with Mitsuba/Dr.Jit CUDA; no application-interpreter fallback`。worker 未启动、result=null、没有该作业的 PNG，也没有 CPU 降级。属于所需隔离运行时缺失，不能将先前同机 CLI CUDA 成功冒充队列成功；未改运行时选择或绕过 gate。证据 `.02/audit-render02/gpu-scheduler02-report.json` 及命令退出码、原始日志。
- 两版显式沉积 GPU / DirectML 的真实作业分别因缺少 `cupy` / `torch_directml` 而 `failed / algorithm_failed`。结果为空，读取结果返回 `job_result_unavailable`，无提交 NPZ；不存在 CPU 静默降级。这是沉积后端依赖缺失，未完成相应 GPU 沉积硬件验证；外观 CUDA 可用不能代替此项。未安装包、驱动或修改安全设置。

## 失败、定位与最小修复

1. **`.02` 随附示例不符合当前协议。** 六模块修复前结果为 6 failed / 52 passed；其中 5 项示例失败于 `ProjectError(unsupported_version, "Unsupported film optimization material-reference configuration version")`。生成器也仍向目标添加被新版拒绝的顶层 `upper_bound_percent`。修复生成器使用 `pointwise.upper_bound_percent` 并明确 11 点采样；仅重写示例中的优化配置及其冻结副本，并更新两文件 manifest 哈希、snapshot/import 哈希。`example-schema-update-proof.json` 证明其余物理及仿真输入未改、文档/项目 ID 未改；没有运行时迁移或 fallback。新增生成器回归和示例当前协议断言。
2. **验证脚本启动器替身过期。** `tests/scheduler_real_checks.py` 原假 Harness 缺少 `_runtime_shutdown_callback`，未能执行现版 shutdown。改为真实 `LauncherBridge` 注册离屏 `QQuickWindow`，断言关闭后 observer 已 detach、busy 清除，随后在真实 scheduler 确认 admitted job 成功，保留 MCP / CLI 状态一致检查。这是离屏生命周期证据，不是 MainWindow 桌面点击证据。
3. **输入哈希检查原为空。** fixture 实际使用 `.csrc/.csub/.cmsk/.cmac/.cproc`，脚本却只哈希 `*.json`，原证据为 `{}`。改为核验五类实际设计文件并要求集合非空，未降低检查强度。
4. **过期断言。** `.01` 示例默认 synthesis options 漏了 `randomize_template_search=false` / `structure_search_seed=null`；`.02` 测试仍注入已拒绝的旧 flat strict 字段。更新为当前明确字段。Windows `QUrl.toLocalFile()` 的 `/` 与 pathlib 的 `\` 只在文本表示上不同；修正板候选已实际保存正确，测试改为核对解析后的路径和候选索引，保持原覆盖/取消/数值断言。

原始失败日志均保留。生产验证 `example/verify_example.py` 修复后 exit 0：R 最小 0.012688886230938414、最大 0.02537804301580716、R+T 最大残差 8.88e-16；原测试的 1e-9 光谱基线和 1e-12 能量阈值均未修改。

## 自动测试与可复现入口

`.01` 物理初轮 JUnit 有 32 个 testcase（31 通过、1 个旧示例断言失败，另有 subtests）；示例断言修复后在后续界面回归通过。`.01` 六模块界面/工程运行 57 passed / 1 路径表示断言失败，另 30 subtests passed；该路径项定向复验 1 passed。`.01` 外观/圆片/保存资源定向运行显式 `--run-gpu`：57 passed / 0 failed / 0 skipped，另 10 subtests passed。各次运行分别报告，不相加冒充独立全套覆盖。

`.02` 六模块修复后为 **59 passed / 0 failed / 0 skipped，另 30 subtests passed**，94.64 s，进程 exit 0，见 `project-ui02-fixed.xml` / `.log`；包含新增的实际生成器回归。`tests/scheduler_real_checks.py` 在当前 `.02` 修复上为 exit 0、6 项检查通过，设计文件哈希集合明确含 5 个文件。新增 GPU 外观队列失败单独报告，不因自动测试全绿而隐藏。

标准 runner 总是附加 `tests` 目录，文件参数不能缩窄它；本次后续均使用 `-k`。审计曾中止误扩大运行，仅结束能以唯一命令行确认属于本次审计的进程，保留 `audit-render/aborted-test-summary.json`，不得将被中止的运行称为全套通过。没有执行完整大计算套件。

在当前 worktree 使用明确的既有解释器，以下命令可复现已提交的定向检查：

```powershell
$py = 'C:/Users/youai/Documents/coating-dev/coating-sim/.venv/Scripts/python.exe'
& $py example/verify_example.py
& $py tests/scheduler_real_checks.py
& $py tools/run_tests.py all -- -k 'test_example_project or test_project_deposition_bridge or test_machine_process_preview or test_process_editor_navigation_qt or test_source_material_editor or test_machine_process_reload'
```

一次性完整生产链路、数值、GPU 和取消协议脚本与其输入、输出、worker 日志、exit JSON 均保存在各自 artifact 目录；不是 mock。普通沙箱中的 Git、fixture 原子 hard-link 保存和测试缓存创建曾被 WinError 5 拒绝；同一有界操作经显式提权后成功，没有关闭安全设置或切换路线绕过拒绝。

## 真实桌面阻塞与未测试项

目标为 coating-sim 原生窗口的启动、导航、动画、点击、参数编辑和设备选择。当前 executor 未暴露 `node_repl/@oai/sky`；另已实际调用支持的 `cua.getState()`，返回 `apps=[]`，仅两个空浏览器/Apps 面板，且原生 API 被禁用。其他 `capture_screen_context` 入口明确仅支持活动语音、且不支持其他应用，因此未调用。证据为 `.01/desktop-tool-inventory.json`。

该动作状态为**工具不可用，未执行**，不是应用已启动后失败，也不是自动审批拒绝。没有使用自制 Windows 自动化、系统设置变更或其他进程绕过。离屏 QML 的布局、QTest 点击、参数/导航检查和 PNG/图像提供器像素分别有证据，但不替代真正桌面验收。

仍未验收：真实桌面视觉/动画/点击体验；沉积 GPU 成功路径；缺少隔离运行时的 GPU 外观队列成功路径；高采样外观色准、工程收敛及设备绝对标定。所有结束的 scheduler 清理仅针对本任务新建项目/root 的已验证进程；用户原有窗口任务未触碰。
