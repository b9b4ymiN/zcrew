from __future__ import annotations

import errno
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from test_zcode_activity import Fixture, NOW, SANDBOX, tool

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

OTHER_DIR = r"D:\elsewhere"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


cli = _load("activity_cli")


def runs_args(**over: object) -> SimpleNamespace:
    base: dict[str, object] = dict(command="runs", dir=None, limit=20, all=False, json=False)
    base.update(over)
    return SimpleNamespace(**base)


def show_args(session: str, **over: object) -> SimpleNamespace:
    base: dict[str, object] = dict(command="show", session=session, json=False)
    base.update(over)
    return SimpleNamespace(**base)


def watch_args(**over: object) -> SimpleNamespace:
    base: dict[str, object] = dict(command="watch", dir=SANDBOX, all=False, interval=0.0, since=10.0)
    base.update(over)
    return SimpleNamespace(**base)


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


class PipeOut(io.StringIO):
    """write() raises OSError(code) once the write budget is used up."""

    def __init__(self, fail_after: int, code: int) -> None:
        super().__init__()
        self.writes = 0
        self.fail_after = fail_after
        self.code = code

    def write(self, s: str) -> int:  # type: ignore[override]
        self.writes += 1
        if self.writes > self.fail_after:
            raise OSError(self.code, "fake pipe failure")
        return super().write(s)


def seed_completed(fx: Fixture, sid: str = "sess_aaaa1111-done", directory: str = SANDBOX, at: int = NOW - 60_000) -> None:
    fx.session(sid, directory, "Fix the calc\nsecond line", at, at + 900)
    fx.message(f"{sid}_u", sid, "user", at)
    fx.message(f"{sid}_a", sid, "assistant", at + 10)
    fx.part(sid, f"{sid}_a", tool("Read", "completed", {"file_path": directory + r"\src\calc.py"}), at + 100, at + 150)
    fx.part(sid, f"{sid}_a", tool("Bash", "completed",
                                  {"command": f'cd "{directory}" && python -m unittest discover 2>&1'},
                                  output="Ran 3 tests\r\n\r\nFAILED (failures=2)\r\n"), at + 200, at + 400)
    fx.part(sid, f"{sid}_a", tool("Edit", "error", {"file_path": r"D:\other\x.py",
                                                    "old_string": "a", "new_string": "b\nc"},
                                  error="old_string not found\nmore"), at + 500, at + 510)
    fx.part(sid, f"{sid}_a", {"type": "step-finish", "reason": "stop",
                             "tokens": {"total": 60315}}, at + 800)
    fx.part(sid, f"{sid}_a", {"type": "text", "text": "All done.",
                             "time": {"start": at + 700, "end": at + 790}}, at + 700, at + 790)
    fx.turn(sid, "turn_1", "completed", at, at + 900, tools=3, errors=1)


class ActivityCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.db = self.fx.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(
        self, args: SimpleNamespace, *, env_extra: dict[str, str] | None = None,
        out: io.StringIO | None = None, **kw: object,
    ) -> tuple[int, str, str]:
        env = {"ZCREW_ZCODE_DB": str(self.db)}
        env.update(env_extra or {})
        out = out if out is not None else io.StringIO()
        err = io.StringIO()
        code = cli.run(args, env=env, out=out, err=err, **kw)  # type: ignore[arg-type]
        return code, out.getvalue(), err.getvalue()

    # --- runs ---------------------------------------------------------------------------

    def seed_three(self) -> None:
        seed_completed(self.fx)  # sess_aaaa1111-done in SANDBOX, completed
        fx = self.fx
        fx.session("sess_bbbb2222-live", SANDBOX, "Running one", NOW - 5_000, NOW - 1_000)
        fx.message("m_live", "sess_bbbb2222-live", "assistant", NOW - 5_000)
        fx.part("sess_bbbb2222-live", "m_live", tool("Bash", "running", {"command": "sleep 5"}), NOW - 2_000)
        seed_completed(self.fx, sid="sess_cccc3333-alt", directory=OTHER_DIR, at=NOW - 30_000)

    def test_runs_table_filters_by_dir(self) -> None:
        self.seed_three()
        code, out, err = self.run_cli(runs_args(dir=SANDBOX), clock=lambda: NOW)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ID", out)
        self.assertIn("STATUS", out)
        self.assertIn("TITLE", out)
        self.assertIn("sess_aaaa1111", out)  # 13-char short id
        self.assertIn("sess_bbbb2222", out)
        self.assertNotIn("sess_cccc3333", out)  # other directory filtered out
        self.assertIn("completed", out)
        self.assertIn("running", out)
        self.assertIn("Fix the calc", out)
        self.assertIn("60,315", out)

    def test_runs_all_includes_other_dirs(self) -> None:
        self.seed_three()
        code, out, _ = self.run_cli(runs_args(dir=None, all=True), clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("sess_aaaa1111", out)
        self.assertIn("sess_cccc3333", out)

    def test_runs_json(self) -> None:
        self.seed_three()
        code, out, _ = self.run_cli(runs_args(dir=SANDBOX, json=True), clock=lambda: NOW)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)
        by = {r["id"]: r for r in rows}
        self.assertIn("sess_aaaa1111-done", by)
        self.assertEqual(by["sess_aaaa1111-done"]["status"], "completed")
        self.assertEqual(by["sess_aaaa1111-done"]["tool_calls"], 3)
        self.assertEqual(by["sess_aaaa1111-done"]["errors"], 1)
        self.assertEqual(by["sess_aaaa1111-done"]["last_context_tokens"], 60315)
        self.assertNotIn("sess_cccc3333-alt", by)

    def test_runs_renders_thai_title(self) -> None:
        fx = self.fx
        fx.session("sess_thai00001", SANDBOX, "แก้ bug ใน calc", NOW - 60_000, NOW - 50_000)
        code, out, _ = self.run_cli(runs_args(dir=SANDBOX), clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("แก้ bug ใน calc", out)

    # --- show ---------------------------------------------------------------------------

    def test_show_prefix_header_and_lines(self) -> None:
        seed_completed(self.fx)
        code, out, err = self.run_cli(show_args("sess_aaaa"), clock=lambda: NOW)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("session   sess_aaaa1111-done", out)
        self.assertIn("title     Fix the calc", out)
        self.assertIn(f"dir       {SANDBOX}", out)
        self.assertIn("status    completed", out)
        self.assertIn("R read", out)
        self.assertIn("src/calc.py", out)
        self.assertIn("$ cmd", out)
        self.assertIn("python -m unittest discover 2>&1 -> FAILED (failures=2)", out)
        self.assertIn("E edit", out)
        self.assertIn("x D:\\other\\x.py", out)  # failed marker
        self.assertIn("!! old_string not found", out)
        self.assertIn("> msg", out)
        self.assertIn("All done.", out)
        self.assertIn(". step", out)
        self.assertIn("60,315", out)
        self.assertIn("# turn", out)

    def test_show_running_marker(self) -> None:
        fx = self.fx
        fx.session("sess_run1", SANDBOX, "run", NOW - 60_000, NOW - 50_000)
        fx.message("m", "sess_run1", "assistant", NOW - 50_000)
        fx.part("sess_run1", "m", tool("Bash", "running", {"command": "sleep 5"}), NOW - 2_000)
        code, out, _ = self.run_cli(show_args("sess_run1"), clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("status    running", out)
        self.assertIn("$ cmd", out)
        self.assertIn("sleep 5 ...", out)

    def test_show_json(self) -> None:
        seed_completed(self.fx)
        code, out, _ = self.run_cli(show_args("sess_aaaa", json=True), clock=lambda: NOW)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["id"], "sess_aaaa1111-done")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["tool_calls"], 3)
        self.assertEqual([a["kind"] for a in payload["activity"]],
                         ["file_read", "command", "file_edit", "message", "step", "turn_end"])

    def test_show_header_status_matches_runs_without_turn_row(self) -> None:
        fx = self.fx
        fx.session("sess_sfstop", SANDBOX, "sf", NOW - 60_000, NOW - 59_000)
        fx.message("m", "sess_sfstop", "assistant", NOW - 60_000)
        fx.part("sess_sfstop", "m", tool("Read", "completed", {"file_path": SANDBOX + r"\a.py"}), NOW - 59_500)
        fx.part("sess_sfstop", "m", {"type": "step-finish", "reason": "stop", "tokens": {"total": 9}}, NOW - 59_000)
        code, out, _ = self.run_cli(show_args("sess_sfstop"), clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("status    completed", out)  # same verdict as the runs table

    # --- watch --------------------------------------------------------------------------

    def test_watch_new_tool_line_and_completion_once(self) -> None:
        fx = self.fx
        sid = "sess_watch0001"
        fx.session(sid, SANDBOX, "inc", NOW - 5_000, NOW - 5_000)
        fx.message("ma", sid, "assistant", NOW - 5_000)
        state = {"n": 0, "pid": ""}
        running = tool("Bash", "running", {"command": "python -m unittest discover"})

        def fake_sleep(_seconds: float) -> None:
            state["n"] += 1
            if state["n"] == 1:
                state["pid"] = fx.part(sid, "ma", running, NOW - 1_000)
            elif state["n"] == 2:
                fx.update_part(state["pid"], tool("Bash", "completed",
                                                  {"command": "python -m unittest discover"},
                                                  output="...\nOK"), NOW - 500)

        code, out, err = self.run_cli(watch_args(), sleep=fake_sleep, clock=lambda: NOW, max_polls=4)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("w1 sess_watch000 > started: inc", out)
        self.assertEqual(out.count("python -m unittest discover ..."), 1)  # running, once
        self.assertEqual(out.count("python -m unittest discover -> OK"), 1)  # completed, once
        self.assertEqual(out.count(" > started: "), 1)

    def test_watch_first_sight_skips_backlog(self) -> None:
        fx = self.fx
        now = 1_700_000_000_000  # NOW - 3h would fall before the epoch for localtime()
        sid = "sess_backlog1"
        three_hours_ago = now - 3 * 60 * 60 * 1000
        fx.session(sid, SANDBOX, "long one", three_hours_ago, three_hours_ago)
        fx.message("ma", sid, "assistant", three_hours_ago)
        for i in range(50):
            fx.part(sid, "ma", tool("Bash", "completed", {"command": f"old-cmd-{i:03d}"}, output="done"),
                    three_hours_ago + i * 100, three_hours_ago + i * 100 + 10)
        fx.part(sid, "ma", tool("Bash", "completed", {"command": "new-cmd"}, output="OK"), now - 60_000)
        code, out, err = self.run_cli(watch_args(), clock=lambda: now, max_polls=2)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("w1 sess_backlog1 > active: long one", out)  # started before the window
        self.assertNotIn(" > started: ", out)
        self.assertIn("new-cmd -> OK", out)
        self.assertNotIn("old-cmd-", out)  # the 50-call backlog is never printed

    def test_watch_backlog_bound_is_per_session_first_sight(self) -> None:
        fx = self.fx
        base = 1_700_000_000_000
        sid = "sess_latebee1"
        three_hours_ago = base - 3 * 60 * 60 * 1000
        fx.session(sid, SANDBOX, "late one", three_hours_ago, three_hours_ago)
        fx.message("ma", sid, "assistant", three_hours_ago)
        fx.part(sid, "ma", tool("Bash", "completed", {"command": "ancient-cmd"}, output="done"),
                three_hours_ago + 100)
        ticks = {"t": base}
        sleeps = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            sleeps["n"] += 1
            ticks["t"] += 10 * 60_000  # each poll happens 10 minutes later
            if sleeps["n"] == 2:  # fresh activity lands between poll 2 and poll 3
                fx.part(sid, "ma", tool("Bash", "completed", {"command": "fresh-cmd"}, output="OK"),
                        base + 19 * 60_000, base + 19 * 60_000)

        code, out, err = self.run_cli(watch_args(), sleep=fake_sleep, clock=lambda: ticks["t"], max_polls=4)
        self.assertEqual((code, err), (0, ""))
        # polls 1-2: outside the window (updated 3h ago); first sight only at poll 3
        self.assertEqual(out.count(" > started: ") + out.count(" > active: "), 1)
        self.assertIn("w1 sess_latebee1 > active: late one", out)  # bound is first sight - since
        self.assertIn("fresh-cmd -> OK", out)
        self.assertNotIn("ancient-cmd", out)  # older than the per-session bound is never printed

    def test_watch_second_session_gets_w2(self) -> None:
        fx = self.fx
        fx.session("sess_alpha0001", SANDBOX, "first", NOW - 5_000, NOW - 5_000)
        fx.message("m1", "sess_alpha0001", "assistant", NOW - 5_000)

        state = {"inserted": False}

        def fake_sleep(_seconds: float) -> None:
            if state["inserted"]:
                return
            state["inserted"] = True
            fx.session("sess_beta00002", SANDBOX, "second", NOW - 1_000, NOW - 1_000)
            fx.message("m2", "sess_beta00002", "assistant", NOW - 1_000)

        code, out, _ = self.run_cli(watch_args(), sleep=fake_sleep, clock=lambda: NOW, max_polls=3)
        self.assertEqual(code, 0)
        self.assertEqual(out.count(" > started: "), 2)
        self.assertIn("w1 sess_alpha000 > started: first", out)
        self.assertIn("w2 sess_beta0000 > started: second", out)

    def test_watch_keyboard_interrupt_exits_zero(self) -> None:
        fx = self.fx
        fx.session("sess_ki", SANDBOX, "ki", NOW - 5_000, NOW - 5_000)

        def boom(_seconds: float) -> None:
            raise KeyboardInterrupt()

        code, out, err = self.run_cli(watch_args(), sleep=boom, clock=lambda: NOW, max_polls=None)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("watching"))
        self.assertEqual(err, "")

    # --- errors -------------------------------------------------------------------------

    def test_missing_db_message_and_exit_code(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "db.sqlite"
        env = {"ZCREW_ZCODE_DB": str(missing)}
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(runs_args(dir=SANDBOX), env=env, out=out, err=err, clock=lambda: NOW)
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("zcrew: "))
        self.assertIn("not be installed", err.getvalue())
        self.assertIn("has not run yet", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_show_unknown_session(self) -> None:
        seed_completed(self.fx)
        code, out, err = self.run_cli(show_args("sess_nope"), clock=lambda: NOW)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("zcrew: no ZCode session matches", err)

    # --- broken pipe --------------------------------------------------------------------

    def test_pipe_error_returns_zero_without_raising(self) -> None:
        seed_completed(self.fx)
        out = PipeOut(fail_after=2, code=errno.EINVAL)  # reader went away mid-output
        code, text, _ = self.run_cli(show_args("sess_aaaa1111-done"), out=out, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("session   sess_aaaa1111-done", text)

    def test_other_oserror_still_propagates(self) -> None:
        seed_completed(self.fx)
        out = PipeOut(fail_after=1, code=errno.EACCES)
        with self.assertRaises(OSError):
            self.run_cli(show_args("sess_aaaa1111-done"), out=out, clock=lambda: NOW)

    # --- colour -------------------------------------------------------------------------

    def test_no_colour_when_not_a_tty(self) -> None:
        seed_completed(self.fx)
        code, out, _ = self.run_cli(runs_args(dir=SANDBOX), clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", out)

    def test_no_colour_with_no_color_env(self) -> None:
        seed_completed(self.fx)
        tty = FakeTty()
        code = cli.run(runs_args(dir=SANDBOX), env={"ZCREW_ZCODE_DB": str(self.db), "NO_COLOR": "1"},
                       out=tty, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", tty.getvalue())

    def test_colour_on_tty(self) -> None:
        seed_completed(self.fx)
        tty = FakeTty()
        with mock.patch.object(cli, "_VT_STATE", True):
            code = cli.run(runs_args(dir=SANDBOX), env={"ZCREW_ZCODE_DB": str(self.db)},
                           out=tty, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("\x1b[36m", tty.getvalue())  # completed status painted


# --- zcrew.py wiring --------------------------------------------------------------------


class ZcrewWiringTest(unittest.TestCase):
    def test_load_sibling_imports_zcode_activity(self) -> None:
        zcrew = _load("zcrew")
        za = zcrew._load_sibling("zcode_activity")
        self.assertTrue(hasattr(za, "list_sessions"))
        self.assertTrue(hasattr(za, "session_activity"))
        self.assertIn("_zcrew_zcode_activity", sys.modules)

    def test_parser_accepts_new_commands(self) -> None:
        zcrew = _load("zcrew")
        parser = zcrew.build_parser()
        self.assertEqual(parser.parse_args(["runs"]).command, "runs")
        self.assertEqual(parser.parse_args(["runs", "--limit", "3", "--all", "--json"]).limit, 3)
        self.assertEqual(parser.parse_args(["show", "sess_x"]).command, "show")
        watch = parser.parse_args(["watch", "--all"])
        self.assertEqual(watch.command, "watch")
        self.assertEqual((watch.interval, watch.since), (1.5, 10.0))


if __name__ == "__main__":
    unittest.main()
