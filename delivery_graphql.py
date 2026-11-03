"""A small read-only GraphQL endpoint over the delivery metrics.

The schema has a Query type and nothing else, so there is no mutation to
reach. Requests that carry a mutation or subscription operation are rejected
before execution, and the server only ever reads from the database.

Run it with:

    python delivery_graphql.py --port 8099

Then POST to http://127.0.0.1:8099/graphql with {"query": "..."}.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from graphql import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLField,
    GraphQLFloat,
    GraphQLID,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    graphql_sync,
    parse,
)
from graphql.language import OperationDefinitionNode, OperationType

from delivery_ingest import ingested_repos
from delivery_metrics import DEFINITIONS, METRICS, UNITS, MetricError, compute
from delivery_metrics import repo_is_ingested as _repo_is_ingested

MAX_QUERY_CHARS = 8000

MetricName = GraphQLEnumType(
    "MetricName", {name.upper(): name for name in METRICS},
    description="A delivery metric that can be computed.",
)

RepoType = GraphQLObjectType(
    "Repo",
    {
        "name": GraphQLField(GraphQLNonNull(GraphQLString),
                             resolve=lambda r, _: r["repo"]),
        "ingestedAt": GraphQLField(GraphQLString, resolve=lambda r, _: r["ingested_at"]),
        "complete": GraphQLField(GraphQLBoolean),
        "pullRequests": GraphQLField(GraphQLInt, resolve=lambda r, _: r["pull_requests"]),
        "reviews": GraphQLField(GraphQLInt),
        "workflowRuns": GraphQLField(GraphQLInt, resolve=lambda r, _: r["workflow_runs"]),
    },
    description="A repository whose delivery data has been ingested.",
)

WeekType = GraphQLObjectType(
    "Week",
    {
        "weekStarting": GraphQLField(GraphQLString, resolve=lambda r, _: r["week_starting"]),
        "merges": GraphQLField(GraphQLInt),
        "pullRequests": GraphQLField(
            GraphQLList(GraphQLInt), resolve=lambda r, _: r["pull_requests"]
        ),
    },
)

MetricType = GraphQLObjectType(
    "Metric",
    {
        "repo": GraphQLField(GraphQLNonNull(GraphQLString)),
        "metric": GraphQLField(GraphQLNonNull(GraphQLString)),
        "definition": GraphQLField(GraphQLString),
        "unit": GraphQLField(GraphQLString),
        "statistic": GraphQLField(GraphQLString),
        "value": GraphQLField(GraphQLFloat),
        "mean": GraphQLField(GraphQLFloat),
        "sampleSize": GraphQLField(GraphQLInt, resolve=lambda r, _: r["sample_size"]),
        "since": GraphQLField(GraphQLString, resolve=lambda r, _: r["window"]["since"]),
        "until": GraphQLField(GraphQLString, resolve=lambda r, _: r["window"]["until"]),
        "failures": GraphQLField(GraphQLInt),
        "completedRuns": GraphQLField(GraphQLInt, resolve=lambda r, _: r.get("completed_runs")),
        "totalMerges": GraphQLField(GraphQLInt, resolve=lambda r, _: r.get("total_merges")),
        "weeks": GraphQLField(GraphQLList(WeekType)),
        "sourcePullRequests": GraphQLField(
            GraphQLList(GraphQLInt),
            resolve=lambda r, _: r["sources"].get("pull_requests"),
            description="Pull request numbers this value was computed from.",
        ),
        # Run ids are well past a 32-bit Int, so they travel as IDs.
        "sourceWorkflowRuns": GraphQLField(
            GraphQLList(GraphQLID),
            resolve=lambda r, _: _as_ids(r["sources"].get("workflow_runs")),
            description="Workflow run ids this value was computed from.",
        ),
        "failedWorkflowRuns": GraphQLField(
            GraphQLList(GraphQLID),
            resolve=lambda r, _: _as_ids(r["sources"].get("failed_runs")),
        ),
    },
    description="One computed metric, with the rows it came from.",
)


def _as_ids(values: list[int] | None) -> list[str] | None:
    """Workflow run ids are larger than GraphQL's 32-bit Int allows."""
    return None if values is None else [str(value) for value in values]


def _resolve_repos(_root: Any, _info: Any) -> list[dict]:
    return ingested_repos()


def _resolve_metric(
    _root: Any,
    _info: Any,
    repo: str,
    metric: str,
    since: str | None = None,
    until: str | None = None,
) -> dict | None:
    # Mirrors the tool layer: an un-ingested repository yields nothing rather
    # than a fabricated zero.
    if not _repo_is_ingested(repo):
        return None
    try:
        return compute(repo, metric, since, until)
    except MetricError:
        return None


QueryType = GraphQLObjectType(
    "Query",
    {
        "repos": GraphQLField(
            GraphQLNonNull(GraphQLList(GraphQLNonNull(RepoType))),
            resolve=_resolve_repos,
            description="Repositories that have been ingested.",
        ),
        "definitions": GraphQLField(
            GraphQLList(GraphQLString),
            resolve=lambda *_: [f"{name}: {DEFINITIONS[name]}" for name in METRICS],
        ),
        "units": GraphQLField(
            GraphQLList(GraphQLString),
            resolve=lambda *_: [f"{name}: {UNITS[name]}" for name in METRICS],
        ),
        "metric": GraphQLField(
            MetricType,
            args={
                "repo": GraphQLArgument(GraphQLNonNull(GraphQLString)),
                "metric": GraphQLArgument(GraphQLNonNull(MetricName)),
                "since": GraphQLArgument(GraphQLString),
                "until": GraphQLArgument(GraphQLString),
            },
            resolve=_resolve_metric,
            description="Null when the repository has not been ingested.",
        ),
    },
)

# No Mutation and no Subscription: the schema itself is read-only.
schema = GraphQLSchema(query=QueryType)


def execute(query: str, variables: dict[str, Any] | None = None) -> dict:
    """Run one GraphQL query, refusing anything that is not a query."""
    if len(query) > MAX_QUERY_CHARS:
        return {"errors": [{"message": "query too long"}]}

    try:
        document = parse(query)
    except Exception as exc:  # graphql raises GraphQLSyntaxError
        return {"errors": [{"message": f"syntax error: {exc}"}]}

    for definition in document.definitions:
        if (
            isinstance(definition, OperationDefinitionNode)
            and definition.operation is not OperationType.QUERY
        ):
            return {
                "errors": [
                    {"message": f"this endpoint is read-only; {definition.operation.value} "
                                "operations are not accepted"}
                ]
            }

    result = graphql_sync(schema, query, variable_values=variables)
    payload: dict[str, Any] = {}
    if result.errors:
        payload["errors"] = [{"message": str(error)} for error in result.errors]
    if result.data is not None:
        payload["data"] = result.data
    return payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "DeliveryMetricsGraphQL/1.0"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (http.server naming)
        if self.path.split("?")[0] != "/graphql":
            return self._send(404, {"errors": [{"message": "not found"}]})
        return self._send(
            200,
            {
                "data": {
                    "service": "delivery metrics",
                    "readOnly": True,
                    "post": "send {\"query\": \"...\"} to /graphql",
                }
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/graphql":
            return self._send(404, {"errors": [{"message": "not found"}]})

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            return self._send(400, {"errors": [{"message": "body is not JSON"}]})
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            return self._send(400, {"errors": [{"message": "expected a 'query' string"}]})

        variables = body.get("variables")
        payload = execute(body["query"], variables if isinstance(variables, dict) else None)
        return self._send(200, payload)


def serve(host: str = "127.0.0.1", port: int = 8099) -> HTTPServer:
    """Create the HTTP server. Call serve_forever() on the result to run it."""
    return HTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)

    httpd = serve(args.host, args.port)
    print(f"read-only GraphQL on http://{args.host}:{args.port}/graphql", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
