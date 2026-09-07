window.STUDY_CATALOG = {
  title: "nanobot 内部原理学习手册",
  chapters: [
    {
      id: "request-lifecycle",
      number: "01",
      title: "从一条用户消息理解内部 Agent",
      shortTitle: "请求生命周期",
      description: "沿着 HTTP 输入、统一消息、状态机、上下文、模型工具循环和结果持久化追踪一条消息。",
      href: "chapters/01-request-lifecycle/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/01-request-lifecycle/index.html" },
        { id: "http-request", title: "后端接收用户消息", href: "chapters/01-request-lifecycle/01-http-request.html" },
        { id: "agent-message", title: "转换成 Agent 消息", href: "chapters/01-request-lifecycle/02-agent-message.html" },
        { id: "message-lifecycle", title: "消息完整生命周期", href: "chapters/01-request-lifecycle/03-message-lifecycle.html" },
        { id: "context-building", title: "构建模型上下文", href: "chapters/01-request-lifecycle/04-context-building.html" },
        { id: "model-tool-loop", title: "模型与工具循环", href: "chapters/01-request-lifecycle/05-model-tool-loop.html" },
        { id: "provider-interface", title: "Provider 接口层", href: "chapters/01-request-lifecycle/06-provider-interface.html" },
        { id: "save-and-response", title: "保存并返回结果", href: "chapters/01-request-lifecycle/07-save-and-response.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/01-request-lifecycle/workshop.html" }
      ]
    },
    {
      id: "llm-provider-gateway",
      number: "02",
      title: "LLM 配置、Provider 实现与 Gateway 装配",
      shortTitle: "LLM 与 Provider",
      description: "理解配置加载、Provider 匹配与实例化、统一调用契约、协议适配、可靠性和 Gateway 热更新。",
      href: "chapters/02-llm-provider-gateway/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/02-llm-provider-gateway/index.html" },
        { id: "config-model", title: "LLM 配置模型与加载", href: "chapters/02-llm-provider-gateway/01-config-model.html" },
        { id: "provider-matching", title: "Provider 匹配算法", href: "chapters/02-llm-provider-gateway/02-provider-matching.html" },
        { id: "registry-factory", title: "Registry、Factory 与 Snapshot", href: "chapters/02-llm-provider-gateway/03-registry-factory.html" },
        { id: "provider-contract", title: "Provider 统一契约", href: "chapters/02-llm-provider-gateway/04-provider-contract.html" },
        { id: "provider-implementations", title: "Provider 实现分类", href: "chapters/02-llm-provider-gateway/05-provider-implementations.html" },
        { id: "request-reliability", title: "请求适配与可靠性", href: "chapters/02-llm-provider-gateway/06-request-reliability.html" },
        { id: "gateway-assembly", title: "Gateway 装配与热更新", href: "chapters/02-llm-provider-gateway/07-gateway-assembly.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/02-llm-provider-gateway/workshop.html" }
      ]
    },
    {
      id: "memory-system",
      number: "03",
      title: "Memory 系统",
      shortTitle: "Memory 系统",
      description: "理解 Session、MemoryStore、Consolidator、Dream 两阶段整理、AutoCompact 和版本恢复。",
      href: "chapters/03-memory-system/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/03-memory-system/index.html" },
        { id: "memory-architecture", title: "Memory 系统全景", href: "chapters/03-memory-system/01-memory-architecture.html" },
        { id: "memory-store", title: "MemoryStore 与存储布局", href: "chapters/03-memory-system/02-memory-store.html" },
        { id: "consolidator", title: "Consolidator 会话归档", href: "chapters/03-memory-system/03-consolidator.html" },
        { id: "dream", title: "Dream 两阶段记忆整理", href: "chapters/03-memory-system/04-dream.html" },
        { id: "autocompact-recovery", title: "AutoCompact 与恢复机制", href: "chapters/03-memory-system/05-autocompact-recovery.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/03-memory-system/workshop.html" }
      ]
    },
    {
      id: "context-management",
      number: "04",
      title: "Context 管理",
      shortTitle: "Context 管理",
      description: "理解 System Prompt、Session replay、token 分层治理、多模态、工具结果和消息协议修复。",
      href: "chapters/04-context-management/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/04-context-management/index.html" },
        { id: "context-sources", title: "Context 来源与 System Prompt", href: "chapters/04-context-management/01-context-sources.html" },
        { id: "session-replay", title: "Session 历史重放", href: "chapters/04-context-management/02-session-replay.html" },
        { id: "token-budget", title: "Token 预算与分层压缩", href: "chapters/04-context-management/03-token-budget.html" },
        { id: "multimodal-tools", title: "多模态、工具结果与持久化", href: "chapters/04-context-management/04-multimodal-tools.html" },
        { id: "protocol-safety", title: "消息协议合法性", href: "chapters/04-context-management/05-protocol-safety.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/04-context-management/workshop.html" }
      ]
    },
    {
      id: "agent-loop",
      number: "05",
      title: "Agent Loop 深入",
      shortTitle: "Agent Loop",
      description: "深入 AgentLoop 与 AgentRunner 的边界、状态机、并发、消息注入、中断和检查点恢复。",
      href: "chapters/05-agent-loop/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/05-agent-loop/index.html" },
        { id: "two-layer-loop", title: "两层循环与职责边界", href: "chapters/05-agent-loop/01-two-layer-loop.html" },
        { id: "turn-state-machine", title: "外层事件循环与 Turn 状态机", href: "chapters/05-agent-loop/02-turn-state-machine.html" },
        { id: "model-tool-loop", title: "内层模型—工具循环", href: "chapters/05-agent-loop/03-model-tool-loop.html" },
        { id: "concurrency-injection", title: "并发隔离与消息注入", href: "chapters/05-agent-loop/04-concurrency-injection.html" },
        { id: "interrupt-checkpoint", title: "中断、Ask User 与 Checkpoint", href: "chapters/05-agent-loop/05-interrupt-checkpoint.html" },
        { id: "hooks-streaming", title: "Hooks、流式输出与可观测性", href: "chapters/05-agent-loop/06-hooks-streaming.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/05-agent-loop/workshop.html" }
      ]
    },
    {
      id: "tools-mcp-extensions",
      number: "06",
      title: "工具系统、MCP 与能力扩展",
      shortTitle: "工具与 MCP",
      description: "理解 Tool 契约、Registry、插件发现、MCP 三类能力适配，以及 Tool、MCP、Skill 的扩展边界。",
      href: "chapters/06-tools-mcp-extensions/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/06-tools-mcp-extensions/index.html" },
        { id: "tool-contract", title: "Tool 抽象与 Schema 契约", href: "chapters/06-tools-mcp-extensions/01-tool-contract.html" },
        { id: "registry-execution", title: "Registry 与调用生命周期", href: "chapters/06-tools-mcp-extensions/02-registry-execution.html" },
        { id: "loader-context", title: "ToolLoader、插件与运行时上下文", href: "chapters/06-tools-mcp-extensions/03-loader-context.html" },
        { id: "mcp-connection", title: "MCP 连接架构与生命周期", href: "chapters/06-tools-mcp-extensions/04-mcp-connection.html" },
        { id: "mcp-wrappers", title: "MCP Wrappers 与可靠性", href: "chapters/06-tools-mcp-extensions/05-mcp-wrappers.html" },
        { id: "extension-guide", title: "能力扩展选择与实现指南", href: "chapters/06-tools-mcp-extensions/06-extension-guide.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/06-tools-mcp-extensions/workshop.html" }
      ]
    },
    {
      id: "subagent-system",
      number: "07",
      title: "Subagent 子代理系统",
      shortTitle: "Subagent",
      description: "理解 SpawnTool、SubagentManager、隔离 AgentRunner、结果回注、持久化、取消和安全边界。",
      href: "chapters/07-subagent-system/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/07-subagent-system/index.html" },
        { id: "architecture-boundaries", title: "Subagent 架构与边界", href: "chapters/07-subagent-system/01-architecture-boundaries.html" },
        { id: "spawn-context", title: "SpawnTool 与父请求身份", href: "chapters/07-subagent-system/02-spawn-context.html" },
        { id: "isolated-runtime", title: "隔离运行环境", href: "chapters/07-subagent-system/03-isolated-runtime.html" },
        { id: "execution-observability", title: "执行与可观测性", href: "chapters/07-subagent-system/04-execution-observability.html" },
        { id: "result-injection", title: "结果回注父 Agent", href: "chapters/07-subagent-system/05-result-injection.html" },
        { id: "persistence-lifecycle", title: "持久化与生命周期", href: "chapters/07-subagent-system/06-persistence-lifecycle.html" },
        { id: "config-safety", title: "配置、安全与限制", href: "chapters/07-subagent-system/07-config-safety.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/07-subagent-system/workshop.html" }
      ]
    },
    {
      id: "automation-system",
      number: "08",
      title: "自动化任务系统",
      shortTitle: "自动化任务",
      description: "理解用户 Cron、Heartbeat 与 Dream 系统任务的调度、Agent 执行、渠道回注和可靠性设计。",
      href: "chapters/08-automation-system/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/08-automation-system/index.html" },
        { id: "automation-architecture", title: "自动化体系全景", href: "chapters/08-automation-system/01-automation-architecture.html" },
        { id: "cron-model", title: "Cron 数据模型与时间语义", href: "chapters/08-automation-system/02-cron-model.html" },
        { id: "cron-service", title: "CronService 调度与持久化", href: "chapters/08-automation-system/03-cron-service.html" },
        { id: "cron-agent-delivery", title: "CronTool、Agent 执行与回注", href: "chapters/08-automation-system/04-cron-agent-delivery.html" },
        { id: "heartbeat-dream", title: "Heartbeat、Dream 与可靠性", href: "chapters/08-automation-system/05-heartbeat-dream.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/08-automation-system/workshop.html" }
      ]
    },
    {
      id: "slash-command-system",
      number: "09",
      title: "Agent 聊天斜杠命令系统",
      shortTitle: "斜杠命令",
      description: "理解 CommandContext、四级 Router、AgentLoop 双入口分流、内置命令、渠道菜单与扩展边界。",
      href: "chapters/09-slash-command-system/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "章节总览", href: "chapters/09-slash-command-system/index.html" },
        { id: "command-contract", title: "命令契约与元数据", href: "chapters/09-slash-command-system/01-command-contract.html" },
        { id: "router-matching", title: "Router 与四级匹配", href: "chapters/09-slash-command-system/02-router-matching.html" },
        { id: "loop-dispatch", title: "AgentLoop 命令分流", href: "chapters/09-slash-command-system/03-loop-dispatch.html" },
        { id: "lifecycle-commands", title: "生命周期命令", href: "chapters/09-slash-command-system/04-lifecycle-commands.html" },
        { id: "query-commands", title: "查询命令", href: "chapters/09-slash-command-system/05-query-commands.html" },
        { id: "memory-commands", title: "Memory 命令", href: "chapters/09-slash-command-system/06-memory-commands.html" },
        { id: "channels-extension", title: "渠道呈现与命令扩展", href: "chapters/09-slash-command-system/07-channels-extension.html" },
        { id: "workshop", title: "源码精讲与设计实作", href: "chapters/09-slash-command-system/workshop.html" }
      ]
    },
    {
      id: "build-your-agent",
      number: "10",
      title: "从零设计并实现你的 Agent",
      shortTitle: "从零实现 Agent",
      description: "运行标准库教学项目，推演完整轨迹，按里程碑实现生产能力。",
      href: "chapters/10-build-your-agent/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "独立实现与扩展验收", href: "chapters/10-build-your-agent/index.html" }
      ]
    },
    {
      id: "interview",
      number: "11",
      title: "项目面试题库与模块复习",
      shortTitle: "项目面试题库",
      description: "40 道主问题按八类主题递进，答案默认折叠，每题关联手册内模块讲解。",
      href: "chapters/11-interview/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "40 题 · 分类练习与模块复习", href: "chapters/11-interview/index.html" }
      ]
    }
  ]
};
