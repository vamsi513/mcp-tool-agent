"""Racing confirmations of one pending id must still post exactly once.

`confirm_issue_comment` claims the pending row with a conditional UPDATE
before it issues the POST. These tests put two real callers on separate SQLite
connections into that window at the same moment and check the stub only ever
receives one comment.

The sequential case lives in test_connector_github.py; this file is about the
concurrent one, in both forms: OS threads sharing an interpreter, and separate
processes that share nothing but the database file and the stub.
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import connector_github as gh
from evals.stub_github import StubGitHub

REPO_ROOT = Path(__file__).resolve().parent.parent

THREAD_TRIALS = 20
PROCESS_TRIALS = 5

# Each child confirms the same id, waiting for a shared wall-clock deadline so
# both land in the claim window together without needing any IPC.
CHILD_SOURCE = """
import json, os, sys, time
sys.path.insert(0, {root!r})
import connector_github as gh

deadline = float(sys.argv[2])
now = time.time()
if deadline > now:
    time.sleep(deadline - now)

try:
    result = gh.confirm_issue_comment(sys.argv[1])
    print(json.dumps({{"status": result["status"], "written": result["written"]}}))
except Exception as exc:
    print(json.dumps({{"status": "error", "error": f"{{type(exc).__name__}}: {{exc}}"}}))
"""


@pytest.fixture
def stub():
    with StubGitHub() as server:
        yield server


def _consumed_count(db_path: Path, action_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM pending_comments"
            " WHERE id = ? AND consumed_ts IS NOT NULL",
            (action_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _race_confirm_in_threads(
    action_id: str, workers: int = 2
) -> tuple[list[dict], list[BaseException]]:
    """Call confirm_issue_comment from `workers` threads released together.

    Each thread opens its own SQLite connection inside the connector, so this
    is a real race on the conditional UPDATE, not a serialised one.
    """
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=20)
            outcome = gh.confirm_issue_comment(action_id)
        except BaseException as exc:  # reported to the test, never swallowed
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, name=f"confirm-{i}") for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a confirming thread did not finish"
    return results, errors


def test_two_threads_confirming_at_once_post_once(governance_db, stub, monkeypatch):
    """Two OS threads on separate connections, one independent race per trial."""
    monkeypatch.setenv("GITHUB_API_BASE", stub.base_url)

    for trial in range(THREAD_TRIALS):
        stub.reset()
        pending = gh.prepare_issue_comment(
            "octocat/hello-world", 1, f"concurrent thread trial {trial}"
        )
        action_id = pending["pending_action_id"]

        results, errors = _race_confirm_in_threads(action_id)

        assert not errors, f"trial {trial}: {errors!r}"
        posted = [r for r in results if r["status"] == "posted"]
        skipped = [r for r in results if r["status"] == "already_confirmed"]

        assert len(stub.posted) == 1, (
            f"trial {trial}: stub received {len(stub.posted)} POST(s)"
        )
        assert len(posted) == 1, f"trial {trial}: {len(posted)} callers claimed the write"
        assert len(skipped) == 1, f"trial {trial}: {len(skipped)} callers backed off"
        assert posted[0]["written"] is True
        assert skipped[0]["written"] is False
        assert _consumed_count(governance_db, action_id) == 1
        assert stub.posted[0]["body"] == f"concurrent thread trial {trial}"


def test_two_processes_confirming_at_once_post_once(governance_db, stub, monkeypatch):
    """Separate interpreters sharing only the database file and the stub."""
    monkeypatch.setenv("GITHUB_API_BASE", stub.base_url)

    child_env = {
        **os.environ,
        "GOVERNANCE_DB": str(governance_db),
        "GITHUB_API_BASE": stub.base_url,
    }
    source = CHILD_SOURCE.format(root=str(REPO_ROOT))

    for trial in range(PROCESS_TRIALS):
        stub.reset()
        pending = gh.prepare_issue_comment(
            "octocat/hello-world", 1, f"concurrent process trial {trial}"
        )
        action_id = pending["pending_action_id"]

        # Start both children, then let them converge on a shared deadline.
        deadline = time.time() + 1.5
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", source, action_id, str(deadline)],
                cwd=REPO_ROOT,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            assert proc.returncode == 0, f"trial {trial}: child failed: {stderr}"
            outputs.append(json.loads(stdout.strip()))

        assert all(o["status"] != "error" for o in outputs), f"trial {trial}: {outputs}"
        posted = [o for o in outputs if o["status"] == "posted"]
        skipped = [o for o in outputs if o["status"] == "already_confirmed"]

        assert len(stub.posted) == 1, (
            f"trial {trial}: stub received {len(stub.posted)} POST(s)"
        )
        assert len(posted) == 1, f"trial {trial}: {len(posted)} processes claimed the write"
        assert len(skipped) == 1, f"trial {trial}: {len(skipped)} processes backed off"
        assert _consumed_count(governance_db, action_id) == 1
