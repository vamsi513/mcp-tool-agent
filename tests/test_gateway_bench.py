"""The benchmark script runs and reports a measurement.

No performance assertion: timings vary by machine, so this only checks the
script works and labels its own scope.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bench_gateway  # noqa: E402


def test_bench_reports_both_percentiles_and_its_scope():
    result = bench_gateway.run(requests=5, delay_ms=1.0, endpoint="chat", warmup=1)

    assert result["endpoint"] == "/v1/chat/completions"
    assert result["requests"] == 5
    assert result["provider_delay_ms"] == 1.0
    for field in ("total_p50_ms", "total_p95_ms", "added_p50_ms", "added_p95_ms"):
        assert isinstance(result[field], float)
    assert result["total_p95_ms"] >= result["total_p50_ms"]
    # The round trip cannot be faster than the delay the fake provider sleeps.
    assert result["total_p50_ms"] >= result["provider_delay_ms"]
    assert "loopback" in result["scope"]
    assert "not a provider or throughput measurement" in result["scope"]


def test_bench_runs_the_responses_endpoint_too():
    result = bench_gateway.run(requests=3, delay_ms=1.0, endpoint="responses", warmup=1)
    assert result["endpoint"] == "/v1/responses"
    assert result["requests"] == 3


def test_bench_uses_a_temporary_database(tmp_path, monkeypatch):
    """The benchmark must not write into the repository's database."""
    before = sorted(p.name for p in REPO_ROOT.glob("*.db"))
    bench_gateway.run(requests=2, delay_ms=0.0, endpoint="chat", warmup=0)
    assert sorted(p.name for p in REPO_ROOT.glob("*.db")) == before
