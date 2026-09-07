"""Deterministic teaching provider. No network, model request, or credential."""
import json

from contracts import Call, Reply


class ScriptedProvider:
    async def complete(self, messages, schemas):
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            if result["ok"]:
                return Reply(content=f"Result: {result['value']}")
            return Reply(content=f"Tool error: {result['error']}")
        parts = last["content"].split()
        if len(parts) == 3 and parts[0] == "add":
            try:
                a, b = int(parts[1]), int(parts[2])
            except ValueError:
                return Reply(content="Usage: add INTEGER INTEGER")
            return Reply(calls=[Call(f"call-{len(messages)}", "add", {"a": a, "b": b})])
        return Reply(content="Usage: add INTEGER INTEGER")
