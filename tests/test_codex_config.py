from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import tomllib
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


cc = _load("codex_config")

EXPECTED = {"startup_timeout_sec": 60, "tool_timeout_sec": 180, "default_tools_approval_mode": "approve"}

MID_FILE = """# my codex config
model = "gpt-5"

[mcp_servers.other]
command = "node"
args = ["other.js"]
tool_timeout_sec = 30 # keep mine

[mcp_servers.zcode_executor]
command = "python"
args = [
    "C:/x/zcode_bridge_launcher.py",
]

# comment before the next table
[mcp_servers.zcode_executor.env]
FOO = "bar"

[profiles.fast]
model = "gpt-5-mini"
"""

AT_EOF = """[mcp_servers.other]
command = "node"

[mcp_servers.zcode_executor]
command = "python"
args = ["C:/x/zcode_bridge_launcher.py"]"""


class ApplySettingsTests(unittest.TestCase):
    def check(self, original: str) -> str:
        new, changed = cc.apply_settings(original)
        self.assertTrue(changed)
        data = tomllib.loads(new.lstrip("\ufeff"))
        server = data["mcp_servers"]["zcode_executor"]
        for key, value in EXPECTED.items():
            self.assertEqual(server[key], value)
        again, changed_again = cc.apply_settings(new)
        self.assertFalse(changed_again)
        self.assertEqual(again, new)
        return new

    def test_mid_file_keeps_everything_else(self) -> None:
        new = self.check(MID_FILE)
        data = tomllib.loads(new)
        self.assertEqual(data["mcp_servers"]["other"], {"command": "node", "args": ["other.js"], "tool_timeout_sec": 30})
        self.assertEqual(data["mcp_servers"]["zcode_executor"]["env"], {"FOO": "bar"})
        self.assertEqual(data["profiles"]["fast"]["model"], "gpt-5-mini")
        self.assertIn("tool_timeout_sec = 30 # keep mine", new)
        self.assertIn("# comment before the next table", new)
        # inserted after the last key of the table, before the comment and subtable
        self.assertIn(']\nstartup_timeout_sec = 60\ntool_timeout_sec = 180\ndefault_tools_approval_mode = "approve"\n', new)
        self.assertEqual(new.replace('startup_timeout_sec = 60\ntool_timeout_sec = 180\n'
                                     'default_tools_approval_mode = "approve"\n', ""), MID_FILE)

    def test_section_at_eof_without_newline(self) -> None:
        new = self.check(AT_EOF)
        self.assertTrue(new.startswith(AT_EOF + "\n"))
        self.assertEqual(new.count("tool_timeout_sec"), 1)

    def test_crlf_preserved(self) -> None:
        new = self.check(MID_FILE.replace("\n", "\r\n"))
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_bom_preserved(self) -> None:
        new = self.check("\ufeff" + AT_EOF + "\n")
        self.assertTrue(new.startswith("\ufeff"))

    def test_existing_wrong_values_replaced_once(self) -> None:
        text = '[mcp_servers.zcode_executor]\ncommand = "python"\ntool_timeout_sec = 60\n\n[x]\ny = 1\n'
        new = self.check(text)
        self.assertEqual(new.count("tool_timeout_sec"), 1)
        self.assertIn("[x]\ny = 1\n", new)

    def test_quoted_header(self) -> None:
        self.check('[mcp_servers."zcode_executor"]\ncommand = "python"\n')

    def test_already_set_is_no_op(self) -> None:
        text = (
            '[mcp_servers.zcode_executor]\ncommand = "python"\nstartup_timeout_sec = 60\n'
            'tool_timeout_sec = 180\ndefault_tools_approval_mode = "approve"\n'
        )
        self.assertEqual(cc.apply_settings(text), (text, False))

    def test_missing_server_raises(self) -> None:
        with self.assertRaises(cc.CodexConfigError):
            cc.apply_settings('[mcp_servers.other]\ncommand = "x"\n')

    def test_inline_form_raises(self) -> None:
        with self.assertRaises(cc.CodexConfigError):
            cc.apply_settings('mcp_servers = { zcode_executor = { command = "python" } }\n')

    def test_invalid_toml_raises(self) -> None:
        with self.assertRaises(cc.CodexConfigError):
            cc.apply_settings("[mcp_servers.zcode_executor\n")


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.config = self.base / ".codex" / "config.toml"
        self.config.parent.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_backup_then_write_then_no_op(self) -> None:
        self.config.write_bytes(MID_FILE.encode("utf-8"))
        changed, backup = cc.update_file(self.config, now=0)
        self.assertTrue(changed)
        self.assertTrue(backup.name.startswith("config.toml.bak-zcrew-"))
        self.assertEqual(backup.read_bytes(), MID_FILE.encode("utf-8"))
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"]["zcode_executor"]["tool_timeout_sec"], 180)
        before = sorted(p.name for p in self.config.parent.iterdir())
        self.assertEqual(cc.update_file(self.config), (False, None))
        self.assertEqual(sorted(p.name for p in self.config.parent.iterdir()), before)

    def test_backup_name_never_overwrites(self) -> None:
        first = cc.backup_path(self.config, now=0)
        first.write_text("x", encoding="utf-8")
        self.assertNotEqual(cc.backup_path(self.config, now=0), first)

    def test_error_leaves_file_untouched(self) -> None:
        self.config.write_bytes(b"[other]\n")
        with self.assertRaises(cc.CodexConfigError):
            cc.update_file(self.config)
        self.assertEqual([p.name for p in self.config.parent.iterdir()], ["config.toml"])

    def test_default_path(self) -> None:
        self.assertEqual(cc.default_config_path({"CODEX_HOME": str(self.base / "ch")}), self.base / "ch" / "config.toml")
        self.assertEqual(cc.default_config_path({}, home=self.base), self.base / ".codex" / "config.toml")

    def test_cli(self) -> None:
        self.config.write_bytes(AT_EOF.encode("utf-8"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cc.main(["apply"], env={"CODEX_HOME": str(self.config.parent)}), 0)
            self.assertEqual(cc.main(["apply", "--config", str(self.config)]), 0)
            self.assertEqual(cc.main(["apply", "--config", str(self.base / "none.toml")]), 1)
        text = out.getvalue()
        self.assertIn("backup: config.toml.bak-zcrew-", text)
        self.assertIn("(no change)", text)
        self.assertIn("not found", text)


if __name__ == "__main__":
    unittest.main()
