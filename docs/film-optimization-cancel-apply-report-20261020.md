# 取消时应用当前最好候选：阶段验收

日期：2026-10-20。基于当前目录HEAD `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2`和真实未提交源码实施；保留原工作，没有提交、推送或release。[跨模块契约](film-optimization-cancel-apply-contract.md)先于实现更新；离线[算法14x](algorithms.html#film-cancel-best-20261020)同步说明候选选择、计量和界限。

## 当前行为

当前活动常规优化和从零合成的明确取消，会冻结父进程取消前已完整评分并提交的最好合法候选。训练最好优于旧审计候选时保留训练最好，不在取消后追加TMM或长审计；没有同一处方的已有独立审计就如实为null/false。任务仍cancelled，未审计或未达目标不会显示为优化达标。结构优化旧桥的candidate_verified=false保持原义，不伪造物理重放。

候选只有通过当前请求/spec/digest绑定、typed校验、项目/session/revision/edit epoch/材料闭包守卫才自动应用。成功是一次原子提交、一次撤销，不自动保存；相同设计不新增dirty/revision/undo。来源已编辑或切换时保留候选、显示冲突，不覆盖新编辑。匹配且传输已确认的当前工程队列取消也生效；关闭、shutdown、历史任务、旧operation和重复终态不触发。

应用后通过现有来源光谱入口请求当前处方光谱。requestSourceRefresh仅合并同一完整输入的在途或已完成新鲜谱；谱页手动requestCalculation仍强制计算。独立真实TMM复现捕获了Future在注册回调前已完成的合法时序，旧实现启动2次，修复后1次，处方及源/谱revision一致。launcher现有窗口、待创建窗口和project host使用同一路由。材料n/k变化、身份未知、失败/半成品和旧来源不准复用。

## 已完成验收及证据

各组独立执行，不能把不同时间/源码身份的结果称为一次全套：

| 检查 | 结果 | 证据 |
|---|---:|---|
| 核心新取消语义 | 6项通过，另135项既有回归通过 | cancel-apply-20261020/core-cancel-*.log及freeze |
| typed/worker/scheduler取消回包、结构桥及迟到完成 | 30项通过 | cancel-jobs-green.log；cancel-jobs-freeze.json |
| 既有CLI/worker取消契约 | 2项通过 | root-compat.log及JUnit |
| 独立GUI真实会话/实际N0任务/真实TMM及版本守卫 | 35项通过 | independent-review-35-junit.xml；当前测试test_film_optimization_cancel_apply_review.py |
| 原生QML实际取消按钮、成功/来源冲突、重复点击/终态、撤销与不保存 | 2项通过 | native-final.log、native-final-junit.xml及native-final/*.png |
| 既有GUI兼容 | 先42项通过，另run-contract 13项通过 | compat-ui-r2.log、run-contract-r3.log；原fixture错误红证据保留 |
| 真实fast Future双算竞态 | 2次红→1次绿 | artifacts/cancel-spectrum-review-20261020/red-01及green-01/report.json |

证据根目录为`artifacts/film_synthesis_20261020/cancel-apply-20261020/`，独立谱竞态证据单独位于`artifacts/cancel-spectrum-review-20261020/`。测试fixture按实际queue cancelReceived和冻结材料快照接口修正；保留原提交/重试/关闭、IPC、规范化请求及输出排他性断言，未降低产品校验来迎合fixture。成功状态明确“已应用到当前膜系，未保存，并已请求刷新光谱”；原生最终截图已逐张查看，冲突状态明确未自动应用。

## 最终收口

新增真实谱合并最终20项定向回归通过（25.50秒）；另一次19项定向+43项既有workflow/launcher/project兼容，共62项和13个subtests通过（45.18秒），多偏振材料交错补例另行通过。最终独立只读窄审无剩余发现。当前冻结源码的原生QML两例再次通过（5.00秒），`native-r2-final/`成功和冲突截图已逐张查看。最终全量静态R5覆盖981项源文件：相对静态基线仍40项既有欠账，和前CPU阶段逐指纹比较，本轮新增0、fatal0；完整门禁因既有40项仍exit1，不称全库静态通过。基线、规则、分析工具及锁文件SHA守卫均未变。历次红诊断和Windows长参数失败证据保留；最终修正的是材料owner状态的真实类型及空值收窄，缺失材料仍False。冻结清单为`cpu-cancel-stage-final-freeze.json`。输入草稿阶段的13项原生/45项相关回归已单独交付，本轮没有重复算作取消成果。当前[输入阶段入口](film-optimization-input-report-20261020.md)启动的是当前目录源码，不是历史打包程序。

静态基线、规则配置与工具源码不因本轮取消修改而改写。上一CPU阶段仍有40项既有相对基线欠账；不能把本轮没有新增诊断说成全库静态全部通过。原全量命令遇Windows参数长度上限，保留失败；证据目录的adapter只改变同一固定版本Ruff/mypy的参数运输，完整source inventory、配置、基线、指纹与源码稳定性守卫保持原样。

本阶段未复用旧阶段的外部模型源码披露授权，尚无新DeepSeek/Seed复审。内部gpt-6.1-sol独立窄审与测试结果如实记录；不冒称指定外部审查已完成。当前未有环境或权限操作待批准。

材料时序验收覆盖project先冻结A后live切B、standalone busy A→worker B→live A、身份未知和真实附加s/p材料版本不同。复用判据绑定实际frozen/provenance，必要时仅补一次当前输入计算；不加TMM审计，不自循环，不改变手动强制计算。
