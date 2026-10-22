"""Structured string inputs must be rejected before the MCP session is called."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.registry import ToolRegistry


def registry_for(fragment):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
        content=[TextContent(type="text", text="VALID")], isError=False)))
    wrapper = MCPToolWrapper(session, "test", SimpleNamespace(
        name="echo", description="synthetic echo",
        inputSchema={"type": "object", "properties": {"payload": fragment},
                     "required": ["payload"]}))
    registry = ToolRegistry()
    registry.register(wrapper)
    return registry, wrapper, session.call_tool


STRING_SCHEMAS = [
    {"type": "string"},
    {"type": ["string", "null"]},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
]


@pytest.mark.parametrize("schema", STRING_SCHEMAS)
@pytest.mark.parametrize("value", [[], ["A17"], {}, {"order_id": "A17"}])
async def test_registry_rejects_structured_string_before_mcp(schema, value):
    registry, wrapper, remote = registry_for(schema)
    result = await registry.execute(wrapper.name, {"payload": value})
    assert "Invalid parameters" in result
    assert "payload should be string" in result
    remote.assert_not_awaited()
    _, prepared, error = registry.prepare_call(wrapper.name, {"payload": value})
    assert error is not None
    assert prepared["payload"] == value
    assert type(prepared["payload"]) is type(value)


@pytest.mark.parametrize("value,expected", [
    ("A17", "A17"), ("", ""), (0, "0"), (42, "42"),
    (3.14, "3.14"), (True, "True"), (False, "False"),
])
async def test_registry_preserves_scalar_string_compatibility(value, expected):
    registry, wrapper, remote = registry_for({"type": "string"})
    assert await registry.execute(wrapper.name, {"payload": value}) == "VALID"
    remote.assert_awaited_once_with("echo", arguments={"payload": expected})


@pytest.mark.parametrize("schema", STRING_SCHEMAS[1:] + [{"type": "string", "nullable": True}])
async def test_registry_preserves_nullable_string(schema):
    registry, wrapper, remote = registry_for(schema)
    assert await registry.execute(wrapper.name, {"payload": None}) == "VALID"
    remote.assert_awaited_once_with("echo", arguments={"payload": None})


async def test_registry_rejects_non_nullable_none():
    registry, wrapper, remote = registry_for({"type": "string"})
    assert "payload should be string" in await registry.execute(wrapper.name, {"payload": None})
    remote.assert_not_awaited()


@pytest.mark.parametrize("invalid", [["A17"], {"order_id": "A17"}])
@pytest.mark.parametrize("nested", [False, True])
async def test_registry_nested_strings_reject_before_mcp_and_accept_correction(invalid, nested):
    fragment = {"type": "array", "items": {"type": "string"}}
    bad, good, expected_error = [invalid], [17], "payload[0] should be string"
    if nested:
        fragment = {"type": "object", "properties": {"orders": {
            "type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "note": {"type": ["string", "null"]}},
                "required": ["id"]}}}, "required": ["orders"]}
        bad = {"orders": [{"id": invalid}]}
        good = {"orders": [{"id": 17, "note": None}]}
        expected_error = "payload.orders[0].id should be string"
    registry, wrapper, remote = registry_for(fragment)
    assert expected_error in await registry.execute(wrapper.name, {"payload": bad})
    remote.assert_not_awaited()
    assert await registry.execute(wrapper.name, {"payload": good}) == "VALID"
    expected = {"orders": [{"id": "17", "note": None}]} if nested else ["17"]
    remote.assert_awaited_once_with("echo", arguments={"payload": expected})


async def test_arbitrary_python_object_is_not_stringified():
    registry, wrapper, remote = registry_for({"type": "string"})
    assert "payload should be string" in await registry.execute(wrapper.name, {"payload": object()})
    remote.assert_not_awaited()
