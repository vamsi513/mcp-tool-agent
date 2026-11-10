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
    finish_reason: str | None = None,
) -> dict:
    choice: dict[str, Any] = {"index": 0, "delta": {}, "finish_reason": finish_reason}
    if delta is not None:
        choice["delta"] = {"role": "assistant", "content": delta}
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }


def response_body(
    response_id: str, model: str, text: str, input_tokens: int, output_tokens: int,
    status: str = "completed",
) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": [
            {
                "type": "message",
                "id": new_id("msg_"),
                "role": "assistant",
                "status": status,
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "output_text": text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
