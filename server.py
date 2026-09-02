"""MCP server exposing three tools: GitHub repo search, URL text extraction,
and a query over a small local book database.

Run directly for stdio transport:

    python server.py
"""

import ipaddress
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

DB_PATH = Path(__file__).parent / "books.db"
GITHUB_API = "https://api.github.com"
USER_AGENT = "mcp-tool-agent/0.1"
MAX_REDIRECTS = 5
MAX_GITHUB_PAGES = 10
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
READABLE_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)

mcp = FastMCP("tool-agent-demo")


def _reject_reason(url: str) -> str | None:
    """Return a reason string if `url` is not safe to fetch, else None.

    Blocks non-http(s) schemes and any host that resolves to a loopback,
    private, link-local, or otherwise non-public address. This is what keeps
    an LLM (or an instruction hidden in a fetched page) from pointing the
    tool at cloud metadata endpoints or services on the internal network.

    Note: the host is re-resolved by the HTTP client when it connects, so a
    hostile DNS server could still rebind between this check and the request.
    Pinning the connection to the validated address would close that gap.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "url must start with http:// or https://"
    if not parsed.hostname:
        return "url has no host"

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, parsed.port or 0)
    except socket.gaierror as exc:
        return f"could not resolve host: {exc}"

    for *_, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"host resolves to a non-public address ({ip})"
    return None


@mcp.tool()
def search_github_repos(username: str, query: str = "", limit: int = 5) -> dict:
    """Search a GitHub user's public repositories.

    Returns repos owned by `username` whose name or description contains
    `query` (case-insensitive). If `query` is empty, returns the user's
    most recently updated repos. `limit` caps the number of results (1-20).

    Repos are read in pages of 100, following the API's Link header up to
    MAX_GITHUB_PAGES pages. Set GITHUB_TOKEN to raise the rate limit; without
    it, users with many repos may exhaust the unauthenticated quota.
    """
    limit = max(1, min(limit, 20))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos = []
    next_url = f"{GITHUB_API}/users/{username}/repos"
    params = {"per_page": 100, "sort": "updated", "type": "owner"}
    with httpx.Client(timeout=15) as client:
        for _ in range(MAX_GITHUB_PAGES):
            resp = client.get(next_url, headers=headers, params=params)
            if resp.status_code == 404:
                return {"error": f"GitHub user '{username}' not found", "results": []}
            if resp.status_code != 200:
                return {
                    "error": f"GitHub API returned {resp.status_code}: {resp.text[:200]}",
                    "results": [],
                }
            repos.extend(resp.json())
            if "next" not in resp.links:
                break
            next_url = resp.links["next"]["url"]
            params = None  # the next link already carries the query string

    needle = query.strip().lower()
    matches = []
    for repo in repos:
        name = repo.get("name") or ""
        description = repo.get("description") or ""
        if needle and needle not in name.lower() and needle not in description.lower():
            continue
        matches.append(
            {
                "name": repo.get("full_name"),
                "description": description,
                "stars": repo.get("stargazers_count", 0),
                "language": repo.get("language"),
                "url": repo.get("html_url"),
                "updated_at": repo.get("updated_at"),
            }
        )

    top = matches[:limit]
    return {"count": len(top), "results": top}


@mcp.tool()
def fetch_url_text(url: str, max_chars: int = 4000) -> dict:
    """Fetch a web page and return its visible text with HTML stripped.

    Useful when the caller needs the content of a specific page to read or
    summarize. `max_chars` truncates the returned text (500-20000).
    """
    max_chars = max(500, min(max_chars, 20000))

    headers = {"User-Agent": USER_AGENT}
    current = url
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                reason = _reject_reason(current)
                if reason:
                    return {"error": reason, "text": ""}

                with client.stream("GET", current, headers=headers) as resp:
                    if resp.is_redirect and resp.has_redirect_location:
                        current = str(resp.next_request.url)
                        continue
                    if resp.status_code != 200:
                        return {"error": f"HTTP {resp.status_code}", "text": ""}

                    raw_type = resp.headers.get("content-type", "")
                    content_type = raw_type.split(";")[0].strip().lower()
                    if content_type and content_type not in READABLE_CONTENT_TYPES:
                        return {"error": f"unsupported content type: {content_type}", "text": ""}

                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            return {"error": "response too large", "text": ""}
                    body = b"".join(chunks)
                    final_url = str(resp.url)
                    encoding = resp.charset_encoding or "utf-8"
                break
            else:
                return {"error": "too many redirects", "text": ""}
    except httpx.RequestError as exc:
        return {"error": f"request failed: {exc}", "text": ""}

    soup = BeautifulSoup(body.decode(encoding, errors="replace"), "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    text = " ".join(soup.get_text(separator=" ").split())
    truncated = len(text) > max_chars

    return {
        "url": final_url,
        "title": title,
        "truncated": truncated,
        "text": text[:max_chars],
    }


def _like_contains(value: str) -> str:
    """Lowercase `value` and escape LIKE metacharacters so it matches literally."""
    lowered = value.lower()
    for ch in ("\\", "%", "_"):
        lowered = lowered.replace(ch, "\\" + ch)
    return lowered


@mcp.tool()
def query_books(
    author: str = "",
    genre: str = "",
    min_year: int | None = None,
    min_rating: float | None = None,
    limit: int = 10,
) -> dict:
    """Query the local book database.

    Filters are combined with AND. `author` and `genre` match on substring
    (case-insensitive); `min_year` and `min_rating` are lower bounds. Results
    are ordered by rating descending. `limit` caps results (1-20).
    """
    if not DB_PATH.exists():
        return {"error": "books.db not found; run seed_books.py first", "results": []}

    limit = max(1, min(limit, 20))
    clauses = []
    args: list = []
    if author:
        clauses.append(r"LOWER(author) LIKE ? ESCAPE '\'")
        args.append(f"%{_like_contains(author)}%")
    if genre:
        clauses.append(r"LOWER(genre) LIKE ? ESCAPE '\'")
        args.append(f"%{_like_contains(genre)}%")
    if min_year is not None:
        clauses.append("year >= ?")
        args.append(min_year)
    if min_rating is not None:
        clauses.append("rating >= ?")
        args.append(min_rating)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT title, author, year, genre, rating FROM books {where} "
        "ORDER BY rating DESC LIMIT ?"
    )
    args.append(limit)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()

    results = [dict(row) for row in rows]
    return {"count": len(results), "results": results}


if __name__ == "__main__":
    mcp.run()
