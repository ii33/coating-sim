# 本项目 GitHub/CNB 获取与依赖部署

2026-10-16。本入口只服务本项目（默认 `ii33/coating-sim`，旧开发源显式选择），不提供通用构建、EXE 打包、
在线更新或发行平台。接口真值为 `tools/dependency_manager/project_deploy.py`
及对应契约测试。

## 计划及执行

`plan_project_deployment(directory, ref='main', groups=('desktop',),
backend='cpu', profile='runtime', mirror='pypi', python=None, proxy=None, *,
emit=None, cancel_event=None, timeout=300, source="github", repository_url=None)` 只接受新的目录。它通过已安装的
Git 获取受限来源仓库，把 branch/tag 解析并固定到完整 commit SHA，检出后调用
同一个 `manager.plan_environment`。源码来自该 commit，默认依赖目标仅是
该 checkout 的 `.venv`。profile 可选 runtime/development；镜像、代理、
正常 CPython 和计算后端沿用已有环境管理器的验证规则。

计划阶段不执行项目启动入口、Git hooks、子模块、LFS filter、setup.py 或
仓库提供的命令。保留现有 Git 认证方式，但禁用交互认证；不接收或保存
密码、token、私钥，不更改凭据 helper。缺少认证时先由用户在 Git 中完成
认证，然后在新目录重试。会改变固定 HTTPS 地址的 Git URL rewrite 被拒绝。

`read_project_deployment_plan(directory)` 读取已准备的计划；CLI execute 不应
再次在该目录调用 prepare。`execute_project_deployment(plan, confirmed=False,
emit=None, cancel_event=None, proxy=None)` 必须匹配用户审阅的计划。它验证目录
身份、commit、Git-clean 源码和输入指纹后，将依赖执行全部交给
`manager.execute_plan`。未审核/被改动/过期计划不执行安装。

唯一源码/部署状态所有者是 project_deploy；唯一依赖状态所有者是 manager。
CLI/GUI 只收集参数、展示完整 commit/目标/依赖/命令和转发事件与确认。
计划及结果保存在新 checkout 的 `.git/dependency-manager-deploy/`，不制造
普通源码文件的修改。已有目录、dirty checkout、已有 venv、不相关内容均
不覆盖；失败/取消保留现场，新部署须选新目录。

CLI `project-plan --deployment-dir <新目录> --ref main --python <正常Python>
--profile development --json` 会获取源码并返回完整待确认计划。
审阅 commit、目标及依赖后，使用 `project-deploy --deployment-dir <同目录>
--plan-id <上一步plan_id> --yes --json`。执行读取保存的计划，不重新获取源码；
`--yes` 和完整匹配的部署 plan_id 两者缺一不可。该入口不接受 `--venv` 覆盖。

## 隔离、错误及完成标准

依赖安装限定在项目 `.venv`，不启用 system site packages，不修改系统
Python、PATH、全局 pip、其他环境或运行时安装记录。源码及环境是文件系统
写入；venv 不是操作系统安全沙箱。正常 CPython 与私有 MinGit 自动安装各走独立审核计划；project-plan 不隐式安装它们。

Git 网络操作有总超时和可取消进程组；Windows 使用系统 taskkill 尽力停止
进程树，不能确认停止时明确警告。脱敏日志不保留 URL 凭据/查询或常见
token。依赖阶段沿用已有的取消、wheel dry-run、锁及安装后重新检查。
结果明确 blocked/failed/cancelled/timed_out/success，不把仅源码检出成功
称作依赖部署成功。success 必须来自环境管理器的安装后检查。

部署不会启动 GUI 或跑测试。成功后给出已存在的开发入口：项目 `.venv`
Python + `launcher.py`，以及 development profile 下的
`python tools/run_tests.py quick`。这些仅是后续命令提示；不自动执行。
它不迁移其他环境/用户数据，不切换现用安装，也不构建或发布软件包。

## 2026-10-16：公开源码、CNB 镜像与只读版本检查

默认源码 URL 为 `https://github.com/ii33/coating-sim.git`。该公开仓库已于
2026-10-16创建，但本版本记录时源码尚未上传；仓库存在不等于目标ref可获取，
网络/权限/ref错误必须原样报告为失败。
旧开发源仅通过 `--source github --repository-url
https://github.com/ii33/deposition-sim.git` 显式选择。GitHub 不接受其他仓库。
本工具仍只识别和部署本项目，不提供任意仓库构建服务。

CNB 使用 `--source cnb --repository-url <本项目真实镜像的HTTPS地址>`，
没有预设组织或虚构镜像地址。仅接受 `https://cnb.cool/组织/仓库`，支持
多层组织及可选 `.git` 后缀，拒绝 URL 中凭据、端口、查询、fragment、其他
协议或域名。所选 URL/平台写入待确认计划，commit 仍在 fetch 前固定。
重新读取计划时再次验证来源；不改变已有认证 helper、不存储凭据。
用户应先在自己的 Git 中配置适当只读访问权；本工具不会代建 token。

官方依据（2026-10-16核对）：
- [CNB Git 地址与认证](https://docs.cnb.cool/en/guide/git-access.html)：HTTPS 仓库页面地址及 `.git` 形式均可，SSH 不支持。
- [CNB OpenAPI](https://docs.cnb.cool/en/develops/openapi.html)：API 与 Swagger 在 `https://api.cnb.cool`。本实现复用 Git，不使用 API，不推断 archive URL。
- [CNB 访问令牌](https://docs.cnb.cool/en/guide/access-token.html)：用户名为 `cnb`，密码为访问令牌。令牌不得放入本工具的命令行/URL/日志。

### CLI

```text
python dependency_manager.py project-check --project <本地目录> --json
python dependency_manager.py project-check --project <本地目录> --check-remote --source github --ref main --json
python dependency_manager.py project-plan --source cnb --repository-url <真实镜像URL> --ref main --deployment-dir <新目录> --python <完整Python> --json
```

`project-check` 默认不联网，只读内部版本文本、Git HEAD/ref/dirty/origin，
并附上已有环境检查器的 Python/依赖状态。无 `.git` 的源码包或无 Git
程序时，版本仍可读取，但 commit/ref/dirty/source 明确 unknown/null；
不从路径、父仓库或环境变量伪造来源。未知/带凭据的 origin 不回显。
只读状态检查先读取配置键名，命令级禁用全部 clean/smudge/process 过滤器及
required 标记，不运行仓库过滤命令、不改配置或索引。过滤器转换过的文件
可能被保守标为 dirty，结果明确提示此边界。status显式忽略子模块工作树，
避免子仓库配置触发外部命令；submodule_dirty=not_checked，dirty仅描述父
工作树，不能视为包含子模块的完整clean结论。源码新部署仍拒绝gitlink。
Git子进程同时设置GIT_ATTR_NOSYSTEM=1，不加载系统gitattributes。
内部版本来自 `coat_sim/version.txt`，不导入所选项目代码。

只有 `--check-remote` 才通过 `ls-remote` 查询所选来源/ref；不 fetch、不
checkout、不更新本地文件或索引。结果 comparison 为 equal/different/
unknown/not_requested，不能推断 ahead/behind。完整 SHA 直接作为指定
版本比较，并警告没有验证它在远端存在；命名分支/tag 才有远端解析结果。
远端失败仍保留已读到的本地版本信息。源码检查 success 只表示检查完成，
不能代替 environment.status，也不表示依赖已安装。

### Tk 与状态边界

“获取本项目”页选择平台、仓库 URL、分支/ref 与新目录，然后获取源码并
审核既有计划。新增“本地版本检查（离线）”及“检查并比较所选远端”按钮，
检查目标是窗口顶部的本地项目目录，远端为该页所选来源/ref。完整结果
在同页展示。后台任务沿用 generation/queue 状态所有者、busy 禁止重复
提交、取消与关闭边界；检查不弹安装确认，也不自动执行下一步。

### 零部署与失败恢复的真实前提

Windows `dependency_manager.cmd` 不要求预装 Python；既有 bootstrap
下载官方完整 CPython，校验固定 SHA-256，含 venv/pip/Tk，不使用嵌入式包，
不更改系统 PATH、代理或安全策略。下载失败移除本次临时下载，重新启动
即可重试；安装中断保留现场/锁，须先检查，不能盲目覆盖正常目录。
Windows 无 Git 时，可先在“私有 Git”页或 git-plan/git-install 安装固定官方
MinGit；默认位置会被 project-plan/project-check 自动发现，自定义位置用
--git <目录>/cmd/git.exe 显式选。全过程无需管理员，不改系统 PATH。Linux/macOS
仍需用户提供完整 Python 和 Git；系统 Qt/Tk/发行版基础库缺失时应由用户或
管理员按系统策略处理，不自动修改系统。

自动测试只用假网络或临时 Git 仓库覆盖 CNB 配置、来源拒绝、源码包未知、
脏文件保留、网络失败/超时/取消、CLI 默认离线及计划读回执行。干净 Windows
仍需实机验收：无 Python、完整 bootstrap 与 Tk、Git 缺失提示、无系统 Git 时的私有 MinGit 下载/校验/重试，真实 GitHub
和实际 CNB 镜像、代理/TLS失败、下载中断重试、依赖安装/启动、只读检查
前后文件/索引一致。没有实际 CNB 地址时不能声称 CNB 在线部署已验收。


## 私有 Git 与启动入口

详细安全边界及验收参见 [私有 MinGit](dependency-manager-git-runtime.md) 和
[启动脚本与快捷方式](dependency-manager-launchers.md)。Windows 从零路径：
启动 dependency_manager.cmd → 审核完整 Python bootstrap → git-plan/git-install
或 GUI“私有 Git” → project-plan/project-deploy → launch-plan/launch-create。
每一步确认当前计划，不自动启动项目；不创建 Release、不打包 EXE。

```text
python dependency_manager.py git-plan --json
python dependency_manager.py git-install --plan-id <审核计划ID> --yes --json
python dependency_manager.py launch-plan --project <已部署目录> --shortcut desktop --json
python dependency_manager.py launch-create --project <同目录> --shortcut desktop --plan-id <审核计划ID> --yes --json
```

Git 目录可用 --git-dir 显式选择用户主目录内的新路径。默认目录安装成功后
自动发现，不需要全局 PATH。GUI 保留显式 Git 可执行文件选择框。
启动入口默认只建项目根目录的 `Coating Simulator.cmd`（Linux `.sh`）；
快捷方式 `desktop` / `start-menu` / `custom` 仅 Windows，默认 `none`。
自定义目录传 --shortcut-dir，名称传 --launch-name。已有文件一律保留，
需换名称或由用户明确处理，不能盲目覆盖。快捷方式使用本项目现有 ICO。
生成入口是短事务，原生 COM/PowerShell 有超时，GUI 不提供假取消：创建期间
请等待结果再关闭。它只写明示入口，不运行项目。
