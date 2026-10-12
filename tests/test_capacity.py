"""zcrew capacity: concurrency limit from 1302 errors, cap, --apply."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_zcode_activity import Fixture, _load

cap = _load("capacity")
MIN = 60_000
NOW = 1_800_000_000_000


def args(**over: object) -> SimpleNamespace:
    base: dict[str, object] = dict(days=14.0, apply=False, limit=None, target=None, json=False)
    base.update(over)
    return SimpleNamespace(**base)


class AnalyzeTests(unittest.TestCase):
    def test_busy_profile(self) -> None:
        turns = [(0, 10 * MIN), (5 * MIN, 15 * MIN)]
        busy = cap.busy_profile(turns, cap=2)
        self.assertEqual(busy["peak"], 2)
        self.assertEqual(busy["avg"], 1.33)  # 20 worker-minutes over 15 busy minutes
        self.assertEqual(busy["at_cap_share"], 0.333)
        self.assertEqual(cap.busy_profile([], cap=2), {"avg": None, "peak": 0, "at_cap_share": None})

    def test_estimate_from_1302(self) -> None:
        turns = [(0, 60 * MIN), (0, 60 * MIN), (10 * MIN, 60 * MIN), (20 * MIN, 60 * MIN)]
        errors = [(15 * MIN, "concurrency"), (25 * MIN, "concurrency"), (30 * MIN, "usage")]
        a = cap.analyze(turns, errors, cap=5)
        self.assertEqual(a["errors"], {"concurrency": 2, "usage": 1})
        self.assertEqual(a["busy_at_1302"], {"min": 3, "max": 4})
        self.assertEqual(a["estimate"], 2)  # 1302 came with 3 busy
        # 4 ran cleanly at 20m, 5 min from either hit: the limit moves with load, and
        # the estimate keeps the lowest count that failed
        self.assertEqual(a["clean_peak"], 4)
        self.assertEqual(cap.analyze(turns[:1], [(5, "concurrency")], cap=1)["estimate"], 1)  # never below 1

    def test_no_1302_gives_only_a_floor(self) -> None:
        a = cap.analyze([(0, 10 * MIN), (MIN, 9 * MIN)], [(MIN, "overload")], cap=5)
        self.assertIsNone(a["estimate"])
        self.assertEqual(a["clean_peak"], 2)

    def test_target_cap(self) -> None:
        self.assertEqual(cap.target_cap(3, 80, 10), 2)
        self.assertEqual(cap.target_cap(10, 80, 5), 5)
        self.assertEqual(cap.target_cap(1, 50, 5), 1)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.fx = Fixture(root)
        conn = sqlite3.connect(self.fx.path)
        conn.execute("CREATE TABLE model_usage (id TEXT, started_at INTEGER, provider_id TEXT, model_id TEXT,"
                     " status TEXT, computed_total_tokens INTEGER, error_type TEXT, error_message TEXT)")
        conn.commit()
        conn.close()
        self.home = root / "home"
        self.home.mkdir()
        self.env = {"ZCREW_ZCODE_DB": str(self.fx.path), "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "AGENT_MCP_LEASE_DB": str(root / "leases.sqlite")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def worker(self, sid: str, start: int, end: int, parent: str | None = None) -> None:
        self.fx.session(sid, r"C:\Work", sid, start, end)
        if parent:
            self.fx.exec("UPDATE session SET parent_id = ? WHERE id = ?", (parent, sid))
        self.fx.turn(sid, "t1", "completed", start, end)

    def usage_error(self, at: int, message: str) -> None:
        self.fx.exec("INSERT INTO model_usage (id, started_at, error_type, error_message) VALUES (?,?,?,?)",
                     (f"u{at}", at, "rate_limited", message))

    def run_cli(self, **over: object) -> tuple[int, str]:
        out = io.StringIO()
        code = cap.run(args(**over), env=self.env, out=out, now_ms=NOW)
        return code, out.getvalue()

    def seed_hit(self) -> None:
        for i in range(4):
            self.worker(f"sess_w{i}", NOW - 60 * MIN + i * MIN, NOW - 5 * MIN)
        self.worker("sess_sub", NOW - 59 * MIN, NOW - 5 * MIN, parent="sess_w0")  # sub-agent: not a worker
        self.usage_error(NOW - 50 * MIN, "[1302][Rate limit reached for requests][id]")

    def test_report_with_measured_limit(self) -> None:
        self.seed_hit()
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertIn("limit errors     1302 concurrency 1", out)
        self.assertIn("at each 1302     4-4 workers busy", out)
        self.assertIn("limit estimate   about 3 at once", out)
        self.assertIn("concurrencyLimit not set", out)
        self.assertIn("cap now          5 worker(s) at once", out)  # the 1302 is older than 10 min
        self.assertIn("zcrew capacity --apply  (concurrencyLimit 3 -> cap 2)", out)

    def test_apply_writes_user_config(self) -> None:
        self.seed_hit()
        config = self.home / ".zcode-commander" / "config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"thoughtLevel": "high"}), encoding="utf-8")
        code, out = self.run_cli(apply=True)
        self.assertEqual(code, 0, out)
        self.assertIn("set concurrencyLimit 3", out)
        self.assertIn("up to 2 worker(s) at once", out)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")),
                         {"thoughtLevel": "high", "concurrencyLimit": 3})
        _, out = self.run_cli()
        self.assertIn("config matches the measurement; cap 2 = 80% of 3", out)

    def test_apply_needs_a_limit(self) -> None:
        code, out = self.run_cli(apply=True)
        self.assertEqual(code, 1)
        self.assertIn("pass --limit N", out)
        code, out = self.run_cli(apply=True, limit=5, target=60)
        self.assertEqual(code, 0, out)
        self.assertIn("up to 3 worker(s) at once", out)
        code, out = self.run_cli(apply=True, limit=0)
        self.assertEqual(code, 1)
        code, out = self.run_cli(apply=True, limit=3, target=20)
        self.assertEqual(code, 1)
        self.assertIn("--target must be from 50 to 100", out)

    def test_unmeasured_advice_and_json(self) -> None:
        self.worker("sess_one", NOW - 30 * MIN, NOW - 20 * MIN)
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("limit errors     none", out)
        self.assertIn("not reached yet; at least 1 ran without a 1302", out)
        self.assertIn("about 3 sessions at once", out)
        code, out = self.run_cli(json=True)
        data = json.loads(out)
        self.assertIsNone(data["analysis"]["estimate"])
        self.assertEqual(data["cap"], {"cap": 5, "configured": 5, "backoff": False})
        self.assertIsNone(data["slots_in_use"])

    def test_slots_in_use(self) -> None:
        conn = sqlite3.connect(self.env["AGENT_MCP_LEASE_DB"])
        conn.execute("CREATE TABLE resource_lease (owner_id TEXT, run_id TEXT, resource_key TEXT, mode TEXT,"
                     " acquired_ms INTEGER, updated_ms INTEGER)")
        conn.executemany("INSERT INTO resource_lease VALUES (?,?,?,?,0,0)", [
            ("o1", "r1", "zcrew:worker-slot:0", "exclusive"), ("o1", "r1", "/repo", "exclusive"),
            ("o2", "r2", "zcrew:worker-slot:1", "exclusive"),
        ])
        conn.commit()
        conn.close()
        _, out = self.run_cli()
        self.assertIn("· 2 running", out)

    def test_missing_db(self) -> None:
        self.env["ZCREW_ZCODE_DB"] = str(self.home / "nope.sqlite")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("ZCode database not found", out)


if __name__ == "__main__":
    unittest.main()
