"""zcrew runs / show / watch - read-only terminal view of ZCode worker sessions.

Thin renderers over zcode_activity.py (same directory): ``runs`` lists sessions
as an aligned table, ``show`` prints one session's activity timeline, ``watch``
follows live activity with one line per event prefixed by a stable w1/w2/...
worker label. Colour is applied to worker labels and session statuses only, and
only when stdout is a TTY without NO_COLOR set (on Windows VT processing is
enabled via SetConsoleMode, silently falling back to no colour). Everything is
read-only; the database is only ever opened through zcode_activity.connect.
"""

from __future__ import annotations

import dataclasses
import errno
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

SHORT_ID = 13
LINE_WIDTH = 100
WATCH_SESSION_LIMIT = 200

ICONS = {
    "file_read": "R",
    "file_edit": "E",
    "file_write": "E",
    "command": "$",
    "search": "?",
    "tool": "*",
    "step": ".",
    "turn_end": "#",
    "message": ">",
}
LABELS = {
    "file_read": "read",
    "file_edit": "edit",
    "file_write": "write",
    "command": "cmd",
    "search": "search",
    "tool": "tool",
    "step": "step",
    "turn_end": "turn",
    "message": "msg",
}
STATUS_COLOURS = {"running": "32", "completed": "36", "failed": "31", "idle": "90"}
PALETTE = ("36", "95", "32", "33", "94", "35", "92", "96")

Clock = Callable[[], int]
_Sleep = Callable[[float], None]

# One stderr line per process when zcode_activity fell back to exact parsing.
NOTE_LAYOUT = "zcrew: note: ZCode data layout changed; using slower exact mode"
_layout_note_shown = False

_LIB_NAME = "_zcrew_zcode_activity"


def _library() -> ModuleType:
    """Load zcode_activity.py next to this file (same name zcrew.py would use)."""
    module = sys.modules.get(_LIB_NAME)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(_LIB_NAME, Path(__file__).resolve().parent / "zcode_activity.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load zcode_activity.py next to {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


# --- colour ----------------------------------------------------------------------------


_VT_STATE: bool | None = None


def _use_colour(out: Any, env: Mapping[str, str]) -> bool:
    if (env.get("NO_COLOR") or "").strip():
        return False
    if not getattr(out, "isatty", lambda: False)():
        return False
    return _vt_ready()


def _vt_ready() -> bool:
    global _VT_STATE
    if _VT_STATE is None:
        _VT_STATE = _probe_vt()
    return _VT_STATE


def _probe_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enabled_virtual_terminal = 0x0004
        if mode.value & enabled_virtual_terminal:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enabled_virtual_terminal))
    except Exception:
        return False


def _paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def _paint_label(label: str, use_colour: bool) -> str:
    if not use_colour:
        return label
    index = int(label[1:]) if len(label) > 1 and label[1:].isdigit() else 1
    return _paint(label, PALETTE[(index - 1) % len(PALETTE)])


def _layout_note(za: ModuleType, err: Any) -> None:
    """Emit the exact-mode note once per process (stderr, never stdout)."""
    global _layout_note_shown
    if not _layout_note_shown and getattr(za, "LAYOUT_DEGRADED", False):
        _layout_note_shown = True
        err.write(NOTE_LAYOUT + "\n")
        if hasattr(err, "flush"):
            err.flush()


# --- formatting helpers -----------------------------------------------------------------


def _hms(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000))


def _fmt_started(ms: int, now_ms: int) -> str:
    stamp, now = time.localtime(ms / 1000), time.localtime(now_ms / 1000)
    return time.strftime("%H:%M" if stamp[:3] == now[:3] else "%Y-%m-%d %H:%M", stamp)


def _fmt_duration(ms: int) -> str:
    secs = max(0, ms) / 1000
    if secs < 60:
        return f"{secs:.1f}s"
    minutes, secs = divmod(int(secs), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _line_text(act: Any) -> str:
    text = act.summary
    if act.detail and act.detail not in text:
        text += f" -> {act.detail}"
    if act.status in ("running", "pending"):
        text += " ..."
    elif act.status in ("error", "failed"):
        text = "x " + text
    return text


def _clip_cell(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 3)].rstrip() + "..."


def _db_path(za: ModuleType, env: Mapping[str, str]) -> Path:
    path = za.default_db_path(env)
    if not path.is_file():
        raise za.ActivityError(
            f"ZCode database not found: {path}. ZCode may not be installed or has not run yet "
            f"(set {za.ENV_DB} to point at one)."
        )
    return path


def _is_pipe_error(exc: BaseException) -> bool:
    return isinstance(exc, BrokenPipeError) or exc.errno in (errno.EPIPE, errno.EINVAL)  # type: ignore[attr-defined]


def _silence_stdout(out: Any) -> None:
    """After the reader of a pipe exited (`zcrew ... | head`), point the real
    stdout fd at devnull so interpreter shutdown flushes nothing and prints no
    'Exception ignored' note (the docs' SIGPIPE recommendation)."""
    if out is not sys.stdout:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


# --- runs -------------------------------------------------------------------------------


RUNS_HEADER = ("ID", "STATUS", "STARTED", "DURATION", "TOOLS", "ERR", "CONTEXT", "TITLE")


def _table_line(row: list[str], widths: list[int], paint: dict[int, str] | None = None) -> str:
    cells = []
    for i, cell in enumerate(row):
        padded = cell.ljust(widths[i]) if i < len(row) - 1 else cell
        cells.append(_paint(padded, (paint or {}).get(i, "")))
    return "  ".join(cells).rstrip()


def run_runs(
    za: ModuleType,
    db: Any,
    *,
    cwd: str | None,
    limit: int,
    as_json: bool = False,
    out: Any = None,
    env: Mapping[str, str] | None = None,
    now_ms: int | None = None,
) -> int:
    summaries = za.list_sessions(db, cwd=cwd, limit=max(1, int(limit)), now_ms=now_ms)
    if as_json:
        out.write(json.dumps([dataclasses.asdict(s) for s in summaries], indent=2) + "\n")
        return 0
    if not summaries:
        scope = "all directories" if cwd is None else str(cwd)
        out.write(f"no sessions found ({scope})\n")
        return 0
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rows = [list(RUNS_HEADER)]
    for s in summaries:
        rows.append([
            s.id[:SHORT_ID],
            s.status,
            _fmt_started(s.created_ms, now),
            _fmt_duration(s.duration_ms),
            str(s.tool_calls),
            str(s.errors),
            "-" if s.last_context_tokens is None else f"{s.last_context_tokens:,}",
            s.title or "(no title)",
        ])
    widths = [max(len(row[i]) for row in rows) for i in range(len(RUNS_HEADER))]
    fixed = sum(widths[:-1]) + 2 * (len(widths) - 1)
    widths[-1] = min(widths[-1], max(20, LINE_WIDTH - fixed))
    for row in rows:
        row[-1] = _clip_cell(row[-1], widths[-1])
    use_colour = _use_colour(out, env or {})
    out.write(_table_line(rows[0], widths) + "\n")
    for row in rows[1:]:
        paint = {1: STATUS_COLOURS[row[1]]} if use_colour and row[1] in STATUS_COLOURS else None
        out.write(_table_line(row, widths, paint) + "\n")
    return 0


# --- show -------------------------------------------------------------------------------


def run_show(
    za: ModuleType,
    db: Any,
    session: str,
    *,
    as_json: bool = False,
    out: Any = None,
    env: Mapping[str, str] | None = None,
    now_ms: int | None = None,
) -> int:
    summary = za.session_summary(db, session, now_ms=now_ms)
    acts, _cursor = za.session_activity(db, summary.id)
    if as_json:
        payload = {
            **dataclasses.asdict(summary),
            "activity": [
                {"at_ms": a.at_ms, "kind": a.kind, "status": a.status, "summary": a.summary,
                 "tool": a.tool, "detail": a.detail}
                for a in acts
            ],
        }
        out.write(json.dumps(payload, indent=2) + "\n")
        return 0
    use_colour = _use_colour(out, env or {})
    out.write(f"session   {summary.id}\n")
    out.write(f"title     {summary.title or '(no title)'}\n")
    out.write(f"dir       {summary.directory or '?'}\n")
    out.write(f"status    {_paint(summary.status, STATUS_COLOURS.get(summary.status, '') if use_colour else '')}\n\n")
    for act in acts:
        out.write(f"{_hms(act.at_ms)}  {ICONS.get(act.kind, '*')} {LABELS.get(act.kind, act.kind):<6}  {_line_text(act)}\n")
    if not acts:
        out.write("(no activity yet)\n")
    return 0


# --- watch ------------------------------------------------------------------------------


def run_watch(
    za: ModuleType,
    db: Any,
    *,
    cwd: str | None,
    interval: float,
    since_ms: float,
    out: Any = None,
    err: Any = None,
    env: Mapping[str, str] | None = None,
    sleep: _Sleep = time.sleep,
    clock: Clock | None = None,
    max_polls: int | None = None,
) -> int:
    err = sys.stderr if err is None else err
    clock = clock or (lambda: int(time.time() * 1000))
    use_colour = _use_colour(out, env or {})
    target = "all directories" if cwd is None else str(cwd)
    out.write(f"watching {target} — Ctrl+C to stop\n")
    labels: dict[str, str] = {}
    cursors: dict[str, str | None] = {}
    bounds: dict[str, int] = {}
    # A session first seen late only prints activity recent relative to that
    # first sight, not to watch start; its cursor still advances past the rest.
    polls = 0
    try:
        while max_polls is None or polls < int(max_polls):
            now = int(clock())
            # Ask the database for recent sessions only: a running session is
            # always fresh (its session row is updated with its parts), so
            # STALE_MS is a safe floor for the window next to --since.
            window = int(max(since_ms, za.STALE_MS))
            sessions = [
                s for s in za.list_sessions(
                    db, cwd=cwd, limit=WATCH_SESSION_LIMIT, now_ms=now, since_ms=now - window)
                if s.updated_ms >= now - since_ms or s.status == "running"
            ]
            for s in sessions:
                first = s.id not in labels
                if first:
                    labels[s.id] = f"w{len(labels) + 1}"
                    cursors[s.id] = None
                    bounds[s.id] = int(now - since_ms)
                    verb = "started" if s.created_ms >= bounds[s.id] else "active"
                    out.write(f"{_hms(s.created_ms)} {_paint_label(labels[s.id], use_colour)} {s.id[:SHORT_ID]}"
                              f" > {verb}: {s.title or '(no title)'}\n")
                acts, cursor = za.session_activity(db, s.id, after=cursors[s.id])
                cursors[s.id] = cursor
                if first:
                    acts = [a for a in acts if a.at_ms >= bounds[s.id]]
                label = _paint_label(labels[s.id], use_colour)
                for act in acts:
                    out.write(f"{_hms(act.at_ms)} {label} {ICONS.get(act.kind, '*')} {_line_text(act)}\n")
            if hasattr(out, "flush"):
                out.flush()
            _layout_note(za, err)  # during a long watch the note shows up promptly
            polls += 1
            if max_polls is not None and polls >= int(max_polls):
                break
            sleep(max(0.0, interval))
    except KeyboardInterrupt:
        pass
    return 0


# --- entry point ------------------------------------------------------------------------


def run(
    args: Any,
    *,
    env: Mapping[str, str] | None = None,
    out: Any = None,
    err: Any = None,
    sleep: _Sleep = time.sleep,
    clock: Clock | None = None,
    max_polls: int | None = None,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    # Thai titles must never crash a non-UTF-8 console or pipe (same as doctor.py).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    za = _library()
    try:
        db = _db_path(za, env)
        if args.command == "runs":
            rc = run_runs(
                za, db,
                cwd=None if args.all else (args.dir or os.getcwd()),
                limit=args.limit, as_json=args.json, out=out, env=env,
                now_ms=int(clock()) if clock else None,
            )
        elif args.command == "show":
            rc = run_show(
                za, db, args.session, as_json=args.json, out=out, env=env,
                now_ms=int(clock()) if clock else None,
            )
        elif args.command == "watch":
            rc = run_watch(
                za, db,
                cwd=None if args.all else (args.dir or os.getcwd()),
                interval=float(args.interval), since_ms=max(0.0, float(args.since)) * 60_000,
                out=out, err=err, env=env, sleep=sleep, clock=clock, max_polls=max_polls,
            )
        else:
            raise AssertionError(args.command)
        _layout_note(za, err)
        return rc
    except za.ActivityError as exc:
        err.write(f"zcrew: {exc}\n")
        return 1
    except OSError as exc:
        if not _is_pipe_error(exc):
            raise
        _silence_stdout(out)
        return 0
