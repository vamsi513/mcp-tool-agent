"""The provider interface the gateway talks to.

One shape in, one shape out, so the endpoints never branch on which provider
is in use. Implementations are synchronous: the endpoints are plain `def`, so
the framework already runs them off the event loop, and the repository's other
HTTP code is synchronous httpx.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    ) -> Iterator[str]:
        """Yield text deltas. Only text is streamed; see the README."""
        ...
