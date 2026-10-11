"""zcrew plan / zcrew worktree - the task plan and parallel worktrees.

``.zcrew/plan.json`` (written by the commander once the user approves a plan)
lists tasks with the files they touch and the tasks they wait for::

    {"tasks": [
      {"id": "T1", "title": "Hero section", "files": ["src/hero/**"], "after": []},
      {"id": "T3", "title": "Integrate", "files": ["src/app.ts"], "after": ["T1", "T2"]}
    ]}

A worker contract names its task with a ``TASK: T1`` line; the bridge records
it (bridge_compat.parse_contract) and ``zcrew gate --accept`` records the
verdict, so the plan's state is read from the gate records alone:

- accepted  the last verdict for the task is accept
- started   a run exists for the task, no accept yet (working, in review, or sent back)
- ready     every task it waits for is accepted, and no run exists yet
- waiting   some task it waits for is not accepted yet

``zcrew plan`` prints that, the ready tasks, and warnings: unknown or circular
dependencies, and tasks that touch the same files without one waiting for the
other (they cannot safely run at once). ``zcrew worktree add TASK`` creates
``../<repo>-zc-<task>`` on branch ``zc/<task>`` and links the main checkout's
``node_modules`` into it, so a parallel worker does not install everything
again; ``zcrew worktree remove TASK`` unlinks that first, because deleting a
worktree through a link could otherwise reach the shared folder.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

PLAN_REL = Path(".zcrew") / "plan.json"
SHARED_DIRS = ("node_modules",)
DEFAULT_MAX_WORKERS = 5
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_LIBS: dict[str, ModuleType] = {}


def _sibling(name: str) -> ModuleType:
    if name not in _LIBS:
        spec = importlib.util.spec_from_file_location(f"_zcrew_{name}", Path(__file__).resolve().parent / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {name}.py next to plan.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _LIBS[name] = module
    return _LIBS[name]


class PlanError(Exception):
    """A problem the user can fix; printed as one line."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    files: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


# --- plan file -------------------------------------------------------------------------


def plan_path(project: Path) -> Path:
    return Path(project) / PLAN_REL


def load_plan(project: Path) -> list[Task]:
    path = plan_path(project)
    if not path.is_file():
        raise PlanError(f"no plan at {path} (the commander writes it once you approve a plan)")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path} is not valid JSON ({exc})") from None
    raw = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise PlanError(f'{path} needs {{"tasks": [...]}}')
    tasks = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PlanError(f"task #{n} in {path} has no string id")
        tasks.append(Task(
            id=item["id"].strip(),
            title=str(item.get("title") or item["id"]).strip(),
            files=[str(f) for f in item.get("files") or [] if str(f).strip()],
            after=[str(a).strip() for a in item.get("after") or [] if str(a).strip()],
        ))
    return tasks


def _overlap(a: list[str], b: list[str]) -> list[str]:
    """Patterns of ``a`` that can name the same files as one of ``b``: a path one
    pattern matches, or one pattern's fixed prefix lying under the other's."""
    gate = _sibling("gate")

    def prefix(p: str) -> str:
        p = gate._norm(p)
        cut = min([i for i in (p.find("*"), p.find("?")) if i >= 0] or [len(p)])
        return p[:cut].rstrip("/")

    hits = []
    for pa in a:
        for pb in b:
            fa, fb = prefix(pa), prefix(pb)
            if not fa or not fb or gate.matches_any(fa, [pb]) or gate.matches_any(fb, [pa]) or \
                    (fa and fb and (fa.startswith(fb + "/") or fb.startswith(fa + "/") or fa == fb)):
                hits.append(f"{pa} ~ {pb}")
    return hits


def _depends(tasks: dict[str, Task], start: str, target: str) -> bool:
    """Whether ``start`` waits for ``target``, directly or through other tasks."""
    seen, todo = set(), list(tasks[start].after)
    while todo:
        dep = todo.pop()
        if dep == target:
            return True
        if dep in seen or dep not in tasks:
            continue
        seen.add(dep)
        todo.extend(tasks[dep].after)
    return False


def validate(tasks: list[Task]) -> list[str]:
    problems: list[str] = []
    by_id: dict[str, Task] = {}
    for t in tasks:
        if not ID_RE.match(t.id):
            problems.append(f"task id {t.id!r}: use letters, digits, '-', '_' or '.' (it names a branch)")
        if t.id in by_id:
            problems.append(f"task id {t.id} appears twice")
        by_id[t.id] = t
    for t in tasks:
        for dep in t.after:
            if dep not in by_id:
                problems.append(f"{t.id} waits for unknown task {dep}")
        if _depends(by_id, t.id, t.id):
            problems.append(f"{t.id} waits for itself (a dependency cycle)")
    ids = list(by_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if _depends(by_id, a, b) or _depends(by_id, b, a):
                continue
            shared = _overlap(by_id[a].files, by_id[b].files)
            if shared:
                problems.append(f"{a} and {b} touch the same files ({shared[0]}) but neither waits for the other:"
                                " they cannot run at once; add one to the other's \"after\"")
    return problems


# --- state from gate records -------------------------------------------------------------


def task_records(env: Mapping[str, str], project: Path) -> dict[str, list[dict[str, Any]]]:
    """Gate records per task id, for runs started in this project or its worktrees."""
    compat = _sibling("bridge_compat")
    root = os.path.normcase(os.path.realpath(project))
    parent = os.path.dirname(root)
    name = os.path.basename(root)
    out: dict[str, list[dict[str, Any]]] = {}
    for path in reversed(compat.record_files(env)):  # oldest first
        data = compat._read_json(path) or {}
        task = (data.get("contract") or {}).get("task")
        cwd = os.path.normcase(os.path.realpath(data.get("cwd") or ""))
        mine = cwd == root or cwd.startswith(root + os.sep) or \
            (os.path.dirname(cwd) == parent and os.path.basename(cwd).startswith(name + "-zc-"))
        if task and mine:
            out.setdefault(str(task), []).append(data)
    return out


def task_state(task: Task, records: dict[str, list[dict[str, Any]]], accepted: set[str]) -> str:
    if task.id in accepted:
        return "accepted"
    if records.get(task.id):
        return "started"
    return "ready" if all(dep in accepted for dep in task.after) else "waiting"


def accepted_ids(records: dict[str, list[dict[str, Any]]]) -> set[str]:
    done = set()
    for task, runs in records.items():
        verdicts = [v for r in runs for v in (r.get("verdicts") or [])]
        verdicts.sort(key=lambda v: v.get("at", 0))
        if verdicts and verdicts[-1].get("verdict") == "accept":
            done.add(task)
    return done


def max_workers(project: Path, env: Mapping[str, str]) -> int:
    compat = _sibling("bridge_compat")
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    for path in (Path(project) / ".claude" / "zcode-commander.json", home / ".zcode-commander" / "config.json"):
        value = (compat._read_json(path) or {}).get("maxWorkers")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return DEFAULT_MAX_WORKERS


def plan_report(project: Path, env: Mapping[str, str]) -> dict[str, Any]:
    tasks = load_plan(project)
    records = task_records(env, project)
    accepted = accepted_ids(records)
    rows = [{**asdict(t), "state": task_state(t, records, accepted), "runs": len(records.get(t.id, []))} for t in tasks]
    return {
        "plan": str(plan_path(project)),
        "tasks": rows,
        "ready": [r["id"] for r in rows if r["state"] == "ready"],
        "started": [r["id"] for r in rows if r["state"] == "started"],
        "accepted": len([r for r in rows if r["state"] == "accepted"]),
        "total": len(rows),
        "max_workers": max_workers(project, env),
        "problems": validate(tasks),
    }


ICONS = {"accepted": "✅", "started": "🔧", "ready": "▶", "waiting": "⏳"}


def render(report: dict[str, Any]) -> str:
    width = max([len(r["id"]) for r in report["tasks"]] + [2])
    out = [f"plan  {report['accepted']}/{report['total']} accepted  ({report['plan']})", ""]
    for r in report["tasks"]:
        note = f"waits for {', '.join(r['after'])}" if r["state"] == "waiting" else r["state"]
        out.append(f"{ICONS[r['state']]} {r['id']:<{width}}  {r['title'][:50]:<50}  {note}")
    if report["ready"]:
        out += ["", f"ready to start: {', '.join(report['ready'])}"
                    f"  (started {len(report['started'])} of {report['max_workers']} workers)"]
    for p in report["problems"]:
        out.append(f"warning: {p}")
    return "\n".join(out) + "\n"


# --- worktrees -----------------------------------------------------------------------


def _git(project: Path, *args: str, run: Runner = subprocess.run) -> str:
    done = run(["git", "-C", str(project), *args], capture_output=True, text=True, errors="replace", check=False)
    if done.returncode != 0:
        raise PlanError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


def worktree_path(project: Path, task_id: str) -> Path:
    root = Path(project).resolve()
    return root.parent / f"{root.name}-zc-{task_id}"


def is_link(path: Path) -> bool:
    """A symbolic link or (Windows) a junction: removing it never touches its target."""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    if isjunction and isjunction(path):
        return True
    try:
        os.readlink(path)  # also reads junctions on Windows
        return True
    except (OSError, ValueError):
        return False


def link_dir(target: Path, link: Path, run: Runner = subprocess.run) -> None:
    if os.name == "nt":  # a junction needs no admin rights or developer mode
        done = run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise PlanError(f"could not link {link}: {(done.stderr or done.stdout).strip()[:200]}")
    else:
        os.symlink(target, link, target_is_directory=True)


def unlink_dir(link: Path) -> None:
    if os.name == "nt" and not link.is_symlink():
        os.rmdir(link)  # removes a junction, never what it points to
    else:
        os.unlink(link)


def worktree_add(project: Path, task_id: str, *, base: str | None, share: bool,
                 run: Runner = subprocess.run) -> tuple[Path, list[str]]:
    if not ID_RE.match(task_id):
        raise PlanError(f"task id {task_id!r}: use letters, digits, '-', '_' or '.'")
    root = Path(_git(project, "rev-parse", "--show-toplevel", run=run).strip())
    path = worktree_path(root, task_id)
    if path.exists():
        raise PlanError(f"{path} already exists")
    _git(root, "worktree", "add", str(path), "-b", f"zc/{task_id}", *([base] if base else []), run=run)
    linked = []
    if share:
        for name in SHARED_DIRS:
            source = root / name
            if source.is_dir() and not (path / name).exists():
                link_dir(source, path / name, run)
                linked.append(name)
    return path, linked


def worktree_remove(project: Path, task_id: str, *, force: bool, run: Runner = subprocess.run) -> Path:
    root = Path(_git(project, "rev-parse", "--show-toplevel", run=run).strip())
    path = worktree_path(root, task_id)
    if not path.exists():
        raise PlanError(f"{path} does not exist")
    for name in SHARED_DIRS:
        if is_link(path / name):
            unlink_dir(path / name)
    _git(root, "worktree", "remove", *(["--force"] if force else []), str(path), run=run)
    return path


# --- entry point -------------------------------------------------------------------------


def run(args: Any, *, env: Mapping[str, str] | None = None, out: Any = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    project = Path(getattr(args, "dir", None) or os.getcwd())
    try:
        if args.command == "plan":
            report = plan_report(project, env)
            out.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n" if args.json else render(report))
            return 0
        if args.action == "add":
            path, linked = worktree_add(project, args.task, base=args.base, share=not args.no_shared)
            out.write(f"created {path} on branch zc/{args.task}\n")
            for name in linked:
                out.write(f"linked {name} from the main checkout (remove with: zcrew worktree remove {args.task})\n")
            return 0
        path = worktree_remove(project, args.task, force=args.force)
        out.write(f"removed {path} (branch zc/{args.task} kept; delete it once merged)\n")
        return 0
    except PlanError as exc:
        sys.stderr.write(f"zcrew: {exc}\n")
        return 2
