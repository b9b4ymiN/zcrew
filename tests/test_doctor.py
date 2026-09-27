from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = _load("doctor")

SECRET = "sk-test-SECRET-do-not-print-1234"
PROVIDER_ID = "builtin:zai-coding-plan"


def _provider(enabled: bool = True, api_key: str = SECRET) -> dict:
    return {
        "enabled": enabled,
        "name": "Z.ai Coding Plan",
        "options": {"apiKey": api_key, "baseURL": "https://example.invalid"},
        "models": {"GLM-5.3": {"name": "GLM-5.3"}, "GLM-5.3-Flash": {"name": "GLM-5.3-Flash"}},
    }


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")


class CliConfigCheckTests(ConfigTestCase):
    def test_missing_file_fails_with_hint(self) -> None:
        result = doctor.check_cli_config(self.path)
        self.assertFalse(result.ok)
        self.assertIn("-EnsureZCodeCliConfig", result.detail)

    def test_invalid_json_fails(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(doctor.check_cli_config(self.path).ok)

    def test_missing_model_main_fails_with_hint(self) -> None:
        self.write({"mcp": {}, "plugins": {}, "skills": {}})
        result = doctor.check_cli_config(self.path)
        self.assertFalse(result.ok)
        self.assertIn("model.main", result.detail)
        self.assertIn("-EnsureZCodeCliConfig", result.detail)

    def test_model_main_without_provider_prefix_fails(self) -> None:
        self.write({"model": {"main": "GLM-5.3"}, "provider": {PROVIDER_ID: _provider()}})
        self.assertFalse(doctor.check_cli_config(self.path).ok)

    def test_model_main_pointing_to_absent_provider_fails(self) -> None:
        self.write({"model": {"main": "builtin:other/GLM-5.3"}, "provider": {PROVIDER_ID: _provider()}})
        result = doctor.check_cli_config(self.path)
        self.assertFalse(result.ok)
        self.assertIn("builtin:other", result.detail)
        self.assertIn("-EnsureZCodeCliConfig", result.detail)

    def test_valid_config_passes(self) -> None:
        self.write({"model": {"main": f"{PROVIDER_ID}/GLM-5.3"}, "provider": {PROVIDER_ID: _provider()}})
        result = doctor.check_cli_config(self.path)
        self.assertTrue(result.ok)
        self.assertIn(f"{PROVIDER_ID}/GLM-5.3", result.detail)

    def test_secret_never_in_output(self) -> None:
        cases = [
            {"model": {"main": f"{PROVIDER_ID}/GLM-5.3"}, "provider": {PROVIDER_ID: _provider()}},
            {"model": {"main": "builtin:other/GLM-5.3"}, "provider": {PROVIDER_ID: _provider()}},
            {"provider": {PROVIDER_ID: _provider()}, "headers": {"Authorization": SECRET}},
        ]
        for data in cases:
            with self.subTest(data=list(data)):
                self.write(data)
                self.assertNotIn(SECRET, doctor.check_cli_config(self.path).detail)


class DesktopConfigTests(ConfigTestCase):
    def test_missing_desktop_config_fails(self) -> None:
        self.assertFalse(doctor.check_desktop_config(self.path).ok)

    def test_present_desktop_config_passes(self) -> None:
        self.write({"provider": {}})
        self.assertTrue(doctor.check_desktop_config(self.path).ok)

    def test_lists_enabled_providers_with_key_only(self) -> None:
        self.write({
            "provider": {
                PROVIDER_ID: _provider(),
                "builtin:disabled": _provider(enabled=False),
                "builtin:nokey": _provider(api_key=""),
            }
        })
        detail = doctor.describe_desktop_providers(self.path)
        self.assertEqual(detail, f"{PROVIDER_ID}: GLM-5.3, GLM-5.3-Flash")
        self.assertNotIn(SECRET, detail)

    def test_no_usable_providers(self) -> None:
        self.write({"provider": {"builtin:disabled": _provider(enabled=False)}})
        self.assertIn("none", doctor.describe_desktop_providers(self.path))

    def test_unreadable_desktop_config(self) -> None:
        self.assertIn("cannot read", doctor.describe_desktop_providers(self.path))


class AppServerOutputTests(unittest.TestCase):
    READY = '{"method":"startup/storageState","params":{"phase":"ready"}}'

    def test_ready_signal_passes(self) -> None:
        stdout = '{"method":"startup/storageState","params":{"phase":"checking"}}\n' + self.READY
        self.assertTrue(doctor.check_app_server_output(stdout, "", 0).ok)

    def test_launch_failure_reports_stderr(self) -> None:
        result = doctor.check_app_server_output("", "cannot locate zcode-builtin.json", 1)
        self.assertFalse(result.ok)
        self.assertIn("zcode-builtin.json", result.detail)

    def test_no_ready_signal_fails(self) -> None:
        result = doctor.check_app_server_output("not json\n", "", None)
        self.assertFalse(result.ok)


class ProbeOutputTests(unittest.TestCase):
    @staticmethod
    def _payload(available: bool, reason: str | None = None) -> str:
        zcode = {"available": available, **({"reason": reason} if reason else {})}
        return json.dumps({"availableBackends": {"zcode": zcode}})

    def test_available_backend_passes(self) -> None:
        result = doctor.check_probe_output(self._payload(True), "", 0)
        self.assertTrue(result.ok)

    def test_unavailable_backend_fails_with_reason(self) -> None:
        result = doctor.check_probe_output(self._payload(False, "ZCode runtime not found"), "", 0)
        self.assertFalse(result.ok)
        self.assertIn("ZCode runtime not found", result.detail)

    def test_non_json_output_fails(self) -> None:
        result = doctor.check_probe_output("", "Traceback: boom", 1)
        self.assertFalse(result.ok)
        self.assertIn("boom", result.detail)


class ZCodeVersionTests(unittest.TestCase):
    def test_tested_version_ok(self) -> None:
        level, detail = doctor.zcode_version_status([("Git", "2.45"), ("ZCode 3.14.0", "3.14.0")])
        self.assertEqual(level, "OK")
        self.assertEqual(detail, "3.14.0 (tested)")

    def test_untested_version_warns(self) -> None:
        level, detail = doctor.zcode_version_status([("ZCode 3.15.1", "3.15.1")])
        self.assertEqual(level, "WARN")
        self.assertIn("3.15.1 is untested", detail)
        self.assertIn("validated on 3.14.0", detail)
        self.assertIn("run a small task", detail)

    def test_missing_warns(self) -> None:
        level, detail = doctor.zcode_version_status([("Other", "1.0"), ("ZCode", "")])
        self.assertEqual(level, "WARN")
        self.assertIn("not found", detail)

    def test_name_must_start_with_zcode(self) -> None:
        self.assertEqual(doctor.zcode_version_status([("Not ZCode", "3.14.0")])[0], "WARN")

    def test_warn_line_format(self) -> None:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.warn("zcode version", "x")
        self.assertEqual(out.getvalue(), "[WARN] zcode version: x\n")

    def test_registry_reader_returns_pairs(self) -> None:
        entries = doctor.read_uninstall_entries()
        self.assertIsInstance(entries, list)
        for name, version in entries:
            self.assertIsInstance(name, str)
            self.assertIsInstance(version, str)


class CommanderConfigCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.commander = _load("commander")
        rule = {
            "providerId": "account:zai-individual-coding-plan",
            "config": {
                "builtinModelIds": ["GLM-5.3"],
                "access": {"type": "zhipu-account", "mode": "individual-coding-plan", "accountType": "zai"},
            },
        }
        self.table = base / "zcode-builtin.json"
        self.table.write_text(json.dumps({"config": {"providerConfigRules": {"providerRules": [rule]}}}), encoding="utf-8")
        self.v2 = base / "v2"
        self.v2.mkdir()
        (self.v2 / "coding-plan-cache.json").write_text(
            json.dumps({"entryStatus": {"items": {PROVIDER_ID: {"status": "available"}}}}), encoding="utf-8"
        )
        self.user = base / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_check(self) -> "doctor.CheckResult":
        return doctor.check_commander_config(self.commander, self.user, str(self.table), self.v2)

    def test_missing_user_file_is_defaults_ok(self) -> None:
        result = self.run_check()
        self.assertTrue(result.ok, result.detail)
        self.assertIn("defaults", result.detail)
        self.assertIn("GLM-5.3", result.detail)

    def test_invalid_user_config_fails(self) -> None:
        self.user.write_text(json.dumps({"thoughtLevel": "low"}), encoding="utf-8")
        result = self.run_check()
        self.assertFalse(result.ok)
        self.assertIn("thoughtLevel", result.detail)

    def test_unentitled_fails(self) -> None:
        (self.v2 / "coding-plan-cache.json").unlink()
        result = self.run_check()
        self.assertFalse(result.ok)
        self.assertIn("not entitled", result.detail)



CODEX_GET_OK = """zcode_executor
  enabled: true
  transport: stdio
  command: C:/Python312/python.exe
  args: C:/Users/u/.zcode-commander/zcode_bridge_launcher.py
  startup_timeout_sec: 60
  tool_timeout_sec: 180
  default_tools_approval_mode: approve
"""


class FakeCli:
    """which + run doubles: tools maps name -> (path, returncode, stdout, stderr)."""

    def __init__(self, **tools: tuple[str, int, str, str]) -> None:
        self.tools = tools
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return self.tools[name][0] if name in self.tools else None

    def run(self, argv, **kwargs):  # noqa: ANN001
        import subprocess

        self.calls.append(list(argv))
        assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace"
        for path, code, out, err in self.tools.values():
            if path in argv:
                return subprocess.CompletedProcess(argv, code, out, err)
        raise AssertionError(argv)


class RegistrationTests(unittest.TestCase):
    def statuses(self, cli: FakeCli) -> list:
        return [doctor.claude_registration(cli.which, cli.run), doctor.codex_registration(cli.which, cli.run)]

    def test_both_registered(self) -> None:
        cli = FakeCli(claude=("C:/c/claude.exe", 0, "zcode_executor: ok", ""),
                      codex=("C:/n/codex.cmd", 0, CODEX_GET_OK, ""))
        statuses = self.statuses(cli)
        self.assertEqual([s.level for s in statuses], ["OK", "OK"])
        self.assertIn("tool_timeout_sec=180", statuses[1].detail)
        self.assertEqual(doctor.registrations_ready(statuses), (True, None))
        self.assertEqual(cli.calls[1][-4:], ["C:/n/codex.cmd", "mcp", "get", "zcode_executor"])

    def test_claude_absent_is_info_codex_ok_is_ready(self) -> None:
        statuses = self.statuses(FakeCli(codex=("codex.exe", 0, CODEX_GET_OK, "")))
        self.assertEqual(statuses[0], doctor.Status("INFO", "claude", "not installed"))
        self.assertEqual(statuses[1].level, "OK")
        self.assertTrue(doctor.registrations_ready(statuses)[0])

    def test_codex_absent_is_info_claude_ok_is_ready(self) -> None:
        statuses = self.statuses(FakeCli(claude=("claude.exe", 0, "ok", "")))
        self.assertEqual(statuses[1], doctor.Status("INFO", "codex", "not installed"))
        self.assertTrue(doctor.registrations_ready(statuses)[0])

    def test_neither_installed_fails(self) -> None:
        ready, extra = doctor.registrations_ready(self.statuses(FakeCli()))
        self.assertFalse(ready)
        self.assertEqual(extra.level, "FAIL")
        self.assertIn("neither", extra.detail)

    def test_codex_not_registered_warns_and_claude_ok_is_ready(self) -> None:
        cli = FakeCli(claude=("claude.exe", 0, "ok", ""),
                      codex=("codex.exe", 1, "", "Error: No MCP server named 'zcode_executor' found."))
        statuses = self.statuses(cli)
        self.assertEqual(statuses[1].level, "WARN")
        self.assertFalse(statuses[1].registered)
        self.assertIn("zcrew update --commander both", statuses[1].detail)
        self.assertEqual(doctor.registrations_ready(statuses), (True, None))

    def test_claude_not_registered_warns_and_codex_ok_is_ready(self) -> None:
        cli = FakeCli(claude=("claude.exe", 1, 'No MCP server named "zcode_executor". Configured servers: x', ""),
                      codex=("codex.exe", 0, CODEX_GET_OK, ""))
        statuses = self.statuses(cli)
        self.assertEqual(statuses[0].level, "WARN")
        self.assertIn("zcrew update --commander both", statuses[0].detail)
        self.assertTrue(doctor.registrations_ready(statuses)[0])

    def test_none_registered_fails(self) -> None:
        cli = FakeCli(claude=("claude.exe", 1, "", 'No MCP server named "zcode_executor".'),
                      codex=("codex.exe", 1, "", "Error: No MCP server named 'zcode_executor' found."))
        statuses = self.statuses(cli)
        self.assertEqual([s.level for s in statuses], ["WARN", "WARN"])
        ready, extra = doctor.registrations_ready(statuses)
        self.assertFalse(ready)
        self.assertEqual(extra.level, "FAIL")
        self.assertIn("no commander has zcode_executor registered", extra.detail)

    def test_broken_registration_fails(self) -> None:
        cli = FakeCli(claude=("claude.exe", 0, "ok", ""),
                      codex=("codex.exe", 2, "", "Error: failed to parse config.toml"))
        statuses = self.statuses(cli)
        self.assertEqual(statuses[1].level, "FAIL")
        self.assertIn("config.toml", statuses[1].detail)
        self.assertEqual(doctor.registrations_ready(statuses), (False, None))
        statuses = self.statuses(FakeCli(claude=("claude.exe", 1, "", "unexpected crash")))
        self.assertEqual(statuses[0].level, "FAIL")
        self.assertFalse(doctor.registrations_ready(statuses)[0])

    def test_codex_low_or_missing_timeout_warns_but_counts(self) -> None:
        for out, shown in ((CODEX_GET_OK.replace("180", "60"), "60"),
                           (CODEX_GET_OK.replace("  tool_timeout_sec: 180\n", ""), "not set")):
            with self.subTest(shown=shown):
                statuses = self.statuses(FakeCli(codex=("codex.exe", 0, out, "")))
                self.assertEqual(statuses[1].level, "WARN")
                self.assertIn(shown, statuses[1].detail)
                self.assertIn("zcrew update", statuses[1].detail)
                self.assertTrue(doctor.registrations_ready(statuses)[0])

    def test_run_exception_fails(self) -> None:
        def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise OSError("cannot start")
        status = doctor.codex_registration(lambda name: "codex.exe", boom)
        self.assertEqual((status.level, status.detail), ("FAIL", "cannot start"))

    def test_parse_tool_timeout_text_and_json(self) -> None:
        self.assertEqual(doctor.parse_tool_timeout(CODEX_GET_OK), 180.0)
        self.assertEqual(doctor.parse_tool_timeout('{"tool_timeout_sec": 180.0}'), 180.0)
        self.assertIsNone(doctor.parse_tool_timeout("tool_timeout_sec: -"))

    def test_cmd_shims_run_through_cmd_exe(self) -> None:
        argv = doctor.cli_argv("C:/npm/codex.cmd", "mcp", "get", "x", env={"COMSPEC": "C:/Windows/cmd.exe"})
        if doctor.os.name == "nt":
            self.assertEqual(argv, ["C:/Windows/cmd.exe", "/d", "/s", "/c", "C:/npm/codex.cmd", "mcp", "get", "x"])
        self.assertEqual(doctor.cli_argv("codex.exe", "a"), ["codex.exe", "a"])

    def test_emit_levels(self) -> None:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for level in ("OK", "FAIL", "WARN", "INFO"):
                doctor.emit(doctor.Status(level, "n", "d"))
        self.assertEqual(out.getvalue().splitlines(), ["[OK]   n: d", "[FAIL] n: d", "[WARN] n: d", "[INFO] n: d"])


if __name__ == "__main__":
    unittest.main()
