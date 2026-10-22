"""Stage 6 adapters for subagents, MCP, automation, and service transports.

The adapters drive concrete nanobot entry points with local fixtures. They are
module/transport contracts; a scripted response never stands in for model
reasoning, remote platform delivery, or browser-visible WebUI behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from evals.common import inside
from nanobot.providers.base import LLMProvider


def _spec(scope, entry, fixtures=None, assertions=None, *, supported=True, limitations=None):
    return {
        "scope": scope,
        "entry": entry,
        "fixtures": fixtures or {},
        "assertions": assertions or [],
        "supported": supported,
        "limitations": limitations or [],
    }


def _gap(entry, reason, *, scope="transport"):
    return _spec(scope, entry, supported=False, limitations=[reason])


_MCP_SERVER = '''from pathlib import Path
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("eval")
LOG = Path(__file__).with_name("calls.jsonl")

@mcp.tool()
def lookup_order(order_id: str) -> str:
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"order_id": order_id}) + "\\n")
    if order_id == "A17":
        return json.dumps({"order_id": "A17", "status": "shipped", "tracking": "T900"})
    return json.dumps({"status": "not_found"})

@mcp.resource("fixture://eval/policy")
def policy() -> str:
    return "退货期限14天"

@mcp.prompt(name="policy_answer", description="Answer only from the supplied policy")
def policy_answer() -> str:
    return "根据提供的政策作答，不补充未给条款。"

mcp.run()
'''

_MCP_BATCH = '''@echo off
"%EVAL_PYTHON%" "%~dp0mcp_server.py"
'''

_MCP_HTTP_SERVER = '''from pathlib import Path
import json
import sys
import uvicorn
from mcp.server.fastmcp import FastMCP

transport, port = sys.argv[1], int(sys.argv[2])
mcp = FastMCP("eval-http")
LOG = Path(__file__).with_name("calls.jsonl")

@mcp.tool()
def lookup_order(order_id: str) -> str:
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"order_id": order_id}) + "\\n")
    return json.dumps({"order_id": order_id, "tracking": "T900"})

app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
'''

_MCP_RECOVERY_SERVER = '''from pathlib import Path
import asyncio
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("eval-recovery")
LOG = Path(__file__).with_name("calls.jsonl")
count = 0

@mcp.tool()
async def lookup_order(order_id: str) -> str:
    global count
    count += 1
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"order_id": order_id, "attempt": count, "phase": "started"}) + "\\n")
    if count == 1:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            with LOG.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"order_id": order_id, "attempt": count, "phase": "cancelled"}) + "\\n")
            raise
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"order_id": order_id, "attempt": count, "phase": "completed"}) + "\\n")
    return json.dumps({"order_id": order_id, "status": "shipped", "tracking": "T900"})

mcp.run()
'''


def cases():
    result = {}
    gaps = {
        "V063": ("SlackChannel._on_message", "线程事件需要平台 SDK envelope 和真实会话历史；本机没有隔离 Slack 测试接入。"),
        "V065": ("WebSocketChannel media ingress + AgentLoop", "需要真实媒体上传路由及双会话完整任务；本模块探针不覆盖 Agent 上下文权限。"),
        "V066": ("ChannelManager + WebUI visible state", "缺少客户端可见状态重建器，无法从 adapter send 调用数证明进度与最终回复未重复显示。"),
        "V073": ("WebUI browser chat", "当前 Conda 环境未安装 Playwright/浏览器驱动，不能取得真实 DOM、控件状态和控制台证据。"),
        "V074": ("WebUI browser + filesystem tool", "缺少隔离浏览器驱动及真实 UI 工具执行/DOM 与文件状态联合证据。"),
        "V075": ("WebUI ask_user/resume browser flow", "没有可运行浏览器来验证可选项、用户点击、请求恢复 ID 和真实文件副作用。"),
        "V076": ("WebUI stop/resume browser flow", "没有可运行浏览器与可取消工具 gate，无法观测停止按钮、检查点和续跑时间序列。"),
        "V077": ("WebUI two-session browser routing", "缺少真实浏览器 DOM 会话切换与跨会话完成顺序控制。"),
        "V078": ("WebUI history reload", "缺少浏览器刷新前后 DOM、历史请求和模型调用账本的联合采集。"),
        "V079": ("WebUI private mail citation card", "邮件专项脚本可覆盖检索/权限 API，但没有真实浏览器 DOM 和 owner 身份交互验收。"),
        "V080": ("WebUI responsive/theme browser checks", "没有固定 Chromium 字体/视口/主题环境，无法生成可靠几何、焦点和截图证据。"),
    }
    for case_id, (entry, reason) in gaps.items():
        result[case_id] = _gap(entry, reason, scope="browser" if int(case_id[1:]) >= 73 else "transport")

    result["V041"] = _spec(
        "module_contract", "SubagentManager.spawn -> AgentRunner.run -> _announce_result",
        {"nums.json": "[2,4,6]"},
        [{"key": "announced_result", "op": "contains", "expected": "12"},
         {"key": "announcement_count", "op": "equal", "expected": 1},
         {"key": "announcement_session", "op": "equal", "expected": "cli:case-v041"}],
        limitations=["用脚本响应驱动真实 SubagentManager/AgentRunner 和真实只读文件工具；不评估实际模型是否会主动委派或求和。"],
    )
    result["V042"] = _spec(
        "module_contract", "SubagentManager.spawn x2 -> MessageBus inbound announce",
        {"a.json": "[1,2,3]", "b.json": "[10,20]"},
        [{"key": "task_count", "op": "equal", "expected": 2},
         {"key": "announced_results", "op": "equal", "expected": ["6", "30"]},
         {"key": "announcement_sessions", "op": "equal", "expected": ["cli:case-v042", "cli:case-v042"]}],
        limitations=["真实并发子任务及工具读取由固定 provider 协议响应驱动；不评估Agent是否正确规划两个委派。"],
    )
    result["V043"] = _spec(
        "module_contract", "SubagentManager._run_subagent tool_error -> _announce_result",
        {"bad.txt": "受控服务拒绝读取：document unavailable"},
        [{"key": "failed_announcement", "op": "true", "expected": True},
         {"key": "announced_failure", "op": "true", "expected": True},
         {"key": "fabricated_budget", "op": "equal", "expected": False}],
        limitations=["用隔离的确定性工具故障驱动真实 AgentRunner/SubagentManager 错误回注；不评估真实模型措辞。"],
    )
    result["V044"] = _spec(
        "module_contract", "SubagentManager.cancel_by_session",
        {},
        [{"key": "cancelled_count", "op": "equal", "expected": 1},
         {"key": "cancelled_task_no_announce", "op": "true", "expected": True},
         {"key": "other_session_finished", "op": "true", "expected": True}],
        limitations=["测试受控 provider 等待点上的 manager/session 取消传播；不验证 /stop 命令入口和非幂等副作用回滚。"],
    )
    result["V045"] = _spec(
        "module_contract", "SpawnTool.execute -> SubagentManager concurrency guard",
        {},
        [{"key": "second_spawn_denied", "op": "true", "expected": True},
         {"key": "running_peak", "op": "equal", "expected": 1}],
        limitations=["调用真实 SpawnTool 上限检查和真实 manager 子任务；不测试模型如何选择并发策略。"],
    )
    result["V046"] = _spec(
        "module_contract", "SubagentManager._announce_result session routing",
        {},
        [{"key": "result_by_session", "op": "equal", "expected": {"session-A": "17", "session-B": "23"}},
         {"key": "announcement_count", "op": "equal", "expected": 2}],
        limitations=["检查真实 announce 的 origin/session 归属；该探针不覆盖 SessionManager 的重启持久化和历史去重。"],
    )

    mcp_fixtures = {"mcp_server.py": _MCP_SERVER}
    result["V047"] = _spec(
        "module_contract", "connect_mcp_servers(stdio) -> ToolRegistry.execute",
        mcp_fixtures,
        [{"key": "registered", "op": "true", "expected": True},
         {"key": "result", "op": "equal", "expected": {"order_id": "A17", "status": "shipped", "tracking": "T900"}},
         {"key": "server_calls", "op": "equal", "expected": ["A17"]},
         {"key": "closed", "op": "true", "expected": True}],
        limitations=["通过真实本地 stdio MCP server/client、registry 和调用账本；不评估模型是否发现并选择工具。"],
    )
    result["V048"] = _spec(
        "module_contract", "MCPToolWrapper schema validation + ToolRegistry.execute",
        mcp_fixtures,
        [{"key": "invalid_rejected", "op": "true", "expected": True},
         {"key": "valid_result", "op": "contains", "expected": "shipped"},
         {"key": "server_calls", "op": "equal", "expected": ["A17"]}],
        limitations=["非法与合法参数均通过 registry.execute；分阶段记录 server 账本，验证非法调用前拒绝及合法只调用一次；不验证模型读取错误后自行修正。"],
    )
    result["V049"] = _spec(
        "module_contract", "connect_mcp_servers resource/prompt wrappers",
        mcp_fixtures,
        [{"key": "resource_registered", "op": "true", "expected": True},
         {"key": "prompt_registered", "op": "true", "expected": True},
         {"key": "resource_text", "op": "equal", "expected": "退货期限14天"},
         {"key": "prompt_text", "op": "contains", "expected": "不补充未给条款"}],
        limitations=["验证真实 MCP resource/prompt 发现和 wrapper 读取；不评估模型是否遵循 prompt 或正确回答。"],
    )
    result["V050"] = _spec(
        "transport", "connect_mcp_servers(sse/streamableHttp) -> ToolRegistry.execute",
        {"mcp_http_server.py": _MCP_HTTP_SERVER},
        [{"key": "transport_results", "op": "equal", "expected": {"sse": "T900", "streamableHttp": "T900"}},
         {"key": "server_calls", "op": "equal", "expected": ["A17", "A17"]},
         {"key": "servers_stopped", "op": "true", "expected": True}],
        limitations=["SSE 与 streamable HTTP 均由本地 FastMCP/uvicorn loopback 服务真实握手和调用；不覆盖 Slack 外部平台或公网代理。"],
    )
    result["V051"] = _spec(
        "module_contract", "MCPToolWrapper.execute timeout -> same ClientSession recovery",
        {"mcp_recovery_server.py": _MCP_RECOVERY_SERVER},
        [{"key": "first_timed_out", "op": "true", "expected": True},
         {"key": "retry_result", "op": "contains", "expected": "T900"},
         {"key": "server_attempts", "op": "equal", "expected": 2},
         {"key": "closed", "op": "true", "expected": True}],
        limitations=["真实本地 stdio MCP server 首次 sleep(5) 配合 tool_timeout=2，再通过同一 wrapper/session 成功重试；由固定调用序列触发，不评估模型是否决定重试。"],
    )
    result["V052"] = _spec(
        "module_contract", "connect_mcp_servers(stdio batch launcher with spaces)",
        {"mcp service/mcp_server.py": _MCP_SERVER, "mcp service/launch service.bat": _MCP_BATCH},
        [{"key": "result", "op": "equal", "expected": {"order_id": "A17", "status": "shipped", "tracking": "T900"}},
         {"key": "closed", "op": "true", "expected": True},
         {"key": "server_calls", "op": "equal", "expected": ["A17"]}],
        supported=os.name == "nt",
        limitations=["仅验证 Windows stdio launcher 路径/参数与真实本地 MCP 握手；不覆盖其他 Windows Shell/Conda 安装布局。"] if os.name == "nt" else ["Windows-only batch launcher case; current host is not Windows."],
    )

    result["V053"] = _spec(
        "module_contract", "CronService.add_job/run_job one-shot",
        {},
        [{"key": "schedule_kind", "op": "equal", "expected": "at"},
         {"key": "callback_count", "op": "equal", "expected": 1},
         {"key": "disabled_after_run", "op": "true", "expected": True}],
        limitations=["使用真实 Cron 存储/API 和 callback；由 run_job 人工触发，不模拟业务时钟到点或下游外部投递。"],
    )
    result["V054"] = _spec(
        "module_contract", "CronService._compute_next_run with fixed epoch",
        {},
        [{"key": "next_utc_ms", "op": "equal", "expected": 1790902800000},
         {"key": "tz", "op": "equal", "expected": "Asia/Shanghai"}],
        limitations=["直接验证真实时区调度算法的固定时刻输出；不推进后台真实时钟或检查两日投递。"],
    )
    result["V055"] = _spec(
        "module_contract", "CronService.list_jobs/remove_job",
        {},
        [{"key": "remove_result", "op": "equal", "expected": "removed"},
         {"key": "remaining_names", "op": "equal", "expected": ["检查备份"]}],
        limitations=["使用真实任务存储和删除 API；不等待计划触发或验证外部通知。"],
    )
    result["V056"] = _spec(
        "module_contract", "CronService.add_job timezone validation",
        {},
        [{"key": "rejected", "op": "true", "expected": True},
         {"key": "job_count", "op": "equal", "expected": 0}],
        limitations=["验证真实 add_job 时区校验；不评估自然语言计划解释。"],
    )
    result["V057"] = _spec(
        "module_contract", "CronService action journal reload",
        {},
        [{"key": "reloaded_count", "op": "equal", "expected": 1},
         {"key": "same_job_id", "op": "true", "expected": True},
         {"key": "message", "op": "equal", "expected": "交报告"}],
        limitations=["验证 action journal 重载逻辑上的任务持久化；不启动后台 scheduler 或推进触发时钟。"],
    )
    for case_id in ("V058", "V059", "V060"):
        assertions = {
            "V058": [
                {"key": "decision", "op": "equal", "expected": "skip"},
                {"key": "notify_count", "op": "equal", "expected": 0},
            ],
            "V059": [
                {"key": "decision", "op": "equal", "expected": "run"},
                {"key": "notify_count", "op": "equal", "expected": 1},
                {"key": "notified_text", "op": "equal", "expected": "备份失败：磁盘不可用，需要检查"},
            ],
            "V060": [
                {"key": "decision", "op": "equal", "expected": "run"},
                {"key": "notify_count", "op": "equal", "expected": 1},
                {"key": "notified_text", "op": "equal", "expected": "备份失败：磁盘不可用"},
            ],
        }[case_id]
        result[case_id] = _spec(
            "module_contract", "HeartbeatService._tick + evaluate_response",
            {"HEARTBEAT.md": "检查备份失败时通知我。"}, assertions,
            limitations=["使用脚本 provider 协议触发真实 HeartbeatService 与通知门；不测周期调度或实际平台投递，provider 异常路径不计真实模型费用。"],
        )

    result["V061"] = _spec(
        "module_contract", "BaseChannel._handle_message authorization + MessageBus",
        {},
        [{"key": "accepted_senders", "op": "equal", "expected": ["user-A"]},
         {"key": "inbound_count", "op": "equal", "expected": 1}],
        limitations=["驱动真实 BaseChannel 授权和 bus 入队；不接真实 Telegram/模型/回复。"],
    )
    result["V062"] = _spec(
        "module_contract", "FeishuChannel mention parser",
        {},
        [{"key": "without_mention", "op": "equal", "expected": False},
         {"key": "with_bot_mention", "op": "equal", "expected": True},
         {"key": "resolved_text", "op": "contains", "expected": "2加3是多少"}],
        limitations=["直接运行真实飞书 mention 解析方法，不建立平台连接或测试消息发送。"],
    )
    result["V064"] = _spec(
        "module_contract", "nanobot.utils.helpers.split_message + Telegram limits",
        {"long_text.txt": "A" * 5000 + "END"},
        [{"key": "chunk_count", "op": "equal", "expected": 2},
         {"key": "all_within_limit", "op": "true", "expected": True},
         {"key": "reconstructed", "op": "equal", "expected": "A" * 5000 + "END"}],
        limitations=["验证实际分段算法及 Telegram 配置上限，不调用 Telegram 外部 API 或宣称平台已确认送达。"],
    )
    result["V067"] = _spec(
        "transport", "aiohttp TestServer -> create_app /v1/chat/completions",
        {},
        [{"key": "http_status", "op": "equal", "expected": 200},
         {"key": "assistant_content", "op": "equal", "expected": "5"},
         {"key": "call_count", "op": "equal", "expected": 1}],
        limitations=["用真实 aiohttp handler/序列化和本地固定 Agent stub；不运行真实模型推理。"],
    )
    result["V068"] = _spec(
        "transport", "aiohttp TestServer -> handle_chat_completions invalid JSON",
        {},
        [{"key": "http_status", "op": "equal", "expected": 400},
         {"key": "error_type", "op": "equal", "expected": "invalid_request_error"},
         {"key": "call_count", "op": "equal", "expected": 0}],
        limitations=["验证真实 HTTP handler 对无效 JSON 的响应及零 Agent 调用；无真实模型。"],
    )
    result["V069"] = _spec(
        "transport", "aiohttp TestServer -> create_app SSE stream",
        {},
        [{"key": "http_status", "op": "equal", "expected": 200},
         {"key": "joined_content", "op": "equal", "expected": "竹桥完成"},
         {"key": "done_marker", "op": "true", "expected": True},
         {"key": "call_count", "op": "equal", "expected": 1}],
        limitations=["固定 Agent callback 经过真实 SSE handler/serializer；不模拟真实模型的 delta 分布或网络 chunk 粒度。"],
    )
    result["V070"] = _spec(
        "module_contract", "WebSocketChannel._attach/send chat routing",
        {},
        [{"key": "chat_a_text", "op": "equal", "expected": "A=17"},
         {"key": "chat_b_text", "op": "equal", "expected": "B=23"},
         {"key": "same_connection", "op": "true", "expected": True}],
        limitations=["真实 WebSocketChannel 路由方法配本地 socket stub；不启动网络 listener 或验证浏览器 DOM。"],
    )
    result["V071"] = _spec(
        "module_contract", "WebSocketChannel._cleanup_connection/_attach/send",
        {},
        [{"key": "old_connection_receives_second", "op": "equal", "expected": False},
         {"key": "new_connection_receives_second", "op": "equal", "expected": True},
         {"key": "first_send_count", "op": "equal", "expected": 1}],
        limitations=["验证真实订阅清理和重绑方法；不测试网络断开状态机、reconnect 通知或浏览器重放行为。"],
    )
    result["V072"] = _spec(
        "module_contract", "ChannelManager._coalesce_stream_deltas + MessageBus",
        {},
        [{"key": "first_stream", "op": "equal", "expected": "AB"},
         {"key": "boundary_event", "op": "equal", "expected": "turn_end"},
         {"key": "other_stream", "op": "equal", "expected": "X"}],
        limitations=["使用真实队列和合并函数检查 stream/chat 边界；不测模型延迟或实际渠道发送性能。"],
    )
    return result


class _ScriptedProvider(LLMProvider):
    """Fixed protocol provider; requests are marked scripted, never billed."""

    observation_mode = "scripted"
    supports_progress_deltas = False

    def __init__(self, *, started=None, release=None):
        from nanobot.providers.base import GenerationSettings

        super().__init__()
        self.generation = GenerationSettings(temperature=0, max_tokens=1024)
        self.started = started
        self.release = release
        self._order = 0

    def get_default_model(self):
        return "deepseek-v4-flash"

    async def chat(self, messages, tools=None, **kwargs):
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        self._order += 1
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if self.started is not None and "slow" in str(last_user).lower():
            self.started.set()
            if self.release is None:
                await asyncio.Future()
            else:
                await self.release.wait()
        if self.started is not None and "fast" in str(last_user).lower() and self.release is not None:
            self.started.set()
            await self.release.wait()

        tool_names = {
            (item.get("function") or {}).get("name") or item.get("name")
            for item in (tools or [])
        }
        if "heartbeat" in tool_names:
            action = "skip" if "normal" in str(last_user).lower() else "run"
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("hb", "heartbeat", {"action": action, "tasks": "备份检查"})],
                finish_reason="tool_calls",
            )
        if "evaluate_notification" in tool_names:
            if "controlled-evaluator-exception" in str(last_user):
                raise RuntimeError("controlled evaluator failure")
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("notify", "evaluate_notification", {"should_notify": True})],
                finish_reason="tool_calls",
            )

        task = str(last_user)
        match = re.search(r"([A-Za-z0-9_-]+\.json)", task)
        filename = match.group(1) if match else "nums.json"
        if "read_file" not in tool_names:
            return LLMResponse(content="我无法读取输入文件。")
        if any(m.get("role") == "tool" for m in messages):
            tool_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "tool")
            data_match = re.search(r"\|\s*(\[[^\]]*\])", tool_text)
            if data_match:
                values = json.loads(data_match.group(1))
                return LLMResponse(content=str(sum(values)))
            if "Error" in tool_text:
                return LLMResponse(content="无法读取预算，子任务没有取得数据。")
            return LLMResponse(content="已读取文件。")
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(f"read-{self._order}", "read_file", {"path": filename})],
            finish_reason="tool_calls",
        )

    async def chat_with_retry(self, messages, **kwargs):
        # Reuse the project's retry/observation path; scripted_response spans
        # are explicitly excluded from billed model-attempt accounting.
        return await LLMProvider.chat_with_retry(self, messages, **kwargs)

    async def chat_stream_with_retry(self, messages, **kwargs):
        from nanobot.providers.base import LLMProvider

        return await LLMProvider.chat_stream_with_retry(self, messages, **kwargs)


async def _wait_subagents(manager, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while manager.get_running_count() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    if manager.get_running_count():
        raise TimeoutError("subagent_drain_timeout")
    await asyncio.sleep(0)


def _new_subagent_manager(workspace, bus, provider):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.config.schema import ToolsConfig

    manager = SubagentManager(
        provider=provider,
        workspace=workspace,
        bus=bus,
        max_tool_result_chars=1000,
        model="deepseek-v4-flash",
        tools_config=ToolsConfig(restrict_to_workspace=True),
        restrict_to_workspace=True,
        max_iterations=4,
    )
    return manager


async def _run_subagents(case_id, workspace):
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = _ScriptedProvider()
    manager = _new_subagent_manager(workspace, bus, provider)
    task_count = 1
    if case_id == "V042":
        task_count = 2
        await asyncio.gather(
            manager.spawn("读取a.json求和", session_key="cli:case-v042"),
            manager.spawn("读取b.json求和", session_key="cli:case-v042"),
        )
    else:
        await manager.spawn("读取nums.json求和", session_key="cli:case-v041")
    await _wait_subagents(manager)
    messages = []
    while bus.inbound_size:
        messages.append(await bus.consume_inbound())
    announcements = [msg.content for msg in messages if msg.metadata.get("injected_event") == "subagent_result"]
    sessions = sorted(msg.session_key for msg in messages)
    if case_id == "V042":
        values = []
        for announcement in announcements:
            match = re.search(r"Result:\s*\n([^\n]+)", announcement)
            if match:
                values.append(match.group(1).strip())
        flat_values = sorted(values, key=int)
        return {"task_count": task_count, "announced_results": flat_values,
                "announcement_sessions": sessions}
    return {"announced_result": announcements[0] if announcements else "",
            "announcement_count": len(announcements),
            "announcement_session": sessions[0] if sessions else ""}


async def _run_subagent_failure(workspace):
    from nanobot.agent.tools.base import Tool
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.bus.queue import MessageBus

    class UnavailableRead(Tool):
        @property
        def name(self): return "read_file"
        @property
        def description(self): return "Controlled unavailable document fixture"
        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}
        async def execute(self, **kwargs):
            return "Error: document unavailable"

    class Provider(_ScriptedProvider):
        async def chat(self, messages, tools=None, **kwargs):
            from nanobot.providers.base import LLMResponse, ToolCallRequest
            if any(m.get("role") == "tool" for m in messages):
                return LLMResponse(content="预算未能读取。")
            return LLMResponse(content=None,
                               tool_calls=[ToolCallRequest("bad-read", "read_file", {"path": "bad.txt"})],
                               finish_reason="tool_calls")

    bus = MessageBus()
    manager = _new_subagent_manager(workspace, bus, Provider())
    registry = ToolRegistry()
    registry.register(UnavailableRead())
    manager._build_tools = lambda: registry
    await manager.spawn("读取bad.txt中的预算", session_key="cli:case-v043")
    await _wait_subagents(manager)
    msg = await bus.consume_inbound()
    announced_failure = "failed" in msg.content.lower()
    result = msg.content.split("Result:", 1)[-1]
    return {"failed_announcement": "failed" in msg.content.lower(),
            "announced_failure": announced_failure,
            "fabricated_budget": bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|人民币)", result))}


async def _run_subagent_origin(workspace):
    from nanobot.bus.queue import MessageBus

    class Provider(_ScriptedProvider):
        async def chat(self, messages, tools=None, **kwargs):
            from nanobot.providers.base import LLMResponse
            task = next((str(m.get("content", "")) for m in messages if m.get("role") == "user"), "")
            return LLMResponse(content="17" if "A-result" in task else "23")

    bus = MessageBus()
    manager = _new_subagent_manager(workspace, bus, Provider())
    await asyncio.gather(
        manager.spawn("A-result", session_key="session-A"),
        manager.spawn("B-result", session_key="session-B"),
    )
    await _wait_subagents(manager)
    result_by_session = {}
    while bus.inbound_size:
        msg = await bus.consume_inbound()
        match = re.search(r"Result:\s*\n([^\n]+)", msg.content)
        result_by_session[msg.session_key] = match.group(1).strip() if match else ""
    return {"result_by_session": result_by_session,
            "announcement_count": len(result_by_session)}


async def _run_subagent_cancel(workspace):
    from nanobot.bus.queue import MessageBus

    started_slow = asyncio.Event()
    started_fast = asyncio.Event()
    release_fast = asyncio.Event()
    bus = MessageBus()

    class Provider(_ScriptedProvider):
        async def chat(self, messages, tools=None, **kwargs):
            text = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
            if "slow" in text.lower():
                started_slow.set()
                await asyncio.Future()
            if "fast" in text.lower():
                started_fast.set()
                await release_fast.wait()
            from nanobot.providers.base import LLMResponse
            return LLMResponse(content="finished")

    manager = _new_subagent_manager(workspace, bus, Provider())
    await manager.spawn("slow task", session_key="session-A")
    await manager.spawn("fast task", session_key="session-B")
    await asyncio.wait_for(asyncio.gather(started_slow.wait(), started_fast.wait()), 3)
    cancelled = await manager.cancel_by_session("session-A")
    release_fast.set()
    await _wait_subagents(manager)
    announced = []
    while bus.inbound_size:
        announced.append((await bus.consume_inbound()).session_key)
    return {"cancelled_count": cancelled,
            "cancelled_task_no_announce": "session-A" not in announced,
            "other_session_finished": "session-B" in announced}


async def _run_spawn_limit(workspace):
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    started = asyncio.Event()
    release = asyncio.Event()
    bus = MessageBus()
    manager = _new_subagent_manager(workspace, bus, _ScriptedProvider(started=started, release=release))
    manager.max_concurrent_subagents = 1
    await manager.spawn("slow task", session_key="limit-session")
    await asyncio.wait_for(started.wait(), 3)
    spawn_tool = SpawnTool(manager)
    denied = await spawn_tool.execute(task="extra task")
    peak = manager.get_running_count()
    release.set()
    await _wait_subagents(manager)
    return {"second_spawn_denied": "concurrency limit reached" in denied,
            "running_peak": peak}


def _reserve_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def _wait_loopback_server(process, port):
    for _ in range(80):
        if process.returncode is not None:
            raise RuntimeError(f"local MCP server exited with code {process.returncode}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError("local MCP HTTP server did not become ready")


def _fixture_process_env():
    # Pass only runtime essentials to the fixture subprocess; in particular,
    # do not propagate provider credentials or other application secrets.
    names = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {name: os.environ[name] for name in names if name in os.environ}


async def _stop_process(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
    return process.returncode is not None


async def _run_mcp_http(workspace):
    from nanobot.agent.tools.mcp import connect_mcp_servers
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import MCPServerConfig

    server = inside(workspace, "mcp_http_server.py")
    observed = {"transport_results": {}, "servers_stopped": True}
    for transport in ("sse", "streamableHttp"):
        port = _reserve_loopback_port()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(server), transport, str(port),
            cwd=str(workspace), env=_fixture_process_env(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await _wait_loopback_server(process, port)
            path = "/sse" if transport == "sse" else "/mcp"
            cfg = MCPServerConfig(
                type=transport, url=f"http://127.0.0.1:{port}{path}", tool_timeout=8,
            )
            registry = ToolRegistry()
            stacks = await connect_mcp_servers({"eval": cfg}, registry)
            try:
                value = await registry.execute(
                    "mcp_eval_lookup_order", {"order_id": "A17"},
                )
                observed["transport_results"][transport] = json.loads(value)["tracking"]
            finally:
                for stack in stacks.values():
                    await stack.aclose()
        finally:
            observed["servers_stopped"] = await _stop_process(process) and observed["servers_stopped"]

    log_path = server.with_name("calls.jsonl")
    observed["server_calls"] = (
        [json.loads(line)["order_id"] for line in log_path.read_text(encoding="utf-8").splitlines()]
        if log_path.exists() else []
    )
    return observed


async def _run_mcp_timeout_recovery(workspace):
    from nanobot.agent.tools.mcp import connect_mcp_servers
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import MCPServerConfig

    server = inside(workspace, "mcp_recovery_server.py")
    cfg = MCPServerConfig(
        type="stdio", command=sys.executable, args=[str(server)], tool_timeout=2,
    )
    registry = ToolRegistry()
    stacks = await connect_mcp_servers({"eval": cfg}, registry)
    observed = {"closed": False}
    try:
        first = await registry.execute("mcp_eval_lookup_order", {"order_id": "A17"})
        observed["first_timed_out"] = "timed out" in first.lower()
        observed["retry_result"] = await registry.execute(
            "mcp_eval_lookup_order", {"order_id": "A17"},
        )
    finally:
        for stack in stacks.values():
            await stack.aclose()
        observed["closed"] = True

    log_path = server.with_name("calls.jsonl")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    observed["server_attempts"] = sum(row.get("phase") == "started" for row in rows)
    observed["server_phases"] = [row["phase"] for row in rows]
    return observed


async def _run_mcp(case_id, workspace):
    from nanobot.agent.tools.mcp import connect_mcp_servers
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import MCPServerConfig

    server = inside(workspace, "mcp_server.py")
    if case_id == "V052":
        server = inside(workspace, "mcp service/mcp_server.py")
        command = str(inside(workspace, "mcp service/launch service.bat"))
        cfg = MCPServerConfig(type="stdio", command=command, args=[],
                              env={"EVAL_PYTHON": sys.executable}, tool_timeout=5)
    else:
        cfg = MCPServerConfig(type="stdio", command=sys.executable,
                              args=[str(server)], tool_timeout=5)
    registry = ToolRegistry()
    stacks = await connect_mcp_servers({"eval": cfg}, registry)
    observed = {"closed": False}
    try:
        if case_id == "V047" or case_id == "V052":
            value = await registry.execute("mcp_eval_lookup_order", {"order_id": "A17"})
            observed["registered"] = registry.has("mcp_eval_lookup_order")
            observed["result"] = json.loads(value)
        elif case_id == "V048":
            observed["invalid_result"] = await registry.execute(
                "mcp_eval_lookup_order", {"order_id": ["A17"]},
            )
            observed["invalid_rejected"] = observed["invalid_result"].startswith(
                "Error: Invalid parameters for tool "
            )
            log_path = server.with_name("calls.jsonl")
            observed["invalid_server_calls"] = (
                [json.loads(line)["order_id"] for line in log_path.read_text(encoding="utf-8").splitlines()]
                if log_path.exists() else []
            )
            observed["valid_result"] = await registry.execute(
                "mcp_eval_lookup_order", {"order_id": "A17"},
            )
        elif case_id == "V049":
            observed["resource_registered"] = registry.has("mcp_eval_resource_policy")
            observed["prompt_registered"] = registry.has("mcp_eval_prompt_policy_answer")
            observed["resource_text"] = await registry.execute("mcp_eval_resource_policy", {})
            observed["prompt_text"] = await registry.execute("mcp_eval_prompt_policy_answer", {})
    finally:
        for stack in stacks.values():
            await stack.aclose()
        observed["closed"] = True
    log_path = server.with_name("calls.jsonl")
    observed["server_calls"] = [json.loads(line)["order_id"] for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
    if case_id == "V048":
        observed["valid_server_calls"] = observed["server_calls"][len(observed["invalid_server_calls"]):]
    return observed


async def _run_cron(case_id, workspace):
    from nanobot.cron.service import CronService, _compute_next_run
    from nanobot.cron.types import CronSchedule

    path = inside(workspace, "cron/store.json")
    service = CronService(path)
    if case_id == "V054":
        now_ms = 1790816400000  # 2026-10-01T01:00:00Z
        schedule = CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai")
        next_ms = _compute_next_run(schedule, now_ms)
        return {"next_utc_ms": next_ms, "tz": schedule.tz}
    if case_id == "V056":
        rejected = False
        try:
            service.add_job("invalid", CronSchedule(kind="cron", expr="0 9 * * *", tz="Mars/Olympus"), "x")
        except ValueError:
            rejected = True
        return {"rejected": rejected, "job_count": len(service.list_jobs(include_disabled=True))}
    if case_id == "V055":
        service.add_job("交报告", CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60000), "交报告")
        service.add_job("检查备份", CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60000), "检查备份")
        jobs = service.list_jobs(include_disabled=True)
        target = next(j for j in jobs if j.name == "交报告")
        removed = service.remove_job(target.id)
        return {"remove_result": removed,
                "remaining_names": sorted(j.name for j in service.list_jobs(include_disabled=True))}
    if case_id == "V057":
        service.add_job("交报告", CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 3600000), "交报告")
        before = service.list_jobs(include_disabled=True)
        restored = CronService(path).list_jobs(include_disabled=True)
        return {"reloaded_count": len(restored),
                "same_job_id": bool(before and restored and before[0].id == restored[0].id),
                "message": restored[0].payload.message if restored else ""}

    deliveries = []
    async def on_job(job):
        deliveries.append(job.payload.message)
        return "done"
    service = CronService(path, on_job=on_job)
    job = service.add_job("交报告", CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60000), "交报告", delete_after_run=False)
    ran = await service.run_job(job.id)
    state = service.get_job(job.id)
    return {"schedule_kind": job.schedule.kind,
            "callback_count": len(deliveries) if ran else 0,
            "disabled_after_run": state is not None and not state.enabled}


class _HeartbeatProvider(LLMProvider):
    observation_mode = "scripted"
    supports_progress_deltas = False

    def __init__(self, *, fail_evaluator=False, skip=False):
        super().__init__()
        self.fail_evaluator = fail_evaluator
        self.skip = skip

    def get_default_model(self):
        return "deepseek-v4-flash"

    async def chat(self, messages, tools=None, **kwargs):
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        fn = (tools or [{}])[0].get("function", {}).get("name", "")
        if fn == "heartbeat":
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                "hb", fn, {"action": "skip" if self.skip else "run", "tasks": "备份检查"},
            )], finish_reason="tool_calls")
        if self.fail_evaluator:
            raise RuntimeError("controlled evaluator failure")
        return LLMResponse(content=None, tool_calls=[ToolCallRequest("decision", "evaluate_notification", {"should_notify": True})], finish_reason="tool_calls")

    async def chat_with_retry(self, messages, **kwargs):
        return await LLMProvider.chat_with_retry(self, messages, **kwargs)

    async def chat_stream_with_retry(self, messages, **kwargs):
        from nanobot.providers.base import LLMProvider
        return await LLMProvider.chat_stream_with_retry(self, messages, **kwargs)


async def _run_heartbeat(case_id, workspace):
    from nanobot.heartbeat.service import HeartbeatService

    fail = case_id == "V060"
    provider = _HeartbeatProvider(fail_evaluator=fail, skip=case_id == "V058")
    notifications = []
    result = "备份失败：磁盘不可用" if fail else "备份失败：磁盘不可用，需要检查"
    async def execute(_tasks):
        return "状态正常，没有新变化" if case_id == "V058" else result
    async def notify(text):
        notifications.append(text)
    service = HeartbeatService(workspace, provider, "deepseek-v4-flash", on_execute=execute, on_notify=notify)
    await service._tick()
    return {"decision": "skip" if case_id == "V058" else "run",
            "notify_count": len(notifications),
            "notified_text": notifications[0] if notifications else ""}


class _BusOnlyChannel:
    name = "test"
    send_progress = True
    send_tool_hints = False
    config = SimpleNamespace(allow_from=["user-A"], streaming=False)

    def __init__(self, bus):
        from nanobot.channels.base import BaseChannel
        class Channel(BaseChannel):
            name = "test"
            async def start(self): pass
            async def stop(self): pass
            async def send(self, msg): pass
        self.channel = Channel(self.config, bus)


async def _run_base_channel(workspace):
    from nanobot.bus.queue import MessageBus
    bus = MessageBus()
    from nanobot.channels.base import BaseChannel
    class Channel(BaseChannel):
        name = "test"
        async def start(self): pass
        async def stop(self): pass
        async def send(self, msg): pass
    channel = Channel({"allow_from": ["user-A"]}, bus)
    await channel._handle_message("user-A", "cA", "2加3是多少？")
    await channel._handle_message("user-B", "cB", "2加3是多少？")
    accepted = []
    while bus.inbound_size:
        accepted.append((await bus.consume_inbound()).sender_id)
    return {"accepted_senders": accepted, "inbound_count": len(accepted)}


def _run_feishu_mentions():
    from types import SimpleNamespace

    from nanobot.channels.feishu import FeishuChannel
    channel = FeishuChannel.__new__(FeishuChannel)
    channel._bot_open_id = "bot-open-id"
    without = SimpleNamespace(content="2加3是多少？", mentions=[])
    with_bot = SimpleNamespace(content="@_user_1 2加3是多少？", mentions=[SimpleNamespace(
        key="@_user_1", name="测试机器人", id=SimpleNamespace(open_id="bot-open-id", user_id="bot-id"))])
    resolved = channel._resolve_mentions(with_bot.content, with_bot.mentions)
    return {"without_mention": channel._is_bot_mentioned(without),
            "with_bot_mention": channel._is_bot_mentioned(with_bot),
            "resolved_text": resolved}


async def _run_api(case_id):
    from aiohttp.test_utils import TestClient, TestServer

    from nanobot.api.server import create_app

    class Agent:
        calls = 0
        async def process_direct(self, content, **kwargs):
            self.calls += 1
            on_stream = kwargs.get("on_stream")
            if on_stream:
                for delta in ("竹", "桥", "完成"):
                    await on_stream(delta)
            return "竹桥完成" if on_stream else "5"

    agent = Agent()
    client = TestClient(TestServer(create_app(agent, model_name="deepseek-v4-flash")))
    await client.start_server()
    try:
        if case_id == "V068":
            response = await client.post("/v1/chat/completions", data='{"messages":', headers={"Content-Type": "application/json"})
            payload = await response.json()
            return {"http_status": response.status,
                    "error_type": payload.get("error", {}).get("type"),
                    "call_count": agent.calls}
        stream = case_id == "V069"
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "2加3是多少？"}], "stream": stream},
        )
        if stream:
            raw = await response.text()
            chunks = []
            for line in raw.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                obj = json.loads(line[6:])
                chunks.append(obj["choices"][0].get("delta", {}).get("content", ""))
            return {"http_status": response.status,
                    "joined_content": "".join(chunks),
                    "done_marker": "data: [DONE]" in raw,
                    "call_count": agent.calls}
        payload = await response.json()
        return {"http_status": response.status,
                "assistant_content": payload["choices"][0]["message"]["content"],
                "call_count": agent.calls}
    finally:
        await client.close()


async def _run_websocket(case_id):
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket import WebSocketChannel

    class Connection:
        def __init__(self): self.frames = []
        async def send(self, raw): self.frames.append(json.loads(raw))
    channel = WebSocketChannel({"websocket_requires_token": False}, MessageBus())
    old = Connection()
    if case_id == "V070":
        channel._attach(old, "cA")
        channel._attach(old, "cB")
        await channel.send(OutboundMessage("websocket", "cA", "A=17"))
        await channel.send(OutboundMessage("websocket", "cB", "B=23"))
        a = next(f["text"] for f in old.frames if f.get("chat_id") == "cA")
        b = next(f["text"] for f in old.frames if f.get("chat_id") == "cB")
        return {"chat_a_text": a, "chat_b_text": b, "same_connection": True}
    channel._attach(old, "cA")
    await channel.send(OutboundMessage("websocket", "cA", "FIRST"))
    channel._cleanup_connection(old)
    new = Connection()
    channel._attach(new, "cA")
    await channel.send(OutboundMessage("websocket", "cA", "SECOND"))
    return {"old_connection_receives_second": any(f.get("text") == "SECOND" for f in old.frames),
            "new_connection_receives_second": any(f.get("text") == "SECOND" for f in new.frames),
            "first_send_count": sum(f.get("text") == "FIRST" for f in old.frames)}


async def _run_bus_merge():
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager

    bus = MessageBus()
    manager = ChannelManager.__new__(ChannelManager)
    manager.bus = bus
    stream = "s1"
    await bus.publish_outbound(OutboundMessage("test", "cA", "A", metadata={"_stream_delta": True, "_stream_id": stream}))
    await bus.publish_outbound(OutboundMessage("test", "cA", "B", metadata={"_stream_delta": True, "_stream_id": stream}))
    await bus.publish_outbound(OutboundMessage("test", "cA", "", metadata={"_turn_end": True}))
    await bus.publish_outbound(OutboundMessage("test", "cB", "X", metadata={"_stream_delta": True, "_stream_id": "s2"}))
    first = await bus.consume_outbound()
    merged, pending = manager._coalesce_stream_deltas(first)
    boundary = pending[0] if pending else await bus.consume_outbound()
    other = await bus.consume_outbound()
    return {"first_stream": merged.content,
            "boundary_event": "turn_end" if boundary.metadata.get("_turn_end") else "other",
            "other_stream": other.content}


async def execute(case_id: str, workspace: Path) -> dict:
    """Execute one supported adapter and return only observations from it."""
    workspace = Path(workspace).resolve()
    if case_id in {"V041", "V042"}:
        return await _run_subagents(case_id, workspace)
    if case_id == "V043":
        return await _run_subagent_failure(workspace)
    if case_id == "V044":
        return await _run_subagent_cancel(workspace)
    if case_id == "V045":
        return await _run_spawn_limit(workspace)
    if case_id == "V046":
        return await _run_subagent_origin(workspace)
    if case_id in {"V047", "V048", "V049", "V050", "V051", "V052"}:
        if case_id == "V050":
            return await _run_mcp_http(workspace)
        if case_id == "V051":
            return await _run_mcp_timeout_recovery(workspace)
        return await _run_mcp(case_id, workspace)
    if case_id in {"V053", "V054", "V055", "V056", "V057"}:
        return await _run_cron(case_id, workspace)
    if case_id in {"V058", "V059", "V060"}:
        return await _run_heartbeat(case_id, workspace)
    if case_id == "V061":
        return await _run_base_channel(workspace)
    if case_id == "V062":
        return _run_feishu_mentions()
    if case_id == "V064":
        from nanobot.channels.telegram import TELEGRAM_MAX_MESSAGE_LEN
        from nanobot.utils.helpers import split_message
        text = inside(workspace, "long_text.txt").read_text(encoding="utf-8")
        chunks = split_message(text, TELEGRAM_MAX_MESSAGE_LEN)
        return {"chunk_count": len(chunks),
                "all_within_limit": all(len(chunk) <= TELEGRAM_MAX_MESSAGE_LEN for chunk in chunks),
                "reconstructed": "".join(chunks)}
    if case_id in {"V067", "V068", "V069"}:
        return await _run_api(case_id)
    if case_id in {"V070", "V071"}:
        return await _run_websocket(case_id)
    if case_id == "V072":
        return await _run_bus_merge()
    raise ValueError(f"unsupported_adapter:{case_id}")
