# 自动膜系合成：独立算法与边界审查（v2）

日期：2026-10-20。审查模型：gpt-6.1-sol。基线 HEAD：0c1c3e0；审查对象为该基线上本轮新增的现场文件，未读取旧分支来替换实现。本报告来自直接阅读源代码、运行测试和独立数值复算；不等同于 Seed Pro 阶段审查。

## 当前结论与范围

**v1 无阻断结论已撤回。v2 确认层 ID 冲突修复通过，但双窗口 1% 性能目标尚未完成。** v2 核心采用运行级 ID 保留集合；独立完整回归 99 项通过，原真实七层种子按原 2500 次预算续跑到第 17 周期正常 evaluation_limit 结束，未再出现 ID 或数值失败。两窗最坏 R 仍为 1.5656%/1.6957%，不能把“故障修复通过”当作“性能达标”。有限搜索和审计不构成全局最优、物理不可行或连续光谱认证。

只读检查了正式算法与测试；本审查未修改生产实现或测试，也未调整测试阈值。测试命令：

```text
.venv\Scripts\python.exe -m pytest -q tests/test_film_auto_synthesis.py tests/test_film_auto_synthesis_review.py tests/test_film_synthesis_growth.py tests/test_film_synthesis_audit.py tests/test_film_physical_structures.py tests/test_film_synthesis_cli.py tests/test_film_design_mcp.py
99 passed in 21.74s
```

原始零层、单层 AR、R≥0.9 多层反射器、真实 n/k 多角度 s/p/unpolarized 针层导数、57 层高 Q 腔、rugate 收敛等物理验收目标未放宽；反射器原 8 周期/1800 次评价/R≥0.9 条件保留。测试适配了新审计接口：errors 数量由固定 17 改为 ≥33 且单独断言 dense_points=17；多角度 holdout 仅抽取原 17 点固定密网与直接正演核对，2e−13 容差不变；注入失败/取消改 patch 新 audit helper，仍禁止 success。between-node 案例先对原裸候选独审并保留 max>.15，再要求继续探索后的返回候选仍高于用户容差且不差于该初始独审、cycles>0、not_found、dense_points=5。这不是降低 R/T 输入门槛，而是允许修复后的搜索返回实质改善的已审计结构。README 中的 algorithms.html 入口仍在。

## 直接发现及修复复测

1. **已修复：训练网格通过而独审失败导致提前停机。** 构造 ns=1、n=2.2 单层、d=550.5×550/(2×2.2)=68812.5 nm，目标为 549–551 nm、3 个训练点、T≤0.6。旧实现在种子训练通过、独审检出点间 T≈1 后以 cycle=0 退出，终止原因为 training_targets_met。冻结实现改由训练和独审共同通过决定停机，并审计所有训练可行候选；同一实际反例现在 cycle=1、逻辑 evaluations=9，训练及独审最大违规均为 0，状态 completed。独立审计档案与训练档案分开维护。

2. **已修复本例：换材需要同步改变厚度。** λ=550 nm、ns=1.5，原材料 n=1.3，d=11λ/(4n)=1163.4615384615 nm，R=0.0035475280314。仅换成 n=√1.5 并保留物理厚度会使 R 升为 0.0286662413297；保持参考光学厚度后 d=1234.9510786532 nm，R=5.9028618272×10⁻³²。冻结实现会尝试保物理厚度和保参考光学厚度两个提案，此例在 cycle=0 通过完整独审。注意：这仍是有限邻域启发式，不是所有换材厚度盆地的穷尽搜索。

3. **已修复：结果来源有效域单位。** 原 `film_synthesis_result_to_payload` 将原表 wavelength_min/max 直接标为 nm。当前分别导出 n/k 有效域和原始单位、来源信息；我将真实合成测试的三种源表全部改为 um，完成合成后 n/k 域均为 [450, 700] nm，转换正确。分离 n 与独立 k_data 的单位另有集成回归。

4. **已修复：公共 audit helper 不能继承兼容 k=0。** 底层兼容路径在 k 源域外可能回传 k=0 且 k_known=True。审计现在对实际 design 的层、基片、材料型入射/出射介质，在每个 dense、offset、extremum 实际波长上用 source 策略检查，不依赖调用者已通过 synthesis freeze。我另构造 candidate_materials 仅含 low、snapshot 额外含 high、实际 design 使用 high 的场景：high 的 k 域只有 540–560 nm，查询 520 nm 被明确拒绝，错误包含材料/集合/波长。该例验证检查覆盖实际材料而非仅候选池。

## 独立物理复算

另行编写普通 NumPy 逐层 2×2 特征矩阵，没有调用项目 coherent_rta 生成参考值。对每层 δ=2πnd/λ，按入射介质至基片顺序相乘；由出射边界 B、C 得 r=(n0 B−C)/(n0 B+C)，t=2n0/(n0 B+C)。本独立核只用于无损、正入射数值对照。

- 五对 Bragg：nH=2.2、nL=1.45、λ0=1000 nm、ns=1.5。中心 R=0.9595879822772295；850/1000/1150 nm 与正式正演 R 最大差 1.8874×10⁻¹⁵，R+T 闭合误差不超过 4.5×10⁻¹⁶。
- 四对端镜单半波腔：以腔内侧镜的复振幅 rm 进行 Airy 合成，T=(1−|rm|²)²/|1−rm² exp(2iδc)|²。970/985/1000/1015/1030 nm 的 T 分别为 0.183595186445、0.470123682904、1、0.484888657384、0.201196464622。Airy 与完整层矩阵最大差 9.44×10⁻¹⁶，与正式正演最大差 4.50×10⁻¹⁵。这支持层序和相干组合正确，不能推广为真实材料制造验证。
- 单腔/耦合腔、多堆栈、匹配层均展开为实际层序；不是各段功率谱相乘。
- 数字 rugate 导出真实 H/L 薄层，以参考波长处的实介电常数填充分数逼近目标；不生成虚构混合 n/k。零调幅、分片加密收敛、零 chirp 退化、独立比例控制已有真实正演回归。深亚波长、法向、弱吸收近似已明确。

## 边界与状态检查

- 独立探测逻辑预算 2、3、4、5：实际 evaluations 分别为 2、3、4、5，均保留完整已审计候选；预算 8 的一周期试验在 evaluations=6 以 cycle_limit 结束。没有越过显式逻辑预算；审计内部物理评估/波长点另计。
- 审计 progress 阶段设置取消：返回 cancelled、targets_met=false、validation_complete=false，不把部分审计提交为通过。已有快照取消回归验证已完成档案按值复制且保留。
- 缺失 k、负 k 均明确拒绝；710–720 nm 查询超出 450–700 nm 源域时明确拒绝外推。缺失 k 错误包含材料、光学集合、查询波长与目标区间。
- 2502 个训练点、默认四倍独审时实际检查 10005 个加密点，未受旧 helper 单块点数限制。
- 最坏标准化违规先于固定网格加权 RMS；通过所有目标后才按层数、总物理厚度排序。权重不能掩盖单项违规。偏移/局部极值样本不改变 RMS 的固定测度。
- 只承诺 R/T。phase、GD、GDD 明确 unsupported。有限采样与局部极值搜索始终 continuous_certificate=false；不能排除未括住的更窄谱特征。

## 已知搜索限制

局部厚度方向来自平滑加权平方损失，接受与档案排序使用真正的最坏违规优先。两者不等价，不能据此声称全局最优或不可行证明。删除是逐层直接删除并重新评分；材料替换是上述两个有限提案，只有提案改善才继续局部精修。相位限步是采样传播相位上限，不是高 Q 功率信赖域。停滞不构成物理不可行证据。取消为物理 helper 调用之间的协作边界。

## v1 冻结身份（已撤回）

- `coat_sim/core/film_auto_synthesis.py`: `c02dcde25bc47f6ac24ca1d09275820ec7c6b4ccf4b0a0b6aecafd4542218175`
- `coat_sim/core/film_synthesis_audit.py`: `b92766e6af17e642f67814b4cfd36ee706c62235222e486675d75aa99d7bf9d6`
- `coat_sim/core/film_physical_structures.py`: `d506da914d5096122709fdb7a5e93005bd70133cc2c60dbdab182f2df261d1f8`

## v1 接口与进程复核（历史记录）

IO/CLI 实现者通知冻结后，独立重跑上述六组测试得到 **83 passed in 14.14s**。重构期间曾出现 CLI 返回 tuple 解包错误，已由实现者修正；最终完整套件和此前两项 CLI 单独复跑均通过，此处为 v1 当时记录，后续 ID 阻断及 v2 结论见下文。

直接检查了 assistant 与 CLI 共用 `prepare_film_synthesis_payload`：物理结构展开成实际种子后，再次严格冻结检查材料域和厚度约束，离线重放不访问 live catalog。CLI `--plan` 与 assistant `plan_film_synthesis` 产生相同请求；新增源域外种子拒绝回归通过。结果区分不相交窗口，不把外包络当连续覆盖；n/k 原始单位与独立来源分别保留。

真实子进程回归覆盖：成功结果与已审计 checkpoint、输出已存在拒绝覆盖、路径别名保护、checkpoint 请求身份不匹配拒绝、非有限 heartbeat 参数拒绝、心跳、取消文件、exit 130、进程 communicate 收尾。采用非 daemon 工作线程和 finally 中协作取消/join；不承诺强制中断已经开始的物理 helper 调用。

接口冻结文件：

- `coat_sim/core/film_synthesis_io.py`: `aca297b892fe09673ea8551c7331b0bcfe8f61dba982841d303a4c01fe4a71e4`
- `coat_sim/film_synthesis_cli.py`: `2a822add92e8679c92cbac909c760a7069f1725db4ca692ddf8650066b4ffb88`
- `coat_sim/core/mcp_contracts.py`: `07b327e9a4f19c0e1ebd15fcce6a3a807d03e5dbda7e77870c66ba3c5835164a`
- `coat_sim/mcp/server.py`: `9b5d98a3134869f587e9e7eca7000ff97f95cff6db96555177c56b97e90a99e5`
- `coat_sim/agent/runtime.py`: `7e26bcbd33428192492ad3829079549b58d5e8c63d20c7f89f953752b632bc43`

## v2 补充审查：种子续跑层 ID 碰撞修复

原始证据：`artifacts/film_synthesis_20261020/acceptance/dual_ar_seeded_frozenhelper/`。`run.json` 记录旧核心哈希 c02dcde25bc47f6ac24ca1d09275820ec7c6b4ccf4b0a0b6aecafd4542218175；输入保存了真实七层种子，逻辑预算 2500，max_layers=64，不限制周期。第 10 周期、1304 次评价报层 ID 不唯一，独审最佳保留 8 层，最大标准化违规 0.0069567254839；两窗口 R≈1.566%/1.696%，未达到 1% 目标。错误不能解释为物理不可行。

### v2 ID 实现与独立续跑结果

最终核心 SHA256：`2c27b43bb1ad2bfd5145ce5237694b52291fbb9ae3c7f51e23f3f70c7f62c71d`（早期 65fa…候选已被本运行级策略取代）。直接阅读确认 `reserved_layer_ids` 由一次 `synthesize_film_design` 调用唯一拥有，初始包含 environment 与全部 seed 的 front/back ID。`_proposal_layer_ids` 同时保留实际设计全部 ID 与运行级集合，并登记每次新分配；删层、候选丢弃和档案更新不会释放旧 ID，因此覆盖其他分支、已审档案和先前提案。相同概念提案的 zero/finite 变体复用同一组 ID；保留的真实层 ID 不重编号。冲突稳定添加 -1/-2 等后缀，128 字符边界先截断根名再追加后缀；needle 与 split 相互排除，growth 的 `grown_protected_id` 使用实际分配 ID。

独立性质检查覆盖 128 字符根名、已有 -1/-2 后缀及重复 preferred ID，输出全部唯一、长度不超过 128，重放一致。新增真实回归覆盖跨 seed 和丢弃提案保留、针层/内部拆分/growth 的实际物理评价、真实 `_execute` 已审 checkpoint 写入读取恢复、删除后重新评分及增长、冻结请求确定性重放。

直接读取原保存请求，不重建近似几何，也不修改目标或 2500 次逻辑评价预算。输入文件 SHA256：`93ad329e481efb9dd313964e0e78203d3c7033b446b5aa52599df7b0723df8c5`。最终核心独立运行耗时 18.56 秒，17 周期、2499 次评价，以 `evaluation_limit` 正常 `not_found` 结束；`failed_evaluations=0`、`validation_complete=true`。保留 8 层，全部 ID 唯一；审计 71 次物理评价、3907 波长条件点。原第 10 周期 ID 故障已解除。

两窗独审最坏 R 分别为 0.015656086544720737（3561.935583636 nm）、0.016956725483918283（8000 nm），目标仍为 R≤0.01，最大违规 0.006956725483918283，**尚未达到双窗口 1% 性能目标**。有限预算结束不能解释为物理不可行。

### v2 最终完整回归与取消时序

最终冻结后独立运行同样六组算法/CLI 测试，并追加当前基线 MCP 的 11 项，得到 **99 passed in 21.74s**。原 83 项保留，新增 5 项 ID/续跑回归及上述 11 项 MCP 回归。生产 CLI 没有为修复时序测试而改变行为。

此前一次 v2 检查为 85 passed、1 failed，唯一失败是固定启动 80 ms 后创建取消文件，进程正确 exit 130、但快速退出前没有 heartbeat。父线程只调整测试同步：使用明确互相矛盾的 R 目标和无隐式预算，实际观察到 heartbeat 且已审 checkpoint 落盘后才请求取消；保留有界等待、finally 中取消/communicate、超时终止收尾。最终仍要求 exit 130、validation_complete、targets_met=false，返回候选与已审 checkpoint 相等。这是消除启动竞态，未放宽心跳、取消或审计要求。

旧 MCP 测试不再依赖已删除 examples 文件，而以当前 new_design 与临时根中的当前内置 N-BK7 生成夹具；保存/加载、覆盖保护、来源与能量断言保留。该测试修复不读取或恢复旧版本实现。

### v2 最终文件身份

- `coat_sim/core/film_auto_synthesis.py`: `2c27b43bb1ad2bfd5145ce5237694b52291fbb9ae3c7f51e23f3f70c7f62c71d`
- `coat_sim/core/film_synthesis_audit.py`: `b92766e6af17e642f67814b4cfd36ee706c62235222e486675d75aa99d7bf9d6`
- `coat_sim/core/film_physical_structures.py`: `d506da914d5096122709fdb7a5e93005bd70133cc2c60dbdab182f2df261d1f8`
- `coat_sim/core/film_synthesis_io.py`: `aca297b892fe09673ea8551c7331b0bcfe8f61dba982841d303a4c01fe4a71e4`
- `coat_sim/film_synthesis_cli.py`: `2a822add92e8679c92cbac909c760a7069f1725db4ca692ddf8650066b4ffb88`
- `coat_sim/core/mcp_contracts.py`: `07b327e9a4f19c0e1ebd15fcce6a3a807d03e5dbda7e77870c66ba3c5835164a`
- `coat_sim/mcp/server.py`: `9b5d98a3134869f587e9e7eca7000ff97f95cff6db96555177c56b97e90a99e5`
- `coat_sim/agent/runtime.py`: `7e26bcbd33428192492ad3829079549b58d5e8c63d20c7f89f953752b632bc43`
- `tests/test_film_synthesis_growth.py`: `e767cd9f22165d75f35940fc2305f27ef20d9fd897b2cb32b579239a401e6cc9`
- `tests/test_film_synthesis_cli.py`: `13e6f3d81ded20df57d0305489715b67f3d868123233e82b590ed90015f0b8b8`
- `tests/test_film_design_mcp.py`: `292d0de707be237004bd42fa94d17861140dbfd5fe906377fd6a292ec50281b5`


## v3 independent execution-boundary review (2026-10-20)

Reviewer model: **gpt-6.1-sol**. Baseline HEAD remains `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`; this review used the current working-tree implementation. The preceding v1/v2 sections are historical. The external v2 acceptance report at `C:/Users/youai/Documents/Codex/2026-10-20/task-3/evidence_v2/report.json` concluded `changes_required`, including inactive-port lookup, static diagnostics, and the missing assistant execution path. This appendix does not turn that report into an acceptance or claim a Seed review.

**Scoped outcome: no remaining blocking finding in the execution boundaries examined below after the fixes and reruns.** This is a code and regression review of the new execution chain, not complete product acceptance, a GUI integration test, or the complete supported-environment gate. No production file was edited by this reviewer. Under explicit authorization, this reviewer added only `tests/test_film_synthesis_execution_review.py` and this report appendix.

### Scope and actual verification

Read the assistant runtime discussion/execution guard, host project binding, submit/estimate schema and idempotency, typed frozen scheduler specification, worker dispatch, heartbeat publishing, cancel-wins transition, result capability, audited checkpoint identity/replay, and physical provenance validation. Execution follows the existing business `CoatService.root` workspace. `execution_project_root` identifies that workspace in receipts/results; it does not claim routing to the UI-selected `.coatproj` root. Model-supplied project root overrides remain outside the public submit schema. Project memory binding guards identity and authority rather than implementing dynamic execution routing.

The reviewer independently ran:

```text
.venv\Scripts\python.exe -m pytest -q tests/test_film_synthesis_execution_review.py
15 passed, 28 warnings in 7.54s

.venv\Scripts\python.exe -m pytest -q tests/test_film_synthesis_audit.py
36 passed in 1.81s

ruff check --config ruff.toml --no-cache tests/test_film_synthesis_execution_review.py
All checks passed!
```

Ruff used the already available independent analysis binary; no dependency was installed. The 28 warnings are the existing `PurePath.is_reserved()` deprecation in `agent/toolkit/policy.py`, not test failures. These are two separately executed test runs, not a claim that the full repository suite or all static checks passed. Syntax compilation of the new independent test also passed. Longer real scheduler-worker completion/cancellation tests belong to the implementation owner's evidence, and are not counted as reviewer-executed here.

The 15 independent cases exercise actual discussion-mode dispatch (including attempted forged approval/mode), two real business roots with repeated input/client identities, nonzero revision preservation, full real layer tables after cancelled checkpoint recovery, incident-medium alteration, a 0.1 um/50 nm constraint bypass, missing/nonfinite audit scores, actual atomic JSON persistence, scheduler spec/input-digest/checkpoint identity binding, legitimate generated-seed identity/name/note differences, and physical provenance at canonical, worker and remote-admission boundaries. The last case computes a real completed single-layer AR candidate and invokes the actual scheduler cancellation transition; no numerical evaluator is mocked. No existing test or numerical target was relaxed.

The 36 audit cases cover the active/inactive material-port matrix and the high-Q audit checks. The current audit uses `active_material_references` consistently with the supported single-sided, front-incidence forward path. Inactive exit/back draft sources are not sampled; missing active sources are rejected with material/set/target context. Double-sided and back-incidence synthesis remain explicitly unsupported.

### Counterexamples observed and fixes verified

- Checkpoint replay originally accepted altered physical environment, a unit-based thickness-constraint bypass, or missing score evidence. Current replay enforces the same physical environment keys as synthesis, validates feasibility, and recomputes training and independent audit scores. It preserves permitted generated-design identity/name/note/expression changes and nonzero design revision.
- A real audited score contained dataclass error records that a normal scheduler JSON writer could not serialize. The actual atomic writer/reader regression now passes; recovered output retains the real layer table.
- Canonical physical provenance validation alone left a downstream bypass: after building a legitimate physical seed job, changing its references to a forged DOI or its approximations to a manufacturing-certification claim was still accepted by direct typed worker execution and remote admission. Both reviewer regressions initially failed with `DID NOT RAISE`. Current typed deserialization also regenerates the library structure from frozen spec/environment/materials, compares metadata, and requires the generated candidate in the seed set. The same legitimate metadata still replays successfully.
- A cancellation accepted at the scheduler completion boundary initially produced queue/result state `cancelled` while `result.targets_met=true`, `review.targets_met=true`, and `review.termination={state:completed, reason:validated_targets_met}`. This was reproduced using an actually completed, audited one-layer AR result. The added independent regression initially failed on the success flag. Current cancellation updates the result/review terminal state and both top-level target flags consistently while retaining the audited candidate, scores and layer table, with an explanatory warning. The regression now passes.

### Limits that remain explicit

The dedicated submit now follows the existing durable estimate/confirmation/scheduler path; ordinary assistant-turn completion is not a worker cancellation authority. Explicit cancellation follows the scheduler boundary and retains only replay-verified audited checkpoints. Heartbeat publication and algorithm progress are distinct signals; no promise of interrupting an already running optical helper is made here.

This review does not certify phase/GD/GDD, metal/dielectric synthesis qualification, manufacturing, a continuous spectral domain, or general difficult-target attainment. The historical original seven-layer continuation at 2500 evaluations remains `not_found`, 17 cycles/2499 evaluations, with worst window reflectances about **1.5656% and 1.6957%**, so the two 1% targets remain unmet. That long search was not rerun for this execution-boundary appendix. A finite search failure is not physical infeasibility. Full environment/static-gate status and a newly designated independent overall acceptance must be reported separately by their actual owners.

### v3 reviewed file SHA256

| File | SHA256 |
| --- | --- |
| `coat_sim/core/film_auto_synthesis.py` | `95db8246d5d19d8f82ac855d9174a35a7acc2ed1d3fec0af315aec7f345b00ab` |
| `coat_sim/core/film_synthesis_audit.py` | `93d47ef02830f49e87f178cf9ed806cdbef7b18dd208eb1a495a3eb21ef6cc59` |
| `coat_sim/core/film_physical_structures.py` | `e9ce902b700bfb942f8c74f7a55b328e2fd2f59749f9da96579f04d4caf36a84` |
| `coat_sim/core/film_synthesis_io.py` | `8e61fa9c845a5745b27b807dab5c0e2d2e82aa276eda895a1c4ebd7756e1a07d` |
| `coat_sim/core/film_synthesis_jobs.py` | `223be64daf2b31e65533160cb2675b8ac569ef07e5394b34d528b0c31da6e14a` |
| `coat_sim/core/compute_estimation.py` | `eb74cc6cb6e981980d02e722203f220a83a2cd539fe9d0ec1d2a0e0003fb2637` |
| `coat_sim/core/mcp_contracts.py` | `39dff07b49d66b29b4ff6f96e48f296b45ac3d24ac624b364d5f972d8f059103` |
| `coat_sim/mcp/server.py` | `0704420556e01badc27ba3419bb313bf71f8a60a3f4ca89b7272bbc5730e03e8` |
| `coat_sim/agent/runtime.py` | `cee4dd1ae0180b05f33fe1cdb927983b6aaac22f4487d58ace091037b4feceb3` |
| `coat_sim/agent/tool_dispatch.py` | `b145b0bb40980b29b9549aed34b8fc96bfe96e8707d85b095c505a4747a6b5a9` |
| `coat_sim/scheduler/jobs.py` | `ccf827d11d8f5b5bedf163f555800bd3d83ac6f58e24e575775df3d38b296c27` |
| `coat_sim/scheduler/submission.py` | `8ac83565d3a6622bfcd767c786a057e92bf0b0be3e64f6109d2cfeea8bb7f6cc` |
| `coat_sim/scheduler/worker.py` | `d0150679c17017d4dc5ac076e8704eb397800fd967ab5a8b72d0b44381a13b13` |
| `coat_sim/scheduler/daemon.py` | `daa253559a28a562aa5db0d9ec09f767971e0f753207af25d3a8bd128a532dee` |
| `coat_sim/scheduler/remote_contract.py` | `dc2f73b1fd1d011037c24424f96bfb4856a3904f5a65299f202aebd96b38057e` |
| `coat_sim/scheduler/result_capability.py` | `81e8d9a612f9813f5f9e1c3db2e05a7d4e517f4a1c7765dca89a3a9e876c1e1f` |
| `coat_sim/film_synthesis_cli.py` | `9c90ad5a13b68b93eab7e994f14bf7e6cb8650f044b99d1f1c2122a81d0c26a8` |
| `tests/test_film_synthesis_execution_review.py` | `58d4242a7995d9e8e08d2c4d1e522d87c0f37de0df0f50634f559a17984500fe` |


## v3 follow-up: cancelled task versus audited candidate facts

This follow-up was requested **after** the preceding 15-test review. That earlier passing run did not establish the new `best_candidate_audit` semantics. The reviewer again used gpt-6.1-sol, changed only the independently owned test file and this report, and read the production changes without editing them.

Task completion and candidate physics are now reported separately. A cancelled task has `result.targets_met=false` and matching cancelled review termination/task flags. These task flags do not say that an already audited candidate failed optically. The shared formatter's top-level `best_candidate_audit` states whether complete audit evidence is available and, when it is, whether **both** training and audit maximum normalized errors are at or below `acceptable_normalized_error`. It carries those two measured errors and the threshold. With no complete audit its candidate `targets_met` is `null`, meaning unknown, rather than a physical failure. The sampled audit remains `continuous_certificate=false`.

Three independent real-computation scenarios now explicitly check this distinction:

1. A cancel event set before core execution yields a cancelled task without a candidate or completed audit. Both the core formatter and actual `execute_job` return candidate audit unavailable/unknown. The executor output is a valid synthesis result, is not a verified best-candidate capability, and writes no audited checkpoint.
2. A real completed, qualified single-layer AR result loses the scheduler completion race to cancellation. The real scheduler transition reports task/review cancellation and false task completion flags, while retaining `best_candidate_audit.targets_met=true`, the exact original training/audit scores, all per-objective physical facts and the complete layer table.
3. The actual core snapshot callback sets cancellation only after receiving a newly retained candidate whose training and independent audit errors are both within threshold. The core returns task `cancelled`, candidate audit available/true, unchanged scores and per-objective facts, and the exact positive-thickness one-layer prescription. The request environment remains unchanged and the candidate is returned separately; this does not automatically apply the candidate to a user design.

The first two additions and the strengthened existing race test bring the independent execution file from 15 to **17 cases**. All earlier assertions remain. The final reviewer-executed combined run and lint were:

```text
.venv\Scripts\python.exe -m pytest -q tests/test_film_synthesis_execution_review.py tests/test_film_synthesis_audit.py
53 passed, 28 warnings in 7.49s

ruff check --config ruff.toml --no-cache tests/test_film_synthesis_execution_review.py
All checks passed!
```

The 53 cases are the 17 execution cases plus 36 audit cases. The same existing `PurePath.is_reserved()` deprecation accounts for the warnings. This new run supersedes the earlier local execution-test count for cancellation semantics. The implementation owner's broader 36-module regression and full static/environment checks are separate evidence and are not claimed here.

**Follow-up scoped outcome: no remaining blocker found in these cancellation/task/candidate boundaries.** All reviewer-owned tests are now frozen. The v2 changes-required history, unproven difficult-target performance, unsupported metrics and finite-audit limitations above remain explicit. In particular, a cancelled task can retain a physically qualified candidate; cancellation itself does not negate those physical measurements or justify an automatic application.

The following hashes were recomputed after this follow-up. They supersede the earlier v3 rows for any file changed by the new requirement:

| File | SHA256 |
| --- | --- |
| `coat_sim/core/film_auto_synthesis.py` | `95db8246d5d19d8f82ac855d9174a35a7acc2ed1d3fec0af315aec7f345b00ab` |
| `coat_sim/core/film_synthesis_audit.py` | `93d47ef02830f49e87f178cf9ed806cdbef7b18dd208eb1a495a3eb21ef6cc59` |
| `coat_sim/core/film_physical_structures.py` | `e9ce902b700bfb942f8c74f7a55b328e2fd2f59749f9da96579f04d4caf36a84` |
| `coat_sim/core/film_synthesis_io.py` | `169fb063d464bccac01916890e3be57432a92347bf959e854373221119a47bb4` |
| `coat_sim/core/film_synthesis_jobs.py` | `4ced06106d63ee812050c15e9016ad91c98ae464cf52cfed3629323ca14abbfa` |
| `coat_sim/core/compute_estimation.py` | `eb74cc6cb6e981980d02e722203f220a83a2cd539fe9d0ec1d2a0e0003fb2637` |
| `coat_sim/core/mcp_contracts.py` | `39dff07b49d66b29b4ff6f96e48f296b45ac3d24ac624b364d5f972d8f059103` |
| `coat_sim/mcp/server.py` | `0704420556e01badc27ba3419bb313bf71f8a60a3f4ca89b7272bbc5730e03e8` |
| `coat_sim/agent/runtime.py` | `cee4dd1ae0180b05f33fe1cdb927983b6aaac22f4487d58ace091037b4feceb3` |
| `coat_sim/agent/tool_dispatch.py` | `b145b0bb40980b29b9549aed34b8fc96bfe96e8707d85b095c505a4747a6b5a9` |
| `coat_sim/scheduler/jobs.py` | `ccf827d11d8f5b5bedf163f555800bd3d83ac6f58e24e575775df3d38b296c27` |
| `coat_sim/scheduler/submission.py` | `8ac83565d3a6622bfcd767c786a057e92bf0b0be3e64f6109d2cfeea8bb7f6cc` |
| `coat_sim/scheduler/worker.py` | `d0150679c17017d4dc5ac076e8704eb397800fd967ab5a8b72d0b44381a13b13` |
| `coat_sim/scheduler/daemon.py` | `376c77e39b41886c2996f341be302d9c909324c1416fcb2be2adb2d26fb26f53` |
| `coat_sim/scheduler/remote_contract.py` | `dc2f73b1fd1d011037c24424f96bfb4856a3904f5a65299f202aebd96b38057e` |
| `coat_sim/scheduler/result_capability.py` | `81e8d9a612f9813f5f9e1c3db2e05a7d4e517f4a1c7765dca89a3a9e876c1e1f` |
| `coat_sim/film_synthesis_cli.py` | `9c90ad5a13b68b93eab7e994f14bf7e6cb8650f044b99d1f1c2122a81d0c26a8` |
| `tests/test_film_synthesis_execution_review.py` | `757d87428e7d935617f747b7f80071a5494e98f77038c65e7aeb080571487f82` |
