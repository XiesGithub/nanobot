# nanobot 原理与 Agent 实作学习手册

从 [index.html](index.html) 开始。整份 `study/` 可以单独复制：页面为静态 HTML，源码摘录和 SVG 图都已嵌入，不需要 nanobot 后端、项目源码、联网、CDN 或 API 密钥。

## 怎样学

具备 Python 类、字典、异常和基本 async/await 知识即可开始。每章按“专题概念 → 源码精讲 → 数据演算 → 设计任务与验收”阅读。首页提供按实现难度排列的五个里程碑。

| 章节 | 学习能力 | 精讲与实作 |
|---|---|---|
| 01 请求生命周期 | 拆分入口、会话、编排、执行和响应 | [本章实作](chapters/01-request-lifecycle/workshop.html) |
| 02 LLM、Provider、Gateway | 定义模型契约、协议适配、装配与可靠性 | [本章实作](chapters/02-llm-provider-gateway/workshop.html) |
| 03 Memory | 区分事实与长期知识，设计归档和恢复 | [本章实作](chapters/03-memory-system/workshop.html) |
| 04 Context | 构建有预算且协议合法的模型输入 | [本章实作](chapters/04-context-management/workshop.html) |
| 05 Agent Loop | 实现有界工具循环、并发与中断状态 | [本章实作](chapters/05-agent-loop/workshop.html) |
| 06 工具与 MCP | 设计执行契约、校验和扩展边界 | [本章实作](chapters/06-tools-mcp-extensions/workshop.html) |
| 07 Subagent | 隔离任务上下文，关联父会话并回注结果 | [本章实作](chapters/07-subagent-system/workshop.html) |
| 08 自动化 | 区分调度、执行和投递，处理失败时序 | [本章实作](chapters/08-automation-system/workshop.html) |
| 09 斜杠命令 | 实现确定性路由和忙闲控制平面 | [本章实作](chapters/09-slash-command-system/workshop.html) |
| 10 从零实现 Agent | 运行完整模块化示例，再按里程碑扩展 | [综合实作](chapters/10-build-your-agent/index.html) |
| 11 项目面试题库 | 用 40 道递进问题检验理解，按题复习相关模块 | [面试题库与模块复习](chapters/11-interview/index.html) |

真实源码摘录标记文件、符号和实际行区间，配有上下文解释；它们是连续原文的局部片段，不是独立程序。原专题中的简化骨架用于解释流程。第10章则是完整可运行的教学实现，所有模块和测试代码都在页面内。

源码地图优先跳转到对应文件的站内摘录；未逐行收录的文件明确指向“模块讲解”。`data-original-href` 只是维护者使用的来源记录，不会向仓库跳转或加载文件。

## 面试练习与按题复习

[第11章](chapters/11-interview/index.html)集成 40 道主问题、8 类主题与 80 个条件式追问，保留默认折叠答案、搜索、难度筛选、批量展开和打印。每题显示 2—3 个相关模块讲解链接；从题目跳到讲解页后，页首提供“返回面试题 Qxx”，可回到原题。

所有讲解链接都留在 `study/` 内，包括原来证据附录中的来源说明也不再要求打开仓库。对 ChromaDB、邮件摘要闭环、多租户和监控平台等尚无对应实现专题的内容，明确说明链接提供的是相关基础，不冒充已实现方案。

维护环境中，题目仍以 `interview/` 内的 Markdown 为唯一内容源；`interview/study-links.json` 维护每题模块映射，`interview/study-reader.template.html` 维护手册版布局。运行 `conda run -n nanobot python interview/build_html.py` 会同时更新独立题库与手册第11章。**阅读和离线复制不需要这些构建源文件。**

## 运行教学 Agent

从仓库根目录执行；也可复制 `examples/mini_agent/` 到任意目录后执行同名文件。

```powershell
conda run -n nanobot python study/examples/mini_agent/app.py
conda run -n nanobot python -m unittest discover -s study/examples/mini_agent -v
```

默认输出包含工具往返并以 `answer: Result: 5` 结束。示例只依赖 Python 3.11 标准库，以 ScriptedProvider 确定性验证控制流，不发出真实 LLM 请求。真实 Provider 接入及总线、长期记忆、MCP、子代理等生产能力是第10章中的后续扩展任务，并附参考设计与验收场景。

## 验证与维护

```powershell
# 链接、锚点、目录覆盖、离线资源、211道原有答案、精讲基本结构
conda run -n nanobot python study/tools/check_study.py

# 40道面试题、默认折叠、逐题模块链接和返回参数
conda run -n nanobot python study/tools/check_interview.py

# 仅在有仓库的维护环境：逐字核对带来源元数据的源码摘录
conda run -n nanobot python study/tools/check_study.py --check-source

# 真实导航脚本在每页元数据上运行，验证生成链接和选中状态
conda run -n nanobot node study/tools/check_navigation.cjs

# 检查器负向回归、示例完整源码一致性与跨进程恢复
conda run -n nanobot python -m unittest discover -s study/tools -p test_learning_checks.py -v
conda run -n nanobot python study/examples/verify_example.py

# 临时复制整份手册，在无nanobot源码的目录中复验
conda run -n nanobot python study/tools/verify_portable.py
```

这些检查验证结构和运行行为，不替代真实浏览器的视觉验收。

新增页面须声明 `data-study-root`、`data-chapter`、`data-page`，引入现有静态资源，并在 `assets/catalog.js` 注册。图示用内联 SVG 并提供 title/desc 和文字说明，用 `.diagram-scroll` 包裹以适应窄屏。源码摘录容器应包含 `id`、`data-source`、`data-start`、`data-end`，内部第一个 `code` 存放未改写原文。

源码更新后先维护摘录与解释，再运行 `conda run -n nanobot python study/tools/integrate_handbook.py` 更新站内来源链接，最后执行上述校验。不要仅调整行号使检查通过而保留过时说明。

改进依据见 [学习手册审计](design/2026-09-05-handbook-audit.md)。

## 阅读导航

所有 75 个章节页面底部都有常驻阅读导航，显示当前章节、页面和小节进度。点击“本页目录”可直接选择小节；“上一节 / 下一节”按正文顺序推进，读完一页后切换为“下一页”，章末切换为“下一章”。“本章概览”始终可用。左侧目录优先显示本章页面，手机上也可通过底部导航连续阅读。

例如，从概览点击“输入输出契约”进入实作页后，底部“下一节”就是“模块数据流图”。这两个入口是同一篇实作长文中的小节；“下一页”和“下一章”用于跨页面、跨章节阅读。从专题讲解跳入的链接保留“返回来源页”，从面试题跳入的链接保留“返回原题”，无需依赖浏览器历史。

新增或调整正文二级标题后，运行 `conda run -n nanobot python study/tools/build_reading_navigation.py`，更新页内锚点与 `assets/sections.js`。原有锚点保留；题库生成器和手册整合脚本会自动执行此步骤。

```powershell
# 验证全书小节顺序、跨章边界、概览及来源返回链接
conda run -n nanobot node study/tools/check_reading_navigation.cjs
```

导航问题分析、修复范围和验证记录见 [阅读导航审查记录](design/2026-09-07-reading-navigation-audit.md)。
