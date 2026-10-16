# 完整可打开示例 / Complete portable example

打开 **`example/synthetic-demo/project.coatproj`**。这是完整工程，不是零散资源文件。
请先把整个 `synthetic-demo/` 文件夹复制到可写工作目录，再打开其中的 `project.coatproj`，以保留原始示例。
不能只复制 `.coatproj`：`data/` 内的托管文件也是工程的一部分。

## 输入与许可

所有数值均为本项目原创的公开合成教学数据，随仓库根目录的 [MIT LICENSE](../LICENSE) 发布。
没有复制用户私有 `data/`、第三方材料数据库或机器实测数据；运行不需要下载材料。
两种理想材料仅使用 400–700 nm 范围内的常数折射率：膜料 n=1.38、基底 n=1.52，k=0。
它们不是实际材料牌号。几何、源速率、膜厚与优化目标均未实测标定，不承诺生产指标。
工程中不包含结果、缓存、运行队列、owner 锁、恢复文件或旧事务版本。

工程包含：
- 保存的单层膜系（正面 70 nm；1 mm 光学基底）及完整光谱/优化页面配置
- 两个项目内光学材料记录，含数据来源与许可说明
- 点源、半径 10 mm 平面基片、100 mm 源基距离的合成机器
- z=50 mm 的矩形不透明修正板和单层工艺
- 冻结的膜系、材料及源映射来源；工艺使用当前 schema v7
- 默认沉积配置：CPU，21×21 网格，12 时间样本，公转速度为 0

## 操作流程

1. 按主 README 安装依赖并启动应用，在工程打开操作中选择上述 `.coatproj` 文件。
2. 默认入口应为 `Small CPU demo (21 × 21, 12 time samples)`；工艺、机器、源、基片和修正板应全部可加载。
3. 在膜系设计中查看 `Synthetic single-layer coating`。正面只有 `demo-layer`，膜厚 70 nm；材料来自本工程。
4. 打开“光谱仿真”，保留已存配置：400–700 nm、31 点、0°、非偏振，开始计算。
   应看到随波长变化的反射率曲线，R 约为 1.2689%–2.5378%；理想无吸收模型中 R+T≈1。
5. 打开“膜系优化”。配置为固定层序、基线坐标搜索；厚度变量 35–105 nm，目标是 500–600 nm 的 R≤1.3%，
   11 点采样，最多 12 次迭代/40 次评估，种子 0。可运行并查看候选与原始曲线。
   预算结束或未达到目标均是有效演示结果，不是生产验收；优化不会自动改写已冻结的沉积工艺。
6. 打开沉积计算，使用默认配置开始 CPU 计算。应看到非均匀膜厚图与修正板阴影：
   平均厚度约 70 nm，最小 0 nm，最大约 99.0861 nm，网格 21×21，实际时间采样数 12。
   70 nm 是目标均值归一化，不是从已标定源预测的绝对生长量。
7. 如需观察修改的影响，在工作副本中修改板形或膜厚后保存；将新膜系显式导入工艺并保存沉积配置，
   才能使下次打开采用新入口。独立膜系文件与工艺内冻结快照不会自动相互覆盖。

21×21 网格与 12 时间样本仅用于功能演示，不是性能基准，也不是工程收敛证据。
遮挡区的 0 nm 是本示例静止几何设置的预期结果。静止几何使低时间采样即可快速展示阴影，但网格很粗，板边界与均匀性不具备精度保证。
需要研究数值收敛时，应在同一工作副本中加密网格、比较结果；本示例不表示真实行星运动或完整工艺校准。

## 命令行复核

在仓库根目录、已安装项目依赖的 Python 环境中运行：

```sh
python example/verify_example.py
# 也可验证移动后的整个工程：
python example/verify_example.py /path/to/copied-project/project.coatproj
```

此命令通过真实 ProjectStore/default loader 解析所有文档，然后完成沉积及光谱计算，仅将指标输出到终端。
预期 `documents=9`、`default_ready=true`，数值如上；不会把结果写入示例工程。

回归检查（Qt 无显示环境）：

```sh
QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_example_project.py -q -p no:cacheprovider
```

Windows PowerShell 可先设置 `$env:QT_QPA_PLATFORM="offscreen"` 和 `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"`，再运行 Python 命令。
测试包括复制到带空格/中文的新目录、完整依赖闭包、默认入口、真实 CPU/光谱计算和 Qt 页面配置恢复。
还通过真实优化页面构建请求并执行有限预算的优化计算；本次检查在 15 次评估后得到 96.25 nm 候选，
`targets_met=true`（含配置的误差容限）。这只是合成模型上的功能检查，不是实测指标、全局最优或跨平台性能保证。

## 重新生成

```sh
python example/build_example.py /path/to/new-nonexistent-directory
```

生成器使用当前应用的 ProjectStore、ProjectFilmService、工艺导入和页面配置导出 API；不读取全局材料库。
为避免覆盖，目标目录必须不存在。它创建新文档 ID，因此重建后的字节与 ID 可以不同，但物理输入等价。
生成过程只在全新的目标目录内移除自己的旧事务版本，保留最后一次保存的输入。

## English quick start

Copy the entire `synthetic-demo/` directory to a writable location and open its `project.coatproj`.
The default configuration includes a film, two original synthetic constant-index materials, source, machine,
substrate, mask and a film-derived deposition recipe. No external inputs are required.
Run the saved spectrum settings (400–700 nm, 31 samples) and the saved CPU deposition settings (21×21, 12 samples).
Expected reflectance: 1.2689–2.5378%; expected deposited thickness: mean 70 nm, min 0, max about 99.0861 nm.
The mask shadow is intentional. The saved bounded optimization is optional; no convergence or target attainment is promised.
These MIT-licensed mathematical fixtures are uncalibrated educational inputs, not measured materials or production specifications.
Run `python example/verify_example.py` for a read-only numerical smoke check; no generated results are shipped.
