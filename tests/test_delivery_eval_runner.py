"""The eval runner must work on a fresh clone, with no database anywhere.

It builds its own database from the recorded fixture and removes it, so these
tests also check that it leaves nothing behind and never reaches for the
default database.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import delivery_fixtures
import store

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "evals" / "run_delivery_eval.py"


def _run(tmp_path, env_extra=None, args=(), timeout=300):
    """Run the script as a subprocess from a clean working directory."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_runner_scores_twenty_from_a_clean_directory(tmp_path):
    """The reported bug: this used to die on `no such table: dm_repos`."""
    workdir = tmp_path / "clean"
    workdir.mkdir()

    result = _run(workdir)

    assert result.returncode == 0, result.stderr
    assert "20/20 passed" in result.stdout
    assert "no such table" not in result.stderr
    assert "Traceback" not in result.stderr


def test_runner_leaves_no_database_behind(tmp_path):
    workdir = tmp_path / "clean"
    workdir.mkdir()
    pointed_at = workdir / "must_not_appear.db"

    result = _run(workdir, {"GOVERNANCE_DB": str(pointed_at)})

    assert result.returncode == 0, result.stderr
    assert "20/20 passed" in result.stdout
    # It builds its own temporary database, so the one it was pointed at by
    # the environment is never created.
    assert not pointed_at.exists()
    assert list(workdir.iterdir()) == []


def test_runner_does_not_touch_the_default_database(tmp_path):
    """Whatever the default path holds must be left exactly as it was."""
    default = store.db_path()
    before = default.stat().st_mtime_ns if default.exists() else None

    workdir = tmp_path / "clean"
    workdir.mkdir()
    result = _run(workdir)

    assert result.returncode == 0, result.stderr
    after = default.stat().st_mtime_ns if default.exists() else None
    assert after == before


def test_runner_json_output(tmp_path):
    workdir = tmp_path / "clean"
    workdir.mkdir()

    result = _run(workdir, args=("--json",))

    payload = json.loads(result.stdout)
    assert payload["mode"] == "tools"
    assert payload["total"] == 20
    assert payload["passed"] == 20
    assert all("id" in row for row in payload["results"])


def test_db_option_uses_the_given_database_and_warns(tmp_path):
    """--db reads an existing database and says the numbers will not match."""
    existing = tmp_path / "existing.db"
    with delivery_fixtures.ingested_database(db=existing):
        pass  # the file is kept because the path was explicit
    assert existing.exists()

    workdir = tmp_path / "clean"
    workdir.mkdir()
    result = _run(workdir, args=("--db", str(existing)))

    assert "will not match live data" in result.stderr
    # That database really does hold the fixture, so the questions still pass.
    assert "20/20 passed" in result.stdout


def test_db_option_warns_when_the_file_is_absent(tmp_path):
    workdir = tmp_path / "clean"
    workdir.mkdir()
    missing = tmp_path / "nowhere.db"

    result = _run(workdir, args=("--db", str(missing)))

    assert "does not exist yet" in result.stderr
    # Everything is refused rather than crashing, so the answers do not match.
    assert "20/20 passed" not in result.stdout
    assert "Traceback" not in result.stderr


def test_real_mode_without_a_key_exits_cleanly(tmp_path):
    workdir = tmp_path / "clean"
    workdir.mkdir()

    result = _run(workdir, args=("--mode", "real"))

    assert result.returncode == 2
    assert "OPENAI_API_KEY" in result.stderr


def test_eval_database_restores_the_previous_environment(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "evals"))
    import run_delivery_eval as runner

    monkeypatch.setenv("GOVERNANCE_DB", "/somewhere/else.db")
    with runner.eval_database(None) as path:
        import os

        assert os.environ["GOVERNANCE_DB"] == str(path)
    import os

    assert os.environ["GOVERNANCE_DB"] == "/somewhere/else.db"


def test_eval_database_clears_an_unset_variable(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "evals"))
    import run_delivery_eval as runner

    monkeypatch.delenv("GOVERNANCE_DB", raising=False)
    with runner.eval_database(None):
        pass
    import os

    assert "GOVERNANCE_DB" not in os.environ


@pytest.mark.parametrize("metric", ["pr_cycle_time", "workflow_failure_rate"])
def test_tools_refuse_instead_of_crashing_on_an_empty_database(
    tmp_path, monkeypatch, metric
):
    """A database that was never ingested must refuse, not raise.

    This is the failure the runner hit: dm_repos did not exist, so the tool
    raised OperationalError instead of reporting that nothing was ingested.
    """
    monkeypatch.setenv("GOVERNANCE_DB", str(tmp_path / "fresh.db"))
    import delivery_tools

    result = delivery_tools.delivery_metric("psf/cachecontrol", metric)

    assert result["refused"] is True
    assert "not been ingested" in result["reason"]
    assert result["ingested_repos"] == []


# --- real mode, driven by a stubbed model -------------------------------


class _StubModel:
    """Stands in for openai.OpenAI for one agent.run call.

    Plays a cooperative model: first turn issues the tool call the question
    declares, second turn answers with whatever the tool returned. That
    exercises the whole real-mode path — agent loop, governed server, policy,
    tools, scoring — without any API key.
    """

    def __init__(self, tool: str, arguments: dict) -> None:
        self._tool = tool
        self._arguments = arguments
        self._called = False
        import types

        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        import json as _json
        import types

        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)

        if not self._called and kwargs.get("tools"):
            self._called = True
            call = types.SimpleNamespace(
                id="call-1",
                function=types.SimpleNamespace(
                    name=self._tool, arguments=_json.dumps(self._arguments)
                ),
            )
            message = types.SimpleNamespace(content=None, tool_calls=[call])
        else:
            last_tool_message = ""
            for entry in reversed(kwargs.get("messages") or []):
                if entry.get("role") == "tool":
                    last_tool_message = entry.get("content") or ""
                    break
            message = types.SimpleNamespace(
                content=last_tool_message or "(no tool result)", tool_calls=[]
            )

        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message)], usage=usage
        )


def test_real_mode_runs_all_twenty_with_a_stubbed_model(monkeypatch):
    """Requirement: the real path must run and score, with no API key."""
    sys.path.insert(0, str(REPO_ROOT / "evals"))
    import asyncio

    import run_delivery_eval as runner

    import agent
    from tests.delivery_eval_core import (
        expectations,
        load_questions,
        resolve_arguments,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-used-by-the-stub")

    _, questions = load_questions()
    expected = expectations()
    pending = [
        (q["tool"], resolve_arguments(q.get("arguments") or {}, expected))
        for q in questions
    ]
    order = iter(pending)

    monkeypatch.setattr(agent, "OpenAI", lambda *a, **k: _StubModel(*next(order)))

    with runner.eval_database(None):
        results = asyncio.run(runner._run_real("stub-model", "bob", 4))

    assert len(results) == 20
    for row in results:
        assert isinstance(row["passed"], bool)
        assert isinstance(row["failures"], list)
        assert "answer" in row
        assert not any(
            "Traceback" in failure or "OperationalError" in failure
            for failure in row["failures"]
        ), row

    passed = sum(1 for r in results if r["passed"])
    # A cooperative model that reports exactly what the tool returned should
    # satisfy the rule checks on every question.
    assert passed == 20, [(r["id"], r["failures"]) for r in results if not r["passed"]]


# --- real mode over HTTP, against a local fake model --------------------


def _plan_from_questions():
    """Map each question to the tool call that should answer it."""
    from tests.delivery_eval_core import (
        expectations,
        load_questions,
        resolve_arguments,
    )

    _, questions = load_questions()
    expected = expectations()
    return {
        q["question"]: (q["tool"], resolve_arguments(q.get("arguments") or {}, expected))
        for q in questions
    }, questions


def test_real_mode_over_http_against_a_fake_model(monkeypatch):
    """Regression guard for the replay layer leaking into the model's client.

    Unlike the stubbed-client test, this leaves openai's own transport in
    place and points it at a local server, so anything that breaks httpx for
    the model shows up here. When replaying() rebound the global
    httpx.Client, every question failed with a TypeError from
    isinstance(value, httpx.Client) inside openai's request builder, and the
    fake server saw no requests at all.
    """
    sys.path.insert(0, str(REPO_ROOT / "evals"))
    import asyncio

    import run_delivery_eval as runner

    from tests.fake_openai import FakeOpenAI

    plan, questions = _plan_from_questions()

    with FakeOpenAI(plan) as fake:
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-not-used")
        monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
        monkeypatch.setenv("POLICY_FILE", "policy.example.yaml")

        with runner.eval_database(None):
            results = asyncio.run(runner._run_real("fake-model", "bob", 4))

        # The model was really reached over HTTP: two calls per question,
        # one to choose the tool and one to answer.
        assert len(fake.requests) == 2 * len(questions)

    assert len(results) == 20
    assert all(
        not any("TypeError" in failure for failure in row["failures"]) for row in results
    ), [r["failures"] for r in results if r["failures"]]

    passed = sum(1 for r in results if r["passed"])
    assert passed == 20, [(r["id"], r["failures"]) for r in results if not r["passed"]]

    # Each question reached the tool the question declares.
    for row, question in zip(results, questions, strict=True):
        assert row["id"] == question["id"]
        called = [call["tool"] for call in row["trace"]]
        assert called == [question["tool"]], (row["id"], called)


def test_replay_layer_leaves_the_global_httpx_client_alone():
    """httpx.Client must stay a class: libraries check isinstance against it."""
    import httpx

    import delivery_fixtures

    before = httpx.Client
    with delivery_fixtures.replaying("github_delivery"):
        assert httpx.Client is before
        assert isinstance(httpx.Client, type)
        # An unrelated client still behaves normally.
        isinstance(httpx.Client(), httpx.Client)
    assert httpx.Client is before


def test_replay_layer_refuses_a_non_github_host():
    """The GitHub fixture must never answer for another origin."""
    import httpx

    import delivery_fixtures

    transport = delivery_fixtures.mock_transport("github_delivery")
    with httpx.Client(transport=transport) as client:
        with pytest.raises(AssertionError, match="not the configured GitHub origin"):
            client.post("https://api.openai.com/v1/chat/completions", json={})


def test_ingested_database_does_not_replay_after_the_ingest():
    """The replay layer is off by the time the caller gets the database."""
    import delivery_fixtures
    import delivery_ingest

    original = delivery_ingest.http_client
    with delivery_fixtures.ingested_database() as db:
        assert db.exists()
        assert delivery_ingest.http_client is original
