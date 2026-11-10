"""The gateway app: two endpoints, two providers, budgets and sessions.

Built by `create_app`, so tests inject a config, a provider map, a price table
and a database without touching module state. Importing this module starts
nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import metering
from gateway import budgets, errors
from gateway.config import GatewayConfig, KeyConfig, load_config
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import Completion, Provider, StreamCollector
from gateway.providers.fake import FakeProvider
from gateway.providers.openai import OpenAIProvider
from gateway.schemas import (
    UNSUPPORTED_CHAT_FIELDS,
    UNSUPPORTED_RESPONSES_FIELDS,
    ChatCompletionRequest,
    ResponsesRequest,
    chat_chunk_body,
    chat_completion_body,
    new_id,
    response_body,
)
from gateway.sessions import Session, SessionStore, build_store
from gateway.translate import content_to_text, responses_input_to_messages

logger = logging.getLogger("gateway")

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# Constants, not configuration. The root route must not leak anything about
# how this instance is set up, so it reads neither the config nor the
# provider map.
SERVICE_NAME = "mcp-tool-agent llm gateway"
SUPPORTED_PATHS = ("/v1/chat/completions", "/v1/responses", "/healthz")


def _sse(data: dict, event: str | None = None) -> str:
    """One SSE frame. `event` is set for the responses endpoint's named events."""
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n"


def default_providers(config: GatewayConfig) -> dict[str, Provider]:
    return {
        "openai": OpenAIProvider(),
        "anthropic": AnthropicProvider(default_max_tokens=config.default_max_tokens),
        "fake": FakeProvider(),
    }


def create_app(
    config: GatewayConfig | None = None,
    providers: dict[str, Provider] | None = None,
    prices: metering.PriceTable | None = None,
    db: Any = None,
    store: SessionStore | None = None,
) -> FastAPI:
    """Build the app. Every collaborator is injectable for tests."""
    settings = config or load_config()
    provider_map = providers if providers is not None else default_providers(settings)
    price_table = prices if prices is not None else metering.load_prices()
    sessions = store or build_store(
        settings.sessions.backend, settings.sessions.redis_url, db
    )

    app = FastAPI(
        title="mcp-tool-agent llm gateway",
        description=(
            "Translates OpenAI chat-completions and responses requests to "
            "OpenAI or Anthropic. Text only; see the README."
        ),
        version="0.1.0",
    )
    app.state.config = settings
    app.state.providers = provider_map
    app.state.prices = price_table
    app.state.sessions = sessions
    app.state.db = db

    def authorise(authorization: str | None) -> KeyConfig:
        if not authorization:
            raise errors.missing_key()
        token = authorization.removeprefix("Bearer ").removeprefix("bearer ").strip()
        if not token:
            raise errors.missing_key()
        key = settings.key_for(token)
        if key is None:
            raise errors.unknown_key()
        return key

    def route(model: str) -> tuple[str, Provider]:
        name = settings.provider_for(model)
        if name is None:
            raise errors.unknown_model(model, settings.prefixes)
        provider = provider_map.get(name)
        if provider is None:
            raise errors.unknown_model(model, settings.prefixes)
        return name, provider

    def reject_unsupported(raw: dict, fields: tuple[str, ...]) -> None:
        for field in fields:
            if raw.get(field) not in (None, [], {}):
                raise errors.unsupported(field)

    def enforce_budget(key: KeyConfig, model: str, endpoint: str) -> None:
        state = budgets.check(key, price_table, db)
        if state.exceeded:
            budgets.log_refusal(state, model, endpoint)
            raise errors.budget_exceeded(
                state.key_id, state.spent, state.limit or 0.0, state.currency
            )

    def charge(key: KeyConfig, model: str, usage: Completion | None) -> None:
        if usage is None:
            logger.warning(
                "no usage reported by the upstream; recording a zero row",
                extra={"key_id": key.id, "model": model},
            )
            budgets.record(key, model, 0, 0, db)
            return
        budgets.record(key, model, usage.input_tokens, usage.output_tokens, db)

    # ---------------------------------------------------------------- health

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness only.

        Unauthenticated, so it says nothing about how this instance is set
        up: no provider names, session backend, routes or model prefixes.
        It reads neither the config nor the provider map.
        """
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict:
        """What this service is and which paths it answers on.

        Unauthenticated, and deliberately static: no key, config value, model
        name or provider detail appears here. A browser hitting the root
        should not get a 404, and should not learn anything either.
        """
        return {"service": SERVICE_NAME, "paths": list(SUPPORTED_PATHS)}

    @app.get("/favicon.ico", status_code=204, response_class=Response)
    def favicon() -> Response:
        """Browsers ask for this unprompted; answer empty rather than 404."""
        return Response(status_code=204)

    # ------------------------------------------------------- chat completions

    @app.post("/v1/chat/completions")
    def chat_completions(
        body: ChatCompletionRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        key = authorise(authorization)
        reject_unsupported(
            body.model_extra or {}, UNSUPPORTED_CHAT_FIELDS
        )
        if not body.messages:
            raise errors.bad_request("messages must not be empty", param="messages")
        _, provider = route(body.model)
        enforce_budget(key, body.model, "/v1/chat/completions")

        messages = [
            {"role": m.get("role", "user"), "content": content_to_text(m.get("content"))}
            for m in body.messages
        ]
        completion_id = new_id("chatcmpl-")

        if not body.stream:
            result = provider.complete(
                model=body.model,
                messages=messages,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
            )
            charge(key, body.model, result)
            return chat_completion_body(
                completion_id,
                result.model or body.model,
                result.text,
                result.finish_reason,
                result.input_tokens,
                result.output_tokens,
            )

        collector = StreamCollector(
            provider.stream(
                model=body.model,
                messages=messages,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
            )
        )

        def frames() -> Iterator[str]:
            yield _sse(chat_chunk_body(completion_id, body.model, delta=""))
            for delta in collector:
                yield _sse(chat_chunk_body(completion_id, body.model, delta=delta))
            usage = collector.usage
            yield _sse(
                chat_chunk_body(
                    completion_id,
                    body.model,
                    finish_reason=usage.finish_reason if usage else "stop",
                )
            )
            yield "data: [DONE]\n\n"
            charge(key, body.model, usage)

        return StreamingResponse(frames(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    # ------------------------------------------------------------- responses

    @app.post("/v1/responses")
    def responses(
        body: ResponsesRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        key = authorise(authorization)
        reject_unsupported(body.model_extra or {}, UNSUPPORTED_RESPONSES_FIELDS)
        provider_name, provider = route(body.model)
        enforce_budget(key, body.model, "/v1/responses")

        history: list[dict] = []
        instructions = body.instructions or ""
        if body.previous_response_id:
            previous = sessions.get(body.previous_response_id)
            if previous is None:
                raise errors.unknown_session(body.previous_response_id)
            history = list(previous.messages)
            # The earlier instructions stand unless this request replaces them.
            instructions = instructions or previous.instructions

        turns = responses_input_to_messages(body.input)
        if not history and not turns:
            raise errors.bad_request("input must not be empty", param="input")
        messages = history + turns
        response_id = new_id("resp_")

        def remember(text: str) -> None:
            sessions.put(
                Session(
                    response_id=response_id,
                    key_id=key.id,
                    model=body.model,
                    provider=provider_name,
                    messages=messages + [{"role": "assistant", "content": text}],
                    instructions=instructions,
                ),
                settings.sessions.ttl_seconds,
            )

        if not body.stream:
            result = provider.complete(
                model=body.model,
                messages=messages,
                temperature=body.temperature,
                max_tokens=body.max_output_tokens,
                extra_system=instructions,
            )
            charge(key, body.model, result)
            remember(result.text)
            return response_body(
                response_id,
                result.model or body.model,
                result.text,
                result.input_tokens,
                result.output_tokens,
            )

        collector = StreamCollector(
            provider.stream(
                model=body.model,
                messages=messages,
                temperature=body.temperature,
                max_tokens=body.max_output_tokens,
                extra_system=instructions,
            )
        )
        item_id = new_id("msg_")

        def frames() -> Iterator[str]:
            created = response_body(response_id, body.model, "", 0, 0, status="in_progress")
            yield _sse(
                {"type": "response.created", "response": created}, event="response.created"
            )
            for delta in collector:
                yield _sse(
                    {
                        "type": "response.output_text.delta",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": delta,
                    },
                    event="response.output_text.delta",
                )
            usage = collector.usage
            final = response_body(
                response_id,
                body.model,
                collector.text,
                usage.input_tokens if usage else 0,
                usage.output_tokens if usage else 0,
            )
            yield _sse(
                {
                    "type": "response.output_text.done",
                    "response_id": response_id,
                    "item_id": item_id,
                    "text": collector.text,
                },
                event="response.output_text.done",
            )
            yield _sse(
                {"type": "response.completed", "response": final}, event="response.completed"
            )
            charge(key, body.model, usage)
            remember(collector.text)

        return StreamingResponse(frames(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    @app.exception_handler(errors.GatewayError)
    def gateway_error_handler(request: Request, exc: errors.GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=exc.detail, headers=exc.headers
        )

    return app
