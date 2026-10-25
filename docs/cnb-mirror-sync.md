# GitHub main → CNB main 镜像同步

本项目的源为 `https://github.com/ii33/coating-sim.git`，镜像为 `https://cnb.cool/nonereason/coating-sim`，仅同步两端 `main`。使用 Windows PowerShell 脚本 [`tools/sync_cnb.ps1`](../tools/sync_cnb.ps1)。它不会发布 Release，也不会把工作区未提交内容作为推送来源。

先按执行环境批准的方式运行已审查脚本；脚本不调整执行策略、系统权限或凭据设置。以下示例不安装软件，也不执行下载内容。遇到明确权限拒绝时停止并报告。

## 固定网络与仓库身份

- GitHub 始终使用命令级 `http://127.0.0.1:10808`。代理不可达即停止，不能自动回退直连，也不修改全局或系统网络设置。
- CNB 独立直连；TLS 校验开启，HTTP 重定向关闭。CNB 凭据不能发送到 GitHub 代理或其他域名。
- 在隔离 checkout 中配置 `origin` 为上述 GitHub HTTPS URL，`cnb` 为上述 CNB HTTPS URL，字符串必须完全一致。每个远端只允许一个 fetch URL，不能单独配置 `pushurl`（即使值相同也拒绝）。脚本核对原始 URL 与展开改写后的 fetch/push URL 均为固定 canonical URL，拒绝端口写法、额外 URL、用户名密码、query/fragment、跨域或协议改写。不能用 SSH 别名或其他项目替代；脚本不自动修改配置使其通过。
- 公共 refs 查询禁用凭据助手和交互提示，并清除继承的代理、认证入口和 Git 跟踪环境。读取失败不会触发登录、写入凭据或改用其他认证路径。

Git 的 [HTTP 实现](https://github.com/git/git/blob/master/http.c)还允许 libcurl 使用 netrc 文件认证。因此公共网络读取前，脚本只检查标准主目录下 `.netrc` / `_netrc` 是否存在，以及是否声明了 `NETRC` 环境变量名；发现这些隐式认证入口或无法核实其存在性时即停止，不读取文件内容或该变量值，也不删除、迁移或改写凭据。不能通过更改认证文件绕过这项检查。

客户端证书与代理证书认证也不在这个公共读取流程中。脚本仅检查相关 Git 配置**名称**，发现证书/私钥输入配置即停止，不取配置值、不打开文件。不会用空证书路径来“清空”认证：该写法会在部分 Git 版本触发证书认证逻辑。保留原有 HTTPS 凭据助手供显式推送使用，不新增助手或客户端证书设置。

隔离 checkout 应已拥有待比较提交对象；取得对象也必须遵守同一 GitHub 路由和当前环境授权。不要复用包含私有工程或未审查代码的运行目录。脚本不负责克隆、清理或覆盖工作区。

## 默认只读检查

提供明确的完整目标 SHA 与预期版本；目标必须仍为 GitHub `main`：

```powershell
powershell.exe -NoProfile -File .\tools\sync_cnb.ps1 `
  -RepoPath . -TargetSha '<40位小写提交SHA>' `
  -ExpectedVersion 'dev.YYYYMMDD.NN'
```

默认行为只读取 refs、已有提交对象、祖先关系及目标的 `coat_sim/version.txt`，不 fetch、不生成推送日志、不创建认证辅助文件、不读取 CNB 凭据。缺少本地对象时停止并说明，不隐式下载。脚本不安装或运行目标提交中的程序。

所有 Git 子进程禁用本地 replacement objects，避免 `refs/replace` 改写版本或祖先验证。若 Git 指定的旧式 `info/grafts` 历史覆盖文件存在或无法核实其存在性，脚本在读取提交图与访问网络前停止；只检查元数据，不读取、删除或修改该文件。

结果有三种：

| 远端关系 | 行为 |
| --- | --- |
| CNB SHA 等于目标 | no-op，不重复推送或读取凭据 |
| CNB 是目标祖先 | 报告可普通前向同步；默认仍只读 |
| CNB 与目标分叉或领先 | 停止，不强推、不改历史 |

`-DryRun` 同样只读；即使同时提供 `-Push -DryRun`，也不会 fetch、认证、写推送日志或执行推送。纯逻辑自检不访问网络或凭据：

```powershell
powershell.exe -NoProfile -File .\tools\sync_cnb.ps1 -SelfTest
```

## 显式推送及认证边界

只有在当前任务授权覆盖这两个仓库、目标和认证方式时才使用 `-Push`：

```powershell
powershell.exe -NoProfile -File .\tools\sync_cnb.ps1 `
  -RepoPath . -TargetSha '<40位小写提交SHA>' `
  -ExpectedVersion 'dev.YYYYMMDD.NN' -Push
```

普通模式复用原生 Git 已有 HTTPS 认证入口，命令级用户名为 `cnb`。脚本不提取、创建或修改持久凭据。若现有入口不可用，停止；不尝试 SSH、不猜测其他凭据、不自动开启登录或云开发环境。

如当前环境已经具备名为 `CNB` 的进程环境凭据，且当前授权明确允许本次向上述 CNB 仓库使用它，才可以另加 `-UseCnbEnvironmentCredential`。此开关只对实际 `-Push` 生效。凭据由单次、严格主机和仓库路径校验的 askpass 子进程读取，并经 Git 管道传递；临时辅助代码仅含变量名与守卫，不含凭据值。秘密不能出现在参数、URL、文件、持久 Git 配置、日志或编码后的 Authorization 输出中。创建新凭据、扩大目的地或绕过新的审批拒绝均不属于此开关的授权范围。

推送前重新读取两端 refs；GitHub `main` 必须仍等于明确目标，CNB 必须未变化且为目标祖先。先保存两端 SHA、祖先结论和版本，再发送唯一普通 refspec `完整目标SHA:refs/heads/main`。不会使用本地 `HEAD`、工作树内容、`--force` 或 `--force-with-lease` 作为同步捷径。

实际推送日志保存在 Git 内部元数据目录，包含公开仓库、SHA、版本和安全结果类别，不记录机器名、用户名、工作区绝对路径或秘密。不要把运行日志、恢复文件、用户配置或任务记录提交入仓。

## 验收和异常处理

成功后重新读取 CNB `main`；它必须等于目标。目标提交中的版本文件与已核验版本绑定，因此相同 SHA 也证明镜像拥有相同版本源码。源码版本读取不代表已安装安装器验收。

命令超时、网络中断或认证失败后，不自动重试。先无认证只读核对远端结果，区分未执行、已执行、结果不确定；远端变化、权限拒绝或新登录要求须准确报告。不得换机器、路径、代理或其他工具绕过明确拒绝。

镜像同步与发布工作站的实际运行副本更新分别验收。后者遵循[安全更新与本机版本验收](workstation-source-update.md)；仅完成 GitHub 发布或 CNB 推送不能宣称本机版本已经更新。
