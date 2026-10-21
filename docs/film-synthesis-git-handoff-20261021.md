# Git 同提交算法交接（2026-10-21）

本入口只读取本项目正常发布的源码与公共冻结请求，不读取Library包、用户工程或未保存设计。八请求由当前正式GUI构造/冻结/JobSpec链重新生成，纯CPU云运行不加载GUI；材料真实n/k、未知k显式假设、目标和制造界均保留。公共请求文件为`benchmarks/film_synthesis/20261021/cloud-requests.json`，它不嵌入自身提交SHA，避免循环哈希。

## 云端执行

父端取得本地最终准确SHA后，在同一仓库检出该SHA。下面SHA占位符必须替换为实际完整40位值：

```text
git fetch origin main
git checkout --detach <EXACT_SHA>
python -m pip install -r benchmarks/film_synthesis/20261021/requirements-cpu.txt
python tools/run_film_synthesis_cloud.py --manifest benchmarks/film_synthesis/20261021/cloud-requests.json --repo-root . --expect-commit <EXACT_SHA> --mode clustered --preflight --output unused-preflight
python tools/run_film_synthesis_cloud.py --manifest benchmarks/film_synthesis/20261021/cloud-requests.json --repo-root . --expect-commit <EXACT_SHA> --case ar_ultrabroad_visible --mode v23_matching --seed 0 --workers 2 --output runs/ar-control-seed0
python tools/run_film_synthesis_cloud.py --manifest benchmarks/film_synthesis/20261021/cloud-requests.json --repo-root . --expect-commit <EXACT_SHA> --case ar_ultrabroad_visible --mode clustered --seed 0 --workers 2 --output runs/ar-cluster-seed0
```

输出目录必须不存在。每个case/mode/seed用新进程；两模式同主机同材料/目标/制造界，每次120秒、2候选worker、1数学线程。seed0 control→cluster，seed1 cluster→control，seed2 control→cluster；各配对不并发争用CPU。完整8case×3seed×2mode确认实验由云端执行，本地不新增研究长算。缩短`--budget-seconds`只作非确认性smoke，不计成功率。原始最差R/T及匹配密/错位/极值审计判定；lambda/loglambda均值分metric，仅作描述。

## 身份、依赖与取消

Git模式要求HEAD等于准确SHA，仓库根正确，声明的核心源码、请求文件、材料目录来源和运行器与该提交的常规blob逐一匹配。哈希仅将UTF-8文本CRLF变LF，以便Windows/Linux相同；不忽略空格或真实代码变化。实际Git blob ID与LF规范SHA256都保留，无关未提交内容不纳入运行输入。源码在运行前后复核；源不匹配、缺失/untracked输入或路径逃逸直接拒绝。请求与配对请求哈希另存，两模式只能改变结构模式。

纯CPU依赖为NumPy、SciPy、Pydantic，版本边界与项目保持一致。Git命令只用于读取身份，产品GUI/核心没有新Git或pwsh依赖；研究CLI需已安装Git。每秒心跳，30/120秒无算法进度只标stalled/unresponsive；SIGINT/SIGTERM合作取消、保留最佳候选、恢复handler并清理自有worker。未知工作保留null及下界，不能填0。零CLI退出码只表示返回记录，不代表光学达标；读取result.json的原始验收和confirmation_eligible。

## 公开证据与历史

带`-public`的资格/DE证据只替换本机绝对路径字符串，所有数值、失败、处方及来源不变；原始证据SHA保留，原文件本地原样保留。v21/v22/v23历史结果不升级为本轮新结构成绩。已有399项本地回归和有界包测试也不等于云端确认或原生GUI交互验收。

本轮仅准备本地算法提交A，不推送。父提交4d5071c仍是未推送GPU修复；算法运行不调用其UI设置，但正常线性推送会包含该祖先，须对整条提交链取得明确批准。没有通过新分支、历史复制或其他写入通道绕开此前拒绝。
