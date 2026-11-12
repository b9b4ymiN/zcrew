"""Optional Antigravity ACP worker for zcrew (foreground pilot).

Requires `pip install agent-client-protocol` and Antigravity CLI >=1.5.4.
Existing ZCode/Codex adapters remain untouched. No model call in `doctor`.

ACP mode `auto` is explicitly requested within `--sandbox`. If a tool still
requests an interactive permission, reject it rather than silently escalating.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from types import ModuleType
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
MODES = ("auto", "accept-edits", "default", "plan")


class AgError(Exception):
    pass


def sibling(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_zcrew_ag_{name}", HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise AgError(f"missing zcrew module: {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def find_run(compat: ModuleType, env: Mapping[str, str], ident: str, repo: Path):
    hits = []
    for path in compat.record_files(env):
        data = compat._read_json(path)
        if not isinstance(data, dict) or data.get("worker") != "antigravity":
            continue
        ids = [str(data.get("threadId") or ""), *(str(x) for x in data.get("runIds") or [])]
        if any(x == ident or (len(ident) >= 6 and x.startswith(ident)) for x in ids if x):
            hits.append((path, data))
    if len(hits) != 1:
        raise AgError("unknown or ambiguous Antigravity run; give a longer run id" if hits else
                      "no Antigravity run matches that id")
    path, data = hits[0]
    if Path(str(data.get("cwd") or "")).resolve() != repo.resolve():
        raise AgError("run belongs to a different repository; open its working directory")
    return path, data


def file_prompt(path: str, cwd: Path) -> str:
    source = Path(path)
    if not source.is_absolute():
        source = cwd / source
    if not source.is_file():
        raise AgError(f"prompt file not found: {source}")
    text = source.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise AgError("prompt file is empty")
    return text


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, exclude_none=True)
    return {}


class CaptureClient:
    """Never expose client's filesystem/terminal capabilities to the ACP agent.

    Text output and reduced, non-secret tool metadata are stored outside git.
    Interactive permissions fail closed. The agent's own terminal sandbox is
    controlled by `agy acp --sandbox`.
    """
    def __init__(self, report: Path, events: Path):
        self.report, self.events = report, events
        self.denied = 0
        self.chunks: list[str] = []
        self.errors: list[str] = []

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any):
        self.denied += 1
        title = str(_obj(tool_call).get("title") or "tool call")[:160]
        self.errors.append(f"ACP permission needed, refused: {title}")
        return {"outcome": {"outcome": "cancelled"}}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        event = _obj(update)
        kind = str(event.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            content = event.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text") or "")
                self.chunks.append(text)
                with self.report.open("a", encoding="utf-8") as out:
                    out.write(text)
        # Do not persist raw tool arguments, model reasoning, filesystem contents,
        # or environment variables in the event log.
        minimal = {"event": kind, "session": session_id, "at": int(time.time() * 1000)}
        if kind in ("tool_call", "tool_call_update"):
            minimal["title"] = str(event.get("title") or "")[:160]
            minimal["status"] = str(event.get("status") or "")[:50]
        with self.events.open("a", encoding="utf-8") as out:
            out.write(json.dumps(minimal, ensure_ascii=False) + "\n")


async def acp_turn(agy: str, cwd: Path, prompt: str, session_id: str | None,
                   mode: str, timeout: int, report: Path, events: Path,
                   on_session: Any = None) -> tuple[str, str, int]:
    """One ACP turn; process is short-lived, session is resumed on corrections."""
    try:
        from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
    except ImportError as exc:
        raise AgError("missing optional dependency: python -m pip install agent-client-protocol") from exc

    client = CaptureClient(report, events)
    async with spawn_agent_process(client, agy, "acp", "--sandbox", "--disable-telemetry") as (conn, _):
        await asyncio.wait_for(conn.initialize(protocol_version=PROTOCOL_VERSION), timeout=30)
        if session_id:
            await asyncio.wait_for(conn.resume_session(session_id=session_id, cwd=str(cwd),
                                                       mcp_servers=[]), timeout=30)
            actual = session_id
        else:
            session = await asyncio.wait_for(conn.new_session(cwd=str(cwd), mcp_servers=[]), timeout=30)
            actual = session.session_id
        if on_session:
            on_session(actual)
        # Fail instead of silently falling back to a different permission mode.
        await asyncio.wait_for(conn.set_session_mode(session_id=actual, mode_id=mode), timeout=30)
        try:
            result = await asyncio.wait_for(conn.prompt(session_id=actual,
                                                        prompt=[text_block(prompt)]), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                await asyncio.wait_for(conn.cancel(session_id=actual), timeout=3)
            except Exception:
                pass
            raise AgError(f"ACP turn timed out after {timeout}s; inspect partial changes")
        reason = str(getattr(result, "stop_reason", ""))
        return actual, reason, client.denied


def doctor(agy: str | None = None) -> int:
    executable = agy or shutil.which("agy")
    if not executable:
        print("ag: agy not found on PATH; install official Antigravity CLI")
        return 1
    try:
        r = subprocess.run([executable, "acp", "--help"], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ag: cannot probe agy acp: {exc}")
        return 1
    if r.returncode:
        print(f"ag: agy acp --help failed (exit {r.returncode})")
        return 1
    if importlib.util.find_spec("acp") is None:
        print("ag: ACP available; install Python SDK: python -m pip install agent-client-protocol")
        return 1
    print("ag: ACP CLI and Python SDK found; sign in once with 'agy' (not checked)")
    return 0


def execute(args: argparse.Namespace, *, repo: Path, env: Mapping[str, str]) -> int:
    if args.command == "doctor":
        return doctor(args.agy)
    compat = sibling("bridge_compat")
    if args.command == "gate":
        _, record = find_run(compat, env, args.run_id, repo)
        if any(x == "--accept" for x in args.extra) and record.get("antigravity", {}).get("status") != "completed":
            raise AgError("cannot accept a worker that is not completed; inspect the report and retry")
        result = subprocess.run([sys.executable, str(HERE / "zcrew.py"), "gate", args.run_id,
                                 *args.extra], cwd=repo)
        return result.returncode
    if args.command == "status":
        for path in compat.record_files(env):
            data = compat._read_json(path)
            if data and data.get("worker") == "antigravity" and Path(data.get("cwd", "")).resolve() == repo:
                meta = data.get("antigravity") or {}
                print(f"{(data.get('runIds') or ['?'])[-1]}  {meta.get('status', 'unknown')}  "
                      f"session={data.get('threadId') or 'pending'}  "
                      f"report={(meta.get('reports') or ['?'])[-1]}")
        return 0

    executable = args.agy or shutil.which("agy")
    if not executable:
        raise AgError("agy executable not found; install Antigravity CLI and log in with 'agy'")
    text = file_prompt(args.prompt, repo)
    now = int(time.time() * 1000)
    if args.command == "run":
        contract = compat.parse_contract(text)
        if contract["missing"]:
            raise AgError("contract missing: " + ", ".join(contract["missing"]))
        snap = compat.git_snapshot(str(repo))
        if not snap["base"]:
            raise AgError("this directory is not a Git repository with an initial commit")
        gate = sibling("gate")
        blind = [x for x in snap["dirty"] if gate.matches_any(x, contract.get("scope") or [])]
        if blind and not args.allow_dirty:
            raise AgError("dirty SCOPE files blind the gate: " + ", ".join(blind) + "; commit/revert or --allow-dirty")
        stamp = time.strftime("%Y%m%d%H%M%S")
        run_id = f"agy_{stamp}_{secrets.token_hex(3)}"
        record = {"runIds": [run_id], "threadId": None, "cwd": str(repo),
                  "startedAt": now, "roundStartedAt": now, "rounds": 1,
                  "base": snap["base"], "dirty": snap["dirty"], "contract": contract,
                  "verdicts": [], "mode": "warn", "worker": "antigravity",
                  "antigravity": {"status": "starting", "sessionId": None, "reports": []}}
        record_path = compat.state_dir(env) / f"{run_id}.json"
    else:
        record_path, record = find_run(compat, env, args.run_id, repo)
        meta = record["antigravity"]
        if not meta.get("sessionId"):
            raise AgError("no ACP session id in this run; start a fresh contract")
        if any(v.get("verdict") == "accept" for v in record.get("verdicts") or []):
            raise AgError("run already accepted; do not mutate it with a correction")
        round_number = int(record["rounds"]) + 1
        if round_number > sibling("gate").max_rounds(str(repo), env) + 1:
            raise AgError("correction budget exceeded; request commander review")
        run_id = f"{record['runIds'][0]}.r{round_number}"
        record["runIds"].append(run_id)
        record["rounds"] = round_number
        record["roundStartedAt"] = now
        meta["status"] = "starting"

    output_dir = compat.state_dir(env).parent / "ag"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"{run_id}.report.md"
    events = output_dir / f"{run_id}.events.jsonl"
    record["antigravity"]["reports"].append(str(report))
    compat.save_record(record_path, record)

    def session_created(value: str) -> None:
        record["threadId"] = value
        record["antigravity"]["sessionId"] = value
        compat.save_record(record_path, record)

    try:
        actual, reason, denied = asyncio.run(acp_turn(
            executable, repo, text, record["antigravity"]["sessionId"],
            args.mode, args.timeout, report, events, on_session=session_created))
        record["antigravity"].update({"sessionId": actual, "stopReason": reason,
                                      "deniedPermissions": denied,
                                      "status": "completed" if reason == "end_turn" and denied == 0 else "blocked"})
    except Exception as exc:
        # Always retain the gate record and partial files for recovery.
        record["antigravity"].update({"status": "failed", "error": str(exc)[:300]})
        compat.save_record(record_path, record)
        raise AgError(f"ACP worker failed: {exc}; partial report: {report}") from exc
    record["antigravity"]["endedAt"] = int(time.time() * 1000)
    compat.save_record(record_path, record)
    print(f"run id: {run_id}\nsession: {record['threadId']}\nstatus: {record['antigravity']['status']}\nreport: {report}")
    print(f"next: zcrew ag gate {run_id}")
    return 0 if record["antigravity"]["status"] == "completed" else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zcrew ag", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "fix"):
        x = sub.add_parser(name)
        if name == "fix":
            x.add_argument("run_id")
        x.add_argument("prompt", help="worker contract / correction Markdown file")
        x.add_argument("--agy", help="path to the agy executable")
        x.add_argument("--mode", choices=MODES, default="auto",
                       help="ACP permission mode (default: auto inside terminal sandbox)")
        x.add_argument("--timeout", type=int, default=1800)
        if name == "run":
            x.add_argument("--allow-dirty", action="store_true")
    x = sub.add_parser("doctor")
    x.add_argument("--agy")
    sub.add_parser("status")
    x = sub.add_parser("gate")
    x.add_argument("run_id")
    x.add_argument("extra", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None, *, repo: Path | None = None,
         env: Mapping[str, str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args, repo=Path(repo or Path.cwd()).resolve(),
                       env=os.environ if env is None else env)
    except (AgError, OSError, ValueError) as exc:
        print(f"ag: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())