"""The /v1/responses event stream: ids, order, and what the SDK accepts.

The SDK check is offline. Each event this gateway emits is parsed with the
installed openai package's own stream-event models, with no network and no
client, so "the SDK accepts these events" is asserted rather than claimed.
"""

import json

import pydantic
import pytest
from fastapi.testclient import TestClient
from openai.types.responses import ResponseStreamEvent

from gateway.providers.fake import FakeProvider
from tests.gateway_support import HEADERS, build, sse_events

# The sequence this gateway emits, in order.
EXPECTED_EVENTS = [
    "response.created",
    "response.output_item.added",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
    "response.completed",
]

# The fake yields one delta per word, so this reply gives three deltas.
REPLY = "a b c"


@pytest.fixture
def streamed(governance_db):
    """Drive one streamed response and hand back its parsed events."""
    app, fake = build(governance_db, FakeProvider(reply=REPLY))
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "hi", "stream": True},
        ) as response:
            response.read()
        yield sse_events(response), client, fake


def item_ids(events) -> set[str]:
    """Every message item id mentioned anywhere in the stream."""
    found: set[str] = set()
    for entry in events:
        data = entry["data"]
        if data.get("item_id"):
            found.add(data["item_id"])
        item = data.get("item") or {}
        if item.get("id"):
            found.add(item["id"])
        for output in (data.get("response") or {}).get("output") or []:
            if output.get("id"):
                found.add(output["id"])
    return found


# --- one stable item id -----------------------------------------------


def test_one_stable_item_id_across_every_event(streamed):
    events, _, _ = streamed
    assert len(item_ids(events)) == 1


def test_the_item_id_appears_in_created_deltas_done_and_completed(streamed):
    events, _, _ = streamed
    by_type = {entry["data"]["type"]: entry["data"] for entry in events}
    the_id = item_ids(events).pop()

    assert by_type["response.created"]["response"]["output"][0]["id"] == the_id
    assert by_type["response.output_text.delta"]["item_id"] == the_id
    assert by_type["response.output_text.done"]["item_id"] == the_id
    assert by_type["response.completed"]["response"]["output"][0]["id"] == the_id


def test_the_item_id_looks_like_a_message_id(streamed):
    events, _, _ = streamed
    assert item_ids(events).pop().startswith("msg_")


# --- exact order ------------------------------------------------------


def test_the_event_order_is_exact(streamed):
    events, _, _ = streamed
    assert [entry["data"]["type"] for entry in events] == EXPECTED_EVENTS


def test_the_sse_event_name_matches_the_payload_type(streamed):
    events, _, _ = streamed
    for entry in events:
        assert entry["event"] == entry["data"]["type"]


def test_delta_count_matches_the_reply(streamed):
    events, _, _ = streamed
    deltas = [e["data"]["delta"] for e in events
              if e["data"]["type"] == "response.output_text.delta"]
    assert deltas == ["a", " b", " c"]
    assert "".join(deltas) == REPLY


def test_the_done_and_completed_events_carry_the_whole_text(streamed):
    events, _, _ = streamed
    by_type = {entry["data"]["type"]: entry["data"] for entry in events}
    assert by_type["response.output_text.done"]["text"] == REPLY
    assert by_type["response.content_part.done"]["part"]["text"] == REPLY
    assert by_type["response.output_item.done"]["item"]["content"][0]["text"] == REPLY
    assert by_type["response.completed"]["response"]["output_text"] == REPLY


def test_status_moves_from_in_progress_to_completed(streamed):
    events, _, _ = streamed
    by_type = {entry["data"]["type"]: entry["data"] for entry in events}
    assert by_type["response.created"]["response"]["status"] == "in_progress"
    assert by_type["response.completed"]["response"]["status"] == "completed"


# --- sequence_number --------------------------------------------------


def test_sequence_numbers_are_present_and_consecutive_from_zero(streamed):
    """Required by the SDK's event models, so it is asserted here."""
    events, _, _ = streamed
    numbers = [entry["data"]["sequence_number"] for entry in events]
    assert numbers == list(range(len(events)))


# --- the installed SDK accepts every event, offline -------------------


def test_every_event_validates_against_the_sdk_models(streamed):
    events, _, _ = streamed
    adapter = pydantic.TypeAdapter(ResponseStreamEvent)

    parsed = [adapter.validate_python(entry["data"]) for entry in events]

    assert [event.type for event in parsed] == EXPECTED_EVENTS
    assert len({getattr(e, "item_id", None) for e in parsed if hasattr(e, "item_id")}) == 1


def test_the_raw_sse_bytes_validate_against_the_sdk(governance_db):
    """Parsed from the wire, not from a helper, so framing is covered too."""
    adapter = pydantic.TypeAdapter(ResponseStreamEvent)
    app, _ = build(governance_db, FakeProvider(reply="one two"))

    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/responses", headers=HEADERS,
            json={"model": "fake-1", "input": "hi", "stream": True},
        ) as response:
            response.read()

    seen = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            seen.append(adapter.validate_python(json.loads(line[len("data:"):])))

    assert [event.type for event in seen][0] == "response.created"
    assert [event.type for event in seen][-1] == "response.completed"
    assert len(seen) == 9  # two deltas rather than three


# --- the session still works after a streamed response ----------------


def test_previous_response_id_works_after_a_streamed_response(streamed):
    events, client, fake = streamed
    completed = [e["data"] for e in events
                 if e["data"]["type"] == "response.completed"][0]
    response_id = completed["response"]["id"]

    follow_up = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "next", "previous_response_id": response_id},
    )

    assert follow_up.status_code == 200
    # The stored conversation carried the streamed reply through.
    assert [m["content"] for m in fake.calls[-1]["messages"]] == ["hi", REPLY, "next"]


def test_the_stored_session_uses_the_response_id_not_the_item_id(streamed):
    events, client, _ = streamed
    the_item_id = item_ids(events).pop()

    refused = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "x", "previous_response_id": the_item_id},
    )
    assert refused.status_code == 404


def test_a_streamed_chain_can_continue_twice(governance_db):
    app, fake = build(governance_db, FakeProvider(scripted=["first", "second"]))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/responses", headers=HEADERS,
            json={"model": "fake-1", "input": "one", "stream": True},
        ) as response:
            response.read()
        first_id = [
            e["data"]["response"]["id"]
            for e in sse_events(response)
            if e["data"]["type"] == "response.completed"
        ][0]

        second = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "two", "previous_response_id": first_id},
        ).json()

    assert second["output_text"] == "second"
    assert [m["content"] for m in fake.calls[-1]["messages"]] == ["one", "first", "two"]


# --- chat streaming sends the role once -------------------------------


def test_chat_streaming_sends_the_role_only_in_the_first_chunk(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="x y z"))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/chat/completions", headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "q"}],
                  "stream": True},
        ) as response:
            response.read()

    chunks = [e["data"] for e in sse_events(response) if "data" in e]
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]

    assert deltas[0]["role"] == "assistant"
    assert all("role" not in delta for delta in deltas[1:])
    assert "".join(d.get("content", "") for d in deltas) == "x y z"
