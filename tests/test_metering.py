import json
import types

import pytest

import agent
import metering

PRICES = {
    "currency": "USD",
    "models": {
        # Deliberately synthetic round numbers so the arithmetic is checkable.
        "fake-model": {"input_per_million": 3.0, "output_per_million": 15.0},
    },
}


@pytest.fixture
def prices():
    return metering.parse_prices(PRICES)


# --- price table -----------------------------------------------------------


def test_cost_arithmetic_is_exact(prices):
    # 2000 input @ 3.0 per million = 0.006; 1000 output @ 15.0 per million = 0.015
    assert round(prices.cost("fake-model", 2000, 1000), 6) == 0.021
    assert round(prices.cost("fake-model", 1_000_000, 0), 6) == 3.0
    assert round(prices.cost("fake-model", 0, 1_000_000), 6) == 15.0
    assert prices.cost("fake-model", 0, 0) == 0.0


def test_unknown_model_has_no_cost(prices):
    assert prices.cost("not-in-table", 1000, 1000) is None


def test_example_price_table_is_valid_and_all_zero():
    table = metering.load_prices("prices.example.yaml")
    assert table.currency == "USD"
    assert table.models
    for name, price in table.models.items():
        assert price.input_per_million == 0.0, name
        assert price.output_per_million == 0.0, name


def test_malformed_price_table_raises(tmp_path):
    bad = tmp_path / "prices.yaml"
    bad.write_text("models: [1, 2, 3]\n")
    with pytest.raises(metering.PriceError, match="models must be a mapping"):
        metering.load_prices(bad)


def test_non_numeric_price_raises(tmp_path):
    bad = tmp_path / "prices.yaml"
    bad.write_text("models:\n  m:\n    input_per_million: cheap\n")
    with pytest.raises(metering.PriceError, match="non-numeric"):
        metering.load_prices(bad)


def test_missing_price_file_raises():
    with pytest.raises(metering.PriceError, match="no price file found"):
        metering.load_prices("/nonexistent/prices.yaml")


# --- recording and reporting ----------------------------------------------


def test_report_by_user_totals_and_cost(governance_db, prices):
    metering.record_llm_turn("bob", "fake-model", 1200, 400)
    metering.record_llm_turn("bob", "fake-model", 800, 600)
    metering.record_llm_turn("alice", "fake-model", 1000, 1000)

    result = metering.report(group="user", prices=prices)
    by_user = {r["user"]: r for r in result["rows"]}

    assert by_user["bob"]["turns"] == 2
    assert by_user["bob"]["input_tokens"] == 2000
    assert by_user["bob"]["output_tokens"] == 1000
    assert by_user["bob"]["estimated_cost"] == 0.021  # 0.006 + 0.015

    assert by_user["alice"]["input_tokens"] == 1000
    assert by_user["alice"]["estimated_cost"] == 0.018  # 0.003 + 0.015

    assert result["totals"]["input_tokens"] == 3000
    assert result["totals"]["output_tokens"] == 2000
    assert result["totals"]["estimated_cost"] == 0.039
    assert "estimate" in result["cost_basis"]


def test_report_filters_by_user(governance_db, prices):
    metering.record_llm_turn("bob", "fake-model", 100, 100)
    metering.record_llm_turn("alice", "fake-model", 100, 100)

    result = metering.report(group="user", user="bob", prices=prices)
    assert [r["user"] for r in result["rows"]] == ["bob"]


def test_report_by_day(governance_db, prices):
    metering.record_llm_turn("bob", "fake-model", 500, 500)
    result = metering.report(group="day", prices=prices)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["day"]
    assert result["rows"][0]["input_tokens"] == 500


def test_report_by_tool_counts_calls(governance_db, prices):
    for _ in range(3):
        metering.record_tool_call("bob", "list_issues")
    metering.record_tool_call("bob", "query_books")
    metering.record_tool_call("alice", "list_issues")

    result = metering.report(group="tool", prices=prices)
    counts = {(r["user"], r["tool"]): r["calls"] for r in result["rows"]}
    assert counts[("bob", "list_issues")] == 3
    assert counts[("bob", "query_books")] == 1
    assert counts[("alice", "list_issues")] == 1
    assert result["totals"]["calls"] == 5


def test_unpriced_model_is_reported_not_hidden(governance_db, prices):
    metering.record_llm_turn("bob", "mystery-model", 1000, 1000)
    result = metering.report(group="user", prices=prices)

    assert result["rows"][0]["estimated_cost"] is None
    assert result["models_without_prices"] == ["mystery-model"]
    # A null cost must not be silently counted as zero in a way that hides it.
    assert result["totals"]["estimated_cost"] == 0.0


def test_report_rejects_bad_group(governance_db, prices):
    with pytest.raises(ValueError, match="group must be one of"):
        metering.report(group="sideways", prices=prices)


def test_empty_report_is_well_formed(governance_db, prices):
    result = metering.report(group="user", prices=prices)
    assert result["rows"] == []
    assert result["totals"]["input_tokens"] == 0


# --- agent integration with a fake LLM ------------------------------------


def _message(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _tool_call(call_id, name, arguments):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _completion(message, prompt_tokens, completion_tokens):
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


class FakeLLM:
    """Returns queued completions with fixed, known token counts."""

    def __init__(self, script):
        self._script = list(script)
        self.requests = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_agent_run_meters_tokens_and_tool_calls(governance_db, monkeypatch, prices):
    monkeypatch.setenv("POLICY_FILE", "policy.example.yaml")
    llm = FakeLLM(
        [
            _completion(
                _message(tool_calls=[_tool_call("c1", "query_books", {"genre": "fantasy"})]),
                prompt_tokens=1200,
                completion_tokens=400,
            ),
            _completion(_message(content="Four books."), prompt_tokens=800, completion_tokens=600),
        ]
    )
    monkeypatch.setattr(agent, "OpenAI", lambda *a, **k: llm)

    answer = await agent.run(
        "fantasy books?", model="fake-model", max_turns=5, user="bob"
    )
    assert answer == "Four books."

    usage = metering.report(group="user", prices=prices)
    row = usage["rows"][0]
    assert row["user"] == "bob"
    assert row["model"] == "fake-model"
    assert row["turns"] == 2
    assert row["input_tokens"] == 2000
    assert row["output_tokens"] == 1000
    assert row["estimated_cost"] == 0.021

    tools = metering.report(group="tool", prices=prices)
    assert tools["rows"] == [{"user": "bob", "tool": "query_books", "calls": 1}]


@pytest.mark.asyncio
async def test_agent_without_user_records_nothing(governance_db, monkeypatch, prices):
    """The ungoverned path stays exactly as it was: no identity, no metering."""
    llm = FakeLLM([_completion(_message(content="hi"), prompt_tokens=10, completion_tokens=5)])
    monkeypatch.setattr(agent, "OpenAI", lambda *a, **k: llm)

    answer = await agent.run("hello", model="fake-model", max_turns=3)

    assert answer == "hi"
    assert metering.report(group="user", prices=prices)["rows"] == []
