# Review Answer Reveals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为学习手册 211 道复习题添加基于当前源码、默认折叠且可访问的答案。

**Architecture:** 答案以内联原生 `details/summary` 保存在每个章节页面，共享 CSS 只负责外观和状态文案。`check_study.py` 扩展为结构与覆盖率验收器，从而防止漏题、空答案和重复答案。

**Tech Stack:** 静态 HTML5、共享 CSS、Python 标准库 `html.parser`、Conda 环境 `nanobot`

**Spec:** `study/design/2026-09-05-review-answer-reveals.md`

## Global Constraints

- 仅修改 `study/`。
- 项目命令通过 Conda 环境 `nanobot` 运行。
- 覆盖 61 个复习块、211 道题；不改造 7 项“上线前安全清单”。
- 答案描述当前仓库实现，不调用外部 LLM，不读取或输出 `api.txt`。

---

### Task 1: 建立答案覆盖率校验

**Files:**
- Modify: `study/tools/check_study.py`

**Interfaces:**
- Consumes: 手册现有标题和 `ul.check-list` 结构。
- Produces: 复习块数、问题数、答案结构和非空内容的失败报告。

- [ ] 扩展 HTMLParser，识别目标问题块及每个顶层问题。
- [ ] 运行 `conda run -n nanobot python -B study/tools/check_study.py`，确认因缺少 211 个答案而失败。
- [ ] 保留原有重复 ID、页面元数据和本地链接校验。

### Task 2: 实现共享折叠答案样式

**Files:**
- Modify: `study/assets/study.css`

**Interfaces:**
- Consumes: `.review-question`、`.answer-reveal`、`.answer-content`。
- Produces: 默认折叠、展开状态、键盘 focus、深浅主题兼容的展示。

- [ ] 添加问题文本和折叠答案容器样式。
- [ ] 使用 `details[open]` 与 summary 伪元素表达展开/收起状态。
- [ ] 检查窄屏下内容不会横向溢出。

### Task 3: 补全请求、Provider、Memory 与 Context 答案

**Files:**
- Modify: `study/chapters/01-request-lifecycle/*.html`
- Modify: `study/chapters/02-llm-provider-gateway/*.html`
- Modify: `study/chapters/03-memory-system/*.html`
- Modify: `study/chapters/04-context-management/*.html`

**Interfaces:**
- Consumes: 各页面引用的 `nanobot/` 源码。
- Produces: 前四章共 100 道内联答案。

- [ ] 对照入口、Provider、Session、Memory、Context 源码编写答案。
- [ ] 运行覆盖校验，确认已改页面不再报告缺失答案。

### Task 4: 补全 Agent Loop、Tools/MCP、Subagent 答案

**Files:**
- Modify: `study/chapters/05-agent-loop/*.html`
- Modify: `study/chapters/06-tools-mcp-extensions/*.html`
- Modify: `study/chapters/07-subagent-system/*.html`

**Interfaces:**
- Consumes: Runner、ToolRegistry、MCP、Subagent 源码。
- Produces: 第五至七章共 67 道内联答案。

- [ ] 对照循环、工具执行、MCP 包装与子任务生命周期源码编写答案。
- [ ] 保持“上线前安全清单”不变。

### Task 5: 补全 Automation 与 Slash Command 答案

**Files:**
- Modify: `study/chapters/08-automation-system/*.html`
- Modify: `study/chapters/09-slash-command-system/*.html`

**Interfaces:**
- Consumes: CronService、Gateway 回调、CommandRouter 与 builtin handlers。
- Produces: 最后两章共 44 道内联答案。

- [ ] 对照调度、投递、Heartbeat、Dream 和命令分流源码编写答案。
- [ ] 覆盖 `07-channels-extension.html` 的 3 道复习题。

### Task 6: 完整验收

**Files:**
- Verify: `study/`

**Interfaces:**
- Consumes: 所有改造页面与共享资源。
- Produces: 可复现的静态验收结果。

- [ ] 运行 `conda run -n nanobot python -B study/tools/check_study.py`，期望 65 个页面、61 个复习块、211 道问题和 211 个答案全部通过。
- [ ] 运行 `node --check study/assets/study.js` 与 `node --check study/assets/catalog.js`。
- [ ] 搜索 `answer-reveal`、`review-question` 和 `open` 属性，确认数量与默认折叠状态。
- [ ] 检查 `git diff --check -- study`，确认无空白错误。
