# 工程包与操作栏约定

工程包 `.cproject` 是受管工程已保存状态的 ZIP 快照，不是项目描述文件，也不是旧 `.coatasset` 的改名。ZIP 内的清单入口固定为 `project.coatproj`，与原工程实际描述文件的名称无关；例如从 `My-study.coatproj` 导出，包内仍写入 `project.coatproj`。该清单是 kind `coat_sim.project`、version 1 的严格 UTF-8 JSON；原有七个必需字段保留，可另有可选 summary/notes；受管路径允许 data/分类/r修订号/资源名.扩展名，旧 objects 路径仍可读取。旧严格 reader 会拒绝不认识的字段或路径。包包含清单及其当前 manifest 引用的所有设计、材料快照、沉积配置，包括非默认设计，并保留默认配置关系。导入到尚不存在的新目录后，项目描述文件使用默认名 `project.coatproj`，并像此前一样分配新工程 ID。旧包中以 `project.json` 为清单入口的格式不兼容，导入时拒绝；不得扫描、迁移或猜测其内容。

普通打开与包导入是不同边界：打开项目必须显式选择实际 `.coatproj` 文件（任意前缀），其父目录才是工程根；目录、`project.json` 和 `.cproject` 都不是打开别名。包导入则必须显式选择 `.cproject` ZIP，再指定一个新目录。格式、路径及安全边界见[工程核心契约](project-core-contract.md)。

外部文件后缀识别不区分大小写，`.COATPROJ` 与 `.CPROJECT` 亦有效；打开后保留所选描述文件的原始名称和大小写。ZIP 内成员名仍精确校验，规范清单入口始终为小写 `project.coatproj`。

导入仅到不存在的新目录，分配新工程 ID，保留内部设计 ID 和默认依赖关系；原工程可同时打开且互不串用。描述文件落为新目录的默认 `project.coatproj`。先在同卷临时目录完整验证再原子发布，不覆盖现有项目。归档只包含默认清单入口和清单声明的受管输入，不打包任意目录文件、未被配置引用为输入的仿真结果、运行历史、凭据或环境状态；明确引用的沉积输入资产例外见下节。拒绝路径穿越、绝对路径、重复/大小写冲突条目、符号链接和非普通文件、未知/多余文件、缺失输入、超容量、摘要错误和不受支持版本；不执行包内代码。旧版 `project.json` 清单入口会因缺少规范 `project.coatproj` 而拒绝，不会尝试迁移。

UI 顶部操作行统一称“操作栏”，不另加占宽标题。窗口下拉收起时显示 `projectModules` 中与当前窗口 `currentModuleKey` 对应的名称；未知键显示明确占位，不借用第一项。展开后提供“膜系”（设计、优化）、“沉积”（工艺、机器、源、修正板、基片设计器）和“仿真”（光谱、沉积、光线追迹、外观）三个子菜单，工程材料和工程计算队列保留为分隔后的独立工具项。分类仅用于显示，不修改模块键或工程路由。打开分类、悬停、取消不导航；仅叶项激活一次 `openProjectModule(key)`，选择当前窗口仍由后端显示并激活已有实例，收起名称保持当前窗口。键盘可进入、退出子菜单和取消，并恢复选择器焦点。各窗口沿用同一共享控件和主题逻辑尺寸，始终携带所属工程上下文。

普通设计器不再提供旧 `.coatasset` 包浏览或导出入口；底层兼容 reader/exporter 仍可读取并导出旧资源，仿真与队列的“旧版结果文件”入口仍可命名保存和读取 `.coatasset` 结果。它们不能替代 `.cproject` 工程迁移。结果文件缺失仍如实提示，不用工程输入替代结果。

## 使用方法

启动器“工程包”菜单提供“导入工程包”和“导出已保存的工程”。导出针对当前选中工程；有未保存输入时先保存，计算队列中的任务可继续。选择尚不存在的 `.cproject` 文件，不覆盖旧备份。导入时选择工程包，再填写一个尚不存在的新工程完整目录；验证成功后打开新工程，原工程保持不变。项目管理/API 的已有工程操作使用描述文件路径作为 `root`，响应中的 `root` 仍是工程目录，`project_file` 返回实际描述文件路径；只有创建操作接收新工程目录。

无界面接口：

```sh
python -m coat_sim.project_cli export-package /path/to/project/My-study.coatproj /path/to/backup.cproject
python -m coat_sim.project_cli import-package /path/to/backup.cproject /path/to/new-project
```

同理，既有工程的 CLI 操作（如 `inspect`、`resolve`、`import` 和 `save`）传入所选实际 `.coatproj` 文件，例如 `/path/to/project/My-study.coatproj`，而不是工程目录。`create` 仍传入一个尚不存在的新目录；创建结果使用 `project.coatproj`。

只备份已经保存的受管工程输入。原始 `.coatproj` 描述文件可以采用任意名前缀；导出包的清单名仍固定是 `project.coatproj`，不携带原始文件名。若需长期归档计算图像/逐层结果，应另外保留结果文件；未被配置明确引用为输入的运行结果与异常恢复草稿不属于工程包。工程包导入不会启动计算或执行脚本。

## 明确引用的二进制输入（2026-10-15）

上述“结果不打包”指未被输入配置引用的运行输出、结果图像、历史与恢复状态。已保存光追/外观配置明确引用的沉积 `.coatasset` 是输入依赖，必须随所选依赖闭包携带；原始标量 SPD CSV 同理。`.casset` 小JSON严格声明role、项目相对payload路径、UUID、size和SHA-256，包仅额外允许这些精确声明的字节成员，不接纳任意附件。只读`read_project_package`不解包到工程目录；流式核验size/hash，选中副本还须通过既有coatasset/CSV安全解析器后才能发布。角色上限、可配置I/O预算、旧reader未知kind明确失败及spool生命周期详见[受管仿真输入契约](project-simulation-input-contract.md)。项目描述JSON仍version 1，默认输出结果不会因此进入工程包。
