"""zcrew codex-hook - hands-free waiting on ZCode workers for a Codex commander.

``zcrew codex-hook stop``              the Codex ``Stop`` hook (JSON on stdin)
``zcrew codex-hook install [DIR]``     add it to DIR/.codex/hooks.json
``zcrew codex-hook uninstall [DIR]``   remove it again

Claude Code has the zcrew mod to wake the commander when a worker run ends;
Codex has lifecycle hooks (the ``hooks`` feature, stable and on by default in
current Codex). A ``Stop`` hook that answers ``{"decision": "block", "reason":
...}`` makes Codex continue the turn with ``reason`` as the next prompt, so:

- when the commander ends its turn with ``[zcrew:wait]`` in its last message
  and worker runs are still going, the hook waits (up to ``WAIT_SECONDS``),
  then wakes it with what ended, the ``zcrew gate`` result included;
- a run that ended, or stalled (no activity for 30 min: ZCode's ``idle``), is
  reported once, at the next turn end, wait mark or not;
- with nothing to report or wait for, the hook prints nothing and Codex stops.

Runs are the gate records the bridge writes at agent-start
(``~/.zcode-commander/gate``) for this project and its git worktrees, without a
verdict yet; their state comes from ZCode's database (read-only). Every failure
inside the hook lets Codex stop normally. Codex runs a project's hooks only
after the user trusts them (it asks at start-up, or ``/hooks``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

HERE = Path(__file__).resolve().parent
WAIT_MARK = "[zcrew:wait]"
HOOK_TIMEOUT_SECONDS = 3600
WAIT_SECONDS = 2700  # leaves the rest of the hook timeout for the gate runs
POLL_SECONDS = 15.0
GATE_TIMEOUT_SECONDS = 780
RECORD_MAX_AGE_MS = 24 * 3600 * 1000
MATCH_SLACK_MS = 10_000
# A correction whose turn has not shown up yet is still starting, not finished.
ROUND_GRACE_MS = 120_000
# ZCode reports a session "idle" when it is empty (just created) or silent this long.
STALL_MS = 30 * 60 * 1000
NOTIFIED_KEEP_MS = 7 * 24 * 3600 * 1000
HOOK_MARK = "codex-hook stop"
CODEX_BEGIN = "<!-- zcrew-codex:begin"
STATUS_MESSAGE = "zcrew: waiting for ZCode workers"


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


def _norm(path: str | None) -> str:
    return os.path.normcase(os.path.realpath(path)) if path else ""


# --- which runs, in which state ---------------------------------------------------------


@dataclass(frozen=True)
class RunState:
    run_id: str
    session_id: str | None
    title: str
    cwd: str
    rounds: int
    state: str  # queued | running | ended | stalled
    status: str  # ZCode's session status, or "queued"
    started_ms: int
    ended_ms: int | None

    @property
    def key(self) -> str:
        return f"{self.session_id or self.run_id}:{self.rounds}:{self.state}"


def git_common_dir(path: str, cache: dict[str, str | None]) -> str | None:
    if path not in cache:
        try:
            done = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                  capture_output=True, text=True, timeout=10, check=False)
            cache[path] = _norm(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            cache[path] = None
    return cache[path]


def related(record_cwd: str | None, project: str, cache: dict[str, str | None]) -> bool:
    """The project itself, a folder in it, or one of its git worktrees."""
    if not record_cwd:
        return False
    a, b = _norm(record_cwd), _norm(project)
    if a == b or a.startswith(b + os.sep):
        return True
    common = git_common_dir(project, cache)
    return common is not None and git_common_dir(record_cwd, cache) == common


def follow(records: list[dict[str, Any]], sessions: list[Any], now_ms: int) -> list[RunState]:
    """Match each record with its ZCode session (its threadId, else the first
    session started in its folder after it) and say where the run stands."""
    by_id = {s.id: s for s in sessions}
    claimed = {r.get("threadId") for r in records if r.get("threadId")}
    out = []
    for rec in sorted(records, key=lambda r: int(r.get("startedAt") or 0)):
        started = int(rec.get("startedAt") or 0)
        session = by_id.get(rec.get("threadId") or "")
        if session is None:
            candidates = sorted(
                (s for s in sessions if s.id not in claimed and _norm(s.directory) == _norm(rec.get("cwd"))
                 and s.created_ms >= started - MATCH_SLACK_MS),
                key=lambda s: s.created_ms,
            )
            session = candidates[0] if candidates else None
            if session is not None:
                claimed.add(session.id)
        round_start = int(rec.get("roundStartedAt") or started)
        if session is None:
            state, status, ended = "queued", "queued", None
        elif session.status == "running":
            state, status, ended = "running", "running", None
        elif session.status == "idle":
            if now_ms - session.updated_ms >= STALL_MS:
                state, status, ended = "stalled", "idle", session.updated_ms
            else:  # created, no activity yet
                state, status, ended = "running", "running", None
        elif session.updated_ms < round_start:
            # the correction's turn has not started: still starting, or waiting for a slot
            state = "running" if now_ms - round_start < ROUND_GRACE_MS else "queued"
            status, ended = state, None
        else:
            state, status, ended = "ended", session.status, session.updated_ms
        run_ids = rec.get("runIds") or ["?"]
        out.append(RunState(
            run_id=str(run_ids[-1]), session_id=session.id if session else rec.get("threadId"),
            title=(session.title if session else "") or "(no title)", cwd=str(rec.get("cwd") or ""),
            rounds=int(rec.get("rounds") or 1), state=state, status=status, started_ms=round_start,
            ended_ms=ended,
        ))
    return out


# --- gate and messages ------------------------------------------------------------------


def gate_summary(report: Mapping[str, Any] | None) -> str:
    """One line of a ``zcrew gate --json`` report (same wording as the Claude Code mod)."""
    if not report or not isinstance(report.get("mechanical"), str):
        return "gate not run (run it yourself)"
    checks = [c for c in report.get("checks") or [] if isinstance(c, dict)]
    failing = [f"{c.get('name')}: {c.get('detail')}" for c in checks if c.get("status") == "FAIL"]
    warns = [f"{c.get('name')}: {c.get('detail')}" for c in checks if c.get("status") == "WARN"]
    detail = "; ".join(failing or warns)[:240]
    return f"gate MECHANICAL {report['mechanical']}" + (f" ({detail})" if detail else "")


def run_gate(run: RunState) -> dict[str, Any] | None:
    try:
        done = subprocess.run(
            [sys.executable, str(HERE / "zcrew.py"), "gate", run.run_id, "--json"],
            capture_output=True, text=True, timeout=GATE_TIMEOUT_SECONDS, check=False,
            cwd=run.cwd if run.cwd and Path(run.cwd).is_dir() else None,
        )
        data = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _minutes(ms: int) -> int:
    return max(0, round(ms / 60000))


def wake_message(news: list[tuple[RunState, str]], active: list[RunState], now_ms: int) -> str:
    lines = ["[zcrew] Worker run(s) need you. Continue the review:"]
    for run, gate in news:
        took = _minutes((run.ended_ms or now_ms) - run.started_ms)
        if run.state == "stalled":
            lines.append(f'- {run.run_id} "{run.title}" ({run.session_id}): STALLED, no worker activity for'
                         " 30+ min. Look with agent-observe; if it is stuck, cancel it with agent-control and"
                         " start it again (or send a correction to the same thread).")
        else:
            lines.append(f'- {run.run_id} "{run.title}": {run.status} after {took} min; {gate}.')
    if active:
        lines.append("Still running: " + ", ".join(f'{r.run_id} "{r.title}"' for r in active) + ".")
    lines.append("Review each ended run with `zcrew gate <runId>` (its VERIFY results count as evidence), fetch the"
                 " worker REPORT with agent-wait (resultChars > 0) if you need it, then accept with"
                 ' `zcrew gate <runId> --accept "..."` or send a correction to the same thread.'
                 f" To wait for runs again, end your turn with {WAIT_MARK}.")
    return "\n".join(lines)


def still_running_message(active: list[RunState], now_ms: int) -> str:
    runs = ", ".join(f'{r.run_id} "{r.title}" ({r.state}, {_minutes(now_ms - r.started_ms)} min)' for r in active)
    return (f"[zcrew] Still waiting on {len(active)} worker run(s): {runs}. Nothing ended yet. End your turn"
            f" with {WAIT_MARK} to keep waiting (the hook wakes you when one ends), or do other work first.")


# --- state of what was already reported --------------------------------------------------


def notified_path(env: Mapping[str, str]) -> Path:
    explicit = (env.get("ZCODE_COMMANDER_STATE") or "").strip()
    base = Path(explicit) if explicit else Path(env.get("USERPROFILE") or env.get("HOME") or Path.home()) / ".zcode-commander"
    return base / "codex-hook" / "notified.json"


def load_notified(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def save_notified(path: Path, notified: Mapping[str, int], now_ms: int) -> None:
    keep = {k: v for k, v in notified.items() if now_ms - v < NOTIFIED_KEEP_MS}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    os.replace(tmp, path)


# --- the hook ---------------------------------------------------------------------------


def project_enabled(cwd: str) -> bool:
    """The Codex commander policy is enabled in cwd or a parent (zcrew enable --commander codex)."""
    here = Path(cwd)
    for folder in (here, *here.parents):
        override = folder / "AGENTS.override.md"
        try:
            if override.is_file() and CODEX_BEGIN in override.read_text(encoding="utf-8-sig"):
                return True
        except OSError:
            return False
        if (folder / ".git").exists():
            return False
    return False


def current_runs(cwd: str, env: Mapping[str, str], now_ms: int, cache: dict[str, str | None]) -> list[RunState]:
    compat, za = _load("bridge_compat"), _load("zcode_activity")
    records = []
    for path in compat.record_files(env):
        data = compat._read_json(path)
        if not data or data.get("verdicts") or now_ms - int(data.get("startedAt") or 0) > RECORD_MAX_AGE_MS:
            continue
        if related(data.get("cwd"), cwd, cache):
            records.append(data)
    if not records:
        return []
    since = min(int(r.get("startedAt") or now_ms) for r in records) - 60_000
    sessions = [s for s in za.list_sessions(za.default_db_path(env), since_ms=since, limit=500, now_ms=now_ms)
                if not s.parent_id]
    return follow(records, sessions, now_ms)


def stop(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    gate: Callable[[RunState], dict[str, Any] | None] = run_gate,
    runs_fn: Callable[[str, Mapping[str, str], int, dict], list[RunState]] = current_runs,
) -> dict[str, Any] | None:
    """The hook's answer: a ``block`` decision with a prompt, or None to let Codex stop."""
    cwd = str(payload.get("cwd") or "")
    if not cwd or not project_enabled(cwd):
        return None
    wait = WAIT_MARK in str(payload.get("last_assistant_message") or "")
    began = clock()
    deadline = began + WAIT_SECONDS
    path = notified_path(env)
    cache: dict[str, str | None] = {}
    while True:
        now_ms = int(clock() * 1000)
        runs = runs_fn(cwd, env, now_ms, cache)
        notified = load_notified(path)
        news = [r for r in runs if r.state in ("ended", "stalled") and r.key not in notified]
        active = [r for r in runs if r.state in ("running", "queued")]
        if news:
            reported = []
            for r in news:
                # several runs ending together must not push the hook past its timeout
                room = HOOK_TIMEOUT_SECONDS - 60 - (clock() - began) >= GATE_TIMEOUT_SECONDS
                reported.append((r, gate(r) if room and r.state == "ended" and r.status == "completed" else None))
            for run, _ in reported:
                notified[run.key] = now_ms
            save_notified(path, notified, now_ms)
            message = wake_message([(r, gate_summary(g) if r.status == "completed" else "")
                                    for r, g in reported], active, now_ms)
            return {"decision": "block", "reason": message}
        if not wait or not active:
            return None
        if clock() >= deadline:
            return {"decision": "block", "reason": still_running_message(active, now_ms)}
        sleep(POLL_SECONDS)


# --- install into a project's .codex/hooks.json -----------------------------------------


def hook_command(python: str | None = None, zcrew_py: Path | None = None) -> str:
    return f'"{python or sys.executable}" "{zcrew_py or HERE / "zcrew.py"}" {HOOK_MARK}'


def _is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and HOOK_MARK in str(handler.get("command") or "")


def _without_ours(data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    groups = []
    for group in hooks.get("Stop") or []:
        if not isinstance(group, dict):
            groups.append(group)
            continue
        kept = [h for h in group.get("hooks") or [] if not _is_ours(h)]
        if kept:
            groups.append({**group, "hooks": kept})
    new_hooks = {k: v for k, v in hooks.items() if k != "Stop"}
    if groups:
        new_hooks["Stop"] = groups
    return {**data, "hooks": new_hooks}


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"hooks": {}}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install(project_dir: Path, command: str | None = None) -> tuple[bool, str]:
    path = Path(project_dir) / ".codex" / "hooks.json"
    try:
        data = _read_hooks(path)
    except (OSError, ValueError) as exc:
        return False, f"Codex hook: cannot read {path} ({exc}); fix it, then run: zcrew codex-hook install"
    entry = {"type": "command", "command": command or hook_command(), "timeout": HOOK_TIMEOUT_SECONDS,
             "statusMessage": STATUS_MESSAGE}
    current = [h for g in (data.get("hooks") or {}).get("Stop") or [] if isinstance(g, dict)
               for h in g.get("hooks") or [] if _is_ours(h)]
    if current == [entry]:
        return False, f"Codex hook: already in {path} (no change)"
    new = _without_ours(data)
    new["hooks"].setdefault("Stop", []).append({"hooks": [entry]})
    _write(path, new)
    return True, (f"Codex hook: added the zcrew Stop hook to {path}. Codex asks you to trust it on the next"
                  " start (or run /hooks); it wakes the commander when ZCode workers end.")


def uninstall(project_dir: Path) -> tuple[bool, str]:
    path = Path(project_dir) / ".codex" / "hooks.json"
    try:
        data = _read_hooks(path)
    except (OSError, ValueError) as exc:
        return False, f"Codex hook: cannot read {path} ({exc}); remove the zcrew entry by hand"
    new = _without_ours(data)
    if new == data:
        return False, f"Codex hook: none in {path} (no change)"
    if not new["hooks"] and set(new) <= {"hooks"}:
        path.unlink()
        try:
            path.parent.rmdir()  # only when empty
        except OSError:
            pass
        return True, f"Codex hook: removed {path}"
    _write(path, new)
    return True, f"Codex hook: removed the zcrew Stop hook from {path}"


def run(args: Any, *, env: Mapping[str, str] | None = None, stdin: Any = None, out: Any = None) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    if args.action == "stop":
        try:
            payload = json.loads((stdin or sys.stdin).read() or "{}")
            answer = stop(payload if isinstance(payload, dict) else {}, env)
        except Exception as exc:  # noqa: BLE001 - a broken hook must never trap Codex
            sys.stderr.write(f"zcrew codex-hook: {exc}\n")
            return 0
        if answer:
            out.write(json.dumps(answer) + "\n")
        return 0
    project = Path(args.dir or os.getcwd())
    ok, message = install(project) if args.action == "install" else uninstall(project)
    out.write(message + "\n")
    return 0 if ok or "no change" in message else 1
