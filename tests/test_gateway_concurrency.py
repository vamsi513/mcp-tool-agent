"""The gateway under parallel load, offline.

Every test here drives the app through TestClient from a thread pool. The
endpoints are plain `def`, so the framework runs each one in its own worker
thread: the parallelism is real, and so is anything that races.

No provider is reached. The echo provider below answers from the request it
was given, which is what lets the streaming test tell two concurrent
responses apart.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gateway.budgets import AdmissionGate
from gateway.config import KeyConfig
from gateway.providers.base import Completion
from gateway.translate import content_to_text
from tests.gateway_support import HEADERS, build, sse_events

WORKERS = 24


class EchoProvider:
    """Replies with the text it was sent, so each caller's reply is unique.

    FakeProvider answers with one canned string, which cannot show whether
    two concurrent responses were kept apart. This one makes every reply
    traceable to the request that caused it.
    """

    name = "fake"

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.concurrent = 0
        self.peak_concurrent = 0
        self._guard = threading.Lock()

    def _enter(self) -> None:
        with self._guard:
            self.concurrent += 1
            self.peak_concurrent = max(self.peak_concurrent, self.concurrent)

    def _leave(self) -> None:
        with self._guard:
            self.concurrent -= 1

    def _prompt(self, messages: list[dict]) -> str:
        return " ".join(content_to_text(m.get("content")) for m in messages)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Completion:
        self._enter()
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            reply = self._prompt(messages)
            return Completion(
                text=reply,
                finish_reason="stop",
                input_tokens=len(reply.split()),
                output_tokens=len(reply.split()),
                model=model,
            )
        finally:
            self._leave()

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Generator[str, None, Completion]:
        self._enter()
        try:
            reply = self._prompt(messages)
            words = reply.split()
            for index, word in enumerate(words):
                # A pause between deltas, so two streams genuinely overlap
                # rather than each finishing before the next begins.
                if self.delay_seconds:
                    time.sleep(self.delay_seconds / max(1, len(words)))
                yield word if index == 0 else " " + word
            return Completion(
                text=reply,
                finish_reason="stop",
                input_tokens=len(words),
                output_tokens=len(words),
                model=model,
            )
        finally:
            self._leave()


def in_parallel(work, count: int, workers: int = WORKERS) -> list:
    """Run `work(i)` for i in range(count), returning results in order."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, range(count)))


# --------------------------------------------------------- (a) parallel load


def test_parallel_chat_requests_all_succeed_with_their_own_body(tmp_path):
    app, _ = build(tmp_path / "c.db", provider=EchoProvider(delay_seconds=0.01))
    client = TestClient(app)
    count = 40

    def call(index: int):
        marker = f"marker{index}"
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": marker}]},
        )
        return index, response

    for index, response in in_parallel(call, count):
        assert response.status_code == 200
        body = response.json()
        # Each reply must be the marker this caller sent, not another's.
        assert body["choices"][0]["message"]["content"] == f"marker{index}"
        assert body["object"] == "chat.completion"
        assert body["usage"]["total_tokens"] == 2


def test_parallel_requests_really_do_overlap(tmp_path):
    """Guards the other tests: without overlap they would prove nothing."""
    provider = EchoProvider(delay_seconds=0.05)
    app, _ = build(tmp_path / "c.db", provider=provider)
    client = TestClient(app)

    def call(index: int):
        return client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        ).status_code

    assert set(in_parallel(call, 20)) == {200}
    assert provider.peak_concurrent > 1, (
        "requests ran one at a time, so the concurrency tests prove nothing"
    )


def test_every_parallel_request_is_metered_exactly_once(tmp_path):
    import metering

    db = tmp_path / "c.db"
    app, _ = build(db, provider=EchoProvider(delay_seconds=0.005))
    client = TestClient(app)
    count = 30

    def call(index: int):
        return client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "one"}]},
        ).status_code

    assert set(in_parallel(call, count)) == {200}
    report = metering.report(group="user", db=db)
    assert report["totals"]["input_tokens"] == count
    assert report["totals"]["output_tokens"] == count


# ------------------------------------------------------------- (b) budgets


def spend(db) -> float:
    import metering
    from tests.gateway_support import prices

    report = metering.report(group="user", prices=prices(), db=db)
    return float(report["totals"].get("estimated_cost") or 0.0)


def test_a_budget_is_not_overshot_by_concurrent_requests(tmp_path):
    """The budget must hold under parallel load, not just one call at a time.

    Each request here costs two units: the echo provider reports one input
    and one output token, and the test price table charges one unit per
    token. The budget allows exactly one request, so once that request has
    been recorded every later one must be refused. The most that can slip
    through is the request already in flight when the limit was crossed,
    which is why the ceiling below is the budget plus one request.
    """
    db = tmp_path / "c.db"
    cost_of_one_request = 2.0
    budget = cost_of_one_request
    app, _ = build(db, provider=EchoProvider(delay_seconds=0.02), budget=budget)
    client = TestClient(app)

    def call(index: int):
        return client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )

    responses = in_parallel(call, 30)
    codes = [r.status_code for r in responses]
    accepted = codes.count(200)
    refused = codes.count(429)

    assert accepted >= 1, "every request was refused; the budget never allowed one"
    assert accepted + refused == len(codes), f"unexpected statuses: {set(codes)}"
    for response in responses:
        if response.status_code == 429:
            assert response.json()["error"]["code"] == "budget_exceeded"

    recorded = spend(db)
    assert recorded <= budget + cost_of_one_request, (
        f"budget overshot: {recorded} recorded against a budget of {budget}, "
        f"which allows at most {budget + cost_of_one_request}. "
        f"{accepted} of {len(codes)} requests were admitted."
    )


def test_an_abandoned_stream_gives_the_budget_window_back(tmp_path):
    """A client that hangs up must not block the key behind it.

    A streamed request holds its key's check-and-charge window until the
    charge is written in the final frame. If a caller walks away mid-stream
    the window has to be released anyway, or the next request on that key
    waits forever. The follow-up call below runs with a deadline so a leak
    shows up as a failure rather than a hung test.
    """
    db = tmp_path / "c.db"
    app, _ = build(db, provider=EchoProvider(delay_seconds=0.05), budget=1000.0)
    client = TestClient(app)
    body = {
        "model": "fake-1",
        "messages": [{"role": "user", "content": "a b c d e"}],
        "stream": True,
    }

    with client.stream("POST", "/v1/chat/completions", headers=HEADERS, json=body) as r:
        assert r.status_code == 200
        # Read nothing and leave: the generator is closed unfinished.

    # A daemon thread rather than a pool: if the window leaked, this call
    # never returns, and waiting on a pool's shutdown would hang the run
    # instead of failing this test.
    outcome: list[int] = []
    done = threading.Event()

    def follow_up() -> None:
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )
        outcome.append(response.status_code)
        done.set()

    threading.Thread(target=follow_up, daemon=True).start()
    assert done.wait(timeout=10), "the abandoned stream never released the key"
    assert outcome == [200]


def acquires_within(admission, seconds: float = 2.0) -> bool:
    """Whether a second thread can take this window inside the deadline.

    A plain daemon thread, not a pool: a pool's shutdown waits for its
    worker, so a window that never frees up would hang the test run instead
    of failing it.
    """
    taken = threading.Event()

    def take() -> None:
        admission.acquire()
        taken.set()

    threading.Thread(target=take, daemon=True).start()
    if not taken.wait(timeout=seconds):
        return False
    admission.release()
    return True


def test_a_budgeted_key_does_not_hold_up_another_key():
    gate = AdmissionGate()
    one = KeyConfig(id="one", secret="s1", monthly_budget_usd=5.0)
    two = KeyConfig(id="two", secret="s2", monthly_budget_usd=5.0)

    held = gate.admit(one)
    held.acquire()
    try:
        assert acquires_within(gate.admit(two)), "a different key had to wait"
    finally:
        held.release()


def test_a_budgeted_key_admits_one_request_at_a_time():
    """The negative check is last: it leaves a waiter parked on the lock."""
    gate = AdmissionGate()
    one = KeyConfig(id="one", secret="s1", monthly_budget_usd=5.0)

    held = gate.admit(one)
    held.acquire()
    try:
        assert not acquires_within(gate.admit(one), seconds=0.3), (
            "the same key was admitted twice at once"
        )
    finally:
        held.release()


def test_releasing_gives_the_window_back():
    gate = AdmissionGate()
    one = KeyConfig(id="one", secret="s1", monthly_budget_usd=5.0)

    held = gate.admit(one)
    held.acquire()
    held.release()
    assert acquires_within(gate.admit(one)), "the window was not given back"
    # Releasing a window twice must not raise or free someone else's.
    held.release()
    assert acquires_within(gate.admit(one))


def test_a_key_without_a_budget_takes_no_lock():
    gate = AdmissionGate()
    unlimited = KeyConfig(id="free", secret="s", monthly_budget_usd=None)
    first = gate.admit(unlimited)
    first.acquire()
    second = gate.admit(unlimited)
    second.acquire()  # would block if an unbudgeted key took a real lock
    assert not first.held and not second.held
    first.release()
    second.release()


def test_a_key_without_a_budget_is_never_refused_under_load(tmp_path):
    db = tmp_path / "c.db"
    app, _ = build(db, provider=EchoProvider(delay_seconds=0.01), budget=None)
    client = TestClient(app)

    def call(index: int):
        return client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        ).status_code

    assert set(in_parallel(call, 30)) == {200}


# ----------------------------------------------------------- (c) streaming


def test_parallel_streams_do_not_mix_their_chunks(tmp_path):
    app, _ = build(tmp_path / "c.db", provider=EchoProvider(delay_seconds=0.03))
    client = TestClient(app)
    count = 16

    def call(index: int):
        words = " ".join(f"w{index}x{position}" for position in range(5))
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={
                "model": "fake-1",
                "messages": [{"role": "user", "content": words}],
                "stream": True,
            },
        )
        return index, words, response

    for index, words, response in in_parallel(call, count):
        assert response.status_code == 200
        events = sse_events(response)
        assert events[-1].get("done") is True
        text = "".join(
            event["data"]["choices"][0]["delta"].get("content") or ""
            for event in events
            if not event.get("done")
        )
        assert text == words, f"stream {index} came back as {text!r}"
        # Nothing from another stream may appear in this one.
        others = {f"w{other}x" for other in range(count) if other != index}
        assert not any(marker in text for marker in others)
        ids = {
            event["data"]["id"] for event in events if not event.get("done")
        }
        assert len(ids) == 1, "one stream must carry one completion id"


def test_parallel_responses_streams_keep_one_item_id_each(tmp_path):
    app, _ = build(tmp_path / "c.db", provider=EchoProvider(delay_seconds=0.03))
    client = TestClient(app)
    count = 12

    def call(index: int):
        words = " ".join(f"r{index}x{position}" for position in range(4))
        response = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": words, "stream": True},
        )
        return index, words, response

    seen_item_ids: list[str] = []
    for _index, words, response in in_parallel(call, count):
        assert response.status_code == 200
        events = sse_events(response)
        assert [e["event"] for e in events][-1] == "response.completed"
        final = events[-1]["data"]["response"]
        assert final["output_text"] == words
        item_ids = {
            event["data"]["item_id"]
            for event in events
            if "item_id" in event.get("data", {})
        }
        assert len(item_ids) == 1
        seen_item_ids.append(item_ids.pop())

    # Two concurrent streams must not share an item id either.
    assert len(set(seen_item_ids)) == count


# ------------------------------------------------------------ (d) sessions


def test_parallel_turns_on_one_previous_response_id_each_branch(tmp_path):
    """Defined behaviour: concurrent continuations branch, they do not merge.

    Each request creates a new response id of its own and stores its own
    continuation, so N parallel turns on one parent produce N independent
    children. The parent is left untouched. No request overwrites another's
    history, and none of them sees another's turn.
    """
    db = tmp_path / "c.db"
    app, _ = build(db, provider=EchoProvider(delay_seconds=0.02))
    client = TestClient(app)

    first = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "parent turn"},
    )
    assert first.status_code == 200
    parent_id = first.json()["id"]
    count = 20

    def call(index: int):
        return client.post(
            "/v1/responses",
            headers=HEADERS,
            json={
                "model": "fake-1",
                "input": f"child{index}",
                "previous_response_id": parent_id,
            },
        )

    responses = in_parallel(call, count)
    assert {r.status_code for r in responses} == {200}

    child_ids = [r.json()["id"] for r in responses]
    assert len(set(child_ids)) == count, "each continuation needs its own id"
    assert parent_id not in child_ids

    from gateway.sessions import SqliteSessionStore

    store = SqliteSessionStore(db)
    for index, child_id in enumerate(child_ids):
        session = store.get(child_id)
        assert session is not None
        contents = [m["content"] for m in session.messages]
        # Parent history, this child's turn, and this child's reply. Nothing
        # belonging to a sibling.
        assert contents[0] == "parent turn"
        assert f"child{index}" in contents
        siblings = {f"child{other}" for other in range(count) if other != index}
        assert not siblings.intersection(contents)

    parent = store.get(parent_id)
    assert parent is not None
    assert [m["content"] for m in parent.messages] == ["parent turn", "parent turn"]


def test_an_expired_parent_is_a_404_for_every_parallel_child(tmp_path):
    app, _ = build(tmp_path / "c.db", provider=EchoProvider(delay_seconds=0.01))
    client = TestClient(app)

    def call(index: int):
        return client.post(
            "/v1/responses",
            headers=HEADERS,
            json={
                "model": "fake-1",
                "input": "x",
                "previous_response_id": "resp_missing",
            },
        )

    responses = in_parallel(call, 20)
    assert {r.status_code for r in responses} == {404}
    assert {r.json()["error"]["code"] for r in responses} == {"response_not_found"}


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
def test_errors_stay_correct_under_parallel_load(tmp_path, endpoint):
    app, _ = build(tmp_path / "c.db", provider=EchoProvider(delay_seconds=0.005))
    client = TestClient(app)

    def call(index: int):
        body = {"model": "nope-1"}
        if endpoint == "/v1/chat/completions":
            body["messages"] = [{"role": "user", "content": "x"}]
        else:
            body["input"] = "x"
        return client.post(endpoint, headers=HEADERS, json=body)

    responses = in_parallel(call, 20)
    assert {r.status_code for r in responses} == {400}
    assert {r.json()["error"]["code"] for r in responses} == {"model_not_found"}
