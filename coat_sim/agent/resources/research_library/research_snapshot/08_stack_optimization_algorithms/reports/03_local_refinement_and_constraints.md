# 03 · 固定拓扑的高精度精修

original_url: https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/08_stack_optimization_algorithms/reports/03_local_refinement_and_constraints.md
来源版本：d1acac3766a3a1f0a14064edcbeadb7ea4244f4e。原文中“本轮”“本机”“代码”等指来源研究的范围，不自动代表本软件已实现或已验证。
权利：按用户要求收录项目原创研究文字；来源未声明全库许可证，本导入不改授软件 MIT。第三方原文/图片未内置。
阅读：相对 Markdown 链接改为固定版本在线来源；图片改为原图链接。正文中的源码、数据路径及复算命令仅供阅读，附件与可执行代码未随此快照收录。

主题检索：膜系优化、薄膜设计优化。


## 3.1 默认流程

选择可信起点→冻结材料/目标→无量纲化d→验证J→盒约束TRF或标量L-BFGS-B→独立加密与多角度验收→保留失败指标。代码中`accurate`只是“31点/每band、解析J、最多60次函数评估”的路线标签，绝非已收敛证明；实际success、status、optimality和budget必须一起看。B03的有限预算耗尽即标耗尽。

## 3.2 LM、TRF和Gauss–Newton

在当前x，把残差近似r+Jδ，求min ½||r+Jδ||²。LM形式(JᵀJ+µDᵀD)δ=−Jᵀr，以阻尼抑制病态大步；µ趋小接近Gauss–Newton，µ大时接近缩放梯度方向。实际计算更宜QR/SVD或增广最小二乘，避免显式正规方程把条件数平方。

SciPy `least_squares(method='trf')`是真正有界信赖域反射法，本册没有把它叫“自研LM”。`method='lm'`没有盒约束，不能靠优化完再裁剪修复。上下界内的初始点、单位尺度和残差尺度必须协调；求解器调整微小内点不构成制造余量。[O01](https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/08_stack_optimization_algorithms/sources/summaries/O01.md)

何时用TRF：中小L、残差结构清楚、有d上下界、需要利用J或检测秩亏。残差矩阵大时可使用线性算子Jv/Jᵀv，但薄膜层普遍影响全谱，不能随意伪造稀疏J。只有确有结构零项才可声明稀疏性。

## 3.3 L-BFGS-B

对F=½rᵀr，提供(g=Jᵀr)并保留少量历史(s_k,y_k)近似逆Hessian，在盒约束内搜索。它避免显式大Hessian，适合层数较多或残差无法简洁写成最小二乘的标量目标。线路搜索仍依赖可靠物理值；如果目标含非光滑max或离散材料变化，原有曲率历史应重置。[O02](https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/08_stack_optimization_algorithms/sources/summaries/O02.md)

驻点应检查投影梯度：下界处若g_j>0（下降方向朝不可行负方向）则该分量不阻碍受限驻点；上界处g_j<0同理。原始||g||∞不能替代投影指标。本代码L-BFGS-B记录原始Jac范数供诊断，solver message才描述其停止条件；TRF的optimality来自其缩放指标，两列不可直接横比。

## 3.4 制造约束要进入子问题

盒界dmin≤d≤dmax是必要但不充分；Σd≤Dmax是耦合线性约束，用SLSQP或trust-constr等处理。`solve_constrained`提供真正Σs_j x_j≤Dmax约束及解析导数，结果再独立计算违例；不是加入一个任意大罚系数后就说约束满足。若L dmin>Dmax，先判不可行，不浪费精修。

罚函数或增广Lagrangian可处理非线性应力/温度约束，但惩罚参数、量纲、可行性收敛及物理模型来源必须记录。对不连续材料兼容性约束优先在结构生成时剪枝。d=exp u能保证正厚度，却不能自然保证dmax/总厚度，还可能在薄层处压扁导数；本册用明确边界。

每次结构变更后把单位重新检查：30nm=0.03µm。所有例子中的最小厚度只是人为教学约束，尚无制程可沉积性证据。物理总厚度与光学厚度不是同一成本。[O04](https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/08_stack_optimization_algorithms/sources/summaries/O04.md)

## 3.5 停止和验收是两件事

停止原因至少区分：预算耗尽、ftol、xtol、gtol、用户停止、物理模型错误、线搜索失败、不可行。ftol是“当前目标变化小”；xtol是“步小”；gtol是“某一驻点指标小”。它们均不意味着连续谱达标、鲁棒性达标或全局最优。有限预算下两个梯度实现可能走向略不同的非凸路径，即便单次J差小，也不能预期最后厚度逐位一致。

精修交付必须有：目标版本、训练分数、独立审计分数、峰值违例位置、制造违例、实际调用数和结束原因。对B03追加240次、61点精修仍耗尽预算，则有限首轮停止并留下失败证据；不通过隐藏预算来“跑到看起来收敛”。

## 3.6 预算计数不能偷换

SciPy `nfev`不含数值Jac额外调用。双边差分每次J需要约2L次forward，因此把两方法同为60 nfev写成“相同计算量”是错的。本册同时记录：solver nfev、完整角度/偏振光谱调用数、其中带Jac调用数、Σ(M·L)工作代理、cache命中、墙钟。带Jac调用比纯forward更贵，不能再把二者当等价FLOP。本册是相同求解器主评估上限的比较，实际资源差异全部列出。

时间受到CPU、线程、BLAS、缓存、Python解释器和共享环境影响。求解实例时间为单次观测；核吞吐另做7次取中位，Python分配峰值与进程RSS分别报告。没有GPU或商业求解器性能结论。
