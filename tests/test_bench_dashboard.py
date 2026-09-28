from __future__ import annotations

import time
import unittest

import bench_dashboard

ENDPOINTS = {"sessions_limit_100", "activity_full", "activity_incremental_empty", "quota", "page"}


class BenchDashboardTest(unittest.TestCase):
    def test_small_run_reports_all_four_endpoints(self) -> None:
        started = time.monotonic()
        report = bench_dashboard.run_bench(n=5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(report["n"], 5)
        self.assertEqual(set(report["endpoints"]), ENDPOINTS)
        for name, row in report["endpoints"].items():
            self.assertGreater(row["response_bytes"], 0, name)
            self.assertLessEqual(row["p50_ms"], row["p95_ms"], name)
            self.assertLessEqual(row["p95_ms"], row["max_ms"], name)

    def test_pct_is_nearest_rank_on_sorted_samples(self) -> None:
        samples = [1.0] * 94 + [5.0] * 5 + [9.0]
        self.assertEqual(bench_dashboard._pct(samples, 50), 1.0)
        self.assertEqual(bench_dashboard._pct(samples, 95), 5.0)
        self.assertEqual(bench_dashboard._pct(samples, 100), 9.0)


if __name__ == "__main__":
    unittest.main()
