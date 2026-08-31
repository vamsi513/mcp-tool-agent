# mcp-tool-agent

A minimal end-to-end demonstration of the Model Context Protocol (MCP): one
MCP server exposing three real tools, and a command-line agent that lets an
LLM decide which tool to call and invokes it over the protocol.

Tool calls always travel through MCP (`tools/list` + `tools/call` over a
stdio JSON-RPC connection). The agent never imports the tool functions
directly.

## The server

`server.py` is a `FastMCP` server named `tool-agent-demo` that speaks over
stdio. It exposes:

| Tool | Description | Inputs |
|---|---|---|
| `search_github_repos` | Lists a GitHub user's public repos, filtered by a substring match on name/description. Calls the live GitHub REST API. | `username` (str), `query` (str, optional), `limit` (int 1–20, default 5) |
| `fetch_url_text` | Fetches a web page, strips scripts/styles/nav/header/footer, returns the visible text plus the page title. | `url` (http/https), `max_chars` (int 500–20000, default 4000) |
| `query_books` | Queries a local SQLite database of 20 books. Filters are AND-combined; results ordered by rating descending. | `author` (substring), `genre` (substring), `min_year` (int), `min_rating` (float), `limit` (int 1–20, default 10) |

Each tool's input schema is generated from its typed signature and returned
in `tools/list`.

## The agent

`agent.py` takes a natural-language question and:

1. Spawns `server.py` and connects over stdio with the MCP client.
2. Runs the `initialize` handshake, then `tools/list`.
3. Converts the MCP tool definitions into the LLM's tool-calling format.
4. Asks the LLM (OpenAI, `gpt-4o-mini` by default) to answer, with the tools available.
5. For each tool call the model returns, executes it via `tools/call` and feeds the result back.
6. Prints the model's final answer once it stops calling tools (max 5 turns).

Every protocol step is logged to stderr with an `[agent]` prefix.

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
- `GITHUB_TOKEN` – optional, raises the GitHub API rate limit.

## Running

Run the agent with a question:

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

## Notes

- Pinned to `mcp==1.29.1` (the 1.x `FastMCP` API). The 2.x release renames
  the server class and changes several APIs.
- No secrets are committed; `.env` and `books.db` are gitignored.
