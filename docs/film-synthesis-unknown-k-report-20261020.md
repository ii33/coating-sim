# 从零合成：未知 k 显式无损假设阶段报告

日期：2026-10-20。当前工作目录 `C:\Users\youai\Documents\coating-dev\coating-sim`，HEAD `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2` 未变。保留进入本阶段前的未提交工作及旧阶段证据，没有从旧副本或历史版本合并、覆盖，没有推送或发布。此前 GUI 修复冻结清单 SHA256 `804cec329dbf08475f535061f9ce9a600e511aee4d14620a011ea703e89de6a1` 保持原文件不变；本阶段用新的 `unknown-k-policy-20261020/policy-freeze-manifest.json` 记录。

当前结论：未知k选项及其GUI边界已完成；DeepSeek R3实际答复为ready_for_seed、findings为空，Seed未配置且未执行。恢复连接后核对969源与218项+5子测试、22项原生、独立9项的最终SHA完全相同，本次没有重复模型调用或整套测试。静态扫描仍有此前40项基线外指纹，本轮新增0，整库门禁仍未通过。

可交互源码版快捷方式为`artifacts/film_synthesis_20261020/unknown-k-policy-20261020/CoatingSim-current-source.lnk`，读回核验其目标为现有`.venv/Scripts/pythonw.exe`，参数为当前同目录`launcher.py`，工作目录为当前源码根。快捷方式不依赖PowerShell，不改启动器、不安装依赖，也没有替用户启动窗口。既有`launcher.pyw`桌面包装需要的`.conda/pythonw.exe`当前不存在，故本次入口直接使用项目已有验收运行时。进入当前工程→膜系设计→优化→算法→从零自动合成，勾选“未知 k 按 0 计算（无损假设）”。这不是新安装包，未发布或更新已安装版本。

## 已完成的可用行为

从零合成界面增加默认未选中的“未知 k 按 0 计算（无损假设）”。默认严格拒绝缺失 k；只有明确勾选时，才把没有提供的 k 作为无量纲0。已知非零 k 保留，n 的正值/有限检查和 n、已知 k 的源波长域继续严格检查。原材料记录、原始 `k=None` 和来源标识不修改，不把假设值写回材料库。

唯一数值策略沿用请求 `environment.assume_lossless`，并保持 `use_k=True`；GUI 可编辑配置为 `synthesis.assume_unknown_k_zero`。该策略冻结到执行、显式/物理 seed、物理步长控制、TMM 计算、每个新增审计点、候选、检查点和 `.cfilm` 保存。结果读取冻结结果的假设说明，区分请求候选池中受影响的引用与最终实际光路引用；可读设计注记写明未知值假设、实际材料/数据集及“此结果不是实测吸收验证”。原 note 保留，空间不足时用已有 summary，二者均满则明确拒绝而不截断。未增加 wire schema 版本或产品 pwsh 依赖。

核心辅助函数在 `coat_sim/core/film_synthesis_absorption.py`；现有 auto_synthesis、audit、IO、typed jobs 和 physical structures 接入同一策略。`generate_physical_structure(..., *, assume_unknown_k_zero=False)` 默认为严格且须与环境标志一致；仅自动合成派生 seed 显式传递本次请求策略，独立物理构造 GUI 沿用严格模式。已有透明模板、nH>nL 等物理约束保持。

同轮收口 DeepSeek 第一轮的两项有效问题：角度草稿必须在选择/新增/复制/删除目标前按原目标消费或保留错误；配置导出须拒绝已登记但尚未完成的非法选项草稿，不能悄悄导出旧值。可编辑材料草稿允许恢复未完成行，冻结/开始仍拒绝非法材料，这与完整执行请求不同。取消、心跳、无响应检测及异步结果围栏继续沿用已有实现；没有恢复低额默认预算。

## R2修复前的历史验证及确切边界

- 主回归16模块：206 passed、5 subtests passed，75.94秒。执行期间唯一源变化为新增测试中的 `deepcopy(Mapping)` 改成 `deepcopy(dict(...))`；所有生产代码稳定。因此该整批不标为全源稳定，最终版本另跑该文件10 passed（1.10秒）且969源前后稳定。核心 owner 最终40 passed（7.67秒），含该10项与物理结构/typed jobs。
- 原生 Windows Qt/QTest 私有窗口回归：22 passed，124.96秒，969源前后稳定；119张截图。独立定向数值及原生验证8 passed，9.86秒，与22项为不同模块；不把重叠 owner 集合累加成独立总数。额外只重跑该独立集的1个 ZnS 原生用例获取7张可见证据，源稳定。
- 已知100nm薄膜 k=0.12 确实进入 TMM：未知基片策略的透射率与基片显式 k=0 的严格对照及公开 frozen simulate 结果一致到2e-13；如果错误地把已知薄膜 k 也清零，T 相差大于0.02。n 域外、已知 k 域外、非正 n 均拒绝；源材料记录摘要不变。覆盖请求往返、审计、检查点、保存和物理 seed/typed job 一致性。
- 用户真实仅 n 的 ZnS：严格模式原生点击开始拒绝且不增队列；实际点击复选框后原生点击开始，job `e5fdbb581abe4963a40bd6c5f5962f78` 到 `succeeded`，冻结环境保留真实 ZnS 引用、`assume_lossless=true`、`use_k=true`。详情可见假设，保存 `.cfilm` 保留引用和中文注记，关闭选项后再次严格拒绝。
- 上述 ZnS 用例目标为600–900nm、0°、R0–100%、N=0、无 seed，只验收策略输送/计算/保存，不是原截图 R0–1% 的减反达标证明。实际0层、0轮、2次评价；13个唯一审计点、2次审计调用，最坏 R=0.16418883249515048（约16.4189%，600nm）。结果为本假设下有限审计达标，不是连续证书或真实吸收验证。此前 synthetic 已知解的 R≤1% 成功仍是单独历史算例，不能替代真实 ZnS 指标。
- 锁定环境全量静态扫描969源，exit1、相对既有基线40项、resolved222、fatal0；40项 fingerprint 全已存在于此前 GUI 修复扫描，本阶段引入0项。全量扫描期间只有上述测试 typing-copy 变化，故不声称该全量报告与最终全部源同一时刻冻结。最终 owner 范围 Ruff/自有新逻辑和类型诊断为0；已有 SciPy stubs 缺失及其他导入诊断仍记录，整库门禁没有通过。未改基线、忽略规则、配置、依赖或启动器。

原生证据是应用自有私有窗口及 QTest 真实鼠标/键盘事件，不是操作系统桌面自动化或对用户窗口的操作。root 已查看实际结果截图及离线算法 SVG 示意。

## 实际模型调用与剩余复审

用户明确允许只发送本次修复代码片段和测试摘要后，实际调用 `opencode-go/deepseek-v4.1-flash`，不是把其他模型重命名为 DeepSeek。第一轮127.684秒、exit0，会话 `ses_ea8b7f8fcffegTiBtFJm1QV7TJ`，返回 changes_required/9项；2项有效问题已修复，上述其余条目有逐项反例核查/契约解释。第一轮只审当时 GUI 修复；后续未知 k 策略改变数值输入语义，不能用旧轮次替代新算法复审。

第二轮实际发送92718字节的相关代码/合成测试摘要包，SHA256 `441cfbb2fb5499059ec7f3a9a26742fec68c2a4c9089f6f10c059b912be24470`，会话 `ses_ea89b17ccffedQ7DuXqR56wwVy`。CLI在240秒超时，exit124；随后再次只读导出取得实际已完成的 assistant 最终答复，finish=stop、会话outcome=succeeded，result=changes_required。没有因超时重复发送；六项发现按真实源码/反例核实后收口。归档仅含授权路由、发送包、调用摘要和最终答复/元数据，不归档模型推理。

R2的三项真实问题已修：候选claim跟随实际used材料；检查点保存worker累计工作量、旧候选继续恢复但未知计数对外null并显示“未知”，恢复重放工作独列；非法角度草稿下的复选框视觉漂移经真实native红例证明，现按busy/shutdown/pending禁用并在拒绝竞争中恢复Qt.binding。另三项为上下文误判：原prepare_optical_set默认已经source（调用处现显式声明并补窄k反例）；k_table不是支持字段、仍明确拒绝；材料行property保留稳定UUID，模型误用了另一个平面catalog。真实57层腔探针确认所有密集/错位/极值物理波长都事先source严格校验，QW物理seed早已转换nm、往返metadata不变；缺assume_lossless按既有schema明确ValueError，不是后续KeyError。

实际 `opencode models` 及限定的非秘密配置/安装入口核查未找到可调用 Seed 2.1 Pro。用户补充的 `deepseek-v4.1-flash` 是 DeepSeek ID，不是 Seed。因此 Seed 仍 pending；没有伪造 Seed 或指定 Sol 签核，没有调用用户禁止的 GPT-6-sol/Astra。复杂核心及独立测试由实际 gpt-6.1-sol 完成，Luna 做只读落地清单。旧的自动审批拒绝记录原样保留；新调用是在收到明确授权之后执行，与旧拒绝区分。

## 只读算法落地清单

完整13项在 [当前算法落地清单](film-synthesis-algorithm-rollout-20261020.md)。本次只盘点，不借该清单扩写新算法：

- N0起步的有限厚度增层、删层、换材、厚度精修及逐项目标审计已接 GUI/CLI/MCP。
- classic-local 与 classic-global 有核心和 CLI；GUI/MCP 未路由，local→global 串联未实现。global 为经典种子枚举后固定拓扑差分进化，不是自动调用 local 精修。
- 多膜堆、耦合腔、啁啾、数字 rugate、组合结构已有处方构造及 GUI/CLI/MCP seed；结构生成不是自动结构搜索。匹配层/半波包仍是手动构造，不是自动发现。
- 有限 birth 集合、needle 探测、seed 轮转不等于任意结构完备搜索、beam/lookahead 或已验证复响应多样性。phase 步长限制不是相位目标/同伦；GD/GDD 未支持。
- 密集/偏移网格和局部极值搜索为有限审计；有限角度目标不是连续角域证书。最大违规/RMS/代价排序已实现，独立 KKT 退化探测未实现；未把研究中的不稳定优势写成已验证。

## 交付与证据

契约为 [未知k策略](film-synthesis-unknown-k-contract.md)、GUI/执行/审计契约，离线 [算法文档14v](algorithms.html#film-synthesis-unknown-k-20261020) 已更新用途、I/O、单位、伪代码、SVG、默认、实现、近似及来源。未知值取0和元数据组织明确标“本项目约定，未找到外部依据，仅为本项目实现”。README 保留算法入口。

主证据目录：`artifacts/film_synthesis_20261020/unknown-k-policy-20261020/`。真实 ZnS 详情：`artifacts/gui-repair-20261020/unknown-k-review/native-job-evidence.json`；可见截图：主目录下 `native-policy-capture/f71a266bdf2d-4-filmSynthesisAbsorptionSummary.png`。每批命令、耗时、源前后 SHA 和退出码保留，最终冻结清单记录证据 SHA 与全部969源。

可用工程阶段已完成；指定模型最终状态以本报告下方R3归档及冻结清单为准。Seed调用入口缺失和整库既有静态诊断仍保留，没有把这些写成完成，也没有推送或发布release。后续算法按用户新指示先在云端研究测试，本轮没有扩展算法或在sakuya新跑探索。

## R2修复后的同源最终验证

全部969源冻结后，主回归16模块：218 passed、5 subtests passed，79.90秒；原生22项：22 passed，122.94秒。两批及最终ZnS原生capture均源前后稳定，并与最终清单完全一致，替代上文历史批次的最终源效力。定向核心43passed/13.57秒；独立未知k验证以新版unknown-k-review-r2最终summary为准，新增pool-unused反例，不与重叠批次累计计总数。GUI复选框busy/pending/修正重开绑定及旧检查点nullable计数均有原生定向覆盖。

最终锁定全量静态扫描及其source-binding以新manifest为准；既有诊断不豁免、不刷新基线。算法14t同步增加累计统计的输入输出、单位、伪代码、默认与限制，14v明确pool/used claim及source策略。GUI、执行和未知k契约同步记录null/未知兼容。

R3为发送实际修复和新证据的定向后续复审，不是超时重发。实际状态：`completed_ready_for_seed`，会话`ses_ea8898201ffethmSdWn2hH0YTr`，模型`{'id': 'deepseek-v4.1-flash', 'providerID': 'opencode-go', 'variant': 'default'}`；原始final JSON和发送包SHA保留在model-review/r3-*。若result为ready_for_seed，仅表示DeepSeek该轮无待修问题，不是Seed已审。Seed没有可调用入口，保持pending；没有调用被禁止的GPT-6-sol/Astra。

本轮最终真实ZnS独立job为`fe371921dd62435f8b22239beacc5e34`（上文e5开头job为历史批次），仍是0层、0轮、2次评价、1次候选验证、2次审计物理调用、13个唯一波长点。最终图像为`native-policy-capture-r2/f71a266bdf2d-4-filmSynthesisAbsorptionSummary.png`，root已查看其真实像素；R0–100%的通路用例不提升为R≤1%的光学指标成果。独立新版9项通过8.99秒，与最终13个核心/GUI/测试文件SHA相同。

指定模型未实际调用的审查不能标完成。[model-stage-orchestration/SKILL.md](skill://user/6ab76038067081919df7dfefaa46e665/model-stage-orchestration/SKILL.md)原文：“未实际调用指定模型时，相应审核必须保持 pending。” 本轮据此保留Seed pending，实际路由名deepseek-v4.1-flash不代表Seed。

R3保留的上下文限制原样归档。补充纠正文包措辞：R3发送包曾说删除了k_table探测，实际当前代码仍含该死分支；validate_optical_set拒绝此非schema字段，因此无支持输入反例，不声称已删除。历史已物化结果若无工作量来源标记，不能追溯证明原累计成本；本次legacy null兼容指经当前validate_checkpoint重新核验恢复的旧检查点。R3没有亲自运行测试，不覆盖Seed独立审查，也不是全路径证明。
