"""zcrew gate - mechanical review gate for a ZCode worker run.

``zcrew gate [RUN|THREAD] [--self] [--no-verify] [--accept NOTE | --reject NOTE]``

Reads the worker contract the bridge recorded at agent-start
(bridge_compat.record_start: ~/.zcode-commander/gate/<runId>.json) and checks
what a program can check, so the reviewer only spends judgment on the rest:

- scope      changed files all match the SCOPE patterns written in `backticks`
- do-not     no changed file matches a DO NOT pattern
- tests      removed test lines, skip markers and deleted test files (a warning:
             a person decides whether they were intended)
- verify     every `backticked` VERIFY command, run here; full output goes to a
             log file and only the failing lines are printed

The verdict is MECHANICAL: PASS / FAIL. Acceptance criteria written as plain
text are listed under JUDGMENT for the reviewer. ``--accept`` / ``--reject``
record the reviewer's decision (and bump nothing else); ``--self`` is the same
check without recording, for the worker to run before it reports.

Read-only for the repository: it runs git read commands and the VERIFY commands
the contract names, and writes only its own record and logs.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping

DEFAULT_MAX_ROUNDS = 4
DEFAULT_VERIFY_TIMEOUT = 900
# VERIFY runs queue machine-wide: parallel workers' test suites (browsers,
# builds) slow each other down and turn flaky when they run at once.
QUEUE_WAIT_SECONDS = 900
QUEUE_STALE_SECONDS = 2 * 3600
QUEUE_POLL_SECONDS = 2.0
LINE_CLIP = 160
FAIL_LINES = 6
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?)/|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$"
    r"|_test\.(py|go)$|Tests?\.cs$",
    re.IGNORECASE,
)
REMOVED_TEST_RE = re.compile(
    r"^-\s*(def test_|async def test_|it\(|test\(|describe\(|\[Fact|\[Theory|\[Test|@Test|func Test|assert|expect\()"
)
SKIP_RE = re.compile(
    r"^\+.*(@(unittest\.)?skip|pytest\.mark\.skip|\bxit\(|\bxdescribe\(|\.skip\(|\.only\(|\[Ignore|t\.Skip\()"
)
ERRORISH_RE = re.compile(r"fail|error|assert|expected|exception|traceback|cannot|not found", re.IGNORECASE)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_LIB_NAME = "_zcrew_bridge_compat"


def _compat() -> ModuleType:
    """bridge_compat.py next to this file; it owns the record format."""
    module = sys.modules.get(_LIB_NAME)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(_LIB_NAME, Path(__file__).resolve().parent / "bridge_compat.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot load bridge_compat.py next to gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GateError(Exception):
    """A problem the user can fix; printed as one line."""


# --- pure helpers ------------------------------------------------------------------


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def pattern_regex(pattern: str) -> "re.Pattern[str]":
    """``**`` crosses folders, ``*`` and ``?`` stay inside one; a pattern without
    wildcards also matches everything under it (``src/hero`` and ``src/hero/``)."""
    pat = _norm(pattern).rstrip("/")
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    tail = "" if "*" in pat or "?" in pat else "(/.*)?"
    return re.compile("^" + out + tail + "$", re.IGNORECASE if os.name == "nt" else 0)


def matches_any(path: str, patterns: list[str]) -> bool:
    path = _norm(path)
    return any(pattern_regex(p).match(path) for p in patterns)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(_norm(path)))


def clip(text: str, limit: int = LINE_CLIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def failing_lines(output: str, limit: int = FAIL_LINES) -> list[str]:
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    picked = [ln for ln in lines if ERRORISH_RE.search(ln)][:limit]
    return [clip(ln) for ln in (picked or lines[-limit:])]


def scan_test_diff(diff: str) -> dict[str, dict[str, int]]:
    """Per test file: removed test lines and added skip markers, from ``git diff -U0``."""
    found: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = _norm(target[2:]) if target.startswith("b/") else None
        elif line.startswith("--- ") or line.startswith("@@") or current is None:
            continue
        elif REMOVED_TEST_RE.match(line):
            found.setdefault(current, {"removed": 0, "skips": 0})["removed"] += 1
        elif SKIP_RE.match(line):
            found.setdefault(current, {"removed": 0, "skips": 0})["skips"] += 1
    return found


# --- records and config --------------------------------------------------------------


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    def norm(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    return norm(a) == norm(b)


def resolve_record(env: Mapping[str, str], ident: str | None, cwd: str) -> tuple[Path, dict[str, Any]]:
    compat = _compat()
    files = compat.record_files(env)
    if not files:
        raise GateError(f"no gate records yet in {compat.state_dir(env)} (they are written by agent-start)")
    if ident:
        hits = []
        for path in files:
            data = compat._read_json(path) or {}
            ids = [str(data.get("threadId") or "")] + [str(r) for r in data.get("runIds") or []]
            if any(i == ident or (len(ident) >= 4 and (i.startswith(ident) or i.startswith("sess_" + ident))) for i in ids):
                hits.append((path, data))
        if not hits:
            raise GateError(f"no gate record matches {ident!r}")
        if len(hits) > 1:
            raise GateError(f"{ident!r} matches {len(hits)} records; give a longer id")
        return hits[0]
    for path in files:
        data = compat._read_json(path) or {}
        if _same_dir(data.get("cwd"), cwd):
            return path, data
    raise GateError(f"no gate record for {cwd}; pass a run id or thread id")


def max_rounds(cwd: str | None, env: Mapping[str, str]) -> int:
    compat = _compat()
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    paths = ([Path(cwd) / ".claude" / "zcode-commander.json"] if cwd else []) + [home / ".zcode-commander" / "config.json"]
    for path in paths:
        value = (compat._read_json(path) or {}).get("maxCorrectionRounds")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return DEFAULT_MAX_ROUNDS


# --- git ------------------------------------------------------------------------------


def _git(cwd: str, *args: str, run: Runner = subprocess.run) -> str:
    try:
        done = run(["git", "-C", cwd, *args], capture_output=True, text=True, errors="replace", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise GateError(f"git {args[0]} failed: {clip(done.stderr or done.stdout)}")
    return done.stdout


def changed_files(cwd: str, base: str, dirty: list[str], run: Runner = subprocess.run) -> list[str]:
    tracked = _git(cwd, "diff", "--name-only", base, run=run).splitlines()
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", run=run).splitlines()
    skip = {_norm(p) for p in dirty}
    seen: dict[str, None] = {}
    for path in tracked + untracked:
        if path.strip() and _norm(path) not in skip:
            seen[_norm(path)] = None
    return sorted(seen)


# --- checks ---------------------------------------------------------------------------


def check(name: str, status: str, detail: str = "", lines: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "lines": lines or []}


def check_scope(files: list[str], scope: list[str]) -> dict[str, Any]:
    if not scope:
        return check("scope", "SKIP", "no `backticked` path in SCOPE, so nothing to check by program")
    outside = [f for f in files if not matches_any(f, scope)]
    if outside:
        return check("scope", "FAIL", f"{len(outside)} of {len(files)} changed files outside SCOPE", outside[:8])
    return check("scope", "PASS", f"{len(files)} files changed, all inside {', '.join(scope)}")


def check_do_not(files: list[str], do_not: list[str]) -> dict[str, Any]:
    if not do_not:
        return check("do-not", "SKIP", "no `backticked` path in DO NOT")
    hit = [f for f in files if matches_any(f, do_not)]
    if hit:
        return check("do-not", "FAIL", f"{len(hit)} changed files match DO NOT", hit[:8])
    return check("do-not", "PASS", f"no change in {', '.join(do_not)}")


def check_tests(cwd: str, base: str, files: list[str], run: Runner = subprocess.run) -> dict[str, Any]:
    deleted = [
        _norm(p) for p in _git(cwd, "diff", "--name-only", "--diff-filter=D", base, run=run).splitlines()
        if is_test_path(p)
    ]
    touched = [f for f in files if is_test_path(f) and f not in deleted]
    notes = [f"deleted test file {p}" for p in deleted]
    if touched:
        diff = _git(cwd, "diff", "-U0", base, "--", *touched, run=run)
        for path, counts in sorted(scan_test_diff(diff).items()):
            if counts["removed"]:
                notes.append(f"{path}: {counts['removed']} test/assert line(s) removed")
            if counts["skips"]:
                notes.append(f"{path}: {counts['skips']} skip/only marker(s) added")
    if notes:
        return check("tests", "WARN", "check these were intended", notes[:8])
    return check("tests", "PASS", "no removed tests, skip markers or deleted test files")


def check_verify(
    commands: list[str], cwd: str, log_dir: Path, tag: str, timeout: int,
    run: Runner = subprocess.run,
) -> list[dict[str, Any]]:
    if not commands:
        return [check("verify", "SKIP", "no `backticked` command in VERIFY")]
    log_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for n, command in enumerate(commands, 1):
        log = log_dir / f"{tag}-verify{n}.log"
        started = time.monotonic()
        try:
            done = run(command, shell=True, cwd=cwd, capture_output=True, text=True, errors="replace",
                       timeout=timeout, check=False)
            text, code = (done.stdout or "") + (done.stderr or ""), done.returncode
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            code = None
        except OSError as exc:
            text, code = str(exc), 127
        try:
            log.write_text(text, encoding="utf-8")
        except OSError:
            pass
        took = f"{time.monotonic() - started:.0f}s"
        if code == 0:
            out.append(check("verify", "PASS", f"{command} -> exit 0 ({took})"))
        elif code is None:
            out.append(check("verify", "FAIL", f"{command} -> timed out after {timeout}s (log: {log})",
                             failing_lines(text)))
        else:
            out.append(check("verify", "FAIL", f"{command} -> exit {code} ({took}, log: {log})", failing_lines(text)))
    return out


@contextlib.contextmanager
def verify_queue(
    lock: Path, wait: float, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> Iterator[dict[str, Any]]:
    """Hold the machine-wide VERIFY lock; yields ``{"waited": s, "held": bool}``.

    Waits at most ``wait`` seconds, then runs without the lock rather than block
    a review forever; a lock older than QUEUE_STALE_SECONDS (a gate that died) is
    taken over. ``wait <= 0`` skips the queue.
    """
    start, held = clock(), False
    if wait > 0:
        lock.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > QUEUE_STALE_SECONDS:
                        lock.unlink()
                        continue
                except OSError:
                    continue  # released between the two calls: try again
                if clock() - start >= wait:
                    break
                sleep(QUEUE_POLL_SECONDS)
                continue
            os.write(fd, f"{os.getpid()} {int(time.time())}".encode())
            os.close(fd)
            held = True
            break
    try:
        yield {"waited": round(clock() - start, 1), "held": held}
    finally:
        if held:
            with contextlib.suppress(OSError):
                lock.unlink()


def draft_correction(checks: list[dict[str, Any]], rnd: int, budget: int) -> str:
    lines = [f"REVIEW RESULT: FAIL (correction round {rnd} of {budget})"]
    for item in checks:
        if item["status"] not in ("FAIL", "WARN"):
            continue
        lines.append(f"- {item['name']}: {item['detail']}")
        lines.extend(f"    {ln}" for ln in item["lines"][:4])
    lines.append("Scope unchanged. Fix only these.")
    return "\n".join(lines)


def build_report(
    data: dict[str, Any], *, cwd: str, env: Mapping[str, str], verify: bool, self_check: bool,
    timeout: int, run: Runner = subprocess.run, log_dir: Path | None = None,
    queue_wait: float = QUEUE_WAIT_SECONDS,
) -> dict[str, Any]:
    contract = data.get("contract") or {}
    work_dir = data.get("cwd") or cwd
    base = data.get("base")
    if not base:
        raise GateError("the record has no git base commit (was the folder a git repository at agent-start?)")
    files = changed_files(work_dir, base, data.get("dirty") or [], run)
    checks = [check_scope(files, contract.get("scope") or []), check_do_not(files, contract.get("do_not") or []),
              check_tests(work_dir, base, files, run)]
    tag = (data.get("runIds") or ["run"])[-1]
    queue: dict[str, Any] = {"waited": 0.0, "held": False}
    if verify:
        commands = contract.get("verify") or []
        lock = _compat().state_dir(env) / "verify.lock"
        with verify_queue(lock, queue_wait if commands else 0) as queue:
            checks.extend(check_verify(commands, work_dir, log_dir or (Path(tempfile.gettempdir()) / "zcrew-gate"),
                                       tag, timeout, run))
    else:
        checks.append(check("verify", "SKIP", "--no-verify"))
    failed = [c for c in checks if c["status"] == "FAIL"]
    sections = contract.get("sections") or {}
    judgment = list(sections.get("ACCEPTANCE CRITERIA") or [])
    judgment += [i for i in sections.get("SCOPE") or [] if not re.search(r"`[^`]+`", i)]
    budget = max_rounds(work_dir, env)
    rnd = max(0, int(data.get("rounds", 1)) - 1)
    report: dict[str, Any] = {
        "run": tag, "thread": data.get("threadId"), "title": clip(" ".join((sections.get("OBJECTIVE") or [""])[:1]), 60),
        "round": rnd, "budget": budget, "changed": files, "dirty_excluded": len(data.get("dirty") or []),
        "checks": checks, "judgment": judgment, "mechanical": "FAIL" if failed else "PASS",
        "contract_missing": contract.get("missing") or [], "self": self_check,
        "queue": {"waited": queue["waited"], "held": queue["held"]},
    }
    if failed and not self_check:
        if rnd >= budget:
            report["budget_exhausted"] = True
        else:
            report["draft"] = draft_correction(checks, rnd + 1, budget)
    return report


# --- output ---------------------------------------------------------------------------


def render(report: dict[str, Any]) -> str:
    head = "SELF-CHECK" if report["self"] else "GATE"
    where = f"correction round {report['round']} of {report['budget']}" if report["round"] else "initial run"
    out = [f"{head}  {report['run']}  \"{report['title']}\"  ({where})", ""]
    for item in report["checks"]:
        out.append(f"[{item['status']:<4}] {item['name']:<8} {item['detail']}")
        out.extend(f"         {ln}" for ln in item["lines"])
    if report["dirty_excluded"]:
        out.append(f"note: {report['dirty_excluded']} file(s) were already modified before the worker started and are left out")
    queue = report.get("queue") or {}
    if queue.get("waited", 0) >= 1:
        how = "then ran" if queue.get("held") else "then ran without the queue"
        out.append(f"note: waited {queue['waited']:.0f}s for another gate's VERIFY, {how}")
    if report["contract_missing"]:
        out.append("note: the contract has no " + ", ".join(report["contract_missing"]) + "; those parts are not checked")
    if report["judgment"] and not report["self"]:
        out += ["", "JUDGMENT (you decide, from the diff):"] + [f"  - {clip(j, 140)}" for j in report["judgment"]]
    out += ["", f"MECHANICAL: {report['mechanical']}" + ("" if report["mechanical"] == "PASS" else "  -> do not accept")]
    if report.get("budget_exhausted"):
        out.append("BUDGET EXHAUSTED: stop and report to the user what was tried, what fails, and what decision you need.")
    elif report.get("draft"):
        out += ["", "--- draft correction (edit, then send to the same thread) ---", report["draft"]]
    return "\n".join(out) + "\n"


# --- verdict --------------------------------------------------------------------------


def record_verdict(path: Path, data: dict[str, Any], verdict: str, note: str, mechanical: str,
                   override: str | None) -> None:
    entry: dict[str, Any] = {
        "at": int(time.time() * 1000), "round": max(0, int(data.get("rounds", 1)) - 1),
        "verdict": verdict, "mechanical": mechanical, "note": note,
    }
    if override:
        entry["override"] = override
    data.setdefault("verdicts", []).append(entry)
    _compat().save_record(path, data)


def run(args: Any, *, env: Mapping[str, str] | None = None, out: Any = None, runner: Runner = subprocess.run) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    cwd = os.getcwd()
    try:
        path, data = resolve_record(env, args.ident, cwd)
        report = build_report(
            data, cwd=cwd, env=env, verify=not args.no_verify, self_check=args.self_check,
            timeout=args.verify_timeout, run=runner,
            queue_wait=0 if getattr(args, "no_queue", False) else QUEUE_WAIT_SECONDS,
        )
        if args.accept is not None or args.reject is not None:
            verdict = "accept" if args.accept is not None else "reject"
            if verdict == "accept" and report["mechanical"] == "FAIL" and not args.override:
                raise GateError("the mechanical check FAILED; fix it, or accept anyway with --override \"reason\"")
            record_verdict(path, data, verdict, (args.accept if verdict == "accept" else args.reject) or "",
                           report["mechanical"], args.override)
            report["recorded"] = verdict
    except GateError as exc:
        sys.stderr.write(f"zcrew: {exc}\n")
        return 2
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        out.write(render(report))
        if report.get("recorded"):
            out.write(f"recorded: {report['recorded']}\n")
    return 0 if report["mechanical"] == "PASS" else 1
