from pathlib import Path
import re
root=Path(__file__).resolve().parents[1]
app=root/'other/ai'

def write(relative,text):
    path=app/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8',newline='\n')

write('harness_app/agent/resources/software_manual.md', '''# 独立AI助手能力说明 · 2026-10-03

## overview · 应用能力

提供持久对话、模型配置与能力探测、附件和产物预览、桌面浮动图标、通知、外部MCP、Harness规划与子代理、工作流引擎。实际模型由用户配置OpenAI兼容服务；不自带模型或API密钥。

## workspace · 文件工作空间

文件工具仅操作本应用data目录。支持列表、分页UTF-8读取、写入、移动和删除。外部附件由用户选择后复制到data/attachments。覆盖须显式overwrite=true并遵循用户授权。输出用工具返回的data引用展示。

## workflows · 持久工作流

list_workflows列出注册定义和输入格式。workspace.note校验title/text后生成Markdown；workspace.confirmed_note先暂停，收到用户真实response={"approved":true}后保存，false结束且不写文件。run_workflow接受稳定run_id，同ID不能改变输入。workflow_status查询真实状态和事件，workflow_runs查询任务，resume_workflow恢复，cancel_workflow取消。

引擎支持程序/Agent节点、跳转、循环、检查点、暂停恢复、取消、租约与节点独立Agent上下文。自定义流程由开发者注册可信Python callable；配置不执行任意代码。副作用须由节点自行保证幂等。

## harness · Agent执行机制

沿用Pydantic AI 2.51.0和Harness 0.36.0：步骤持久化、上下文压缩、任务用量保护、流式输出与中途插话。攻关模式按需启用Planning与Skills；操作和攻关模式可使用文件/工作流执行子代理。各角色共享任务预算。推理能力及上下文上限由设置页探测和配置。

## mcp · 外部与本机工具

设置页管理stdio/HTTP连接、工具目录、连接测试和配置导出。密钥保存在系统keyring。开启服务器不扩大当前目标或消息发送授权；allow_project_data遵循用户设置。内置MCP默认stdio，也可在设置中显式启动Bearer保护的回环HTTP端点。

## sandbox · 隔离Python

保留原Windows LPAC实现。首次使用运行tools/setup_sandbox.py准备私有运行时；脚本只能读取显式输入副本。不可用时拒绝执行，不回退到普通Python。绘图API保留coat_plot兼容名称，公式、参数和图解见docs/algorithms.html。

## development · 源码诊断

设置中开启开发能力后，提供固定环境/语法/单元测试检查、带指纹的人设编辑、独立只读路径授权、问题日志和本地调试接口。HARNESS_RELEASE_BUILD=1或冻结发行门禁优先。不提供通用终端或任意宿主脚本执行。

## limitations · 能力边界

本应用没有镀膜物理计算、机器/工艺/几何设计器。工作流不是分布式调度器，不保证外部副作用恰好一次。LPAC能力仅Windows；其他系统仍可使用对话、MCP及标准库工作流。真实模型调用需要用户自行配置服务和凭据；测试替身不等于真实服务商验收。
''')
for name,title,body in (
    ('workspace','Work with explicitly authorized files in the assistant workspace.','先读取必要文件及精确工具schema；普通路径仅限data。保持单位和引用。覆盖、移动、删除须符合用户要求。沙箱前查询python_sandbox_status，不可用时不得回退宿主执行。未知效果先核对，避免重复动作。产物通过返回的data引用展示。'),
    ('workflows','Run and resume host-owned persistent workflows.','先list_workflows获取定义和输入；run_workflow使用稳定run_id。同ID重试保持相同输入。paused须说明waiting_for并等待真实回答。用户授权继续后才resume_workflow。cancel_workflow保留检查点；workflow_status读取真实事件。不运行任意工作流代码，不扩大任务。')):
    write(f'harness_app/agent/skills/{name}/SKILL.md',f'---\nname: {name}\ndescription: {title}\n---\n\n{body}\n')
write('harness_app/agent/resources/default_persona.md',(app/'agent.md').read_text(encoding='utf-8'))
write('README.md','''# 独立 AI 助手与 Harness

从沉积仿真项目提取的完整桌面应用，Python 3.10+，推荐3.12。可以将整个 `other/ai` 目录复制到其他位置独立使用。设置页、聊天窗口、浮动图标、待决提问、托盘、通知及Harness机制来自原应用；原仿真项目继续保留原有功能。

## 启动

首次安装（Windows）：

```powershell
python -m venv .venv
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe launcher.py
```

日常双击 `start.bat`，优先本目录 `.venv`，其次本目录 `.conda`、系统Python；`launcher.pyw` 是无终端入口，启动诊断在 `artifacts/logs`。本工作副本已创建 `.venv`，安装结果见验收记录。迁移后重建虚拟环境，不能跨目录搬运已有环境。

Linux/macOS：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sh start.sh
```

Qt桌面需要目标系统的图形会话和Qt运行库。软件渲染可用 `python launcher.py --software`。`--module settings` 直接打开设置；`--development` 启用源码诊断。`AI_HARNESS_PYTHON` 可指定启动解释器。支持 `pip install -e .` 和 `ai-harness`；安装为普通包时状态默认保存于 `~/.ai-harness`，`AI_HARNESS_HOME` 可指定独立状态目录。

第一次启动同时打开原版设置页和对话窗口，浮动图标默认启用。保存服务器地址、模型及API密钥后即可对话；外部MCP沿用原设置页。API密钥使用独立系统keyring命名空间 `harness_app/assistant/*`；未复制原项目的密钥、配置、模型缓存或历史。系统安全凭据库不可用时拒绝明文保存。

## 包含的机制

- 原QML设置、聊天、浮标、待决项、通知和托盘；流式输出、插话、附件、产物预览、会话历史和用量统计。
- Pydantic AI 2.51.0 + Harness 0.36.0的StepPersistence、StableCompaction、Planning、Skills、SubAgents、共享额度与工具结果归档。攻关模式保留规划和技能；操作/攻关有文件和工作流执行助手。
- 外部MCP工具目录、连接测试、凭据、配置导出；内置stdio服务及设置页管理的回环HTTP服务。
- 原标准库工作流引擎：程序/Agent节点、状态跳转、循环、暂停恢复、取消、SQLite检查点/租约和节点Agent历史。
- 原Windows LPAC沙箱及离线SVG绘图API；运行前单独准备 `python tools/setup_sandbox.py`。不可用时不会在宿主降级执行。

内置工具已替换为独立文件与工作流服务。没有复制物理计算或仿真设计器，也不依赖父目录的 `coat_sim`。资源、字体、许可和提示音来源说明都放在本目录。

## 工作流

对助手说“创建一份标题为测试、内容为你好世界的笔记”，可使用 `workspace.note`；`workspace.confirmed_note` 在写入前暂停等待答复。流程输出在 `data/workflows/<run_id>/note.md`，状态及事件在 `artifacts/workflows.sqlite3`。相同run_id重试校验原输入和文件内容，拒绝覆盖不同内容。

```powershell
.venv\\Scripts\\python.exe tools/workflow_demo.py --text "hello"
.venv\\Scripts\\python.exe tools/workflow_demo.py --text "hello" --confirm
.venv\\Scripts\\python.exe tools/workflow_demo.py --resume RUN_ID --approve
```

自定义流程在 `harness_app/core/workflow_service.py` 注册；通用引擎和Agent适配器见 [工作流说明](docs/workflows.md)。GUI队列显示真实工作流检查点；MCP与CLI复用同一个服务。没有假造耗时或新增分布式调度能力。

## MCP 与验收

MCP客户端command使用本目录Python绝对路径，args使用 `-m harness_app.mcp.server`，cwd设为本目录；也支持服务脚本绝对路径。stdio启动后等待客户端协议输入是正常行为。

```powershell
.venv\\Scripts\\python.exe tools/verify.py
.venv\\Scripts\\python.exe launcher.py --smoke-test
```

检查会在隔离状态目录中验证文件边界、工作流恢复/取消/幂等、Agent节点、实际MCP子进程、Harness及GUI加载，不使用真实模型密钥。模型协议验收使用本机测试服务器，与真实服务商调用明确区分。

[算法图解与实现边界](docs/algorithms.html) 为离线单文件HTML。[提取与验收记录](docs/extraction.md) 记录来源、修改范围和本轮检查结果。
''')
# Offline documentation for unchanged copied sampling/statistical algorithms.
source=(root/'docs/algorithms.html').read_text(encoding='utf-8-sig')
style=re.search(r'<style>(.*?)</style>',source,re.S).group(1)
sections=re.findall(r'<section\b[^>]*>.*?</section>',source,re.S)
picked=[s for s in sections if 'id="sandbox-visualization"' in s[:120] or 'sandbox_helpers.py' in s]
assert len(picked)==2, [s[:100] for s in picked]
content='\n'.join(picked).replace('../coat_sim/','../harness_app/').replace('2026-10-01','2026-10-03')
content=re.sub(r'<p>[^<]*(?:2026-10-01|2026-10-03)本机生产运行器.*?</p>','',content,flags=re.S)
# Historical host evidence is not standalone verification evidence.
content=content.replace('证据为artifacts/debug/lpac-boundary-report.json；','原项目历史证据为artifacts/debug/lpac-boundary-report.json；')
write('docs/algorithms.html', '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>独立AI助手算法说明</title><style>'+style+'</style><main><h1>独立AI助手 · 算法图解</h1><p>更新：2026-10-03。本目录复制原实现，未改变绘图公式和默认采样。图中mm/nm默认值为原API兼容值；一般数据必须明确设置轴标签。历史验收不表示本轮重新验收LPAC。</p><nav><a href="#sandbox-visualization">脚本隔离与输出筛选</a> · <a href="#sandbox-scientific-plots">网格图与径向统计</a> · <a href="#workflow-host">工作流宿主</a></nav>'+content+'''<section id="workflow-host"><h2>工作流宿主</h2><p>用途与输入输出：输入title/text和稳定run_id，输出持久检查点、Markdown文件及SHA256；没有物理坐标，长度为字符、时间为秒。验证→可选暂停等待真实答复→保存。默认上限10次节点，title≤100字，text≤32000字；本项目约定。</p><pre>validate(input) → [confirm(response)] → save(data/workflows/run_id/note.md)
重试：已有文件内容相同则复用；不同则拒绝覆盖。</pre><figure class="figure"><svg viewBox="0 0 760 130" role="img" aria-label="校验、暂停和保存"><g font-family="sans-serif" font-size="18" text-anchor="middle"><rect x="20" y="20" width="190" height="65" fill="#e3f1ed"/><text x="115" y="60">校验输入</text><path d="M210 52H280" stroke="#64878b" stroke-width="3"/><rect x="280" y="20" width="190" height="65" fill="#fff2dd"/><text x="375" y="60">可选暂停 / 答复</text><path d="M470 52H540" stroke="#64878b" stroke-width="3"/><rect x="540" y="20" width="190" height="65" fill="#e3f1ed"/><text x="635" y="60">保存并核对内容</text></g></svg><figcaption>检查点写在节点边界；中断重跑仍需节点自身核对副作用。</figcaption></figure><p>实现：harness_app/workflow.py、harness_app/core/workflow_service.py。只执行注册的可信callable；没有分布式调度或外部恰好一次保证。固定笔记流程未找到外部依据，仅为本项目实现。</p><p>参考：<a href="https://docs.python.org/3/library/sqlite3.html">Python sqlite3官方文档</a>、<a href="https://docs.python.org/3/library/hashlib.html">hashlib官方文档</a>。用于底层存储及指纹，不作为业务流程验证依据。</p></section></main></html>''')
# The project's main documentation records relocated implementations as required.
notice='<p class="small">2026-10-03：同一算法已提取到独立应用 <a href="../other/ai/docs/algorithms.html">other/ai 算法文档</a>；新增实现位置为 <a href="../other/ai/harness_app/agent/sandbox_helpers.py">harness_app/agent/sandbox_helpers.py</a> 与 <a href="../other/ai/harness_app/agent/sandbox.py">sandbox.py</a>。公式、采样和近似沿用本节；通用工作流与笔记管线见该独立文档。迁移不视为新的数值验证。</p>'
source=source.replace('<h2>15 · 沙箱脚本、输出筛选与离线图表</h2>', '<h2>15 · 沙箱脚本、输出筛选与离线图表</h2>'+notice)
source=source.replace('更新：2026-10-02；有效尝试预算', '更新：2026-10-03；独立AI提取、有效尝试预算',1)
# Add the independent entry beside the existing sandbox TOC entry.
source=source.replace('<a href="#sandbox-visualization">', '<a href="../other/ai/docs/algorithms.html">独立AI算法</a> · <a href="#sandbox-visualization">',1)
(root/'docs/algorithms.html').write_text(source,encoding='utf-8',newline='\n')
readme=(root/'README.md').read_text(encoding='utf-8-sig')
anchor='**工作流引擎：**'
pos=readme.index(anchor)
readme=readme[:pos]+'**独立AI助手：**[other/ai启动与安装](other/ai/README.md)，包含原设置页、对话窗口、浮动图标、Harness与工作流引擎，可脱离仿真项目运行。\n\n'+readme[pos:]
(root/'README.md').write_text(readme,encoding='utf-8',newline='\n')
workflow=(root/'docs/workflows.md').read_text(encoding='utf-8-sig')
workflow=workflow[:workflow.index('本项目的 MCP `create_default_workflow`')]
workflow=workflow.replace('coat_sim','harness_app').replace('CoatService','WorkflowService')
workflow=workflow[:workflow.index('## 最小示例')]+'''## 最小示例

```python
import asyncio
from harness_app.workflow import SQLiteCheckpointStore, StepResult, Workflow, WorkflowEngine, WorkflowNode

async def main():
    engine = WorkflowEngine(SQLiteCheckpointStore("artifacts/example.sqlite3"))
    flow = Workflow("hello", "1", "hello", [WorkflowNode("hello", lambda ctx: StepResult(output={"hello": ctx.input}))])
    result = await engine.run(flow, {"name": "world"})
    print(result.snapshot())

asyncio.run(main())
```

Agent节点适配器 `pydantic_agent_node` 随引擎原样提取。消息历史存于run内的节点身份；宿主重新启动后须注册同ID、同版本的定义。

本应用 `WorkflowService` 注册两个可启动的笔记流程，CLI/MCP共用：直接保存、暂停答复后保存。对话里的Harness执行机制负责模型步骤，轻量引擎负责应用节点检查点。引擎不包含原仿真服务或分布式调度器。
'''
write('docs/workflows.md', workflow)
print('Wrote standalone manuals, skills, README and offline algorithms; updated project entry points')