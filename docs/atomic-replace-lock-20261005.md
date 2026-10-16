# 计算任务失败：原子替换的 Windows 文件锁竞态

日期：2026-10-05。范围：`coat_sim` 内所有「临时文件 → `os.replace`」的原子发布点。
本修复只改变文件发布的鲁棒性，不改变任何沉积、几何、采样或统计算法，因此不涉及
`docs/algorithms.html`。冻结基线仍为 `cub`（2026-10-05）；本文件描述该基线之后的工作树修改。

## 1. 结论

计算任务在**数值计算完成之后**失败，报：

```
[WinError 5] 拒绝访问。: '…/status.tmp' -> '…/status.json'
[service.simulation_error · <error_id>]
```

根因是 `os.replace`（Windows `MoveFileEx` + `REPLACE_EXISTING`）与**外部文件监视/索引进程**
的瞬态句柄竞争：新写出的文件刚落地，索引器或工作区监视器立即打开它，紧随其后的重命名
就返回 `ERROR_ACCESS_DENIED`（WinError 5）。这是**概率性**失败，单次写入命中率约 2–3%；
任务在计算期间会多次写进度文件，写入次数越多，累计命中概率越接近 100%，因此大网格
（约 ≥ 281）必然失败，小网格通常侥幸通过。

**不是** DirectML 原生崩溃。本轮 DirectML 在网格 901 仍正常完成。

## 2. 复现

命令（项目 Python）：

```
.conda\python.exe -m coat_sim.algorithm_cli --process examples/process_example.json \
    --algorithm fast --grid-size <N> --time-steps 600 --backend <CPU|DirectML>
```

修复前结果（`process_example.json`，3 层）：

| 网格 | 后端 | 结果 |
|---|---|---|
| 101（4000 步） | CPU | 通过（13.6 s） |
| 181 | DirectML | 通过 |
| 251 / 261 | CPU | 通过（261 连跑 3 次） |
| **281 / 301 / 401 / 501** | CPU / DirectML | **100% 失败** |
| 61（`--algorithm reference`，3 次评估） | CPU | 失败 |

与后端无关；阈值出现在网格 261 与 281 之间。

## 3. 根因证据

### 3.1 隔离实验：同一写循环的失败率取决于目录

```
写 status.tmp → os.replace(status.json)，循环 12–15 s：
```

| 目录 | 写入次数 | 失败次数 | 失败率 |
|---|---|---|---|
| `<项目>/artifacts/` | 1158 | 31 | 2.68% |
| `<项目>/artifacts/`（复测） | 928 | 27 | 2.91% |
| `<项目>/data/` | 919 | 19 | 2.07% |
| `<项目>/`（根） | 885 | 17 | 1.92% |
| `C:/a/wk/`（工作区外、同盘） | 1007 | 10 | 0.99% |
| 系统 `%TEMP%` | 1176 | 0 | **0.00%** |

失败全部为 `PermissionError(13, '拒绝访问。')`。系统 `%TEMP%` 为 0 是关键对照：Windows
搜索索引器默认不索引 `%TEMP%`，而工作区被索引/监视，因此新写文件会被瞬态占用。

### 3.2 现场进程

`tasklist` 观察到 `SearchIndexer.exe`（Windows 搜索索引）、`OneDrive.Sync.Service.exe`
与 `WorkBuddyAI.exe`（IDE 自身的工作区文件监视）；未发现 `MsMpEng.exe`（Defender 实时防护
不在运行）。三者都能在文件写入后立即打开它。

### 3.3 放大机制

- `algorithm_cli.py` 的 progress 回调**每批写一次** `status.json`。
- 每批覆盖的基片点数 `count ≈ π/4·n²`，批大小 `batch = min(32, 220000 // count)`，
  于是写入次数 = `time_steps / batch`：网格 251/261 → 约 150 次；网格 ≥ 281 → 约 200 次。
- 异常从父进程读取循环（`isolated_compute._run` 的 `read_one`）冒出，`finally` 直接
  `terminate()` 子进程，任务以 `simulation_error` 结束（CLI 退出码 2）。

## 4. 影响面

- **CLI 路径**（`python -m coat_sim.algorithm_cli`）受影响，且在大网格下必然失败。
- **GUI 计算队列不受影响**：进度写入 `artifacts/runtime/compute-tasks.sqlite3`（SQLite，
  `timeout=15` 自带重试），不写 JSON 文件；`input_snapshot.py` 也不写文件。
- 全项目另有约 20 处一次性 `os.replace`（保存设计、导出资产、导出 PDF、设置、材料库、
  MCP 服务等）属同一缺陷类，调用频次低、命中概率小，但同样可能在保存/导出时偶发「拒绝访问」。

## 5. 修复

新增 `coat_sim/core/atomic_io.py`，提供 `replace_with_retry(source, destination)`：

- 对 `os.replace` 做**有界重试**（默认 20 次、间隔 10 ms，覆盖约 200 ms 的瞬态锁窗口）；
- 仅重试瞬态锁：`PermissionError`，或 `OSError.winerror ∈ {5, 32, 33}`
  （`ERROR_ACCESS_DENIED` / `SHARING_VIOLATION` / `LOCK_VIOLATION`）；
- 其它错误立即抛出；重试耗尽后仍抛出真实权限错误——**不掩盖真问题**。

替换全部 **23 处**调用点（19 个文件）：

`core/atomic_io.py`(新)、`algorithm_cli.py`、`core/api.py`、`core/data_files.py`、
`core/demo_workflow.py`、`core/design_visualization.py`、`core/mask_cad.py`、
`core/materials.py`、`core/result_pdf.py`、`core/result_visualization.py`(2)、
`core/simulation_assets.py`(2)、`agent/development.py`、`agent/environment_issues.py`、
`agent/tool_results.py`、`mcp/server.py`(2)、`ui/assistant_desktop.py`、
`ui/assistant_render.py`、`ui/control.py`、`ui/desktop_identity.py`、`ui/settings.py`(2)。

无残留 `os.replace` 直接调用（仅保留在新模块内部）。`core/result_pdf.py`、
`ui/assistant_desktop.py` 因不再使用 `os`，已移除其 `import os`。

## 6. 验证

### 6.1 直接验证（修复后）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 281 / CPU | 100% 失败 | 通过（15.6 s） |
| 301 / DirectML | 100% 失败 | 通过（24.5 s） |
| 501 / CPU | 100% 失败 | 通过（49.6 s） |
| reference / 61 | 失败 | `evaluation_limit`（8.8 s，正常终态） |

修复前用「给 `Path.replace` 加同款重试」的非侵入式 wrapper 也复现了同一结论：281/301/501
与 reference-61 全部完成，每次命中 0–4 次重试。

### 6.2 单元测试

新增 `tests/test_atomic_io.py`（6 项，全通过）：正常替换、瞬态 `PermissionError` 重试、
`SHARING_VIOLATION` 重试、重试耗尽后抛出、无关 `OSError` 不重试，以及一条产品级用例
——`write_data_file` 在前两次 `os.replace` 被拒后仍成功写入且不残留临时文件。

### 6.3 相关测试子集

`test_atomic_io` + `test_portable_asset_bundle` + `test_typed_design_extensions` +
`test_input_snapshot`：46 项，2 项失败，**均为既有环境问题**（见 §7），已用 `git stash`
在基线上逐项复核确认。

## 7. 已知局限与未决

- 本机 `unittest` 套件显著变慢（约 35 项 / 7 min），因为每次子进程导入都要经过索引器/
  监视器的文件扫描——与本次缺陷同一来源。完整 discovery 未在本机跑完；建议在安静环境复跑。
- 以下失败在**基线上完全相同**，与本次修复无关：
  - `test_compute_confirmation_policy.ComputePolicyTests` 8 项 ERROR：teardown 删除临时目录
    时 `state.sqlite3` 仍被占用（`WinError 32`）；栈中出现 IDE 注入的 `sitecustomize.py`
    shim（`_safe_remove`）。
  - `test_input_snapshot…test_restricts_outside_root_dependency_and_symlink` 与
    `test_portable_asset_bundle…test_export_rejects_symlink_parent`：符号链接语义在
    Windows 上的差异。
- 修复只降低概率，**不消除**外部进程持锁：重试窗口约 200 ms，若某进程长期独占该文件，
  仍会如实报错。若需根治，可在部署侧将项目目录排除出索引/同步范围。
- 未在 Windows/DirectML 实机上做性能或数值对照；本修复不改变任何数值结果。

## 8. 硬链接发布路径复核

`os.link` 用于「不覆盖」发布（`core/api.py:791`、`core/materials.py:432`、
`core/simulation_assets.py:106`）。**复核结论：不适用该竞态，未改动。**

理由与实测：

| 测试 | 结果 |
|---|---|
| 工作区连续 500 次 `os.link` | 500/500 成功，0 失败（1.6 s） |
| 源文件被另一句柄打开时 `os.link` | 3/3 成功 |
| 同一条件下 `os.replace`（目标被打开） | 立即 `PermissionError`（WinError 5） |

机制上，`os.replace` 必须替换已存在的目标目录项，目标被打开即失败；`os.link` 只是
新增一个目录项，不触碰既有条目，因此外部扫描器持有的句柄不影响它。此外 `os.link`
的 `FileExistsError`（目标已存在）是**刻意**的拒绝覆盖语义，加通用重试反而有掩盖风险，
故保持原样。

## 9. 已完成动作与后续

- 已在 `docs/CHANGELOG.md`「未发布 · 2026-10-05」段追加条目，记录本次修复、触发原因、
  `os.link` 复核结论与验证结果。
- 后续建议：在安静环境复跑一次完整 discovery；若某部署环境存在长期独占文件的进程，
  可将项目目录排除出索引/同步范围，而非继续加大重试窗口。
