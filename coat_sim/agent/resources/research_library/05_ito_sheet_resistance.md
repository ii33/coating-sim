# ITO 电学：方阻、电阻率、Hall 与几何

版本：2026-10-18.1。原创研究学习资料；算例为合成教学，非实测、非生产配方或资格证明。

## 三个不同的量
电阻 R 的单位是 Ω，受电极与样片形状影响。体电阻率 ρ 的单位是 Ω·m。均匀薄层的方阻 R□=ρ/d，惯用 Ω/□，其中“□”提示几何归一化而非新增物理量纲。[resistivity]

原创教学例：d=120 nm，ρ=2.4×10⁻⁶ Ω·m，则 R□=20 Ω/□。对均匀矩形条，两个理想电极完整覆盖宽度且忽略接触电阻时，R=R□L/W；L/W=3 时为 60 Ω。只改成点接触、电极有间隙或膜有孔洞，这个矩形公式就不够。

## 载流子解释及其限制
单主导载流子近似有 σ=qnμ，ρ=1/σ。仅有方阻和厚度约束的是 nμ 的乘积，不能唯一求出 n 与 μ；Hall 测量可增加独立约束。[hall] 多载流子、非均匀、多层并联及接触问题会破坏简单解释。

若两个均匀导电层共享面内电场、接触足够好，其片电导相加：1/R□总=1/R□1+1/R□2。自设 30 与 60 Ω/□的两层给出 20 Ω/□。这是并联推导，不说明真实 ITO 双层界面必然满足理想接触。

## 测量设计
van der Pauw 关系为 exp(−πRA/R□)+exp(−πRB/R□)=1，要求均匀薄片、无孔、周边足够小的欧姆接触。[hall] 对有孔或大面积电极的成品，应另建几何导电模型，不能套用该式并把误差归到材料。

本资料建议保存厚度位置与方法、接线图、接触质量、温度、磁场/电流极性与原始电压。改变电流和反转极性可帮助检查非欧姆性、偏置和自热，但具体激励应按仪器与样品安全限值确定。NIST 页面是历史存档方法资料，不是 ITO 产品验收标准。

## 公开来源与使用边界

- [hall] NIST, The Hall Effect。https://www.nist.gov/pml/nanoscale-device-characterization-division/popular-links/hall-effect/hall-effect 。已读取公开存档页；支持单载流子解释及 van der Pauw 适用条件；页面已停止更新。
- [resistivity] NIST, Resistivity and Hall Measurements。https://www.nist.gov/pml/nanoscale-device-characterization-division/popular-links/hall-effect/resistivity-and-hall 。已读取公开存档页；支持电阻率/方阻与测量反转检查；不沿用仪器阈值。

本篇正文、推导组织及教学例为本项目新编写；未收录引用作品全文或图片。公开来源用于核验有限命题，不代表其作者认可本软件。
