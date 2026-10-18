# ITO蒸镀工艺：从原始实验窗口到可标定模型的证据底稿

original_url: https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/11_transparent_conductive_films/ito/process/report.md
来源版本：d1acac3766a3a1f0a14064edcbeadb7ea4244f4e。原文中“本轮”“本机”“代码”等指来源研究的范围，不自动代表本软件已实现或已验证。
权利：按用户要求收录项目原创研究文字；来源未声明全库许可证，本导入不改授软件 MIT。第三方原文/图片未内置。
阅读：相对 Markdown 链接改为固定版本在线来源；图片改为原图链接。正文中的源码、数据路径及复算命令仅供阅读，附件与可执行代码未随此快照收录。


版本：ito-process-v0.1｜检索与核验：2026-10-03｜边界：蒸发法优先，非完整TCO综述

## 1. 可以采取的结论

1. **ITO蒸镀不存在可脱离设备搬用的单一氧流量/温度配方。** 本版得到6篇可用原始实验全文、1篇有内部矛盾的全文和1份成套机构工程报告，另有1份辅助机构年报；已将设备、氧化方式、厚度、测试口径分开。它们支持建立设备内的局部响应模型，尚不足以建立跨设备的通用定量预测。
2. **把源料、蒸气、到片通量、最终膜组分分开。** “90/10 ITO”必须注明金属合金还是In₂O₃/SnO₂质量配比，不能直接替换成膜中Sn阳离子原子分数。当前核心全文没有足够的源料耗尽—蒸气分馏—膜组成随时间数据，分馏系数不可凭经验填写。[ITO-C04, C09]
3. **先保留完整样品配对再比较性能。** 厚膜低方阻、薄膜高透射、另一片高载流子浓度不能拼成“最优样品”。光学数值还要区分T₅₅₀、400–700 nm均值、谱峰Tmax、相对裸玻璃T。[ITO-C08, C10]
4. **“离子辅助”不是一个可互换的设备状态。** Ebert低能活化氧、End-Hall、带栅离子束、70 V/数百A的HDPE弧源，其电源设置、能谱、通量和热负载含义不同。电源电压不能直接写成到片单能离子eV，放电电流不能直接换成到片离子剂量。[ITO-C05]
5. **退火和增氧都可能存在拐点。** Pokaipisit的500 nm膜空气300°C×1 h改善，但350°C回升；Rao的80 nm低温膜经200°C×4 h空气退火后Rs从65增至1100 Ω/□。两者并不矛盾，起始膜和热历史不同。[ITO-C02]

本报告中的“窗口”均是来源的实测扫描范围或离散样品，**不是保证达标的通用工艺建议**。未做本项目实测。

## 2. 路线与证据完整性

|路线|已核原始证据|能回答什么|不能回答什么|
|---|---|---|---|
|金属合金反应电阻热蒸发|Berkeley 2009完整工程报告C；Jan & Lee 1987原论文仅机构摘要A|舟/坩埚选择实例，氧环境、低速沉积、源漂移线索|Jan全文中的源温、装料量/消耗比例、Sn/In膜成分和批次稳定性|
|氧化物源反应电子束|Pokaipisit 2007完整期刊全文A；George 2000公开摘要/章节片段A|厚度—方阻—透射—粗糙度配对；热处理线索|氧化物各蒸气物种、膜深度组分及源寿命|
|金属源活化反应蒸发|Rao 2004完整期刊全文A|同装置中性氧/活化氧差异，低温成膜与空气退火|实际到片能谱/离子通量、坩埚及基压|
|End-Hall辅助电子束|Pokaipisit 2008/2009与Korneluk 2023完整期刊全文A|氧流量、退火、低温70 nm纳米层与Hall/椭偏测量|完整离子能量分布、到片剂量、刻蚀/再溅射产额|
|高密度等离子体HDPE|Kojima 2012完整期刊全文A|1×1 m玻璃、高速厚膜、预热/厚度与雾度|真实Ts(t)、全片均匀性误差、源到膜成分映射|
|传统PIAD/带栅离子辅助|Laux 1998、Füchsel 2008、Bae 2000仅摘要|研究存在及有限性能指标|具体能量—剂量最优值，Drude/DC校准细节|

A是来源类别，不表示每个机理解释已经验证。本文区分reported、derived、cross_checked、contradicted。表中NR表示本次读取范围未报告；若只读摘要，则不能以NR断言全文没有。

## 3. 装置与条件矩阵

### 3.1 可靠全文的可比较工艺范围

|来源|源料（配比基准）|基片/温度|速率、厚度|压力与气体|激励与几何|
|---|---|---|---|---|---|
|P01 Rao 2004|In+10 wt% Sn，金属|玻璃；58–150°C|0.18–0.30 nm/s；80–160 nm|O₂=2×10⁻⁴ mbar=0.02 Pa；流量/基压NR|Ebert源阴极0.8–1.2 kV，200–250 mA；典型225 mA；旋转架；源片距NR|
|P02 Kojima 2012|烧结In₂O₃+0–15 wt% SnO₂|1×1 m浮法玻璃；预热RT–200°C，成膜继续升温|最高约0.5 μm/min=8.33 nm/s；120–2150 nm|基压约10⁻⁴ Pa；总压约0.2 Pa；Ar35/50 sccm；O₂表1为30–60、图3为65 sccm|LaB₆/Ta空心阴极HDPE；70 V，150–300 A；源片距0.7 m；玻璃负自偏压10–20 V为正文陈述|
|P03 Korneluk 2023|90/10 wt% In₂O₃/SnO₂|φ25 mm石英；25–200°C扫描，选择80°C|方法0.5–10 Å/s；选择1 nm/s；70 nm|中性O₂ 0–8 sccm；plasma 5 sccm；压力原文矛盾，见§7|Lesker PVD75+End-Hall；120–180 V、0.2–0.8 A；W丝15–20 A；旋转架，源片距NR|
|P04 Pokaipisit 2007|90/10 wt%氧化物，99.99%|玻璃150°C|0.2 nm/s；200–700 nm|基压6×10⁻⁶ mbar；正文最佳氧压4×10⁻⁵ mbar；12 sccm|Denton DVB SJ-26C；本文未述离子源/源片距|
|P05 Pokaipisit 2008|90/10 wt%氧化物，99.99%|玻璃150°C|0.2 nm/s；500 nm|基压6×10⁻⁶、蒸发约6×10⁻⁵ mbar；纯O₂ 12 sccm|Denton+Hanil End-Hall；源片距60 cm，离子束与法线45°；离子电压/电流NR|
|P06 Pokaipisit 2009|90/10 wt%氧化物，99.99%|玻璃150°C|0.2 nm/s；120 nm|基压6×10⁻⁶、工压约6×10⁻⁵ mbar；O₂ 8/10/12/14 sccm|Denton+Hanil End-Hall；60 cm，旋转；离子电压/电流及角度NR|

定位：P01 pp.201–203 §2–3；P02 p.174 §2、p.175 Tables 1–2；P03 Methods pp.10–11及Figs.1–6；P04 p.256 Methods；P05 p.363 Methods；P06 p.578 §2。完整字段在apparatus_conditions.json。

**源处理缺口：** 上述6篇均未提供足够的“装料质量—预熔/预蒸—挡板时间—束斑扫描—消耗比例—多炉复用”记录，不能生成可复用的源调理SOP。除HDPE泛称hearth/crucible外，坩埚材质大多NR。George公开方法片段报道粉料800°C×6 h烧结，但这是源料制备，不是坩埚预蒸参数。[P14, Experimental details snippet]

### 3.2 电阻热蒸发的单独工程实例

Berkeley报告：5N In–10 wt% Sn合金置于BN坩埚、W篮加热；基压10⁻⁶ Torr、玻璃175°C、加热50 A、18 Å/min。Table 1氧分压2.5×10⁻⁴ Torr；摘要写0.32 mTorr，二者应并列保留。透射样品40/230 nm；ρ=2.4×10⁻⁴ Ωcm由图形条获得，但与哪片光谱样品对应不清。>95%是除以裸玻璃透射后的相对值。[P08, pp.1–2]

它直接记录W氧化粉状残留，讨论了用BN容器抑制金属爬料，并援引Mo舟作为替代。增氧使沉积速率下降为该报告的观察；“源表面氧化导致下降”为作者解释。热蒸发的源组成漂移援引Jan & Lee，尚未从该原论文全文核验。**因此应把这份报告当装置实例和风险线索，不当普适舟材规范。** [P08, p.1 Film Preparation；p.3 Discussion]

## 4. 原始成套数值：避免把独立最优值拼接

### 4.1 氧流量：Pokaipisit 2009（同一设备/120 nm/150°C/0.2 nm·s⁻¹）

|O₂ sccm|Rs Ω/□|ρ ×10⁻⁴ Ωcm|T₄₀₀–₇₀₀平均 %|RMS nm|Tauc表观带隙 eV|
|---:|---:|---:|---:|---:|---:|
|8|130|15.6|75|0.52|4.15|
|10|74|8.9|82|0.74|4.17|
|12|60|7.2|84|0.93|4.19|
|14|84|10.1|80|0.38|4.16|

[P06, p.578 Table 1；T均值定义p.579] Rs为每片三次读数平均，不是三炉重复；ρ由Rs×厚度推得。没有Hall数据，作者对n和氧空位的解释不能升级为已测n。文首将氧分压说成氧/惰性气流比，量纲不成立；本版采用其明确压力/流量实数并保持分离。此扫描可支持“设备内非单调响应”，不能推导别的腔体也应设12 sccm。

### 4.2 空气退火：Pokaipisit 2008（500 nm；沉积150°C，12 sccm）

|退火温度 °C|保温 h|ρ ×10⁻⁴ Ωcm|T₄₀₀–₇₀₀平均 %|XRD晶粒尺度 nm|
|---:|---:|---:|---:|---:|
|原沉积|0|2.85|60|36.69|
|200|1|2.77|62|39.94|
|250|1|2.49|72|44.63|
|300|1|2.25|83|44.69|
|350|1|5.14|77|46.73|

[P05, p.364 Table 1] 晶粒继续增大而ρ回升，说明“晶粒大→一定更导电”不足以描述该组数据；需同时考虑载流子和散射。未测Hall时不能单独拟合两者。

Rao的原沉积58°C、80 nm、1.8 Å/s样品，Rs=65 Ω/□、Tmax=87%；空气100/150/200°C×4 h后Rs依次188/438/1100 Ω/□，T为87/88/88%。其原沉积65°C、160 nm样品相应Rs为50→68→87→115 Ω/□。两例显示光学变化很小也可能电学严重恶化。[P01, p.203 Table 1；原文未明确这些退火是否在同片上顺序实施]

### 4.3 厚度：Pokaipisit 2007（150°C、12 sccm、0.2 nm/s）

|厚度 nm|Rs Ω/□|ρ由Rs·d推得 ×10⁻⁴ Ωcm|T₄₀₀–₇₀₀平均 %|RMS nm|
|---:|---:|---:|---:|---:|
|200|16|3.20|76.63|1.01|
|400|8|3.20|66.17|2.33|
|500|5.7|2.85|60.23|2.44|
|700|4|2.80|57.89|5.30|

[P04, p.260 Table 1；ρ列为本项目derived] 方阻下降很大一部分来自几何厚度；不可把Rs降4倍直接说成材料电阻率改善4倍。该设备的400 nm以上衍射峰开始明显，不代表所有ITO都在400 nm结晶。[P04, p.257 Fig.2]

### 4.4 HDPE厚度系列：Kojima 2012

7.5 wt% SnO₂源、O₂ 40 sccm、不预热。测量为Hall与四探针；T是550 nm单点。[P02, p.175 Table 2]

|厚度 nm|Rs Ω/□|ρ ×10⁻⁴ Ωcm|n ×10²⁰ cm⁻³|μ cm²/(V·s)|T₅₅₀ %|
|---:|---:|---:|---:|---:|---:|
|120|61|7.4|2.2|38|82.0|
|640|7.8|5.0|2.4|53|79.1|
|950|4.4|4.2|4.1|36|75.2|
|2150|1.7|3.6|7.4|23|61.2|

最低Rs=1.7 Ω/□这片的T仅61.2%；不能把它和别处的高透射一起宣称。正文180°C、4/7.5 wt%源得到ρ最低1.7×10⁻⁴ Ωcm是另一组条件。7.5 wt%源约125°C预热出现雾度峰，关联混合非晶/晶态；180°C样品雾度又降低。持续等离子体加热使“无预热厚度序列”同时包含热历史变化。[P02, Figs.5–8]

另有70°C预热、250 A/70 V的氧流量表：60/50/40/30 sccm对应ρ=6.4/6.1/3.7/2.8×10⁻⁴ Ωcm；对应厚度1.12/0.955/1.06/1.01 μm；T₅₅₀=76.2/79.1/77.2/76.1%。Table 1未明示源SnO₂含量，不能从邻图自动代填。[P02, p.175 Table 1]

### 4.5 低温70 nm膜：Korneluk 2023

这是本版工艺—表征桥接最完整的一篇：80°C、1 nm/s、5 sccm氧等离子体，扫描放电电压和电流；70 nm石英基片膜。150 V/0.4 A样品图示Rs=90 Ω/□，按标称厚度推得ρ=6.3×10⁻⁴ Ωcm。原文摘要还报告全研究最小ρ=5.2×10⁻⁴ Ωcm、最高n=1.2×10²¹ cm⁻³、RMS<1 nm及85%透射，**这些极值不在本版自动联结为同一片**。[P03, Figs.3–6、结论]

180 V时电学恶化；电流/粗糙度也不是单调。Hall在23°C、0.515 T、100 μA条件下测量；椭偏使用Drude+Tauc–Lorentz+Gaussian并加入透射约束，不能说作者仅用单Drude覆盖全光谱。每组设置3次独立沉积、各3片，是比单点读数更有价值的重复性设计。[P03, Methods]

## 5. 源组分、氧与轰击：进入模型前的变量定义

### 5.1 质量分数换成阳离子原子分数，只做计量换算

对氧化物混合源，w为SnO₂质量分数：

x_Sn,cation = (w/M_SnO2) / [(w/M_SnO2) + 2(1−w)/M_In2O3]

按M_In₂O₃=277.633、M_SnO₂=150.708 g/mol：95/5、90/10、80/20氧化物质量配比分别约4.624%、9.284%、18.717% Sn阳离子原子分数。金属In/Sn=90/10 wt%则约9.704%。这里**均为源料计量值，非成膜值**；“全原子at%”还包含O，分母不同。推导采用标准原子量近似、保留有效位数，不表示源料分析精度。[ITO-C04, derived]

Yamaguchi公开片段以95/5 wt%氧化物源做实验，报告气氛与离子照射影响XPS的Sn⁴⁺组分。它说明需要测膜，但不提供本版可用的源→总Sn/In传递系数。Jan1987机构摘要的Sn:In金属质量比3:1更不能读成“膜中75% Sn”；蒸发分馏和投料口径必须读全文才能判断。[P13, abstract/conclusion snippet；P09, institutional abstract]

### 5.2 氧流量与氧到达通量分开

平衡中性气体的表面碰撞通量可作为低阶参照：Γ_O2 = p_O2 / √(2πm_O2k_BT_g)。p必须为分压、T_g为气体温度；sccm是标准状态体积流量，缺有效抽速、温度、气体分布、离解/反应消耗时不能唯一换成分压或活性氧通量。原子氧、分子离子、快中性粒子需额外物种项。

建议模型把氧供给拆为：中性分子供给、活性物种份额、表面反应/再蒸发及氧消耗。用实测pO₂/残余气分析加表面组成来校准，不直接以“某篇12 sccm”作为物理边界条件。此处为物理量定义和待验证建模建议，不是对当前论文的拟合结果。

### 5.3 离子参数至少保留四层

- 控制量：放电电压、电流、气流、离子源型式和角度
- 到片量：能量分布f(E)、电流密度j_i(x,t)、离子/中性物种比例
- 积分量：离子剂量D_i=∫Γ_i dt；能量剂量D_E=∫Γ_i〈E〉dt
- 生长竞争量：离子/沉积原子通量比Γ_i/Γ_dep、每沉积原子能量Γ_i〈E〉/Γ_dep

P02的体等离子体密度10¹³ ions/cm³不是流量；P01的阴极kV不是其低能氧到片eV；P03放电V/A缺到片能谱；P06甚至没有提供离子源电压/电流。因此本版本**不拟合统一“离子能量—ρ”曲线**。[P01 §2；P02 §3.1/3.4；P03 Methods；P06 §2]

## 6. 可执行的最小验证方案（待验证建议）

### 6.1 先校准记录，不先寻全局最优

1. 给每炉、源料批号和每片建立唯一ID，记录起止装料质量/使用次数、源料形式和配比基准；QCM工具因子用独立台阶厚度校准
2. 记录p_total(t)、p_O₂(t)及残余H₂O，实际Ts(t)，速率r(t)、累计厚度、挡板/束斑/气流；只记录加热设定值不能反演真实热历史
3. 每炉中心/边缘见证片同时做厚度、四探针地图、T/R光谱；选代表样品做Hall、XRD、AFM和带误差的膜Sn/In/O分析
4. 有离子源时先测片位离子电流密度及能谱；不能测时将V/I标作设备分类变量，禁止跨源直接外推

### 6.2 分阶段试验及放行标准

- **源稳定性阶段：** 以固定设备设置、分时挡板见证片测试早/中/晚段膜Sn/In与r(t)、n/μ漂移；至少包含独立装料重复。分清耗尽、源氧化与传感器漂移
- **氧—速率阶段：** 固定厚度与可测Ts(t)，二维氧/速率扫描；目标同时监控T、ρ、AFM，不仅追最低Rs。各个条件需重复批次，随机/区组化顺序防止源寿命与条件混淆
- **温度/退火阶段：** 同一沉积母批切片，分气氛/温度/时间处理；采集n和μ来区分载流子变化与迁移率变化；升降温程序和总热预算必须记录
- **模型放行：** 保留独立测试批次；预测误差需分别按膜厚、ρ、光谱T/R、n/μ及均匀性统计。没有真实设备数据前，文献输入仅是先验与实验设计范围

适用/安全边界：须由合格人员按实际真空、电源、氧气、源料与设备说明审核；这里不是高压电源、热源或氧气系统的现场操作SOP。

## 7. 数据质量与尚未解决的原文问题

### 7.1 两个必须隔离的问题

- **P03压力疑点：** 原文写起始5×10⁻⁵ Torr，随后“pressure increasing to 2×10⁻⁵ Torr”。出版社HTML/PDF和Undermind复读一致，数值/方向冲突；本版不擅改指数，不把其当已知氧分压。它仍可支撑设置扫描和表征方法，但不能支撑精确气体输运边界。[ITO-C06]
- **P07 Far：** 正文标2012、在线元数据2022；方法称氧化物90/10、结论又称金属90In–10Sn；Table1的25 sccm/140 nm/Rs600 Ω/□应给ρ=8.4×10⁻³ Ωcm，却表报1.65×10⁻²、摘要/结论报1.75×10⁻²。其数值不能用于拟合或宣称最佳工艺，保存为可审查的反例。[ITO-C07]

### 7.2 优先取得的关键论文全文

|优先级|题名及DOI|已查合法入口/结果|全文要解决的问题|
|---|---|---|---|
|高|Jan & Lee (1987), Preparation and Characterization…Direct Thermal Evaporation…；10.1149/1.2100819|台大机构库只有摘要；DOI入口读取失败；尚无合法全文|Sn:In=3:1的实际投料/分馏，Mo舟温度、装料量、消耗比例、膜Sn/In与稳定速率|
|高|Laux et al. (1998), Room-temperature deposition…plasma ion-assisted evaporation；10.1016/S0040-6090(98)00861-X|ScienceDirect摘要；题名/DOI/机构限定检索未发现可读全文|APS设置、压力、速率、氧响应、300 nm膜的ρ及A₅₅₀对照|
|高|Füchsel et al. (2008), Low temperature deposition…plasma ion-assisted evaporation；10.1364/AO.47.00C297|Optica明确无订阅访问；Fraunhofer PUBLICA记录遇bot页；官方年报可读但非论文|平均离子能量、活性气体、温升、Drude与四探针偏差及膜结构|
|中|Bae et al. (2000), Effects of oxygen ion beam plasma conditions…；10.1016/S0042-207X(99)00169-4|SKKU机构摘要；未取得全文|900 V究竟哪级电极设置、到片能谱/流密、厚度和非单调氧通量曲线|
|中|Yamaguchi et al. (2004), Characteristics…electron beam evaporation；10.1016/j.tsf.2003.09.033|ScienceDirect摘要+方法/结论片段|表中温度/压力及ECR能量；Sn⁴⁺定量和总Sn/In是否分离|

摘要只支持其明确写出的事实：例如Laux摘要报告室温、300 nm、ρ=5×10⁻⁶ Ωm=5×10⁻⁴ Ωcm且A₅₅₀<5%；没有其原始实验表不能把未见到的设置补齐。Füchsel官方年报2008 pp.74–75另报道<100°C、非晶膜ρ约4 μΩm和平均k≈7×10⁻³，但气氛/平均波段/厚度未给齐，不能替代期刊全文。[P10 abstract；年报见sources.json新增记录]

### 7.3 不是靠补PDF就能消除的缺口

- 源料真实批次、几何/泵速、温度与厚度传感器校准
- 膜Sn/In/O的时间/深度分布；氧空位电活性与Sn施主补偿不能仅由XPS O1s峰拟合唯一识别
- 到片离子能谱、流密、物种和再溅射；中性活性氧份额
- 实际基片热曲线、附着/应力、长期空气稳定性与湿热循环
- 一致的T/R归一化、积分球雾度及独立批次不确定度

## 8. 文件、复核与使用方式

- `sources.json`：逐条作者/年份/DOI/访问级别/版权/缺口。摘要、原始全文、机构工程报告严格分层
- `apparatus_conditions.json`：装置及未报告字段；不从别篇自动补填
- `evidence_ledger.json`：命题链与30条成套观测；`evidence_ledger.csv`为观测宽表，`claims.csv`为命题表
- 带`derived`的字段由明确公式推导，不是作者直接测量；原始reported值保留，不因推导差异覆盖
- 本地只保存已确认CC BY 4.0的Korneluk原文，登记SHA-256。**所有第三方原文均排除出对外发布/报告打包**；全文可读但权利未确认的其他PDF未留存
- 本版未开展新实验、未进行统计拟合、未声称validated_in_scope。正文数据来源定位保留；网络缓存截图失败处采用完整PDF文字和可用Undermind全文交叉核验，未声称已逐图手工数字化

## 参考文献

以下书目信息与稳定链接均在本次原论文/出版社/机构库核验。完整访问记录见sources.json。

- [ITO-P01] K. Narasimha Rao. Optical and electrical properties of indium-tin oxide films. Indian Journal of Pure & Applied Physics 42:201–204, 2004. [来源](https://nopr.niscpr.res.in/bitstream/123456789/9573/1/IJPAP%2042(3)%20201-204.pdf). 访问：2026-10-03；fulltext_web_pdf

- [ITO-P02] Hiroyasu Kojima, Nagahiro Saito, Osamu Takai. Effects of Substrate Temperature on Properties of Tin-Doped Indium Oxide Films Deposited by Activated Electron Beam Evaporation. Journal of the Surface Finishing Society of Japan 63(3):173–178, 2012. DOI: [10.4139/sfj.63.173](https://doi.org/10.4139/sfj.63.173). 访问：2026-10-03；fulltext_web_pdf_and_undermind

- [ITO-P03] Alexander Korneluk, Julia Szymczak, Tomasz Stefaniuk. Annealing-free fabrication of high-quality indium tin oxide films for free-carrier-based hybrid metal–semiconductor nanophotonics. Scientific Reports 13:18520, 2023. DOI: [10.1038/s41598-023-45651-w](https://doi.org/10.1038/s41598-023-45651-w). 访问：2026-10-03；fulltext_publisher_and_undermind

- [ITO-P04] Artorn Pokaipisit, Mati Horprathum, Pichet Limsuwan. Effect of Films Thickness on the Properties of ITO Thin Films Prepared by Electron Beam Evaporation. Kasetsart J. (Nat. Sci.) 41:255–261, 2007. [来源](https://www.thaiscience.info/Journals/Article/TKJN/10471521.pdf). 访问：2026-10-03；fulltext_web_pdf

- [ITO-P05] Artorn Pokaipisit, Mati Horprathum, Pichet Limsuwan. Influence of Annealing Temperature on the Properties of ITO Films Prepared by Electron Beam Evaporation and Ion-Assisted Deposition. Kasetsart J. (Nat. Sci.) 42(5):362–366, 2008. [来源](https://li01.tci-thaijo.org/index.php/anres/article/view/244621). 访问：2026-10-03；fulltext_web_pdf

- [ITO-P06] Artorn Pokaipisit, Mati Horprathum, Pichet Limsuwan. Influence of oxygen flow rate on properties of indium tin oxide thin films prepared by ion-assisted electron beam evaporation. Songklanakarin J. Sci. Technol. 31(5):577–581, 2009. [来源](https://sjst.psu.ac.th/journal/31-5/0125-3395-31-5-577-581.pdf). 访问：2026-10-03；fulltext_web_pdf

- [ITO-P07] Mansour Suker Farhan. Electrical and Optical properties of ITO Films Prepared by Ion-Assisted Deposition at Ambient Substrate Temperatures. Wasit Journal for Science & Medicine 5(1):156–164, 2012 in PDF; 2022 in online metadata. DOI: [10.31185/jwsm.180](https://doi.org/10.31185/jwsm.180). 访问：2026-10-03；fulltext_undermind

- [ITO-P08] D. R. Queen, J. Morford. Transparent indium tin oxide films prepared by reactive thermal evaporation in the U.C. Berkeley Microlab. UC Berkeley Microfabrication Laboratory process report, 2009-02-25, 2009. [来源](https://nanolab.berkeley.edu/public/process/reports/ITO.pdf). 访问：2026-10-03；fulltext_institutional_pdf

- [ITO-P09] S.-W. Jan, S.-C. Lee. Preparation and Characterization of Indium-Tin-Oxide Deposited by Direct Thermal Evaporation of Metal Indium and Tin. Journal of The Electrochemical Society 134(8):2056–2061, 1987. DOI: [10.1149/1.2100819](https://doi.org/10.1149/1.2100819). 访问：2026-10-03；abstract_institutional_only

- [ITO-P10] S. Laux, N. Kaiser, A. Zöller, R. Götzelmann, H. Lauth, H. Bernitzki. Room-temperature deposition of indium tin oxide thin films with plasma ion-assisted evaporation. Thin Solid Films 335(1–2):1–5, 1998. DOI: [10.1016/S0040-6090(98)00861-X](https://doi.org/10.1016/S0040-6090(98)00861-X). 访问：2026-10-03；publisher_abstract_only

- [ITO-P11] Kevin Füchsel, Ulrike Schulz, Norbert Kaiser, Andreas Tünnermann. Low temperature deposition of indium tin oxide films by plasma ion-assisted evaporation. Applied Optics 47:C297–C302, 2008. DOI: [10.1364/AO.47.00C297](https://doi.org/10.1364/AO.47.00C297). 访问：2026-10-03；publisher_abstract_only

- [ITO-P12] J. W. Bae, H. J. Kim, J. S. Kim, N. E. Lee, G. Y. Yeom. Effects of oxygen ion beam plasma conditions on the properties of indium tin oxide thin films. Vacuum 56(1):77–81, 2000. DOI: [10.1016/S0042-207X(99)00169-4](https://doi.org/10.1016/S0042-207X(99)00169-4). 访问：2026-10-03；institutional_abstract_only

- [ITO-P13] Mika Yamaguchi, Ari Ide-Ektessabi, Hiroshi Nomura, Nobuto Yasui. Characteristics of indium tin oxide thin films prepared using electron beam evaporation. Thin Solid Films 447–448:115–118, 2004. DOI: [10.1016/j.tsf.2003.09.033](https://doi.org/10.1016/j.tsf.2003.09.033). 访问：2026-10-03；publisher_abstract_and_section_snippets

- [ITO-P14] Joseph George, C. S. Menon. Electrical and optical properties of electron beam evaporated ITO thin films. Surface and Coatings Technology 132(1):45–48, 2000. DOI: [10.1016/S0257-8972(00)00726-X](https://doi.org/10.1016/S0257-8972(00)00726-X). 访问：2026-10-03；publisher_abstract_and_section_snippets

- [ITO-P15] Kevin Füchsel, Ulrike Schulz, Norbert Kaiser. Low temperature deposition of indium tin oxide. Fraunhofer IOF Annual Report 2008, pp.74–75, 2008. [来源](https://www.iof.fraunhofer.de/content/dam/iof/en/documents/publications/annual-report/2008/iof_jahresbericht_2008_10_fuechsel.pdf). 访问：2026-10-03；fulltext_institutional_pdf
