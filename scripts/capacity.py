"""zcrew capacity - how many ZCode workers the Coding Plan account carries at once.

``zcrew capacity [--days N] [--json] [--apply [--limit N] [--target PCT]]``

Z.ai publishes no concurrency number for the Coding Plan and moves it with
load; past it a request fails with 1302 "Rate limit reached for requests" /
"High concurrency usage of this API". ZCode records every model request in
``model_usage`` (error type and message included) and every worker turn in
``turn_usage``, so the limit can be measured from this machine's own history
(read-only):

- each 1302 is matched with the number of workers busy at that moment (top-level
  sessions inside a turn); the limit is below the lowest such count;
- without any 1302 the limit is unknown, only bounded from below by the most
  workers that ran cleanly.

``--apply`` writes the estimate (or ``--limit N``) as ``concurrencyLimit`` to
``~/.zcode-commander/config.json``; the bridge then caps running workers at
``concurrencyTarget`` percent of it (default 80, ``--target``), never above
``maxWorkers``, one lower for 10 min after a fresh 1302 (bridge_compat worker
slots). Requests made outside ZCode on the same account (another tool using the
plan's key) share the limit but are not in this database.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
# A 1302 this close to a moment does not count that moment as clean.
CLEAN_MARGIN_MS = 2 * 60 * 1000
LEASE_DB_ENVS = ("AGENT_MCP_LEASE_DB", "ZCODE_MCP_LEASE_DB")
ERROR_ROWS = 20_000
LABELS = {
    "concurrency": "1302 concurrency",
    "overload": "1305 overloaded",
    "usage": "1308+ usage quota",
    "rate_limited": "other 429",
}


def _load(name: str) -> ModuleType:
    key = f"_zcrew_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}.py next to {HERE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


# --- reading (read-only) ----------------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def read_turns(conn: sqlite3.Connection, since_ms: int) -> list[tuple[int, int]]:
    """(start, end) of every top-level worker turn since ``since_ms``; a turn still
    open has no end yet and is left out."""
    rows = conn.execute(
        "SELECT t.started_at, COALESCE(t.completed_at, t.started_at + COALESCE(t.duration_ms, 0))"
        " FROM turn_usage t JOIN session s ON s.id = t.session_id"
        " WHERE s.parent_id IS NULL AND t.started_at >= ?", (int(since_ms),),
    ).fetchall()
    return [(int(a), int(b)) for a, b in rows if a is not None and b is not None and b > a]


def read_errors(conn: sqlite3.Connection, since_ms: int, classify: Any) -> list[tuple[int, str]]:
    """(time, class) of every limit error since ``since_ms``; [] on builds without model_usage."""
    try:
        rows = conn.execute(
            "SELECT started_at, error_type, error_message FROM model_usage WHERE started_at >= ?"
            " AND (COALESCE(error_type, '') != '' OR COALESCE(error_message, '') != '')"
            f" ORDER BY started_at LIMIT {ERROR_ROWS}", (int(since_ms),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for at, error_type, message in rows:
        kind = classify(error_type, message)
        if kind and at is not None:
            out.append((int(at), kind))
    return out


def lease_db_path(env: Mapping[str, str]) -> Path:
    for name in LEASE_DB_ENVS:
        if (env.get(name) or "").strip():
            return Path(env[name])
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    return home / ".zcode" / "cli" / "zcode-mcp-resource-leases.sqlite"


def slots_in_use(path: Path, prefix: str) -> int | None:
    """Runs holding a worker slot right now (None when the bridge lease db is absent)."""
    if not Path(path).is_file():
        return None
    try:
        conn = _connect_ro(path)
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT owner_id || ' ' || run_id) FROM resource_lease WHERE resource_key LIKE ?",
                (prefix + "%",),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0] or 0)


# --- analysis (pure) --------------------------------------------------------------------


def busy_at(turns: list[tuple[int, int]], at: int) -> int:
    return sum(1 for start, end in turns if start <= at < end)


def busy_profile(turns: list[tuple[int, int]], cap: int) -> dict[str, Any]:
    """Average and peak workers busy over the time at least one was, and the share
    of that time spent at or above ``cap``."""
    edges = sorted([(s, 1) for s, _ in turns] + [(e, -1) for _, e in turns], key=lambda x: (x[0], x[1]))
    busy = peak = 0
    last = None
    busy_ms = weighted = at_cap_ms = 0
    for at, step in edges:
        if last is not None and busy > 0:
            busy_ms += at - last
            weighted += busy * (at - last)
            if busy >= cap:
                at_cap_ms += at - last
        busy += step
        peak = max(peak, busy)
        last = at
    return {
        "avg": round(weighted / busy_ms, 2) if busy_ms else None,
        "peak": peak,
        "at_cap_share": round(at_cap_ms / busy_ms, 3) if busy_ms else None,
    }


def analyze(turns: list[tuple[int, int]], errors: list[tuple[int, str]], cap: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for _, kind in errors:
        counts[kind] = counts.get(kind, 0) + 1
    hits = [at for at, kind in errors if kind == "concurrency"]
    busy_at_hits = [busy_at(turns, at) for at in hits]
    clean_peak = 0
    for start, _ in turns:
        if all(abs(start - at) > CLEAN_MARGIN_MS for at in hits):
            clean_peak = max(clean_peak, busy_at(turns, start))
    estimate = None
    if busy_at_hits:
        # the request that hit 1302 came from one of the busy workers: the
        # account carried one fewer, at most
        estimate = max(1, min(busy_at_hits) - 1)
    return {
        "errors": counts,
        "concurrency_hits": len(hits),
        "busy_at_1302": {"min": min(busy_at_hits), "max": max(busy_at_hits)} if busy_at_hits else None,
        "clean_peak": clean_peak,
        "estimate": estimate,
        "busy": busy_profile(turns, cap),
    }


def target_cap(limit: int, target: int, max_workers: int) -> int:
    return min(max_workers, max(1, limit * target // 100))


# --- output -----------------------------------------------------------------------------


def _pct(share: float | None) -> str:
    return "-" if share is None else f"{round(100 * share)}%"


def render(report: Mapping[str, Any]) -> list[str]:
    a, cfg, cap = report["analysis"], report["settings"], report["cap"]
    lines = [f"Coding Plan capacity, last {report['days']:g} days (every directory on this machine)", ""]
    errors = a["errors"]
    lines.append("limit errors     " + (" · ".join(f"{LABELS.get(k, k)} {v}" for k, v in sorted(errors.items()))
                                      if errors else "none"))
    if a["busy_at_1302"]:
        lines.append(f"at each 1302     {a['busy_at_1302']['min']}-{a['busy_at_1302']['max']} workers busy")
    busy = a["busy"]
    if busy["avg"] is not None:
        lines.append(f"workers busy     avg {busy['avg']:.1f}, peak {busy['peak']}, clean peak {a['clean_peak']};"
                     f" at the cap {_pct(busy['at_cap_share'])} of busy time")
    if a["estimate"] is not None:
        lines.append(f"limit estimate   about {a['estimate']} at once (measured: 1302 came with"
                     f" {a['busy_at_1302']['min']} busy)")
    else:
        lines.append(f"limit estimate   not reached yet; at least {max(1, a['clean_peak'])} ran without a 1302")
    configured = cfg["concurrencyLimit"] or "not set"
    lines.append(f"config           concurrencyLimit {configured} · concurrencyTarget {cfg['concurrencyTarget']}%"
                 f" · maxWorkers {cfg['maxWorkers']}  (~/.zcode-commander/config.json)")
    now = f"cap now          {cap['cap']} worker(s) at once"
    if cap["backoff"]:
        now += f" (one below {cap['configured']}: a 1302 in the last 10 min)"
    if report["slots_in_use"] is not None:
        now += f" · {report['slots_in_use']} running"
    lines.append(now)
    lines.append("")
    lines += advice(report)
    return lines


def advice(report: Mapping[str, Any]) -> list[str]:
    a, cfg = report["analysis"], report["settings"]
    target, max_workers = cfg["concurrencyTarget"], cfg["maxWorkers"]
    if a["estimate"] is not None:
        if cfg["concurrencyLimit"] == a["estimate"]:
            return [f"next             config matches the measurement; cap {report['cap']['configured']}"
                    f" = {target}% of {a['estimate']}"]
        return [f"next             zcrew capacity --apply  (concurrencyLimit {a['estimate']} ->"
                f" cap {target_cap(a['estimate'], target, max_workers)})"]
    lines = [
        "next             the limit has not been hit, so there is nothing to measure yet. Run independent",
        "                 tasks in parallel (git worktrees) up to maxWorkers; the bridge backs off by one",
        "                 after a 1302, and the next zcrew capacity measures the limit from it.",
    ]
    if not cfg["concurrencyLimit"]:
        lines.append("                 Unofficial: GLM Coding Pro users report about 3 sessions at once"
                     " (zcrew capacity --apply --limit 3 -> cap 2).")
    return lines


# --- config write -----------------------------------------------------------------------


def apply_settings(path: Path, values: Mapping[str, int]) -> None:
    """Set keys in the user config, keeping every other key; written atomically."""
    data: dict[str, Any] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} is not a JSON object; fix it first")
        data = loaded
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(args: Any, *, env: Mapping[str, str] | None = None, out: Any = None, now_ms: int | None = None) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    compat = _load("bridge_compat")
    za = _load("zcode_activity")
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    since = now - int(max(0.0, float(args.days)) * 86_400_000)
    settings = compat.concurrency_settings(env)
    cap = compat.worker_cap(env, now_ms=now)
    try:
        db = za.default_db_path(env)
        conn = za.connect(db)
    except za.ActivityError as exc:
        out.write(f"zcrew: {exc}\n")
        return 1
    try:
        turns = read_turns(conn, since)
        errors = read_errors(conn, since, compat.classify_limit_error)
    except sqlite3.Error as exc:
        out.write(f"zcrew: cannot read ZCode database: {exc}\n")
        return 1
    finally:
        conn.close()
    report = {
        "days": float(args.days),
        "since_ms": since,
        "analysis": analyze(turns, errors, cap["cap"]),
        "settings": settings,
        "cap": {k: cap[k] for k in ("cap", "configured", "backoff")},
        "slots_in_use": slots_in_use(lease_db_path(env), compat.SLOT_PREFIX),
    }
    if getattr(args, "apply", False):
        limit = args.limit if args.limit is not None else report["analysis"]["estimate"]
        if limit is None:
            out.write("zcrew: no 1302 in this window, so there is no measured limit to apply;"
                      " pass --limit N to set one yourself\n")
            return 1
        lo, hi = compat.CONCURRENCY_SETTINGS["concurrencyLimit"][1:]
        if not lo < limit <= hi:
            out.write(f"zcrew: --limit must be from 1 to {hi}\n")
            return 1
        values = {"concurrencyLimit": int(limit)}
        if args.target is not None:
            lo, hi = compat.CONCURRENCY_SETTINGS["concurrencyTarget"][1:]
            if not lo <= args.target <= hi:
                out.write(f"zcrew: --target must be from {lo} to {hi}\n")
                return 1
            values["concurrencyTarget"] = int(args.target)
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        path = home / ".zcode-commander" / "config.json"
        try:
            apply_settings(path, values)
        except (OSError, ValueError) as exc:
            out.write(f"zcrew: {exc}\n")
            return 1
        new = compat.concurrency_settings(env)
        report["settings"] = new
        report["applied"] = values
        out.write(f"set {', '.join(f'{k} {v}' for k, v in values.items())} in {path}:"
                  f" up to {compat.configured_cap(new)} worker(s) at once"
                  " (running bridges pick it up within seconds)\n")
        if not args.json:
            return 0
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
        return 0
    out.write("\n".join(render(report)) + "\n")
    return 0
