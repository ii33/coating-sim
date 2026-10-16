# 临界邻域稳定候选与100位独立对照

2026-10-13。仅用于隔离验证，原 `drjit_coherent_tmm.py`、`gpu_tmm_probe.py` 与生产路径均不替换。新增实现为 `tools/validation/drjit_tmm_stable_candidate.py`；独立oracle为 `tmm_boundary_oracle.py`；比较入口 `check_tmm_boundary_oracle.py`；测试 `tests/test_gpu_tmm_boundary_oracle.py`。

## 已证实的失败分账

真实 CUDA 裸界面 `[1.5,1]`、beta=1：FP64三偏振均通过，R相对CPU绝对差2.22e-16、T=0；FP32三偏振均为 `[1.0000002384185791,0,-2.384185791015625e-7]`，绝对误差初筛通过但生产被动下限失败。它符合Dr.Jit复除 `z0*conj(z1)/|z1|²` 的FP32舍入次序预测，不是FP64裸界面失败，也不能用balance=1隐藏。

另外，原 FP64全suite在 `synthetic_internal_critical_nextafter_d0_p` 失败。零厚度 `[1.5,1,1.5]` 物理上就是 `[1.5,1.5]` 裸界面，真值R=0/T=1/A=0。beta下侧nextafter时，普通递推中p接口r≈±0.999999940024，组合分母≈1.199520265e-7，复除/FMA的几ULP差异可被约8.3e6放大。生产CPU同输入已偏离真值2.53e-10，不能只靠GPU与该递推互证。远端d0_p完整原始值仍需保存核验，不据猜测重写原版结果。

## 候选算法契约

输入仍为等层数批次的波长nm、被动复n、共享厚度nm、真实beta、偏振；输出前五项保持原探针API，新增内部近临界选择mask和小delta级数层-lane计数。

1. 只将原始 **d==0** 的内部介质精确消去，端口保留。厚度为正但很小、或phase≈1均不能当零层。这是批次共享膜系的静态准备，不是逐光线CPU解算。
2. 内部若任意 `|q|<=1e-3*max(1,|n|)`，选全设备归一化E/H场传播；其他lane仍走原被动散射递推。该1e-3阈值是未批准的**本项目候选约定**，不是物理分界或认证精度阈值。
3. 令 `length=2*pi*d/lambda`，`delta=q*length`，`P=exp(i*delta)`。当 `|delta|<=1e-3` 时：
   - `sinc(delta)=1-delta²/6+delta⁴/120-delta⁶/5040+delta⁸/362880`
   - `off/q=-i*P*length*sinc(delta)`
   - `diagonal=P*cos(delta)`
   - s：upper=off/q，lower=(off/q)*q²
   - p：upper=(off/q)*q²/n²，lower=(off/q)*n²
4. 小delta掩码外，传给cos及级数的delta先置零，避免未选中分支仍计算大虚部cos而出现0*inf。大delta保留衰减安全的 `off=(1-P²)/2`、`diagonal=(1+P²)/2`；不形成增长指数。
5. 每层按E/H最大模归一，保留独立传播幅度因子；q=0自然得到s/p解析极限。FastMath关闭，仍不clip功率、放宽生产门槛或抹去真实吸收。

```text
exact d=0 layers -> exact identity elimination -> regular device recurrence
nonzero layers -> per-lane near-q mask -> normalized field propagation
                                      small delta: guarded sinc/cos
                                      other delta: passive phase/off
```

图示：敏感分支仍在GPU；不发生每条光线CPU roundtrip。候选不处理所有FP32输入/功率舍入问题，也不代表完整GPU BSDF或整单光追速度验证。

## 独立oracle与本机证明

oracle使用已有可选mpmath、100十进制位，输入float转换保留精确binary64 nextafter，pi为高精度；使用E/H正弦/sinc解析传播，不复制候选阈值/截断级数，也不调用生产递推。它是验证oracle，不能用于性能对照。mpmath缺失时测试明确importorskip，不自动安装。

固定15组：0/100/270nm层的beta nextafter上下侧与exact，405/550/830nm，全偏振；另有内部临界+1e6nm吸收层及裸出口临界。NumPy方程适配器验证候选，不能冒称CUDA通过：原递推对oracle的2e-12绝对误差/生产物理边界gate有9组失败，候选0组失败、最大绝对差5.55e-16；厚吸收在浮点over/invalid/divide设为raise时通过。真实CUDA候选尚待运行。

```bash
python -m pytest tests/test_gpu_tmm_boundary_oracle.py -q
python tools/validation/check_tmm_boundary_oracle.py --backend numpy --output /tmp/tmm-boundary-proof.json
```

remote应保留原版，再运行新候选比较：

```bash
python tools/validation/check_tmm_boundary_oracle.py --backend cuda --precision float64 --case internal_nextafter --output /tmp/tmm-boundary-cuda.json
```

若remote无mpmath，不安装，传入本机预计算的已验证proof JSON：

```bash
python tools/validation/check_tmm_boundary_oracle.py --backend cuda --precision float64 --oracle-fixture /absolute/path/boundary-cpu-proof.json --output /tmp/tmm-boundary-cuda.json
```

fixture检查oracle源码SHA256、100位标记、每项确切输入hash和shape；原始输入与R/T/A保存在同名NPZ，JSON保留每项原版/候选/oracle/CPU数值、mask计数和真正JIT backend history。这里只能证明列出的边界，不能代替63项、真实60层材料、窄共振或全渲染验收。

参考：[Dr.Jit v1.4.0 complex实现](https://raw.githubusercontent.com/mitsuba-renderer/drjit/v1.4.0/include/drjit/complex.h)、[被动多层薄膜公式与分支](https://arxiv.org/abs/1603.02720)。上述切换参数、级数阶数、测试误差门限均是本项目约定。
