"""The only module allowed to turn provider requests into tool execution."""
import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

from contracts import Call, Provider, Reply


class Runner:
    def __init__(self, provider: Provider, tools, store, max_steps=4,
                 max_calls_per_step=4, provider_timeout=5.0):
        if max_steps < 1 or max_calls_per_step < 1 or provider_timeout <= 0:
            raise ValueError("budgets must be positive")
        self.provider, self.tools, self.store = provider, tools, store
        self.max_steps = max_steps
        self.max_calls_per_step = max_calls_per_step
        self.provider_timeout = provider_timeout

    async def run(self, key, text):
        messages = self.store.load(key)
        messages.append({"role": "user", "content": text})
        trace = [f"input: {text}"]
        seen_ids = {call["id"] for message in messages
                    for call in message.get("tool_calls", [])}

        def finish(content):
            messages.append({"role": "assistant", "content": content})
            self.store.save(key, messages)
            trace.append(f"final: {content}")
            trace.append(f"saved: {len(messages)} messages")
            return content, trace

        for step in range(1, self.max_steps + 1):
            trace.append(f"provider: step {step}")
            try:
                reply = await asyncio.wait_for(
                    self.provider.complete(deepcopy(messages), self.tools.schemas()),
                    timeout=self.provider_timeout,
                )
            except TimeoutError:
                return finish("Stopped: provider_timeout")
            except Exception:
                return finish("Stopped: provider_error")
            if (not isinstance(reply, Reply) or not isinstance(reply.content, str)
                    or not isinstance(reply.calls, list)):
                return finish("Stopped: invalid_reply")
            if not reply.calls:
                return finish(reply.content)
            ids = []
            for call in reply.calls:
                if (not isinstance(call, Call) or not isinstance(call.id, str) or not call.id
                        or not isinstance(call.name, str) or not call.name):
                    return finish("Stopped: invalid_tool_calls")
                ids.append(call.id)
            if (len(ids) > self.max_calls_per_step or len(set(ids)) != len(ids)
                    or seen_ids.intersection(ids)):
                return finish("Stopped: invalid_tool_calls")
            # Reject the whole invalid batch before adding a protocol record or executing.
            seen_ids.update(ids)
            messages.append({"role": "assistant", "content": reply.content,
                             "tool_calls": [asdict(call) for call in reply.calls]})
            for call in reply.calls:
                trace.append(f"request: {call.id} {call.name} {json.dumps(call.arguments)}")
                result = self.tools.execute(call)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                status = "ok" if json.loads(result)["ok"] else "error"
                trace.append(f"tool: {call.name} -> {status}")
        # Even the final allowed batch receives all tool results before stopping.
        return finish("Stopped: step_budget")
