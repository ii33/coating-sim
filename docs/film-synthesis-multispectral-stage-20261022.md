# 多光谱合成阶段交付（2026-10-22）

本阶段交付分离HR窗口先验、全部原R/T条件联合minimax精修，以及默认GUI适配器→冻结→typed job→`execute_job`的接入。**研发三案的成功不代表用户四窗主目标已通过。** 无用户工程或现有材料记录被改写；旧冻结基准保持原值。

## 默认实现及边界

- `film_synthesis_multispectral_priors.py`：各HR窗口独立估算中心/配对需求，合并重叠窗口而不填间隙；需求1/2、3/4、全需求的低层阶梯，显式层数上限另保留边界候选。匹配/连接层计入层数和厚度。
- `film_synthesis_multispectral_minimax.py`：真实色散/吸收和生产解析厚度Jacobian；联合R/T下界、上界和值目标；按窗口归一质量的logsumexp仅引导搜索，不改原max_error/RMS；再做SLSQP epigraph。
- `film_auto_synthesis.py`：纯分离HR先运行新前缀；混合目标先保留原结构快速通过路径，未达标再进入新前缀。每阶段48迭代是调度量子，父级时间、物理/逻辑评价和取消预算仍统一控制。原搜索继续回退，不保证全局收敛。
- 原评分相同时，精修器可按有符号最坏余量择优；父级严格rank冠军另存并参与独立审计。训练达标不能覆盖更好的已审计候选。独立加密/偏移/极值审计与固定快照反馈修复保留。

公式、图解、默认参数、单位和项目经验规则见离线单文件[算法图谱14ah](algorithms.html#film-multispectral-minimax-20261022)。所有光学前向对完整膜栈相干计算，不能相加两个子堆的性能。

## 同机研发回归协议与结果

[原冻结协议](../benchmarks/film_synthesis/20261022/multispectral-v1/protocol.json)在跑结果前制定，baseline为`924c989738926e7b3a9b6c1e7a8a86dd9fc457aa`。完整公开结果和运行源码SHA见[comparison.json](../benchmarks/film_synthesis/20261022/multispectral-v1/comparison.json)，处方见[candidates.json](../benchmarks/film_synthesis/20261022/multispectral-v1/candidates.json)。这些处方只用于结果回放，不注入生产初值。

两种材料为TiO2 Franta薄膜和MgF2 Franta薄膜，保留来源全部复数n/k；基片为Malitson块体SiO2，仅其n-only模型显式假设未知k=0。基准请求的完整envelope按`json.dumps(sort_keys=True, ensure_ascii=False, allow_nan=False)`默认分隔符计算SHA256：`f9865f384892b80688a5493e4310f128b102bbbf64fecbe62ed29d0f4f3214d3`。该模型覆盖这些可见/NIR窗口，不能直接套用至12μm。

来源：[TiO2 Franta DOI](https://doi.org/10.1117/12.2190104)、[MgF2 Franta DOI](https://doi.org/10.1016/j.apsusc.2016.09.149)、[Malitson DOI](https://doi.org/10.1364/JOSA.55.001205)。固定RII上游`c5c2f188e848453def5970e347399d653df2ffc2`，各源身份与哈希保留在协议和原请求中。模型低k并非本用户沉积工艺的吸收认证。

三案均为无答案种子的裸栈；训练1nm、独立审计加密倍率10（0.1nm及偏移/局部极值），CPU worker=1、进程数学线程环境=1。每案seed=0、1。间隙无性能约束。角度是表中离散条件，不能称连续角度证书。

|研发用例|原目标、物理上限、预算|旧默认链|新链|
|---|---|---|---|
|双窗HR|570–930及1034–1094nm，40/50°非偏振R≥99%；最多96层，单层10–500nm，总厚14000nm；180s/6000评价|0/2；181.28/181.15s超时；96层|本阶段未跑新链端到端，不宣称改善|
|双窗R/T|650–670nm R≥90%；1000–1020nm T≥85%；0/30°各s,p；最多32层，单层5–500nm，总厚5000nm；60s/2000评价|2/2；2.37/2.27s；16层1492.76nm|2/2；1.33/1.31s；仍16层、115次评价，走原快路径|
|三窗AR-HR-AR|480–500nm R≤5%；650–670nm R≥90%；1000–1020nm R≤5%；0/30°各s,p；最多48层，单层5–500nm，总厚8000nm；90s/3000评价|1/2；62.44s通过、90.36s超时；8层|2/2；6.93/6.84s；8层946.15nm；434次评价|

新三窗两次结果相同：两个AR窗最坏R分别4.4214343%、4.3580267%，HR最差R90.6789109%，全部原条件经独立审计通过。联合minimax段2.219/2.174s、全部独立审计0.706/0.696s；其余结构筛选、准备、档案、进度和序列化时间单列为未归类，不捏造profile归因。双窗R/T仍用相同处方和工作量，墙钟波动不能归功于新精修。各比较均同材料、同目标、同约束/停止条件；baseline导入/源码身份预检在计时之外，计时含GUI请求构造至完整结果JSON写出。

旧双窗HR曾训练max_error=0，但最后保存的独立审计最坏R仅72.2631441%（宽窗）和99.7175730%（窄窗），未过99%全窗要求；超时返回训练候选不等于已有匹配审计。本阶段保留该失败而不降低指标。96层只作为旧研发硬上限，新需求阶梯并不默认填满。

## 用户四窗主目标与明确来源模型

用户指定单面入射至ZnS基片：580–920、1044–1084nm非偏振R≥99%；3000–5000、7000–12000nm非偏振R严格小于3%；角度39–51°。不按双面整件T验收，不计背面。目标与采样计划见[requested-four-window-v1.json](../benchmarks/film_synthesis/20261022/multispectral-v1/requested-four-window-v1.json)。

来源研究库固定提交[78fb88a](https://github.com/ii33/coating-research-library/tree/78fb88a9610ba2cdc3da26480dc70424b6cbf337/manual_studies/2026-10-22_four_window_material_qualification)。四个公开CSV落盘SHA全部与source_catalog一致。新[模型manifest](../benchmarks/film_synthesis/20261022/multispectral-v1/four-window-models/manifest.json)和两个请求均已通过正常GUI适配器、材料冻结、job编码/解码预检，**尚未运行主目标优化，尚无四窗达标结果**：

1. `author_export`：Amotchkina2020 ZnS/YbF3各自原始成对n/k导出，保留导出零值为显式模型值，不称实测无吸收；基片独立使用Querry1987块体ZnS筛选模型。
2. `supplier_ybf3`：ZnS薄膜来源保持不变，YbF3改用Merck2019完整成对n/k稀疏表；不将一家n与另一家k拼接。基片同上。

两分支均不外推、不启用未知k=0；标量等效各向同性为模型假设。Querry低损耗k不确定性较大，Merck稀疏表未解析水吸收，作者模型零值不构成制造认证。原用户材料记录域不足的拒绝仍正确，不因新增独立模型请求而扩展原数据集。

首轮云端协议在结果前声明：每分支seed0、300s/6000逻辑评价、最多64层、最薄5nm，无单层/总厚人为上限。64层是这次探索的显式资源/拓扑范围，不是用户制造约束或可行性界限。未达标须报告差距与预算耗尽，不宣布不可行。训练1nm、39–51°每1°共13角；原生产审计每原角做0.1nm/偏移/极值。**主验收另需0.5°共25角的独立复核、单列s/p诊断**，不能仅凭13角通过宣称覆盖整个角区间。严格R<.03以`nextafter(.03,-inf)`上界和零容差表达，未改成含等号或平均反射率。

## 验证与复现

318项有界定向回归通过（315项核心/原HR/AR/带通/审计反馈/预算，另3项公开协议与job）；本阶段8个Python文件Ruff/Mypy定向检查；独立Sol6.1只读技术复核。旧Linux contract原生堆损坏仍开放；本阶段不修改Qt生命周期或声称完整CI全绿。

研发回归公开命令（新Temp目录，不覆盖）：

```text
python tools/run_film_synthesis_multispectral.py --case three_ar_hr_ar --seed 0 --output <TEMP>/new-multispectral-run
```

该整理后运行器另做一次真实服务smoke：7.39s、8层、434次评价、原审计通过；其源码与HEAD运行中未变。它不是早先基线实验的原脚本，时间不混入同口径四次比较。

四窗云端使用既有CLI，无GUI/外部材料目录依赖；从准确发布SHA检出源码，最低Python3.12+NumPy/SciPy及项目既有依赖。以下`<TEMP>`应替换成实际临时目录，每次结果路径必须新建：

```text
python -m coat_sim.film_synthesis_cli --request benchmarks/film_synthesis/20261022/multispectral-v1/four-window-models/author_export-request.json --output <TEMP>/four-author-result.json --checkpoint <TEMP>/four-author-best.json --heartbeat-interval 10 --stalled-after-seconds 30 --unresponsive-after-seconds 60
```

供应商分支使用`supplier_ybf3-request.json`与独立输出路径；取消通过CLI现有`--cancel-file`。物理/逻辑工作与心跳、取消、已审计最好解仍由原执行器计量和保存。
