"""A small agent that answers a question by letting an LLM choose among the
tools exposed by server.py, calling them over the MCP protocol.

Usage:

    python agent.py "which fantasy books in the database are rated above 4.3?"

The agent spawns server.py as a subprocess and talks to it over stdio using
the MCP client. Tool calls always go through the protocol (tools/call), never
by importing the tool functions directly.
"""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

load_dotenv()

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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


async def run(question: str, max_turns: int = 5) -> str:
    server = StdioServerParameters(command=sys.executable, args=["server.py"])

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            log("connected to MCP server", {"server": init.serverInfo.name,
                                            "protocol": init.protocolVersion})

            listed = await session.list_tools()
            tool_names = [t.name for t in listed.tools]
            log("tools/list returned", tool_names)

            client = OpenAI()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]
            openai_tools = mcp_tools_to_openai(listed.tools)

            for turn in range(1, max_turns + 1):
                completion = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                )
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
                    args = json.loads(tc.function.arguments or "{}")
                    log(f"turn {turn}: model chose tool", {"name": tc.function.name, "args": args})

                    result = await session.call_tool(tc.function.name, args)
                    text = tool_result_text(result)
                    log(f"turn {turn}: tools/call result", text)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": text,
                        }
                    )

            return "(stopped: reached max turns without a final answer)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="the question to answer")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set (copy .env.example to .env and fill it in)")

    answer = asyncio.run(run(args.question))
    print("\n" + answer)


if __name__ == "__main__":
    main()
