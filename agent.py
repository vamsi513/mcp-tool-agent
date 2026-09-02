"""A small agent that answers a question by letting an LLM choose among the
tools exposed by server.py, calling them over the MCP protocol.

Usage:

    python agent.py "which fantasy books in the database are rated above 4.3?"
    python agent.py --model gpt-4o --max-turns 8 "..."

The agent spawns server.py as a subprocess and talks to it over stdio using
the MCP client. Tool calls always go through the protocol (tools/call), never
by importing the tool functions directly.
"""

import argparse
import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

load_dotenv()

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "5"))
TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0"))
RETRY_ATTEMPTS = 3

SYSTEM_PROMPT = (
    "You answer the user's question. You have a set of tools available. "
    "Use a tool when it helps answer the question, then give a short, direct "
    "answer based on the tool results. If no tool is relevant, just answer."
)


def log(step: str, payload=None):
    line = f"[agent] {step}"
    if payload is not None:
        line += f": {json.dumps(payload, default=str)[:600]}"
    print(line, file=sys.stderr)


def mcp_tools_to_openai(tools):
    """Convert MCP tool definitions into the OpenAI tool-calling format."""
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
        )
    return converted


def tool_result_text(result) -> str:
    """Flatten an MCP CallToolResult into a string for the LLM."""
    parts = []
    for block in result.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else "(no content)"


def complete_with_retry(client: OpenAI, **kwargs):
    """Call chat.completions.create, retrying transient errors with backoff."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (APIConnectionError, RateLimitError, APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            retryable = isinstance(exc, (APIConnectionError, RateLimitError)) or status in (500, 502, 503, 504)
            if not retryable or attempt == RETRY_ATTEMPTS:
                raise
            wait = 2 ** (attempt - 1)
            log(f"LLM call failed ({exc.__class__.__name__}), retrying in {wait}s", {"attempt": attempt})
            time.sleep(wait)


async def call_tool_safely(session: ClientSession, name: str, arguments_json: str) -> str:
    """Run one tool call, turning bad arguments or protocol errors into text
    the model can read and react to rather than a crash."""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return f"error: could not parse tool arguments as JSON ({exc})"
    if not isinstance(args, dict):
        return "error: tool arguments must be a JSON object"

    log("model chose tool", {"name": name, "args": args})
    try:
        result = await session.call_tool(name, args)
    except McpError as exc:
        return f"error: MCP call failed ({exc})"
    text = tool_result_text(result)
    log("tools/call result", text)
    return text


async def run(question: str, model: str, max_turns: int) -> str:
    server = StdioServerParameters(command=sys.executable, args=["server.py"])

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            log("connected to MCP server", {"server": init.serverInfo.name,
                                            "protocol": init.protocolVersion})

            listed = await session.list_tools()
            log("tools/list returned", [t.name for t in listed.tools])

            client = OpenAI()
            openai_tools = mcp_tools_to_openai(listed.tools)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]

            for turn in range(1, max_turns + 1):
                # On the last turn, drop the tools so the model has to answer
                # from what it already has instead of stalling on another call.
                use_tools = turn < max_turns
                completion = complete_with_retry(
                    client,
                    model=model,
                    messages=messages,
                    temperature=TEMPERATURE,
                    tools=openai_tools if use_tools else None,
                    tool_choice="auto" if use_tools else None,
                )
                if completion.usage:
                    log(f"turn {turn}: token usage", {
                        "prompt": completion.usage.prompt_tokens,
                        "completion": completion.usage.completion_tokens,
                    })

                choice = completion.choices[0].message
                if not choice.tool_calls:
                    log(f"turn {turn}: model returned a final answer")
                    return choice.content or "(empty answer)"

                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in choice.tool_calls
                        ],
                    }
                )

                for tc in choice.tool_calls:
                    text = await call_tool_safely(session, tc.function.name, tc.function.arguments)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": text}
                    )

            return "(stopped: reached max turns without a final answer)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="the question to answer")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                        help=f"max LLM turns before giving up (default: {DEFAULT_MAX_TURNS})")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set (copy .env.example to .env and fill it in)")
    if args.max_turns < 1:
        sys.exit("--max-turns must be at least 1")

    answer = asyncio.run(run(args.question, args.model, args.max_turns))
    print("\n" + answer)


if __name__ == "__main__":
    main()
