"""Dispatch worker contracts to the Codex CLI and record them for zcrew gate.

run/fix send a contract to the OpenAI Codex plugin (codex-companion.mjs) and
write a gate record per round; audit is a read-only one-shot; gate forwards to
zcrew's own gate; status lists this repo's Codex runs and settles background
jobs. Child output is captured to the report file, never streamed. --background
on run/fix enqueues through the companion and returns at once; the job's result
is reconciled into the record later by `zcrew cx status`. `main` accepts a
subprocess.run-compatible runner so the tests run without Node or Codex.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping


SCRIPTS = Path(__file__).resolve().parent

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
THREAD = re.compile(r"\[codex\] Thread ready \(([0-9a-fA-F-]{36})\)\.")
JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
ENDED_JOBS = ("completed", "failed", "cancelled")
ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000
# The companion stores its job queue per CLAUDE_PLUGIN_DATA (else the OS temp
# dir) and pins the host session via CODEX_COMPANION_SESSION_ID. Dispatching
# and later status polls run in different shells, so every companion call must
# see the same shared job store: strip both keys from the child env.
STRIPPED_ENV_KEYS = ("CLAUDE_PLUGIN_DATA", "CODEX_COMPANION_SESSION_ID")

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]

_SIBLINGS: dict[str, ModuleType] = {}


class CxError(Exception):
    """An actionable invocation or dependency error."""


def load_sibling(name: str) -> ModuleType:
    if name not in _SIBLINGS:
        path = SCRIPTS / f"{name}.py"
        require_file(path)
        spec = importlib.util.spec_from_file_location(f"_cx_{name}", path)
        if spec is None or spec.loader is None:
            raise CxError(f"cannot load zcrew helpers: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
        spec.loader.exec_module(module)
        _SIBLINGS[name] = module
    return _SIBLINGS[name]


def discover_plugin(home: Path) -> Path:
    """Choose the newest numeric version, rather than lexicographic order."""
    root = home / ".claude/plugins/cache/openai-codex/codex"

    def version(path):
        match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", path.parent.parent.name)
        if not match:
            return ((), False, path.parent.parent.name)
        parts = tuple(int(n) for n in match[1].split("."))
        return (parts + (0,) * max(0, 4 - len(parts)), not bool(match[2]), match[2])

    paths = list(root.glob("*/scripts/codex-companion.mjs"))
    paths = [p for p in paths if p.is_file()]
    if not paths:
        raise CxError(f"Codex plugin not found under {root}")
    return max(paths, key=version)


def require_file(path: Path) -> None:
    if not path.is_file():
        raise CxError(f"file not found: {path}")


def read_prompt(path: str, repo: Path) -> str:
    path = relative_path(path, repo)
    require_file(path)
    prompt = path.read_text(encoding="utf-8")
    if not prompt.strip() or prompt.strip() == "--help":
        raise CxError(f"{path}: expected a nonempty worker prompt, not --help")
    return prompt


def relative_path(path: str, repo: Path) -> Path:
    path = Path(path)
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def filtered(text: str | None) -> str:
    return "".join(line for line in (text or "").splitlines(keepends=True)
                   if "DEP0190" not in line)


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    clean = dict(env)
    for key in STRIPPED_ENV_KEYS:
        clean.pop(key, None)
    return clean


def executable(name: str, *, which: Callable[..., str | None], env: Mapping[str, str]) -> str:
    path = which(name, path=env.get("PATH"))
    if not path:
        raise CxError(f"executable not found: {name}")
    return path


def execute(command: list[str], *, runner: Runner, env: Mapping[str, str], repo: Path,
            input: str | None = None) -> tuple["subprocess.CompletedProcess[Any]", str]:
    # subprocess.run creates and closes a pipe for input; never also set stdin.
    stdin_options = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    result = runner(command, cwd=str(repo), env=child_env(env), **stdin_options,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
    stderr = filtered(result.stderr)
    if stderr:
        sys.stderr.write(stderr)
    return result, filtered(result.stdout)


def record_list(compat: ModuleType, env: Mapping[str, str]) -> Iterator[tuple[Path, dict]]:
    for path in compat.state_dir(env).glob("*.json"):
        data = compat._read_json(path)
        if isinstance(data, dict):
            yield path, data


def resolve_record(compat: ModuleType, env: Mapping[str, str], ident: str) -> tuple[Path, dict]:
    # Match gate's prefix semantics, including ambiguity even for an exact ID.
    hits: list[tuple[Path, dict]] = []
    for path, data in record_list(compat, env):
        ids = [str(data.get("threadId") or ""), *(data.get("runIds") or [])]
        if any(i == ident or (len(ident) >= 4 and
                              (i.startswith(ident) or i.startswith("sess_" + ident)))
               for i in ids):
            hits.append((path, data))
    if not hits:
        raise CxError(f"gate record not found: {ident}")
    if len(hits) != 1:
        raise CxError(f"ambiguous gate record {ident!r}; give a longer id")
    # Reuse the authoritative exact run lookup once the prefix is resolved.
    return compat.find_record(env, run_id=hits[0][1]["runIds"][0]) or hits[0]


def same_repo(cwd: Any, repo: Path) -> bool:
    return bool(cwd) and os.path.normcase(str(Path(str(cwd)).resolve())) == os.path.normcase(str(repo))


def is_codex_record(record: Mapping[str, Any]) -> bool:
    if record.get("worker") == "codex":
        return True
    ids = record.get("runIds") or []
    return bool(ids) and str(ids[0]).startswith("codex_")


def task_command(plugin: Path, effort: str, prompt: str, *, node: str,
                 write: bool = False, resume: bool = False, background: bool = False) -> list[str]:
    cmd = [node, str(plugin), "task", "--effort", effort]
    if write:
        cmd.append("--write")
    if resume:
        cmd.append("--resume-last")
    if background:
        cmd += ["--background", "--json"]
    return [*cmd, prompt]


def resume_candidate_matches(plugin: Path, session: str, *, node: str, runner: Runner,
                             env: Mapping[str, str], repo: Path) -> bool:
    """Audits and tasks outside cx can replace the plugin's latest session.

    This plugin command reads its local job state only. Older plugin versions
    or malformed replies require a fresh thread rather than guessing.
    """
    result, stdout = execute([node, str(plugin), "task-resume-candidate", "--json"],
                             runner=runner, env=env, repo=repo)
    if result.returncode:
        return False
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    candidate = data.get("candidate") if isinstance(data, dict) else None
    return (isinstance(candidate, dict) and data.get("available") is True
            and candidate.get("threadId") == session)


def parse_job(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    job = data.get("job") if isinstance(data, dict) else None
    return job if isinstance(job, dict) and job.get("status") in JOB_STATUSES else None


def prepare_report(path: str | None, repo: Path, prompt_path: str) -> Path:
    report = relative_path(path, repo)
    if report == relative_path(prompt_path, repo):
        raise CxError(f"report path would overwrite prompt: {report}")
    report.parent.mkdir(parents=True, exist_ok=True)
    ignore = report.parent / ".gitignore"
    if report.parent == repo / ".cx" and not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")  # else zcrew gate counts reports as worker changes
    return report


# --- background rounds ---------------------------------------------------------------


def start_background(cmd: list[str], *, runner: Runner, env: Mapping[str, str],
                     repo: Path) -> tuple[int, str | None, str]:
    """Enqueue through the companion; returns (exit, jobId or None, report text)."""
    result, stdout = execute(cmd, runner=runner, env=env, repo=repo)
    if result.returncode:
        stderr = filtered(result.stderr)
        text = stdout + ("\n" if stdout and stderr and not stdout.endswith("\n") else "") + stderr
        return result.returncode, None, text
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    job_id = data.get("jobId") if isinstance(data, dict) else None
    if isinstance(job_id, str) and job_id:
        return 0, job_id, ""
    return 0, None, stdout


def enqueue_round(cmd: list[str], record: dict, record_path: Path, *, run_id: str, report: Path,
                  compat: ModuleType, runner: Runner, env: Mapping[str, str], repo: Path,
                  baseline: bool) -> int:
    """Queue one round and return at once.

    baseline=True (run): the record is already persisted, so a failed enqueue is
    stamped onto it; a correction round is only committed once it has a job id.
    """
    code, job_id, failure = start_background(cmd, runner=runner, env=env, repo=repo)
    meta = record["codex"]
    if job_id is None:
        report.write_text(failure, encoding="utf-8")
        if baseline:
            meta["lastExit"] = code or 1
            meta["endedAt"] = int(time.time() * 1000)
            compat.save_record(record_path, record)
        print(f"cx: background dispatch failed (exit {code or 1}); see {report}", file=sys.stderr)
        return code or 2
    meta["jobId"] = job_id
    compat.save_record(record_path, record)
    print(f"run id: {run_id}\njob id: {job_id}\nreport: {report}")
    print("next: zcrew cx status")
    return 0


def adopt_thread(record: dict, meta: dict, thread_id: Any) -> None:
    """A background job may report a Codex thread the record has not adopted yet."""
    if not isinstance(thread_id, str) or not thread_id or thread_id == meta.get("sessionId"):
        return
    previous = meta.get("sessionId")
    if previous:
        meta.setdefault("previousSessions", []).append(previous)
    if meta.get("resume"):
        meta["warning"] = (f"resume-last correction returned on thread {thread_id} instead of "
                           f"{previous}; it did not continue the recorded thread")
    record["threadId"] = meta["sessionId"] = thread_id


def reconcile(record: dict, path: Path, *, compat: ModuleType, runner: Runner,
              which: Callable[..., str | None], env: Mapping[str, str], home: Path) -> str:
    """Settle the latest round's finished background job into its record.

    Fetches the job's final output into the round's report, stamps the end and
    adopts the thread. Jobs still queued/running are left for a later call;
    companion failures leave the record untouched and report 'unknown'.
    """
    meta = record.get("codex") or {}
    job_id = meta.get("jobId")
    if not isinstance(job_id, str) or not job_id or meta.get("endedAt") is not None:
        return "unknown"
    try:
        plugin = discover_plugin(home)
        node = executable("node", which=which, env=env)
        cwd = Path(str(record.get("cwd") or "."))
        result, stdout = execute([node, str(plugin), "status", job_id, "--json"],
                                 runner=runner, env=env, repo=cwd)
        if result.returncode:
            return "unknown"
        job = parse_job(stdout)
        if job is None:
            return "unknown"
        status = str(job["status"])
        if status not in ENDED_JOBS:
            return status
        # The companion keeps at most 50 jobs: copy the final report out now.
        final = execute([node, str(plugin), "result", job_id], runner=runner, env=env, repo=cwd)[1]
        reports = meta.get("reportPaths") or []
        if reports:
            Path(reports[-1]).write_text(final, encoding="utf-8")
        meta["endedAt"] = int(time.time() * 1000)
        meta["lastExit"] = 0 if status == "completed" else 1
        meta["jobStatus"] = status
        adopt_thread(record, meta, job.get("threadId"))
        compat.save_record(path, record)
        return status
    except (CxError, OSError, UnicodeError):
        return "unknown"


# --- status --------------------------------------------------------------------------


def current_status(meta: Mapping[str, Any]) -> str | None:
    """The record's status without companion calls; None when a job needs reconciling."""
    if meta.get("background"):
        if meta.get("endedAt") is not None:
            return str(meta["jobStatus"] or
                       ("completed" if meta.get("lastExit") == 0 else "failed"))
        return None if meta.get("jobId") else "unknown"
    last_exit = meta.get("lastExit")
    if last_exit is None:
        # A foreground round persists before dispatch: no lastExit/endedAt yet means
        # it is still executing (a crashed launch is indistinguishable from that).
        return "running" if meta.get("endedAt") is None else "failed"
    return "completed" if last_exit == 0 else "failed"


def contract_title(text: Any) -> str:
    for line in (text or "").splitlines() if isinstance(text, str) else []:
        line = line.strip()
        if line:
            return line[:80]
    return ""


def status_entry(record: Mapping[str, Any], status: str) -> dict[str, Any]:
    meta = record.get("codex") or {}
    reports = meta.get("reportPaths") or []
    verdicts = record.get("verdicts") or []
    last = verdicts[-1] if verdicts else None
    verdict = last.get("verdict") if isinstance(last, dict) else last
    return {
        "runId": (record.get("runIds") or [None])[-1],
        "worker": "codex",
        "threadId": record.get("threadId"),
        "jobId": meta.get("jobId"),
        "cwd": record.get("cwd"),
        "title": contract_title(meta.get("contractText")),
        "status": status,
        "startedAt": record.get("startedAt"),
        "roundStartedAt": record.get("roundStartedAt"),
        "endedAt": meta.get("endedAt"),
        "round": record.get("rounds"),
        "report": reports[-1] if reports else None,
        "verdict": verdict if isinstance(verdict, str) and verdict else None,
        "warning": meta.get("warning"),
    }


def is_active(record: Mapping[str, Any], now: int) -> bool:
    """--active keeps unverdicted runs from the last day, ended ones included:
    the mod needs to see a run's end exactly once."""
    if record.get("verdicts"):
        return False
    started = record.get("startedAt")
    return (isinstance(started, int) and not isinstance(started, bool)
            and 0 <= now - started < ACTIVE_WINDOW_MS)


def status_line(entry: Mapping[str, Any]) -> str:
    bits = [str(entry["runId"] or "?"), str(entry["status"]), f"round {entry['round'] or 1}"]
    if entry["title"]:
        bits.append(str(entry["title"]))
    return "  ".join(bits)


def status_command(args: argparse.Namespace, *, runner: Runner, which: Callable[..., str | None],
                   env: Mapping[str, str], home: Path, repo: Path) -> int:
    compat = load_sibling("bridge_compat")
    target = relative_path(args.dir, repo) if args.dir else repo
    now = int(time.time() * 1000)
    runs: list[dict[str, Any]] = []
    for path, record in sorted(record_list(compat, env),
                               key=lambda item: int(item[1].get("startedAt") or 0)):
        if not is_codex_record(record) or not same_repo(record.get("cwd"), target):
            continue
        if args.active and not is_active(record, now):
            continue
        status = current_status(record.get("codex") or {})
        if status is None:
            status = reconcile(record, path, compat=compat, runner=runner,
                               which=which, env=env, home=home) or "unknown"
        runs.append(status_entry(record, status))
    if args.json:
        # The JSON feed is read through pipes with legacy code pages; keep it pure ASCII.
        print(json.dumps({"runs": runs}, ensure_ascii=True))
    else:
        for entry in runs:
            print(status_line(entry))
    return 0


# --- run / fix / audit / gate ----------------------------------------------------------


def dispatch(args: argparse.Namespace, *, runner: Runner, which: Callable[..., str | None],
             env: Mapping[str, str], home: Path, repo: Path) -> int:
    if args.command == "gate":
        script = SCRIPTS / "zcrew.py"
        require_file(script)
        result, stdout = execute([sys.executable, str(script), "gate", args.run_id, *args.extra],
                                 runner=runner, env=env, repo=repo)
        sys.stdout.write(stdout)
        return result.returncode
    if args.command == "status":
        return status_command(args, runner=runner, which=which, env=env, home=home, repo=repo)

    prompt = read_prompt(args.prompt, repo)
    now = int(time.time() * 1000)
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(now / 1000))
    if args.command == "audit":
        plugin = discover_plugin(home)
        node = executable("node", which=which, env=env)
        report = prepare_report(args.report or f".cx/audit_{stamp}_{secrets.token_hex(2)}.md",
                                repo, args.prompt)
        result, stdout = execute(task_command(plugin, args.effort, prompt, node=node),
                                 runner=runner, env=env, repo=repo)
        report.write_text(stdout, encoding="utf-8")
        print(f"report: {report}")
        return result.returncode

    compat = load_sibling("bridge_compat")
    if args.command == "run":
        plugin = discover_plugin(home)
        node = executable("node", which=which, env=env)
        run_id = f"codex_{stamp}_{secrets.token_hex(2)}"
        contract = compat.parse_contract(prompt)
        if contract["missing"]:
            print("cx: contract is missing: " + ", ".join(contract["missing"]), file=sys.stderr)
        snap = compat.git_snapshot(str(repo))
        # zcrew gate skips files that were already dirty at dispatch, so a dirty SCOPE file would be
        # reviewed blind. Refuse unless the owner explicitly accepts that.
        gate_mod = load_sibling("gate")
        blind = [d for d in snap["dirty"] if contract["scope"] and gate_mod.matches_any(d, contract["scope"])]
        if blind and not args.allow_dirty:
            raise CxError("SCOPE files already have uncommitted changes, so cx gate could not review them: "
                          + ", ".join(blind) + " — commit (with THP's go-ahead) or revert them first, "
                          "or pass --allow-dirty and review those files by hand")
        report = prepare_report(args.report or f".cx/{run_id}.report.md", repo, args.prompt)
        record = {
            "runIds": [run_id], "threadId": None, "cwd": str(repo),
            "startedAt": now, "roundStartedAt": now, "rounds": 1,
            "base": snap["base"], "dirty": snap["dirty"], "contract": contract,
            "verdicts": [], "mode": "warn", "worker": "codex",
            "codex": {"sessionId": None, "effort": args.effort,
                      "write": args.write, "reportPaths": [str(report)],
                      "contractText": prompt,
                      "jobId": None, "background": bool(args.background),
                      "endedAt": None, "jobStatus": None, "resume": False,
                      "startedAtNs": time.time_ns()},
        }
        record_path = compat.state_dir(env) / f"{run_id}.json"
        cmd = task_command(plugin, args.effort, prompt, node=node, write=args.write,
                           background=bool(args.background))
        fallback = False
        # Persist the run baseline first; a correction round is only committed after success.
        compat.save_record(record_path, record)
        if args.background:
            return enqueue_round(cmd, record, record_path, run_id=run_id, report=report,
                                 compat=compat, runner=runner, env=env, repo=repo, baseline=True)
    else:
        record_path, record = resolve_record(compat, env, args.run_id)
        metadata = record.get("codex")
        if not isinstance(metadata, dict) or not metadata.get("sessionId"):
            raise CxError(f"record {args.run_id}: no Codex sessionId; inspect its report")
        if not same_repo(record.get("cwd"), repo):
            raise CxError(f"record {args.run_id}: cwd differs from repo {repo}")
        plugin = discover_plugin(home)
        node = executable("node", which=which, env=env)
        fallback = not resume_candidate_matches(plugin, metadata["sessionId"],
                                                node=node, runner=runner, env=env, repo=repo)
        round_number = int(record["rounds"]) + 1
        run_id = f'{record["runIds"][0]}.r{round_number}'
        report = prepare_report(f".cx/{run_id}.report.md", repo, args.prompt)
        if fallback:
            contract_text = metadata.get("contractText")
            if not isinstance(contract_text, str) or not contract_text.strip():
                raise CxError(f"record {args.run_id}: missing codex.contractText; cannot rebuild original contract")
            paths = metadata.get("reportPaths", [])
            if not paths:
                raise CxError(f"record {args.run_id}: no previous report path")
            previous_report = relative_path(paths[-1], repo)
            require_file(previous_report)
            previous_text = previous_report.read_text(encoding="utf-8")[-8000:]
            prompt = (f"Correction round {round_number} on a new thread.\n\n{prompt}"
                      f"\n\nPREVIOUS REPORT\n{previous_text}"
                      f"\n\nORIGINAL CONTRACT\n{contract_text}")
        cmd = task_command(plugin, args.effort, prompt, node=node,
                           write=metadata["write"], resume=not fallback,
                           background=bool(args.background))
        print("correction path: " + ("fresh thread (contract + last report + correction)"
                                     if fallback else "plugin task --resume-last"))
        record["rounds"] = round_number
        record["runIds"].append(run_id)
        record["roundStartedAt"] = now
        metadata["effort"] = args.effort
        metadata["reportPaths"].append(str(report))
        metadata["jobId"] = None
        metadata["background"] = bool(args.background)
        metadata["endedAt"] = None
        metadata["jobStatus"] = None
        metadata["resume"] = not fallback
        if args.background:
            return enqueue_round(cmd, record, record_path, run_id=run_id, report=report,
                                 compat=compat, runner=runner, env=env, repo=repo, baseline=False)

    result, stdout = execute(cmd, runner=runner, env=env, repo=repo)
    report_text = stdout
    if result.returncode:
        stderr = filtered(result.stderr)
        report_text += ("\n" if stdout and stderr and not stdout.endswith("\n") else "") + stderr
    report.write_text(report_text, encoding="utf-8")
    if result.returncode and args.command == "fix":
        print(f"cx: worker failed (exit {result.returncode}); see {report}", file=sys.stderr)
        return result.returncode
    # Plugin releases differ: progress may be on stderr, final answer on stdout.
    match = THREAD.search(stdout + "\n" + filtered(result.stderr))
    if match:
        session = match[1]
        if args.command == "fix" and not fallback and session != record["codex"]["sessionId"]:
            raise CxError(f"resume-last returned unexpected thread {session}; expected {record['threadId']}; see {report}")
        if args.command == "fix" and session != record["codex"]["sessionId"]:
            record["codex"].setdefault("previousSessions", []).append(record["codex"]["sessionId"])
        record["threadId"] = record["codex"]["sessionId"] = session
    elif args.command == "fix" and fallback:
        raise CxError(f"no Thread ready session id in {report}; fresh correction not recorded")
    record["codex"]["lastExit"] = result.returncode
    record["codex"]["endedAt"] = int(time.time() * 1000)
    compat.save_record(record_path, record)
    if result.returncode:
        print(f"cx: worker failed (exit {result.returncode}); see {report}", file=sys.stderr)
        return result.returncode
    if not record["codex"]["sessionId"]:
        print(f"cx: no Thread ready session id in {report}; corrections unavailable", file=sys.stderr)
    print(f"run id: {run_id}\nthread id: {record['threadId'] or 'unavailable'}\nreport: {report}")
    print(f"next: zcrew cx gate {run_id}")
    return result.returncode


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="zcrew cx", description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("run", "fix", "audit"):
        sub = commands.add_parser(name)
        if name == "fix":
            sub.add_argument("run_id")
        sub.add_argument("prompt", help="UTF-8 contract/correction/audit Markdown file")
        sub.add_argument("--effort", choices=EFFORTS, default="high")
        if name == "run":
            sub.add_argument("--write", action="store_true")
            sub.add_argument("--allow-dirty", action="store_true",
                             help="dispatch even if SCOPE files are already dirty (gate will not see them)")
        if name != "fix":
            sub.add_argument("--report")
        if name != "audit":
            sub.add_argument("--background", action="store_true",
                             help="enqueue through the companion and return at once; settle it with 'zcrew cx status'")
    gate = commands.add_parser("gate")
    gate.add_argument("run_id")
    gate.add_argument("extra", nargs=argparse.REMAINDER,
                      help="extra zcrew gate options, e.g. --json or --accept \"...\"")
    status = commands.add_parser("status")
    status.add_argument("dir", nargs="?", default=None, metavar="DIR")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("--active", action="store_true",
                        help="only runs from the last 24 h without a verdict (ended ones included)")
    return cli


def main(argv: list[str] | None = None, *, runner: Runner | None = None,
         which: Callable[..., str | None] | None = None, env: Mapping[str, str] | None = None,
         home: Path | None = None, repo: Path | None = None) -> int:
    # Piped stdio on Windows defaults to a legacy code page; human lines can carry
    # any Unicode title or path, so replace unencodable characters instead of dying.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    args = parser().parse_args(argv)
    try:
        return dispatch(args, runner=runner or subprocess.run,
                        which=shutil.which if which is None else which,
                        env=os.environ if env is None else env,
                        home=Path.home() if home is None else Path(home),
                        repo=Path.cwd() if repo is None else Path(repo).resolve())
    except (CxError, OSError, UnicodeError) as exc:
        print(f"cx: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
