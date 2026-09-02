"""End-to-end check that the server answers real MCP requests over stdio."""

import json
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.asyncio


async def _session():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    return stdio_client(params)


async def test_list_tools_exposes_all_three():
    async with await _session() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}

    assert set(tools) == {"search_github_repos", "fetch_url_text", "query_books"}
    assert tools["query_books"].inputSchema["properties"]["min_rating"]["default"] is None


async def test_call_tool_query_books_round_trip():
    async with await _session() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "query_books", {"genre": "fantasy", "min_rating": 4.4}
            )

    payload = json.loads(result.content[0].text)
    assert payload["count"] == 4
    assert payload["results"][0]["rating"] >= 4.4


async def test_schema_validation_error_is_flagged():
    async with await _session() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("query_books", {"min_rating": "high"})

    assert result.isError
