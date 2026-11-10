"""Measure the gateway's own added latency, against a fake provider.

The fake provider sleeps for a fixed interval, so the difference between the
round trip through the gateway and that interval is the gateway's overhead:
request parsing, routing, the budget read, the provider call, the session
write and the response build.

This is one machine over loopback. It is not a provider latency measurement,
not a throughput measurement, and says nothing about behaviour under load.

    python scripts/bench_gateway.py
    python scripts/bench_gateway.py --requests 300 --delay-ms 10 --endpoint responses
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

import metering  # noqa: E402
from gateway.app import create_app  # noqa: E402
from gateway.config import GatewayConfig, KeyConfig, Route, SessionConfig  # noqa: E402
from gateway.providers.fake import FakeProvider  # noqa: E402
from gateway.sessions import SqliteSessionStore  # noqa: E402

KEY = "bench-key"
PRICES = {"currency": "USD", "models": {"fake-1": {"input_per_million": 0.0,
                                                   "output_per_million": 0.0}}}


def build_app(db: Path, delay_seconds: float):
    config = GatewayConfig(
        routes=(Route("fake-", "fake"),),
        keys={KEY: KeyConfig(id="bench", secret=KEY, monthly_budget_usd=None)},
        sessions=SessionConfig(backend="sqlite", ttl_seconds=600),
    )
    provider = FakeProvider(reply="bench reply text", delay_seconds=delay_seconds)
    return create_app(
        config,
        {"fake": provider},
        metering.parse_prices(PRICES),
        str(db),
        SqliteSessionStore(str(db)),
    )


class Server:
    """uvicorn on an ephemeral loopback port, in a thread."""

    def __init__(self, app) -> None:
        self._config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._server.started and self._server.servers:
                sockets = self._server.servers[0].sockets
                if sockets:
                    port = sockets[0].getsockname()[1]
                    return f"http://127.0.0.1:{port}"
            time.sleep(0.02)
        raise RuntimeError("the benchmark server did not start")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


def payload(endpoint: str) -> dict:
    if endpoint == "chat":
        return {
            "model": "fake-1",
            "messages": [{"role": "user", "content": "benchmark request body"}],
        }
    return {"model": "fake-1", "input": "benchmark request body"}


def run(requests: int, delay_ms: float, endpoint: str, warmup: int) -> dict:
    import httpx

    delay_seconds = delay_ms / 1000.0
    path = "/v1/chat/completions" if endpoint == "chat" else "/v1/responses"

    with tempfile.TemporaryDirectory(prefix="bench-gateway-") as tmp:
        db = Path(tmp) / "bench.db"
        with Server(build_app(db, delay_seconds)) as base_url:
            headers = {"Authorization": f"Bearer {KEY}"}
            body = payload(endpoint)
            with httpx.Client(timeout=30) as client:
                url = f"{base_url}{path}"
                for _ in range(warmup):
                    client.post(url, headers=headers, json=body).raise_for_status()

                samples: list[float] = []
                for _ in range(requests):
                    started = time.perf_counter()
                    response = client.post(url, headers=headers, json=body)
                    elapsed = (time.perf_counter() - started) * 1000.0
                    response.raise_for_status()
                    samples.append(elapsed)

    samples.sort()
    total_p50 = statistics.median(samples)
    total_p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return {
        "endpoint": path,
        "requests": requests,
        "warmup": warmup,
        "provider_delay_ms": delay_ms,
        "total_p50_ms": round(total_p50, 3),
        "total_p95_ms": round(total_p95, 3),
        "added_p50_ms": round(total_p50 - delay_ms, 3),
        "added_p95_ms": round(total_p95 - delay_ms, 3),
        "total_min_ms": round(samples[0], 3),
        "total_max_ms": round(samples[-1], 3),
        "scope": (
            "one machine, loopback, a fake provider with a fixed delay; "
            "not a provider or throughput measurement"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--delay-ms", type=float, default=5.0,
                        help="the fake provider's fixed delay")
    parser.add_argument("--endpoint", choices=["chat", "responses"], default="chat")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run(args.requests, args.delay_ms, args.endpoint, args.warmup)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"endpoint            {result['endpoint']}")
    print(f"requests            {result['requests']} (after {result['warmup']} warmup)")
    print(f"provider delay      {result['provider_delay_ms']:.3f} ms (fixed)")
    print(f"round trip P50      {result['total_p50_ms']:.3f} ms")
    print(f"round trip P95      {result['total_p95_ms']:.3f} ms")
    print(f"gateway added P50   {result['added_p50_ms']:.3f} ms")
    print(f"gateway added P95   {result['added_p95_ms']:.3f} ms")
    print(f"min / max           {result['total_min_ms']:.3f} / {result['total_max_ms']:.3f} ms")
    print()
    print(f"scope: {result['scope']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
