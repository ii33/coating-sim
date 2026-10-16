# 膜系参数化设计器

日期：2026-10-12

膜系参数化设计器在“膜系设计”页右侧共用侧栏的“参数设计”区运行。侧栏还包含“表达式”和“设计库”区；设计库会分开显示内置经典模板和已有用户设计。窗口窄于 1030 逻辑像素时侧栏初始折叠为 64 像素功能按钮条；宽度低于 720 像素时展开内容移到层列表下方，按钮改为横向排列，层列表仍保留可用高度。其它宽度下内容和层列表并列。参数表单使用单列滚动布局。生成器按目标真空波长和材料库中的实际折射率生成可检查的物理膜厚候选。预览不会改动设计；只有按下明确标注的“应用并替换当前设计”按钮才会提交到现有编辑会话。候选为单面正面涂层，清空背面并将基片设为 single，保留现有基片厚度；中心参考按半无限基片处理。应用保留当前文件路径，成为未保存草稿；撤销只在保存或其它编辑发生前可用。

当前内置五类参数化结构，另有四条只读公开参考层表，分别显示在“设计库”的参数模板、文献/教程参考设计和用户保存设计区域。参数模板按所选真实n重算厚度；参考层表保留原固定nm，须明确映射真实材料后载入。原名义常数只作来源事实，不伪造材料数据。用户可在“设计库”点击“保存当前设计到库”，刷新查看已保存名称/备注，再“加载用户设计”。当前没有新增用户库删除、版本管理或检索分类功能。

## 操作步骤

1. 选择一个内置经典结构。
2. 输入目标真空波长（nm）。周期堆栈还需输入整数对数 N。
3. 为每个膜层角色选择材料和该材料的光学数据集。选择入射介质和基片；新建窗口默认带入当前设计的这两项，仍可在本次生成中调整。入射介质列表沿用现有介质/膜层/基片材料目录的可选记录，始终按明确选中的材料 ID 和数据集求值，不从名称猜折射率。
4. 若任一所选数据集没有目标波长处的 k 数据，可勾选“对未知 k 假设无损”。此选项只允许 k 缺失或 k 数据波段无法覆盖的情形；膜层/基片已知吸收大于 `1e-9` 会拒绝。入射端口已知k须精确为0，微小非零也拒绝，以符合实际求解器；所有已知k均保留原值。
5. 选择“生成预览”并检查层次、物理厚度、总厚度、使用的数据集、中心参考值、假设和限制。确认后按“应用并替换当前设计”。任何报错、关闭或取消都会保留原设计。

## 内置结构

| 结构 | 膜层角色与默认参数 | 适用提示 | 公开参考 |
| --- | --- | --- | --- |
| 单层 QW AR | `F`；无 pairs | 正入射、透明设计点。MIT 理想示例为 n0=1、ns=3.5、nF=√3.5；未指定实际膜材料或可用波段，运行时只用选择的数据集 | MIT OpenCourseWare, *Fundamentals of Photonics*, [讲义 PDF](https://ocw.mit.edu/courses/6-974-fundamentals-of-photonics-quantum-electronics-spring-2006/98fcc94d2216c26db424e294c28d459a_mirror_inter_thn.pdf) |
| 双层 Q/Q V-coat AR | `F1`, `F2`；无 pairs | 以中心波长的导纳匹配关系构造起点；参考公式未指定运行时材料或材料数据波段，实际表现需仿真 | D. A. Steck, *Classical and Modern Optics*, §10.3.2, [教材页面](https://atomoptics.uoregon.edu/~dsteck/teaching/optics/) |
| Q/H/Q 比较种子 | `F1`, `F2`, `F3`；无 pairs | 只作公开结构示例的可替换材料比较起点；不保证优于双层或达到宽带指标 | COMSOL, [Antireflective Coating Multilayer](https://doc.comsol.com/6.4/doc/com.comsol.help.models.roptics.antireflective_coating_multilayer/antireflective_coating_multilayer.html) |
| `(HL)^N` QW HR | `H`, `L`；默认 `N=8` | 需在目标波长满足真实 `nH > nL`；中心公式假设理想透明层及正常入射。MIT 的理想参考为 800 nm、N=8、nH=2.4/nL=1.45、低折射率端口；不是材料数据带，本模板的 H 起始次序由独立实现指定 | MIT OpenCourseWare §2.3.9 [讲义 PDF](https://ocw.mit.edu/courses/6-974-fundamentals-of-photonics-quantum-electronics-spring-2006/98fcc94d2216c26db424e294c28d459a_mirror_inter_thn.pdf)，Oregon State [Dielectric Mirrors](https://sites.science.oregonstate.edu/~mcintyre/mirrors.html) |
| `(HL)^N C (LH)^N` 半波缺陷 FP | `H`, `L`, `C`；默认 `N=8`、`C=H` | `C` 可另选材料；中心透射 `T0=4n0ns/(n0+ns)^2`，只有 n0=ns 时为 1。Oregon State 示例约 470 nm，玻璃→(HL)² HH (LH)²→空气，TiO2/AlPO；未提供可用数据带，实测性能不作为本目录承诺 | Oregon State [Dielectric Mirrors](https://sites.science.oregonstate.edu/~mcintyre/mirrors.html)，MIT OpenCourseWare [讲义 PDF](https://ocw.mit.edu/courses/6-974-fundamentals-of-photonics-quantum-electronics-spring-2006/98fcc94d2216c26db424e294c28d459a_mirror_inter_thn.pdf) |

Q 表示在目标波长为四分之一波光学厚度。Q/H/Q 条目里的 H 是中间半波层（2Q）；HR/FP 条目里的 H/L 才表示高/低折射率角色。每层使用其所选材料数据集计算

`d = q λ0 / (4 n(λ0))`

并把所得物理厚度（nm）写入设计。仿真扫谱时保持该物理厚度不变，不会在其他波长重新生成四分之一波层。

目录元数据记录公开来源、原始示例条件和独立撰写的公式摘要；示例材料与常数只作为引用出处元数据，不进入材料库数据集或运行时求值回退。只独立结构化数值事实，没有复制教程正文、图像、软件模型、TFD或第三方数据库，也不会将文献示例折射率用作实际材料数据。使用范围、精确公式、近似和验证参考请看 [离线算法文档](algorithms.html) 与 [实现契约](film-design-generator-contract.md)。

## 固定公开参考设计

四条参考为550nm理想单层AR公式的项目固定化、COMSOL MgF₂/CeF₃ Q/Q、COMSOL MgF₂/ZrO₂/CeF₃ Q/H/Q，以及TFCalc/Hulinks 13层红外AR原表。前者是理想基准，不是已发表的真实材料配方；其余三条有明确公开层序/厚度依据。来源目标与名义复算不作为本机工艺或映射后性能保证，详情见[参考库契约](film-design-reference-library-contract.md)。

1. 在“设计库”选择参考条目，查看原入射→基片层序、名义n/k、固定物理nm和来源。
2. 为所有膜材角色、入射介质、基片明确选择实际材料及所属数据集。膜层选择初始为空，不按名称自动猜配。波长固定为原550或10000nm；数据必须覆盖该点。
3. 确认“映射后的设计不同于原名义模型，性能须重新仿真”。未知k另须明确无损假设。参考映射允许膜层/基片真实非负吸收并保留k；入射端口须透明。该规则不改变上面的透明参数模板限制。
4. 预览检查实际n/k、Δn/Δk、固定nm和总厚度。未知k的Δk表示未知，并非实测零。换材料不会重算QW厚度，故通常已不满足原QW/半波条件。
5. 再确认“应用映射结果并替换当前设计”。取消、切换或改映射均不覆盖旧设计。应用沿用一次撤销/dirty；文件与用户库只经既有显式保存操作写入。应用后重新设定有效扫描域、仿真并优化。single/半无限吸收基片的T为进入基片界面的功率，不是整片透射率；A仅为膜层吸收，不包含基片后续体吸收。

