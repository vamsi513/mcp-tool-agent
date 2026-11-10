"""Error responses, in the shape OpenAI clients expect.

Every failure the gateway raises itself carries a `type` and a `code`, so a
caller can branch on the code rather than parse prose.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def error_body(message: str, error_type: str, code: str, param: str | None = None) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


class GatewayError(HTTPException):
    """An HTTPException whose detail is already an OpenAI-shaped error body."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str,
        code: str,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail=error_body(message, error_type, code, param),
            headers=headers,
        )


def unknown_model(model: str, known: Any = ()) -> GatewayError:
    routes = ", ".join(sorted(known)) if known else "none configured"
    return GatewayError(
        400,
        f"no route configured for model {model!r}; configured prefixes: {routes}",
        "invalid_request_error",
        "model_not_found",
        param="model",
    )


def unknown_session(response_id: str) -> GatewayError:
    return GatewayError(
        404,
        f"response {response_id!r} is unknown or has expired",
        "invalid_request_error",
        "response_not_found",
        param="previous_response_id",
    )


def budget_exceeded(key_id: str, spent: float, limit: float, currency: str) -> GatewayError:
    return GatewayError(
        429,
        f"monthly budget for key {key_id!r} is exhausted: estimated "
        f"{spent:.6f} {currency} spent against a limit of {limit:.6f} "
        f"{currency}. Costs are estimates from the configured price table.",
        "insufficient_quota",
        "budget_exceeded",
    )


def missing_key() -> GatewayError:
    return GatewayError(
        401,
        "no API key supplied; send it as 'Authorization: Bearer <key>'",
        "invalid_request_error",
        "missing_api_key",
    )


def unknown_key() -> GatewayError:
    return GatewayError(
        401, "the supplied API key is not configured", "invalid_request_error",
        "invalid_api_key",
    )


def bad_request(message: str, param: str | None = None) -> GatewayError:
    return GatewayError(400, message, "invalid_request_error", "invalid_value", param=param)


def upstream_failure(provider: str, message: str) -> GatewayError:
    return GatewayError(
        502,
        f"the {provider} upstream failed: {message}",
        "api_error",
        "upstream_error",
    )


def unsupported(feature: str) -> GatewayError:
    return GatewayError(
        400,
        f"{feature} is not supported by this gateway; see the README for what is",
        "invalid_request_error",
        "unsupported_feature",
    )
