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
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


gate = _load("gate")
compat = gate._compat()

PY = f'"{sys.executable}"'
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def contract(verify: list[str], scope: list[str] | None = None, do_not: list[str] | None = None) -> dict:
    scope = ["src/hero/**"] if scope is None else scope
    do_not = ["package.json", "migrations/"] if do_not is None else do_not
    return {
        "sections": {
            "OBJECTIVE": ["Add the hero section."],
            "SCOPE": [f"`{p}`" for p in scope] + ["tests for Hero when needed"],
            "DO NOT": [f"touch `{p}`" for p in do_not],
            "ACCEPTANCE CRITERIA": ["The title shows.", "Existing pages unchanged."],
            "VERIFY": [f"`{c}`" for c in verify],
        },
        "missing": [], "scope": scope, "do_not": do_not, "verify": verify,
    }


class PureTests(unittest.TestCase):
    def test_patterns(self) -> None:
        cases = [
            ("src/hero/a.ts", "src/hero/**", True),
            ("src/hero/deep/a.ts", "src/hero/**", True),
            ("src/hero.ts", "src/hero/**", False),
            ("src/hero/a.ts", "src/hero/*.ts", True),
            ("src/hero/deep/a.ts", "src/hero/*.ts", False),
            ("src/hero/deep/a.ts", "src/hero", True),  # no wildcard: everything under it
            ("src/hero/deep/a.ts", "src/hero/", True),
            ("src/heroes/a.ts", "src/hero", False),
            ("package.json", "package.json", True),
            ("web/package.json", "package.json", False),
            ("src\\hero\\a.ts", "src/hero/**", True),
            ("src/hero/a.ts", "./src/hero/**", True),
            ("a1.ts", "a?.ts", True),
        ]
        for path, pattern, expected in cases:
            self.assertEqual(gate.matches_any(path, [pattern]), expected, (path, pattern))

    def test_test_paths(self) -> None:
        for path in ("tests/test_x.py", "src/a.test.ts", "src/a.spec.tsx", "pkg/foo_test.go", "A/FooTests.cs",
                     "web/__tests__/a.js", "test_thing.py"):
            self.assertTrue(gate.is_test_path(path), path)
        for path in ("src/contest.py", "src/hero/Hero.tsx", "latest.py"):
            self.assertFalse(gate.is_test_path(path), path)

    def test_scan_test_diff(self) -> None:
        diff = (
            "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,3 +1,2 @@\n"
            "-    def test_old(self):\n-        self.assertEqual(1, 1)\n-    helper = 3\n"
            "+    @unittest.skip('later')\n+    def test_new(self):\n"
            "diff --git a/src/a.test.ts b/src/a.test.ts\n--- a/src/a.test.ts\n+++ b/src/a.test.ts\n@@ -2 +2 @@\n"
            "-  it('works', () => {\n+  it.skip('works', () => {\n"
        )
        found = gate.scan_test_diff(diff)
        self.assertEqual(found["tests/test_a.py"], {"removed": 1, "skips": 1})
        self.assertEqual(found["src/a.test.ts"], {"removed": 1, "skips": 1})

    def test_failing_lines_prefers_errorish_then_tail(self) -> None:
        out = "ok 1\nok 2\nFAIL src/a.test.ts renders\nExpected 1 received 2\nok 3\n"
        self.assertEqual(gate.failing_lines(out), ["FAIL src/a.test.ts renders", "Expected 1 received 2"])
        self.assertEqual(gate.failing_lines("a\nb\nc\n", limit=2), ["b", "c"])
        self.assertEqual(len(gate.failing_lines("error " + "x" * 400)[0]), gate.LINE_CLIP)

    def test_draft_correction(self) -> None:
        checks = [gate.check("scope", "PASS", "fine"),
                  gate.check("verify", "FAIL", "npm test -> exit 1", ["FAIL Hero renders"]),
                  gate.check("tests", "WARN", "check these", ["a.test.ts: 1 removed"])]
        draft = gate.draft_correction(checks, 2, 4)
        self.assertIn("REVIEW RESULT: FAIL (correction round 2 of 4)", draft)
        self.assertIn("- verify: npm test -> exit 1", draft)
        self.assertIn("    FAIL Hero renders", draft)
        self.assertNotIn("scope", draft)


class RepoFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {"ZCODE_COMMANDER_STATE": str(self.root / "state"), "HOME": str(self.root),
                    "USERPROFILE": str(self.root), **GIT_ENV}
        self._saved = dict(os.environ)
        os.environ.update(GIT_ENV)
        self.git("init", "-q")
        self.write("src/hero/Hero.ts", "export const hero = 1\n")
        self.write("src/hero/Hero.test.ts", "it('renders', () => {})\nit('titles', () => {})\n")
        self.write("package.json", "{}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def record(self, verify: list[str], run_id: str = "run_1", thread: str | None = "sess_abcd1234",
               rounds: int = 1, dirty: list[str] | None = None, **kw: object) -> dict:
        data = {
            "runIds": [run_id], "threadId": thread, "cwd": str(self.repo), "startedAt": 1, "rounds": rounds,
            "base": self.base, "dirty": dirty or [], "contract": contract(verify, **kw), "verdicts": [],
        }
        compat.save_record(compat.state_dir(self.env) / f"{run_id}.json", data)
        return data

    def report(self, data: dict, **kw: object) -> dict:
        args = dict(cwd=str(self.repo), env=self.env, verify=True, self_check=False, timeout=60,
                    log_dir=self.root / "logs")
        args.update(kw)
        return gate.build_report(data, **args)  # type: ignore[arg-type]

    @staticmethod
    def by_name(report: dict, name: str) -> list[dict]:
        return [c for c in report["checks"] if c["name"] == name]


class BuildReportTests(RepoFixture):
    def test_clean_run_passes_and_lists_judgment(self) -> None:
        self.write("src/hero/Hero.ts", "export const hero = 2\n")
        self.write("src/hero/New.ts", "export {}\n")  # untracked files count
        report = self.report(self.record([f"{PY} -c \"print('ok')\""]))
        self.assertEqual(report["mechanical"], "PASS")
        self.assertEqual(report["changed"], ["src/hero/Hero.ts", "src/hero/New.ts"])
        self.assertEqual(self.by_name(report, "scope")[0]["status"], "PASS")
        self.assertEqual(self.by_name(report, "verify")[0]["status"], "PASS")
        self.assertEqual(report["judgment"], ["The title shows.", "Existing pages unchanged.", "tests for Hero when needed"])
        self.assertNotIn("draft", report)

    def test_scope_and_do_not_failures_list_the_files(self) -> None:
        self.write("src/other/X.ts", "x\n")
        self.write("package.json", '{"a": 1}\n')
        report = self.report(self.record([]))
        self.assertEqual(report["mechanical"], "FAIL")
        scope, do_not = self.by_name(report, "scope")[0], self.by_name(report, "do-not")[0]
        self.assertEqual((scope["status"], scope["lines"]), ("FAIL", ["package.json", "src/other/X.ts"]))
        self.assertEqual((do_not["status"], do_not["lines"]), ("FAIL", ["package.json"]))
        self.assertIn("REVIEW RESULT: FAIL (correction round 1 of 4)", report["draft"])

    def test_failing_verify_shows_failing_lines_and_keeps_the_log(self) -> None:
        cmd = f"{PY} -c \"import sys; print('ok'); print('FAIL Hero renders'); sys.exit(3)\""
        report = self.report(self.record([cmd]))
        verify = self.by_name(report, "verify")[0]
        self.assertEqual(verify["status"], "FAIL")
        self.assertIn("exit 3", verify["detail"])
        self.assertEqual(verify["lines"], ["FAIL Hero renders"])
        self.assertTrue(list((self.root / "logs").glob("run_1-verify1.log")))
        self.assertIn("FAIL Hero renders", (self.root / "logs" / "run_1-verify1.log").read_text(encoding="utf-8"))

    def test_verify_runs_inside_the_queue(self) -> None:
        lock = compat.state_dir(self.env) / "verify.lock"
        seen: list[bool] = []

        def runner(*args: object, **kwargs: object) -> object:
            if kwargs.get("shell"):
                seen.append(lock.exists())
                return subprocess.CompletedProcess(args, 0, "ok", "")
            return subprocess.run(*args, **kwargs)  # type: ignore[call-overload]

        report = self.report(self.record(["echo ok"]), run=runner)
        self.assertEqual(seen, [True])
        self.assertFalse(lock.exists())
        self.assertTrue(report["queue"]["held"])

    def test_queue_wait_note_in_render(self) -> None:
        report = self.report(self.record([]))
        report["queue"] = {"waited": 42.0, "held": True}
        self.assertIn("note: waited 42s for another gate's VERIFY, then ran", gate.render(report))

    def test_verify_timeout_is_a_failure(self) -> None:
        report = self.report(self.record([f"{PY} -c \"import time; time.sleep(30)\""]), timeout=1)
        verify = self.by_name(report, "verify")[0]
        self.assertEqual(verify["status"], "FAIL")
        self.assertIn("timed out after 1s", verify["detail"])

    def test_no_verify_flag_and_missing_commands(self) -> None:
        skipped = self.report(self.record([f"{PY} -c \"raise SystemExit(1)\""]), verify=False)
        self.assertEqual(self.by_name(skipped, "verify")[0]["status"], "SKIP")
        self.assertEqual(skipped["mechanical"], "PASS")
        none = self.report(self.record([]))
        self.assertEqual(self.by_name(none, "verify")[0]["status"], "SKIP")

    def test_weakened_tests_warn_but_do_not_fail(self) -> None:
        self.write("src/hero/Hero.test.ts", "it.skip('renders', () => {})\n")
        report = self.report(self.record([]))
        tests = self.by_name(report, "tests")[0]
        self.assertEqual(tests["status"], "WARN")
        self.assertIn("src/hero/Hero.test.ts: 2 test/assert line(s) removed", tests["lines"])
        self.assertIn("src/hero/Hero.test.ts: 1 skip/only marker(s) added", tests["lines"])
        self.assertEqual(report["mechanical"], "PASS")

    def test_deleted_test_file_warns(self) -> None:
        (self.repo / "src/hero/Hero.test.ts").unlink()
        tests = self.by_name(self.report(self.record([])), "tests")[0]
        self.assertEqual(tests["status"], "WARN")
        self.assertIn("deleted test file src/hero/Hero.test.ts", tests["lines"])

    def test_files_dirty_before_the_worker_are_left_out(self) -> None:
        self.write("README.md", "mine\n")
        self.write("src/hero/Hero.ts", "export const hero = 3\n")
        report = self.report(self.record([], dirty=["README.md"]))
        self.assertEqual(report["changed"], ["src/hero/Hero.ts"])
        self.assertEqual(report["dirty_excluded"], 1)
        self.assertEqual(report["mechanical"], "PASS")

    def test_no_machine_checkable_scope_is_skipped_not_passed(self) -> None:
        self.write("anything.txt", "x\n")
        report = self.report(self.record([], scope=[], do_not=[]))
        self.assertEqual(self.by_name(report, "scope")[0]["status"], "SKIP")
        self.assertEqual(self.by_name(report, "do-not")[0]["status"], "SKIP")

    def test_round_budget(self) -> None:
        self.write("src/other/X.ts", "x\n")
        (self.root / ".zcode-commander").mkdir()
        (self.root / ".zcode-commander" / "config.json").write_text('{"maxCorrectionRounds": 2}', encoding="utf-8")
        second = self.report(self.record([], rounds=2))
        self.assertEqual((second["round"], second["budget"]), (1, 2))
        self.assertIn("correction round 2 of 2", second["draft"])
        exhausted = self.report(self.record([], rounds=3))
        self.assertTrue(exhausted["budget_exhausted"])
        self.assertNotIn("draft", exhausted)

    def test_self_check_drafts_nothing(self) -> None:
        self.write("src/other/X.ts", "x\n")
        report = self.report(self.record([]), self_check=True)
        self.assertEqual(report["mechanical"], "FAIL")
        self.assertNotIn("draft", report)

    def test_record_without_base_is_an_error(self) -> None:
        data = self.record([])
        data["base"] = None
        with self.assertRaises(gate.GateError):
            self.report(data)

    def test_render_mentions_every_part(self) -> None:
        self.write("src/other/X.ts", "x\n")
        text = gate.render(self.report(self.record([])))
        self.assertIn("GATE  run_1", text)
        self.assertIn("[FAIL] scope", text)
        self.assertIn("JUDGMENT", text)
        self.assertIn("MECHANICAL: FAIL  -> do not accept", text)
        self.assertIn("draft correction", text)


class VerifyQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self._tmp.name) / "q" / "verify.lock"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_takes_and_releases_the_lock(self) -> None:
        with gate.verify_queue(self.lock, 60) as q:
            self.assertTrue(self.lock.exists())
            self.assertEqual(q, {"waited": 0.0, "held": True})
        self.assertFalse(self.lock.exists())

    def test_waits_for_a_holder_then_gives_up_and_runs(self) -> None:
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text("123 0", encoding="utf-8")
        now = [0.0]
        with gate.verify_queue(self.lock, 5, sleep=lambda s: now.__setitem__(0, now[0] + s),
                               clock=lambda: now[0]) as q:
            self.assertEqual(q["held"], False)
            self.assertGreaterEqual(q["waited"], 5)
        self.assertTrue(self.lock.exists())  # someone else's lock is left alone

    def test_stale_lock_is_taken_over(self) -> None:
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text("123 0", encoding="utf-8")
        old = time.time() - gate.QUEUE_STALE_SECONDS - 60
        os.utime(self.lock, (old, old))
        with gate.verify_queue(self.lock, 5) as q:
            self.assertTrue(q["held"])

    def test_zero_wait_skips_the_queue(self) -> None:
        with gate.verify_queue(self.lock, 0) as q:
            self.assertEqual(q["held"], False)
        self.assertFalse(self.lock.exists())


class ResolveRecordTests(RepoFixture):
    def test_by_run_thread_prefix_and_cwd(self) -> None:
        self.record([], run_id="run_a", thread="sess_aaaa1111")
        self.record([], run_id="run_b", thread="sess_bbbb2222")
        for ident, expected in (("run_a", "run_a"), ("sess_bbbb2222", "run_b"), ("sess_aaaa", "run_a"), ("bbbb2", "run_b")):
            _path, data = gate.resolve_record(self.env, ident, str(self.repo))
            self.assertEqual(data["runIds"], [expected], ident)
        _path, data = gate.resolve_record(self.env, None, str(self.repo))  # newest for this folder
        self.assertEqual(data["runIds"], ["run_b"])

    def test_errors(self) -> None:
        with self.assertRaises(gate.GateError):
            gate.resolve_record(self.env, None, str(self.repo))  # no records at all
        self.record([], run_id="run_a", thread="sess_aaaa1111")
        self.record([], run_id="run_b", thread="sess_aaaa2222")
        with self.assertRaises(gate.GateError):
            gate.resolve_record(self.env, "nothing", str(self.repo))
        with self.assertRaises(gate.GateError):
            gate.resolve_record(self.env, "sess_aaaa", str(self.repo))  # ambiguous
        with self.assertRaises(gate.GateError):
            gate.resolve_record(self.env, None, str(self.root))  # no record for that folder


class RunCommandTests(RepoFixture):
    def args(self, **over: object) -> SimpleNamespace:
        base = dict(ident="run_1", self_check=False, no_verify=True, no_queue=False, verify_timeout=60,
                    accept=None, reject=None, override=None, json=False)
        base.update(over)
        return SimpleNamespace(**base)

    def call(self, **over: object) -> tuple[int, str]:
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            code = gate.run(self.args(**over), env=self.env, out=out)
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def saved(self) -> dict:
        return json.loads((compat.state_dir(self.env) / "run_1.json").read_text(encoding="utf-8"))

    def test_pass_then_accept_records_a_verdict(self) -> None:
        self.record([])
        self.write("src/hero/Hero.ts", "export const hero = 2\n")
        code, text = self.call(accept="title checked")
        self.assertEqual(code, 0)
        self.assertIn("recorded: accept", text)
        [verdict] = self.saved()["verdicts"]
        self.assertEqual((verdict["verdict"], verdict["note"], verdict["mechanical"], verdict["round"]),
                         ("accept", "title checked", "PASS", 0))

    def test_accepting_a_failed_check_needs_an_override(self) -> None:
        self.record([])
        self.write("src/other/X.ts", "x\n")
        code, _ = self.call(accept="")
        self.assertEqual(code, 2)
        self.assertEqual(self.saved()["verdicts"], [])
        code, _ = self.call(accept="ok", override="spec allows it")
        self.assertEqual(code, 1)  # the check itself still failed
        self.assertEqual(self.saved()["verdicts"][0]["override"], "spec allows it")

    def test_reject_records_and_exit_code_follows_the_check(self) -> None:
        self.record([])
        self.write("src/other/X.ts", "x\n")
        code, text = self.call(reject="out of scope")
        self.assertEqual(code, 1)
        self.assertIn("recorded: reject", text)
        self.assertEqual(self.saved()["verdicts"][0]["verdict"], "reject")

    def test_self_check_and_plain_run_record_nothing(self) -> None:
        self.record([])
        self.call(self_check=True)
        self.call()
        self.assertEqual(self.saved()["verdicts"], [])

    def test_json_output(self) -> None:
        self.record([])
        code, text = self.call(json=True)
        data = json.loads(text)
        self.assertEqual((code, data["mechanical"], data["run"]), (0, "PASS", "run_1"))

    def test_unknown_run_exit_code_two(self) -> None:
        self.record([])
        code, _ = self.call(ident="run_zzz")
        self.assertEqual(code, 2)


class WiringTests(unittest.TestCase):
    def test_parser_accepts_gate(self) -> None:
        zcrew = _load("zcrew")
        parser = zcrew.build_parser()
        args = parser.parse_args(["gate", "sess_ab", "--self", "--no-verify", "--accept", "fine"])
        self.assertEqual((args.command, args.ident, args.self_check, args.no_verify, args.accept),
                         ("gate", "sess_ab", True, True, "fine"))
        self.assertTrue(parser.parse_args(["gate", "--no-queue"]).no_queue)
        bare = parser.parse_args(["gate", "--reject"])
        self.assertEqual((bare.ident, bare.reject, bare.accept), (None, "", None))
        self.assertEqual(zcrew._load_sibling("gate").__name__ != "", True)


if __name__ == "__main__":
    unittest.main()


class ConsoleEncodingTests(RepoFixture):
    def test_judgment_with_non_ascii_text_survives_a_narrow_console_code_page(self) -> None:
        # issue #2: printing "≥" to a cp1252 pipe raised UnicodeEncodeError and the gate never answered
        data = self.record([])
        data["contract"]["sections"]["ACCEPTANCE CRITERIA"] = ["Coverage ≥ 80%"]
        compat.save_record(compat.state_dir(self.env) / "run_1.json", data)
        env = {**os.environ, **self.env, "PYTHONIOENCODING": "cp1252"}
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "zcrew.py"), "gate", "run_1", "--no-verify"],
            cwd=self.repo, env=env, capture_output=True, check=False,
        )
        self.assertNotIn(b"UnicodeEncodeError", done.stderr)
        self.assertIn(done.returncode, (0, 1))
        self.assertIn("Coverage ≥ 80%", done.stdout.decode("utf-8"))
