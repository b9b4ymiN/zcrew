"""Offline contract tests: subprocesses are injected and state is temporary."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


cx = _load("cx")

SESSION = "12345678-1234-1234-1234-123456789abc"
NODE = r"C:\Program Files\nodejs\node.exe"
KEYS = {"runIds", "threadId", "cwd", "startedAt", "roundStartedAt", "rounds",
        "base", "dirty", "contract", "verdicts", "mode", "codex", "worker"}
JSON_RUN_KEYS = {"runId", "worker", "threadId", "jobId", "cwd", "title", "status",
                 "startedAt", "roundStartedAt", "endedAt", "round", "report",
                 "verdict", "warning"}
CONTRACT_TEXT = "OBJECTIVE\nทดสอบ\nSCOPE\n`tools/cx.py`"
DIRTY_CONTRACT = "OBJECTIVE\nx\nSCOPE\n`existing.txt`"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


class CxFixture(unittest.TestCase):
    """A temp repo plus the real bridge_compat/gate siblings and an injected runner."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cx-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        os.environ.update(GIT_ENV)
        self.git("init", "-q")
        self.prompt = self.repo / "contract.md"
        self.prompt.write_text(CONTRACT_TEXT, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "existing.txt").write_text("uncommitted\n", encoding="utf-8")
        self.runtime = (self.home / ".claude/plugins/cache/openai-codex/codex/1.10.0"
                        / "scripts/codex-companion.mjs")
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_text("", encoding="utf-8")
        # CLAUDE_PLUGIN_DATA / CODEX_COMPANION_SESSION_ID are set by a Claude Code
        # host; the companion must never see them (shared job store).
        self.env = {
            "ZCODE_COMMANDER_STATE": str(self.root / "state"),
            "USERPROFILE": str(self.home),
            "CLAUDE_PLUGIN_DATA": str(self.root / "plugin-data"),
            "CODEX_COMPANION_SESSION_ID": "host-session",
        }
        self.calls: list[tuple[list[str], dict]] = []

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                              text=True, check=True, env={**os.environ, **GIT_ENV}).stdout

    def track(self, command: list[str], kwargs: dict) -> None:
        self.calls.append((command, kwargs))
        self.assertNotIn("CLAUDE_PLUGIN_DATA", kwargs["env"])
        self.assertNotIn("CODEX_COMPANION_SESSION_ID", kwargs["env"])
        if "input" in kwargs:
            self.assertNotIn("stdin", kwargs)
        else:
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def runner(self, command: list[str], **kwargs):
        self.track(command, kwargs)
        if "task-resume-candidate" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(
                {"available": True, "candidate": {"threadId": SESSION}}), "")
        if "--background" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"jobId": "task-default"}), "")
        return subprocess.CompletedProcess(
            command, 0, f"[codex] Thread ready ({SESSION}).\nanswer ไทย\n",
            "(node:42) [DEP0190] DeprecationWarning: harmless\nreal warning\n")

    def invoke(self, args: list[str], **kw) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cx.main(args, runner=kw.pop("runner", self.runner),
                           which=kw.pop("which", lambda name, **opts: {"node": NODE}.get(name)),
                           env=self.env, home=self.home, repo=self.repo, **kw)
        return code, out.getvalue(), err.getvalue()

    def records(self) -> list[tuple[Path, dict]]:
        return [(p, json.loads(p.read_text(encoding="utf-8")))
                for p in (Path(self.env["ZCODE_COMMANDER_STATE"]) / "gate").glob("*.json")]


class DiscoveryTests(unittest.TestCase):
    def test_discovery_numeric_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runtime = (home / ".claude/plugins/cache/openai-codex/codex/1.10.0"
                       / "scripts/codex-companion.mjs")
            runtime.parent.mkdir(parents=True)
            runtime.write_text("", encoding="utf-8")
            older = runtime.parents[2] / "1.9.0/scripts/codex-companion.mjs"
            older.parent.mkdir(parents=True)
            older.write_text("", encoding="utf-8")
            self.assertEqual(cx.discover_plugin(home), runtime)


class RunFixAuditTests(CxFixture):
    def test_run_records_before_dispatch_and_captures_thread(self) -> None:
        prompt_text = self.prompt.read_text(encoding="utf-8")

        def runner(command, **kwargs):
            [(path, data)] = self.records()
            self.assertEqual(set(data), KEYS)
            self.assertIsNone(data["threadId"])
            self.assertIsNone(data["codex"]["sessionId"])
            self.assertEqual(data["worker"], "codex")
            self.assertEqual(data["cwd"], str(self.repo.resolve()))
            self.assertEqual(data["base"], self.base)
            self.assertEqual(data["dirty"], ["existing.txt"])
            self.assertEqual(data["rounds"], 1)
            self.assertEqual(data["verdicts"], [])
            self.assertEqual(data["mode"], "warn")
            self.assertEqual(data["startedAt"], data["roundStartedAt"])
            self.assertEqual(command[2:6], ["task", "--effort", "high", "--write"])
            self.assertEqual(command[0], NODE)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("input", kwargs)
            self.assertEqual(command[-1], prompt_text)
            self.assertEqual(kwargs["cwd"], str(self.repo.resolve()))
            return subprocess.CompletedProcess(
                command, 7, f"[codex] Thread ready ({SESSION}).\nไทย\n[DEP0190] noise\n",
                "[DEP0190] noise\nuseful error\n")

        code, out, err = self.invoke(["run", str(self.prompt), "--write"], runner=runner)
        self.assertEqual(code, 7)
        [(path, data)] = self.records()
        self.assertEqual(data["threadId"], SESSION)
        self.assertEqual(data["codex"]["sessionId"], SESSION)
        self.assertEqual(set(data), KEYS)
        report = Path(data["codex"]["reportPaths"][0])
        self.assertTrue(report.read_text(encoding="utf-8").endswith("ไทย\nuseful error\n"))
        self.assertEqual(data["codex"]["contractText"], prompt_text)
        self.assertEqual(data["codex"]["lastExit"], 7)
        self.assertNotIn("DEP0190", out + err)
        self.assertIn("useful error", err)
        self.assertNotIn("next:", out)
        self.assertIn(f"cx: worker failed (exit 7); see {report}", err)

    def test_fix_latest_round_and_prefix(self) -> None:
        for write in (False, True):
            with self.subTest(write=write):
                self.setUp()
                self.assertEqual(self.invoke(["run", str(self.prompt)] + (["--write"] if write else []))[0], 0)
                [(path, before)] = self.records()
                with mock.patch.object(cx.time, "time", lambda: before["startedAt"] / 1000 + 10):
                    code, out, err = self.invoke(
                        ["fix", before["runIds"][0][:-1], str(self.prompt), "--effort", "medium"])
                self.assertEqual(code, 0)
                after = json.loads(path.read_text(encoding="utf-8"))
                cmd, opts = self.calls[-1]
                self.assertEqual(cmd[0], NODE)
                self.assertIn("--resume-last", cmd)
                self.assertEqual(("--write" in cmd), write)
                self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
                self.assertEqual(after["rounds"], 2)
                self.assertEqual(after["runIds"], [before["runIds"][0], before["runIds"][0] + ".r2"])
                self.assertGreater(after["roundStartedAt"], before["roundStartedAt"])
                self.assertEqual(after["startedAt"], before["startedAt"])
                self.assertEqual(after["base"], before["base"])
                self.assertEqual(after["contract"], before["contract"])
                self.assertEqual(Path(after["codex"]["reportPaths"][-1]).name,
                                 before["runIds"][0] + ".r2.report.md")
                self.assertIn("resume-last", out)

    def test_fix_fresh_thread_prompt_and_metadata(self) -> None:
        for write in (False, True):
            for previous, label in (("short ไทย", "short"), ("discard-me" + "ก" * 8000, "trimmed")):
                with self.subTest(write=write, previous=label):
                    self.setUp()
                    original = self.prompt.read_text(encoding="utf-8")
                    self.invoke(["run", str(self.prompt)] + (["--write"] if write else []))
                    [(path, before)] = self.records()
                    last_report = self.repo / "latest.md"
                    last_report.write_text(previous, encoding="utf-8")
                    before["codex"]["reportPaths"].append(str(last_report))
                    path.write_text(json.dumps(before), encoding="utf-8")
                    correction = 'Correct "quotes" %PATH% ^ & | < >\nsecond line ไทย\n'
                    self.prompt.write_text(correction, encoding="utf-8")
                    new_session = "aaaaaaaa-1234-1234-1234-123456789abc"

                    def runner(cmd, **kwargs):
                        self.track(cmd, kwargs)
                        if "task-resume-candidate" in cmd:
                            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                                {"available": True, "candidate": {"threadId": "audit-thread"}}), "")
                        self.assertEqual(cmd[0], NODE)
                        self.assertEqual(cmd[2], "task")
                        self.assertNotIn("--resume-last", cmd)
                        self.assertEqual(("--write" in cmd), write)
                        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
                        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                        self.assertNotIn("input", kwargs)
                        self.assertEqual(cmd[-1], ("Correction round 2 on a new thread.\n\n" + correction
                                                   + "\n\nPREVIOUS REPORT\n" + previous[-8000:]
                                                   + "\n\nORIGINAL CONTRACT\n" + original))
                        return subprocess.CompletedProcess(
                            cmd, 0, f"[codex] Thread ready ({new_session}).\ncorrected", "")

                    code, out, err = self.invoke(
                        ["fix", before["runIds"][0], str(self.prompt), "--effort", "medium"],
                        runner=runner)
                    self.assertEqual(code, 0)
                    after = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(after["rounds"], 2)
                    self.assertEqual(after["runIds"], [before["runIds"][0], before["runIds"][0] + ".r2"])
                    self.assertEqual(after["threadId"], new_session)
                    self.assertEqual(after["codex"]["sessionId"], new_session)
                    self.assertEqual(after["codex"]["contractText"], original)
                    self.assertEqual(after["codex"]["previousSessions"], [SESSION])
                    self.assertTrue(
                        Path(after["codex"]["reportPaths"][-1]).read_text(encoding="utf-8").endswith("corrected"))
                    self.assertIn("correction path: fresh thread (contract + last report + correction)", out)
                    self.assertIn("next: zcrew cx gate", out)

    def test_audit_readonly_and_custom_report(self) -> None:
        code, out, err = self.invoke(["audit", str(self.prompt), "--report", "reports/audit.md"])
        self.assertEqual(code, 0)
        cmd, _ = self.calls[-1]
        self.assertNotIn("--write", cmd)
        self.assertNotIn("--resume-last", cmd)
        self.assertEqual(self.records(), [])
        self.assertIn("answer ไทย", (self.repo / "reports/audit.md").read_text(encoding="utf-8"))

    def test_missing_sections_warn_and_default_audit_report(self) -> None:
        self.prompt.write_text("OBJECTIVE\nonly", encoding="utf-8")
        code, out, err = self.invoke(["run", str(self.prompt)])
        self.assertEqual(code, 0)
        self.assertIn("SCOPE", err)
        code, out, err = self.invoke(["audit", str(self.prompt)])
        self.assertEqual(code, 0)
        self.assertTrue(list((self.repo / ".cx").glob("audit_*.md")))

    def test_real_gate_resolves_record(self) -> None:
        self.assertEqual(self.invoke(["run", str(self.prompt)])[0], 0)
        [(path, data)] = self.records()
        gate = cx.load_sibling("gate")
        resolved, record = gate.resolve_record(self.env, data["runIds"][0], str(self.repo))
        self.assertEqual(resolved, path)
        self.assertEqual(record, data)

    def test_failed_launch_leaves_reviewable_baseline(self) -> None:
        def unavailable(*args, **kwargs):
            self.assertTrue(self.records())
            raise FileNotFoundError("node executable not found")

        code, out, err = self.invoke(["run", str(self.prompt)], runner=unavailable)
        self.assertEqual(code, 2)
        [(path, record)] = self.records()
        self.assertIsNone(record["threadId"])
        self.assertEqual(record["rounds"], 1)
        self.assertIn("node executable not found", err)

    def test_failed_task_without_thread_preserves_exit_and_report(self) -> None:
        def failed(cmd, **kwargs):
            self.track(cmd, kwargs)
            return subprocess.CompletedProcess(cmd, 9, "partial output", "failed")

        code, out, err = self.invoke(["run", str(self.prompt), "--report", "custom.md"], runner=failed)
        self.assertEqual(code, 9)
        [(path, record)] = self.records()
        self.assertIsNone(record["threadId"])
        self.assertFalse(record["codex"]["write"])
        self.assertEqual((self.repo / "custom.md").read_text(encoding="utf-8"), "partial output\nfailed")
        self.assertEqual(record["codex"]["lastExit"], 9)
        self.assertIn("worker failed (exit 9)", err)
        self.assertNotIn("next:", out)

    def test_other_repo_record_does_not_change_latest_thread(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, record)] = self.records()
        other = dict(record, runIds=["codex_other_repo"], threadId="other-session",
                     cwd=str(self.repo.parent / "other"), startedAt=record["startedAt"] + 100)
        (path.parent / "other.json").write_text(json.dumps(other), encoding="utf-8")
        self.assertEqual(self.invoke(["fix", record["runIds"][0], str(self.prompt)])[0], 0)
        self.assertIn("--resume-last", self.calls[-1][0])

    def test_plugin_candidate_wins_over_record_creation_order(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, record)] = self.records()
        # Both launches can happen in one millisecond. UUID sort is not time order.
        later = path.parent / "aaa-newer.json"
        newer_meta = dict(record["codex"], startedAtNs=record["codex"]["startedAtNs"] + 1)
        later.write_text(json.dumps(dict(record, codex=newer_meta, runIds=["codex_later"],
                                         threadId="later")), encoding="utf-8")
        self.assertEqual(self.invoke(["fix", record["runIds"][0], str(self.prompt)])[0], 0)
        self.assertIn("--resume-last", self.calls[-1][0])

    def test_report_cannot_overwrite_prompt(self) -> None:
        original = self.prompt.read_text(encoding="utf-8")
        code, out, err = self.invoke(["run", str(self.prompt), "--report", str(self.prompt)])
        self.assertEqual(code, 2)
        self.assertEqual(self.prompt.read_text(encoding="utf-8"), original)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records(), [])

    def test_plugin_thread_progress_on_stderr(self) -> None:
        def realistic_plugin(cmd, **kwargs):
            self.track(cmd, kwargs)
            return subprocess.CompletedProcess(
                cmd, 0, "final answer ไทย", f"[DEP0190] noise\n[codex] Thread ready ({SESSION}).\n")

        self.assertEqual(self.invoke(["run", str(self.prompt)], runner=realistic_plugin)[0], 0)
        [(path, record)] = self.records()
        self.assertEqual(record["threadId"], SESSION)
        self.assertEqual(record["codex"]["sessionId"], SESSION)
        self.assertEqual(Path(record["codex"]["reportPaths"][0]).read_text(encoding="utf-8"),
                         "final answer ไทย")

    def test_fix_preflight_prevents_resuming_audit_or_unknown_thread(self) -> None:
        for candidate in ("audit-thread", None, "malformed"):
            with self.subTest(candidate=candidate):
                self.setUp()
                self.invoke(["run", str(self.prompt), "--write"])
                [(path, record)] = self.records()
                self.invoke(["audit", str(self.prompt)])

                def runner(cmd, **kwargs):
                    self.track(cmd, kwargs)
                    if "task-resume-candidate" in cmd:
                        stdout = ("not json" if candidate == "malformed" else json.dumps({
                            "available": candidate is not None,
                            "candidate": {"threadId": candidate} if candidate else None}))
                        return subprocess.CompletedProcess(cmd, 0, stdout, "")
                    self.assertEqual(cmd[0], NODE)
                    self.assertEqual(cmd[2], "task")
                    self.assertNotIn("--resume-last", cmd)
                    self.assertIn("--write", cmd)
                    self.assertIn("ORIGINAL CONTRACT", cmd[-1])
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    return subprocess.CompletedProcess(cmd, 0, f"[codex] Thread ready ({SESSION}).", "")

                self.assertEqual(self.invoke(["fix", record["runIds"][0], str(self.prompt)],
                                             runner=runner)[0], 0)
                self.assertIn("task-resume-candidate", self.calls[-2][0])
                self.assertNotIn("--resume-last", self.calls[-1][0])

    def test_fix_missing_node_exits_two_without_dispatch_or_round_update(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, record)] = self.records()
        newer = dict(record, runIds=["codex_newer"], startedAt=record["startedAt"] + 1)
        (path.parent / "newer.json").write_text(json.dumps(newer), encoding="utf-8")
        count = len(self.calls)
        code, out, err = self.invoke(["fix", record["runIds"][0], str(self.prompt)],
                                     which=lambda name, **opts: None)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), count)
        self.assertIn("executable not found: node", err)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)

    def test_default_lookup_uses_shutil_which_with_child_path(self) -> None:
        self.env["PATH"] = "test-child-search-path"
        lookups = []

        def lookup(name, *, path):
            lookups.append((name, path))
            return NODE

        def stat_runner(cmd, **kwargs):
            self.track(cmd, kwargs)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"job": {"status": "running", "threadId": None}}), "")

        self.invoke(["run", str(self.prompt), "--background"])
        with mock.patch.object(cx.shutil, "which", lookup):
            code, out, err = self.invoke(["status", "--json"], runner=stat_runner, which=None)
        self.assertEqual(code, 0)
        self.assertEqual(lookups, [("node", self.env["PATH"])])

    def test_fix_failure_preserves_record_and_saves_error(self) -> None:
        for fresh in (False, True):
            for stdout in ("", "partial ไทย"):
                with self.subTest(fresh=fresh, stdout=stdout):
                    self.setUp()
                    self.invoke(["run", str(self.prompt), "--write"])
                    [(path, before)] = self.records()
                    original_bytes = path.read_bytes()

                    def failed(cmd, **kwargs):
                        self.track(cmd, kwargs)
                        if "task-resume-candidate" in cmd:
                            return subprocess.CompletedProcess(cmd, 0, json.dumps({
                                "available": True,
                                "candidate": {"threadId": "audit" if fresh else SESSION}}), "")
                        self.assertEqual(("--resume-last" not in cmd), fresh)
                        return subprocess.CompletedProcess(cmd, 1, stdout, "[DEP0190] noise\nactive writer\n")

                    code, out, err = self.invoke(["fix", before["runIds"][0], str(self.prompt)],
                                                 runner=failed)
                    self.assertEqual(code, 1)
                    self.assertEqual(path.read_bytes(), original_bytes)
                    report = self.repo / ".cx" / (before["runIds"][0] + ".r2.report.md")
                    self.assertEqual(report.read_text(encoding="utf-8"),
                                     stdout + ("\n" if stdout else "") + "active writer\n")
                    self.assertIn(f"cx: worker failed (exit 1); see {report}", err)
                    self.assertNotIn("next:", out)
                    self.assertNotIn("DEP0190", out + err)

    def test_run_refuses_when_scope_file_already_dirty(self) -> None:
        self.prompt.write_text(DIRTY_CONTRACT, encoding="utf-8")
        code, out, err = self.invoke(["run", str(self.prompt), "--write"])
        self.assertEqual(code, 2)
        self.assertIn("existing.txt", err)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records(), [])

    def test_run_allow_dirty_overrides_guard(self) -> None:
        self.prompt.write_text(DIRTY_CONTRACT, encoding="utf-8")
        self.assertEqual(self.invoke(["run", str(self.prompt), "--write", "--allow-dirty"])[0], 0)
        self.assertEqual(len(self.records()), 1)


class ErrorExitTests(CxFixture):
    def test_clear_error_exits(self) -> None:
        for case in ("plugin", "file", "record", "ambiguous", "session"):
            with self.subTest(case=case):
                self.setUp()
                args = ["run", str(self.prompt)]
                if case == "plugin":
                    self.runtime.unlink()
                elif case == "file":
                    args = ["audit", str(self.prompt) + ".missing"]
                else:
                    args = ["fix", "codex_", str(self.prompt)]
                    if case != "record":
                        self.invoke(["run", str(self.prompt)])
                        [(path, data)] = self.records()
                        if case == "ambiguous":
                            data["runIds"] = ["codex_another"]
                            (path.parent / "another.json").write_text(json.dumps(data), encoding="utf-8")
                        else:
                            data["threadId"] = data["codex"]["sessionId"] = None
                            path.write_text(json.dumps(data), encoding="utf-8")
                count = len(self.calls)
                code, out, err = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(len(self.calls), count)
                self.assertIn("cx:", err)

    def test_invalid_prompt_never_starts_task(self) -> None:
        for text in ("", "--help"):
            with self.subTest(text=text):
                self.prompt.write_text(text, encoding="utf-8")
                self.assertEqual(self.invoke(["audit", str(self.prompt)])[0], 2)
                self.assertEqual(self.calls, [])

    def test_missing_node_exits_two_and_names_executable(self) -> None:
        for command in ("run", "audit"):
            with self.subTest(command=command):
                args = [command, str(self.prompt)]
                code, out, err = self.invoke(args, which=lambda name, **opts: None)
                self.assertEqual(code, 2)
                self.assertEqual(self.calls, [])
                self.assertIn("node", err)


class BackgroundTests(CxFixture):
    def run_background(self) -> tuple[Path, dict]:
        def runner(command, **kwargs):
            self.track(command, kwargs)
            self.assertIn("--background", command)
            self.assertIn("--json", command)
            return subprocess.CompletedProcess(command, 0, json.dumps({"jobId": "task-42"}), "")

        code, out, err = self.invoke(["run", str(self.prompt), "--write", "--background"], runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("run id:", out)
        self.assertIn("job id: task-42", out)
        self.assertIn("report:", out)
        self.assertIn("next: zcrew cx status", out)
        [(path, record)] = self.records()
        return path, record

    def test_background_run_writes_record_and_returns_at_once(self) -> None:
        path, record = self.run_background()
        self.assertEqual(record["worker"], "codex")
        self.assertEqual(record["codex"]["jobId"], "task-42")
        self.assertTrue(record["codex"]["background"])
        self.assertIsNone(record["codex"]["endedAt"])
        self.assertIsNone(record["threadId"])
        self.assertIsNone(record["codex"]["sessionId"])
        self.assertEqual(record["base"], self.base)
        self.assertEqual(record["dirty"], ["existing.txt"])
        report = Path(record["codex"]["reportPaths"][0])
        self.assertFalse(report.exists())  # written later by status reconciliation
        cmd = self.calls[-1][0]
        self.assertEqual(cmd[0], NODE)
        self.assertEqual(cmd[2], "task")
        self.assertIn("--write", cmd)

    def test_background_env_has_no_plugin_data_or_session_pin(self) -> None:
        self.run_background()
        self.assertTrue(self.calls)
        for cmd, kwargs in self.calls:
            self.assertNotIn("CLAUDE_PLUGIN_DATA", kwargs["env"])
            self.assertNotIn("CODEX_COMPANION_SESSION_ID", kwargs["env"])

    def test_background_run_failed_enqueue_stamps_baseline(self) -> None:
        def runner(command, **kwargs):
            self.track(command, kwargs)
            return subprocess.CompletedProcess(command, 3, "enqueue exploded", "boom")

        code, out, err = self.invoke(["run", str(self.prompt), "--background"], runner=runner)
        self.assertEqual(code, 3)
        [(path, record)] = self.records()
        self.assertIsNone(record["codex"]["jobId"])
        self.assertEqual(record["codex"]["lastExit"], 3)
        self.assertIsNotNone(record["codex"]["endedAt"])
        report = Path(record["codex"]["reportPaths"][0])
        self.assertEqual(report.read_text(encoding="utf-8"), "enqueue exploded\nboom")
        self.assertIn("background dispatch failed", err)

    def test_fix_background_enqueues_correction(self) -> None:
        self.invoke(["run", str(self.prompt), "--write"])
        [(path, before)] = self.records()

        def runner(command, **kwargs):
            self.track(command, kwargs)
            if "task-resume-candidate" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"available": True, "candidate": {"threadId": SESSION}}), "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"jobId": "task-7"}), "")

        code, out, err = self.invoke(["fix", before["runIds"][0], str(self.prompt), "--background"],
                                     runner=runner)
        self.assertEqual(code, 0)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["rounds"], 2)
        self.assertEqual(after["runIds"], [before["runIds"][0], before["runIds"][0] + ".r2"])
        self.assertEqual(after["codex"]["jobId"], "task-7")
        self.assertTrue(after["codex"]["background"])
        self.assertIsNone(after["codex"]["endedAt"])
        self.assertTrue(after["codex"]["resume"])
        self.assertIn("--resume-last", self.calls[-1][0])
        self.assertIn("job id: task-7", out)
        self.assertIn("next: zcrew cx status", out)

    def test_fix_background_failure_preserves_record(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, before)] = self.records()
        original_bytes = path.read_bytes()

        def runner(command, **kwargs):
            self.track(command, kwargs)
            if "task-resume-candidate" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"available": True, "candidate": {"threadId": SESSION}}), "")
            return subprocess.CompletedProcess(command, 5, "", "queue full")

        code, out, err = self.invoke(["fix", before["runIds"][0], str(self.prompt), "--background"],
                                     runner=runner)
        self.assertEqual(code, 5)
        self.assertEqual(path.read_bytes(), original_bytes)
        report = self.repo / ".cx" / (before["runIds"][0] + ".r2.report.md")
        self.assertEqual(report.read_text(encoding="utf-8"), "queue full")
        self.assertIn("background dispatch failed", err)


class StatusTests(CxFixture):
    def enqueue_background(self, job: str = "task-42") -> tuple[Path, dict]:
        def runner(command, **kwargs):
            self.track(command, kwargs)
            return subprocess.CompletedProcess(command, 0, json.dumps({"jobId": job}), "")

        self.assertEqual(self.invoke(["run", str(self.prompt), "--background"], runner=runner)[0], 0)
        [(path, record)] = self.records()
        return path, record

    def companion(self, job: dict | None, result: str = "final report ไทย\n") -> "callable":
        def runner(command, **kwargs):
            self.track(command, kwargs)
            if command[2] == "status":
                if job is None:
                    return subprocess.CompletedProcess(command, 1, "no such job", "")
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"workspaceRoot": str(self.repo), "job": job}), "")
            if command[2] == "result":
                return subprocess.CompletedProcess(command, 0, result, "")
            raise AssertionError(command)

        return runner

    def status_json(self, *extra: str, runner=None) -> tuple[dict, str, str]:
        code, out, err = self.invoke(["status", "--json", *extra],
                                     runner=runner or self.companion(
                                         {"status": "completed", "threadId": "jobe-thread-1"}))
        self.assertEqual(code, 0)
        return json.loads(out)["runs"], out, err

    def test_status_with_no_records(self) -> None:
        code, out, err = self.invoke(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"runs": []})
        self.assertEqual(self.calls, [])

    def test_status_reconciles_completed_job_once(self) -> None:
        path, record = self.enqueue_background()
        runner = self.companion({"status": "completed", "threadId": "019a-job-thread"})
        runs, out, err = self.status_json(runner=runner)
        [(path2, after)] = self.records()
        self.assertEqual(after["codex"]["jobId"], "task-42")
        self.assertIsNotNone(after["codex"]["endedAt"])
        self.assertEqual(after["codex"]["lastExit"], 0)
        self.assertEqual(after["codex"]["jobStatus"], "completed")
        self.assertEqual(after["threadId"], "019a-job-thread")
        self.assertEqual(after["codex"]["sessionId"], "019a-job-thread")
        self.assertNotIn("previousSessions", after["codex"])
        report = Path(after["codex"]["reportPaths"][-1])
        self.assertEqual(report.read_text(encoding="utf-8"), "final report ไทย\n")
        self.assertEqual([r["runId"] for r in runs], [record["runIds"][0]])
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["report"], str(report))
        self.assertEqual(runs[0]["endedAt"], after["codex"]["endedAt"])
        # second call: the record is settled, no further companion calls
        count = len(self.calls)
        runs2, _, _ = self.status_json(runner=runner)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(runs2[0]["status"], "completed")
        self.assertEqual(runs2[0]["endedAt"], after["codex"]["endedAt"])

    def test_status_reconciles_failed_job(self) -> None:
        self.enqueue_background()
        runs, out, err = self.status_json(runner=self.companion(
            {"status": "failed", "threadId": None}, result="it broke\n"))
        [(path, after)] = self.records()
        self.assertEqual(after["codex"]["lastExit"], 1)
        self.assertEqual(after["codex"]["jobStatus"], "failed")
        self.assertIsNone(after["threadId"])
        self.assertEqual(runs[0]["status"], "failed")
        self.assertTrue(Path(after["codex"]["reportPaths"][-1]).read_text(encoding="utf-8")
                        .endswith("it broke\n"))

    def test_status_cancelled_job_keeps_its_status(self) -> None:
        self.enqueue_background()
        runs, _, _ = self.status_json(runner=self.companion(
            {"status": "cancelled", "threadId": None}, result=""))
        [(path, after)] = self.records()
        self.assertEqual(after["codex"]["jobStatus"], "cancelled")
        self.assertEqual(after["codex"]["lastExit"], 1)
        runs2, _, _ = self.status_json(runner=self.companion(
            {"status": "cancelled", "threadId": None}, result=""))
        self.assertEqual(runs2[0]["status"], "cancelled")

    def test_status_unknown_job_leaves_record_unchanged(self) -> None:
        self.enqueue_background()
        [(path, before)] = self.records()
        original_bytes = path.read_bytes()
        runs, out, err = self.status_json(runner=self.companion(None))
        self.assertEqual(runs[0]["status"], "unknown")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)

    def test_status_running_job_not_settled(self) -> None:
        self.enqueue_background()
        [(path, before)] = self.records()
        runs, _, _ = self.status_json(runner=self.companion({"status": "running", "threadId": None}))
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)

    def test_status_resume_correction_on_new_thread_warns(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, run_record)] = self.records()

        def runner(command, **kwargs):
            self.track(command, kwargs)
            if "task-resume-candidate" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"available": True, "candidate": {"threadId": SESSION}}), "")
            if command[2] == "status":
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"job": {"status": "completed", "threadId": "bounced-thread"}}), "")
            if command[2] == "result":
                return subprocess.CompletedProcess(command, 0, "corrected\n", "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"jobId": "task-9"}), "")

        self.assertEqual(self.invoke(["fix", run_record["runIds"][0], str(self.prompt),
                                      "--background"], runner=runner)[0], 0)
        runs, _, _ = self.status_json(runner=runner)
        [(path, after)] = self.records()
        self.assertEqual(after["codex"]["sessionId"], "bounced-thread")
        self.assertEqual(after["codex"]["previousSessions"], [SESSION])
        self.assertIn("resume-last", after["codex"]["warning"])
        self.assertEqual(runs[0]["warning"], after["codex"]["warning"])
        self.assertEqual(runs[0]["threadId"], "bounced-thread")

    def test_status_json_exact_keys_and_title(self) -> None:
        long_line = "A" * 120
        self.prompt.write_text(long_line + "\nsecond line", encoding="utf-8")
        self.invoke(["run", str(self.prompt)])
        runs, out, err = self.status_json()
        self.assertEqual(set(runs[0]), JSON_RUN_KEYS)
        self.assertEqual(runs[0]["title"], "A" * 80)
        self.assertEqual(runs[0]["worker"], "codex")
        self.assertIsNone(runs[0]["jobId"])
        self.assertIsInstance(runs[0]["endedAt"], int)  # stamped when the foreground round ended
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["round"], 1)
        self.assertIsNone(runs[0]["verdict"])

    def test_status_reports_verdict(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, record)] = self.records()
        record.setdefault("verdicts", []).append({"at": 1, "round": 0, "verdict": "accepted",
                                                  "mechanical": "PASS", "note": ""})
        path.write_text(json.dumps(record), encoding="utf-8")
        runs, _, _ = self.status_json()
        self.assertEqual(runs[0]["verdict"], "accepted")

    def test_status_active_filter(self) -> None:
        self.enqueue_background()  # ended-but-unverdicted must stay visible
        self.status_json(runner=self.companion({"status": "completed", "threadId": None}))
        [(path, active)] = self.records()
        old = dict(active, runIds=["codex_old"], startedAt=active["startedAt"] - 25 * 60 * 60 * 1000)
        (path.parent / "old.json").write_text(json.dumps(old), encoding="utf-8")
        elsewhere = dict(active, runIds=["codex_elsewhere"], cwd=str(self.repo.parent / "other"),
                         startedAt=active["startedAt"] + 1)
        (path.parent / "elsewhere.json").write_text(json.dumps(elsewhere), encoding="utf-8")
        verdicted = dict(active, runIds=["codex_verdicted"],
                         verdicts=[{"verdict": "accepted"}], startedAt=active["startedAt"] + 2)
        (path.parent / "verdicted.json").write_text(json.dumps(verdicted), encoding="utf-8")
        runs, _, _ = self.status_json("--active", runner=self.companion(
            {"status": "completed", "threadId": None}))
        self.assertEqual([r["runId"] for r in runs], [active["runIds"][0]])
        runs_all, _, _ = self.status_json(runner=self.companion(
            {"status": "completed", "threadId": None}))
        self.assertEqual({r["runId"] for r in runs_all},
                         {active["runIds"][0], "codex_old", "codex_verdicted"})

    def test_status_lists_legacy_codex_record_without_worker(self) -> None:
        self.invoke(["run", str(self.prompt)])
        [(path, record)] = self.records()
        record.pop("worker")
        path.write_text(json.dumps(record), encoding="utf-8")
        runs, _, _ = self.status_json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["worker"], "codex")
        self.assertEqual(runs[0]["status"], "completed")

    def test_status_human_lines(self) -> None:
        self.invoke(["run", str(self.prompt)])
        code, out, err = self.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("completed", out)
        self.assertIn("OBJECTIVE", out)

    def test_status_foreground_last_exit_mapping(self) -> None:
        # A crashed launch leaves the record without lastExit/endedAt — the same state
        # as a foreground round still executing — so status reports running, not failed.
        self.invoke(["run", str(self.prompt)], runner=self.unavailable_runner())
        [(path, record)] = self.records()
        for last_exit, expected in ((None, "running"), (0, "completed"), (1, "failed")):
            with self.subTest(lastExit=last_exit):
                if last_exit is not None:
                    record["codex"]["lastExit"] = last_exit
                    record["codex"]["endedAt"] = record["startedAt"] + 1
                    path.write_text(json.dumps(record), encoding="utf-8")
                runs, _, _ = self.status_json()
                self.assertEqual(runs[0]["status"], expected)

    def unavailable_runner(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError("node executable not found")

        return runner


class GateForwardingTests(CxFixture):
    def test_gate_forwards_to_zcrew(self) -> None:
        code, out, err = self.invoke(["gate", "codex_id", "--accept", "looks good", "--no-verify"])
        self.assertEqual(code, 0)
        cmd = self.calls[-1][0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(Path(cmd[1]).name, "zcrew.py")
        self.assertEqual(cmd[2:], ["gate", "codex_id", "--accept", "looks good", "--no-verify"])


class ZcrewWiringTests(unittest.TestCase):
    def run_zcrew(self, *argv: str, env: dict | None = None, cwd: str | None = None):
        return subprocess.run([sys.executable, str(SCRIPTS / "zcrew.py"), *argv],
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, **(env or {})}, cwd=cwd)

    def test_cx_help_and_top_level_help(self) -> None:
        done = self.run_zcrew("cx", "--help")
        self.assertEqual(done.returncode, 0)
        for word in ("run", "fix", "audit", "gate", "status", "--background"):
            self.assertIn(word, done.stdout)
        top = self.run_zcrew("--help")
        self.assertEqual(top.returncode, 0)
        self.assertIn("cx", top.stdout)

    def test_zcrew_dispatches_cx_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_zcrew("cx", "status", "--json",
                                  env={"ZCODE_COMMANDER_STATE": str(Path(tmp) / "state")}, cwd=tmp)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(json.loads(done.stdout), {"runs": []})


class PipeEncodingTests(unittest.TestCase):
    """`zcrew cx status` through a real pipe whose code page cannot encode Thai."""

    THAI = "ทดสอบสัญญาแก้ไข"

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cx-pipe-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        gate = self.root / "state" / "gate"
        gate.mkdir(parents=True)
        now = int(time.time() * 1000)
        record = {
            "runIds": ["codex_20260101000000_aa12"], "threadId": None, "cwd": str(self.repo),
            "startedAt": now, "roundStartedAt": now, "rounds": 1, "verdicts": [],
            "worker": "codex",
            "codex": {"background": False, "contractText": self.THAI + "\nOBJECTIVE",
                      "lastExit": 0, "endedAt": now, "reportPaths": []},
        }
        (gate / (record["runIds"][0] + ".json")).write_text(json.dumps(record), encoding="utf-8")
        self.env = {**os.environ, "ZCODE_COMMANDER_STATE": str(self.root / "state"),
                    "PYTHONIOENCODING": "cp1252"}

    def zcrew_cx_status(self, *extra: str) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run([sys.executable, str(SCRIPTS / "zcrew.py"), "cx", "status",
                               str(self.repo), *extra], capture_output=True, timeout=120,
                              env=self.env)

    def test_status_json_is_ascii_and_keeps_the_thai_title(self) -> None:
        done = self.zcrew_cx_status("--json", "--active")
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout.decode("cp1252")
        self.assertTrue(text.isascii())
        [run] = json.loads(text)["runs"]
        self.assertEqual(run["title"], self.THAI)

    def test_status_human_lines_do_not_crash(self) -> None:
        done = self.zcrew_cx_status("--active")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"completed", done.stdout)


if __name__ == "__main__":
    unittest.main()
