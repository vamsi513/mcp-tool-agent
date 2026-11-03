"""A small local OpenAI-compatible server, for driving real mode with no key.

It answers POST /v1/chat/completions. Given a map of question text to the tool
call that should answer it, it behaves like a cooperative model: the first
request for a conversation returns that tool call, and once a tool result is
in the messages it returns a final answer quoting it.

This is deliberately a real HTTP server rather than a patched client, so the
openai SDK's own transport is exercised. That is what catches a replay layer
leaking into the model's HTTP calls.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

COMPLETIONS_PATH = "/v1/chat/completions"


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/1.0"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802  (http.server naming)
        server: FakeOpenAI = self.server  # type: ignore[assignment]
        if self.path.split("?")[0] != COMPLETIONS_PATH:
            return self._send(404, {"error": {"message": "not found"}})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": {"message": "body is not JSON"}})

        messages = request.get("messages") or []
        server.record(request)

        question = ""
        for message in messages:
            if message.get("role") == "user":
                question = str(message.get("content") or "")
                break

        last_tool_result = ""
        for message in reversed(messages):
            if message.get("role") == "tool":
                last_tool_result = str(message.get("content") or "")
                break

        plan = server.plan.get(question)
        tools_offered = bool(request.get("tools"))

        if plan and tools_offered and not last_tool_result:
            tool, arguments = plan
            return self._send(200, _tool_call_response(tool, arguments))
        return self._send(200, _answer_response(last_tool_result or "(no tool result)"))


def _envelope(message: dict, finish_reason: str) -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _tool_call_response(tool: str, arguments: dict) -> dict:
    return _envelope(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_fake_1",
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "tool_calls",
    )


def _answer_response(content: str) -> dict:
    return _envelope({"role": "assistant", "content": content}, "stop")


class FakeOpenAI(ThreadingHTTPServer):
    """Serves chat completions from a question-to-tool-call plan."""

    daemon_threads = True

    def __init__(self, plan: dict[str, tuple[str, dict]]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.plan = plan
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def record(self, request: dict) -> None:
        with self._lock:
            self.requests.append(request)

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host!s}:{port}/v1"

    def start(self) -> FakeOpenAI:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> FakeOpenAI:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
