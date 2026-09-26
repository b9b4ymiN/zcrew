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


if __name__ == "__main__":
    unittest.main()
