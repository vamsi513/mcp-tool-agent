"""The provider interface the gateway talks to.

One shape in, one shape out, so the endpoints never branch on which provider
is in use. Implementations are synchronous: the endpoints are plain `def`, so
the framework already runs them off the event loop, and the repository's other
HTTP code is synchronous httpx.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    """What every provider returns, whatever its own wire format was."""

    text: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Provider(Protocol):
    """A chat provider.

    `messages` is always OpenAI-shaped: a list of {role, content} with content
    already flattened to text. `extra_system` carries `/v1/responses`
    instructions, which some providers place differently from a system turn.
    """

    name: str

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Completion: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Generator[str, None, Completion]:
        """Yield text deltas, then return the final usage.

        A generator's return value, so the caller gets the upstream's own
        token counts once the stream ends rather than guessing them from the
        text. Budgets are charged from that. An upstream that reports no
        usage yields a Completion with zero counts, which is visible as a
        zero usage row rather than silently invented numbers.

        Only text is streamed; see the README for what is not supported.
        """
        ...


class StreamCollector:
    """Iterates a provider stream, keeping the text and the final usage.

    A provider's stream yields deltas and returns a Completion, so the
    iteration is explicit: each delta is accumulated and passed on, and the
    return value is captured when the generator stops.
    """

    def __init__(self, stream: Generator[str, None, Completion]) -> None:
        self._stream = stream
        self.text = ""
        self.usage: Completion | None = None

    def __iter__(self) -> Iterator[str]:
        while True:
            try:
                delta = next(self._stream)
            except StopIteration as stop:
                self.usage = stop.value if isinstance(stop.value, Completion) else None
                return
            self.text += delta
            yield delta
