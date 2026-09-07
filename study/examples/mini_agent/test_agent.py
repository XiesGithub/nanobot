"""Run with: conda run -n nanobot python -m unittest discover -s . -v"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from contracts import Call, Reply
from provider import ScriptedProvider
from runner import Runner
from session import SessionStore
from tools import make_tools


class FixedProvider:
    def __init__(self, reply):
        self.reply = reply

    async def complete(self, messages, schemas):
        return self.reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root)

    def run_turn(self, provider=None, key="demo", text="add 2 3", **limits):
        agent = Runner(provider or ScriptedProvider(), make_tools(), self.store, **limits)
        return asyncio.run(agent.run(key, text))

    def test_tool_round_trip_and_call_id(self):
        answer, trace = self.run_turn()
        self.assertEqual(answer, "Result: 5")
        messages = self.store.load("demo")
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[1]["tool_calls"][0]["id"], messages[2]["tool_call_id"])
        self.assertEqual(json.loads(messages[2]["content"]), {"ok": True, "value": 5})
        self.assertIn("tool: add -> ok", trace)

    def test_invalid_arguments_do_not_execute(self):
        registry = make_tools()
        for args in ({"a": True, "b": 3}, {"a": "2", "b": 3}, {"a": 2},
                     {"a": 2, "b": 3, "extra": 1}, {"a": 1000001, "b": 1}, []):
            result = json.loads(registry.execute(Call("x", "add", args)))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_arguments")

    def test_unknown_tool(self):
        result = json.loads(make_tools().execute(Call("x", "shell", {})))
        self.assertEqual(result["error"], "unknown_tool")

    def test_error_result_returns_to_provider(self):
        class Recovering:
            async def complete(self, messages, schemas):
                if messages[-1]["role"] == "tool":
                    error = json.loads(messages[-1]["content"])["error"]
                    return Reply(content="Observed: " + error)
                return Reply(calls=[Call("bad-1", "missing", {})])
        answer, _ = self.run_turn(Recovering())
        self.assertEqual(answer, "Observed: unknown_tool")

    def test_step_budget_closes_pending_calls(self):
        answer, trace = self.run_turn(max_steps=1)
        self.assertEqual(answer, "Stopped: step_budget")
        self.assertEqual(self.store.load("demo")[-2]["role"], "tool")
        self.assertEqual(sum(t.startswith("provider:") for t in trace), 1)

    def test_call_batch_budget(self):
        reply = Reply(calls=[Call(str(i), "add", {"a": 1, "b": 2}) for i in range(5)])
        answer, _ = self.run_turn(FixedProvider(reply))
        self.assertEqual(answer, "Stopped: invalid_tool_calls")
        self.assertFalse(any(m["role"] == "tool" for m in self.store.load("demo")))

    def test_duplicate_call_id_rejected_before_execution(self):
        call = Call("same", "add", {"a": 1, "b": 2})
        answer, _ = self.run_turn(FixedProvider(Reply(calls=[call, call])))
        self.assertEqual(answer, "Stopped: invalid_tool_calls")

    def test_provider_exception_is_bounded(self):
        class Broken:
            async def complete(self, messages, schemas):
                raise RuntimeError("private diagnostics must not enter history")
        answer, _ = self.run_turn(Broken())
        self.assertEqual(answer, "Stopped: provider_error")
        self.assertNotIn("private", json.dumps(self.store.load("demo")))

    def test_session_isolation_and_safe_paths(self):
        self.run_turn(key="../alice", text="add 2 3")
        self.run_turn(key="bob", text="add 8 9")
        self.assertEqual(self.store.load("../alice")[-1]["content"], "Result: 5")
        self.assertEqual(self.store.load("bob")[-1]["content"], "Result: 17")
        self.assertEqual(len(list(self.root.glob("*.json"))), 2)

    def test_restart_restores_history(self):
        self.run_turn()
        fresh = SessionStore(self.root)
        self.assertEqual(fresh.load("demo"), self.store.load("demo"))
        answer, _ = asyncio.run(Runner(ScriptedProvider(), make_tools(), fresh)
                              .run("demo", "add 10 20"))
        self.assertEqual(answer, "Result: 30")
        self.assertEqual(len(fresh.load("demo")), 8)

    def test_corrupt_file_is_not_silently_reset(self):
        self.run_turn()
        next(self.root.glob("*.json")).write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.load("demo")

    def test_tool_exception_becomes_error(self):
        registry = make_tools()
        def broken(a, b):
            raise RuntimeError("private")
        registry.register("broken", "fail for testing", broken)
        result = json.loads(registry.execute(Call("x", "broken", {"a": 1, "b": 2})))
        self.assertEqual(result, {"ok": False, "error": "tool_error"})


if __name__ == "__main__":
    unittest.main()
