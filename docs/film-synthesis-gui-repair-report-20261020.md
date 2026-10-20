# 膜系 GUI 输入与用料修复验收

日期：2026-10-20。基线：当前工作目录 `C:\Users\youai\Documents\coating-dev\coating-sim`，HEAD `0c1c3e0671929668d43d8e56f8ccb90d0af1b6d2` 加本轮开始时的未提交文件。`artifacts/film_synthesis_20261020/gui-repair-20261020/before.json`记录逐源 SHA、原有未提交状态及上一阶段冻结清单校验；没有从历史副本合并或覆盖。旧阶段的 514/464 项验收只作历史记录。本轮不修改 TMM、角度网格或搜索策略，不降低默认预算，不推送、不发布。

## 三项修复与根因

1. **角度整组草稿**：原来两个边界分别提交，下限先从 0 改到 12.5 时仍与旧上限 0 比较；失败后的显示同步覆盖正在输入的值。现由 `FilmOptimizationAngleRangeEditor` 保留原文，通过 controller 的 `noteObjectiveAngleDraft` 登记整组，`confirmObjectiveAngleRange` 原子校验下限、上限、步长。Enter、失焦、开始快捷键、导出及关闭共同消费该组。0/0 和非零等上下限均保留为单角度；空白、`1e`、反向范围显示错误并保留文本，不产生任务。原生基线实际复现的是非零下限先改被回退；基线 0/0 两项本来已通过，未虚报它们是修复前失败。
2. **结构用料术语及两材料起点**：原“角色”是结构段引用的用料代号，例如高折射率、低折射率、腔层用途，代号唯一不等于材料必须各不相同。旧 GUI 无条件创建并校验 H/L/C 三个绑定，把尚未使用的 C 也列成必填，因此呈现出强制第三材料的行为。现在默认仅 H/L；普通腔层复用 H，只把结构实际引用的代号提交。独立腔层可显式使用 C，旧配置仍兼容；多个用途可指向同一材料/数据集。核心 `nH > nL`、已知 k 及波长域约束保持，复用并不放宽物理合法性。
3. **从零合成动态候选行**：controller 唯一拥有有序 `{id, material_id, optical_set_id}`。按稳定行 ID 增删、换材、换数据集和排序；材料变化只更新该行的真实数据集。空列表、重复材料/数据集对、已删除资源均显式拒绝。`synthesis.material_rows` 为配置可选扩展，旧选择可迁移，行 ID 不进入物理请求 wire。外部材料目录更新后第一次点击能打开当前同 ID 下拉框，正常第二次点击能关闭。最终截图复核还发现长名称撑宽行并挤出删除/排序按钮，已纳入同一处布局修复与实际命中区域验证；可选预算的 null/undefined 均显示空白。

新界面准确文字为“高折射率膜料（H）”“低折射率膜料（L）”“独立腔层用料（C）”，额外代号显示“附加用料（M1）”；操作为“添加独立用料”。段字段使用“本层用料”“靠近基片的首层用料”“末端匹配层用料（可不加）”。提示说明“用途代号不是不同材料的数量要求”。波长和物理厚度标 nm，光学厚度标无量纲 n·d/λ，相位标 rad，并解释 0.25/0.5 对应四分之一波/半波。候选行区域为“候选膜料与光学数据集”，每行分别选择膜料和该膜料的数据集。

## 代码与契约

- 角度及行状态：`coat_sim/ui/film_optimization_quick_controller.py`；页面与角度控件：`coat_sim/ui/qml/FilmOptimizationWindow.qml`、`FilmOptimizationAngleRangeEditor.qml`。
- 用料表单及适配：`coat_sim/core/film_physical_generator.py`、`coat_sim/ui/film_quick_controller.py`、`FilmPhysicalGeneratorForm.qml`、`FilmPhysicalFields.qml`、`FilmPhysicalSectionEditor.qml`。该核心文件提供 GUI 独立表单适配，厚度公式及物理结构核心不变。
- 契约与操作：`film-synthesis-gui-contract.md`、`film-design-generator-contract.md`、`film-synthesis-gui-guide.md`；`algorithms.html` 14u 同步说明 GUI 默认及配置边界，保留离线单文件。
- 实现与测试由实际 gpt-6.1-sol/root 执行；路由核对由 Luna 执行。分工独立的 GPT 测试复核不计为指定 Seed 审查。

## 验证证据

本节最终数字与源版本绑定由 `artifacts/film_synthesis_20261020/gui-repair-20261020/gui-repair-freeze-manifest.json` 及 `artifacts/film_synthesis_20261020/gui-repair-20261020/final/` 中的 JSON/JUnit 记录。原生交互使用独立进程内、仅属测试的 Qt 窗口及 QTest 键鼠事件；并非 sky/系统桌面自动化，不操作用户窗口。

- 25 模块回归：268 passed，13 subtests passed，210.76 秒；逐源 SHA 在该次执行前后稳定。此集在最终长名布局收口前完成；冻结清单逐项列出其与最终源的差异，收口后只重跑受影响的原生交互及定向检查，不声称全库重新验收。覆盖草稿、旧模式、参数表单、配置、队列、工程边界及请求冻结。
- 原生交互最终结果：22 passed，101.84 秒；966 源在执行前后稳定，与最终冻结源逐项一致。119 张 1100×760 原生截图及逐例索引保存在 `final/native-final/`；已查看角度、动态行和结构表单代表截图。覆盖逐字输入、Enter/失焦、0°/非零等上下限、非法草稿拒绝、真实任务参数、两膜料/非默认数据集、预览确认应用撤销、动态增删与排序、实际 `.cfilm` 保存重开、目录联动及已冻结 n 值。根日志保留曾出现的长名按钮不可点击失败，不将其删除或写成测试驱动错误。
- 完整锁定静态扫描：966 源；新增基线诊断 40、resolved 222、fatal 0、exit 1，完整门禁未通过。40 项路径全部在本轮开始以来未变更；本轮变更源码新增诊断为 0。基线、Ruff/mypy 配置、锁文件和三个产品启动入口 SHA 未变。最后布局修改另作定向 QML 检查，不因模型待授权重复全库扫描。
- 最后长名布局收口：owner 3 passed（4.54 秒），合法 120 字材料/数据集名下 ×/上下移实际点击与每层裁剪祖先内命中区均通过；6 个空预算文本为空，None 默认不变。最终 owner 锁定 Ruff/mypy/QML 的本轮新增诊断为 0，源码稳定；分工测试复核对动态行补充命中区断言，Ruff 通过，本文件 mypy 诊断为 0（依赖导入诊断另外记录，不当成完整门禁通过）。
- 物理表单 owner 定向集：50 passed、5 subtests；新旧两组原生交互各自记录，不将重叠用例累加为独立测试总数。所有有限候选审计保留 `continuous_certificate=false`。

## 新上传截图：ZnS 基片缺少 k

已通过 Library `prepare_materialize` 正式获取 `libfile_0c0b4b8f29e88191a328c915db324a51`，按返回的临时传输地址下载到当前 Windows 执行环境并查看像素。解析得到 file ID `file_00000000536081fd8eadda109e8b5925`，41581 字节，SHA256 `c7a3558053cd0b94235cb2112ed0ad35ccf20b20189c268d4337aa31ea5176c7`；来源 sidecar 记录 Library 版本 0，未将临时 URL 持久化。

截图可见 R 目标 0–1%、权重 1、达标容限 0.1 个百分点；红字指向基片 `builtin_research_ZnS_substrate` / `ri_zns_debenham_n` 在 600 nm 的 n/k 校验，目标区间 `[600, 900] nm`。当前真实目录中，该数据集名为“Debenham-Klein/块材20°C/1986”，n 有效域 0.405–13 µm（405–13000 nm），`k: null`，备注明确“仅n；k未知，不自动补零”。600–900 nm 位于 n 域内，缺失项为 k。`film_auto_synthesis.py` 的既有严格校验因此拒绝构建请求；本轮角度及动态行修复不会补造该物理输入。该基片为校验的首个拒绝点，截图不能证明其他候选膜料的数据已全部通过。继续使用该基片时，需在基片光学数据设置中导入覆盖目标区间且具有可靠明确 k 的实际基片数据。保留用户要求的未知 k 拒绝规则，未用薄膜样品数据冒充基片数据，未自行改用户设计。

## 启动专项：相同警告复现与合法输入实际完成

第二张截图 `libfile_5a794f186e5c8191a98542e94d5fdd16` 也已正式物化并逐像素查看；其解析 file ID 为 `file_00000000384481f587616dc00672a7da`，41581 字节，与第一张 SHA256 完全相同，警告原文相同。两张均已成功读取。

专项不修改生产或已冻结测试源，证据位于 `artifacts/gui-repair-20261020/startup-validation/`。先以当前真实 ZnS 基片记录、N=0、0°及 600–900 nm/R0–1%/0.1 个百分点容限，在自有原生 Qt 窗口实际鼠标点击开始，精确复现截图原文；新 job 数为 0、源设计和 ZnS 原记录未变。只读链路及 600/750/900 nm 的实际 n/k 取样见 `input-chain.json`。

随后通过现有膜系设计的基片 UI 搜索并改选明确标为 synthetic 的 n=1.5、k=0 基片及数据集（覆盖 450–1000 nm），重新打开绑定该私有设计的优化页。候选膜料同样明确 synthetic、n=1.23/k=0；目标仍为 600–900 nm、R0–1%、权重1、0°，采样步长100 nm（4点）。实际鼠标点击开始提交 job `1d2f294505114b42bf2b85cb7eab8e88`，观察 `running → succeeded`；结果 `completed / validated_targets_met`。

- 冻结 spec 核实前/背面均0层、无附加seed、角度 `[0.0]`，候选为 `acceptance-film/film-set`，基片为 `startup-review-glass/startup-glass-nk`。
- 最终1层，厚度152.4390243902439 nm；1轮、5次评价，验证评价1次、失败评价0；有限审计物理调用12次、唯一波长点23个。
- 训练4点及独立有限审计23点的最坏 R 均为0.006080392345633822，即0.6080392346%；最大违规0、审计达标。`continuous_certificate=false`，不声称连续谱证书。
- 该快速任务只观察到心跳序号0；未请求取消，未自动应用，源设计未替换。长任务心跳/取消仍以此前22项原生用例的专项覆盖为准，不用本快速任务代替长程验证。

完整原始 spec、结果、状态序列及 `startup-result.json` 已留档；`01-blocked-unknown-k.png` 与 `03-legal-start-result.png` 已由 root 查看实际像素，结果窗口可见完成、达标及层表。该标准算例证明启动/队列/TMM/审计链路可用；用户当前 ZnS 数据缺 k 的问题仍需可靠实际基片数据，未用 synthetic 数据冒充或改写用户工程。

## 指定模型流程阻塞（与工程完成状态分开）

唯一被拒绝的动作是 `opencode-cli.exe run --model opencode-go/deepseek-v4.1-flash --agent plan --format json <prompt>`，目标为本机配置的外部 `opencode-go/deepseek-v4.1-flash` 提供商路由；实际服务端点和可调用性未验证。自动审批在进程启动前拒绝，没有 PID、请求/响应、usage，也没有实际向模型发送内容。review_id 与 request_id 均为 null。没有重试、改写提示或借其他进程绕过。

原始提示会包含此前 GUI 集成的三项内部边界和拟议方案：同项目冻结候选预览；历史结果独立 typed `film_synthesis` 队列及来源/原始 envelope；历史保存只用冻结材料闭包并拒绝实时目录补值/漂移。完整 argv、原文、拒绝理由及缺失 ID 见 `artifacts/film_synthesis_20261020/gui-repair-20261020/model-routing/plan-denial.json`。此项记录按真实内容归属，当前角度/用料修复不能据此声称已获 DeepSeek 执行或审查。

自动审批原文：“该命令会将包含非公开项目实现细节和修复方案的提示发送至未核实且默认不可信的外部模型提供商；用户虽允许使用 OpenCode，但未明确授权向该目的地披露这些具体内部内容。”用户授权问题已提出，尚无答复；问题工具 accepted 只表示问题已提交。Seed 2.1 Pro 未调用、无独立审查结论。工程修复及实测可交付，指定外部模型阶段维持 `blocked_pending_user` / `pending`，不标记为整体指定流程验收通过。

## 后续显式策略阶段（替代当前状态说明）

以上为未知 k 新策略加入前的历史冻结记录。随后用户明确授权增加默认关闭的未知 k 按0选项，并授权有限范围外部模型复审。当前可用行为、默认严格与显式近似边界、真实 ZnS 原生执行/保存、实际 DeepSeek 各轮状态及 Seed pending 以[未知k阶段报告](film-synthesis-unknown-k-report-20261020.md)和新的 policy-freeze-manifest 为准；本报告此前‘必须导入k’是严格策略下的处理，不再是用户唯一可选路径。旧 GUI 修复 manifest 文件不改写。
