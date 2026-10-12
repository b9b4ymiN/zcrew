"""Codex Stop hook: which runs it follows, when it waits, what it reports, install."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_zcode_activity import Fixture, _load

hook = _load("codex_hook")
MIN = 60_000
NOW = 1_800_000_000_000


def session(sid: str, directory: str, status: str, created: int, updated: int, title: str = "Task") -> SimpleNamespace:
    return SimpleNamespace(id=sid, directory=directory, status=status, created_ms=created, updated_ms=updated,
                           title=title, parent_id=None)


def record(run_id: str, cwd: str, started: int, *, thread: str | None = None, rounds: int = 1,
           round_started: int | None = None) -> dict:
    return {"runIds": [run_id], "threadId": thread, "cwd": cwd, "startedAt": started, "rounds": rounds,
            "roundStartedAt": round_started or started, "verdicts": []}


def run_state(run_id: str, state: str, status: str = "completed", rounds: int = 1) -> object:
    return hook.RunState(run_id=run_id, session_id="sess_" + run_id, title="T " + run_id, cwd="/p", rounds=rounds,
                         state=state, status=status, started_ms=NOW - 10 * MIN, ended_ms=NOW if state == "ended" else None)


class FollowTests(unittest.TestCase):
    def test_states(self) -> None:
        records = [
            record("run_a", "/p", NOW - 30 * MIN, thread="sess_a"),
            record("run_b", "/p", NOW - 20 * MIN),  # thread id never seen: matched by folder and time
            record("run_c", "/p", NOW - 10 * MIN),  # no session yet: queued for a slot
            record("run_d", "/w", NOW - 40 * MIN, thread="sess_d"),
            record("run_e", "/e", NOW - MIN, thread="sess_e"),
        ]
        sessions = [
            session("sess_a", "/p", "completed", NOW - 30 * MIN, NOW - 5 * MIN),
            session("sess_old", "/p", "completed", NOW - 90 * MIN, NOW - 80 * MIN),  # before run_b: not it
            session("sess_b", "/p", "running", NOW - 20 * MIN + 2000, NOW),
            session("sess_d", "/w", "idle", NOW - 40 * MIN, NOW - 35 * MIN),
            session("sess_e", "/e", "idle", NOW - MIN, NOW - MIN),  # just created, nothing yet: not stalled
        ]
        states = {s.run_id: s for s in hook.follow(records, sessions, NOW)}
        self.assertEqual(states["run_a"].state, "ended")
        self.assertEqual((states["run_b"].state, states["run_b"].session_id), ("running", "sess_b"))
        self.assertEqual(states["run_c"].state, "queued")
        self.assertEqual(states["run_d"].state, "stalled")
        self.assertEqual(states["run_e"].state, "running")

    def test_correction_round(self) -> None:
        sess = [session("sess_a", "/p", "completed", NOW - 60 * MIN, NOW - 30 * MIN)]
        just_sent = record("run_a", "/p", NOW - 60 * MIN, thread="sess_a", rounds=2, round_started=NOW - MIN)
        self.assertEqual(hook.follow([just_sent], sess, NOW)[0].state, "running")  # its turn starts any moment
        long_ago = record("run_a", "/p", NOW - 60 * MIN, thread="sess_a", rounds=2, round_started=NOW - 10 * MIN)
        self.assertEqual(hook.follow([long_ago], sess, NOW)[0].state, "queued")
        sess_done = [session("sess_a", "/p", "completed", NOW - 60 * MIN, NOW - MIN)]
        state = hook.follow([long_ago], sess_done, NOW)[0]
        self.assertEqual((state.state, state.key), ("ended", "sess_a:2:ended"))


class StopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "proj"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        (self.project / "AGENTS.override.md").write_text("<!-- zcrew-codex:begin policy-sha256=x -->\n",
                                                         encoding="utf-8")
        self.env = {"ZCODE_COMMANDER_STATE": str(root / "state")}
        self.t = float(NOW // 1000)
        self.sleeps = 0
        self.gated: list[str] = []
        self.timeline: list[list] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += seconds

    def runs(self, cwd, env, now_ms, cache):  # noqa: ANN001
        return self.timeline[min(self.sleeps, len(self.timeline) - 1)]

    def gate(self, run) -> dict:  # noqa: ANN001
        self.gated.append(run.run_id)
        return {"mechanical": "FAIL", "checks": [{"name": "verify", "status": "FAIL", "detail": "npm test exit 1"}]}

    def stop(self, message: str = "") -> dict | None:
        payload = {"cwd": str(self.project), "last_assistant_message": message}
        return hook.stop(payload, self.env, clock=self.clock, sleep=self.sleep, gate=self.gate, runs_fn=self.runs)

    def test_not_enabled_project_never_blocks(self) -> None:
        self.timeline = [[run_state("run_a", "ended")]]
        self.assertIsNone(hook.stop({"cwd": str(self.project.parent)}, self.env, runs_fn=self.runs))

    def test_waits_with_mark_then_wakes_with_gate(self) -> None:
        self.timeline = [[run_state("run_a", "running")], [run_state("run_a", "running")],
                         [run_state("run_a", "ended")]]
        answer = self.stop("Started run_a. [zcrew:wait]")
        self.assertEqual(answer["decision"], "block")
        self.assertIn('run_a "T run_a": completed after 10 min; gate MECHANICAL FAIL (verify: npm test exit 1)',
                      answer["reason"])
        self.assertEqual((self.sleeps, self.gated), (2, ["run_a"]))
        # reported once only
        self.assertIsNone(self.stop("[zcrew:wait]"))

    def test_without_mark_does_not_wait(self) -> None:
        self.timeline = [[run_state("run_a", "running")]]
        self.assertIsNone(self.stop("Which option do you prefer?"))
        self.assertEqual(self.sleeps, 0)

    def test_ended_run_reported_without_mark(self) -> None:
        self.timeline = [[run_state("run_a", "ended"), run_state("run_b", "running")]]
        answer = self.stop()
        self.assertIn("Still running: run_b", answer["reason"])

    def test_stalled_and_failed_runs(self) -> None:
        self.timeline = [[run_state("run_s", "stalled", "idle"), run_state("run_f", "ended", "failed")]]
        reason = self.stop()["reason"]
        self.assertIn("run_s", reason)
        self.assertIn("STALLED", reason)
        self.assertIn('run_f "T run_f": failed after 10 min; .', reason)
        self.assertEqual(self.gated, [])  # no gate for a failed or stalled run

    def test_gate_skipped_without_time_left(self) -> None:
        def slow_gate(run):  # noqa: ANN001
            self.gated.append(run.run_id)
            self.t += hook.GATE_TIMEOUT_SECONDS
            return {"mechanical": "PASS", "checks": []}

        self.gate = slow_gate
        self.timeline = [[run_state(f"run_{i}", "ended") for i in range(6)]]
        reason = self.stop()["reason"]
        self.assertEqual(len(self.gated), 4)  # 4 x 13 min fit in the hour, the rest are left to the commander
        self.assertEqual(reason.count("gate not run"), 2)

    def test_deadline_reports_still_running(self) -> None:
        self.timeline = [[run_state("run_a", "queued")]]
        answer = self.stop("[zcrew:wait]")
        self.assertIn("Still waiting on 1 worker run(s)", answer["reason"])
        self.assertEqual(self.sleeps, int(hook.WAIT_SECONDS / hook.POLL_SECONDS))

    def test_run_never_fails_codex(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = hook.run(SimpleNamespace(action="stop"), env=self.env, stdin=io.StringIO("not json"), out=out)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_current_runs_from_gate_records_and_db(self) -> None:
        fx = Fixture(Path(self._tmp.name))
        fx.session("sess_live", str(self.project), "Build it", NOW - 5 * MIN, NOW - 5 * MIN)
        fx.turn("sess_live", "t1", "completed", NOW - 5 * MIN, NOW - 4 * MIN)
        gate_dir = Path(self.env["ZCODE_COMMANDER_STATE"]) / "gate"
        gate_dir.mkdir(parents=True)
        mine = record("run_1", str(self.project), NOW - 5 * MIN - 1000)
        decided = {**record("run_0", str(self.project), NOW - 9 * MIN), "verdicts": [{"verdict": "accept"}]}
        elsewhere = record("run_x", str(Path(self._tmp.name) / "other"), NOW - 5 * MIN)
        for name, data in (("run_1", mine), ("run_0", decided), ("run_x", elsewhere)):
            (gate_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        env = {**self.env, "ZCREW_ZCODE_DB": str(fx.path)}
        runs = hook.current_runs(str(self.project), env, NOW, {})
        self.assertEqual([(r.run_id, r.session_id, r.state) for r in runs], [("run_1", "sess_live", "ended")])


class HelpersTests(unittest.TestCase):
    def test_gate_summary(self) -> None:
        self.assertEqual(hook.gate_summary(None), "gate not run (run it yourself)")
        self.assertEqual(hook.gate_summary({"mechanical": "PASS", "checks": [{"name": "tests", "status": "WARN",
                                                                             "detail": "1 skip"}]}),
                         "gate MECHANICAL PASS (tests: 1 skip)")
        self.assertEqual(hook.gate_summary({"mechanical": "PASS", "checks": []}), "gate MECHANICAL PASS")

    def test_related_folders(self) -> None:
        cache: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(hook.related(tmp, tmp, cache))
            self.assertTrue(hook.related(str(Path(tmp) / "sub"), tmp, cache))
            self.assertFalse(hook.related(None, tmp, cache))
            self.assertFalse(hook.related(str(Path(tmp).parent), tmp, cache))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.file = self.project / ".codex" / "hooks.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_uninstall_round_trip(self) -> None:
        changed, message = hook.install(self.project, command='"py" "zcrew.py" codex-hook stop')
        self.assertTrue(changed, message)
        data = json.loads(self.file.read_text(encoding="utf-8"))
        [entry] = data["hooks"]["Stop"][0]["hooks"]
        self.assertEqual((entry["type"], entry["timeout"]), ("command", 3600))
        self.assertFalse(hook.install(self.project, command='"py" "zcrew.py" codex-hook stop')[0])
        self.assertTrue(hook.uninstall(self.project)[0])
        self.assertFalse(self.file.exists())
        self.assertFalse(self.file.parent.exists())
        self.assertFalse(hook.uninstall(self.project)[0])

    def test_keeps_other_hooks(self) -> None:
        other = {"type": "command", "command": "lint-on-stop"}
        self.file.parent.mkdir()
        self.file.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [other]}],
                                                   "SessionStart": [{"hooks": [other]}]}}), encoding="utf-8")
        hook.install(self.project, command="old codex-hook stop")
        hook.install(self.project, command="new codex-hook stop")  # replaces, never duplicates
        data = json.loads(self.file.read_text(encoding="utf-8"))
        commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(commands, ["lint-on-stop", "new codex-hook stop"])
        hook.uninstall(self.project)
        data = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(data, {"hooks": {"Stop": [{"hooks": [other]}], "SessionStart": [{"hooks": [other]}]}})

    def test_bad_file_is_left_alone(self) -> None:
        self.file.parent.mkdir()
        self.file.write_text("{oops", encoding="utf-8")
        changed, message = hook.install(self.project)
        self.assertFalse(changed)
        self.assertIn("cannot read", message)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "{oops")


if __name__ == "__main__":
    unittest.main()
