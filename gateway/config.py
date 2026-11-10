"""Gateway configuration: routing, keys and budgets, sessions.

Resolution order for the file: an explicit path, then GATEWAY_CONFIG, then
gateway.yaml beside the repository root, then the committed
gateway.example.yaml. The same order the policy and price tables use.

A config file holds API key secrets, so gateway.yaml is gitignored and only
the example ships. Nothing in this module ever logs a key: callers are handed
a `KeyConfig` whose `id` is the only thing meant for output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "gateway.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "gateway.example.yaml"

PROVIDERS = ("openai", "anthropic", "fake")
SESSION_BACKENDS = ("sqlite", "redis")

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_TOKENS = 1024


class ConfigError(Exception):
    """Raised when the gateway config is missing or malformed."""


@dataclass(frozen=True)
class Route:
    prefix: str
    provider: str


@dataclass(frozen=True)
class KeyConfig:
    """One accepted API key. `secret` is never logged or returned."""

    id: str
    secret: str
    monthly_budget_usd: float | None = None

    def __repr__(self) -> str:  # keeps a stray repr from leaking the secret
        return f"KeyConfig(id={self.id!r}, monthly_budget_usd={self.monthly_budget_usd!r})"


@dataclass(frozen=True)
class SessionConfig:
    backend: str = "sqlite"
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    redis_url: str = "redis://127.0.0.1:6379/0"


@dataclass(frozen=True)
class GatewayConfig:
    routes: tuple[Route, ...] = ()
    keys: dict[str, KeyConfig] = field(default_factory=dict)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    default_max_tokens: int = DEFAULT_MAX_TOKENS

    def provider_for(self, model: str) -> str | None:
        """The provider whose configured prefix matches `model`, first wins."""
        for route in self.routes:
            if model.startswith(route.prefix):
                return route.provider
        return None

    @property
    def prefixes(self) -> tuple[str, ...]:
        return tuple(route.prefix for route in self.routes)

    def key_for(self, secret: str) -> KeyConfig | None:
        return self.keys.get(secret)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping, got {type(value).__name__}")
    return value


def parse_config(raw: Any) -> GatewayConfig:
    document = _mapping(raw, "gateway config")

    routes: list[Route] = []
    raw_routes = document.get("routes") or []
    if not isinstance(raw_routes, list):
        raise ConfigError("routes must be a list")
    for index, entry in enumerate(raw_routes):
        entry = _mapping(entry, f"routes[{index}]")
        prefix = entry.get("prefix")
        provider = entry.get("provider")
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError(f"routes[{index}].prefix must be a non-empty string")
        if provider not in PROVIDERS:
            raise ConfigError(
                f"routes[{index}].provider must be one of {', '.join(PROVIDERS)}, "
                f"got {provider!r}"
            )
        routes.append(Route(prefix=prefix, provider=provider))

    keys: dict[str, KeyConfig] = {}
    raw_keys = document.get("keys") or []
    if not isinstance(raw_keys, list):
        raise ConfigError("keys must be a list")
    for index, entry in enumerate(raw_keys):
        entry = _mapping(entry, f"keys[{index}]")
        secret = entry.get("key")
        identifier = entry.get("id")
        if not isinstance(secret, str) or not secret:
            raise ConfigError(f"keys[{index}].key must be a non-empty string")
        if not isinstance(identifier, str) or not identifier:
            raise ConfigError(f"keys[{index}].id must be a non-empty string")
        budget = entry.get("monthly_budget_usd")
        if budget is not None:
            try:
                budget = float(budget)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"keys[{index}].monthly_budget_usd must be a number"
                ) from exc
            if budget < 0:
                raise ConfigError(f"keys[{index}].monthly_budget_usd must not be negative")
        if secret in keys:
            raise ConfigError(f"keys[{index}].key is a duplicate")
        keys[secret] = KeyConfig(id=identifier, secret=secret, monthly_budget_usd=budget)

    sessions_raw = _mapping(document.get("sessions"), "sessions")
    backend = sessions_raw.get("backend", "sqlite")
    if backend not in SESSION_BACKENDS:
        raise ConfigError(
            f"sessions.backend must be one of {', '.join(SESSION_BACKENDS)}, got {backend!r}"
        )
    try:
        ttl = int(sessions_raw.get("ttl_seconds", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ConfigError("sessions.ttl_seconds must be an integer") from exc
    if ttl <= 0:
        raise ConfigError("sessions.ttl_seconds must be positive")

    defaults = _mapping(document.get("defaults"), "defaults")
    try:
        max_tokens = int(defaults.get("max_tokens", DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError) as exc:
        raise ConfigError("defaults.max_tokens must be an integer") from exc
    if max_tokens <= 0:
        raise ConfigError("defaults.max_tokens must be positive")

    return GatewayConfig(
        routes=tuple(routes),
        keys=keys,
        sessions=SessionConfig(
            backend=backend,
            ttl_seconds=ttl,
            redis_url=str(sessions_raw.get("redis_url", "redis://127.0.0.1:6379/0")),
        ),
        default_max_tokens=max_tokens,
    )


def load_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        from_env = os.getenv("GATEWAY_CONFIG")
        if from_env:
            candidates.append(Path(from_env))
        candidates.append(DEFAULT_CONFIG_PATH)
        candidates.append(EXAMPLE_CONFIG_PATH)

    for candidate in candidates:
        if candidate.is_file():
            try:
                raw = yaml.safe_load(candidate.read_text())
            except yaml.YAMLError as exc:
                raise ConfigError(f"{candidate} is not valid YAML: {exc}") from exc
            return parse_config(raw)

    tried = ", ".join(str(c) for c in candidates)
    raise ConfigError(f"no gateway config found (tried: {tried})")
