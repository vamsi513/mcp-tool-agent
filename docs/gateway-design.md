# LLM gateway: design

An optional HTTP service that speaks the OpenAI `/v1/chat/completions` and
`/v1/responses` shapes, translates to OpenAI or Anthropic, keeps session state
so `previous_response_id` works on both, and enforces a monthly budget per API
key using the metering already in this repo.

Off by default. Nothing existing changes unless the gateway is started.

## What is reused, unchanged

| Existing | Used for |
|---|---|
| `store.connect` / `store.db_path` | The gateway's tables live in the same SQLite file (`GOVERNANCE_DB`), with the same WAL settings. No second storage layer. |
| `metering.record_llm_turn` | One usage row per upstream call. The gateway writes the same rows the agent does, so `govctl usage` already reports them. |
| `metering.load_prices`, `PriceTable.cost` | Cost estimation for budgets. Not reimplemented. |
| `metering.report` | Month-to-date spend per key, via `since=<first of month>`. Derived, not added to. |
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

## Not supported

Tools and function calling, images and any non-text content, batch endpoints,
prompt caching, context compaction, embeddings, logprobs, `n>1`, and
Anthropic's extended thinking. Requests using them are not translated and
nothing claims otherwise.

## Scope of the benchmark

`scripts/bench_gateway.py` measures the gateway's own overhead against a fake
provider with a fixed delay, over loopback, on one machine. It is not a
provider latency measurement and not a throughput claim.
