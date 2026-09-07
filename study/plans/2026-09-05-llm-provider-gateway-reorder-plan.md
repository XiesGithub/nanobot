# LLM Provider/Gateway Chapter and Handbook Reorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增“LLM 配置、Provider 实现与 Gateway 装配”章节，并把 nanobot 学习手册重排为编号连续、依赖清晰的 9 章结构。

**Architecture:** 保持现有静态站点的 Catalog 驱动导航架构，新章节复用公共 CSS/JS 和页面约定。章节重排改变实体目录和展示章号，但保持 `data-chapter`、`data-page` 等语义身份稳定；所有迁移与新内容最终由无第三方依赖的静态校验覆盖。

**Tech Stack:** 静态 HTML、CSS、浏览器 JavaScript、Python 3 标准库校验脚本、PowerShell 文件迁移。

**Spec:** `study/design/2026-09-05-llm-provider-gateway-reorder-design.md`

## Global Constraints

- 只修改 `study/`，不修改 `nanobot/`、`tests/` 或 `webui/`。
- 新第 02 章必须包含 8 个 HTML 页面，使用 `data-chapter="llm-provider-gateway"`。
- 保留所有已有章节的 `data-chapter` 与 `data-page` 值。
- 不创建旧路径兼容页。
- 所有源码摘录必须能由当前仓库源码支持，并提供本地相对源码链接。
- 最终应有 9 章、65 个 HTML 页面，章节目录前缀连续为 `01` 至 `09`。

---

### Task 1: 安全迁移现有章节目录

**Files:**

- Move: `study/chapters/02-memory-system` → `study/chapters/03-memory-system`
- Move: `study/chapters/03-context-management` → `study/chapters/04-context-management`
- Move: `study/chapters/04-agent-loop` → `study/chapters/05-agent-loop`
- Move: `study/chapters/08-subagent-system` → `study/chapters/07-subagent-system`
- Move: `study/chapters/05-automation-system` → `study/chapters/08-automation-system`
- Move: `study/chapters/07-slash-command-system` → `study/chapters/09-slash-command-system`

**Interfaces:**

- Consumes: 设计说明中的目录迁移表。
- Produces: 无冲突、编号连续的新目录布局，供 Catalog 和页面文案更新使用。

- [ ] **Step 1: 校验所有源目录存在且最终目标目录不存在**

Run:

```powershell
Get-ChildItem study/chapters -Directory | Select-Object -ExpandProperty Name
```

Expected: 现有 8 个目录完整，且 `02-llm-provider-gateway`、`03-memory-system` 等目标位置尚未占用。

- [ ] **Step 2: 通过唯一临时目录名执行两阶段移动**

先将六个源目录分别移动到 `study/chapters/__reorder__-*`，再次检查路径均位于 `study/chapters`，再移动到最终目录。这避免 `02 → 03 → 04 → 05` 的目标冲突。

- [ ] **Step 3: 校验最终目录集合**

Expected:

```text
01-request-lifecycle
03-memory-system
04-context-management
05-agent-loop
06-tools-mcp-extensions
07-subagent-system
08-automation-system
09-slash-command-system
```

### Task 2: 更新已迁移章节的展示编号

**Files:**

- Modify: `study/chapters/03-memory-system/*.html`
- Modify: `study/chapters/04-context-management/*.html`
- Modify: `study/chapters/05-agent-loop/*.html`
- Modify: `study/chapters/07-subagent-system/*.html`
- Modify: `study/chapters/08-automation-system/*.html`
- Modify: `study/chapters/09-slash-command-system/*.html`

**Interfaces:**

- Consumes: Task 1 的最终目录。
- Produces: 与新目录号一致的 eyebrow、标题和 footer；语义 ID 保持不变。

- [ ] **Step 1: 对限定文案执行机械编号替换**

映射：Memory `02→03`、Context `03→04`、Agent Loop `04→05`、Subagent `08→07`、Automation `05→08`、Slash Command `07→09`。只修改 `Chapter NN`、`NN.MM` 与章节首页可见章号，不修改源码行号、配置数字或小节编号。

- [ ] **Step 2: 校验语义身份未改变**

Run a Python `HTMLParser` audit asserting each page retains the original `data-chapter` and unique `data-page`.

- [ ] **Step 3: 搜索遗留展示章号**

Run targeted `rg` expressions per directory, and inspect any match before correction.

### Task 3: 编写新第 02 章前四页

**Files:**

- Create: `study/chapters/02-llm-provider-gateway/index.html`
- Create: `study/chapters/02-llm-provider-gateway/01-config-model.html`
- Create: `study/chapters/02-llm-provider-gateway/02-provider-matching.html`
- Create: `study/chapters/02-llm-provider-gateway/03-registry-factory.html`

**Interfaces:**

- Consumes: `nanobot/config/schema.py`, `nanobot/config/loader.py`, `nanobot/providers/registry.py`, `nanobot/providers/factory.py`。
- Produces: `overview`、`config-model`、`provider-matching`、`registry-factory` 四个页面 ID。

- [ ] **Step 1: 创建章节首页**

首页必须展示完整链路、8 页路线图、核心源码地图和读后自测目标。

- [ ] **Step 2: 编写配置模型页**

包含配置分层表、文件/环境变量加载流程、生成参数与连接参数边界、核心源码摘录和常见误区。

- [ ] **Step 3: 编写 Provider 匹配页**

用 stepper 解释显式 Provider、模型前缀、关键词、本地 fallback 和最终 fallback；标明 OAuth 不参与通用 fallback。

- [ ] **Step 4: 编写 Registry/Factory 页**

展示 `ProviderSpec`、backend 分派、凭证验证、`GenerationSettings`、`ProviderSnapshot` 与 signature。

- [ ] **Step 5: 运行四页结构检查**

Assert each page has correct body attributes, one page nav, one non-empty stepper with matching panels, and at least one valid source link.

### Task 4: 编写新第 02 章后四页

**Files:**

- Create: `study/chapters/02-llm-provider-gateway/04-provider-contract.html`
- Create: `study/chapters/02-llm-provider-gateway/05-provider-implementations.html`
- Create: `study/chapters/02-llm-provider-gateway/06-request-reliability.html`
- Create: `study/chapters/02-llm-provider-gateway/07-gateway-assembly.html`

**Interfaces:**

- Consumes: `nanobot/providers/base.py`、各 backend Provider、`nanobot/providers/openai_responses/`、`nanobot/cli/commands.py`、`nanobot/agent/loop.py`。
- Produces: `provider-contract`、`provider-implementations`、`request-reliability`、`gateway-assembly` 四个页面 ID。

- [ ] **Step 1: 编写统一契约页**

解释 `ToolCallRequest`、`LLMResponse`、`GenerationSettings`、`LLMProvider` 和 AgentRunner 依赖边界。

- [ ] **Step 2: 编写实现分类页**

比较 OpenAI-compatible、Anthropic、Azure、Bedrock、OpenAI Codex、GitHub Copilot 的协议、认证、模型名与响应转换差异。

- [ ] **Step 3: 编写可靠性页**

覆盖消息清洗、Chat Completions/Responses 选择和 circuit fallback、流式统一、结构化错误、Retry-After、standard/persistent 重试及图片降级。

- [ ] **Step 4: 编写 Gateway 装配页**

展示 `build_provider_snapshot()` → `AgentLoop.from_config()` → Turn 前 refresh → `_apply_provider_snapshot()` 的数据流，以及 Runner/Subagent/Consolidator/Dream 同步更新。

- [ ] **Step 5: 运行八页内容覆盖检查**

Required terms: `AgentDefaults`, `ProviderConfig`, `_match_provider`, `ProviderSpec`, `make_provider`, `ProviderSnapshot`, `LLMProvider`, `LLMResponse`, `ToolCallRequest`, `OpenAICompatProvider`, `chat_with_retry`, `chat_stream_with_retry`, `_refresh_provider_snapshot`, `_apply_provider_snapshot`.

### Task 5: 重建 Catalog 与首页学习路线

**Files:**

- Modify: `study/assets/catalog.js`
- Modify: `study/index.html`

**Interfaces:**

- Consumes: Tasks 1–4 的最终路径和页面 ID。
- Produces: 9 章顺序、64 个章节页面的可达导航、正确的首页章节卡片与代码地图。

- [ ] **Step 1: 在 Catalog 中插入新第 02 章**

注册 8 个页面，并将后续章节对象按新学习路线排序、更新 `number` 和所有 `href`。

- [ ] **Step 2: 更新首页推荐顺序**

推荐文字应呈现“全景 → 模型装配 → 状态/上下文 → Loop → 工具 → 委派 → 自动化 → 控制平面”。

- [ ] **Step 3: 更新首页章节卡片和代码地图**

加入 Provider/Gateway 卡片；更新所有受影响章号与 href；代码地图加入 config、registry、factory 和 provider backends。

- [ ] **Step 4: 校验 JavaScript 语法**

Run:

```powershell
node --check study/assets/catalog.js
node --check study/assets/navigation.js
node --check study/assets/study.js
```

Expected: all commands exit 0.

### Task 6: 更新维护文档

**Files:**

- Modify: `study/README.md`

**Interfaces:**

- Consumes: 最终目录布局和页数。
- Produces: 与实际目录一致的维护说明。

- [ ] **Step 1: 更新目录树**

列出 9 个章节目录及各章页数，新第 02 章标记为 8 页。

- [ ] **Step 2: 更新完成章节列表**

按新学习路线排序，并增加 LLM/Provider/Gateway 章节摘要。

- [ ] **Step 3: 确认扩展与验证说明仍然有效**

保持 Catalog 单一注册入口和 `check_study.py` 命令不变。

### Task 7: 全站回归验证

**Files:**

- Verify: `study/**/*.html`
- Verify: `study/assets/*.js`
- Verify: `study/README.md`

**Interfaces:**

- Consumes: Tasks 1–6 的完整结果。
- Produces: 可交付的 9 章静态学习手册。

- [ ] **Step 1: 运行全站静态链接检查**

Run:

```powershell
python -B study/tools/check_study.py
```

Expected: `Study handbook validation passed: 65 HTML pages checked`.

- [ ] **Step 2: 运行 Catalog/HTML 身份审计**

Assert 9 chapter IDs、64 unique chapter-page registrations、65 HTML pages、连续章号、无重复 `data-page` in each chapter, and Catalog href targets exist.

- [ ] **Step 3: 搜索旧路径和占位内容**

Search all of `study/` for the six retired directory names and unfinished-content markers. Expected: no matches outside the historical design/plan migration tables; runtime pages and navigation must contain none.

- [ ] **Step 4: 运行文件卫生检查**

Assert modified HTML/JS/Markdown files contain no replacement characters and no trailing whitespace.

- [ ] **Step 5: 检查工作区范围**

Run:

```powershell
git status --short -- study nanobot tests webui
```

Expected: changes are limited to `study/`; no runtime source directories are modified by this task.
