from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
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
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


za = _load("zcode_activity")

REASONING = "SECRET-REASONING-TEXT-7c1e"
FULL_OUTPUT = "SECRET-FULL-OUTPUT-LINE-3b2d"
SANDBOX = r"C:\Work\Sandbox"
NOW = 10_000_000

SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT,
    path TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT, sequence INTEGER);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT, sequence INTEGER);
CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER,
    completed_at INTEGER, duration_ms INTEGER, tool_call_count INTEGER, tool_error_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, computed_total_tokens INTEGER,
    error_type TEXT, error_code TEXT, PRIMARY KEY (session_id, turn_id));
CREATE TABLE tool_usage (session_id TEXT, tool_call_id TEXT, tool_name TEXT, status TEXT,
    exit_code INTEGER, duration_ms INTEGER, error_message TEXT);
"""


def tool(name: str, status: str, inp: dict, output: str | None = None, error: str | None = None) -> dict:
    state: dict = {"status": status, "input": inp, "title": name, "metadata": {}}
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    return {"type": "tool", "callID": f"call_{name}", "tool": name, "state": state}


class Fixture:
    def __init__(self, root: Path) -> None:
        self.path = root / "db.sqlite"
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self._n = 0

    def exec(self, sql: str, args: tuple = ()) -> None:
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute(sql, args)
        conn.close()

    def session(self, sid: str, directory: str, title: str, created: int, updated: int) -> None:
        self.exec("INSERT INTO session (id, directory, title, time_created, time_updated) VALUES (?,?,?,?,?)",
                  (sid, directory, title, created, updated))

    def message(self, mid: str, sid: str, role: str, t: int) -> None:
        self.exec("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                  (mid, sid, t, t, json.dumps({"role": role})))

    def part(self, sid: str, mid: str, data: dict, created: int, updated: int | None = None,
             pid: str | None = None) -> str:
        self._n += 1
        pid = pid or f"part_{self._n:04d}"
        self.exec("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data, sequence)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (pid, mid, sid, created, created if updated is None else updated, json.dumps(data), self._n))
        return pid

    def update_part(self, pid: str, data: dict, updated: int) -> None:
        self.exec("UPDATE part SET data = ?, time_updated = ? WHERE id = ?", (json.dumps(data), updated, pid))

    def turn(self, sid: str, tid: str, status: str, started: int, completed: int, tools: int = 0,
             errors: int = 0) -> None:
        self.exec("INSERT INTO turn_usage (session_id, turn_id, status, started_at, completed_at, duration_ms,"
                  " tool_call_count, tool_error_count) VALUES (?,?,?,?,?,?,?,?)",
                  (sid, tid, status, started, completed, completed - started, tools, errors))


class ZCodeActivityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.db = self.fx.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- fixtures -----------------------------------------------------------------------

    def completed_session(self, sid: str = "sess_aaaa1111-done", directory: str = SANDBOX,
                          at: int = NOW - 60_000) -> None:
        fx = self.fx
        fx.session(sid, directory, "Fix the calc\nsecond line", at, at + 900)
        fx.message(f"{sid}_u", sid, "user", at)
        fx.message(f"{sid}_a", sid, "assistant", at + 10)
        fx.part(sid, f"{sid}_u", {"type": "text", "text": "USER PROMPT TEXT"}, at)
        fx.part(sid, f"{sid}_a", {"type": "step-start"}, at + 10)
        fx.part(sid, f"{sid}_a", {"type": "reasoning", "text": REASONING}, at + 20)
        fx.part(sid, f"{sid}_a", tool("Read", "completed", {"file_path": directory + r"\src\calc.py"},
                                      output=FULL_OUTPUT), at + 100, at + 150)
        fx.part(sid, f"{sid}_a", tool("Bash", "completed",
                                      {"command": f'cd "{directory}" && python -m unittest discover 2>&1'},
                                      output=f"{FULL_OUTPUT}\r\nRan 3 tests\r\n\r\nFAILED (failures=2)\r\n"),
                at + 200, at + 400)
        fx.part(sid, f"{sid}_a", tool("Edit", "error", {"file_path": r"D:\other\x.py",
                                                         "old_string": "a", "new_string": "b\nc"},
                                      error="old_string not found\nmore"), at + 500, at + 510)
        fx.part(sid, f"{sid}_a", {"type": "step-finish", "reason": "stop",
                                  "tokens": {"total": 60315, "input": 60112, "output": 203}}, at + 800)
        fx.part(sid, f"{sid}_a", {"type": "text", "text": "All done.", "time": {"start": at + 700, "end": at + 790}},
                at + 700, at + 790)
        fx.turn(sid, "turn_1", "completed", at, at + 900, tools=3, errors=1)

    # --- schema / db ------------------------------------------------------------------------

    def test_default_db_path_env_override(self) -> None:
        self.assertEqual(za.default_db_path({"ZCREW_ZCODE_DB": "X:/z.sqlite"}), Path("X:/z.sqlite"))
        self.assertEqual(za.default_db_path({}), Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")

    def test_check_schema_ok(self) -> None:
        conn = za.connect(self.db)
        try:
            self.assertEqual(za.check_schema(conn), [])
        finally:
            conn.close()

    def test_check_schema_missing_column_and_table(self) -> None:
        self.fx.exec("ALTER TABLE part DROP COLUMN data")
        self.fx.exec("DROP TABLE turn_usage")
        conn = za.connect(self.db)
        try:
            problems = za.check_schema(conn)
        finally:
            conn.close()
        self.assertTrue(any("part" in p and "data" in p for p in problems), problems)
        self.assertTrue(any("turn_usage" in p for p in problems), problems)
        with self.assertRaises(za.ActivityError) as cm:
            za.list_sessions(self.db)
        self.assertIn("ZCode database schema changed", str(cm.exception))

    def test_connection_is_read_only(self) -> None:
        conn = za.connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                conn.execute("INSERT INTO session (id) VALUES ('sess_x')")
            self.assertIn("readonly", str(cm.exception).replace(" ", "").lower())
        finally:
            conn.close()

    def test_missing_db_is_activity_error(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "db.sqlite"
        with self.assertRaises(za.ActivityError):
            za.list_sessions(missing)
        with self.assertRaises(za.ActivityError):
            za.session_activity(missing, "sess_x")
        self.assertFalse(missing.exists())

    # --- sessions ---------------------------------------------------------------------------

    def test_list_sessions_order_status_and_counts(self) -> None:
        self.completed_session("sess_aaaa1111-done", at=NOW - 60_000)
        fx = self.fx
        fx.session("sess_bbbb2222-live", SANDBOX, "  Running one  ", NOW - 5_000, NOW - 1_000)
        fx.message("m_live", "sess_bbbb2222-live", "assistant", NOW - 5_000)
        fx.part("sess_bbbb2222-live", "m_live", tool("Bash", "running", {"command": "sleep 5"}), NOW - 2_000)
        fx.session("sess_cccc3333-empty", SANDBOX, "", NOW - 3_000, NOW - 3_000)
        fx.session("sess_dddd4444-err", SANDBOX, "Broke", NOW - 90_000, NOW - 80_000)
        fx.message("m_err", "sess_dddd4444-err", "assistant", NOW - 90_000)
        fx.part("sess_dddd4444-err", "m_err", {"type": "step-start"}, NOW - 90_000)
        fx.turn("sess_dddd4444-err", "t", "error", NOW - 90_000, NOW - 80_000)

        rows = za.list_sessions(self.db, now_ms=NOW)
        self.assertEqual([r.id for r in rows], ["sess_bbbb2222-live", "sess_cccc3333-empty",
                                                 "sess_aaaa1111-done", "sess_dddd4444-err"])
        by = {r.id: r for r in rows}
        self.assertEqual(by["sess_bbbb2222-live"].status, "running")
        self.assertEqual(by["sess_bbbb2222-live"].title, "Running one")
        self.assertEqual(by["sess_cccc3333-empty"].status, "idle")
        self.assertEqual(by["sess_dddd4444-err"].status, "failed")
        done = by["sess_aaaa1111-done"]
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.title, "Fix the calc")
        self.assertEqual((done.tool_calls, done.errors), (3, 1))
        self.assertEqual(done.last_context_tokens, 60315)
        self.assertEqual(done.duration_ms, 900)
        self.assertEqual(len(za.list_sessions(self.db, limit=2, now_ms=NOW)), 2)
        self.assertEqual([r.id for r in za.list_sessions(self.db, since_ms=NOW - 4_000, now_ms=NOW)],
                         ["sess_bbbb2222-live", "sess_cccc3333-empty"])

    def test_parts_newer_than_last_turn_is_running_then_completed(self) -> None:
        self.completed_session("sess_aaaa1111-done", at=NOW - 60_000)
        sid = "sess_aaaa1111-done"
        self.fx.message("m2", sid, "user", NOW - 1_000)
        self.fx.part(sid, "m2", {"type": "text", "text": "next please"}, NOW - 1_000)
        self.assertEqual(za.list_sessions(self.db, now_ms=NOW)[0].status, "running")
        self.fx.turn(sid, "turn_2", "completed", NOW - 1_000, NOW - 500)
        self.assertEqual(za.list_sessions(self.db, now_ms=NOW)[0].status, "completed")

    def test_no_turn_row_uses_final_step_or_staleness(self) -> None:
        fx = self.fx
        fx.session("sess_old1", SANDBOX, "old", 1_000, 2_000)
        fx.message("mo", "sess_old1", "assistant", 1_000)
        fx.part("sess_old1", "mo", {"type": "step-finish", "reason": "stop", "tokens": {"total": 5}}, 1_500)
        fx.session("sess_old2", SANDBOX, "abandoned", 1_000, 2_000)
        fx.part("sess_old2", "mo", {"type": "step-start"}, 1_500)
        by = {r.id: r.status for r in za.list_sessions(self.db, now_ms=NOW)}
        self.assertEqual(by, {"sess_old1": "completed", "sess_old2": "idle"})
        self.assertEqual({r.id: r.status for r in za.list_sessions(self.db, now_ms=1_600)}["sess_old2"], "running")

    def test_cwd_filter_subdirs_and_case(self) -> None:
        fx = self.fx
        fx.session("sess_1", r"C:\Work\Sandbox", "a", 1, 5)
        fx.session("sess_2", r"c:\work\sandbox\.worktrees\feat-x", "b", 1, 4)
        fx.session("sess_3", r"C:\Work\SandboxOther", "c", 1, 3)
        fx.session("sess_4", r"D:\elsewhere", "d", 1, 2)
        ids = [r.id for r in za.list_sessions(self.db, cwd="c:/WORK/sandbox/", now_ms=NOW)]
        self.assertEqual(ids, ["sess_1", "sess_2"])

    # --- resolve ----------------------------------------------------------------------------

    def test_resolve_session_prefix(self) -> None:
        fx = self.fx
        fx.session("sess_83cb1cd2-aaaa", SANDBOX, "a", 1, 2)
        fx.session("sess_83cbffff-bbbb", SANDBOX, "b", 1, 3)
        fx.session("sess_x_y", SANDBOX, "c", 1, 4)
        self.assertEqual(za.resolve_session(self.db, "sess_83cb1cd2"), "sess_83cb1cd2-aaaa")
        self.assertEqual(za.resolve_session(self.db, "83cb1"), "sess_83cb1cd2-aaaa")
        self.assertEqual(za.resolve_session(self.db, "sess_83cbffff-bbbb"), "sess_83cbffff-bbbb")
        with self.assertRaises(za.ActivityError) as cm:
            za.resolve_session(self.db, "sess_83cb")
        self.assertIn("ambiguous", str(cm.exception))
        with self.assertRaises(za.ActivityError) as cm:
            za.resolve_session(self.db, "sess_zzzz")
        self.assertIn("no ZCode session", str(cm.exception))
        with self.assertRaises(za.ActivityError):  # '_' is not a LIKE wildcard here
            za.resolve_session(self.db, "sess_x-")

    # --- activity ---------------------------------------------------------------------------

    def test_activity_lines_level2(self) -> None:
        self.completed_session()
        acts, _ = za.session_activity(self.db, "sess_aaaa")
        kinds = [a.kind for a in acts]
        self.assertEqual(kinds, ["file_read", "command", "file_edit", "message", "step", "turn_end"])
        read, bash, edit, msg, step, turn = acts
        self.assertEqual(read.summary, "src/calc.py")
        self.assertEqual(bash.summary, "python -m unittest discover 2>&1 -> FAILED (failures=2)")
        self.assertEqual(bash.detail, "FAILED (failures=2)")
        self.assertEqual(edit.status, "error")
        self.assertIn(r"D:\other\x.py", edit.summary)  # outside the session dir: kept absolute
        self.assertEqual(edit.detail, "old_string not found")
        self.assertEqual(msg.summary, "All done.")
        self.assertIn("60,315", step.summary)
        self.assertEqual(turn.status, "completed")
        self.assertTrue(all(a.session_id == "sess_aaaa1111-done" for a in acts))
        blob = repr(acts)
        self.assertNotIn(REASONING, blob)
        self.assertNotIn(FULL_OUTPUT, blob)
        self.assertNotIn("USER PROMPT TEXT", blob)

    def test_incremental_cursor_running_then_completed_once_each(self) -> None:
        fx = self.fx
        sid = "sess_inc"
        fx.session(sid, SANDBOX, "inc", 1_000, 1_000)
        fx.message("ma", sid, "assistant", 1_000)
        acts, cur = za.session_activity(self.db, sid)
        self.assertEqual(acts, [])

        running = tool("Bash", "running", {"command": "python -m pytest"})
        pid = fx.part(sid, "ma", running, 2_000)
        fx.part(sid, "ma", {"type": "reasoning", "text": REASONING}, 2_000)
        acts, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual([(a.kind, a.status, a.key) for a in acts], [("command", "running", pid)])

        again, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual(again, [])
        fx.update_part(pid, running, 2_500)  # metadata-only update while still running
        again, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual(again, [])

        done = tool("Bash", "completed", {"command": "python -m pytest"}, output="...\n2 failed, 5 passed in 1s\n")
        fx.update_part(pid, done, 3_000)
        acts, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual(len(acts), 1)
        self.assertEqual((acts[0].status, acts[0].key, acts[0].detail),
                         ("completed", pid, "2 failed, 5 passed in 1s"))
        self.assertEqual(acts[0].at_ms, 3_000)

        # a second part landing in the same millisecond as the cursor is not lost
        pid2 = fx.part(sid, "ma", tool("Read", "completed", {"file_path": SANDBOX + r"\a.py"}), 3_000)
        acts, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual([a.key for a in acts], [pid2])
        self.assertEqual(acts[0].summary, "a.py")
        self.assertEqual(za.session_activity(self.db, sid, after=cur)[0], [])

    def test_many_running_tools_not_reemitted_on_metadata_update(self) -> None:
        fx = self.fx
        sid = "sess_many"
        fx.session(sid, SANDBOX, "many", 1, 1)
        fx.message("ma", sid, "assistant", 1)
        running = tool("Bash", "running", {"command": "sleep 60"})
        pids = [fx.part(sid, "ma", running, 1_000 + i, pid=f"p{i}") for i in range(600)]
        acts, cur = za.session_activity(self.db, sid)
        self.assertEqual(len(acts), 600)
        fx.update_part(pids[0], running, 2_000)
        acts, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual(acts, [])
        fx.update_part(pids[0], tool("Bash", "completed", {"command": "sleep 60"}, output="done"), 2_100)
        acts, cur = za.session_activity(self.db, sid, after=cur)
        self.assertEqual([(a.key, a.status) for a in acts], [("p0", "completed")])
        self.assertNotIn("p0", json.loads(cur)["live"])
        self.assertIn("p1", json.loads(cur)["live"])

    def test_streaming_text_waits_for_end(self) -> None:
        fx = self.fx
        sid = "sess_txt"
        fx.session(sid, SANDBOX, "t", 1, 1)
        fx.message("ma", sid, "assistant", 1)
        pid = fx.part(sid, "ma", {"type": "text", "text": "Hel", "time": {"start": 5}}, 5)
        acts, cur = za.session_activity(self.db, sid)
        self.assertEqual(acts, [])
        fx.update_part(pid, {"type": "text", "text": "Hello " + "x" * 400, "time": {"start": 5, "end": 9}}, 9)
        acts, _ = za.session_activity(self.db, sid, after=cur)
        self.assertEqual(len(acts), 1)
        self.assertLessEqual(len(acts[0].summary), za.MESSAGE_CHARS)

    def test_invalid_cursor_and_bad_json_part(self) -> None:
        self.completed_session()
        self.fx.part("sess_aaaa1111-done", "sess_aaaa1111-done_a", {}, NOW, pid="part_bad")
        self.fx.exec("UPDATE part SET data = 'not json' WHERE id = 'part_bad'")
        acts, _ = za.session_activity(self.db, "sess_aaaa1111-done")
        self.assertTrue(acts)
        self.assertEqual(za.list_sessions(self.db, now_ms=NOW)[0].tool_calls, 3)
        with self.assertRaises(za.ActivityError):
            za.session_activity(self.db, "sess_aaaa1111-done", after="garbage")

    def test_display_path_and_command_prefix(self) -> None:
        self.assertEqual(za.display_path(r"C:\Work\Sandbox\pkg\m.py", "c:/work/sandbox"), "pkg/m.py")
        self.assertEqual(za.display_path(r"C:\Work\SandboxX\m.py", SANDBOX), r"C:\Work\SandboxX\m.py")
        long_cmd = "echo " + "y" * 300
        fx = self.fx
        fx.session("sess_cmd", SANDBOX, "c", 1, 1)
        fx.message("ma", "sess_cmd", "assistant", 1)
        fx.part("sess_cmd", "ma", tool("Bash", "running", {"command": long_cmd}), 2)
        acts, _ = za.session_activity(self.db, "sess_cmd")
        self.assertLessEqual(len(acts[0].summary), za.COMMAND_CHARS)
        self.assertTrue(acts[0].summary.endswith("..."))


if __name__ == "__main__":
    unittest.main()
