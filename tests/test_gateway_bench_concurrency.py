"""The concurrency benchmark runs and labels what it measured.

No performance assertion: throughput and latency depend on the machine, so
these only check that the script works offline, reports every field it
promises, and says what its numbers do and do not mean.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bench_gateway_concurrency as bench  # noqa: E402

SMALL = {"requests": 6, "delay_ms": 1.0, "warmup": 2}


def test_it_reports_a_row_per_level():
    result = bench.run(levels=(1, 4), endpoint="chat", **SMALL)

    assert result["endpoint"] == "/v1/chat/completions"
    assert [row["concurrency"] for row in result["rows"]] == [1, 4]
    for row in result["rows"]:
        for field in ("requests_per_second", "p50_ms", "p95_ms", "p99_ms"):
            assert isinstance(row[field], float)
        assert row["errors"] == 0
        assert row["requests"] == SMALL["requests"]
        assert row["peak_memory_mb"] > 0
        assert row["p99_ms"] >= row["p95_ms"] >= row["p50_ms"]
        # A round trip cannot beat the delay the fake provider sleeps for.
        assert row["p50_ms"] >= result["provider_delay_ms"]


def test_it_runs_the_responses_endpoint_too():
    result = bench.run(levels=(2,), endpoint="responses", **SMALL)
    assert result["endpoint"] == "/v1/responses"
    assert len(result["rows"]) == 1


def test_it_states_its_scope_and_what_bounds_it():
    result = bench.run(levels=(1,), **SMALL)
    scope = result["scope"]
    assert "loopback" in scope
    assert "fake provider" in scope
    assert "not provider latency" in scope
    assert "not capacity" in scope
    # The two outside limits a reader needs in order to judge the numbers.
    assert result["server_worker_threads"] is None or result["server_worker_threads"] > 0
    assert result["client_processes"] == 1


def test_the_table_has_a_line_per_level():
    result = bench.run(levels=(1, 2), **SMALL)
    lines = bench.table(result).splitlines()
    assert "concurrency" in lines[0]
    assert "P99 ms" in lines[0]
    assert len(lines) == 2 + len(result["rows"])


def test_results_are_written_outside_the_repository_tree(tmp_path):
    result = bench.run(levels=(1,), **SMALL)
    written = bench.write_results(result, tmp_path)
    assert written.parent == tmp_path
    assert json.loads(written.read_text())["rows"][0]["concurrency"] == 1


def test_it_uses_a_temporary_database():
    """The benchmark must not write into the repository's database."""
    before = sorted(p.name for p in REPO_ROOT.glob("*.db"))
    bench.run(levels=(2,), **SMALL)
    assert sorted(p.name for p in REPO_ROOT.glob("*.db")) == before


@pytest.mark.parametrize(
    "raw,expected",
    [("1,10,50,200", [1, 10, 50, 200]), ("4", [4]), (" 2 , 3 ", [2, 3])],
)
def test_levels_parse(raw, expected):
    assert bench.parse_levels(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "-5", ","])
def test_bad_levels_are_rejected(raw):
    with pytest.raises(argparse.ArgumentTypeError):
        bench.parse_levels(raw)


def test_percentiles_of_a_known_sequence():
    """Nearest rank over a zero-based index, as bench_gateway.py already does.

    For 1..100 that puts the 95th at 95.0 and the 99th at 99.0. The median
    in the reported rows comes from statistics.median, not from here.
    """
    ordered = [float(n) for n in range(1, 101)]
    assert bench.percentile(ordered, 0.95) == 95.0
    assert bench.percentile(ordered, 0.99) == 99.0
    assert bench.percentile(ordered, 0.0) == 1.0
    assert bench.percentile(ordered, 1.0) == 100.0
    assert bench.percentile([], 0.5) == 0.0
    assert bench.percentile([7.0], 0.99) == 7.0
