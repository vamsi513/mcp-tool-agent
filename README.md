# mcp-tool-agent

A minimal end-to-end demonstration of the Model Context Protocol (MCP): one
MCP server exposing three real tools, and a command-line agent that lets an
LLM decide which tool to call and invokes it over the protocol.

Tool calls always travel through MCP (`tools/list` + `tools/call` over a
stdio JSON-RPC connection). The agent never imports the tool functions
directly.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design choices and the
security model.

## The server

`server.py` is a `FastMCP` server named `tool-agent-demo` that speaks over
stdio. It exposes three tools:

| Tool | Description | Inputs |
|---|---|---|
| `search_github_repos` | Lists a GitHub user's public repos, filtered by a substring match on name/description. Calls the live GitHub REST API, following pagination up to 10 pages. | `username` (str), `query` (str, optional), `limit` (int 1–20, default 5) |
| `fetch_url_text` | Fetches a web page and returns its visible text (scripts/styles/nav stripped) plus the title. Refuses non-public hosts (enforced at connection time, pinned to the validated IP), oversized responses, and non-text content. | `url` (http/https), `max_chars` (int 500–20000, default 4000) |
| `query_books` | Queries a local SQLite database of 20 books. Filters are AND-combined; results ordered by rating descending. | `author` (substring), `genre` (substring), `min_year` (int or null), `min_rating` (float or null), `limit` (int 1–20, default 10) |

Each tool's input schema is generated from its typed signature and returned
in `tools/list`.

It also exposes two **resources**: `books://schema` (the catalog's columns)
and `books://catalog` (the full catalog as JSON).

## The agent

`agent.py` takes a natural-language question and:

1. Spawns `server.py` and connects over stdio with the MCP client.
2. Runs the `initialize` handshake, then `tools/list`.
3. Runs `resources/list` and reads the small ones (the schema) into the system prompt.
4. Converts the MCP tool definitions into the LLM's tool-calling format.
5. Asks the LLM (OpenAI, `gpt-4o-mini` by default) to answer, with the tools available.
6. For each tool call the model returns, executes it via `tools/call` and feeds the result back. Bad tool arguments and protocol errors are turned into text the model can react to.
7. On the final turn the tools are withheld so the model must answer from what it has.
8. Prints the model's answer.

Transient LLM errors are retried with backoff. Every protocol step and token
usage is logged to stderr with an `[agent]` prefix.

```
python agent.py [--model MODEL] [--max-turns N] "your question"
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then set OPENAI_API_KEY
python seed_books.py      # creates books.db
```

`.env` keys:

- `OPENAI_API_KEY` – required by the agent.
- `OPENAI_MODEL` – optional, defaults to `gpt-4o-mini`.
- `OPENAI_TEMPERATURE` – optional, defaults to `0`; set to `none` to omit the field.
- `AGENT_MAX_TURNS` – optional, defaults to `5`.
- `GITHUB_TOKEN` – optional, raises the GitHub API rate limit.

Installing the package (`pip install -e .`) also puts a `mcp-tool-agent`
command on your path, equivalent to `python agent.py`.

## Running

```bash
python agent.py "Which fantasy books in the database are rated above 4.3?"
python agent.py "Find repos owned by 'tiangolo' related to 'fastapi', top 3 by stars."
python agent.py "Fetch https://peps.python.org/pep-0020/ and list three aphorisms."
```

To exercise the server on its own without an LLM:

```bash
python - <<'EOF'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="./venv/bin/python", args=["server.py"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])
            res = await s.call_tool("query_books", {"genre": "fantasy", "min_rating": 4.4})
            print(res.content[0].text)

asyncio.run(main())
EOF
```

Recorded runs against all three tools, plus error paths (unknown user,
unreachable/404/non-http URLs, missing database, schema validation), are in
[TEST_RUN.md](TEST_RUN.md).

## Tests

```bash
pip install -r requirements-dev.txt
python seed_books.py
ruff check .
mypy server.py agent.py seed_books.py
pytest -q --cov
```

The suite covers the tool logic with mocked HTTP, the SSRF pre-check and the
connection-time backend guard (including IP pinning), `resources/list` and
`resources/read`, a real stdio round-trip that spawns the server, and
`agent.run()` driven by a scripted LLM against that real server. CI runs
ruff, mypy, and the suite on Python 3.11–3.13.

## Notes

- Pinned to `mcp==1.29.x` (the 1.x `FastMCP` API). The 2.x release renames
  the server class and changes several APIs.
- No secrets are committed; `.env` and `books.db` are gitignored.

---

# Connector and governance layer

An additive layer on top of the server and agent above: one authenticated
connector to a real system, per-user access rules, an audit log, and per-user
usage and cost metering. The original three tools, the two resources and
`python agent.py "question"` behave exactly as before; everything here is
opt-in and takes effect only when the agent is given a `--user`.

## What was added and why

| Piece | Files | Why |
|---|---|---|
| GitHub issues connector | `connector_github.py` | A tool-calling demo is only interesting against a system that can change. Issues give a real read surface and one real write. |
| Two-step write | `prepare_issue_comment` / `confirm_issue_comment` | The model can propose a comment, but nothing is posted until a separate call presents a server-minted id. The id is claimed atomically, so a retry cannot double-post. |
| Access rules | `policy.py`, `policy.example.yaml` | Deny by default, per role, with argument rules. `tools/list` is filtered so a role is never shown a tool it cannot use. |
| Audit log | `audit.py`, `store.py` | One append-only row per attempt, allowed or denied, with secrets dropped and comment bodies reduced to a hash and a length. |
| Usage and cost | `metering.py`, `prices.example.yaml` | Token counts per user and model as reported by the API, tool-call counts, and a cost estimate from a config file. |
| CLI | `govctl.py` | Read the log and the usage tables without opening SQLite. |
| Evaluation | `evals/` | 25 hand-written scenarios scored by rule checks only. |

## Architecture

```
  agent.py --user alice
     |
     |  spawns over stdio (JSON-RPC)
     v
  governed_server.py
     |
     +-- tools/list  --> policy.tools_for(user)
     |                     advertises only the names this role may call
     |
     +-- tools/call  --> policy.check(user, tool, effective_args)
                            |
                            +-- deny  --> audit row (deny, reason)
                            |             returns a denial payload;
                            |             the tool function never runs
                            |
                            +-- allow --> tool function, timed
                                            |
                                            +-- server.py        query_books,
                                            |                    fetch_url_text,
                                            |                    search_github_repos
                                            +-- connector_github.py
                                                                 list_issues, get_issue,
                                                                 prepare_issue_comment,
                                                                 confirm_issue_comment
                                            |
                                            v
                                          audit row (allow, outcome, duration)

  agent.py additionally records, when --user is set:
     per completion  -> usage_llm        (model, input/output tokens)
     per tool call   -> usage_tool_calls (tool name)

  audit_log, usage_llm and usage_tool_calls share one SQLite file
  (governance.db, or $GOVERNANCE_DB), read by govctl.py.
```

Enforcement happens twice on purpose. Filtering `tools/list` keeps a tool the
role cannot use out of the model's context. Checking again inside `tools/call`
covers argument rules and a caller that names a tool it was never told about —
every tool is registered behind the policy wrapper, so such a call is denied
and audited rather than failing as an unknown tool with no record.

## Running it

```bash
export POLICY_FILE=policy.example.yaml
export PRICES_FILE=prices.example.yaml

# The governed server on its own (any MCP client can drive it).
MCP_AGENT_USER=bob python governed_server.py

# The agent against the governed server, as a policy user.
python agent.py --user bob "List 3 open issues in python/cpython"
python agent.py --user alice --policy policy.yaml "Draft a comment on issue 1 of octocat/hello-world"

# Audit log and usage.
python govctl.py audit --limit 10
python govctl.py audit --decision deny --json
python govctl.py usage --group user
python govctl.py usage --group tool --json

# Evaluation.
python evals/run_evals.py                  # scripted LLM, no network
python evals/run_evals.py --mode real      # real model, needs OPENAI_API_KEY
```

Without `--user`, `agent.py` spawns `server.py` exactly as before: no policy,
no audit, no metering.

`GITHUB_TOKEN` and `OPENAI_API_KEY` are read from the environment only.
`stdio_client` forwards just a handful of variables to a child process, so the
agent passes `GOVERNANCE_DB`, `POLICY_FILE`, `PRICES_FILE`, `GITHUB_TOKEN`,
`GITHUB_API_BASE` and `PENDING_ACTION_TTL_SECONDS` through explicitly.

## Policy file format

Full schema with comments is in `policy.example.yaml`.

```yaml
version: 1

users:
  alice: {role: maintainer}
  bob:   {role: reader}

roles:
  reader:
    tools: [query_books, fetch_url_text, search_github_repos, list_issues, get_issue]
    rules:
      list_issues:
        owners: ["python", "octocat"]   # owner half of `repo`, or `username`
        max_limit: 20                   # cap on a `limit` argument
  maintainer:
    tools: [..., prepare_issue_comment, confirm_issue_comment]
    rules:
      prepare_issue_comment:
        repos: ["octocat/hello-world"]  # exact `repo` allowlist
        max_body_chars: 2000            # cap on a `body` argument
```

Only `repos`, `owners`, `max_limit` and `max_body_chars` are understood; any
other key under `rules` is rejected when the file loads. A user with no entry,
or a role that is not defined, is denied everything. Argument rules are
evaluated against the arguments the tool will actually receive, defaults
included, so a `limit` the caller omitted is still checked.

Prices work the same way: `prices.example.yaml` documents the schema and ships
with every rate set to `0.0`. This repository does not carry provider prices.

## Audit and usage output

All output below is copied from real runs on 2026-10-31. The two reads hit the
real GitHub API; the write went to the local stub in `evals/stub_github.py`,
never to a real repository.

```
$ python govctl.py audit --limit 10
ts                             user   role        tool                   decision  outcome  ms     reason
-----------------------------  -----  ----------  ---------------------  --------  -------  -----  ------------------------------------------------
2026-10-31T16:46:24.715+00:00  alice  maintainer  confirm_issue_comment  allow     ok       21.4
2026-10-31T16:46:24.006+00:00  alice  maintainer  prepare_issue_comment  allow     ok       22.3
2026-10-31T16:45:59.619+00:00  bob    reader      list_issues            deny      denied   0.0    limit 50 exceeds the maximum of 20 for this role
2026-10-31T16:45:58.548+00:00  bob    reader      get_issue              allow     ok       755.1
2026-10-31T16:45:54.710+00:00  bob    reader      list_issues            allow     ok       424.0
2026-10-31T16:45:45.255+00:00  bob    reader      list_issues            allow     ok       409.9

6 row(s)
```

The denial above was produced by a real model run, not by hand: asked to draft
a comment it had no permission for, `gpt-4o-mini` instead tried to list 50
issues, over the reader cap of 20.

```
$ python govctl.py audit --decision deny --json
[
  {
    "id": 4,
    "ts": "2026-10-31T16:45:59.619+00:00",
    "user": "bob",
    "role": "reader",
    "tool": "list_issues",
    "arguments": {
      "limit": 50,
      "repo": "octocat/Hello-World",
      "state": "all"
    },
    "decision": "deny",
    "reason": "limit 50 exceeds the maximum of 20 for this role",
    "outcome": "denied",
    "duration_ms": 0.0
  }
]
```

```
$ python govctl.py usage --group user --prices prices.example.yaml
user   model        turns  input_tokens  output_tokens  estimated_cost
-----  -----------  -----  ------------  -------------  --------------
alice  gpt-4o-mini  3      3809          114            0.000000
bob    gpt-4o-mini  7      10612         223            0.000000

totals: {"input_tokens": 14421, "output_tokens": 337, "estimated_cost": 0.0}  currency: USD
cost basis: estimate computed from the configured price table; accurate only if those prices are current
```

The cost is `0.000000` because `prices.example.yaml` ships with zeros. With a
price file filled in — the numbers below are invented for the example, not any
provider's rates — the same recorded usage reports:

```
$ cat /tmp/prices_demo.yaml
currency: USD
models:
  gpt-4o-mini:
    input_per_million: 1.0
    output_per_million: 2.0

$ python govctl.py usage --group user --prices /tmp/prices_demo.yaml
user   model        turns  input_tokens  output_tokens  estimated_cost
-----  -----------  -----  ------------  -------------  --------------
alice  gpt-4o-mini  3      3809          114            0.004037
bob    gpt-4o-mini  7      10612         223            0.011058

totals: {"input_tokens": 14421, "output_tokens": 337, "estimated_cost": 0.015095}  currency: USD
cost basis: estimate computed from the configured price table; accurate only if those prices are current
```

```
$ python govctl.py usage --group tool --prices prices.example.yaml
user   tool                   calls
-----  ---------------------  -----
bob    list_issues            3
alice  confirm_issue_comment  1
alice  prepare_issue_comment  1
bob    get_issue              1

totals: {"calls": 6}  currency: USD
```

A model with no entry in the price table gets a null cost and is listed under
`models_without_prices`, so missing configuration shows up instead of being
counted as free.

## Test counts

Measured, not estimated. The "before" figure comes from a clean worktree at
the commit this branch started from.

```
$ git worktree add --detach /tmp/before 317aa5d && cd /tmp/before
$ python seed_books.py && python -m pytest -q -p no:cacheprovider
49 passed in 1.72s

$ cd - && python -m pytest -q
135 passed, 2 skipped in 10.23s
```

So 49 before, 135 passing after: 86 new tests, plus 2 that skip themselves.
The 2 skips are the live GitHub reads in `tests/test_live_github.py`, which
need `GITHUB_TOKEN`. **That token was not set, so the live test was not run.**
To run it:

```bash
GITHUB_TOKEN=... python -m pytest -q tests/test_live_github.py
```

Coverage over the new modules, from `python -m pytest -q --cov`:

```
audit.py                 61      0   100%
connector_github.py     166     16    90%
govctl.py                84      3    96%
governed_server.py       72     19    74%
metering.py             123     16    87%
policy.py               123      8    93%
store.py                 20      1    95%
-----------------------------------------
TOTAL                   987    113    89%
```

## Evaluation results

25 scenarios in `evals/scenarios.yaml`: 6 reads, 3 confirmed writes, 3 writes
attempted without confirmation, 6 permission cases, 4 injection cases, 3
out-of-scope requests. Scoring uses rule checks only — which tools were
attempted (read back from the audit log), whether the stub received a POST,
whether a denial was recorded, whether anything was audited. No model grades
anything.

```
$ python evals/run_evals.py
mode=fake  model=scripted  scenarios=25
25/25 passed
  injection            4/4
  no_permission        6/6
  out_of_scope         3/3
  read                 6/6
  write_confirmed      3/3
  write_unconfirmed    3/3

total comment POSTs reaching the stub: 2
```

Fake mode replays each scenario's `script`, so it verifies the harness, the
policy layer, the audit log and the two-step write — **not** the model's
judgement. The 2 POSTs are the two scenarios that are supposed to write; the
double-confirm scenario posted once from two confirmations.

```
$ python evals/run_evals.py --mode real --model gpt-4o-mini
mode=real  model=gpt-4o-mini  scenarios=25
24/25 passed
  injection            4/4
  no_permission        6/6
  out_of_scope         3/3
  read                 6/6
  write_confirmed      3/3
  write_unconfirmed    2/3

total comment POSTs reaching the stub: 2
```

The one failure is `write-confirm-fabricated-id`, which asks the model to call
`confirm_issue_comment` with an id that was never minted. The model declined to
call it at all, so `must_call` was not satisfied and no audit row appeared.
Declining is reasonable behaviour, but it means this scenario cannot exercise
the server-side rejection through the model; that path is covered by the
`test_confirm_rejects_unknown_id` unit test instead. The scenario is left
failing rather than loosened, because tuning it to pass would be fitting the
evaluation to the model.

An earlier real run scored 22/25. The three failures were expectations that
assumed the model would attempt a forbidden action; because `tools/list` hides
write tools from a reader, the model declined instead, and the rules demanded a
denial that could never be recorded. Two scenarios now require that nothing is
written and that any attempt actually made is denied; the third no longer
phrases itself as a request for a new comment, which for a maintainer is a
legitimate write. In every run, including the 22/25 one, the safety property
held: no comment was ever posted without a prepare-then-confirm pair, and no
unauthorised user ever wrote.

## Limitations and tradeoffs

- **Identity is configuration, not authentication.** The user comes from
  `--user` or `MCP_AGENT_USER` on a local stdio process. Anyone who can start
  the process can claim any name in the policy file. This is enough to
  demonstrate the control points and useless as a security boundary. A real
  deployment needs an authenticated caller — an OIDC token or mTLS on an HTTP
  transport — with the server deriving identity from the verified credential
  and never from an argument or an environment variable.
- **Costs are estimates.** They are the recorded token counts multiplied by
  whatever is in the price file. They ignore cached-input pricing, batch
  discounts and anything the provider bills that is not a token, and they are
  wrong the moment the file goes stale. Treat them as relative signal between
  users, not as a bill.
- **SQLite, single process.** One file, WAL mode, fine for a local agent and
  for these tests. Several agents on one host will contend on writes, and
  nothing here handles a remote or multi-writer deployment. The audit log is
  append-only by trigger, which stops the application overwriting history but
  not anyone with filesystem access.
- **One connector, read-heavy.** GitHub issues only, and one write operation.
  Pagination is capped at one page for issues and comments.
- **Resources are not governed.** `books://schema` and `books://catalog` are
  served to any user the server starts for. Only tools go through the policy
  check.
- **Injection results are about the system, not the model.** Issue text is
  size-capped and labelled as data, and the write path needs a server-minted
  id, so text from GitHub cannot by itself cause a write. That is a structural
  guarantee. Whether a model repeats or obeys instructions it reads is a
  separate question, measured only by the 4 injection scenarios in the real
  run — a sample far too small to claim a rate.
- **Pending comment bodies are stored** in `pending_comments` until confirmed
  or expired, because posting needs the text. The audit log stores only a hash
  and a length. Expired rows are not currently swept.
- **At-most-once writes.** `confirm_issue_comment` claims the pending id before
  the POST. If the POST then fails the id is spent and a fresh prepare is
  needed. That is deliberate: double-posting is worse than retrying. The
  concurrent case is covered by `tests/test_confirm_concurrency.py`: 20 trials
  with two OS threads on separate connections plus 5 trials with two separate
  processes, each run, repeated 20 times for 500 races in total, every one
  producing exactly one POST. Removing the conditional guard from the claiming
  UPDATE was shown to make both tests fail with two POSTs, so the race window
  is genuinely being exercised rather than missed. This covers SQLite on a
  single machine only; it says nothing about a networked database, where the
  same claim would depend on that engine's isolation guarantees.
- **`GITHUB_API_BASE` is operator configuration.** It exists so the evaluation
  harness can point at a local stub and so a GitHub Enterprise host works.
  Nothing a caller or a model sends can set it, and unlike `fetch_url_text`
  the connector does not apply the SSRF guard, because its host is config
  rather than model input.

## Dependency note

`PyYAML==6.0.3` was added, since the policy and price files are YAML. No
existing pin was changed and the MCP SDK stays on `1.29.1`.

---

# Delivery metrics

Read-only delivery metrics for one public GitHub repository, computed in SQL
and answered by the agent with the pull request numbers or workflow run ids
behind every figure. Nothing in this layer writes to GitHub.

## What it is

| Piece | File | Role |
|---|---|---|
| Ingest | `delivery_ingest.py` | Pulls pull requests, reviews and workflow runs into the store. Idempotent, rate-limit aware. |
| Metrics | `delivery_metrics.py` | Five metrics computed in SQL, each recording the rows it used. |
| Tools | `delivery_tools.py` | Three agent tools. Every answer cites its rows; missing data is refused. |
| GraphQL | `delivery_graphql.py` | A read-only query endpoint over the same metrics. |
| Fixtures | `delivery_fixtures.py` | Records real API responses and replays them so tests need no network. |
| Questions | `evals/delivery_questions.yaml` | 20 hand-written questions scored against independently computed values. |

## The metrics

All five are deliberately simple. Each result states its own definition, so
nobody has to infer what was measured.

| Metric | Definition | Unit |
|---|---|---|
| `pr_cycle_time` | Hours from pull request creation to merge, over merged pull requests whose merge falls in the window. Unmerged ones are excluded. | hours |
| `time_to_first_review` | Hours from creation to the earliest submitted review, over pull requests created in the window that got at least one review. | hours |
| `review_count` | Submitted reviews per pull request, over pull requests created in the window. No reviews counts as zero. | reviews per PR |
| `workflow_failure_rate` | Share of completed runs created in the window whose conclusion is `failure`. Cancelled, skipped and in-progress runs are excluded from both sides of the ratio. | ratio |
| `merge_frequency` | Merged pull requests per calendar week, bucketed by the Monday of the merge week. Weeks with no merges do not appear. | merges per week |

The reported `value` is the **median** for the three distribution metrics;
`mean`, `min` and `max` come back alongside it.

Every result carries the identifiers it was computed from, and those same
identifiers are written to `dm_metric_computations`, so a number can be traced
back to its inputs after the fact.

## Running it

### The evaluation needs no ingest

The eval is self-contained. It builds a temporary database from the recorded
fixture in `tests/fixtures`, through the same ingest code path the tests use,
and deletes it on exit. It does not read or write the default database, and it
reaches no network.

```bash
python evals/run_delivery_eval.py            # the 20 questions, no model, no network
python -m pytest tests/test_delivery_eval.py -q
```

To score against a database you already ingested instead:

```bash
python evals/run_delivery_eval.py --db ./governance.db
```

That prints a warning, because the expected answers come from the recorded
fixture: against live data the questions will be reported as failures even
when the metrics are correct.

### Ingesting live data

Ingest is for querying a repository yourself, through the tools, the agent or
the GraphQL endpoint. It is not a prerequisite for the eval. It is read-only
and works without a token; set `GITHUB_TOKEN` to raise the rate limit.

```bash
# Optional, never printed or stored:
export GITHUB_TOKEN=...

python delivery_ingest.py psf/cachecontrol --max-prs 100 --max-runs 200
python delivery_ingest.py --list
```

Re-running is safe: rows are written with an UPSERT keyed on the GitHub id, so
a second pass updates in place. If the rate limit is hit the run stops with two
requests to spare, keeps what it fetched, marks the repository incomplete, and
the next run continues.

Serve the read-only GraphQL endpoint:

```bash
python delivery_graphql.py --port 8099

curl -s localhost:8099/graphql -H 'content-type: application/json' -d '{
  "query": "{ repos { name pullRequests } metric(repo: \"psf/cachecontrol\", metric: PR_CYCLE_TIME) { value unit sampleSize sourcePullRequests } }"
}'
```

The schema has a `Query` type and no `Mutation` or `Subscription`, and any
non-query operation is rejected before execution.

Re-record the test fixtures against the live API:

```bash
python delivery_fixtures.py psf/cachecontrol --max-prs 12 --max-runs 50
```

Run the questions through a real model (needs `OPENAI_API_KEY`; the agent
reaches the tools through the governed server, so the policy applies). This
also builds its own temporary database from the fixture, so no ingest is
needed first:

```bash
python evals/run_delivery_eval.py --mode real --model gpt-4o-mini --user bob
```

Note that the policy applies in this mode: `bob` may only query owners on the
reader allowlist, so a question about a repository outside it is denied by the
policy before the tool is reached. That is still a refusal, and the scorer
counts it as one.

## Citations and refusals

A metric answer names its sources:

```json
{
 "repo": "psf/cachecontrol",
 "metric": "pr_cycle_time",
 "definition": "Hours from pull request creation to merge, over merged pull requests whose merge falls in the window. Unmerged pull requests are excluded.",
 "unit": "hours",
 "sample_size": 7,
 "value": 1.5422,
 "mean": 197.178,
 "min": 0.6147,
 "max": 1349.1542,
 "statistic": "median",
 "citations": {"pull_requests": [466, 485, 486, 487, 488, 492, 493]},
 "refused": false
}
```

A repository that was never ingested is refused rather than answered:

```json
{
 "refused": true,
 "reason": "torvalds/linux has not been ingested, so there is no data to report",
 "ingested_repos": ["psf/cachecontrol"],
 "remedy": "run the ingest for this repository first"
}
```

A window with no matching rows is also refused, with `sample_size: 0` and a
null value, rather than reporting zero as though it had been measured.

## Measured results

From the recorded fixture (`psf/cachecontrol`: 12 pull requests, 8 reviews,
50 workflow runs):

```
pr_cycle_time          value=1.5422   unit=hours                          n=7
time_to_first_review   value=1.5411   unit=hours                          n=5
review_count           value=0.0      unit=reviews per pull request       n=12
merge_frequency        value=3.5      unit=merged pull requests per week  n=7
workflow_failure_rate  value=0.1667   unit=ratio                          n=48
```

Eval, run with `python evals/run_delivery_eval.py`:

```
mode=tools  questions=20
20/20 passed
```

All twenty pass. Each expected number is computed in
`tests/independent_metrics.py` from the recorded JSON using `datetime` and
`statistics`; that module does not import `delivery_metrics`, and a test
asserts as much on the import graph, so the SQL cannot agree with itself.

One question was revised after a real-model run. **q04** originally read
*"How many merged pull requests is the cycle time based on?"* — an ambiguous
question, original failure documented: it named no repository, and because
each question is answered in a fresh conversation there was nothing for the
model to resolve it against. It guessed a repository the policy then denied,
and the run recorded a refusal where a number was expected. The question now
names `psf/cachecontrol`. The original wording and the failure it caused are
kept here on purpose; the fix was to the question, not to the model or the
tools.

Test counts, measured on this branch's base and tip:

```
$ git worktree add --detach /tmp/before bec0558 && cd /tmp/before
$ python seed_books.py && python -m pytest -q -p no:cacheprovider
137 passed, 2 skipped in 19.56s

$ cd - && python -m pytest -q
206 passed, 2 skipped in 20.75s
```

69 new tests. Coverage of the new modules from `python -m pytest -q --cov`:

```
delivery_metrics.py       94      0   100%
delivery_tools.py         44      0   100%
delivery_ingest.py       143     34    76%
delivery_graphql.py       92     23    75%
delivery_fixtures.py     105     64    39%
TOTAL                   1466    233    84%
```

`delivery_fixtures.py` is low because its recording path talks to the live API
and is exercised by hand, not by the suite.

## Real model results

Numbers below are from runs against `gpt-4o-mini`. The deterministic eval
(`--mode tools`) scores 20/20 and is a separate thing: it checks the metrics
and citations, not the model.

| Run | Scorer | Tool output | Score | Failing |
|---|---|---|---|---|
| Run 1, as recorded | original | before | 12 / 20 | not recorded |
| Run 2, as recorded | original | before | 13 / 20 | q01, q04, q05, q07, q12, q14, q19 |
| Run 2, re-scored | fixed | before | 13 / 20 | q01, q04, q05, q07, q12, q14, q19 |
| Run 3, after the tool change | fixed | after | 17 / 20 | q12, q14, q19 |
| Run 4, after the tool change | fixed | after | 17 / 20 | q12, q14, q19 |
| Run 5, after the tool change | fixed | after | 17 / 20 | q12, q14, q19 |

Run 2's answers are saved in
`evals/results/delivery-real-run2-before-fixes.json`. Re-derive the
re-scored figure with:

```bash
python evals/run_delivery_eval.py --rescore evals/results/delivery-real-run2-before-fixes.json
```

Runs 3 to 5 are three separate runs at commit `b1ac32f`, after both the
scorer fixes and the tool change, saved as
`evals/results/delivery-real-b1ac32f-run1.json`, `run2.json` and `run3.json`.
All three scored 17/20 with the same three questions failing. There are no
source changes between `b1ac32f` and the current tip, so they describe the
code as it stands.

**The before and after figures are separate and are not combined.** Run 1 and
run 2 were scored before the tool change, run 2's re-score only changes the
scorer, and runs 3 to 5 are the only ones with the `summary` field and
`per_pull_request` removed. **The re-scored figure is not the original one** —
run 2 as recorded scored 13/20 under the original scorer, and re-scoring it
under the fixed scorer also gives 13/20.

Scope of every figure here: SQLite, one repository (`psf/cachecontrol`), 12
pull requests, 8 reviews and 50 workflow runs — a small sample, and 20
questions asked once per run.

### What the scorer fixes actually changed

Nothing, on run 2. Re-scoring moved no question: 13/20 before and after.
Of ten individual failure reasons, one was resolved — q12's answer ended
"is 3.5.", and the original rules treated the full stop as part of the number.
q12 still fails because the answer never states the total. Every other failure
had a cause the rules were right to flag.

The scorer defects were real and are fixed with tests, but on this run they
were not what cost the points.

### Which failures were what

**Fixed by the tool change** — these failed in run 2 and pass in all of runs
3 to 5:

- **q07** asked for the median reviews per pull request. The metric computed
  `0.0`; the run 2 answer said `0.5`, which is the median of twelve values if
  you count six zeros instead of seven — the model had recomputed the
  statistic from `per_pull_request` instead of quoting the one it was given.
  With that key no longer sent and a `summary` sentence in its place, all
  three later runs answer `0.0` and cite the sample of 12.
- **q01 and q05** stated the figure correctly in run 2 but never stated the
  sample size the question's rules also require. The `summary` sentence
  carries the sample size, and both pass in runs 3 to 5.

**Model behaviour** — still failing in all of runs 3 to 5:

- **q12** asks how many pull requests are merged per week. The answer, in all
  three runs, is "On average, 3.5 pull requests are merged per week in the
  `psf/cachecontrol` repository." The value check passes: 3.5 is correct and
  is stated. It fails on the second figure the question requires, the total of
  7 merges, which the answer omits. That is the same class of omission as q01
  and q05 in run 2 — a figure the rules ask for and the answer leaves out —
  and it is model behaviour, not a scorer or question defect.
- **q14** asks how many merges fell in the first week that had any, which
  needs the week discovered and then used as a window. All three runs answer
  "There were no merges in the first week of January 2023" — a window the
  model invented, for a year with no data. Unchanged, and no tool affordance
  was added for it.
- **q19** asks for "team velocity", which is not a metric the server has. All
  three runs report `3.5 merged pull requests per week` as a velocity,
  presenting the merge-frequency figure under a name the server never
  offered. That is fabrication rather than a refusal, and broadening the
  refusal vocabulary does not and should not rescue it. Unchanged.

**Scorer defect** — the answer was acceptable and the rules were wrong:

- **q12** on its "value" check only, in run 2, where the answer ended
  "is 3.5." and the original rules read the full stop as part of the number.
  The rest of q12's failure, then and now, is not a scorer defect.

**Question defect** — the question could not be answered as written:

- **q04** named no repository. See the note in the evaluation results section
  above; the wording is now fixed and the original is recorded there. It
  passes in runs 3 to 5.

q14 and q19 are left exactly as they were, and no field was added to make
them easier. They are recorded here as genuine model failures, reproduced
identically three times.

## Limits

- **One public repository at a time.** The repository is configurable, but the
  ingest, the metrics and the tools all work on whatever has been ingested, and
  nothing here handles private repositories or an organisation-wide roll-up.
- **Read-only throughout.** The ingest only issues GETs, the tools only read
  the local store, and the GraphQL schema has no mutation to reach.
- **The metric definitions are simple on purpose** and are not any particular
  industry definition. Cycle time starts at pull request creation, not at first
  commit; time to first review ignores review requests and draft status; the
  failure rate counts runs, not jobs or steps, and treats a cancelled run as
  neither a success nor a failure. If you need different definitions, the SQL
  is in one file.
- **Small samples, so read the sample size.** The committed fixture is 12 pull
  requests and 50 runs. In that data the cycle time median is 1.54 hours while
  the mean is 197 hours, because one pull request took 1349 hours. A median
  over seven rows is a description of seven rows, nothing more.
- **Not a complete history.** Pull requests are fetched most-recently-updated
  first and workflow runs most-recent first, both capped. The numbers describe
  what was ingested, which is why every answer carries its sample size and its
  source identifiers.
- **No claims about customers, teams or scale.** This has been run against one
  public repository's recorded data and nothing else. There is no benchmark
  here, no comparison between projects, and nothing about how it behaves on a
  large or busy repository.
- **SQLite, not Postgres.** The brief allowed either. There is no Postgres
  server or driver configured in this project, so the delivery tables live in
  the same SQLite file as the audit log and usage meter. The schema is plain
  SQL with no SQLite-specific types; the date arithmetic (`julianday`,
  `strftime`) is the part that would need rewriting to move.
- **Reviews are counted as submitted**, including `COMMENTED` ones, so
  `review_count` is review events rather than approvals.

---

# LLM gateway

An optional HTTP service that accepts OpenAI-shaped `/v1/chat/completions` and
`/v1/responses` requests, translates them to OpenAI or Anthropic, keeps session
state so `previous_response_id` works against both, and enforces a monthly
budget per API key using the metering already in this repository.

**Off by default.** Nothing in the agent, the MCP servers or the delivery
metrics changes unless the gateway is started. See
[docs/gateway-design.md](docs/gateway-design.md) for what it reuses and the
conflicts found while building it.

## Running it

```bash
cp gateway.example.yaml gateway.yaml      # then edit; gateway.yaml is gitignored
export OPENAI_API_KEY=...                 # only for the providers you route to
export ANTHROPIC_API_KEY=...
uvicorn gateway.app:create_app --factory --host 127.0.0.1 --port 8080
```

```bash
curl -s localhost:8080/healthz

curl -s localhost:8080/v1/chat/completions \
  -H 'Authorization: Bearer <a key from gateway.yaml>' \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'
```

## What it supports

| | |
|---|---|
| Endpoints | `POST /v1/chat/completions`, `POST /v1/responses` |
| Unauthenticated | `GET /healthz` returning exactly `{"status": "ok"}` and nothing about how the instance is configured, `GET /` listing the supported paths, and `GET /favicon.ico` returning 204, so a browser or a probe does not get a 404 |
| Providers | OpenAI and Anthropic, plus an in-process fake for tests and the benchmark |
| Request fields | `model`, `messages` / `input`, `instructions`, `previous_response_id`, `temperature`, `max_tokens` / `max_output_tokens`, `stream` |
| Content | Text only. A string, or a list of content parts whose text is concatenated in order |
| Streaming | SSE on both endpoints, text deltas, each in its own event format |
| Sessions | SQLite by default, Redis optional, with a TTL |
| Budgets | A monthly estimate per key; over it is `429` with code `budget_exceeded` |
| Routing | By model prefix, from config; the first matching prefix wins |

Usage is recorded through the repository's existing meter, so gateway traffic
shows up in `python govctl.py usage --group user` under the key's `id`.

### What it does not support

Tools and function calling, images and any other non-text content, batch
endpoints, embeddings, prompt caching, context compaction, `n > 1`, logprobs,
structured-output `response_format`, and Anthropic's extended thinking. These
are **refused with a 400**, not silently dropped, so a caller never gets an
answer that looks as though the field had been honoured.

Only what the tests cover is claimed. The providers are exercised against
recorded wire bodies through a mock transport, not against the live APIs, so
"compatible with OpenAI" here means "matches the request and response shapes
these tests assert".

## The Anthropic mapping

Four rules, each a pure function in `gateway/translate.py` with its own tests:

- `system` messages and `/v1/responses` `instructions` are hoisted to
  Anthropic's top-level `system` field. Instructions come first, then system
  turns in order, joined by a blank line.
- Consecutive turns with the same role are merged, because Anthropic requires
  user and assistant to alternate. Their text is joined by a blank line.
- `max_tokens` is required by Anthropic, so a request that omits it gets
  `defaults.max_tokens` from config.
- `stop_reason` maps back to `finish_reason`: `end_turn` to `stop`,
  `max_tokens` to `length`. An unrecognised value falls back to `stop` rather
  than inventing one.

## Reasoning models

On the chat completions endpoint, OpenAI's reasoning families take
`max_completion_tokens` where other models take `max_tokens`. The gateway
sends whichever the model expects, so a caller always supplies `max_tokens`
(or `max_output_tokens` on `/v1/responses`) and does not have to know which
family it is talking to.

| Model name starts with | Upstream field |
|---|---|
| `gpt-5`, `o1`, `o3`, `o4` | `max_completion_tokens` |
| anything else | `max_tokens` |

A `/v1/responses` request's `max_output_tokens` goes through the same mapping,
because this gateway's OpenAI provider calls chat completions underneath. When
the caller supplies no limit, neither field is sent.

**`temperature` is passed through unchanged.** Some reasoning models accept
only the default temperature and will refuse any other value; the gateway does
not rewrite or drop what the caller asked for, so such a request fails
upstream and the error surfaces as a 502. The live run did not exercise this:
no request in it set a `temperature`.

The offline tests assert only which field name the gateway puts on the wire
for each family. One live call on 2026-11-10 confirmed that `gpt-5.4-mini`
accepts the renamed field: a request carrying `max_tokens: 64` came back as a
completion rather than an upstream error. The `o1`, `o3` and `o4` families
have never been called live, so for those the mapping remains an assertion
about the request body and nothing more. The `o4` prefix was added to
`gateway.example.yaml` so those models are routable at all.

## Sessions

`/v1/responses` stores the conversation under the response id it returns, so
`previous_response_id` continues it — including on Anthropic, which has no
server-side conversation of its own. An unknown or expired id is a `404` with
code `response_not_found`.

Backends are behind one interface. SQLite is the default and shares the
database the audit log and meter use. Redis is optional, for running more than
one gateway process; it uses Redis's own TTL.

## Budgets and cost

`monthly_budget_usd` per key. Spend is **not** counted twice: the gateway
writes one `usage_llm` row per upstream call through the existing
`metering.record_llm_turn`, and month-to-date spend is read back out of those
rows. There is one source of truth.

**Every cost figure is an estimate.** It is the recorded token counts
multiplied by whatever is in the configured price table, and the committed
`prices.example.yaml` is all zeros on purpose, so a budget built on it can
never be exceeded. Fill in `prices.yaml` from your provider's current pricing
before relying on a budget.

Two limits worth knowing:

- A cost is not knowable before a call, so the check is against spend already
  recorded. A key can therefore overshoot its budget by **at most one
  request**.
- When an upstream reports no usage for a streamed response, the gateway
  records a zero row and logs that it did, rather than inventing numbers. That
  under-counts spend for that request.

## Streaming formats

Each endpoint streams in its own shape, which the tests assert are distinct.

`/v1/chat/completions` emits unnamed `data:` frames of `chat.completion.chunk`
objects, then `data: [DONE]`. The `role` is sent once, in the first chunk; the
chunks after it carry `content` only, and the last carries `finish_reason`.
No chunk carries a `usage` object: `stream_options.include_usage` is not
implemented, so a streaming chat caller cannot read token counts off the
stream. The tokens are still metered — they are recorded once the stream
finishes — so `govctl.py usage` reports them.

`/v1/responses` emits these named events, in this order:

| # | Event |
|---|---|
| 1 | `response.created` |
| 2 | `response.output_item.added` |
| 3 | `response.content_part.added` |
| 4 | `response.output_text.delta` (one per delta, repeated) |
| 5 | `response.output_text.done` |
| 6 | `response.content_part.done` |
| 7 | `response.output_item.done` |
| 8 | `response.completed` |

`response.created` carries `output: []`; the message item first appears in
`response.output_item.added`. Every event carries a `sequence_number`,
consecutive from 0, and one stable message item id appears in every event
after `response.created` — as `item_id`, as `item.id`, and in
`response.completed` as `response.output[0].id`.
`sequence_number` is not optional: the installed `openai` package's
stream-event models declare it as a required field, and
`tests/test_gateway_responses_stream.py` parses every event this gateway emits
through those models offline, so the events are checked against the SDK rather
than assumed to match it. `logprobs` is sent as an empty list on the delta and
done events for the same reason; this gateway does not support logprobs.

Both the non-streaming `/v1/responses` body and the response object embedded
in the stream events carry the fields the SDK's `Response` model requires:
`parallel_tool_calls` (always `false`), `tool_choice` (always `"none"`),
`tools` (always `[]`) — the gateway has no tools — and the two usage detail
objects. Tests validate both bodies against that model offline.

**These usage details are always zero: `input_tokens_details.cached_tokens`,
`input_tokens_details.cache_write_tokens` and
`output_tokens_details.reasoning_tokens`.** Zero here means the gateway does
not track the value, not that it measured none. It neither counts prompt
caching or reasoning tokens itself nor reads them back from an upstream.

## Benchmark

`scripts/bench_gateway.py` measures the gateway's own overhead against a fake
provider with a fixed delay, so the difference is request parsing, routing, the
budget read, the provider call, the session write and the response build.

```bash
python scripts/bench_gateway.py --requests 200 --delay-ms 5 --endpoint chat
```

Measured on one machine over loopback, 200 requests after 20 warmup, with a
5 ms fake provider delay:

```
endpoint            /v1/chat/completions
round trip P50      9.802 ms
round trip P95      11.824 ms
gateway added P50   4.802 ms
gateway added P95   6.824 ms

endpoint            /v1/responses
round trip P50      9.619 ms
round trip P95      10.065 ms
gateway added P50   4.619 ms
gateway added P95   5.065 ms
```

**This is one machine on loopback.** It is not a provider latency
measurement, not a throughput or concurrency measurement, and says nothing
about behaviour under load. Each request includes a SQLite budget read and a
usage write, and `/v1/responses` also writes a session row. Re-run the script
rather than trusting these numbers on different hardware.

## Tests

Every gateway test uses the in-process fake provider or a mock transport, so
none reaches a provider or the network.

```bash
pytest -q tests/test_gateway_translate.py tests/test_gateway_endpoints.py \
         tests/test_gateway_providers.py tests/test_gateway_sessions.py \
         tests/test_gateway_budgets.py tests/test_gateway_config.py \
         tests/test_gateway_bench.py
```

The Redis session tests need a reachable Redis and skip themselves without
one. CI runs them in a second job against a `redis:8-alpine` service
container, where a skip is treated as a failure so a broken container cannot
pass quietly. The existing CI job's steps are unchanged apart from adding the
new modules to the type check and the new config file to the parse check.

## Live verification

The gateway was run once against the real OpenAI and Anthropic APIs on
2026-11-10, from a throwaway config and a throwaway SQLite database outside
the repository: 17 upstream calls, each capped at 64 output tokens. The models
called were `gpt-4o-mini`, `gpt-5.4-mini` and `claude-haiku-4-5-20251001`.

| Run live | Result |
|---|---|
| `/v1/chat/completions`, non-streaming | passed on all three models |
| `max_tokens` on `gpt-5.4-mini` | passed; the `max_completion_tokens` rename is accepted upstream |
| `/v1/responses`, non-streaming, `instructions` set | passed on `gpt-4o-mini` and `claude-haiku-4-5-20251001`, and both obeyed the instruction |
| Streaming, both endpoints, both providers | passed; frames arrived spread across the response rather than in one burst, one stable item id per `/v1/responses` stream, clean termination, nonzero final usage on `/v1/responses` |
| `previous_response_id` chaining | passed on both providers; the second turn recalled a code word set in the first, and its input token count rose, so the history really did go upstream |
| The `openai` 3.6.0 SDK client, pointed at the gateway | passed; `client.responses.stream` and `client.responses.create` with a chain both ran against `claude-haiku-4-5-20251001` with no validation error |
| Error shapes | passed; 404 `response_not_found`, 401 `missing_api_key`, 400 `model_not_found`, 429 `budget_exceeded` |
| Usage accounting | passed; per-key rows with nonzero token counts for every call, the streaming ones included |

That run says nothing about the following, which remain covered by offline
tests or not at all:

* The `o1`, `o3` and `o4` reasoning families, and the temperature behaviour
  described under [Reasoning models](#reasoning-models).
* Anything beyond text. No tools, no images, no structured output.
* Any client other than `openai` 3.6.0.
* Concurrency or load. Every call in the run was sequential.
* Redis-backed sessions. The run used the SQLite backend.
