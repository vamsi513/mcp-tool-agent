"""The Anthropic mapping, case by case, as pure functions."""

import pytest

from gateway.translate import (
    STOP_REASON_TO_FINISH_REASON,
    content_to_text,
    finish_reason_from_stop_reason,
    merge_consecutive_roles,
    responses_input_to_messages,
    split_system,
    text_from_anthropic_content,
    to_anthropic_request,
)

# --- system and instructions go to the top-level system field ---------


def test_system_turns_are_hoisted_out_of_the_messages():
    system, rest = split_system(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "Also cite."},
        ]
    )
    assert system == "Be terse.\n\nAlso cite."
    assert rest == [{"role": "user", "content": "hi"}]


def test_developer_role_counts_as_system():
    system, rest = split_system([{"role": "developer", "content": "rules"}])
    assert system == "rules"
    assert rest == []


def test_instructions_precede_system_turns():
    body = to_anthropic_request(
        [{"role": "system", "content": "from message"}, {"role": "user", "content": "hi"}],
        model="claude-3-5-sonnet",
        max_tokens=None,
        default_max_tokens=100,
        extra_system="from instructions",
    )
    assert body["system"] == "from instructions\n\nfrom message"
    assert all(m["role"] != "system" for m in body["messages"])


def test_no_system_field_when_there_is_none():
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}],
        model="claude-3-5-sonnet",
        max_tokens=None,
        default_max_tokens=100,
    )
    assert "system" not in body


# --- consecutive same-role turns are merged ---------------------------


def test_consecutive_user_turns_are_merged():
    merged = merge_consecutive_roles(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "c"},
        ]
    )
    assert merged == [
        {"role": "user", "content": "a\n\nb"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "c"},
    ]


def test_consecutive_assistant_turns_are_merged():
    merged = merge_consecutive_roles(
        [
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
    )
    assert merged == [{"role": "assistant", "content": "one\n\ntwo"}]


def test_three_in_a_row_collapse_to_one():
    merged = merge_consecutive_roles([{"role": "user", "content": c} for c in "abc"])
    assert merged == [{"role": "user", "content": "a\n\nb\n\nc"}]


def test_merged_roles_alternate():
    merged = merge_consecutive_roles(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "c"},
        ]
    )
    roles = [m["role"] for m in merged]
    assert roles == ["user", "assistant", "user"]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))


def test_an_empty_turn_does_not_add_a_separator():
    merged = merge_consecutive_roles(
        [{"role": "user", "content": "a"}, {"role": "user", "content": ""}]
    )
    assert merged == [{"role": "user", "content": "a"}]


# --- max_tokens gets a configured default -----------------------------


def test_missing_max_tokens_uses_the_configured_default():
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}],
        model="claude-3-5-sonnet",
        max_tokens=None,
        default_max_tokens=321,
    )
    assert body["max_tokens"] == 321


def test_supplied_max_tokens_wins():
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}],
        model="claude-3-5-sonnet",
        max_tokens=7,
        default_max_tokens=321,
    )
    assert body["max_tokens"] == 7


def test_temperature_is_omitted_when_not_set():
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=1, default_max_tokens=1
    )
    assert "temperature" not in body
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=1,
        default_max_tokens=1, temperature=0.0,
    )
    assert body["temperature"] == 0.0


def test_stream_flag_is_passed_through():
    body = to_anthropic_request(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=1,
        default_max_tokens=1, stream=True,
    )
    assert body["stream"] is True


# --- stop_reason maps to finish_reason --------------------------------


@pytest.mark.parametrize(
    "stop_reason, finish_reason",
    [("end_turn", "stop"), ("max_tokens", "length"), ("stop_sequence", "stop"),
     ("tool_use", "tool_calls"), ("refusal", "content_filter")],
)
def test_stop_reason_mapping(stop_reason, finish_reason):
    assert finish_reason_from_stop_reason(stop_reason) == finish_reason


@pytest.mark.parametrize("value", [None, "", "something_new", 7])
def test_an_unknown_stop_reason_falls_back_to_stop(value):
    assert finish_reason_from_stop_reason(value) == "stop"


def test_the_two_required_mappings_are_present():
    assert STOP_REASON_TO_FINISH_REASON["end_turn"] == "stop"
    assert STOP_REASON_TO_FINISH_REASON["max_tokens"] == "length"


# --- content flattening ----------------------------------------------


def test_content_parts_are_joined_in_order():
    assert content_to_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


def test_non_text_parts_are_dropped():
    assert content_to_text([{"type": "image_url", "image_url": {}}, {"type": "text",
                                                                    "text": "x"}]) == "x"


def test_anthropic_text_blocks_are_joined_and_others_skipped():
    assert text_from_anthropic_content(
        [{"type": "text", "text": "he"}, {"type": "thinking", "thinking": "hmm"},
         {"type": "text", "text": "llo"}]
    ) == "hello"


# --- responses input normalisation ------------------------------------


def test_a_string_input_becomes_one_user_turn():
    assert responses_input_to_messages("hi") == [{"role": "user", "content": "hi"}]


def test_a_list_of_items_keeps_roles():
    assert responses_input_to_messages(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ) == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]


def test_an_item_without_a_role_is_a_user_turn():
    assert responses_input_to_messages([{"content": "a"}]) == [{"role": "user", "content": "a"}]


def test_nested_content_parts_are_flattened():
    assert responses_input_to_messages(
        [{"role": "user", "content": [{"type": "input_text", "text": "x"},
                                      {"type": "input_text", "text": "y"}]}]
    ) == [{"role": "user", "content": "xy"}]


def test_empty_input_is_empty():
    assert responses_input_to_messages(None) == []
