from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


zcrew = _load("zcrew")


class FakeRun:
    """Records argv lists; returns queued (returncode, stdout, stderr) results."""

    def __init__(self, results: list[tuple[int, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.results = list(results or [])

    def __call__(self, argv, **kwargs):  # noqa: ANN001
        self.calls.append([str(a) for a in argv])
        code, out, err = self.results.pop(0) if self.results else (0, "", "")
        return subprocess.CompletedProcess(argv, code, out, err)


def _quiet(fn, *args, **kwargs):  # noqa: ANN001
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_layout(self) -> "zcrew.ZcrewPaths":
        scripts = self.base / "home" / "src" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (self.base / "home" / "src" / "manifest.json").write_text(json.dumps({"version": "9.8.7"}), encoding="utf-8")
        return zcrew.resolve_paths(scripts / "zcrew.py", env={})


class NeedsCliBootstrapTests(TempDir):
    def write(self, content: str) -> Path:
        path = self.base / "config.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.base / "nope.json"))

    def test_invalid_json(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write("{not json")))

    def test_not_an_object(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write("[1, 2]")))

    def test_no_model(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write(json.dumps({"provider": {}}))))

    def test_model_not_object(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write(json.dumps({"model": "x/y"}))))

    def test_blank_main(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write(json.dumps({"model": {"main": "  "}}))))

    def test_non_string_main(self) -> None:
        self.assertTrue(zcrew.needs_cli_bootstrap(self.write(json.dumps({"model": {"main": 5}}))))

    def test_configured(self) -> None:
        path = self.write(json.dumps({"model": {"main": "builtin:zai-coding-plan/GLM-5.3"}}))
        self.assertFalse(zcrew.needs_cli_bootstrap(path))

    def test_bom_is_accepted(self) -> None:
        path = self.base / "bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": {"main": "p/m"}}).encode())
        self.assertFalse(zcrew.needs_cli_bootstrap(path))

    def test_decision(self) -> None:
        home = self.base
        self.assertEqual(zcrew.bootstrap_decision(home), "desktop-missing")
        (home / ".zcode" / "v2").mkdir(parents=True)
        (home / ".zcode" / "v2" / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(zcrew.bootstrap_decision(home), "needed")
        (home / ".zcode" / "cli").mkdir(parents=True)
        (home / ".zcode" / "cli" / "config.json").write_text(json.dumps({"model": {"main": "p/m"}}), encoding="utf-8")
        self.assertEqual(zcrew.bootstrap_decision(home), "not-needed")


class PathStringTests(unittest.TestCase):
    ENTRY = r"C:\Users\me\.zcrew\bin"

    def test_add_to_empty(self) -> None:
        self.assertEqual(zcrew.path_add("", self.ENTRY), (self.ENTRY, True))

    def test_add_appends(self) -> None:
        self.assertEqual(zcrew.path_add(r"C:\a;C:\b", self.ENTRY), (r"C:\a;C:\b;" + self.ENTRY, True))

    def test_add_is_case_insensitive_and_ignores_trailing_backslash(self) -> None:
        value = r"C:\a;c:\users\ME\.zcrew\BIN\;C:\b"
        self.assertEqual(zcrew.path_add(value, self.ENTRY), (value, False))

    def test_add_ignores_quotes_and_forward_slashes(self) -> None:
        value = '"C:/Users/me/.zcrew/bin"'
        self.assertEqual(zcrew.path_add(value, self.ENTRY), (value, False))

    def test_add_drops_empty_segments_only_when_changing(self) -> None:
        self.assertEqual(zcrew.path_add(r"C:\a;;C:\b;", self.ENTRY), (r"C:\a;C:\b;" + self.ENTRY, True))

    def test_add_expands_variables_for_comparison(self) -> None:
        with mock.patch.dict(os.environ, {"ZCREW_TEST_PROFILE": r"C:\Users\me"}):
            value = r"%ZCREW_TEST_PROFILE%\.zcrew\bin"
            self.assertEqual(zcrew.path_add(value, self.ENTRY), (value, False))

    def test_remove_all_duplicates(self) -> None:
        value = r"C:\a;C:\Users\me\.zcrew\bin;C:\b;c:\users\me\.zcrew\bin\ "
        self.assertEqual(zcrew.path_remove(value, self.ENTRY + "\\"), (r"C:\a;C:\b", True))

    def test_remove_absent_leaves_value_untouched(self) -> None:
        value = r"C:\a;;C:\b;"
        self.assertEqual(zcrew.path_remove(value, self.ENTRY), (value, False))

    def test_update_user_path_respects_no_path(self) -> None:
        read = mock.Mock()
        write = mock.Mock()
        message = zcrew.update_user_path(self.ENTRY, True, {"ZCREW_NO_PATH": "1"}, read, write)
        self.assertIn("not modified", message)
        read.assert_not_called()
        write.assert_not_called()

    def test_update_user_path_keeps_registry_type(self) -> None:
        write = mock.Mock()
        zcrew.update_user_path(self.ENTRY, True, {}, lambda: (r"%USERPROFILE%\x", 2), write)
        write.assert_called_once_with(r"%USERPROFILE%\x;" + self.ENTRY, 2)

    def test_update_user_path_no_write_when_unchanged(self) -> None:
        write = mock.Mock()
        message = zcrew.update_user_path(self.ENTRY, False, {}, lambda: (r"C:\a", 1), write)
        write.assert_not_called()
        self.assertIn("no change", message)


class PathsAndVersionTests(TempDir):
    def test_derived_layout(self) -> None:
        paths = self.make_layout()
        self.assertEqual(paths.home, self.base / "home")
        self.assertEqual(paths.src, self.base / "home" / "src")
        self.assertEqual(paths.bin, self.base / "home" / "bin")
        self.assertEqual(paths.home, paths.derived_home)

    def test_env_override(self) -> None:
        other = self.base / "elsewhere"
        paths = zcrew.resolve_paths(self.base / "home" / "src" / "scripts" / "zcrew.py", env={"ZCREW_HOME": str(other)})
        self.assertEqual(paths.home, other)
        self.assertEqual(paths.bin, other / "bin")
        self.assertEqual(paths.src, self.base / "home" / "src")
        self.assertFalse(zcrew.can_self_delete(paths))

    def test_version_from_manifest(self) -> None:
        paths = self.make_layout()
        result, out = _quiet(zcrew.main, ["version"], env={}, run=FakeRun(), paths=paths)
        self.assertEqual(result, 0)
        self.assertEqual(out.strip(), "zcrew 9.8.7")

    def test_repo_version_matches_manifest(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(zcrew.read_version(ROOT), manifest["version"])

    def test_version_unknown(self) -> None:
        self.assertEqual(zcrew.read_version(self.base), "unknown")

    def test_shim_ascii_absolute(self) -> None:
        text = zcrew.shim_text(Path(r"C:\Program Files\x\src\scripts\zcrew.py"))
        self.assertEqual(text, '@echo off\r\npython "C:\\Program Files\\x\\src\\scripts\\zcrew.py" %*\r\n')

    def test_shim_non_ascii_falls_back_to_relative(self) -> None:
        text = zcrew.shim_text(Path("C:\\Users\\\u0e17\u0e14\\.zcrew\\src\\scripts\\zcrew.py"))
        self.assertIn(r'"%~dp0..\src\scripts\zcrew.py"', text)
        text.encode("ascii")

    def test_can_self_delete_requires_shim(self) -> None:
        paths = self.make_layout()
        self.assertFalse(zcrew.can_self_delete(paths))
        zcrew.write_shim(paths)
        self.assertTrue(zcrew.can_self_delete(paths))


class DispatchTests(TempDir):
    def test_help_lists_commands_but_not_internal(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            zcrew.main(["--help"], env={}, run=FakeRun(), paths=self.make_layout())
        self.assertEqual(ctx.exception.code, 0)
        for name in ("enable", "disable", "status", "config", "doctor", "update", "uninstall", "version"):
            self.assertIn(name, buf.getvalue())
        self.assertNotIn("_setup", buf.getvalue())

    def test_command_required(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            zcrew.main([], env={}, run=FakeRun(), paths=self.make_layout())
        self.assertEqual(ctx.exception.code, 2)

    def test_doctor_returns_exit_code(self) -> None:
        paths = self.make_layout()
        run = FakeRun([(1, "", "")])
        self.assertEqual(zcrew.main(["doctor"], env={}, run=run, paths=paths), 1)
        self.assertEqual(run.calls[0][-1], str(paths.scripts / "doctor.py"))

    def test_commander_dispatch_defaults_to_cwd(self) -> None:
        fake = SimpleNamespace(main=mock.Mock(return_value=0))
        with mock.patch.object(zcrew, "_load_sibling", return_value=fake):
            zcrew.main(["enable", "--force", "--with-project-config", "--with-templates"], env={}, run=FakeRun(), paths=self.make_layout())
            zcrew.main(["status", "X"], env={}, run=FakeRun(), paths=self.make_layout())
            zcrew.main(["config", "--project", "P"], env={}, run=FakeRun(), paths=self.make_layout())
            zcrew.main(["config"], env={}, run=FakeRun(), paths=self.make_layout())
        calls = [c.args[0] for c in fake.main.call_args_list]
        self.assertEqual(calls[0], ["enable", os.getcwd(), "--force", "--with-project-config", "--with-templates"])
        self.assertEqual(calls[1], ["status", "X"])
        self.assertEqual(calls[2], ["config", "--project", "P"])
        self.assertEqual(calls[3], ["config"])

    def test_install_and_uninstall_args(self) -> None:
        ps1 = Path("install.ps1")
        self.assertEqual(
            zcrew.install_args(ps1, "needed", {"ZCREW_SKIP_MCP": "1"})[-3:],
            ["-SkipDoctor", "-EnsureZCodeCliConfig", "-SkipMcpRegistration"],
        )
        self.assertEqual(zcrew.install_args(ps1, "not-needed", {})[-1], "-SkipDoctor")
        self.assertEqual(zcrew.install_args(ps1, "desktop-missing", {})[-1], "-SkipDoctor")
        self.assertEqual(zcrew.install_args(ps1, "x", {})[:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(zcrew.uninstall_args(ps1, False, {})[-1], "-RemoveBridge")
        self.assertNotIn("-RemoveBridge", zcrew.uninstall_args(ps1, True, {}))

    def test_setup_runs_installer_shim_path_and_doctor(self) -> None:
        paths = self.make_layout()
        home = self.base / "userhome"
        run = FakeRun([(0, "", ""), (1, "", "")])
        env = {"ZCREW_NO_PATH": "1", "ZCREW_SKIP_MCP": "1"}
        with mock.patch.object(zcrew, "bootstrap_decision", return_value="needed"):
            code, out = _quiet(zcrew.cmd_setup, paths, env, run, home)
        self.assertEqual(code, 0)
        self.assertIn("-EnsureZCodeCliConfig", run.calls[0])
        self.assertIn("-SkipMcpRegistration", run.calls[0])
        self.assertTrue((paths.bin / "zcrew.cmd").is_file())
        self.assertIn("NEEDS ATTENTION", out)
        self.assertIn("ZCREW_NO_PATH=1", out)

    def test_setup_propagates_installer_failure(self) -> None:
        paths = self.make_layout()
        run = FakeRun([(3, "", "")])
        code, _ = _quiet(zcrew.cmd_setup, paths, {"ZCREW_NO_PATH": "1"}, run, self.base)
        self.assertEqual(code, 3)
        self.assertEqual(len(run.calls), 1)
        self.assertFalse((paths.bin / "zcrew.cmd").exists())

    def test_update_refuses_local_changes(self) -> None:
        paths = self.make_layout()
        (paths.src / ".git").mkdir()
        run = FakeRun([(0, " M scripts/x.py\n", "")])
        code, out = _quiet(zcrew.main, ["update"], env={}, run=run, paths=paths)
        self.assertEqual(code, 1)
        self.assertIn("local changes", out)
        self.assertEqual(len(run.calls), 1)

    def test_update_pulls_then_runs_new_setup(self) -> None:
        paths = self.make_layout()
        (paths.src / ".git").mkdir()
        run = FakeRun([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")])
        code, out = _quiet(zcrew.main, ["update"], env={}, run=run, paths=paths)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls[1][-2:], ["fetch", "--quiet"])
        self.assertEqual(run.calls[2][-3:], ["pull", "--ff-only", "--quiet"])
        self.assertEqual(run.calls[3][-2:], [str(paths.scripts / "zcrew.py"), "_setup"])
        self.assertIn("Restart Claude Code", out)

    def test_update_stops_on_pull_failure(self) -> None:
        paths = self.make_layout()
        (paths.src / ".git").mkdir()
        run = FakeRun([(0, "", ""), (0, "", ""), (1, "", "not possible to fast-forward")])
        code, out = _quiet(zcrew.main, ["update"], env={}, run=run, paths=paths)
        self.assertEqual(code, 1)
        self.assertIn("fast-forward", out)
        self.assertEqual(len(run.calls), 3)

    def test_uninstall_aborts_without_confirmation(self) -> None:
        paths = self.make_layout()
        run = FakeRun()
        args = zcrew.build_parser().parse_args(["uninstall"])
        code, out = _quiet(zcrew.cmd_uninstall, paths, args, {}, run, confirm=lambda _: False)
        self.assertEqual(code, 1)
        self.assertEqual(run.calls, [])
        self.assertIn("aborted", out)

    def test_uninstall_keep_config_without_self_delete(self) -> None:
        paths = self.make_layout()
        run = FakeRun()
        args = zcrew.build_parser().parse_args(["uninstall", "--keep-config"])
        with mock.patch.object(zcrew.subprocess, "Popen") as popen:
            code, out = _quiet(zcrew.cmd_uninstall, paths, args, {"ZCREW_NO_PATH": "1"}, run, confirm=lambda _: True)
        self.assertEqual(code, 0)
        self.assertNotIn("-RemoveBridge", run.calls[0])
        popen.assert_not_called()
        self.assertIn("Remove-Item", out)

    def test_uninstall_yes_self_deletes_standard_layout(self) -> None:
        paths = self.make_layout()
        zcrew.write_shim(paths)
        run = FakeRun()
        args = zcrew.build_parser().parse_args(["uninstall", "--yes"])
        with mock.patch.object(zcrew.subprocess, "Popen") as popen:
            code, _ = _quiet(zcrew.cmd_uninstall, paths, args, {"ZCREW_NO_PATH": "1"}, run)
        self.assertEqual(code, 0)
        self.assertIn("-RemoveBridge", run.calls[0])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], zcrew.self_delete_command(paths.home))

    def test_self_delete_command_really_deletes(self) -> None:
        if os.name != "nt":
            self.skipTest("cmd.exe only")
        target = self.base / "del me" / "x"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run(zcrew.self_delete_command(target), check=True, stdout=subprocess.DEVNULL)
        self.assertFalse(target.exists())

    def test_uninstall_failure_keeps_files(self) -> None:
        paths = self.make_layout()
        zcrew.write_shim(paths)
        run = FakeRun([(1, "", "")])
        args = zcrew.build_parser().parse_args(["uninstall", "--yes"])
        with mock.patch.object(zcrew.subprocess, "Popen") as popen:
            code, _ = _quiet(zcrew.cmd_uninstall, paths, args, {"ZCREW_NO_PATH": "1"}, run)
        self.assertEqual(code, 1)
        popen.assert_not_called()


class EnableDisableIntegrationTests(TempDir):
    def test_enable_status_disable_in_git_repo(self) -> None:
        project = self.base / "proj"
        (project / ".git").mkdir(parents=True)
        run = FakeRun()
        code, out = _quiet(zcrew.main, ["enable", str(project)], env={}, run=run)
        self.assertEqual(code, 0, out)
        claude_md = project / "CLAUDE.md"
        self.assertIn("@~/.claude/zcode-commander/COMMANDER.md", claude_md.read_text(encoding="utf-8"))
        code, out = _quiet(zcrew.main, ["disable", str(project)], env={}, run=run)
        self.assertEqual(code, 0, out)
        self.assertFalse(claude_md.exists())
        self.assertFalse((project / ".claude").exists())
        self.assertEqual(run.calls, [])

    def test_enable_with_templates_round_trip(self) -> None:
        project = self.base / "proj3"
        (project / ".git").mkdir(parents=True)
        code, out = _quiet(zcrew.main, ["enable", str(project), "--with-templates"], env={}, run=FakeRun())
        self.assertEqual(code, 0, out)
        self.assertTrue((project / "AGENTS.md").is_file())
        self.assertTrue((project / "CLAUDE.md").is_file())
        code, out = _quiet(zcrew.main, ["disable", str(project)], env={}, run=FakeRun())
        self.assertEqual(code, 0, out)
        self.assertEqual([p.name for p in project.iterdir()], [".git"])

    def test_enable_defaults_to_cwd(self) -> None:
        project = self.base / "proj2"
        (project / ".git").mkdir(parents=True)
        cwd = os.getcwd()
        os.chdir(project)
        try:
            code, _ = _quiet(zcrew.main, ["enable"], env={}, run=FakeRun())
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertTrue((project / "CLAUDE.md").is_file())

    def test_enable_refuses_non_git_dir(self) -> None:
        project = self.base / "plain"
        project.mkdir()
        code, out = _quiet(zcrew.main, ["enable", str(project)], env={}, run=FakeRun())
        self.assertEqual(code, 1)
        self.assertIn("git", out)


if __name__ == "__main__":
    unittest.main()
