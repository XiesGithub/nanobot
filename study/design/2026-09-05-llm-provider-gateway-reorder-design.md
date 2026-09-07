# LLM 配置、Provider 与 Gateway 章节及手册重排设计

日期：2026-09-05

## 目标

在 nanobot 内部原理学习手册中新增一章，沿着“配置加载 → Provider 选择 → Backend 实例化 → 统一调用契约 → 请求适配与可靠性 → Gateway 装配与热更新”的路径，解释 LLM 配置、Provider 实现和 Gateway 组合根之间的关系。同时对现有章节执行完整的实体目录重排，使章节编号与推荐学习顺序一致。

## 范围

本次只修改 `study/` 下的学习手册文件，不修改 `nanobot/`、`tests/` 或 `webui/` 中的运行时代码。

改造包括：

- 新增 `02-llm-provider-gateway/`，共 8 个 HTML 页面。
- 重命名受影响的现有章节目录。
- 同步更新章节首页中的章号、各小节页脚编号、全书目录、首页和 README。
- 保持现有章节的 `data-chapter` 与 `data-page` 稳定，避免导航身份随展示编号变化。
- 不创建旧目录的重定向或占位页，避免静态校验和页面统计出现重复章节。

## 学习路线

| 新章号 | 章节 | 学习依赖 |
|---|---|---|
| 01 | 从一条用户消息理解内部 Agent | 建立全局数据流 |
| 02 | LLM 配置、Provider 实现与 Gateway 装配 | 理解模型能力从哪里进入系统 |
| 03 | Memory 系统 | 理解历史与长期记忆 |
| 04 | Context 管理 | 理解发送给模型的上下文 |
| 05 | Agent Loop 深入 | 理解模型与工具循环 |
| 06 | 工具系统、MCP 与能力扩展 | 理解 Agent 能力边界 |
| 07 | Subagent 子代理系统 | 理解委派与受限执行环境 |
| 08 | 自动化任务系统 | 综合应用 Loop、Tool、Memory 与 Gateway |
| 09 | Agent 聊天斜杠命令系统 | 理解绕过模型的确定性控制平面 |

这条路线先给出请求全景，再说明模型实现如何被装配；随后依次展开状态、上下文、执行循环和能力扩展，最后学习基于这些基础设施构建的委派、自动化与控制功能。

## 目录迁移

| 原目录 | 新目录 | 处理 |
|---|---|---|
| `01-request-lifecycle` | `01-request-lifecycle` | 保持 |
| — | `02-llm-provider-gateway` | 新增 |
| `02-memory-system` | `03-memory-system` | 重命名并更新章号 |
| `03-context-management` | `04-context-management` | 重命名并更新章号 |
| `04-agent-loop` | `05-agent-loop` | 重命名并更新章号 |
| `06-tools-mcp-extensions` | `06-tools-mcp-extensions` | 保持 |
| `08-subagent-system` | `07-subagent-system` | 重命名并更新章号 |
| `05-automation-system` | `08-automation-system` | 重命名并更新章号 |
| `07-slash-command-system` | `09-slash-command-system` | 重命名并更新章号 |

目录迁移只改变章节级路径。章节内部小节文件名保持不变，源码相对链接仍维持三级回退结构。

## 新第 02 章结构

### 章节首页：系统全景

用一张分层架构图建立主链：`config.json / NANOBOT_*` → `Config` → `_match_provider()` → `ProviderSpec` → `make_provider()` → `ProviderSnapshot` → `AgentLoop` → `AgentRunner`。解释配置、注册表、实现类和 Gateway 各自负责什么。

### 02.01：LLM 配置模型与加载

分析 `AgentDefaults`、`ProviderConfig`、`ProvidersConfig` 和根 `Config`；解释 camelCase/snake_case 兼容、Pydantic 环境变量嵌套、配置文件加载和 `${ENV_VAR}` 解析。区分模型生成参数、Provider 连接参数、Context 窗口和 Gateway 网络参数。

### 02.02：Provider 匹配算法

逐步解释 `Config._match_provider()`：显式 `provider`、模型名前缀、注册表关键词、本地 Provider fallback、Gateway/普通 Provider fallback。强调注册表顺序会影响匹配优先级，OAuth Provider 不参与通用 fallback。

### 02.03：Registry 与 Factory

分析 `ProviderSpec` 如何描述 backend、默认 API 地址、Gateway、本地服务、OAuth、模型前缀处理、thinking 风格和参数覆盖；再解释 `make_provider()` 如何验证凭证、选择具体实现并注入 `GenerationSettings`。同时说明 `provider_signature()` 与 `ProviderSnapshot` 的用途。

### 02.04：统一 Provider 契约

分析 `LLMProvider.chat()`、`chat_stream()`、`chat_with_retry()`，以及 `LLMResponse`、`ToolCallRequest`。说明为什么 AgentRunner 只依赖规范化的消息、工具调用、结束原因、usage 和错误元数据，而不需要知道具体厂商 SDK。

### 02.05：Provider 实现分类

对比 `OpenAICompatProvider`、`AnthropicProvider`、`AzureOpenAIProvider`、`BedrockProvider`、`OpenAICodexProvider` 与 `GitHubCopilotProvider`。重点分析公共抽象与协议差异，而不是逐行罗列每个厂商：消息格式、多模态、工具调用、thinking、OAuth 和模型名转换。

### 02.06：请求适配、流式输出与可靠性

解释 OpenAI Chat Completions 与 Responses API 的选择及降级、请求字段清洗、temperature/reasoning 兼容、Prompt Cache 标记、错误规范化、图片失败后的文本降级，以及 standard/persistent 两种重试策略。

### 02.07：Gateway 装配与运行时热更新

从 `cli/commands.py` 的 Gateway 组合根出发，解释配置解析、`build_provider_snapshot()`、`AgentLoop.from_config()` 依赖注入，以及每次 Turn 开始时 `_refresh_provider_snapshot()` 如何比较 signature。说明切换后如何同步主 Runner、Subagent、Consolidator 与 Dream，以及为什么正在执行的 Turn 不被中途替换。

## 页面表现

新页面继续使用现有 `study.css`、`navigation.css` 和 `study.js`，不引入新依赖。每页必须包含：

- `data-study-root="../.."`
- `data-chapter="llm-provider-gateway"`
- 唯一的 `data-page`
- 一组可操作的 `data-stepper`
- 至少一个指向当前项目源码或测试的相对链接
- 架构图、流程图、对比表、核心源码摘录和自测问题中的适用组合

源码摘录保持短小，展示决定架构行为的分支，不复制整份实现。

## 内容边界

- 详细解释主聊天 LLM Provider，不把图像生成和语音转录 Provider 展开成独立子系统；仅在边界说明中指出它们是不同接口。
- 不逐一介绍注册表中所有厂商配置；按实现类别与差异字段归纳。
- 不把 API Server、WebSocket 协议或 Channel 接入重复纳入本章；Gateway 只讲组合根与 Provider 生命周期。
- 不提供真实 API key，示例使用占位值和环境变量名称。

## 迁移策略

先创建临时、无冲突的中间目录名，再把现有章节移动到最终编号，避免 Windows 上目标目录冲突。移动后统一修改：

1. `assets/catalog.js` 中的顺序、章号与 href。
2. `index.html` 的推荐顺序、章节卡片和代码地图。
3. `README.md` 的目录树和完成章节列表。
4. 受影响章节首页的 `Chapter NN` 文案与所有页面的 `NN.MM` 页脚。

已有稳定语义 ID 不变化：`memory-system`、`context-management`、`agent-loop`、`tools-mcp-extensions`、`subagent-system`、`automation-system`、`slash-command-system`。

## 验证标准

完成后必须满足：

- `python -B study/tools/check_study.py` 通过，HTML 总数从 57 增至 65。
- `catalog.js`、`navigation.js`、`study.js` 均通过 `node --check`。
- Catalog 恰好注册 9 章、64 个唯一章节页面；加上全书首页后共有 65 个 HTML 页面。
- 新第 02 章恰好有 8 页，每页章节 ID、页面 ID、页导航、stepper 与源码链接完整。
- 所有目录前缀从 `01` 到 `09` 连续且与 Catalog 章号一致。
- 旧目录名不再存在，项目中不存在指向旧章节路径的链接。
- 不存在任何未完成章节、待补内容或计划态占位文字。
- `nanobot/`、`tests/`、`webui/` 没有因本次手册改造发生变化。

## 风险与处理

- 完整重排会使旧的本地书签失效；这是已确认的取舍，不创建兼容页。
- 机械替换章号可能误改源码行号或示例数字；替换限定在章节标题、eyebrow、footer 和目录元数据，并在迁移后搜索旧路径与旧章号。
- Provider 实现更新较快；章节以稳定接口和决策链为主，同时链接当前源码，减少依赖厂商列表数量的陈述。
