"""Machine-wide worker slots and limit-error classification (bridge_compat)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_test_slots_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = _load("bridge_compat")
NOW = 1_800_000_000_000


class SharedLeases:
    """The lease db every bridge process shares (upstream ResourceLeaseStore semantics:
    all-or-nothing, exclusive conflicts with anything, own rows never block)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], str] = {}  # (owner, run, key) -> mode


def store_class():
    class Store:
        def __init__(self, shared: SharedLeases, owner: str) -> None:
            self.shared, self.owner = shared, owner

        def try_acquire(self, run_id, resource_modes):
            blockers = [
                {"ownerId": o, "runId": r, "key": k, "mode": m}
                for (o, r, k), m in self.shared.rows.items()
                if k in resource_modes and (o, r) != (self.owner, run_id)
                and "exclusive" in {m, resource_modes[k]}
            ]
            if blockers:
                return {"acquired": False, "blockers": blockers}
            for key, mode in resource_modes.items():
                self.shared.rows[(self.owner, run_id, key)] = mode
            return {"acquired": True, "blockers": []}

        def release(self, run_id):
            for key in [k for k in self.shared.rows if k[:2] == (self.owner, run_id)]:
                del self.shared.rows[key]

    return Store


class WorkerSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cap = 2
        self.Store = store_class()
        compat.patch_worker_slots(self.Store, {}, cap_fn=lambda env: {"cap": self.cap}, clock=self.clock)
        self.now = 0.0
        self.shared = SharedLeases()

    def clock(self) -> float:
        return self.now

    def test_cap_holds_across_bridge_processes(self) -> None:
        a, b = self.Store(self.shared, "bridge_a"), self.Store(self.shared, "bridge_b")
        self.assertTrue(a.try_acquire("r1", {"/repo/one": "exclusive"})["acquired"])
        self.assertTrue(b.try_acquire("r2", {"/repo/two": "exclusive"})["acquired"])
        third = b.try_acquire("r3", {"/repo/three": "exclusive"})
        self.assertFalse(third["acquired"])
        self.assertTrue(all(x["key"].startswith(compat.SLOT_PREFIX) for x in third["blockers"]))
        a.release("r1")
        self.assertTrue(b.try_acquire("r3", {"/repo/three": "exclusive"})["acquired"])
        held = sorted(k for (_, r, k) in self.shared.rows if k.startswith(compat.SLOT_PREFIX))
        self.assertEqual(held, [compat.SLOT_PREFIX + "0", compat.SLOT_PREFIX + "1"])

    def test_resource_blocker_is_reported_as_is(self) -> None:
        a, b = self.Store(self.shared, "bridge_a"), self.Store(self.shared, "bridge_b")
        a.try_acquire("r1", {"/repo": "exclusive"})
        busy = b.try_acquire("r2", {"/repo": "exclusive"})
        self.assertFalse(busy["acquired"])
        self.assertEqual([x["key"] for x in busy["blockers"]], ["/repo"])

    def test_empty_resources_still_take_a_slot(self) -> None:
        a = self.Store(self.shared, "bridge_a")
        self.assertTrue(a.try_acquire("r1", {})["acquired"])
        self.assertTrue(a.try_acquire("r2", None)["acquired"])
        self.assertFalse(a.try_acquire("r3", {})["acquired"])

    def test_cap_change_applies_after_cache(self) -> None:
        a = self.Store(self.shared, "bridge_a")
        a.try_acquire("r1", {})
        a.try_acquire("r2", {})
        self.cap = 3
        self.assertFalse(a.try_acquire("r3", {})["acquired"])  # cached cap 2
        self.now += compat.CAP_CACHE_SECONDS
        self.assertTrue(a.try_acquire("r3", {})["acquired"])

    def test_patch_is_idempotent_and_failsafe(self) -> None:
        Store = store_class()
        compat.patch_worker_slots(Store, {}, cap_fn=lambda env: 1 / 0)
        first = Store.try_acquire
        compat.patch_worker_slots(Store, {})
        self.assertIs(Store.try_acquire, first)
        store = Store(SharedLeases(), "o")
        default = compat.CONCURRENCY_SETTINGS["maxWorkers"][0]
        for i in range(default):
            self.assertTrue(store.try_acquire(f"r{i}", {})["acquired"])
        self.assertFalse(store.try_acquire("over", {})["acquired"])


class CapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.env = {"HOME": str(self.home), "USERPROFILE": str(self.home),
                    "ZCREW_ZCODE_DB": str(self.home / "db.sqlite")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, **values: object) -> None:
        folder = self.home / ".zcode-commander"
        folder.mkdir(exist_ok=True)
        (folder / "config.json").write_text(json.dumps(values), encoding="utf-8")

    def usage_rows(self, rows: list[tuple[int, str | None, str | None]]) -> None:
        conn = sqlite3.connect(self.home / "db.sqlite")
        conn.execute("CREATE TABLE model_usage (id TEXT, started_at INTEGER, error_type TEXT, error_message TEXT)")
        conn.executemany("INSERT INTO model_usage VALUES (?,?,?,?)",
                         [(str(i), at, t, m) for i, (at, t, m) in enumerate(rows)])
        conn.commit()
        conn.close()

    def test_classify(self) -> None:
        c = compat.classify_limit_error
        self.assertEqual(c("rate_limited", "[1302][Rate limit reached for requests][x]"), "concurrency")
        self.assertEqual(c(None, "High concurrency usage of this API, please reduce concurrency"), "concurrency")
        self.assertEqual(c("rate_limited", "[1308][Usage limit reached for 5 hour. Your limit will reset at "
                                           "2026-09-25 19:32:20][id]"), "usage")
        self.assertEqual(c("rate_limited", "[1313] fair use"), "usage")
        self.assertEqual(c("server", "[1305] The service may be temporarily overloaded"), "overload")
        self.assertEqual(c("rate_limited", "429"), "rate_limited")
        self.assertIsNone(c("network", "socket hang up"))
        self.assertIsNone(c(None, None))

    def test_settings_and_configured_cap(self) -> None:
        self.assertEqual(compat.concurrency_settings(self.env),
                         {"maxWorkers": 5, "concurrencyLimit": 0, "concurrencyTarget": 80})
        self.config(maxWorkers=10, concurrencyLimit=3, concurrencyTarget=80)
        settings = compat.concurrency_settings(self.env)
        self.assertEqual(compat.configured_cap(settings), 2)  # 80% of 3, rounded down
        self.config(maxWorkers=2, concurrencyLimit=10)
        self.assertEqual(compat.configured_cap(compat.concurrency_settings(self.env)), 2)  # maxWorkers wins
        self.config(concurrencyLimit=1, concurrencyTarget=50)
        self.assertEqual(compat.configured_cap(compat.concurrency_settings(self.env)), 1)  # never below 1
        self.config(maxWorkers=99, concurrencyLimit="3", concurrencyTarget=True)
        self.assertEqual(compat.concurrency_settings(self.env),
                         {"maxWorkers": 5, "concurrencyLimit": 0, "concurrencyTarget": 80})

    def test_backoff_after_recent_1302(self) -> None:
        self.config(concurrencyLimit=5)  # cap 4
        self.usage_rows([
            (NOW - 20 * 60_000, "rate_limited", "[1302][Rate limit reached for requests]"),  # too old
            (NOW - 60_000, None, None),
        ])
        cap = compat.worker_cap(self.env, now_ms=NOW)
        self.assertEqual((cap["cap"], cap["configured"], cap["backoff"]), (4, 4, False))

    def test_backoff_lowers_by_one(self) -> None:
        self.config(concurrencyLimit=5)
        self.usage_rows([(NOW - 60_000, "rate_limited", "[1302][Rate limit reached for requests]"),
                         (NOW - 30_000, "rate_limited", "[1308][Usage limit reached for 5 hour.]")])
        self.assertEqual(compat.recent_limit_errors(self.home / "db.sqlite", NOW - 600_000),
                         {"concurrency": 1, "usage": 1})
        cap = compat.worker_cap(self.env, now_ms=NOW)
        self.assertEqual((cap["cap"], cap["backoff"]), (3, True))

    def test_no_db_means_no_backoff(self) -> None:
        self.assertEqual(compat.recent_limit_errors(self.home / "missing.sqlite", 0), {})
        self.assertEqual(compat.worker_cap(self.env, now_ms=NOW)["cap"], 5)

    def test_flag(self) -> None:
        self.assertTrue(compat.worker_slots_enabled({}))
        self.assertFalse(compat.worker_slots_enabled({"ZCODE_COMMANDER_WORKER_SLOTS": "off"}))


if __name__ == "__main__":
    unittest.main()
