"""Gateway config parsing and the committed example."""

import pytest
import yaml

from gateway.config import ConfigError, GatewayConfig, load_config, parse_config

MINIMAL = {
    "routes": [{"prefix": "gpt-", "provider": "openai"}],
    "keys": [{"key": "s", "id": "demo"}],
}


def test_routing_by_prefix_first_match_wins():
    config = parse_config(
        {
            "routes": [
                {"prefix": "claude-3-opus", "provider": "fake"},
                {"prefix": "claude-", "provider": "anthropic"},
            ],
            "keys": [],
        }
    )
    assert config.provider_for("claude-3-opus-latest") == "fake"
    assert config.provider_for("claude-3-5-sonnet") == "anthropic"
    assert config.provider_for("gpt-4o") is None


def test_defaults_when_absent():
    config = parse_config(MINIMAL)
    assert config.sessions.backend == "sqlite"
    assert config.sessions.ttl_seconds == 3600
    assert config.default_max_tokens == 1024


def test_a_key_without_a_budget_is_unlimited():
    config = parse_config(MINIMAL)
    assert config.key_for("s").monthly_budget_usd is None
    assert config.key_for("missing") is None


def test_repr_of_a_key_does_not_include_the_secret():
    config = parse_config({"routes": [], "keys": [{"key": "top-secret", "id": "demo"}]})
    assert "top-secret" not in repr(config.key_for("top-secret"))


@pytest.mark.parametrize(
    "document, message",
    [
        ({"routes": "nope", "keys": []}, "routes must be a list"),
        ({"routes": [{"provider": "openai"}], "keys": []}, "prefix must be"),
        ({"routes": [{"prefix": "x", "provider": "bedrock"}], "keys": []}, "provider must be"),
        ({"routes": [], "keys": "nope"}, "keys must be a list"),
        ({"routes": [], "keys": [{"id": "a"}]}, "key must be"),
        ({"routes": [], "keys": [{"key": "s"}]}, "id must be"),
        ({"routes": [], "keys": [{"key": "s", "id": "a", "monthly_budget_usd": "free"}]},
         "must be a number"),
        ({"routes": [], "keys": [{"key": "s", "id": "a", "monthly_budget_usd": -1}]},
         "must not be negative"),
        ({"routes": [], "keys": [{"key": "s", "id": "a"}, {"key": "s", "id": "b"}]},
         "duplicate"),
        ({"routes": [], "keys": [], "sessions": {"backend": "memcached"}},
         "sessions.backend must be"),
        ({"routes": [], "keys": [], "sessions": {"ttl_seconds": 0}}, "must be positive"),
        ({"routes": [], "keys": [], "defaults": {"max_tokens": 0}}, "must be positive"),
    ],
)
def test_malformed_config_is_rejected(document, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(document)


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="no gateway config found"):
        load_config("/nonexistent/gateway.yaml")


def test_invalid_yaml_raises(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("routes: {unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_the_committed_example_parses_and_routes():
    config = load_config("gateway.example.yaml")
    assert config.provider_for("gpt-4o-mini") == "openai"
    assert config.provider_for("claude-3-5-sonnet") == "anthropic"
    assert config.provider_for("fake-1") == "fake"
    assert config.provider_for("llama-3") is None
    assert config.sessions.backend == "sqlite"
    # The example ships a placeholder, so it is obvious it must be replaced.
    assert any("replace" in secret for secret in config.keys)


def test_the_example_round_trips_through_yaml(tmp_path):
    text = yaml.safe_dump(
        {
            "routes": [{"prefix": "gpt-", "provider": "openai"}],
            "keys": [{"key": "s", "id": "demo", "monthly_budget_usd": 2.5}],
            "sessions": {"backend": "redis", "ttl_seconds": 60},
            "defaults": {"max_tokens": 32},
        }
    )
    path = tmp_path / "gateway.yaml"
    path.write_text(text)
    config = load_config(path)

    assert config.sessions.backend == "redis"
    assert config.sessions.ttl_seconds == 60
    assert config.default_max_tokens == 32
    assert config.key_for("s").monthly_budget_usd == 2.5


def test_an_empty_config_is_usable_but_routes_nothing():
    config = parse_config({})
    assert isinstance(config, GatewayConfig)
    assert config.provider_for("gpt-4o") is None
    assert config.prefixes == ()
