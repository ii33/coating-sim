# 入口解耦文件所有权

2026-10-11，Sol 主持，Luna xhigh 并行。

本任务新增：docs/entrypoint-capabilities.md、docs/entrypoint-service-contract.md、docs/entrypoint-service-evidence-20261011.md、coat_sim/core/capabilities.py、coat_sim/core/film_result_service.py、coat_sim/capability_cli.py、tests/test_entrypoint_capabilities.py、tests/test_film_result_service.py。定向适配 coat_sim/mcp/server.py 的膜系结果/22 个共享工具及 UI 客户端惰性初始化，不动注册权限集合。README 只追加入口。

追加适配所有权：coat_sim/agent/tool_dispatch.py 对共享操作做同一严格参数校验与默认值归一化，解码同一 code/message；已派发写错误保守记 unknown 并阻止重放。只读 find_calculation_job 保留实际查询 ID，避免不同查询互相累加失败次数；其他提交令牌去重原样保留。docs/mcp.md 和 docs/assistant.md 仅增加入口契约链接。

不修改：coat_sim/core/film_design_service.py、film_design.py、film_optics.py、schemas.py、film_optimization*.py、film_design_session.py、ui/film_simulation_settings.py、点数路径及之前已验收求解器、队列八方法和内存会话同步。现有未提交更改保留，不提交/推送/发布。

若 docs/algorithms.html 由点数 Sol 修改，本轮只需其最终追加“膜系产物编排位置 core/film_result_service.py，数值统计定义不变”位置说明；本任务不竞争编辑该文件。

审计结论：膜系计算结果产物编排在 MCP server；CLI 分散；膜系优化/Mitsuba 外观没有已注册 MCP 工具；研究库 provider 有独立授权。有限落地为共享白名单能力契约、结果编排抽取、JSON CLI、多入口/headless 验证，不新开提交/审批/开发/外部权限。
