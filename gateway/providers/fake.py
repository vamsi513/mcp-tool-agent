"""A provider that answers from memory, for tests and the benchmark.

Deterministic on purpose: the reply, the finish reason and both token counts
are predictable, so a test can assert exact numbers. It reaches no network, so
every gateway test runs offline.
"""

from __future__ import annotations

import time
from collections.abc import Generator

from gateway.providers.base import Completion
from gateway.translate import content_to_text


def count_tokens(text: str) -> int:
    """A stand-in token count: whitespace-separated words.

    Not a real tokenizer and not claimed to be one. It only has to be stable
    so tests can assert on it.
    """
    return len(text.split())


class FakeProvider:
    """Echoes a canned reply, with optional scripted replies and a delay."""

    name = "fake"

    def __init__(
        self,
        reply: str = "fake reply",
        finish_reason: str = "stop",
        delay_seconds: float = 0.0,
        scripted: list[str] | None = None,
    ) -> None:
        self.reply = reply
        self.finish_reason = finish_reason
        self.delay_seconds = delay_seconds
        self._scripted = list(scripted or [])
        self.calls: list[dict] = []

    def _next_reply(self) -> str:
        if self._scripted:
            return self._scripted.pop(0)
        return self.reply

    def _record(self, model: str, messages: list[dict], extra_system: str) -> None:
        self.calls.append(
            {"model": model, "messages": messages, "extra_system": extra_system}
        )

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Completion:
        self._record(model, messages, extra_system)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

        prompt = " ".join(
            [extra_system] + [content_to_text(m.get("content")) for m in messages]
        )
        reply = self._next_reply()
        return Completion(
            text=reply,
            finish_reason=self.finish_reason,
            input_tokens=count_tokens(prompt),
            output_tokens=count_tokens(reply),
            model=model,
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Generator[str, None, Completion]:
        self._record(model, messages, extra_system)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        reply = self._next_reply()
        # One delta per word, so a test sees several events rather than one.
        words = reply.split()
        for index, word in enumerate(words):
            yield word if index == 0 else " " + word

        prompt = " ".join(
            [extra_system] + [content_to_text(m.get("content")) for m in messages]
        )
        return Completion(
            text=reply,
            finish_reason=self.finish_reason,
            input_tokens=count_tokens(prompt),
            output_tokens=count_tokens(reply),
            model=model,
        )
