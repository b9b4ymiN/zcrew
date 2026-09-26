from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commander = _load("commander")

FAKE_KEY = "sk-test-FAKE-commander-0000"


def _rule(pid: str, family: str, mode: str, models: list[str]) -> dict:
    return {
        "providerId": pid,
        "config": {
            "builtinModelIds": models,
            "access": {"type": "zhipu-account", "mode": mode, "accountType": family},
        },
    }


TABLE = {
    "revision": 30,
    "config": {
        "providerConfigRules": {
            "providerRules": [
                _rule("account:zai-individual-coding-plan", "zai", "individual-coding-plan", ["GLM-5.3", "GLM-5.3-Flash"]),
                _rule("account:zai-start-plan", "zai", "start-plan", ["GLM-5.2"]),
                {"providerId": "openai", "config": {"access": {"type": "api-key"}}},
            ]
        }
    },
}


class TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_json(self, path: Path, data: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class DefaultsFileTests(unittest.TestCase):
    def test_default_config_file_matches_code_and_policy(self) -> None:
        data = json.loads((ROOT / "config" / "default-config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, commander.DEFAULT_CONFIG)
        self.assertEqual(data["model"], {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"})
        self.assertEqual((data["thoughtLevel"], data["maxWorkers"], data["maxCorrectionRounds"], data["timeoutSeconds"]),
                         ("max", 5, 4, 1800))


class MergeTests(TempDir):
    def test_defaults_when_no_files(self) -> None:
        cfg = commander.load_effective_config(self.base / "none.json", None, commander.DEFAULT_CONFIG)
        self.assertEqual(cfg, commander.DEFAULT_CONFIG)
        self.assertIsNot(cfg["model"], commander.DEFAULT_CONFIG["model"])

    def test_project_over_user_over_defaults_key_by_key(self) -> None:
        user = self.write_json(self.base / "user.json", {"maxWorkers": 3, "thoughtLevel": "high"})
        project = self.write_json(self.base / "proj.json", {"maxWorkers": 2})
        cfg = commander.load_effective_config(user, project, commander.DEFAULT_CONFIG)
        self.assertEqual(cfg["maxWorkers"], 2)
        self.assertEqual(cfg["thoughtLevel"], "high")
        self.assertEqual(cfg["timeoutSeconds"], 1800)

    def test_model_replaced_as_whole(self) -> None:
        user = self.write_json(self.base / "user.json", {"model": {"providerId": "account:zai-start-plan", "modelId": "GLM-5.2"}})
        project = self.write_json(self.base / "proj.json", {"model": {"modelId": "GLM-5.3-Flash"}})
        cfg = commander.load_effective_config(user, project, commander.DEFAULT_CONFIG)
        self.assertEqual(cfg["model"], {"modelId": "GLM-5.3-Flash"})

    def test_invalid_json_raises_config_error(self) -> None:
        bad = self.base / "user.json"
        bad.write_text("{nope", encoding="utf-8")
        with self.assertRaises(commander.ConfigError):
            commander.load_effective_config(bad, None, commander.DEFAULT_CONFIG)

    def test_non_object_raises_config_error(self) -> None:
        bad = self.write_json(self.base / "user.json", [1, 2])
        with self.assertRaises(commander.ConfigError):
            commander.load_effective_config(bad, None, commander.DEFAULT_CONFIG)

    def test_sources(self) -> None:
        user = self.write_json(self.base / "user.json", {})
        self.assertEqual(commander.config_sources(user, self.base / "missing.json"), ["defaults", f"user {user}"])


class ValidationTests(TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.v2 = self.base / "v2"
        self.write_json(self.v2 / "coding-plan-cache.json", {"entryStatus": {"items": {
            "builtin:zai-coding-plan": {"status": "available"},
        }}})
        self.write_json(self.v2 / "config.json", {"provider": {
            "builtin:zai-coding-plan": {"enabled": True, "options": {"apiKey": FAKE_KEY}},
        }})

    def problems(self, **overrides: object) -> list[str]:
        cfg = {**commander.DEFAULT_CONFIG, **overrides}
        return commander.validate_config(cfg, TABLE, self.v2)

    def assertOneProblem(self, needle: str, **overrides: object) -> None:
        problems = self.problems(**overrides)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(needle, problems[0])

    def test_defaults_are_valid(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_thought_level(self) -> None:
        self.assertEqual(self.problems(thoughtLevel="high"), [])
        self.assertOneProblem('"high" or "max"', thoughtLevel="low")

    def test_max_workers(self) -> None:
        for bad in (0, 6, "5", 2.0, True):
            self.assertOneProblem("maxWorkers", maxWorkers=bad)
        self.assertEqual(self.problems(maxWorkers=1), [])

    def test_max_correction_rounds(self) -> None:
        for bad in (0, 11, None):
            self.assertOneProblem("from 1 to 10", maxCorrectionRounds=bad)
        self.assertEqual(self.problems(maxCorrectionRounds=10), [])

    def test_timeout(self) -> None:
        for bad in (59, 86401, "1800"):
            self.assertOneProblem("from 60 to 86400", timeoutSeconds=bad)
        self.assertEqual(self.problems(timeoutSeconds=60), [])

    def test_model_shape(self) -> None:
        self.assertOneProblem("model must be an object", model="GLM-5.3")
        self.assertOneProblem("model must be an object", model={"modelId": "GLM-5.3"})

    def test_unknown_provider(self) -> None:
        self.assertOneProblem("not a ZCode account plan",
                              model={"providerId": "openai", "modelId": "GLM-5.3"})

    def test_unknown_model(self) -> None:
        self.assertOneProblem("GLM-5.3, GLM-5.3-Flash",
                              model={"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-9"})

    def test_not_entitled(self) -> None:
        self.assertOneProblem("not entitled",
                              model={"providerId": "account:zai-start-plan", "modelId": "GLM-5.2"})

    def test_missing_table(self) -> None:
        problems = commander.validate_config(dict(commander.DEFAULT_CONFIG), None, self.v2)
        self.assertEqual(len(problems), 1)
        self.assertIn("provider table not found", problems[0])

    def test_unknown_key(self) -> None:
        self.assertOneProblem("unknown key 'maxworkers'", maxworkers=3)

    def test_secret_never_in_messages(self) -> None:
        problems = self.problems(model={"providerId": "account:zai-start-plan", "modelId": "X"}, maxWorkers=9)
        self.assertNotIn(FAKE_KEY, "\n".join(problems))


class BlockTextTests(unittest.TestCase):
    CASES = [
        "",
        "# Project\n",
        "# Project\r\n\r\nrules\r\n",
        "# Project",
        "# Project\r\nno trailing",
        "a\n\n",
        "﻿# bom\n",
        "one line\n\n\n",
    ]

    def test_round_trip(self) -> None:
        for original in self.CASES:
            with self.subTest(original=original):
                added = commander.add_block(original)
                self.assertIn(commander.IMPORT_LINE, added)
                self.assertEqual(commander.remove_block(added), original)

    def test_preserves_crlf(self) -> None:
        added = commander.add_block("x\r\ny\r\n")
        self.assertNotIn("\n", added.replace("\r\n", ""))

    def test_block_format(self) -> None:
        self.assertEqual(
            commander.add_block("x\n"),
            "x\n\n<!-- zcode-commander:begin -->\n@~/.claude/zcode-commander/COMMANDER.md\n"
            "<!-- zcode-commander:end -->\n",
        )


class EnableDisableTests(TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.proj = self.base / "proj"
        (self.proj / ".git").mkdir(parents=True)
        self.claude_md = self.proj / "CLAUDE.md"

    def round_trip(self, original: bytes) -> None:
        self.claude_md.write_bytes(original)
        ok, _ = commander.enable(self.proj)
        self.assertTrue(ok)
        self.assertTrue(commander.is_enabled(self.proj))
        ok, _ = commander.disable(self.proj)
        self.assertTrue(ok)
        self.assertEqual(self.claude_md.read_bytes(), original)
        self.assertFalse((self.proj / ".claude").exists())

    def test_round_trip_lf(self) -> None:
        self.round_trip(b"# Rules\n\n- be nice\n")

    def test_round_trip_crlf(self) -> None:
        self.round_trip(b"# Rules\r\n\r\n- be nice\r\n")

    def test_round_trip_no_trailing_newline(self) -> None:
        self.round_trip(b"# Rules\n- be nice")

    def test_round_trip_crlf_no_trailing_newline(self) -> None:
        self.round_trip(b"# Rules\r\n- be nice")

    def test_round_trip_utf8(self) -> None:
        self.round_trip("# กฎ\n- ok\n".encode("utf-8"))

    def test_crlf_block_written_with_crlf(self) -> None:
        self.claude_md.write_bytes(b"a\r\n")
        commander.enable(self.proj)
        data = self.claude_md.read_bytes()
        self.assertIn(b"<!-- zcode-commander:end -->\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_missing_file_created_then_deleted(self) -> None:
        ok, msg = commander.enable(self.proj)
        self.assertTrue(ok)
        self.assertIn("created", msg)
        self.assertTrue((self.proj / commander.CREATED_SIDECAR_REL).is_file())
        commander.disable(self.proj)
        self.assertFalse(self.claude_md.exists())
        self.assertFalse((self.proj / ".claude").exists())

    def test_created_file_kept_if_user_added_content(self) -> None:
        commander.enable(self.proj)
        self.claude_md.write_bytes(self.claude_md.read_bytes() + b"my notes\n")
        commander.disable(self.proj)
        self.assertEqual(self.claude_md.read_bytes(), b"my notes\n")
        self.assertFalse((self.proj / commander.CREATED_SIDECAR_REL).exists())

    def test_existing_claude_dir_kept(self) -> None:
        (self.proj / ".claude").mkdir()
        commander.enable(self.proj)
        commander.disable(self.proj)
        self.assertFalse(self.claude_md.exists())
        self.assertTrue((self.proj / ".claude").is_dir())

    def test_idempotent_enable(self) -> None:
        self.claude_md.write_bytes(b"x\n")
        commander.enable(self.proj)
        first = self.claude_md.read_bytes()
        ok, msg = commander.enable(self.proj)
        self.assertTrue(ok)
        self.assertIn("already enabled", msg)
        self.assertEqual(self.claude_md.read_bytes(), first)

    def test_manual_import_not_duplicated(self) -> None:
        original = b"@~/.claude/zcode-commander/COMMANDER.md\n"
        self.claude_md.write_bytes(original)
        ok, msg = commander.enable(self.proj)
        self.assertTrue(ok)
        self.assertIn("without markers", msg)
        self.assertEqual(self.claude_md.read_bytes(), original)

    def test_disable_when_not_enabled(self) -> None:
        self.claude_md.write_bytes(b"x\n")
        ok, msg = commander.disable(self.proj)
        self.assertTrue(ok)
        self.assertIn("not enabled", msg)
        self.assertEqual(self.claude_md.read_bytes(), b"x\n")

    def test_non_git_refused_unless_forced(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        ok, msg = commander.enable(plain)
        self.assertFalse(ok)
        self.assertIn("git diff", msg)
        self.assertFalse((plain / "CLAUDE.md").exists())
        ok, _ = commander.enable(plain, force=True)
        self.assertTrue(ok)
        self.assertTrue(commander.is_enabled(plain))

    def test_subdirectory_of_git_repo_accepted(self) -> None:
        sub = self.proj / "pkg"
        sub.mkdir()
        self.assertTrue(commander.enable(sub)[0])

    def test_with_project_config_created_and_never_touched(self) -> None:
        self.claude_md.write_bytes(b"x\n")
        commander.enable(self.proj, with_project_config=True)
        cfg_path = self.proj / commander.PROJECT_CONFIG_REL
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8")), commander.DEFAULT_CONFIG)
        cfg_path.write_text('{"maxWorkers": 2}', encoding="utf-8")
        commander.enable(self.proj, with_project_config=True)
        self.assertEqual(cfg_path.read_text(encoding="utf-8"), '{"maxWorkers": 2}')
        commander.disable(self.proj)
        self.assertEqual(cfg_path.read_text(encoding="utf-8"), '{"maxWorkers": 2}')
        self.assertEqual(self.claude_md.read_bytes(), b"x\n")

    def test_no_project_config_by_default(self) -> None:
        commander.enable(self.proj)
        self.assertFalse((self.proj / commander.PROJECT_CONFIG_REL).exists())


class CliTests(TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        v2 = self.home / ".zcode" / "v2"
        self.write_json(v2 / "coding-plan-cache.json", {"entryStatus": {"items": {
            "builtin:zai-coding-plan": {"status": "available"}}}})
        self.table = str(self.write_json(self.base / "zcode-builtin.json", TABLE))
        self.proj = self.base / "proj"
        (self.proj / ".git").mkdir(parents=True)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = commander.main(list(argv), home=self.home, table_path=self.table)
        return code, out.getvalue()

    def test_config_defaults_ok(self) -> None:
        code, out = self.run_cli("config")
        self.assertEqual(code, 0, out)
        self.assertIn("validation: OK", out)
        self.assertIn('"GLM-5.3"', out)

    def test_config_project_override_and_failure(self) -> None:
        self.write_json(self.proj / commander.PROJECT_CONFIG_REL, {"maxWorkers": 9})
        code, out = self.run_cli("config", "--project", str(self.proj))
        self.assertEqual(code, 1)
        self.assertIn("validation: FAIL", out)
        self.assertIn("maxWorkers is 9", out)
        self.assertIn("project ", out)

    def test_config_bad_json_reported(self) -> None:
        path = self.home / ".zcode-commander" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        code, out = self.run_cli("config")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", out)

    def test_status_enable_disable(self) -> None:
        code, out = self.run_cli("status", str(self.proj))
        self.assertIn("enabled: no", out)
        self.assertEqual(code, 0)
        code, out = self.run_cli("enable", str(self.proj))
        self.assertEqual(code, 0)
        code, out = self.run_cli("status", str(self.proj))
        self.assertIn("enabled: yes", out)
        self.assertIn("validation: OK", out)
        code, out = self.run_cli("disable", str(self.proj))
        self.assertEqual(code, 0)
        self.assertFalse((self.proj / "CLAUDE.md").exists())

    def test_enable_non_git_exit_code(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        code, out = self.run_cli("enable", str(plain))
        self.assertEqual(code, 1)
        self.assertIn("--force", out)
        code, _ = self.run_cli("enable", str(plain), "--force")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
