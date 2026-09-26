from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load("zcode_bridge_launcher")


def _make_install(root: Path, with_exe: bool = True) -> Path:
    bundle = root / "resources" / "glm" / "zcode.cjs"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("", encoding="utf-8")
    if with_exe:
        (root / "ZCode.exe").write_text("", encoding="utf-8")
    return bundle


class BundleDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for name in ("local", "roaming", "pf", "pf64"):
            (self.tmp / name).mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _env(self, **extra: str) -> dict[str, str]:
        env = {
            "LOCALAPPDATA": str(self.tmp / "local"),
            "APPDATA": str(self.tmp / "roaming"),
            "ProgramFiles": str(self.tmp / "pf"),
            "ProgramW6432": str(self.tmp / "pf64"),
        }
        env.update(extra)
        return env

    def test_localappdata_layout(self) -> None:
        bundle = _make_install(self.tmp / "local" / "Programs" / "ZCode")
        with mock.patch.dict(os.environ, self._env(), clear=True):
            self.assertEqual(launcher.find_zcode_bundle(), bundle.resolve())

    def test_program_files_layout(self) -> None:
        bundle = _make_install(self.tmp / "pf" / "ZCode")
        with mock.patch.dict(os.environ, self._env(), clear=True):
            self.assertEqual(launcher.find_zcode_bundle(), bundle.resolve())

    def test_program_w6432_layout(self) -> None:
        bundle = _make_install(self.tmp / "pf64" / "ZCode")
        with mock.patch.dict(os.environ, self._env(), clear=True):
            self.assertEqual(launcher.find_zcode_bundle(), bundle.resolve())

    def test_program_files_roots_are_deduped(self) -> None:
        same = str(self.tmp / "pf")
        with mock.patch.dict(os.environ, {"ProgramFiles": same, "ProgramW6432": same + os.sep}, clear=True):
            self.assertEqual(launcher._program_files_roots(), [Path(same)])

    def test_nested_bundle_found_by_bounded_scan(self) -> None:
        bundle = _make_install(self.tmp / "pf" / "ZCode" / "app-3.14.0")
        with mock.patch.dict(os.environ, self._env(), clear=True):
            self.assertEqual(launcher.find_zcode_bundle(), bundle.resolve())

    def test_explicit_bundle_wins(self) -> None:
        _make_install(self.tmp / "local" / "Programs" / "ZCode")
        explicit = _make_install(self.tmp / "custom")
        with mock.patch.dict(os.environ, self._env(ZCODE_CLI_BUNDLE=str(explicit)), clear=True):
            self.assertEqual(launcher.find_zcode_bundle(), explicit.resolve())

    def test_not_found_raises(self) -> None:
        with mock.patch.dict(os.environ, self._env(), clear=True):
            with self.assertRaises(FileNotFoundError) as ctx:
                launcher.find_zcode_bundle()
        self.assertIn("ZCODE_CLI_BUNDLE", str(ctx.exception))

    def test_missing_env_roots_are_skipped(self) -> None:
        with mock.patch.dict(os.environ, {"ProgramFiles": str(self.tmp / "pf")}, clear=True):
            with self.assertRaises(FileNotFoundError):
                launcher.find_zcode_bundle()


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_runtime_next_to_bundle(self) -> None:
        install = self.tmp / "ZCode"
        bundle = _make_install(install)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(launcher.find_zcode_runtime(bundle), (install / "ZCode.exe").resolve())

    def test_explicit_runtime_wins(self) -> None:
        bundle = _make_install(self.tmp / "ZCode")
        custom = self.tmp / "node.exe"
        custom.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ZCODE_BINARY": str(custom)}, clear=True):
            self.assertEqual(launcher.find_zcode_runtime(bundle), custom.resolve())

    def test_runtime_not_found_raises(self) -> None:
        bundle = _make_install(self.tmp / "ZCode", with_exe=False)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            launcher.shutil, "which", return_value=None
        ):
            with self.assertRaises(FileNotFoundError):
                launcher.find_zcode_runtime(bundle)


class BuildEnvTests(unittest.TestCase):
    bundle = Path("C:/ZCode/resources/glm/zcode.cjs")
    runtime = Path("C:/ZCode/ZCode.exe")

    def test_concurrency_defaults_to_five(self) -> None:
        env = launcher.build_env({}, self.bundle, self.runtime)
        self.assertEqual(env["AGENT_MCP_MAX_CONCURRENCY"], "5")
        self.assertEqual(env["AGENT_MCP_DEFAULT_BACKEND"], "zcode")
        self.assertEqual(env["ZCODE_CLI_BUNDLE"], str(self.bundle))
        self.assertEqual(env["ZCODE_BINARY"], str(self.runtime))

    def test_concurrency_env_override_wins(self) -> None:
        env = launcher.build_env({"AGENT_MCP_MAX_CONCURRENCY": "2"}, self.bundle, self.runtime)
        self.assertEqual(env["AGENT_MCP_MAX_CONCURRENCY"], "2")

    def test_base_env_not_mutated(self) -> None:
        base = {"FOO": "bar"}
        launcher.build_env(base, self.bundle, self.runtime)
        self.assertEqual(base, {"FOO": "bar"})


if __name__ == "__main__":
    unittest.main()
