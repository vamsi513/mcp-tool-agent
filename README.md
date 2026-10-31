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
  needed. That is deliberate: double-posting is worse than retrying.
- **`GITHUB_API_BASE` is operator configuration.** It exists so the evaluation
  harness can point at a local stub and so a GitHub Enterprise host works.
  Nothing a caller or a model sends can set it, and unlike `fetch_url_text`
  the connector does not apply the SSRF guard, because its host is config
  rather than model input.

## Dependency note

`PyYAML==6.0.3` was added, since the policy and price files are YAML. No
existing pin was changed and the MCP SDK stays on `1.29.1`.
