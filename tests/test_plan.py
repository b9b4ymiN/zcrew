from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan = _load("plan")
compat = plan._sibling("bridge_compat")
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def task(tid: str, files: list[str], after: list[str] | None = None) -> dict:
    return {"id": tid, "title": f"Task {tid}", "files": files, "after": after or []}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "web"
        self.project.mkdir()
        self.env = {"ZCODE_COMMANDER_STATE": str(self.root / "state"), "HOME": str(self.root),
                    "USERPROFILE": str(self.root)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_plan(self, tasks: list[dict]) -> None:
        (self.project / ".zcrew").mkdir(exist_ok=True)
        (self.project / ".zcrew" / "plan.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")

    def record(self, run_id: str, task_id: str, verdicts: list[str], cwd: Path | None = None) -> None:
        data = {"runIds": [run_id], "cwd": str(cwd or self.project), "contract": {"task": task_id},
                "verdicts": [{"verdict": v, "at": i} for i, v in enumerate(verdicts)]}
        compat.save_record(compat.state_dir(self.env) / f"{run_id}.json", data)
        time.sleep(0.01)  # records are ordered by mtime


class LoadAndValidateTests(Fixture):
    def test_missing_and_bad_plans(self) -> None:
        with self.assertRaises(plan.PlanError):
            plan.load_plan(self.project)
        (self.project / ".zcrew").mkdir()
        (self.project / ".zcrew" / "plan.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            plan.load_plan(self.project)
        (self.project / ".zcrew" / "plan.json").write_text('{"tasks": [{"title": "no id"}]}', encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            plan.load_plan(self.project)

    def test_clean_plan(self) -> None:
        self.write_plan([task("T1", ["src/hero/**"]), task("T2", ["src/footer/**"]),
                         task("T3", ["src/app.ts"], ["T1", "T2"])])
        self.assertEqual(plan.validate(plan.load_plan(self.project)), [])

    def test_problems(self) -> None:
        tasks = [plan.Task("T1", "a", ["src/shared/**"]), plan.Task("T2", "b", ["src/shared/util.ts"]),
                 plan.Task("T3", "c", [], ["T9"]), plan.Task("T4", "d", [], ["T5"]),
                 plan.Task("T5", "e", [], ["T4"]), plan.Task("bad id!", "f")]
        problems = "\n".join(plan.validate(tasks))
        self.assertIn("T1 and T2 touch the same files", problems)
        self.assertIn("T3 waits for unknown task T9", problems)
        self.assertIn("T4 waits for itself", problems)
        self.assertIn("'bad id!'", problems)
        self.assertIn("T1 appears twice", "\n".join(plan.validate([plan.Task("T1", "a"), plan.Task("T1", "b")])))

    def test_overlap_only_matters_without_a_dependency(self) -> None:
        ordered = [plan.Task("T1", "a", ["src/**"]), plan.Task("T2", "b", ["src/x.ts"], ["T1"])]
        self.assertEqual(plan.validate(ordered), [])
        apart = [plan.Task("T1", "a", ["src/hero/**"]), plan.Task("T2", "b", ["src/heroes/**"])]
        self.assertEqual(plan.validate(apart), [])
        everything = [plan.Task("T1", "a", ["**"]), plan.Task("T2", "b", ["docs/a.md"])]
        self.assertEqual(len(plan.validate(everything)), 1)


class StateTests(Fixture):
    def test_states_follow_gate_records(self) -> None:
        self.write_plan([task("T1", ["a/**"]), task("T2", ["b/**"]), task("T3", ["c/**"], ["T1", "T2"]),
                         task("T4", ["d/**"])])
        self.record("run_1", "T1", ["reject", "accept"])
        self.record("run_2", "T2", [])
        report = plan.plan_report(self.project, self.env)
        states = {r["id"]: r["state"] for r in report["tasks"]}
        self.assertEqual(states, {"T1": "accepted", "T2": "started", "T3": "waiting", "T4": "ready"})
        self.assertEqual((report["ready"], report["started"], report["accepted"], report["total"]),
                         (["T4"], ["T2"], 1, 4))
        self.assertEqual(report["max_workers"], 5)

    def test_runs_in_worktrees_count_other_projects_do_not(self) -> None:
        self.write_plan([task("T1", ["a/**"]), task("T2", ["b/**"])])
        self.record("run_w", "T1", ["accept"], cwd=self.root / "web-zc-T1")
        self.record("run_x", "T2", ["accept"], cwd=self.root / "other")
        states = {r["id"]: r["state"] for r in plan.plan_report(self.project, self.env)["tasks"]}
        self.assertEqual(states, {"T1": "accepted", "T2": "ready"})

    def test_max_workers_from_config(self) -> None:
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "zcode-commander.json").write_text('{"maxWorkers": 3}', encoding="utf-8")
        self.assertEqual(plan.max_workers(self.project, self.env), 3)

    def test_render_and_cli(self) -> None:
        self.write_plan([task("T1", ["a/**"]), task("T2", ["b/**"], ["T1"])])
        out = io.StringIO()
        code = plan.run(SimpleNamespace(command="plan", dir=str(self.project), json=False), env=self.env, out=out)
        self.assertEqual(code, 0)
        self.assertIn("plan  0/2 accepted", out.getvalue())
        self.assertIn("▶ T1", out.getvalue())
        self.assertIn("⏳ T2", out.getvalue())
        self.assertIn("waits for T1", out.getvalue())
        self.assertIn("ready to start: T1  (started 0 of 5 workers)", out.getvalue())
        out = io.StringIO()
        plan.run(SimpleNamespace(command="plan", dir=str(self.project), json=True), env=self.env, out=out)
        self.assertEqual(json.loads(out.getvalue())["ready"], ["T1"])
        code = plan.run(SimpleNamespace(command="plan", dir=str(self.root), json=False), env=self.env, out=io.StringIO())
        self.assertEqual(code, 2)


@unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows; the junction path is covered there")
class WorktreeTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self._saved = dict(os.environ)
        os.environ.update(GIT_ENV)
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        (self.project / "app.js").write_text("x\n", encoding="utf-8")
        (self.project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-q", "-m", "base"], check=True)
        (self.project / "node_modules" / "lib").mkdir(parents=True)
        (self.project / "node_modules" / "lib" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        super().tearDown()

    def test_add_links_node_modules_and_remove_keeps_the_original(self) -> None:
        path, linked = plan.worktree_add(self.project, "T1", base=None, share=True)
        self.assertEqual((path.name, linked), ("web-zc-T1", ["node_modules"]))
        self.assertTrue(plan.is_link(path / "node_modules"))
        self.assertTrue((path / "node_modules" / "lib" / "index.js").is_file())
        branches = subprocess.run(["git", "-C", str(self.project), "branch", "--list", "zc/T1"],
                                  capture_output=True, text=True, check=True).stdout
        self.assertIn("zc/T1", branches)
        plan.worktree_remove(self.project, "T1", force=True)
        self.assertFalse(path.exists())
        self.assertTrue((self.project / "node_modules" / "lib" / "index.js").is_file())  # the shared copy survives

    def test_no_shared_and_errors(self) -> None:
        path, linked = plan.worktree_add(self.project, "T2", base=None, share=False)
        self.assertEqual(linked, [])
        self.assertFalse((path / "node_modules").exists())
        with self.assertRaises(plan.PlanError):
            plan.worktree_add(self.project, "T2", base=None, share=True)  # exists
        with self.assertRaises(plan.PlanError):
            plan.worktree_add(self.project, "bad id!", base=None, share=True)
        with self.assertRaises(plan.PlanError):
            plan.worktree_remove(self.project, "T9", force=False)

    def test_is_link(self) -> None:
        self.assertFalse(plan.is_link(self.project / "node_modules"))


class WiringTests(unittest.TestCase):
    def test_parser(self) -> None:
        zcrew = _load("zcrew")
        parser = zcrew.build_parser()
        self.assertEqual(parser.parse_args(["plan", "--json"]).json, True)
        add = parser.parse_args(["worktree", "add", "T1", "--no-shared"])
        self.assertEqual((add.command, add.action, add.task, add.no_shared, add.base), ("worktree", "add", "T1", True, None))
        rm = parser.parse_args(["worktree", "remove", "T1", "--force"])
        self.assertEqual((rm.action, rm.force), ("remove", True))

    def test_contract_task_line(self) -> None:
        self.assertEqual(compat.parse_contract("TASK: T2\nOBJECTIVE\nx")["task"], "T2")
        self.assertIsNone(compat.parse_contract("OBJECTIVE\nx")["task"])


if __name__ == "__main__":
    unittest.main()
