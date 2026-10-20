# P0/P1 自动合成与物理结构阶段回执

状态：v2独立验收changes_required，已按父任务授权解冻；v1已撤回。v3功能实现与最终回归已完成，冻结为有明确静态阻断的候选；不是无条件签收。最终36模块514测试/105子测试通过，静态原门禁退出2，详情见本文末尾与freeze-manifest-v3.json。v2历史源码哈希与证据保存在 freeze-manifest-v2.json；当前三个阻断进度以 artifacts/film_synthesis_20261020/V3-STATUS.json 为准。本阶段文件名按本次工作目录日期20261020命名。

## 当前基线与工作边界

工作目录为 `C:/Users/youai/Documents/coating-dev/coating-sim`，HEAD始终 `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`。未merge、cherry-pick或用旧项目文件覆盖。开始时已有未跟踪auto/classic-local/classic-global核心与classic CLI/测试/契约、algorithms.html修改；在这些现场内容上继续实施。原有examples/、legacy/删除，材料、UI测试和hang_trace均保留。本批不提交、不推送、不release。

PowerShell7.6.5、项目.venv Python3.13.16、SciPy1.18.1。普通受限读取工作树Permission denied，按授权在同一目录require_escalated读写成功；未更改隔离、凭据、Git配置或安全设置。OpenCode有DeepSeek配置但命令未在PATH，Ark未找到可调用命令；本批在原目录直接执行，JUnit记录本机hostname为sakuya；未切换机器或需要SSH，不为模型路由阻塞工作。早期preliminary runner进程cwd在task-2但显式导入原项目，单独标历史，最终runner在原目录执行。

分工：gpt-6.1-sol负责生长和结构两个独占任务；Luna负责接口及数据/实验；另gpt-6.1-sol只读独审，主线程集成审计与文档。没有调用GPT-6-sol或Astra。Seed Pro未调用，独审报告不得当作Seed签署；父任务v2独立验收已完成且不签收，v3已获本批独审53项通过及最终整体514测试/105子测试通过；父任务最终独立签收尚待进行。

## 可用入口及尚未接通项

| 入口 | 当前实际能力 | 证据 |
|---|---|---|
| Python核心 | GUI独立完整合成；N=0有限正厚度、删层/换材/精修、任意seed与独立已审计档案 | test_film_auto_synthesis/growth/audit |
| 产品CLI | 实际完整搜索；stderr进度/独立心跳、无响应标志、SIGINT/取消文件、排他输出、原子审计checkpoint、完整层表 | film_synthesis_cli；真实单层和ZnS/GST CLI请求/结果 |
| 助手/MCP | plan_film_synthesis只读规划；submit_film_synthesis接现有估算/确认/持久队列，讨论模式拒绝提交 | canonical输入与模式/真实registry测试；最终回归见本文末尾 |
| 助手执行 | 后台job接通；既有status/wait/cancel/result返回心跳、审计层表，配置业务root明确披露 | 真实无seed N0 worker完成、审计后取消用例已通过；来源、反篡改及取消语义已通过定向独审 |
| GUI | 未接入新窗口、按钮或自动应用 | 不宣称GUI可用 |

默认max_cycles/max_evaluations/厚度/层数约束仍None，4096层为原schema资源边界。实验的显式预算不是产品默认。结构库并非孤立脚本：纯core被产品CLI与助手统一冻结/任务入口共同消费，统一物理引擎与目标类型，未重写光谱求解器。

## 实现文件和契约

- 生长：coat_sim/core/film_auto_synthesis.py，tests/test_film_synthesis_growth.py；原auto_synthesis两测试文件按新独审语义保留行为保证。
- 审计：coat_sim/core/film_synthesis_audit.py，tests/test_film_synthesis_audit.py。
- 结构：coat_sim/core/film_physical_structures.py，tests/test_film_physical_structures.py。
- IO/CLI：coat_sim/core/film_synthesis_io.py、coat_sim/film_synthesis_cli.py、tests/test_film_synthesis_cli.py。
- 助手注册：coat_sim/mcp/server.py、coat_sim/agent/runtime.py、coat_sim/core/mcp_contracts.py。
- 实验工具：tools/run_film_synthesis_acceptance.py；支持心跳、取消文件、来源和代码hash，心跳不会冒充core实际活动。
- 必要测试修复：tests/test_film_design_mcp.py 不再依赖已删除历史example，使用当前格式的自包含输入。
- 文档：algorithms.html 14q修订和14t/14u新条目（离线单文件、本批图解使用内嵌SVG、无在线运行资源）；README入口；film-auto-synthesis、film-synthesis-audit、film-physical-structures、film-synthesis-execution契约；entrypoint-capabilities.md。

## 目标与实际指标

所有区间目标只声明有限密/偏移/局部极值审计，continuous_certificate=false。合成常数k=0和真实来源n/k分列；种子正控不称N=0发现，低R不等于高T。

| 案例 | 处方来源与目标 | 结果/实际工作 |
|---|---|---|
| 裸片已合格 | 合成明确k=0，保留N=0 | completed，0层，2次逻辑评价 |
| 已知单层AR | N=0自行长出单层，550nm | completed，1层，1cycle，5评价 |
| 多层反射器 | N=0，原8cycle/1800评价限制、R≥.9均未放宽 | completed，6层/6cycle/642评价；独立401点minR=.902399808390441，能量闭合8.9e-16 |
| Bragg生成器正控 | 已知真实展开seed | completed，7层/54评价；不是从零发现 |
| 耦合腔双通带正控 | 21层真实耦合腔seed；4709–4712、5326–5329nm T≥.99，gap4950–5050及两外阻带T≤.06 | completed，67评价；全审计点窗最低T .998419615@4712nm/.999036020@5329nm；gap最高.058320983；外阻带最高.034388043/.038743358。此为窄窗正控，不替代宽窗困难问题 |
| 真实n/k六点新设计 | ZnS Querry基片、GST/ZnS 4/10µm多堆起点；3000/4000/5000/8000/10000/12000nm R≤裸ZnS各点R−.005，本项目约定；acceptable=.001 | 容差内completed，6层/4cycle/447评价；4000nm实际R=.144041540418，严格上限.143657034788，超差.000384505631≤容差.001，严格上限未过；实际审计5次/30条件点；不是六谱线论文复现 |
| 双连续AR | 合成模型，3–5µm及8–12µm各点R≤.01，间隙无目标；7层先前档案继续 | v1运行到8层/10cycle/1304评价后ID碰撞failed；保存最好审计R=.0156561@3561.94nm、.0169567@8000nm；仍未达。v2仅修ID：原2500评价请求独立续跑17cycle/2499评价、零失败、正常evaluation_limit/not_found结束、8层ID全唯一，指标仍为1.565608654%/1.695672548%，未达到1% |
| 宽双通带+阻带 | 合成模型，3–5/8–12µm T下限及显式gap/外侧T上限 | 6518评价/43cycle/5层后协作取消；最大违规.57980286；未达，不是不可行 |
| 六离散线 | 合成模型，532/1064/2100/3900/4800/9500nm各点R≤.02；点间无目标 | 16772评价/46cycle/23层后协作取消；R分别.023556/.008454/.029691/.004833/.010510/.029700；3条未过，最大违规.00970025，不是窗口结论 |

真实6点候选的独立161点/窗R/T/A：3–5µm R .0261–.5258、T .3721–.7516、A .1021–.2231；8–12µm R .0104–.1282、T .7190–.8180、A .1509–.1798，R+T+A残差≤1.2e-16。它只满足指定六点改善，在连续窗可以高反射/有吸收，不能称宽带高透。

更强的项目研究门另存 `real-nk-high-transmission-audit.json`：对同一真实6层候选的六个点要求R≤.01且T≥.90，独审明确不通过；该文件本身仅为加强复核；后续另用MgF2/BaF2/NaCl/ZnS及Ge真实来源池执行同类强R/T双窗联合优化，结果在本文末尾。不能把原容差内R改善说成严格降.005或高透减反。该门槛为本项目研究设定，不冒称用户指定指标。

## 数据和失败保留

真实源数据来自离线RII：ZnS Querry完整tabulated nk、GST Frantz非晶/晶态完整tabulated nk；保留source/provenance/hash，但不匹配目标器件工艺。ZnS/YbF3 Amotchkina的n域可宽而独立k源域不足，不能拼成全窗已知k。六谱线文献缺29层完整厚度和工艺复色散，仅借鉴规格，不复现论文。

准确兼容路径：film_design_service.py:556/762/777、film_optimization_derivatives.py:310、film_performance.py:298、film_optimization_structure.py:331/1149使用source_only_zero_defaults。最小复现见artifacts/film_synthesis_20261020/k-domain-compatibility.json：n域400–1200nm、k域500–600nm，查询800nm，source策略抛越域；兼容策略返回k=0且k_known=True，同时k_default_reason=outside_source_range。本批不改旧兼容路径；新freeze、prepare和公共audit按实际材料/实际波长显式source策略严格拒绝，错误含材料/集合/波长与目标区间。

失败和取消均保留：早期真实runner物理复算NameError已修复，原real_nk失败记录未删；preliminary非PTY Ctrl+C只有checkpoint、取消状态不确定，不算成功取消；最终宽双通带/六线用CANCEL协作返回终态；v1双AR重复ID失败保留，v2独立验证另存。

## v2续跑修复与最终入口验证

运行本地reserved集合初始包含environment和全部seed的front/back层ID；每次needle/split/growth分配登记，不因删层或丢提案释放。冲突按稳定后缀分配并保持128字符边界，旧层ID及引用不重编号；zero/finite共享同一概念层，growth保护引用实际分配ID。新增五类真实回归覆盖碰撞seed续跑、冻结重放同trace、保存七层几何、生产CLI实际写出的审计checkpoint恢复、实际删层后再生长以及跨seed/丢弃proposal保留。

CLI取消测试改为先观察真实heartbeat和完整audited checkpoint再创建CANCEL，断言exit130、targets_met=false、validation_complete=true且返回层表与最后checkpoint一致；不再用固定80ms启动延迟造成竞态。请求输入、结果和checkpoint不能同路径；已有结果不覆盖、无关checkpoint不替换、NaN计时值拒绝。v2历史入口仅prepare，其实际MCP call_tool与CLI --plan canonical输入和结构元数据一致，当时没有助手后台执行链；v3工作版补接现有队列。

## 冻结后的实际R/T联合探索

稳定实现不改动，参数和产物在 artifacts/film_synthesis_20261020/exploration_v2，当前状态见STATUS.json。两连续窗均为3000–5000和8000–12000nm，间隙无目标，41训练点/窗、独立4倍密/偏移/极值审计；请求R≤.0099，并对教学模型T≥.9901、真实输入T≥.9001，容差1e-6。这是更强的本项目研究目标，非论文或用户逐数值指定要求。

教学材料明确k=0、六个显式常数n（约1.0021–1.4969）及n=1.5基片，包含现实工艺未验证的低n材料，不能外推到制造。空栈实际完成3层/3cycle/259评价，两窗最大R=.009348224/.008564566、最低T=.990651776/.991435434；结构起点实际优化并删层到4层/89评价，最大R=.009726977/.002880363，最低T=.990273023/.997119637。两条路线分列，均通过比R≤.01、T≥.99略严格的目标，不是对旧弱六点事后审计。

真实输入新池为MgF2 Franta薄膜（完整n/k、e-beam134.2nm，工艺未与本项目匹配）及BaF2/NaCl/ZnS Querry块材代理（源表k=0是显式数值）。均严格来源域采样，未写用户材料库。空栈13655评价/98cycle后最佳违规.022119232、4层；四个结构起点12293评价/68cycle后.019595072、6层，T已>96%，R仍未过1%，因长期结构平台协作取消且保留结果。既有TRF对同一处方18评价/10迭代收敛到独审违规.014059968；进一步有限单层出生目录+TRF完成56试验、1269求解评价、308.21秒，最好7层最大违规.0116351825951，T门已满足但R仍未过；已全部收尾。不得把这些未达情况解释为物理不可行，也不把结构起点叫从零。

## 已知局限与下一步

P1多堆栈端/缝匹配、单/耦合多腔、三种独立多谱规格已真实展开并联合相干算；P2仅支持可生成chirped和真实H/L数字rugate及退化/收敛检验。数字rugate的法向深亚波长近似不等于未知混合n/k线性律。phase/GD/GDD、金属介质专项稳定性验收、工艺/应力/制造良率未支持。

局部SSE方向配最大违规接受是有限启发式，两种换材提案不覆盖全部局部盆地，删层未承诺全局重综合。审计反例尚未自动反馈进梯度训练，困难多窗仍可停留平台。助手执行链的来源/反篡改与取消语义已通过定向回归，整体冻结验证已完成且静态阻断如实保留；困难目标后续需要改变盆地或更强结构搜索；不把负结果包装新算法优势。

## 回归、冻结与通讯

v2最终扩展回归211 passed、6 subtests passed、2条既有MCP SDK字段弃用警告，36.46s；原始输出 artifacts/film_synthesis_20261020/final-pytest-v2.txt。独立gpt-6.1-sol复核99 passed/21.74s，报告为 film-synthesis-independent-review-20261020.md。v1曾为133定向通过、扩展205通过/1缺夹具失败；按父任务新增授权，将 tests/test_film_design_mcp.py 改为 new_design 加临时根现行内置N-BK7材料，保留完整保存/加载、覆盖拒绝、来源和能量闭合保证。没有恢复旧examples删除，没有跳过失败。

原5类旧审计测试不是删除保证：固定17点改为至少33点且dense_points仍17；独立前向比较仍取同17个dense波长、容差不变；故障注入改到新audit入口仍要求不成功；between-node直接原裸片审计仍>.15，同时要求继续搜索后误差>tol且不劣初审、cycles>0。原物理目标未放宽。

主动向父线程01a11002-2328-779a-825e-1879eb6f900c发送基线/冻结回执的工具两次返回Codex app tool request failed，交付结果不确定，未继续重发；本地清单和本回执为可读交付，最终回合由平台通知父任务。后续官方scipy-stubs安装遇到自动审批拒绝，且未执行或重试；该依赖阻断的精确命令及拒绝原文单列保存，不据此声称完整静态门禁通过。本批未留研究runner进程。

## v2独立验收：不签收并解冻

父任务独立294测试/77子测试通过，但总体changes_required。三项阻断为：单面前入射错误查未启用exit材料造成KeyError；本批新代码Ruff21项及选定新生产文件mypy15项；助手只有plan，后台真实执行链尚缺。父任务明确解除v2冻结用于修复，当前不声称v2签收。历史产物保留原hash，v3修复和新运行证据分开。报告源为 C:/Users/youai/Documents/Codex/2026-10-20/task-3/evidence_v2/report.json。

独立真实材料N=0正控（历史v2，不替代本批验收）：RII Rodriguez-de Marcos MgF2膜材与SiO2基片、520–580nm R≤.026、ε=1e-4，无seed且无额外默认预算。实际1层94.077703851nm、1cycle、11评价达标；601点forward最大R=.0256592551、最低T=.9728808293、最高膜层A=.0014616968，闭合约1.1e-16。T进入半无限基片，源膜工艺非本项目匹配；不是双红外窗R<1%结论。源记录及只读SHA见 v2-independent-summary.json。

## 修复核心的真实Ge强目标探索（终态）

完整证据在 exploration_v3/summary.json。相同ZnS基片/双连续窗R≤.0099且T≥.9001，新增有完整已知k的Ge薄膜来源，不与工艺匹配。直接CLI空栈15169评价/94cycle、4层，Rmax 7.00755%/6.97216%；显式起点续跑12786评价/48cycle、7层，Rmax 2.10466%/1.24936%、Tmin97.24794%/97.96580%；均0数值失败，长期最好解平台后合作取消exit130，未达反射门。48个Ge有限增层+既有TRF邻域1441评价完成，最好未改善。源代码全程哈希不变，所有进程已收尾；不称物理不可行、Ge普遍优势或N=0发现种子解。前轮为修正研究PowerShell日志传输而取消的4676/3620评价另列，不混成成功达标。

分析环境纠正：父提供现成Temp/coating-analysis-af88ee4的Python3.12.11确实存在，当前锁101包全部匹配。此前注册解释器清单未列出它，不能据此声称无合规环境。真实完整门禁已跑到QML，Windows qmllint unsigned -1返回码触发现有runner退出2；完整门禁未通过，另列原诊断与基线比较。官方scipy-stubs隔离安装被自动审批拒绝且未执行/未重试；实际工具无approval/request ID，async accepted:true不是批准卡，详情见v3-static/scipy-stubs-denial.json。


## v3取消与候选事实语义（最终冻结验证前已完成）

任务的`state`、`termination_reason`和`targets_met`表示本次任务终态；取消任务不标为成功。新增`best_candidate_audit`独立保存已审计候选在同一归一化容差下的真实合格事实：已审计合格候选取消后仍为true；尚无完整审计时为null且available=false。原始train/audit分数、逐项目标值和完整层表不因取消而改写。训练候选未经审计不作为已验证层表返回，任何终态均不自动应用到原设计。当前`continuous_certificate=false`。

独立测试实际覆盖预取消、达标候选审计后取消及完成/取消提交竞态，17个执行测试与36个审计测试组合53 passed/7.49s，Ruff通过；对应独审报告SHA256 `dd404c016b3360412e051515b10a04c44da69e6a1b15c9526e14bc34868d303b`。助手/持久队列owner定向47项通过。前一轮整体512 passed/105子测试/2 skips发生在daemon及新增语义变化期间，source_stable=false，明确仅历史证据；不能作为最终版本验收。


## 最终整体回归（当前v3源码）

`v3-root-regression-r2`从冻结源码运行36个模块，514 passed、105 subtests passed、2 skipped、0 failed，pytest 220.31秒（完整driver 221.92秒）。两个skip均为既有外观渲染用例所需vendored Mitsuba runtime unavailable；517 warnings为既有Python pathlib与MCP SDK弃用警告，不是数值失败。运行XML主机名为sakuya。测试前后全部coat_sim Python、所选36测试和验收工具SHA完全一致，source_stable=true、changed_files=[]，退出0。

当前验证包含核心生长、真实光学审计、物理结构、CLI、助手权限与讨论模式、估算确认、持久队列、remote传输、结果能力与既有兼容入口。实际无seed worker和取消后的候选事实由17项独立执行测试覆盖；没有把mock测试、只读plan、已被替代的512测试或层表投影当作唯一实际执行证据。原始命令、完整日志、JUnit和前后SHA见`artifacts/film_synthesis_20261020/v3-root-regression-r2/`。


## 最终完整静态门禁与候选冻结

使用已经存在且101个锁定包完全匹配的Python3.12.11环境，实际原样运行`tools/run_static_analysis.py check`；未改配置、baseline或通用runner，未安装依赖。最终`v3-static/summary.json`为唯一当前摘要，旧摘要已归historical。951个源文件在扫描前后、只读fingerprint比较期间及发布前均一致，源码digest为`f3e40cf393d567fbceea45fd7e8bac6a53adcb45207a1b80f7bd7a78ceb32098`；也已逐文件与最终回归快照比对。

真实门禁退出2：Windows qmllint返回4294967295（unsigned -1），现有runner不接受该返回码。原Ruff1429、mypy3865、QML1741条原始诊断全部保留。只读补充解释使用现有官方fingerprint比较，新增40、已消149；40项为31项Windows/POSIX平台差异、8项既有未变源码/工具差异及1项`scipy.optimize`缺类型stub。本轮新增自有代码诊断0；这不表示原门禁通过。

官方`scipy-stubs>=1.18.1,<1.18.2`曾拟安装到任务隔离analysis目录，自动审批以“网络安装并修改外部分析目录未经授权”拒绝。安装进程未创建，未重试、未绕过；工具没有返回approval/request ID，后续async accepted:true也不是批准。精确命令、目标及拒绝原文见`v3-static/scipy-stubs-denial.json`。现有锁定环境确认并使用后，该stub仍不存在。

候选清单为`artifacts/film_synthesis_20261020/freeze-manifest-v3.json`（同内容当前入口`freeze-manifest.json`），状态`frozen_candidate_with_known_static_blockers`，ready_for_unconditional_acceptance=false。代码/测试和本报告哈希冻结，等待父任务独立复核；v1撤回与v2不签收产物不覆盖。功能可用不代表困难真实双窗目标已达标、GUI已接通或完整静态门禁已通过。全部研究runner终态；未提交、推送或发布。
