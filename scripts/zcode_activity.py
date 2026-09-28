"""zcrew activity - read-only view of what ZCode sessions are doing.

Reads (never writes) ZCode's CLI database, ``~/.zcode/cli/db/db.sqlite``
(override with $ZCREW_ZCODE_DB), and turns it into:

- session summaries (status, tool calls, errors, last context size), and
- "level 2" activity lines per session: file reads/edits/writes, commands with
  a one-line outcome, searches, other tools, step ends, turn ends and the
  assistant's own text (first ~200 chars). Reasoning text and full tool
  outputs are never returned.

Activity is incremental: ``session_activity`` returns an opaque cursor; passing
it back returns only parts that are new or were updated since (a tool that went
running -> completed is emitted again with its new status).

The database is opened with ``mode=ro`` for each call and closed right after.
A session id equals the bridge ``threadId`` the commander receives.

Session summaries are batched (one grouped scan per chunk of sessions); the
tricks that keep those scans cheap are commented where they are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Union
from urllib.parse import quote

ENV_DB = "ZCREW_ZCODE_DB"
SESSION_PREFIX = "sess_"
CONNECT_TIMEOUT = 2.0
MMAP_BYTES = 2 * 1024 * 1024 * 1024  # cap for memory-mapped reads (see connect)

TITLE_CHARS = 100
COMMAND_CHARS = 120
OUTCOME_CHARS = 80
MESSAGE_CHARS = 200
TOOL_CHARS = 100
# An unfinished session silent for this long is reported idle, not running.
STALE_MS = 30 * 60 * 1000

# Columns this module reads; anything missing means ZCode changed its schema.
REQUIRED_SCHEMA: dict[str, tuple[str, ...]] = {
    "session": ("id", "directory", "title", "time_created", "time_updated", "parent_id"),
    "part": ("id", "message_id", "session_id", "time_created", "time_updated", "data"),
    "message": ("id", "session_id", "data"),
    "turn_usage": (
        "session_id", "turn_id", "status", "started_at", "completed_at", "duration_ms",
        "tool_call_count", "tool_error_count",
    ),
}

LIVE_STATUSES = {"running", "pending"}
FAILED_STATUSES = {"error", "failed"}
FILE_KINDS = {
    "Read": "file_read",
    "Edit": "file_edit",
    "MultiEdit": "file_edit",
    "NotebookEdit": "file_edit",
    "Write": "file_write",
}
SHELL_TOOLS = {"Bash", "PowerShell", "Shell"}
SEARCH_TOOLS = {"Grep", "Glob"}

# Activity categories for dashboard color-coding; every activity maps to one.
CATEGORIES = ("read", "edit", "run", "web", "agent", "msg")

READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
RUN_TOOLS = {"Bash", "PowerShell", "TaskOutput", "TaskStop", "BashOutput", "KillShell"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
# A mcp__ tool whose full name carries one of these (lowercase) is web access,
# except servers serving local knowledge bases: their wiki_search/codegraph_search/
# memory_recall style tools contain "search"/"url" without touching the network.
WEB_NAME_HINTS = (
    "playwright", "browser", "web", "search", "fetch", "url", "http", "youtube",
    "tradingview", "image_search",
)
LOCAL_MCP_SERVERS = {"brain", "codegraph", "agentmemory", "alexandria", "obsidian"}

Db = Union[str, "os.PathLike[str]", sqlite3.Connection]


class ActivityError(Exception):
    """User-facing error (database missing, schema changed, unknown session...)."""


@dataclass(frozen=True)
class SessionSummary:
    id: str
    directory: str
    title: str
    created_ms: int
    updated_ms: int
    status: str  # running | completed | failed | idle
    tool_calls: int
    errors: int
    last_context_tokens: int | None
    duration_ms: int
    parent_id: str | None = None


@dataclass(frozen=True)
class Activity:
    at_ms: int
    session_id: str
    kind: str  # file_read | file_edit | file_write | command | search | tool | step | turn_end | message
    status: str
    summary: str
    tool: str | None = None
    detail: str | None = None  # command outcome / error line
    key: str = ""  # stable id: an updated item re-uses its key


def activity_category(kind: str, tool: str | None) -> str:
    """One of CATEGORIES for an activity; ordered rules, first match wins.

    Status never matters here (the UI shows it separately); anything unknown
    falls to "agent".
    """
    if kind in ("message", "step", "turn_end"):
        return "msg"
    if kind in ("file_read", "search") or tool in READ_TOOLS:
        return "read"
    if kind in ("file_edit", "file_write") or tool in EDIT_TOOLS:
        return "edit"
    if kind == "command" or tool in RUN_TOOLS:
        return "run"
    if tool in WEB_TOOLS:
        return "web"
    if tool is not None and tool.startswith("mcp__"):
        if tool[len("mcp__"):].split("__", 1)[0] not in LOCAL_MCP_SERVERS:
            name = tool.lower()
            if any(hint in name for hint in WEB_NAME_HINTS):
                return "web"
    return "agent"


# --- database access --------------------------------------------------------------------


def default_db_path(env: Mapping[str, str]) -> Path:
    override = env.get(ENV_DB)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open the database read-only (``mode=ro``); raises ActivityError if absent."""
    p = Path(path)
    if not p.is_file():
        raise ActivityError(f"ZCode database not found: {p} (set {ENV_DB} to override)")
    uri = "file:" + quote(p.resolve().as_posix(), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=CONNECT_TIMEOUT)
    except sqlite3.Error as exc:
        raise ActivityError(f"cannot open ZCode database {p}: {exc}") from exc
    try:
        # Memory-mapped reads keep whole-database scans (~400k parts, >1 GB of JSON)
        # from paying a syscall per page; harmless where mmap is unavailable.
        conn.execute(f"PRAGMA mmap_size={MMAP_BYTES}")
    except sqlite3.Error:
        pass
    return conn


def check_schema(conn: sqlite3.Connection) -> list[str]:
    """Problems with the tables/columns we depend on; empty list = OK."""
    problems: list[str] = []
    for table, columns in REQUIRED_SCHEMA.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            problems.append(f"missing table {table}")
            continue
        missing = [c for c in columns if c not in have]
        if missing:
            problems.append(f"table {table} missing column(s) {', '.join(missing)}")
    return problems


@contextlib.contextmanager
def _session(db: Db) -> Iterator[sqlite3.Connection]:
    """Yield a checked connection; own connections are closed on exit."""
    own = not isinstance(db, sqlite3.Connection)
    conn = connect(db) if own else db  # type: ignore[arg-type]
    try:
        try:
            problems = check_schema(conn)
        except sqlite3.Error as exc:
            raise ActivityError(f"cannot read ZCode database: {exc}") from exc
        if problems:
            raise ActivityError("ZCode database schema changed: " + "; ".join(problems))
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            raise ActivityError(f"ZCode database query failed (schema changed?): {exc}") from exc
        except sqlite3.Error as exc:
            raise ActivityError(f"ZCode database error: {exc}") from exc
    finally:
        if own:
            conn.close()


# --- small helpers ----------------------------------------------------------------------


def _norm_dir(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def _under(directory: str, root: str) -> bool:
    d, r = _norm_dir(directory), _norm_dir(root)
    return bool(r) and (d == r or d.startswith(r + "/"))


def display_path(path: str, directory: str | None) -> str:
    """``path`` relative to the session directory when inside it (forward slashes)."""
    if directory and _under(path, directory):
        rest = path.replace("\\", "/")[len(directory.replace("\\", "/").rstrip("/")):].lstrip("/")
        return rest or "."
    return path


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _first_line(text: Any) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _last_line(text: Any) -> str:
    for line in reversed(str(text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _load(data: Any) -> dict[str, Any]:
    try:
        value = json.loads(data) if isinstance(data, (str, bytes)) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _strip_cd(command: str, directory: str | None) -> str:
    """Drop a leading ``cd "<session dir>" &&`` - it is noise in a summary."""
    m = re.match(r'\s*cd\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))\s*(?:&&|;)\s*', command)
    if m and directory:
        target = next(g for g in m.groups() if g)
        if _norm_dir(target) == _norm_dir(directory):
            return command[m.end():]
    return command


# --- sessions ---------------------------------------------------------------------------


def resolve_session(db: Db, session_id: str) -> str:
    """Full session id from an exact id or a unique prefix (``sess_`` optional)."""
    wanted = session_id.strip()
    if not wanted:
        raise ActivityError("empty session id")
    with _session(db) as conn:
        return _resolve(conn, wanted)


def _resolve(conn: sqlite3.Connection, wanted: str) -> str:
    if conn.execute("SELECT 1 FROM session WHERE id = ?", (wanted,)).fetchone():
        return wanted
    prefixes = [wanted] if wanted.startswith(SESSION_PREFIX) else [wanted, SESSION_PREFIX + wanted]
    for prefix in prefixes:
        rows = conn.execute(
            "SELECT id FROM session WHERE substr(id, 1, ?) = ? ORDER BY time_updated DESC LIMIT 6",
            (len(prefix), prefix),
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1:
            shown = ", ".join(r[0] for r in rows[:5])
            raise ActivityError(f"session id {wanted!r} is ambiguous: {shown}{', ...' if len(rows) > 5 else ''}")
    raise ActivityError(f"no ZCode session matches {wanted!r}")


def _status(
    last_part_ms: int | None,
    last_part: dict[str, Any],
    last_turn: tuple[str, int] | None,
    now_ms: int,
) -> str:
    if last_part_ms is None and last_turn is None:
        return "idle"
    if last_turn is not None and (last_part_ms is None or last_part_ms <= last_turn[1]):
        return "completed" if last_turn[0] == "completed" else "failed"
    # No turn row covers the newest part: a turn is in flight, or ended without a
    # turn_usage row (old ZCode builds, resumed sub-agents) - read the last part.
    if last_part.get("type") == "step-finish" and last_part.get("reason") not in (None, "tool-calls"):
        return "completed" if last_part.get("reason") == "stop" else "failed"
    if last_part_ms is not None and now_ms - last_part_ms > STALE_MS:
        return "idle"  # never finished and silent for long: interrupted / abandoned
    return "running"


# part.data is compact Go-encoded JSON with a fixed key order: "type" first,
# "reason" second on step-finish parts, and a tool part's state.status right
# behind the first '"status":"'. Verified against every one of the ~400k rows of
# a real 2.1 GB database, so byte-prefix tests replace parsing every blob
# (json_extract over all parts alone costs ~15 s). _layout_ok below samples
# recent parts and compares those prefix tests against real json_extract, so a
# serialization change in ZCode switches to the exact path instead of producing
# silently wrong tool_calls/errors. It guards newly written rows only: old rows
# that a sample never reaches are trusted as verified above.

TOOL_PREFIX = '{"type":"tool"'
STEP_FINISH_PREFIX = '{"type":"step-finish"'
STATUS_TAG = '"status":"'
ERROR_STATUS_TAGS = ('"status":"error",', '"status":"error"}', '"status":"failed"')
# A latest-part pack is printf('%015d%015d', time_updated, time_created) followed
# by hex(substr(data, 1, 44)); the two %015d fields together lead every pack.
PACK_KEY_LEN = 30
SCAN_CHUNK = 450  # sessions per IN (...) batch, far below sqlite's variable limit
# Parts per chunk classified by the layout self-check; the most recently
# updated are sampled because a serialization change shows up in new rows.
LAYOUT_SAMPLE_PARTS = 200

# Classification snippets shared by the batched scan and the layout self-check,
# so the check always exercises the exact expressions the scan runs.
_IS_TOOL_SQL = "substr(data,1,14) = ?"
_IS_STEP_FINISH_SQL = "substr(data,1,21) = ?"
_STATUS_AT_SQL = "instr(data, ?)"
# Reads the is_tool/status_at aliases the enclosing query computes.
_IS_ERROR_SQL = (
    "CASE WHEN is_tool AND status_at > 0"
    " AND substr(data, status_at, 17) IN (?,?,?) THEN 1 ELSE 0 END"
)

# Placeholders in order: three error status tags, tool prefix, step-finish
# prefix, status tag, then the session ids of one chunk.
_PART_SCAN_SQL = (
    "SELECT session_id, MAX(time_updated) AS last_upd,"
    " SUM(is_tool) AS tools,"
    " SUM(" + _IS_ERROR_SQL + ") AS errors,"
    " group_concat(CASE WHEN is_sf THEN time_created || '.' || id END, ';') AS candidates,"
    " MAX(printf('%015d%015d', time_updated, time_created) || hex(substr(data,1,44))) AS latest_pack"
    " FROM (SELECT session_id, id, time_created, time_updated, data,"
    "              " + _IS_TOOL_SQL + " AS is_tool,"
    "              " + _IS_STEP_FINISH_SQL + " AS is_sf,"
    "              " + _STATUS_AT_SQL + " AS status_at"
    "         FROM part WHERE session_id IN ({ids}))"
    " GROUP BY session_id"
)

# Same placeholders as _PART_SCAN_SQL, but the trailing ids are the sampled
# part ids; each row yields the prefix classification and the json_extract
# truth side by side (nested CASE keeps json_extract off malformed rows).
_LAYOUT_PROBE_SQL = (
    "SELECT is_tool, is_sf, " + _IS_ERROR_SQL + " AS fast_err,"
    " CASE WHEN json_valid(data) THEN CASE WHEN json_extract(data,'$.type')='tool'"
    "      THEN 1 ELSE 0 END ELSE 0 END AS exact_tool,"
    " CASE WHEN json_valid(data) THEN CASE WHEN json_extract(data,'$.type')='step-finish'"
    "      THEN 1 ELSE 0 END ELSE 0 END AS exact_sf,"
    " CASE WHEN json_valid(data) THEN CASE WHEN json_extract(data,'$.type')='tool'"
    "           AND json_extract(data,'$.state.status') IN ('error','failed')"
    "      THEN 1 ELSE 0 END ELSE 0 END AS exact_err"
    " FROM (SELECT data, " + _IS_TOOL_SQL + " AS is_tool,"
    "              " + _IS_STEP_FINISH_SQL + " AS is_sf,"
    "              " + _STATUS_AT_SQL + " AS status_at"
    "         FROM part WHERE id IN ({ids}))"
)

# Bare status/ended come from the row that produced MAX(ended) (SQLite's
# documented min/max behaviour; ties would repeat old ORDER BY ... LIMIT 1 anyway).
_TURN_SCAN_SQL = (
    "SELECT session_id, status, COALESCE(completed_at, started_at) AS ended,"
    " MAX(COALESCE(completed_at, started_at)), COALESCE(SUM(duration_ms), 0)"
    " FROM turn_usage WHERE session_id IN ({ids}) GROUP BY session_id"
)


def list_sessions(
    db: Db,
    *,
    cwd: str | None = None,
    since_ms: int | None = None,
    limit: int = 20,
    now_ms: int | None = None,
) -> list[SessionSummary]:
    """Sessions newest first; ``cwd`` matches the directory or anything under it.

    status: ``completed``/``failed`` from the last turn_usage row (or a final
    step-finish when no row exists), ``running`` while newer parts exist,
    ``idle`` for empty sessions or unfinished ones silent for STALE_MS.
    """
    now = int(time.time() * 1000) if now_ms is None else now_ms
    with _session(db) as conn:
        sql = "SELECT id, directory, title, time_created, time_updated, parent_id FROM session"
        args: list[Any] = []
        if since_ms is not None:
            sql += " WHERE time_updated >= ?"
            args.append(since_ms)
        sql += " ORDER BY time_updated DESC, id DESC"
        rows = []
        for row in conn.execute(sql, args):
            if cwd is None or _under(row[1] or "", cwd):
                rows.append(row)
                if len(rows) >= limit:
                    break
        return _summarize_many(conn, rows, now)


def _chunks(ids: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


# Set (never cleared) the first time a call detects a non-standard layout; the
# CLI shows it as one stderr note per process.
LAYOUT_DEGRADED = False


def _layout_ok(conn: sqlite3.Connection, session_ids: list[str]) -> bool:
    """Do the prefix classifications agree with real JSON parsing on a sample?

    Samples the LAYOUT_SAMPLE_PARTS most recently updated parts of the batch's
    newest sessions and compares, per row, the scan's prefix tests (is tool, is
    step-finish, is error/failed) with json_valid/json_extract on the same row,
    reusing the exact SQL snippets the batched scan runs. Only the first chunk
    that has parts is probed (new rows land in recently updated sessions, so a
    serialization change always shows up there); that, plus an id-only indexed
    window query, keeps the check to a few milliseconds per call.
    """
    for chunk in _chunks(session_ids, SCAN_CHUNK):
        sids = ",".join("?" * len(chunk))
        sampled = [
            row[0] for row in conn.execute(
                f"SELECT id FROM part WHERE session_id IN ({sids})"
                f" ORDER BY time_updated DESC LIMIT {LAYOUT_SAMPLE_PARTS}",
                chunk,
            )
        ]
        if not sampled:
            continue  # newest sessions empty: older ones may still hold parts
        ids = ",".join("?" * len(sampled))
        args = [*ERROR_STATUS_TAGS, TOOL_PREFIX, STEP_FINISH_PREFIX, STATUS_TAG, *sampled]
        for is_tool, is_sf, fast_err, exact_tool, exact_sf, exact_err in conn.execute(
            _LAYOUT_PROBE_SQL.replace("{ids}", ids), args
        ):
            # bool(): a NULL blob gives NULL prefix tests but json_valid() = 0
            if tuple(map(bool, (is_tool, is_sf, fast_err))) != tuple(map(bool, (exact_tool, exact_sf, exact_err))):
                return False
        return True
    return True


def _summarize_exact(conn: sqlite3.Connection, row: tuple[Any, ...], now_ms: int) -> SessionSummary:
    """Per-session summary via json_extract only (the pre-batch logic).

    Used when the layout self-check fails: slower, but immune to key order,
    whitespace or nested "status" keys. The latest part is picked with the same
    packed expression the batched scan uses so both paths choose the same row.
    """
    sid, directory, title, created, updated, parent = row
    last_part, tools, errors = conn.execute(
        """
        SELECT MAX(time_updated),
               SUM(CASE WHEN json_valid(data) THEN
                     CASE WHEN json_extract(data, '$.type') = 'tool' THEN 1 ELSE 0 END ELSE 0 END),
               SUM(CASE WHEN json_valid(data) THEN
                     CASE WHEN json_extract(data, '$.type') = 'tool'
                           AND json_extract(data, '$.state.status') IN ('error', 'failed')
                     THEN 1 ELSE 0 END ELSE 0 END)
        FROM part WHERE session_id = ?
        """,
        (sid,),
    ).fetchone()
    turn = conn.execute(
        "SELECT status, COALESCE(completed_at, started_at) AS ended FROM turn_usage"
        " WHERE session_id = ? ORDER BY ended DESC LIMIT 1",
        (sid,),
    ).fetchone()
    duration = conn.execute(
        "SELECT COALESCE(SUM(duration_ms), 0) FROM turn_usage WHERE session_id = ?", (sid,)
    ).fetchone()[0]
    latest = conn.execute(
        "SELECT data FROM part WHERE session_id = ?"
        " ORDER BY printf('%015d%015d', time_updated, time_created) || hex(substr(data,1,44)) DESC LIMIT 1",
        (sid,),
    ).fetchone()
    context: int | None = None
    for (data,) in conn.execute(
        "SELECT data FROM part WHERE session_id = ? AND data LIKE '%step-finish%'"
        " ORDER BY time_created DESC LIMIT 5",
        (sid,),
    ):
        part = _load(data)
        total = (part.get("tokens") or {}).get("total") if part.get("type") == "step-finish" else None
        if isinstance(total, int):
            context = total
            break
    return SessionSummary(
        id=sid,
        directory=directory or "",
        title=_clip(_first_line(title), TITLE_CHARS),
        created_ms=int(created or 0),
        updated_ms=max(int(updated or 0), int(last_part or 0)),
        status=_status(
            last_part,
            _load(latest[0]) if latest else {},
            (turn[0] or "", int(turn[1] or 0)) if turn else None,
            now_ms,
        ),
        tool_calls=int(tools or 0),
        errors=int(errors or 0),
        last_context_tokens=context,
        duration_ms=int(duration or 0),
        parent_id=parent,
    )


def _fetch_part_data(conn: sqlite3.Connection, pids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for chunk in _chunks(pids, SCAN_CHUNK):
        ids = ",".join("?" * len(chunk))
        for pid, data in conn.execute(f"SELECT id, data FROM part WHERE id IN ({ids})", chunk):
            out[pid] = data
    return out


def _decode_latest_pack(pack: str | None) -> dict[str, Any] | None:
    """The newest part as far as _status cares, decoded from its packed prefix.

    ``{}`` when the part cannot be a step-finish (only its type is then read),
    ``{"type": "step-finish", "reason": ...}`` when the standard layout holds,
    and None when a step-finish part deviates and the full row is needed.
    """
    if pack is None:
        return {}
    sig = bytes.fromhex(pack[PACK_KEY_LEN:]).decode("utf-8", "replace")
    if not sig.startswith(STEP_FINISH_PREFIX):
        return {}
    if sig[21:32] == ',"reason":"':
        end = sig.find('"', 32)
        if end > 0:
            return {"type": "step-finish", "reason": sig[32:end]}
    return None


def _summarize_many(conn: sqlite3.Connection, rows: list[tuple[Any, ...]], now_ms: int) -> list[SessionSummary]:
    """SessionSummary per row, batched: one grouped part scan and turn scan per chunk.

    Runs the layout self-check once per call first; a non-standard part layout
    (ZCode changed serialization) recomputes this call exactly per session.
    """
    global LAYOUT_DEGRADED
    if rows and not _layout_ok(conn, [row[0] for row in rows]):
        LAYOUT_DEGRADED = True
        return [_summarize_exact(conn, row, now_ms) for row in rows]
    scan: dict[str, tuple[Any, ...]] = {}
    turns: dict[str, tuple[Any, ...]] = {}
    for chunk in _chunks([row[0] for row in rows], SCAN_CHUNK):
        ids = ",".join("?" * len(chunk))
        args = [*ERROR_STATUS_TAGS, TOOL_PREFIX, STEP_FINISH_PREFIX, STATUS_TAG, *chunk]
        for sid, last_upd, tools, errors, candidates, pack in conn.execute(
            _PART_SCAN_SQL.replace("{ids}", ids), args
        ):
            scan[sid] = (last_upd, tools, errors, candidates, pack)
        for sid, status, ended, _max_ended, duration in conn.execute(
            _TURN_SCAN_SQL.replace("{ids}", ids), chunk
        ):
            turns[sid] = (status, ended, duration)

    # Five newest step-finish candidates per session in the order the previous
    # per-session query produced: time_created DESC, ties in index-scan order
    # (group_concat emits rows in that order and the sort below is stable).
    cand_pids: dict[str, list[str]] = {}
    want: list[str] = []
    for sid, (_last_upd, _tools, _errors, candidates, _pack) in scan.items():
        if not candidates:
            continue
        entries = [entry.split(".", 1) for entry in candidates.split(";")]
        entries.sort(key=lambda e: -int(e[0]))
        picked = [pid for _created, pid in entries[:5]]
        cand_pids[sid] = picked
        want.extend(picked)
    cand_data = _fetch_part_data(conn, want)

    # The newest part matters only when no turn_usage row covers it; its packed
    # prefix decides, with the exact per-session query kept for the rare
    # step-finish part whose layout deviates from the compact norm.
    latest_part: dict[str, dict[str, Any]] = {}
    for sid, (_last_upd, _tools, _errors, _candidates, pack) in scan.items():
        decoded = _decode_latest_pack(pack)
        if decoded is not None:
            latest_part[sid] = decoded
        else:
            latest = conn.execute(
                "SELECT data FROM part WHERE session_id = ?"
                " ORDER BY time_updated DESC, time_created DESC LIMIT 1",
                (sid,),
            ).fetchone()
            latest_part[sid] = _load(latest[0]) if latest else {}

    out: list[SessionSummary] = []
    for row in rows:
        sid, directory, title, created, updated, parent = row
        last_upd, tools, errors, _candidates, _pack = scan.get(sid, (None, 0, 0, None, None))
        turn = turns.get(sid)
        context: int | None = None
        for pid in cand_pids.get(sid, ()):
            part = _load(cand_data.get(pid))
            total = (part.get("tokens") or {}).get("total") if part.get("type") == "step-finish" else None
            if isinstance(total, int):
                context = total
                break
        out.append(SessionSummary(
            id=sid,
            directory=directory or "",
            title=_clip(_first_line(title), TITLE_CHARS),
            created_ms=int(created or 0),
            updated_ms=max(int(updated or 0), int(last_upd or 0)),
            status=_status(
                last_upd,
                latest_part.get(sid, {}),
                (turn[0] or "", int(turn[1] or 0)) if turn else None,
                now_ms,
            ),
            tool_calls=int(tools or 0),
            errors=int(errors or 0),
            last_context_tokens=context,
            duration_ms=int(turn[2] or 0) if turn else 0,
            parent_id=parent,
        ))
    return out


def session_summary(db: Db, session_id: str, *, now_ms: int | None = None) -> SessionSummary:
    """Summary for one session (id or unique prefix) via the same status logic
    ``list_sessions`` uses; ActivityError when the id is unknown or ambiguous."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    with _session(db) as conn:
        sid = _resolve(conn, session_id.strip())
        row = conn.execute(
            "SELECT id, directory, title, time_created, time_updated, parent_id FROM session WHERE id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            raise ActivityError(f"no ZCode session matches {session_id!r}")
        return _summarize_many(conn, [row], now)[0]


# --- activity ---------------------------------------------------------------------------


def _parse_cursor(cursor: str | None) -> tuple[int, set[str], dict[str, str]]:
    if not cursor:
        return -1, set(), {}
    try:
        value = json.loads(cursor)
        return int(value["t"]), set(value.get("at", [])), dict(value.get("live", {}))
    except (ValueError, TypeError, KeyError):
        raise ActivityError(f"invalid activity cursor: {cursor[:40]!r}") from None


def _make_cursor(t: int, at: set[str], live: dict[str, str]) -> str:
    # No size cap: an entry leaves ``live`` only when its tool reaches a terminal
    # status; evicting a still-running one would re-emit it on its next update.
    return json.dumps({"t": t, "at": sorted(at), "live": live}, separators=(",", ":"))


def session_activity(
    db: Db,
    session_id: str,
    *,
    after: str | None = None,
) -> tuple[list[Activity], str]:
    """Level-2 activity for a session (id or unique prefix) plus a cursor.

    With ``after`` set to a previous cursor only new/updated items are returned.
    A still-running tool is not repeated until its status changes.
    """
    since, seen_at, live = _parse_cursor(after)
    with _session(db) as conn:
        sid = _resolve(conn, session_id.strip())
        directory = (conn.execute("SELECT directory FROM session WHERE id = ?", (sid,)).fetchone() or [""])[0]
        roles = {
            mid: _load(data).get("role")
            for mid, data in conn.execute("SELECT id, data FROM message WHERE session_id = ?", (sid,))
        }
        # (ts, key, row kind, payload)
        rows: list[tuple[int, str, str, Any]] = []
        for pid, mid, created, updated, data in conn.execute(
            "SELECT id, message_id, time_created, time_updated, data FROM part"
            " WHERE session_id = ? AND time_updated >= ? ORDER BY time_updated, time_created, id",
            (sid, since),
        ):
            rows.append((int(updated or 0), pid, "part", (mid, int(created or 0), data)))
        for turn_id, status, ended, count, errs, duration in conn.execute(
            "SELECT turn_id, status, COALESCE(completed_at, started_at) AS ended,"
            " tool_call_count, tool_error_count, duration_ms FROM turn_usage"
            " WHERE session_id = ? AND COALESCE(completed_at, started_at) >= ?",
            (sid, since),
        ):
            rows.append((int(ended or 0), f"turn:{turn_id}", "turn", (status, count, errs, duration)))

    out: list[tuple[int, int, Activity]] = []
    new_t, new_at = since, set(seen_at)
    for order, (ts, key, rkind, payload) in enumerate(sorted(rows, key=lambda r: (r[0], r[1]))):
        if ts > new_t:
            new_t, new_at = ts, set()
        if ts == new_t:
            new_at.add(key)
        if ts == since and key in seen_at:
            continue
        if rkind == "turn":
            out.append((ts, order, _turn_activity(sid, key, ts, *payload)))
            continue
        mid, created, data = payload
        act = _part_activity(sid, key, created, ts, _load(data), roles.get(mid), directory)
        if act is None:
            continue
        if act.status in LIVE_STATUSES:
            if live.get(key) == act.status:
                continue
            live[key] = act.status
        else:
            live.pop(key, None)
        out.append((act.at_ms, order, act))
    out.sort(key=lambda item: (item[0], item[1]))
    return [a for _, _, a in out], _make_cursor(new_t, new_at, live)


def _turn_activity(sid: str, key: str, ts: int, status: Any, count: Any, errs: Any, duration: Any) -> Activity:
    status = str(status or "unknown")
    secs = f", {int(duration or 0) / 1000:.1f}s" if duration else ""
    summary = f"turn {status}: {int(count or 0)} tool call(s), {int(errs or 0)} error(s){secs}"
    return Activity(ts, sid, "turn_end", status, summary, key=key)


def _part_activity(
    sid: str,
    key: str,
    created: int,
    updated: int,
    part: dict[str, Any],
    role: Any,
    directory: str,
) -> Activity | None:
    ptype = part.get("type")
    if ptype == "tool":
        return _tool_activity(sid, key, created, updated, part, directory)
    if ptype == "step-finish":
        tokens = part.get("tokens") or {}
        total = tokens.get("total") if isinstance(tokens, dict) else None
        ctx = f"context {total:,} tokens" if isinstance(total, int) else "context ?"
        return Activity(updated, sid, "step", "completed", f"step end ({part.get('reason') or '?'}), {ctx}", key=key)
    if ptype == "text" and role == "assistant":
        timing = part.get("time")
        if isinstance(timing, dict) and not timing.get("end"):
            return None  # still streaming; emitted once it has ended
        text = _clip(part.get("text") or "", MESSAGE_CHARS)
        return Activity(updated, sid, "message", "completed", text, key=key) if text else None
    return None  # reasoning, step-start, timeline, user text, files...


def _tool_activity(
    sid: str,
    key: str,
    created: int,
    updated: int,
    part: dict[str, Any],
    directory: str,
) -> Activity:
    tool = str(part.get("tool") or "?")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = str(state.get("status") or "unknown")
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    at = created if status in LIVE_STATUSES else updated
    detail: str | None = None
    if status in FAILED_STATUSES:
        detail = _clip(_first_line(state.get("error") or state.get("output")), OUTCOME_CHARS) or None

    if tool in FILE_KINDS:
        kind = FILE_KINDS[tool]
        target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "?"
        summary = display_path(str(target), directory)
        if tool == "Edit" and isinstance(inp.get("old_string"), str) and isinstance(inp.get("new_string"), str):
            summary += f" (+{len(inp['new_string'].splitlines())} -{len(inp['old_string'].splitlines())})"
        elif tool == "Write" and isinstance(inp.get("content"), str):
            summary += f" ({len(inp['content'].splitlines())} lines)"
    elif tool in SHELL_TOOLS:
        kind = "command"
        summary = _clip(_strip_cd(str(inp.get("command") or ""), directory), COMMAND_CHARS)
        if status == "completed":
            detail = _clip(_last_line(state.get("output")), OUTCOME_CHARS) or None
            if detail:
                summary += f" -> {detail}"
    elif tool in SEARCH_TOOLS:
        kind = "search"
        where = inp.get("path")
        summary = _clip(str(inp.get("pattern") or ""), TOOL_CHARS)
        if where:
            summary += f" in {display_path(str(where), directory)}"
    else:
        kind = "tool"
        summary = _clip(str(inp.get("description") or state.get("title") or ""), TOOL_CHARS)
    if detail and status in FAILED_STATUSES:
        summary += f" !! {detail}"
    return Activity(at, sid, kind, status, summary, tool=tool, detail=detail, key=key)
