"""The gateway's command line tool, against a temporary config and database.

Nothing here touches the repository's own config or database, and no test
prints or asserts a full existing key: the only secret any of these produce
is one `keys create` generates into a temp file, and even that is checked
through its masked form afterwards.
"""

from __future__ import annotations

import json

import pytest
import yaml

import metering
from gateway import cli
from gateway.config import EXAMPLE_CONFIG_PATH, load_config

EXISTING_SECRET = "existing-secret-value-wxyz"


@pytest.fixture
def config_path(tmp_path):
    """A config with one key already in it, plus a comment to watch."""
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "# a comment that a rewrite does not keep\n"
        "version: 1\n"
        "routes:\n"
        "  - prefix: 'gpt-'\n"
        "    provider: openai\n"
        "  - prefix: 'llama3'\n"
        "    provider: ollama\n"
        "keys:\n"
        f"  - key: '{EXISTING_SECRET}'\n"
        "    id: 'demo'\n"
        "    monthly_budget_usd: 5.0\n"
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def db(tmp_path):
    return tmp_path / "gov.db"


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def payload(out: str) -> dict:
    return json.loads(out)


# ------------------------------------------------------------- keys create


def test_keys_create_writes_the_key_and_prints_it_once(capsys, config_path):
    code, out, _ = run(
        capsys, "--config", str(config_path), "keys", "create",
        "--id", "ci", "--name", "build runner", "--budget", "10",
    )
    assert code == 0

    settings = load_config(config_path)
    created = next(key for key in settings.keys.values() if key.id == "ci")
    assert created.name == "build runner"
    assert created.monthly_budget_usd == 10.0
    assert created.created_at is not None
    assert created.secret.startswith(cli.KEY_PREFIX)
    assert len(created.secret) > 20

    # Printed exactly once, and flagged as the only time.
    assert out.count(created.secret) == 1
    assert "only time the key is shown" in out
    # The key that already existed is not printed at all.
    assert EXISTING_SECRET not in out


def test_keys_create_keeps_the_existing_key_working(capsys, config_path):
    run(capsys, "--config", str(config_path), "keys", "create", "--id", "ci")
    settings = load_config(config_path)
    assert settings.key_for(EXISTING_SECRET) is not None
    assert settings.key_for(EXISTING_SECRET).id == "demo"
    assert settings.key_for(EXISTING_SECRET).monthly_budget_usd == 5.0


def test_keys_create_keeps_the_routes_and_the_file_mode(capsys, config_path):
    before = config_path.stat().st_mode & 0o777
    run(capsys, "--config", str(config_path), "keys", "create", "--id", "ci")

    settings = load_config(config_path)
    assert settings.provider_for("gpt-4o-mini") == "openai"
    assert settings.provider_for("llama3.2") == "ollama"
    assert config_path.stat().st_mode & 0o777 == before


def test_keys_create_rejects_a_duplicate_id(capsys, config_path):
    code, _, err = run(
        capsys, "--config", str(config_path), "keys", "create", "--id", "demo"
    )
    assert code == 2
    assert "already exists" in err
    # Nothing was written.
    assert len(load_config(config_path).keys) == 1


def test_keys_create_refuses_the_committed_example(capsys):
    code, _, err = run(
        capsys, "--config", str(EXAMPLE_CONFIG_PATH), "keys", "create", "--id", "x"
    )
    assert code == 2
    assert "committed example" in err


def test_keys_create_refuses_a_missing_file(capsys, tmp_path):
    code, _, err = run(
        capsys, "--config", str(tmp_path / "absent.yaml"), "keys", "create", "--id", "x"
    )
    assert code == 2
    assert "no config file" in err


def test_two_created_keys_differ(capsys, config_path):
    run(capsys, "--config", str(config_path), "keys", "create", "--id", "one")
    run(capsys, "--config", str(config_path), "keys", "create", "--id", "two")
    secrets_in_file = set(load_config(config_path).keys)
    assert len(secrets_in_file) == 3


# --------------------------------------------------------------- keys list


def test_keys_list_masks_every_key(capsys, config_path):
    code, out, _ = run(capsys, "--config", str(config_path), "keys", "list")
    assert code == 0
    assert EXISTING_SECRET not in out
    assert "****wxyz" in out
    assert "demo" in out


def test_keys_list_masks_in_json_too(capsys, config_path):
    code, out, _ = run(capsys, "--json", "--config", str(config_path), "keys", "list")
    assert code == 0
    assert EXISTING_SECRET not in out
    body = payload(out)
    assert body["keys"][0]["key"] == "********wxyz"
    assert body["keys"][0]["monthly_budget_usd"] == 5.0
    assert body["keys"][0]["name"] is None


def test_a_short_key_is_not_partly_revealed(capsys, tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("routes: []\nkeys:\n  - key: 'tiny'\n    id: 'small'\n")
    code, out, _ = run(capsys, "--config", str(path), "keys", "list")
    assert code == 0
    assert "tiny" not in out
    assert "********" in out


def test_keys_list_shows_a_dash_for_what_is_not_set(capsys, config_path):
    code, out, _ = run(capsys, "--config", str(config_path), "keys", "list")
    line = next(line for line in out.splitlines() if line.startswith("demo"))
    assert "-" in line  # no name and no created_at on a hand-written key


# -------------------------------------------------------------- budget set


def test_budget_set_changes_a_budget(capsys, config_path):
    code, out, _ = run(
        capsys, "--config", str(config_path), "budget", "set", "--id", "demo",
        "--budget", "42.5",
    )
    assert code == 0
    assert "5.0 -> 42.5" in out
    assert load_config(config_path).key_for(EXISTING_SECRET).monthly_budget_usd == 42.5


def test_budget_set_can_remove_a_budget(capsys, config_path):
    code, out, _ = run(
        capsys, "--config", str(config_path), "budget", "set", "--id", "demo",
        "--unlimited",
    )
    assert code == 0
    assert load_config(config_path).key_for(EXISTING_SECRET).monthly_budget_usd is None
    document = yaml.safe_load(config_path.read_text())
    assert "monthly_budget_usd" not in document["keys"][0]


def test_budget_set_on_an_unknown_id_fails(capsys, config_path):
    code, _, err = run(
        capsys, "--config", str(config_path), "budget", "set", "--id", "nope",
        "--budget", "1",
    )
    assert code == 2
    assert "no key with id 'nope'" in err
    assert "demo" in err  # tells the user what ids there are


def test_budget_set_needs_an_amount_or_unlimited(capsys, config_path):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config_path), "budget", "set", "--id", "demo"])


def test_a_negative_budget_is_rejected(capsys, config_path):
    with pytest.raises(SystemExit):
        cli.main(
            ["--config", str(config_path), "budget", "set", "--id", "demo",
             "--budget", "-1"]
        )


def test_a_non_numeric_budget_is_rejected(capsys, config_path):
    with pytest.raises(SystemExit):
        cli.main(
            ["--config", str(config_path), "budget", "set", "--id", "demo",
             "--budget", "soon"]
        )


# -------------------------------------------------------------- usage show


def seed(db) -> None:
    metering.record_llm_turn("demo", "gpt-4o-mini", 30, 12, db=db)
    metering.record_llm_turn("demo", "gpt-4o-mini", 10, 5, db=db)
    metering.record_llm_turn("demo", "llama3.2", 100, 20, db=db)
    metering.record_llm_turn("retired", "gpt-4o-mini", 7, 3, db=db)


def test_usage_show_adds_up_every_model_for_a_key(capsys, config_path, db):
    seed(db)
    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "usage", "show", "--prices", "prices.example.yaml",
    )
    assert code == 0
    rows = {row["id"]: row for row in payload(out)["rows"]}
    # Three turns across two models, added together rather than one row won.
    assert rows["demo"]["turns"] == 3
    assert rows["demo"]["input_tokens"] == 140
    assert rows["demo"]["output_tokens"] == 37
    assert rows["demo"]["monthly_budget_usd"] == 5.0


def test_usage_show_flags_a_cost_it_could_only_partly_price(capsys, config_path, db):
    seed(db)
    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "usage", "show", "--prices", "prices.example.yaml",
    )
    body = payload(out)
    rows = {row["id"]: row for row in body["rows"]}
    # llama3.2 has no price in the example table, so demo's cost is partial.
    assert rows["demo"]["cost_is_partial"] is True
    assert "llama3.2" in body["models_without_prices"]


def test_usage_show_keeps_usage_from_a_key_that_is_gone(capsys, config_path, db):
    seed(db)
    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "usage", "show", "--prices", "prices.example.yaml",
    )
    rows = {row["id"]: row for row in payload(out)["rows"]}
    assert rows["retired"]["turns"] == 1
    assert rows["retired"]["name"] == "(not in the config)"


def test_usage_show_lists_a_key_with_no_usage_at_zero(capsys, config_path, db):
    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "usage", "show", "--prices", "prices.example.yaml",
    )
    rows = {row["id"]: row for row in payload(out)["rows"]}
    assert rows["demo"]["turns"] == 0
    assert rows["demo"]["estimated_cost"] == 0.0


def test_usage_show_never_prints_a_key(capsys, config_path, db):
    seed(db)
    _, out, _ = run(
        capsys, "--config", str(config_path), "--db", str(db), "usage", "show",
        "--prices", "prices.example.yaml",
    )
    assert EXISTING_SECRET not in out
    assert "cost basis" in out


def test_usage_show_reports_a_bad_price_file(capsys, config_path, db, tmp_path):
    bad = tmp_path / "prices.yaml"
    bad.write_text("models: [not, a, mapping]\n")
    code, _, err = run(
        capsys, "--config", str(config_path), "--db", str(db), "usage", "show",
        "--prices", str(bad),
    )
    assert code == 2
    assert err.startswith("error:")


# ------------------------------------------------------------ requests tail


def test_requests_tail_shows_the_newest_first(capsys, config_path, db):
    seed(db)
    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "requests", "tail", "-n", "3",
    )
    assert code == 0
    rows = payload(out)["rows"]
    assert len(rows) == 3
    assert rows[0]["key_id"] == "retired"  # the last one recorded
    assert [row["ts"] for row in rows] == sorted(
        (row["ts"] for row in rows), reverse=True
    )


def test_requests_tail_derives_the_provider_from_the_routes(capsys, config_path, db):
    seed(db)
    _, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "requests", "tail", "-n", "10",
    )
    by_model = {row["model"]: row["provider"] for row in payload(out)["rows"]}
    assert by_model["gpt-4o-mini"] == "openai"
    assert by_model["llama3.2"] == "ollama"


def test_requests_tail_marks_a_row_with_no_reported_usage(capsys, config_path, db):
    metering.record_llm_turn("demo", "gpt-4o-mini", 0, 0, db=db)
    _, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "requests", "tail",
    )
    assert payload(out)["rows"][0]["status"] == "no usage reported"


def test_requests_tail_says_what_it_cannot_show(capsys, config_path, db):
    seed(db)
    _, out, _ = run(
        capsys, "--config", str(config_path), "--db", str(db), "requests", "tail"
    )
    assert "keeps no latency" in out
    assert "HTTP status" in out
    assert "no message content is" in out
    assert "derived from the configured routes" in out


def test_requests_tail_shows_no_message_content_field(capsys, config_path, db):
    seed(db)
    _, out, _ = run(
        capsys, "--json", "--config", str(config_path), "--db", str(db),
        "requests", "tail",
    )
    for row in payload(out)["rows"]:
        assert set(row) == {
            "ts", "key_id", "provider", "model", "status",
            "input_tokens", "output_tokens",
        }


def test_requests_tail_on_an_empty_database(capsys, config_path, db):
    code, out, _ = run(
        capsys, "--config", str(config_path), "--db", str(db), "requests", "tail"
    )
    assert code == 0
    assert "(no rows)" in out


def test_requests_tail_rejects_a_non_positive_count(capsys, config_path, db):
    code, _, err = run(
        capsys, "--config", str(config_path), "--db", str(db),
        "requests", "tail", "-n", "0",
    )
    assert code == 2
    assert "positive" in err


# ------------------------------------------------------------------- shared


def test_every_command_takes_json(capsys, config_path, db):
    seed(db)
    for argv in (
        ["keys", "list"],
        ["usage", "show", "--prices", "prices.example.yaml"],
        ["requests", "tail"],
    ):
        code, out, _ = run(
            capsys, "--json", "--config", str(config_path), "--db", str(db), *argv
        )
        assert code == 0, argv
        assert isinstance(payload(out), dict), argv

    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "budget", "set",
        "--id", "demo", "--budget", "3",
    )
    assert code == 0
    assert payload(out)["monthly_budget_usd"] == 3.0

    code, out, _ = run(
        capsys, "--json", "--config", str(config_path), "keys", "create", "--id", "new"
    )
    assert code == 0
    assert payload(out)["id"] == "new"


def test_a_config_that_would_not_load_is_not_written(capsys, tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("routes: []\nkeys:\n  - key: 'abcdefghij'\n    id: 'one'\n")
    original = path.read_text()

    # A route naming a provider that does not exist would fail to load, so a
    # write carrying it must be refused before it replaces the file.
    document = cli.read_document(path)
    document["routes"] = [{"prefix": "x", "provider": "nonsense"}]
    with pytest.raises(cli.CliError, match="would not load"):
        cli.write_document(path, document)
    assert path.read_text() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_a_rewrite_does_not_keep_comments(capsys, config_path):
    """Stated rather than hidden: a dump loses what YAML comments said."""
    assert "# a comment" in config_path.read_text()
    run(capsys, "--config", str(config_path), "budget", "set", "--id", "demo",
        "--budget", "1")
    assert "# a comment" not in config_path.read_text()
    # The settings themselves survive.
    assert load_config(config_path).provider_for("gpt-4o-mini") == "openai"


def test_no_subcommand_exits_with_usage(capsys):
    with pytest.raises(SystemExit):
        cli.main([])
