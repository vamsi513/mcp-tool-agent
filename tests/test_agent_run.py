"""Drive agent.run() with a scripted LLM but a real MCP server over stdio."""

import json
import types

import pytest

import agent

pytestmark = pytest.mark.asyncio


def _message(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _tool_call(call_id, name, arguments):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _completion(message):
    usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


class ScriptedClient:
    """Stands in for openai.OpenAI: returns queued completions in order and
    records the requests it was called with."""

    def __init__(self, script):
        self._script = list(script)
        self.requests = []
        create = self._create
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._script.pop(0)


@pytest.fixture
def scripted_openai(monkeypatch):
    holder = {}

    def install(script):
        client = ScriptedClient(script)
        monkeypatch.setattr(agent, "OpenAI", lambda *a, **k: client)
        holder["client"] = client
        return client

    return install


async def test_run_executes_tool_call_then_answers(scripted_openai):
    client = scripted_openai(
        [
            _completion(_message(tool_calls=[
                _tool_call("c1", "query_books", {"genre": "fantasy", "min_rating": 4.4})
            ])),
            _completion(_message(content="There are four such books.")),
        ]
    )

    answer = await agent.run("fantasy books over 4.4?", model="fake", max_turns=5)

    assert answer == "There are four such books."
    # the tool result really came back through MCP and was fed to the model
    tool_messages = [m for m in client.requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and '"count": 4' in tool_messages[0]["content"]
    # the small books://schema resource was read over MCP and put in context
    system_text = " ".join(
        m["content"] for m in client.requests[0]["messages"] if m["role"] == "system"
    )
    assert "books://schema" in system_text and "rating  REAL" in system_text


async def test_run_forces_answer_without_tools_on_final_turn(scripted_openai):
    # Model keeps trying to call a tool; the last turn must omit tools.
    loop_call = _completion(_message(tool_calls=[
        _tool_call("c1", "query_books", {"genre": "fantasy"})
    ]))
    client = scripted_openai([loop_call, loop_call, _completion(_message(content="done"))])

    answer = await agent.run("something", model="fake", max_turns=3)

    assert answer == "done"
    assert client.requests[-1]["tools"] is None
    assert client.requests[0]["tools"] is not None
