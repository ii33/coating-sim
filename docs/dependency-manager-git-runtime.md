# Windows 私有 MinGit

2026-10-16。代码真值：`tools/dependency_manager/git_runtime.py` 与
`git_runtime_manifest.json`；本入口只补齐本项目部署所需 Git，不是通用工具安装器。

## CLI 与 Tk

```text
python dependency_manager.py git-plan --json
python dependency_manager.py git-install --plan-id <刚审核计划ID> --yes --json
```

可用 `--git-dir <用户主目录内的新目录>` 自选位置，两步保持相同参数。
Tk“私有 Git”页使用同一计划/执行函数，先展示完整来源、版本、SHA-256、
许可和目录，再明确确认下载。无管理员权限要求，不改系统 PATH、注册表、
安全策略、代理设置或 Git credential helper。默认私有目录安装后自动被
项目获取/版本检查发现；自定义目录用 `--git <目录>/cmd/git.exe` 或顶部
Git选择框显式选择。显式路径错误不会悄悄换用其他 Git。

API：`plan_git_runtime(directory=None, proxy=None)` 只读；
`execute_git_runtime(plan, confirmed=False, emit=None, cancel_event=None, proxy=None)`
执行当前计划。`default_git_directory()`、`find_managed_git(directory=None)`、
`discover_git(explicit=None, directory=None)` 负责路径发现。计划/安装状态唯一
所有者为 git_runtime；CLI/Tk只呈现、转发确认/事件/取消，不复制下载逻辑。
成功状态为 completed，路径为结果 git 字段；取消为 cancelled，失败为 failed。
CLI completed 返回0。取消/失败不等于已安装，必须检查最终结果。

## 固定来源与许可

固定 MinGit 2.56.0.2（Git 2.56.0.windows.2），支持 Windows x64/ARM64。
制品 URL 和 SHA-256 来自[官方 Git for Windows 发布页](https://github.com/git-for-windows/git/releases/tag/v2.56.0.windows.2)，
记录于源码 manifest。下载只允许官方 HTTPS GitHub/release-assets 域，大小有界，
固定完整 SHA-256 比对通过才解压。发布页的签名tag/Authenticode说明只是
来源证据，不声称对 ZIP 做了独立签名验真。TLS失败不绕过。

Git 是独立 GPL-2.0 工具，所附组件各有许可；不受本项目 MIT 许可证覆盖。
本项目源码分发不内嵌 MinGit，按需从上游下载。再分发者须保留上游声明并
履行适用源码义务。没有创建或接受新的账户协议。

## 安全、重复运行和失败恢复

仅接受用户主目录内新路径，拒绝符号链接、junction/reparse父路径和已有
目录。验证父目录身份，下载及解压在同盘私有暂存目录；拒绝ZIP穿越、绝对
路径、反斜杠、ADS、设备名、链接、大小写重名、保留marker、加密项和超限
内容；验证cmd/git.exe存在，Windows原子重命名到新目标，目标出现即失败。
不覆盖现有MinGit、项目或用户文件。网络/校验/解压失败清理本次暂存，
重新生成计划可重试；保留已存在目录。日志使用现有代理脱敏逻辑，不把
凭据放进URL或持久配置。

Linux/macOS 不自动安装系统 Git；检查系统前提并提示用户按系统策略准备。
Windows 32位未提供本版本制品，明确阻塞而非装错架构。

## 验收边界

离线测试覆盖计划确认、身份变化、下载hash/大小/取消/脱敏、解压恶意样本、
失败重试与同名目录保护。它们不能替代真实下载或Windows执行验收。
干净Windows仍须验证：无系统Python/Git时bootstrap→私有Git→源码获取→
隔离依赖→启动入口；真实网络、代理/TLS失败、取消/重试、x64/ARM64匹配、
安装后git --version及真实clone、系统PATH/注册表未变。没有实际CNB镜像
地址或认证时，CNB在线流程仍标待验收。
