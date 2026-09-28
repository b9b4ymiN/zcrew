"""Dashboard performance baseline benchmark (no UI change).

Builds a synthetic ZCode database, serves it with the real dashboard server
(scripts/dashboard.py) on 127.0.0.1:0, and times the five requests the page
issues: the session list, a full activity fetch, the incremental empty-delta
activity poll the page runs every 1.5 s, the quota status poll, and the page
itself. Numbers are for before/after comparison on one machine, not absolute
truth: the synthetic database has no indexes and the timing includes loopback
HTTP.

    python tests/bench_dashboard.py [--n N] [--out PATH]

Not named test_* so unittest discovery skips it; run_bench() itself is
exercised by tests/test_bench_dashboard.py.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import math
import platform
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from test_zcode_activity import SANDBOX, Fixture, tool

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

WARMUP = 5
DEFAULT_N = 200
SESSIONS = 100
RUNNING = 5
FAILED = 10
PARTS = 30
BIG_PARTS = 400
BIG_ID = "sess_big0400"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


dash = _load("dashboard")


def _tool_part(i: int, j: int, status: str) -> dict[str, Any]:
    """A completed-looking tool part cycling Read/Edit/Bash with outputs."""
    if j % 3 == 0:
        return tool("Read", status, {"file_path": SANDBOX + f"\\src\\mod{j:02d}\\file_{i:03d}.py"},
                    output=f"{30 + j} lines")
    if j % 3 == 1:
        return tool("Edit", status, {"file_path": SANDBOX + f"\\src\\mod{j:02d}\\file_{i:03d}.py",
                                     "old_string": "old line", "new_string": f"new line {j}\nplus one"},
                    output="ok")
    if status == "error":
        return tool("Bash", status, {"command": f"python -m pytest tests/test_mod{j:02d}.py -q"},
                    error=f"exit 1\r\nE   assert {j} == {j + 1}")
    return tool("Bash", status, {"command": f"python -m pytest tests/test_mod{j:02d}.py -q"},
                output=f".{'.' * 20}\r\nRan {20 + j} tests\r\n\r\nOK\r\n")


def _seed(fx: Fixture) -> None:
    """Fill the fixture db in bulk; Fixture's per-row exec is too slow at ~3,800 parts."""
    now = int(time.time() * 1000)
    sessions: list[tuple[str, str, str, int, int]] = []
    messages: list[tuple[str, str, int, int, str]] = []
    parts: list[tuple[str, str, str, int, int, str, int]] = []
    turns: list[tuple[str, str, str, int, int, int, int, int]] = []
    seq = 0

    for i in range(SESSIONS):
        running = i < RUNNING
        failed = not running and i < RUNNING + FAILED
        sid = f"sess_bulk{i:03d}"
        # The dashboard derives status against the real clock, so running
        # sessions need parts within STALE_MS of "now"; the rest can sit far back.
        base = now - (i + 1) * (1_000 if running else 60_000)
        sessions.append((sid, SANDBOX, f"bench session {i:03d}", base, base))
        messages.append((f"{sid}_m", sid, base, base, json.dumps({"role": "assistant"})))
        for j in range(PARTS):
            seq += 1
            if running and j == PARTS - 1:
                status = "running"
            elif failed and j == PARTS - 1:
                status = "error"
            else:
                status = "completed"
            at = base + 100 * j + 50
            parts.append((f"{sid}_p{j:03d}", f"{sid}_m", sid, at, at,
                          json.dumps(_tool_part(i, j, status), separators=(",", ":")), seq))
        if running:
            continue  # no turn row: the newest part leaves the session "running"
        end = base + 100 * PARTS + 100
        turns.append((sid, "turn_1", "error" if failed else "completed", base, end,
                      end - base, PARTS, 1 if failed else 0))

    big_base = now - 120_000
    sessions.append((BIG_ID, SANDBOX, "bench big session", big_base, big_base))
    messages.append((f"{BIG_ID}_m", BIG_ID, big_base, big_base, json.dumps({"role": "assistant"})))
    for j in range(BIG_PARTS):
        seq += 1
        at = big_base + j  # distinct ms per part so the incremental delta after a full fetch is empty
        parts.append((f"{BIG_ID}_p{j:04d}", f"{BIG_ID}_m", BIG_ID, at, at,
                      json.dumps(_tool_part(0, j, "completed"), separators=(",", ":")), seq))
    turns.append((BIG_ID, "turn_1", "completed", big_base, big_base + BIG_PARTS + 100,
                  BIG_PARTS + 100, BIG_PARTS, 0))

    conn = sqlite3.connect(fx.path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO session (id, directory, title, time_created, time_updated) VALUES (?,?,?,?,?)",
                sessions)
            conn.executemany(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                messages)
            conn.executemany(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data, sequence)"
                " VALUES (?,?,?,?,?,?,?)", parts)
            conn.executemany(
                "INSERT INTO turn_usage (session_id, turn_id, status, started_at, completed_at,"
                " duration_ms, tool_call_count, tool_error_count) VALUES (?,?,?,?,?,?,?,?)", turns)
    finally:
        conn.close()


def _get(conn: http.client.HTTPConnection, path: str) -> bytes:
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    if resp.status != 200:
        raise RuntimeError(f"GET {path} -> HTTP {resp.status}: {body[:200]!r}")
    return body


def _pct(sorted_ms: list[float], p: int) -> float:
    """Nearest-rank percentile of already-sorted samples (rank 1 = smallest)."""
    rank = max(1, math.ceil(p / 100 * len(sorted_ms)))
    return sorted_ms[rank - 1]


def _measure(conn: http.client.HTTPConnection, path: str, n: int) -> dict[str, Any]:
    for _ in range(WARMUP):
        _get(conn, path)
    samples: list[float] = []
    size = 0
    for i in range(n):
        started = time.perf_counter_ns()
        body = _get(conn, path)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        size = size or len(body)
    samples.sort()
    return {
        "p50_ms": round(_pct(samples, 50), 3),
        "p95_ms": round(_pct(samples, 95), 3),
        "max_ms": round(samples[-1], 3),
        "response_bytes": size,
    }


def run_bench(n: int = DEFAULT_N) -> dict[str, Any]:
    """Seed, serve and measure; returns the report dict written by the CLI."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(Path(tmp))
        _seed(fx)
        state = dash.DashboardState({"ZCREW_ZCODE_DB": str(fx.path)}, SANDBOX)
        state.za.LAYOUT_DEGRADED = False  # sticky per process; reset so runs stay comparable
        httpd = dash.DashboardServer(("127.0.0.1", 0), dash.DashboardHandler, state)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            try:
                full = f"/api/sessions/{BIG_ID}/activity"
                cursor = json.loads(_get(conn, full))["cursor"]
                paths = {
                    "sessions_limit_100": "/api/sessions?limit=100",
                    "activity_full": full,
                    "activity_incremental_empty": f"{full}?after={quote(cursor, safe='')}",
                    "quota": "/api/quota",  # no key in env: cached local estimate
                    "page": "/",
                }
                endpoints = {name: _measure(conn, path, n) for name, path in paths.items()}
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    return {
        "python": platform.python_version(),
        "n": n,
        "warmup": WARMUP,
        "dataset": {
            "sessions": SESSIONS + 1,
            "running": RUNNING,
            "failed": FAILED,
            "completed": SESSIONS - RUNNING - FAILED + 1,  # bulk completed plus the big session
            "parts_per_session": PARTS,
            "big_session_parts": BIG_PARTS,
        },
        "endpoints": endpoints,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard performance baseline benchmark.")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="measured requests per endpoint")
    parser.add_argument("--out", default=None, help="also write the JSON report to this path")
    args = parser.parse_args(argv)
    report = json.dumps(run_bench(args.n), indent=2)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
