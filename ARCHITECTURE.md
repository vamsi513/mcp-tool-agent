# Architecture

## Goal

Show the Model Context Protocol working end to end with the smallest amount of
code that is still honest about it: a real server, real tools, a real client,
and an LLM that decides what to call. Nothing here fakes the protocol or
shortcuts around it.

## Shape

```
  agent.py  ──spawns──▶  server.py
     │                      │
     │   stdio (JSON-RPC)   │
     │◀────────────────────▶│
     │                      ├─ search_github_repos  ──▶ api.github.com
     │                      ├─ fetch_url_text        ──▶ arbitrary http(s)
     │                      └─ query_books           ──▶ books.db (SQLite)
     │
     └──▶ OpenAI chat.completions (tool calling)
```

`agent.py` is an MCP client and an LLM driver. `server.py` is an MCP server.
They only ever talk over the protocol.

## Choices and why

**stdio transport.** The server is launched as a subprocess and speaks
JSON-RPC over stdin/stdout. This is the transport every MCP host (Claude
Desktop, IDE integrations) uses for local servers, it needs no ports or auth,
and it keeps the demo to two files you can run directly. An HTTP/SSE transport
would matter for a remotely hosted server; it is out of scope here.

**FastMCP for the server.** The `@mcp.tool()` decorator generates each tool's
JSON Schema from the function signature and type hints, and the docstring
becomes the tool description the model sees. That keeps the schema and the
implementation in one place and impossible to drift apart. Tool selection
quality depends heavily on those descriptions, so they spell out parameter
ranges and semantics rather than just naming the arguments.

**Pinned to `mcp==1.29.x`.** The 2.x line renamed the server class
(`FastMCP` → `MCPServer`) and changed client APIs shortly before this was
written, with little documentation. 1.x is stable and widely used. The pin is
a deliberate call, noted so it can be revisited.

**Tools return data, not exceptions.** Domain failures (unknown GitHub user,
unreachable URL, missing database) come back as ordinary tool results with an
`error` field. The model reads the error and explains it. Schema violations
are different: FastMCP rejects them before the tool body runs and the protocol
marks the result `isError`. The agent surfaces both without crashing.

**The agent forces an answer on its last turn.** Tools are offered on every
turn except the final one; on that turn they are withheld so the model has to
answer from what it already gathered instead of looping until `max_turns` and
returning nothing.

**OpenAI as the LLM.** One provider, wired through the standard tool-calling
API. `mcp_tools_to_openai` is the only provider-specific glue; swapping
providers means replacing that function and the client call.

## Security model

The server runs tools chosen by an LLM, and one of them fetches arbitrary
URLs. That is the main risk surface.

- **`fetch_url_text` blocks non-public destinations.** Before each request
  (including every redirect hop) the host is resolved and rejected if it maps
  to a loopback, private, link-local, reserved, multicast, or unspecified
  address. This is what stops the tool being pointed at cloud metadata
  endpoints (`169.254.169.254`) or internal services, whether the bad URL
  comes from the model or from an instruction hidden inside a fetched page.
  Residual gap: the HTTP client re-resolves the host when it connects, so a
  hostile DNS server could rebind between the check and the request. Pinning
  the connection to the validated IP would close it.
- **Response size is capped** at 5 MB and read incrementally, so a huge or
  endless response cannot exhaust memory.
- **Only text-like content types** are parsed; anything else is refused.
- **SQL is fully parameterized** and `LIKE` metacharacters in user input are
  escaped, so `query_books` filters cannot be turned into injection or
  unintended wildcards. The database is read-only in practice (no tool writes
  to it).
- **All tools clamp `limit`** and other bounds so the model cannot request
  unbounded work.
- **Secrets** (`OPENAI_API_KEY`, optional `GITHUB_TOKEN`) come from the
  environment / `.env` and are never logged or committed.

## Testing

`tests/` covers the tool logic with mocked HTTP (`httpx.MockTransport`), the
SSRF guard including the redirect-hop case, and a real stdio round-trip that
spawns the server and exercises `initialize` / `tools/list` / `tools/call`.
`TEST_RUN.md` is captured output from running the agent against a live model,
kept as evidence rather than as an automated check.

## What is deliberately not here

Auth, a second transport, resources/prompts (only tools), streaming answers,
conversation memory across runs, and multi-provider support. Each would be
straightforward to add; none is needed to demonstrate the protocol.
