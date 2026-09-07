"""A deliberately small schema subset: exactly two bounded integer arguments."""
import json
from copy import deepcopy


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, name, description, function):
        if name in self._tools:
            raise ValueError("duplicate tool name")
        parameters = {
            "type": "object",
            "properties": {
                key: {"type": "integer", "minimum": -1000000, "maximum": 1000000}
                for key in ("a", "b")
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        }
        schema = {"name": name, "description": description, "parameters": parameters}
        self._tools[name] = (schema, function)

    def schemas(self):
        return [deepcopy(schema) for schema, _ in self._tools.values()]

    def execute(self, call):
        def failure(code):
            return json.dumps({"ok": False, "error": code})
        if call.name not in self._tools:
            return failure("unknown_tool")
        schema, function = self._tools[call.name]
        args = call.arguments
        parameters = schema["parameters"]
        if not isinstance(args, dict) or set(args) != set(parameters["required"]):
            return failure("invalid_arguments")
        for key, value in args.items():
            rule = parameters["properties"][key]
            # bool is a subclass of int in Python; exact type rejects True/False.
            if type(value) is not int or not rule["minimum"] <= value <= rule["maximum"]:
                return failure("invalid_arguments")
        try:
            return json.dumps({"ok": True, "value": function(**args)}, allow_nan=False)
        except Exception:
            return failure("tool_error")


def make_tools():
    registry = ToolRegistry()
    registry.register("add", "Add two integers between -1000000 and 1000000.",
                      lambda a, b: a + b)
    return registry
