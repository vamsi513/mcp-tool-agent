# LLM gateway: design

An optional HTTP service that speaks the OpenAI `/v1/chat/completions` and
`/v1/responses` shapes, translates to OpenAI or Anthropic, keeps session state
so `previous_response_id` works on both, and enforces a monthly budget per API
key using the metering already in this repo.

Off by default. Nothing existing changes unless the gateway is started.

## What is reused

Unchanged except for `store.connect`, which had to be fixed to survive
concurrent opens; conflict 9 below says why.

| Existing | Used for |
|---|---|
| `store.connect` / `store.db_path` | The gateway's tables live in the same SQLite file (`GOVERNANCE_DB`), with the same WAL settings. No second storage layer. `connect` now tolerates two connections racing to switch the journal mode. |
| `metering.record_llm_turn` | One usage row per upstream call. The gateway writes the same rows the agent does, so `govctl usage` already reports them. |
| `metering.load_prices`, `PriceTable.cost` | Cost estimation for budgets. Not reimplemented. |
| `metering.report` | Month-to-date spend per key, via `since=<first of month>`. Derived, not added to. |
| `metering.recent_llm_turns` | Added beside it, for the CLI's `requests tail`. A read of the table metering already owns, rather than SQL written against someone else's schema. |
| The `http_client` module-factory convention | Provider clients create `httpx.Client` through a module-level factory so tests swap the transport without rebinding the global `httpx.Client`. That global rebind has caused a real failure in this repo before. |

## What is added

- `gateway/` package: FastAPI app, config, both endpoints, SSE streaming.
- `gateway/providers/`: a `Provider` protocol with `openai`, `anthropic` and
  `fake` implementations. Every test uses `FakeProvider`; no test reaches a
  provider.
- `gateway/translate.py`: the Anthropic mapping, as pure functions so each
  case is tested on its own.
- `gateway/sessions.py`: a `SessionStore` interface, SQLite default, Redis
  optional.
- `gateway/budgets.py`: month-to-date spend and the budget decision.
- `gateway.example.yaml`: routes, keys and budgets, session TTL, backend.
- `scripts/bench_gateway.py`: added latency against a fixed-delay fake.

Added while hardening:

- `gateway/providers/ollama.py`: self-hosted models over Ollama's OpenAI-compatible API. Its address comes from `providers.ollama` in the config rather than the environment, because there is no credential to keep out of the file.
- `gateway/budgets.py`'s `AdmissionGate`: the per-key window that makes the budget hold under parallel load.
- `gateway/cli.py`: `keys create`, `keys list`, `budget set`, `usage show`, `requests tail`, over the existing config file and the existing usage rows.
- `scripts/bench_gateway_concurrency.py`: throughput, latency percentiles, errors and peak memory per concurrency level.
- Optional `name` and `created_at` on a key, for the CLI to list. Nothing in the request path reads them.

New tables, both created lazily in the shared database:
`gateway_sessions` and nothing else — budget state is derived from
`usage_llm`, so there is one source of truth for spend.

## Conflicts with existing code

**1. Installing FastAPI downgrades starlette.** `fastapi==0.122.0` pins
`starlette<0.51`, which moved the installed version from 1.6.0 to 0.50.0.
`mcp==1.29.1` also depends on starlette. Resolved by verification rather than
assumption: the full existing suite was run after the change and is unchanged
at 275 passed, 2 skipped. Recorded here because it is a real constraint on the
dependency set, not a free addition.

**2. `policy.py` cannot express a budget.** `KNOWN_RULE_KEYS` is a frozenset
of four keys (`repos`, `owners`, `max_limit`, `max_body_chars`) and
`parse_policy` raises `PolicyError` on anything else;
`tests/test_policy.py::test_unknown_rule_key_is_rejected` pins that. Adding a
`monthly_budget_usd` rule would mean editing an existing module and an
existing test. Gateway keys therefore live in their own
`gateway.example.yaml`. The policy layer stays about MCP tool access; the
gateway's axis is per-key spend. These are deliberately not merged.

**3. Metering's unit is a `user`; the gateway's is an API key.** No schema
change: the gateway passes the key's *id* as metering's `user`. The visible
consequence is that `govctl usage --group user` will list gateway key ids
alongside MCP user names in one table. That is accepted rather than hidden,
and is the price of not duplicating the usage schema.

**4. `prices.example.yaml` is all zeros on purpose.** A budget computed from
it can never be exceeded, so the budget tests supply their own price table.
The README says plainly that costs are estimates.

**5. `tests/fake_openai.py` is not the `FakeProvider`.** The existing fake is
an HTTP server shaped for the agent's eval (a question-to-tool plan). The
gateway needs an in-process implementation of its own provider interface.
Both are kept; neither is bent to fit the other.

**6. The gateway does not write to the audit log.** `audit.audit_log` has a
`CHECK (decision IN ('allow','deny'))`, a `role` column and a tool-call shaped
`arguments` field. Forcing HTTP requests into it would make `govctl audit`
output mix two unrelated kinds of event. Gateway usage goes to metering, which
fits exactly; budget refusals are logged through `logging`.

**7. CI.** The existing `test` job's steps are left byte-identical. Redis
tests run in a second job with a service container, so the current matrix is
untouched.

**8. Budgets could not be enforced under parallel load.** `budgets.check`
reads month-to-date spend out of the usage rows and `budgets.record` writes
the row for a call, with the upstream call in between, and nothing tied the
two together. Measured: 30 concurrent requests on a key whose budget allowed
one call admitted 24 and recorded 24 times the limit. A cost is not knowable
before a call, so there is nothing to reserve up front; `budgets.AdmissionGate`
therefore lets one request at a time hold the window between the check and
the charge for a given key. The cost is that a budgeted key serves requests
one at a time. Keys with no budget take no lock and keys never wait on each
other, so the price is paid only where a limit was asked for.

**9. Opening the database concurrently could fail.** `store.connect` set
`PRAGMA journal_mode=WAL` on every connection. Changing the journal mode takes
an exclusive lock and SQLite does not run the busy handler for it, so
connections opening a not-yet-converted database at the same moment raced and
the losers raised "database is locked" rather than waiting. It surfaced as an
intermittent 500 whenever parallel requests first recorded usage. `connect`
now reads the mode first, sets it only when it has to, and waits out a
concurrent attempt. This is in `store.py` rather than the gateway because it
is the shared connection helper and every caller had the same defect.

**10. The CLI has no store of its own to manage.** Keys live in the gateway
config file, so `keys create` and `budget set` rewrite that file through a
YAML load and dump, which loses its comments. The write is atomic, keeps the
file's permissions, validates the result before replacing it and refuses to
touch the committed example. The alternative, a key table in the database,
would have meant two places to look for a key and was not taken.

**11. The meter has no latency or status to show.** `usage_llm` holds a
timestamp, a user, a model and two token counts. `requests tail` therefore
derives the provider from the configured routes, reports a status only as
"reported usage" or "did not", and shows no latency at all rather than
inventing a number. Recording latency would mean changing the shared metering
schema and the gateway's write path, which is more than hardening.

**12. Keys had no name or creation time.** Both were added to `KeyConfig` as
optional fields so `keys list` has something to show. Nothing in the request
path reads them and a key written by hand without them behaves exactly as
before.

## Not supported

Tools and function calling, images and any non-text content, batch endpoints,
prompt caching, context compaction, embeddings, logprobs, `n>1`, and
Anthropic's extended thinking. Requests using them are not translated and
nothing claims otherwise.

## Scope of the benchmarks

`scripts/bench_gateway.py` measures the gateway's own overhead against a fake
provider with a fixed delay, over loopback, on one machine. It is not a
provider latency measurement and not a throughput claim.

`scripts/bench_gateway_concurrency.py` adds concurrency levels, reporting
throughput, P50/P95/P99, errors and peak memory per level. Two things bound
its numbers from outside the gateway and are reported with them: the worker
thread pool the framework runs sync endpoints in, and, by default, the fact
that the load generator shares a process and a GIL with the server.
Separating the client raised measured throughput at 50 in flight about
threefold, so the default numbers understate the gateway. Neither table is a
capacity claim. The README carries both, with that caveat attached.
