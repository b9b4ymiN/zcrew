"""zcrew context - report where a commander session's context went.

Reads (never writes) the commander's own session transcript and aggregates:
context size per model turn (from API usage), and tool-output size per tool.
The report contains numbers and tool names only - never transcript text.

Claude Code: ~/.claude/projects/<slug>/<session>.jsonl, slug = absolute project
path with every character outside [A-Za-z0-9] replaced by '-'.

Codex (best effort): rollout-*.jsonl under $CODEX_HOME/sessions or
~/.codex/sessions. A session is matched to DIR by the ``cwd`` recorded in its
``session_meta`` / ``turn_context`` entry; old rollouts that record no cwd are
skipped (pass --session PATH for those). Sub-agent rollouts are skipped.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

DEFAULT_TOP = 12
CHARS_PER_TOKEN = 4
UNKNOWN_TOOL = "(unknown)"

# Hint thresholds (numbers-based only).
SHELL_SHARE_HINT = 30.0
READ_AVG_HINT = 8000
WAIT_CALLS_HINT = 10
PEAK_CONTEXT_HINT = 150_000

SHELL_TOOLS = {"Bash", "PowerShell", "exec", "exec_command", "shell", "shell_command"}
WAIT_TOOL = "mcp__zcode_executor__agent-wait"
CODEX_HEADER_LINES = 200


class ContextError(Exception):
    """User-facing error (no transcript, bad --session...)."""


# --- locating transcripts -------------------------------------------------------------


def project_slug(project: str | os.PathLike[str]) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.fspath(project)))


def claude_projects_dir(env: Mapping[str, str]) -> Path:
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def claude_project_dir(project: str, env: Mapping[str, str]) -> Path | None:
    root = claude_projects_dir(env)
    slug = project_slug(project)
    exact = root / slug
    if exact.is_dir():
        return exact
    if root.is_dir():  # drive letter / case differences on Windows
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() == slug.lower():
                return child
    return None


def _latest(files: Iterable[Path]) -> Path | None:
    files = [f for f in files if f.is_file()]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def find_claude_session(project: str, session: str | None, env: Mapping[str, str]) -> Path:
    if session and (os.sep in session or "/" in session or session.endswith(".jsonl")):
        path = Path(session)
        if not path.is_file():
            raise ContextError(f"session file not found: {session}")
        return path
    pdir = claude_project_dir(project, env)
    if pdir is None:
        raise ContextError(
            f"no Claude Code transcripts for {os.path.abspath(project)} "
            f"(looked for {claude_projects_dir(env) / project_slug(project)})"
        )
    if session:
        path = pdir / f"{session}.jsonl"
        if not path.is_file():
            raise ContextError(f"session {session} not found for {os.path.abspath(project)}")
        return path
    latest = _latest(pdir.glob("*.jsonl"))
    if latest is None:
        raise ContextError(f"no Claude Code transcripts in {pdir}")
    return latest


def codex_sessions_dir(env: Mapping[str, str]) -> Path:
    base = env.get("CODEX_HOME")
    return (Path(base) if base else Path.home() / ".codex") / "sessions"


def _codex_header(path: Path) -> tuple[str | None, bool]:
    """(cwd, is_subagent) from the first session_meta / turn_context entry."""
    cwd: str | None = None
    subagent = False
    for i, entry in enumerate(iter_jsonl(path)):
        if i >= CODEX_HEADER_LINES:
            break
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry.get("type") == "session_meta":
            source = payload.get("source")
            subagent = isinstance(source, dict) and "subagent" in source
            if isinstance(payload.get("cwd"), str):
                return payload["cwd"], subagent
        elif entry.get("type") == "turn_context" and isinstance(payload.get("cwd"), str):
            return payload["cwd"], subagent
    return cwd, subagent


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)).rstrip("\\/") == os.path.normcase(os.path.abspath(b)).rstrip("\\/")


def find_codex_session(project: str, session: str | None, env: Mapping[str, str]) -> Path:
    if session and (os.sep in session or "/" in session or session.endswith(".jsonl")):
        path = Path(session)
        if not path.is_file():
            raise ContextError(f"session file not found: {session}")
        return path
    root = codex_sessions_dir(env)
    files = sorted(root.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True) if root.is_dir() else []
    if session:
        for f in files:
            if session in f.stem:
                return f
        raise ContextError(f"Codex session {session} not found under {root}")
    for f in files:
        cwd, subagent = _codex_header(f)
        if cwd and not subagent and _same_dir(cwd, project):
            return f
    raise ContextError(
        f"no Codex session with cwd {os.path.abspath(project)} under {root} "
        "(old rollouts record no cwd: pass --session PATH)"
    )


# --- parsing ----------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self.skipped = 0
        self.timestamps: list[str] = []
        self.session_id: str | None = None
        self.turns: list[int] = []  # context tokens per model turn
        self.output_tokens = 0
        self.tool_names: dict[str, str] = {}  # call id -> tool name
        self.calls: dict[str, int] = {}
        self.chars: dict[str, int] = {}
        self.max_chars: dict[str, int] = {}
        self.images: dict[str, int] = {}

    def add_call(self, call_id: str | None, name: str) -> None:
        if call_id:
            self.tool_names[call_id] = name
        self.calls[name] = self.calls.get(name, 0) + 1

    def add_result(self, call_id: str | None, chars: int, images: int) -> None:
        name = self.tool_names.get(call_id or "", UNKNOWN_TOOL)
        if name == UNKNOWN_TOOL:
            self.calls.setdefault(name, 0)
            self.calls[name] += 1
        self.chars[name] = self.chars.get(name, 0) + chars
        self.max_chars[name] = max(self.max_chars.get(name, 0), chars)
        if images:
            self.images[name] = self.images.get(name, 0) + images


def iter_jsonl(path: Path, stats: Stats | None = None) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                if stats is not None:
                    stats.skipped += 1
                continue
            if isinstance(entry, dict):
                yield entry
            elif stats is not None:
                stats.skipped += 1


def content_size(content: Any, text_types: tuple[str, ...] = ("text",)) -> tuple[int, int]:
    """(text chars, image count) of a tool result's content (string or list of blocks)."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                chars += len(block)
            elif isinstance(block, dict):
                kind = block.get("type")
                if kind in text_types and isinstance(block.get("text"), str):
                    chars += len(block["text"])
                elif kind in ("image", "input_image"):
                    images += 1
    return chars, images


def parse_claude(path: Path) -> Stats:
    stats = Stats()
    seen_messages: set[str] = set()
    for entry in iter_jsonl(path, stats):
        if isinstance(entry.get("timestamp"), str):
            stats.timestamps.append(entry["timestamp"])
        if stats.session_id is None and isinstance(entry.get("sessionId"), str):
            stats.session_id = entry["sessionId"]
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if entry.get("type") == "assistant":
            # One API response is written as several entries (one per content block)
            # carrying the same message id and usage: count it once.
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            usage = message.get("usage")
            if isinstance(usage, dict) and key not in seen_messages:
                if key:
                    seen_messages.add(key)
                ctx = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                stats.turns.append(ctx)
                stats.output_tokens += int(usage.get("output_tokens") or 0)
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    stats.add_call(block.get("id"), str(block.get("name") or UNKNOWN_TOOL))
        elif entry.get("type") == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    chars, images = content_size(block.get("content"))
                    stats.add_result(block.get("tool_use_id"), chars, images)
    return stats


def _codex_tool_name(payload: Mapping[str, Any]) -> str:
    name = str(payload.get("name") or UNKNOWN_TOOL)
    namespace = payload.get("namespace")
    if isinstance(namespace, str) and namespace.startswith("mcp__"):
        return f"{namespace.rstrip('_')}__{name}"
    return name


def parse_codex(path: Path) -> Stats:
    stats = Stats()
    last_total: Any = None
    for entry in iter_jsonl(path, stats):
        if isinstance(entry.get("timestamp"), str):
            stats.timestamps.append(entry["timestamp"])
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if entry.get("type") == "session_meta" and stats.session_id is None:
            sid = payload.get("id") or payload.get("session_id")
            stats.session_id = sid if isinstance(sid, str) else None
        elif kind in ("function_call", "custom_tool_call"):
            stats.add_call(payload.get("call_id"), _codex_tool_name(payload))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            chars, images = content_size(payload.get("output"), ("text", "input_text", "output_text"))
            stats.add_result(payload.get("call_id"), chars, images)
        elif kind == "token_count" and isinstance(payload.get("info"), dict):
            info = payload["info"]
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if not isinstance(last, dict) or total == last_total:
                continue  # repeated snapshot, not a new model call
            last_total = total
            # OpenAI usage: cached_input_tokens is a subset of input_tokens.
            stats.turns.append(int(last.get("input_tokens") or 0))
            stats.output_tokens += int(last.get("output_tokens") or 0)
    return stats


# --- report -------------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def mcp_server(name: str) -> str | None:
    if name.startswith("mcp__"):
        rest = name[len("mcp__"):]
        server = rest.split("__", 1)[0]
        return server or None
    return None


def hints_for(report: Mapping[str, Any]) -> list[str]:
    tools = {t["name"]: t for t in report["tools_all"]}
    hints: list[str] = []
    shell_share = sum(t["share"] for n, t in tools.items() if n in SHELL_TOOLS)
    if shell_share > SHELL_SHARE_HINT:
        hints.append(
            f"shell output is {shell_share:.0f}% of tool output: long command output - redirect to a "
            "log file and read the tail (policy: Context hygiene)"
        )
    read = tools.get("Read")
    if read and read["avg"] > READ_AVG_HINT:
        hints.append(f"Read averages {read['avg']:,} chars per call: read ranges, not whole files")
    wait = tools.get(WAIT_TOOL)
    if wait and wait["calls"] > WAIT_CALLS_HINT:
        hints.append(
            f"{wait['calls']} agent-wait calls: update zcrew (economical waiting) - run: zcrew update"
        )
    peak = report["context_tokens"]["peak"]
    if peak > PEAK_CONTEXT_HINT:
        hints.append(
            f"context peaked at {peak:,} tokens: summarize before continuing, or start a fresh session "
            "for the next task"
        )
    return hints


def build_report(stats: Stats, commander: str, session_file: Path, top: int = DEFAULT_TOP) -> dict[str, Any]:
    total_chars = sum(stats.chars.values())
    names = set(stats.calls) | set(stats.chars)
    rows = []
    for name in names:
        calls = stats.calls.get(name, 0)
        chars = stats.chars.get(name, 0)
        rows.append({
            "name": name,
            "calls": calls,
            "chars": chars,
            "avg": chars // calls if calls else 0,
            "share": round(100.0 * chars / total_chars, 1) if total_chars else 0.0,
            "max": stats.max_chars.get(name, 0),
            "images": stats.images.get(name, 0),
        })
    rows.sort(key=lambda r: (-r["chars"], -r["calls"], r["name"]))

    servers: dict[str, dict[str, Any]] = {}
    for row in rows:
        server = mcp_server(row["name"])
        if server:
            agg = servers.setdefault(server, {"server": server, "calls": 0, "chars": 0})
            agg["calls"] += row["calls"]
            agg["chars"] += row["chars"]
    for agg in servers.values():
        agg["share"] = round(100.0 * agg["chars"] / total_chars, 1) if total_chars else 0.0

    times = [t for t in (_parse_ts(s) for s in stats.timestamps) if t is not None]
    start = min(times) if times else None
    end = max(times) if times else None
    top = max(0, top)
    report: dict[str, Any] = {
        "commander": commander,
        "session_id": stats.session_id or session_file.stem,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "duration_s": int((end - start).total_seconds()) if start and end else None,
        "turns": len(stats.turns),
        "context_tokens": {
            "first": stats.turns[0] if stats.turns else 0,
            "peak": max(stats.turns) if stats.turns else 0,
            "last": stats.turns[-1] if stats.turns else 0,
        },
        "output_tokens": stats.output_tokens,
        "tool_output": {
            "calls": sum(r["calls"] for r in rows),
            "chars": total_chars,
            "approx_tokens": total_chars // CHARS_PER_TOKEN,
            "images": sum(stats.images.values()),
        },
        "tools_all": rows,
        "mcp_servers": sorted(servers.values(), key=lambda s: (-s["chars"], s["server"])),
        "lines_skipped": stats.skipped,
    }
    report["hints"] = hints_for(report)
    report["tools"] = rows[:top]
    report["tools_omitted"] = max(0, len(rows) - top)
    del report["tools_all"]
    return report


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    h, rem = divmod(seconds, 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def format_report(report: Mapping[str, Any]) -> str:
    ctx = report["context_tokens"]
    out = report["tool_output"]
    lines = [
        f"session   {report['session_id']} ({report['commander']})",
        f"span      {report['start'] or '?'} .. {report['end'] or '?'} ({_fmt_duration(report['duration_s'])})",
        f"turns     {report['turns']}  (model output {report['output_tokens']:,} tokens)",
        f"context   first {ctx['first']:,} / peak {ctx['peak']:,} / last {ctx['last']:,} tokens",
        f"tool out  {out['calls']} calls, {out['chars']:,} chars (~{out['approx_tokens']:,} tokens)"
        + (f", {out['images']} images" if out["images"] else ""),
        "",
    ]
    header = ("tool", "calls", "chars", "avg", "share", "largest")
    table = [header] + [
        (t["name"], str(t["calls"]), f"{t['chars']:,}", f"{t['avg']:,}", f"{t['share']:.1f}%", f"{t['max']:,}")
        for t in report["tools"]
    ]
    width = [max(len(r[i]) for r in table) for i in range(len(header))]
    for row in table:
        lines.append("  ".join(
            cell.ljust(width[i]) if i == 0 else cell.rjust(width[i]) for i, cell in enumerate(row)
        ).rstrip())
    if report["tools_omitted"]:
        lines.append(f"(+{report['tools_omitted']} more tools; use --top N)")
    if report["mcp_servers"]:
        lines += ["", "MCP servers:"]
        for s in report["mcp_servers"]:
            lines.append(f"  {s['server']}: {s['calls']} calls, {s['chars']:,} chars ({s['share']:.1f}%)")
    if report["lines_skipped"]:
        lines += ["", f"{report['lines_skipped']} unreadable transcript lines skipped"]
    lines += ["", "Hints:"]
    lines += [f"  - {h}" for h in report["hints"]] or ["  (none - nothing stands out)"]
    return "\n".join(lines)


def run(
    project: str | None,
    session: str | None = None,
    top: int = DEFAULT_TOP,
    as_json: bool = False,
    commander: str = "claude",
    env: Mapping[str, str] | None = None,
) -> int:
    env = os.environ if env is None else env
    project = project or os.getcwd()
    try:
        if commander == "codex":
            path = find_codex_session(project, session, env)
            stats = parse_codex(path)
        else:
            path = find_claude_session(project, session, env)
            stats = parse_claude(path)
    except ContextError as exc:
        print(f"zcrew context: {exc}")
        return 1
    report = build_report(stats, commander, path, top)
    print(json.dumps(report, indent=2) if as_json else format_report(report))
    return 0
