"""Request bodies for the two endpoints, and the response builders.

Unsupported fields are declared rather than silently dropped, so a request
using tools or images is refused with a clear reason instead of being
answered as though the field had been honoured.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

# Fields the gateway understands but does not implement. Present in a request
# means a 400, not a quiet omission.
UNSUPPORTED_CHAT_FIELDS = ("tools", "functions", "tool_choice", "function_call", "n",
                           "logprobs", "top_logprobs", "response_format", "audio",
                           "modalities", "prediction")
UNSUPPORTED_RESPONSES_FIELDS = ("tools", "tool_choice", "text", "reasoning",
                                "include", "truncation", "store", "parallel_tool_calls")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False

    model_config = {"extra": "allow"}


class ResponsesRequest(BaseModel):
    model: str
    input: Any = None
    instructions: str | None = None
    previous_response_id: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False

    model_config = {"extra": "allow"}


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def chat_completion_body(
    completion_id: str, model: str, text: str, finish_reason: str,
    input_tokens: int, output_tokens: int,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def chat_chunk_body(
    completion_id: str, model: str, delta: str | None = None,
    finish_reason: str | None = None, include_role: bool = False,
) -> dict:
    """One chat.completion.chunk.

    `include_role` belongs to the first chunk only: OpenAI sends the role once
    at the start of a stream, not on every delta.
    """
    choice: dict[str, Any] = {"index": 0, "delta": {}, "finish_reason": finish_reason}
    if include_role:
        choice["delta"]["role"] = "assistant"
    if delta is not None:
        choice["delta"]["content"] = delta
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }


def message_item(item_id: str, text: str, status: str) -> dict:
    """The assistant message item that carries the output text.

    An empty `text` yields an empty content list, which is what the item looks
    like before any delta has arrived.
    """
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": status,
        "content": (
            [{"type": "output_text", "text": text, "annotations": []}] if text else []
        ),
    }


def output_text_part(text: str) -> dict:
    """The content part referenced by content_part.added and .done."""
    return {"type": "output_text", "text": text, "annotations": []}


def response_body(
    response_id: str, model: str, text: str, input_tokens: int, output_tokens: int,
    status: str = "completed",
    item_id: str | None = None,
    with_item: bool = True,
) -> dict:
    """The response object both the endpoint and the stream events carry.

    `item_id` pins the message item's id. The streaming path passes one so the
    same id appears in every event; omitting it mints a fresh one, which is
    what the non-streaming path does.

    `with_item=False` yields an empty `output`, which is what
    `response.created` looks like: the item first appears in
    `response.output_item.added`.
    """
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": (
            [message_item(item_id or new_id("msg_"), text, status)] if with_item else []
        ),
        "output_text": text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


# Fields the SDK's Response model requires that the plain body above does not
# carry. They are added only to the object embedded in stream events, so the
# non-streaming response body is left exactly as it was.
def stream_response_envelope(
    response_id: str, model: str, text: str, input_tokens: int, output_tokens: int,
    status: str, item_id: str, with_item: bool = True,
) -> dict:
    body = response_body(
        response_id, model, text, input_tokens, output_tokens, status, item_id,
        with_item=with_item,
    )
    # This gateway does not support tools, so these are honest constants.
    body["parallel_tool_calls"] = False
    body["tool_choice"] = "none"
    body["tools"] = []
    body["usage"]["input_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 0}
    body["usage"]["output_tokens_details"] = {"reasoning_tokens": 0}
    return body
