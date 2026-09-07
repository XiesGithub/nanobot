# 实现证据、简历差异与备考校准

本文件是作者核对记录，**不是面试官可见的事实前提，也不是额外题目**。核对基于 2026-09-06 当前工作树，HEAD 为 `fc6a8a5`；没有连接真实邮箱、调用 LLM 或启动服务，未运行项目测试。存在测试文件仅说明有测试代码，不能宣称测试已通过。

## 代码证据映射

| 范围 | 当前能核实的机制与证据 | 不能据此推出的结论 | 对应题目 |
|---|---|---|---|
| 主执行链 | [loop.py](../nanobot/agent/loop.py)、[runner.py](../nanobot/agent/runner.py)：模型与工具反馈循环、会话处理、运行结果 | 完整 DAG、Plan-and-Solve、已部署 Multi-Agent 业务 | P1、P2 |
| 并发 | [runner.py](../nanobot/agent/runner.py) 的 `_partition_tool_batches` 按 `concurrency_safe` 分批，批内 gather；[loop.py](../nanobot/agent/loop.py) 有会话锁及可选 Semaphore | 自动推导任意数据依赖；跨实例互斥；高并发压测结果 | T4、D4 |
| 消息总线 | [queue.py](../nanobot/bus/queue.py)：无 maxsize 参数的进程内 asyncio.Queue | 持久消息队列、有界背压、故障后自动重放 | R5、D4 |
| 提醒基础 | [CronService](../nanobot/cron/service.py)、[类型定义](../nanobot/cron/types.py)、[Cron 工具](../nanobot/agent/tools/cron.py)：at/every/cron、时区处理、持久化及执行历史 | 已完整接入个人日历、打卡状态与跨渠道送达对账 | P1、P3、T1 |
| 恢复 | [loop.py](../nanobot/agent/loop.py) 有 runtime checkpoint；[memory.py](../nanobot/agent/memory.py) 和 [session/manager.py](../nanobot/session/manager.py) 有持久化机制 | 外部发送与本地状态的原子事务；端到端恰好一次 | P3、R4 |
| 工具契约 | [registry.py](../nanobot/agent/tools/registry.py)：prepare_call、cast_params、validate_params、异常转工具反馈；[base.py](../nanobot/agent/tools/base.py) 定义基础契约 | 所有参数都能被安全强转；Schema 能验证用户真实意图 | T1、T2 |
| 邮件 | [email.py](../nanobot/channels/email.py)：IMAP 轮询、SMTP、同意开关、来源相关检查、内存 UID 去重与已读配置 | 邮件实时推送；持久消费账本；完整结构化摘要与待办落库业务 | T2、R4 |
| 长期记忆 | [memory.py](../nanobot/agent/memory.py)：MEMORY.md、history.jsonl、Consolidator、Dream；[context.py](../nanobot/agent/context.py) 加载记忆与近期历史 | 已实现 ChromaDB、语义分块、embedding 或 Query Top-K 注入 | P4、P5、D2 |
| 上下文控制 | [runner.py](../nanobot/agent/runner.py)：微压缩、历史裁剪、工具输出约束；[autocompact.py](../nanobot/agent/autocompact.py) 与 [helpers.py](../nanobot/utils/helpers.py) 提供压缩及估算基础 | 检索已接入；Token 估算完全精确；所有输入保证不超窗口 | R2 |
| 循环保护 | [runner.py](../nanobot/agent/runner.py)、[runtime.py](../nanobot/utils/runtime.py)：迭代上限、恢复次数、特定重复错误节流 | 通用业务无进展检测已完成；单次挂起工具必然会被总轮数截断 | R1 |
| 请求恢复 | [Provider 基类](../nanobot/providers/base.py)、[OpenAI 兼容 Provider](../nanobot/providers/openai_compat_provider.py) 有重试及错误语义基础 | 天气/邮件业务自动降级已经落地；对外发送可无条件重试 | T3 |
| 后台通知判断 | [evaluator.py](../nanobot/utils/evaluator.py)：额外模型判定是否通知，失败默认通知 | 离线自动评测平台；消息必然送达；不会漏掉任何重要事项 | E1、E2 |
| Web 服务 | [websocket.py](../nanobot/channels/websocket.py) 使用 websockets；[server.py](../nanobot/api/server.py) 使用 aiohttp，并有固定 API 会话 | 当前服务基于 FastAPI；默认具备多人租户隔离 | E4、D4、D5 |
| 可视化基础 | [前端流式 Hook 测试](../webui/src/tests/useNanobotStream.test.tsx)、[消息组件测试](../webui/src/tests/message-bubble.test.tsx)、[客户端](../webui/src/lib/nanobot-client.ts)、[进度事件](../nanobot/utils/progress_events.py) | 记忆、任务、提醒管理页面全部完成；统一 Trace 与离线补发协议完整可用 | E1、E4 |
| 子 Agent | [subagent.py](../nanobot/agent/subagent.py) 提供独立执行与状态基础 | 简历生活业务用了多 Agent；一定比单 Agent 更优 | P2 |
| 安全边界 | [security.md](../.agent/security.md)、[network.py](../nanobot/security/network.py)、[filesystem.py](../nanobot/agent/tools/filesystem.py) | 提示注入已完全解决；所有工具组合都满足最小权限 | T5 |

## 需要补证的简历亮点

**ChromaDB 记忆**：在 `nanobot/`、`tests/`、`webui/src/`、`docs/` 与 `pyproject.toml` 的相关检索中未找到对应完整链路。当前上下文直接加载文件记忆和近期历史。不能据此断定其他分支或独立服务没有实现；如有，应补充对应版本、编码器、集合组织、写入与查询入口及端到端样例。

**FastAPI Dashboard**：已读到的服务器入口是 websockets 和 aiohttp。简历若指另一个 Dashboard 服务，需要提供它的代码/部署位置、数据接口和与此仓库的集成证据。目前仅能确认通用 WebUI 与流式状态展示基础，不能补写不存在的生活管理页面。

**生活业务闭环**：框架有天气技能、邮件通道与 Cron 能力，但这些基础组件不等于已完成“邮箱→结构化摘要→任务→提醒→打卡→管理页”的整条业务流程。应补充日程来源、邮件抽取契约、去重账本、打卡状态与发送回执的真实实现。

**个人贡献和实际效果**：代码静态存在无法证明作者、上线情况或收益。面试准备应另行整理本人提交和运行证据。不要把题库中的扩展方案、示例状态机、建议指标或假设并发数写成项目成绩。

## 已有测试可提供的备考线索

- [Runner 测试](../tests/agent/test_runner.py)、[停止时上下文保留](../tests/agent/test_stop_preserves_context.py)：思考执行中断和工具结果边界。
- [工具参数校验](../tests/tools/test_tool_validation.py)、[注册表测试](../tests/tools/test_tool_registry.py)：区分工具合同与业务正确性。
- [邮件通道测试](../tests/channels/test_email_channel.py)、[Cron 持久化测试](../tests/cron/test_cron_persistence.py)：定位邮件读取与提醒存储的测试边界。
- [自动压缩测试](../tests/agent/test_auto_compact.py)、[记忆存储测试](../tests/agent/test_memory_store.py)：不要把摘要整理测试当成向量召回评测。
- [WebSocket 集成测试](../tests/channels/test_websocket_integration.py)：连接层验证不自动覆盖任务状态一致性和送达闭环。

如需运行这些项目命令，使用 Conda 的 `nanobot` 环境。真实 LLM 测试必须统一使用 `deepseek-v4-flash`，凭据不写入题库、配置示例或测试日志。本次只对生成文档做结构与链接核验，不把静态核对描述成系统运行验证。
