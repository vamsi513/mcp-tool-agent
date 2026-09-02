import types

import pytest

import agent


class FakeTool:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.inputSchema = schema


def test_mcp_tools_to_openai_shape():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    converted = agent.mcp_tools_to_openai([FakeTool("do_thing", "does a thing", schema)])
    assert converted == [
        {
            "type": "function",
            "function": {
                "name": "do_thing",
                "description": "does a thing",
                "parameters": schema,
            },
        }
    ]


def test_tool_result_text_joins_text_blocks():
    result = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="first"),
            types.SimpleNamespace(type="text", text="second"),
        ]
    )
    assert agent.tool_result_text(result) == "first\nsecond"


def test_tool_result_text_handles_empty():
    assert agent.tool_result_text(types.SimpleNamespace(content=[])) == "(no content)"


@pytest.mark.asyncio
async def test_call_tool_safely_rejects_bad_json():
    out = await agent.call_tool_safely(session=None, name="query_books", arguments_json="{not json")
    assert "could not parse tool arguments" in out


@pytest.mark.asyncio
async def test_call_tool_safely_rejects_non_object_args():
    out = await agent.call_tool_safely(session=None, name="query_books", arguments_json="[1, 2]")
    assert "must be a JSON object" in out


def test_complete_with_retry_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class Boom(agent.APIConnectionError):
        def __init__(self):
            pass

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Boom()
        return "ok"

    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=flaky))
    )
    assert agent.complete_with_retry(fake_client) == "ok"
    assert calls["n"] == 3
