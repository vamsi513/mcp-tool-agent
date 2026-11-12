"""Measure the gateway at several concurrency levels, against a fake provider.

The companion to bench_gateway.py, which times one request at a time. This
one holds a fixed number of requests in flight and reports what comes out:
throughput, the latency spread, how many requests failed, and the peak
resident memory of the process that ran them.

Everything is loopback on one machine, and the provider is the in-process
fake sleeping for a fixed interval. So the numbers describe this gateway's
own queueing and bookkeeping under parallel load, on whatever machine ran
them. They are not provider latency, not capacity, and not a result that
carries to another machine, another database or a real upstream.

Two things bound the figures from outside the gateway, and both are reported
so a reading of the table is not mistaken for a limit of the code:

* Endpoints are plain `def`, so the framework runs each one in a worker
  thread. The size of that pool caps how many requests are actually being
  served at once, whatever the client offers.
* Budgets are enforced per key by serialising each key's check-and-charge
  window, so a key with a budget takes one request at a time by design. The
  benchmark key has no budget, which is why it measures the parallel path.

    python scripts/bench_gateway_concurrency.py
    python scripts/bench_gateway_concurrency.py --levels 1,10,50 --delay-ms 10
    python scripts/bench_gateway_concurrency.py --endpoint responses --json
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

# Reused rather than copied: same app wiring, same server, same key.
from bench_gateway import KEY, Server, build_app  # noqa: E402

DEFAULT_LEVELS = (1, 10, 50, 200)
RESULTS_DIR = REPO_ROOT / "bench-results"


def peak_memory_bytes() -> int:
    """Peak resident set size of this process.

    getrusage reports kilobytes on Linux and bytes on macOS, which is a
    documented difference and not something to guess at per machine.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already sorted sequence."""
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def payload(endpoint: str) -> dict:
    if endpoint == "chat":
        return {
            "model": "fake-1",
            "messages": [{"role": "user", "content": "benchmark request body"}],
        }
    return {"model": "fake-1", "input": "benchmark request body"}


def worker_pool_size() -> int | None:
    """How many sync endpoint calls the framework will run at once."""
    try:
        import anyio.to_thread

        async def capacity() -> int:
            return int(anyio.to_thread.current_default_thread_limiter().total_tokens)

        import anyio

        return int(anyio.run(capacity))
    except Exception:
        return None


def drive(
    url: str, headers: dict, body: dict, level: int, requests: int
) -> tuple[list[float], int, float]:
    """Keep `level` requests in flight until `requests` have been sent.

    The client's own connection pool is opened wide enough for the level, so
    the number being measured is the gateway's, not the client's.
    """
    samples: list[float] = []
    errors = 0
    guard = threading.Lock()

    limits = httpx.Limits(
        max_connections=level + 10, max_keepalive_connections=level + 10
    )
    with httpx.Client(timeout=60, limits=limits) as client:

        def one(_index: int) -> None:
            nonlocal errors
            started = time.perf_counter()
            try:
                response = client.post(url, headers=headers, json=body)
                elapsed = (time.perf_counter() - started) * 1000.0
                ok = response.status_code == 200
            except httpx.HTTPError:
                elapsed = (time.perf_counter() - started) * 1000.0
                ok = False
            with guard:
                samples.append(elapsed)
                if not ok:
                    errors += 1

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=level) as pool:
            list(pool.map(one, range(requests)))
        wall = time.perf_counter() - started

    return samples, errors, wall


def drive_chunk(args: tuple) -> tuple[list[float], int, float]:
    """`drive` behind a picklable signature, for the process pool."""
    url, headers, body, level, requests = args
    return drive(url, headers, body, level, requests)


def run_level(
    level: int,
    requests: int,
    delay_ms: float,
    endpoint: str,
    warmup: int,
    client_processes: int = 1,
) -> dict:
    delay_seconds = delay_ms / 1000.0
    path = "/v1/chat/completions" if endpoint == "chat" else "/v1/responses"
    body = payload(endpoint)

    with tempfile.TemporaryDirectory(prefix="bench-gateway-concurrency-") as tmp:
        db = Path(tmp) / "bench.db"
        with Server(build_app(db, delay_seconds)) as base_url:
            headers = {"Authorization": f"Bearer {KEY}"}
            url = f"{base_url}{path}"
            if warmup:
                drive(url, headers, body, min(level, warmup), warmup)

            if client_processes <= 1:
                samples, errors, wall = drive(url, headers, body, level, requests)
            else:
                # The client's own threads contend with the server's for one
                # GIL when they share a process, which at high levels is a
                # large part of what the single-process numbers measure.
                per_level = max(1, level // client_processes)
                per_requests = max(1, requests // client_processes)
                work = [
                    (url, headers, body, per_level, per_requests)
                ] * client_processes
                started = time.perf_counter()
                with ProcessPoolExecutor(max_workers=client_processes) as pool:
                    parts = list(pool.map(drive_chunk, work))
                wall = time.perf_counter() - started
                samples = [sample for part in parts for sample in part[0]]
                errors = sum(part[1] for part in parts)
                level = per_level * client_processes
                requests = per_requests * client_processes

    samples.sort()
    succeeded = requests - errors
    return {
        "concurrency": level,
        "requests": requests,
        "errors": errors,
        "requests_per_second": round(succeeded / wall, 2) if wall > 0 else 0.0,
        "wall_seconds": round(wall, 3),
        "p50_ms": round(statistics.median(samples), 3) if samples else 0.0,
        "p95_ms": round(percentile(samples, 0.95), 3),
        "p99_ms": round(percentile(samples, 0.99), 3),
        "min_ms": round(samples[0], 3) if samples else 0.0,
        "max_ms": round(samples[-1], 3) if samples else 0.0,
        "peak_memory_mb": round(peak_memory_bytes() / (1024 * 1024), 1),
    }


def run(
    levels: Sequence[int] = DEFAULT_LEVELS,
    requests: int = 400,
    delay_ms: float = 5.0,
    endpoint: str = "chat",
    warmup: int = 20,
    client_processes: int = 1,
) -> dict:
    rows = [
        run_level(level, requests, delay_ms, endpoint, warmup, client_processes)
        for level in levels
    ]
    return {
        "endpoint": "/v1/chat/completions" if endpoint == "chat" else "/v1/responses",
        "provider_delay_ms": delay_ms,
        "requests_per_level": requests,
        "warmup": warmup,
        "server_worker_threads": worker_pool_size(),
        "client_processes": client_processes,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "scope": (
            "one machine, loopback, an in-process fake provider with a fixed "
            "delay, a key with no budget; not provider latency, not capacity, "
            "and not transferable to another machine or a real upstream"
        ),
    }


def table(result: dict) -> str:
    header = (
        f"{'concurrency':>11}  {'req/s':>9}  {'P50 ms':>9}  {'P95 ms':>9}  "
        f"{'P99 ms':>9}  {'errors':>6}  {'peak MB':>8}"
    )
    lines = [header, "-" * len(header)]
    for row in result["rows"]:
        lines.append(
            f"{row['concurrency']:>11}  {row['requests_per_second']:>9.2f}  "
            f"{row['p50_ms']:>9.3f}  {row['p95_ms']:>9.3f}  {row['p99_ms']:>9.3f}  "
            f"{row['errors']:>6}  {row['peak_memory_mb']:>8.1f}"
        )
    return "\n".join(lines)


def write_results(result: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    endpoint = "chat" if result["endpoint"].endswith("completions") else "responses"
    path = directory / f"concurrency-{endpoint}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    return path


def parse_levels(raw: str) -> list[int]:
    levels = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        value = int(piece)
        if value < 1:
            raise argparse.ArgumentTypeError("a concurrency level must be at least 1")
        levels.append(value)
    if not levels:
        raise argparse.ArgumentTypeError("give at least one concurrency level")
    return levels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--levels", type=parse_levels, default=list(DEFAULT_LEVELS),
        help="comma-separated concurrency levels (default 1,10,50,200)",
    )
    parser.add_argument("--requests", type=int, default=400,
                        help="requests per level")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--delay-ms", type=float, default=5.0,
                        help="the fake provider's fixed delay")
    parser.add_argument("--endpoint", choices=["chat", "responses"], default="chat")
    parser.add_argument(
        "--client-processes", type=int, default=1,
        help=(
            "spread the load generator over this many processes; with 1 the "
            "client's threads share a GIL with the server's, which costs more "
            "the higher the concurrency level"
        ),
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--no-write", action="store_true",
                        help="print only, write no JSON file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run(
        levels=args.levels,
        requests=args.requests,
        delay_ms=args.delay_ms,
        endpoint=args.endpoint,
        warmup=args.warmup,
        client_processes=args.client_processes,
    )

    written = None
    if not args.no_write:
        written = write_results(result, args.results_dir)
        result["written_to"] = str(written)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"endpoint            {result['endpoint']}")
    print(f"provider delay      {result['provider_delay_ms']:.3f} ms (fixed)")
    print(f"requests per level  {result['requests_per_level']} "
          f"(after {result['warmup']} warmup)")
    print(f"server worker pool  {result['server_worker_threads']} threads")
    print(f"client processes    {result['client_processes']}")
    print()
    print(table(result))
    print()
    print(f"scope: {result['scope']}")
    if result["client_processes"] <= 1 and max(
        row["concurrency"] for row in result["rows"]
    ) >= 50:
        print(
            "note: the load generator shared this process with the server, so "
            "the higher levels measure that contention as well as the gateway; "
            "re-run with --client-processes to separate them"
        )
    if written is not None:
        print(f"written to: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
