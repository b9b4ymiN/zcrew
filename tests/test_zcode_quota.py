from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from test_zcode_activity import Fixture

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# A stand-in API key: if the server ever leaks the key into a response body,
# an error text or stderr, this string shows up and the tests below fail.
SENTINEL = "sk-SENTINEL-QUOTA-KEY-91c4e7"

# The real rate-limit message shape, verified against a live ZCode database.
REAL_LIMIT_MESSAGE = (
    "[1308][Usage limit reached for 5 hour. Your limit will reset at 2026-09-25 19:32:20]"
    "[2026092518180740fc30d4564a4d40]"
)
REAL_LIMIT_RESET_MS = 1_790_335_940_000  # 2026-09-25T11:32:20Z == 19:32:20 Beijing

NOW = 1_800_000_000_000  # 2027-01-15T08:00:00Z, an arbitrary fixed "now"

MODEL_USAGE_SQL = """
CREATE TABLE model_usage (id TEXT PRIMARY KEY, started_at INTEGER, provider_id TEXT,
    model_id TEXT, status TEXT, computed_total_tokens INTEGER, error_type TEXT,
    error_message TEXT);
CREATE INDEX model_usage_started_model_idx ON model_usage (started_at, provider_id, model_id);
"""

ENVELOPE = {
    "code": 200,
    "success": True,
    "data": {
        "level": "Individual",
        "limits": [
            {"type": "TIME_LIMIT", "percentage": 10, "nextResetTime": 1_790_400_000_000},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 100000, "usage": 40000,
             "percentage": 40},
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 32000,
             "currentValue": 32000, "remaining": 68000, "percentage": 32,
             "nextResetTime": REAL_LIMIT_RESET_MS},
            {"type": "WEIRD_LIMIT", "percentage": 1},
            {"unit": 3, "percentage": 50},      # no type: ignored
            {"type": "", "percentage": 9},      # empty type: ignored
        ],
    },
}


def _load(name: str) -> ModuleType:
    mod_name = f"_script_{name}"
    module = sys.modules.get(mod_name)  # reuse the instance other test files loaded
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


zq = _load("zcode_quota")
dash = _load("dashboard")


def beijing_ms(*args: int) -> int:
    dt = datetime(*args, tzinfo=timezone(timedelta(hours=8)))
    return int(dt.timestamp() * 1000)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        pass


class FakeOpener:
    """Stand-in urlopen: counts calls/concurrency, returns or raises per setup."""

    def __init__(self, body: bytes = b"", error: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.body = body
        self.error = error
        self.delay = delay
        self.calls = 0
        self.max_concurrent = 0
        self._live = 0
        self._lock = threading.Lock()
        self.requests: list[Any] = []

    def __call__(self, req: Any, timeout: float | None = None) -> FakeResponse:
        with self._lock:
            self.calls += 1
            self._live += 1
            self.max_concurrent = max(self.max_concurrent, self._live)
            self.requests.append(req)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return FakeResponse(self.body)
        finally:
            with self._lock:
                self._live -= 1


def envelope_body(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


class BombedOpener:
    def __call__(self, req: Any, timeout: float | None = None) -> FakeResponse:
        raise AssertionError("network call attempted although no API key is configured")


class QuotaServer:
    """A dashboard server with an injectable quota opener, ephemeral port."""

    def __init__(self, env: dict[str, str]) -> None:
        self.state = dash.DashboardState(env, None)
        self.httpd = dash.DashboardServer(("127.0.0.1", 0), dash.DashboardHandler, self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def get_json(self, path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()


class QuotaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.db = self.fx.path
        self._servers: list[QuotaServer] = []
        self._n = 0

    def tearDown(self) -> None:
        for srv in self._servers:
            srv.close()
        self._tmp.cleanup()

    def add_model_usage(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.executescript(MODEL_USAGE_SQL)
        finally:
            conn.close()

    def mu(self, started_at: int, provider: str = "account:zai-individual-coding-plan",
           tokens: int | None = 0, status: str = "completed", error_type: str | None = None,
           error_message: str | None = None) -> None:
        self._n += 1
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO model_usage (id, started_at, provider_id, status,"
                    " computed_total_tokens, error_type, error_message) VALUES (?,?,?,?,?,?,?)",
                    (f"mu_{self._n:04d}", started_at, provider, status, tokens,
                     error_type, error_message))
        finally:
            conn.close()

    def serve(self, extra_env: dict[str, str] | None = None,
              opener: Any | None = None) -> QuotaServer:
        env = {"ZCREW_ZCODE_DB": str(self.db)}
        env.update(extra_env or {})
        srv = QuotaServer(env)
        srv.state.quota.opener = opener
        self._servers.append(srv)
        return srv

    def get_quota(self, srv: QuotaServer) -> tuple[int, dict, str]:
        """GET /api/quota with stderr captured, so tests can assert key absence."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, data = srv.get_json("/api/quota")
        return status, data, err.getvalue()


# --- pure parsing -------------------------------------------------------------------------


class ParseEnvelopeTest(QuotaTestCase):
    def test_windows_tools_and_level_from_mixed_limits(self) -> None:
        out = zq.parse_quota_envelope(ENVELOPE, NOW)
        self.assertEqual(out["level"], "Individual")
        self.assertEqual([w["id"] for w in out["windows"]], ["5h", "week", "other"])
        five, week = out["windows"][0], out["windows"][1]
        self.assertEqual(five["used_pct"], 32.0)
        self.assertEqual(five["remaining_pct"], 68.0)
        self.assertEqual(five["reset_ms"], REAL_LIMIT_RESET_MS)
        self.assertEqual(five["used"], 32000)
        self.assertEqual(five["total"], 5)
        self.assertEqual(week["used_pct"], 40.0)
        self.assertEqual(week["remaining_pct"], 60.0)
        self.assertIsNone(week["reset_ms"])  # nextResetTime missing
        self.assertEqual(week["used"], 40000)
        self.assertEqual(week["total"], 100000)
        other = out["windows"][2]
        self.assertEqual((other["used_pct"], other["remaining_pct"]), (1.0, 99.0))
        self.assertIsNone(other["used"])
        self.assertEqual(out["tools"], {"remaining_pct": 90.0, "reset_ms": 1_790_400_000_000})

    def test_percentage_is_used_share_and_clamps(self) -> None:
        out = zq.parse_quota_envelope({"data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 120},
            {"type": "TOKENS_LIMIT", "unit": 6, "percentage": -5},
        ]}}, NOW)
        self.assertEqual([w["remaining_pct"] for w in out["windows"]], [0.0, 100.0])
        self.assertIsNone(out["tools"])

    def test_invalid_envelopes_raise_quota_error(self) -> None:
        for bad in ([], "nope", None, {"code": 500, "msg": "boom"}, {"success": False},
                    {"msg": "no data"}, {"data": "x"}, {"data": None}):
            with self.subTest(bad=bad):
                with self.assertRaises(zq.QuotaError) as cm:
                    zq.parse_quota_envelope(bad, NOW)
                self.assertEqual(str(cm.exception), "unexpected response")

    def test_code_and_success_optional_when_data_present(self) -> None:
        out = zq.parse_quota_envelope({"data": {"level": "Lite", "limits": []}}, NOW)
        self.assertEqual(out, {"level": "Lite", "windows": [], "tools": None})


class ParseLimitMessageTest(QuotaTestCase):
    def test_real_message_parses_beijing_time(self) -> None:
        self.assertEqual(zq.parse_limit_message(REAL_LIMIT_MESSAGE), REAL_LIMIT_RESET_MS)

    def test_non_matching_and_empty_messages(self) -> None:
        self.assertIsNone(zq.parse_limit_message("Usage limit reached for 5 hour. Soon."))
        self.assertIsNone(zq.parse_limit_message(""))
        self.assertIsNone(zq.parse_limit_message(None))


# --- configuration ------------------------------------------------------------------------


class ConfigTest(QuotaTestCase):
    def test_key_envs_in_order_and_empty_means_absent(self) -> None:
        self.assertIsNone(zq.api_key({}))
        self.assertIsNone(zq.api_key({"ZCODE_BIGMODEL_USAGE_API_KEY": "  "}))
        self.assertEqual(zq.api_key({"BIGMODEL_USAGE_API_KEY": "k2"}), "k2")
        self.assertEqual(zq.api_key({"ZCODE_BIGMODEL_USAGE_API_KEY": "k1",
                                     "BIGMODEL_USAGE_API_KEY": "k2"}), "k1")

    def test_url_envs_and_default(self) -> None:
        self.assertEqual(zq.quota_url({}), zq.DEFAULT_QUOTA_URL)
        self.assertEqual(zq.quota_url({"BIGMODEL_USAGE_QUOTA_URL": "https://a"}),
                         "https://a")
        self.assertEqual(zq.quota_url({"ZCODE_BIGMODEL_USAGE_QUOTA_URL": "https://b",
                                       "BIGMODEL_USAGE_QUOTA_URL": "https://a"}),
                         "https://b")


class UrlAllowedTest(QuotaTestCase):
    def test_https_always_http_loopback_only(self) -> None:
        allowed = [
            "https://api.z.ai/api/monitor/usage/quota/limit",
            "https://127.0.0.1:9/x",
            "http://127.0.0.1:8765/q",
            "http://localhost:9/q",
            "http://[::1]:9/q",
        ]
        refused = [
            "http://example.com/q",
            "http://127.0.0.1.evil.com/q",
            "ftp://127.0.0.1/x",
            "//example.com/x",
            "example.com",
            "",
        ]
        for url in allowed:
            with self.subTest(url=url):
                self.assertTrue(zq.url_allowed(url))
        for url in refused:
            with self.subTest(url=url):
                self.assertFalse(zq.url_allowed(url))


# --- fetch_api ----------------------------------------------------------------------------


class FetchApiTest(QuotaTestCase):
    KEY_ENV = {"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}

    def test_success_returns_parsed_pieces_and_sends_raw_key(self) -> None:
        opener = FakeOpener(envelope_body(ENVELOPE))
        out = zq.fetch_api(self.KEY_ENV, opener=opener)
        self.assertEqual(out["level"], "Individual")
        self.assertEqual(opener.calls, 1)
        req = opener.requests[0]
        self.assertEqual(req.get_header("Authorization"), SENTINEL)  # no "Bearer "
        self.assertNotIn(SENTINEL, json.dumps(out))

    def test_url_override_and_timeout_forwarded(self) -> None:
        opener = FakeOpener(envelope_body(ENVELOPE))
        env = {**self.KEY_ENV, "ZCODE_BIGMODEL_USAGE_QUOTA_URL": "https://quota.example/x"}
        captured: dict[str, Any] = {}

        def capturing(req: Any, timeout: float | None = None) -> FakeResponse:
            captured["url"], captured["timeout"] = req.full_url, timeout
            return FakeOpener(envelope_body(ENVELOPE))(req, timeout)

        zq.fetch_api(env, timeout=3.25, opener=capturing)
        self.assertEqual(captured["url"], "https://quota.example/x")
        self.assertEqual(captured["timeout"], 3.25)

    def test_no_key_never_touches_network(self) -> None:
        with self.assertRaises(zq.QuotaError) as cm:
            zq.fetch_api({}, opener=BombedOpener())
        self.assertEqual(str(cm.exception), "no api key")

    def test_non_https_non_loopback_url_refused_before_network(self) -> None:
        for url in ("http://example.com/q", "ftp://127.0.0.1/x"):
            with self.subTest(url=url):
                opener = FakeOpener(envelope_body(ENVELOPE))
                env = {**self.KEY_ENV, "ZCODE_BIGMODEL_USAGE_QUOTA_URL": url}
                with self.assertRaises(zq.QuotaError) as cm:
                    zq.fetch_api(env, opener=opener)
                self.assertEqual(str(cm.exception), "insecure quota url")
                self.assertEqual(opener.calls, 0)

    def test_failures_raise_generic_key_free_errors(self) -> None:
        cases = [
            (urllib.error.HTTPError("https://q", 401, "Unauthorized", None, None), "HTTP 401"),
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
            (urllib.error.URLError("connection refused"), "network error"),
            (OSError("unreachable"), "network error"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(zq.QuotaError) as cm:
                    zq.fetch_api(self.KEY_ENV, opener=FakeOpener(error=error))
                self.assertEqual(str(cm.exception), expected)
                self.assertNotIn(SENTINEL, str(cm.exception))

    def test_bad_bodies_raise_unexpected_response(self) -> None:
        for body in (b"<html>denied</html>", b"", b'{"code": 200, "success": false}'):
            with self.subTest(body=body):
                with self.assertRaises(zq.QuotaError) as cm:
                    zq.fetch_api(self.KEY_ENV, opener=FakeOpener(body))
                self.assertEqual(str(cm.exception), "unexpected response")

    def test_http_status_and_chaining_never_leak_key(self) -> None:
        error = urllib.error.HTTPError("https://q", 403, "Forbidden", None, None)
        with self.assertRaises(zq.QuotaError) as cm:
            zq.fetch_api(self.KEY_ENV, opener=FakeOpener(error=error))
        self.assertIsNone(cm.exception.__cause__)  # no chained traceback to print


class RedirectTest(QuotaTestCase):
    """A local http.server answering 302 -> /target: the redirect must be
    refused before urllib follows it, and /target must never see a request."""

    KEY_ENV: dict[str, str] = {"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}

    @staticmethod
    def make_redirect_server() -> tuple[http.server.HTTPServer, dict[str, int]]:
        hits = {"target": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                hits["target"] += 1
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                pass  # keep captured stderr empty for the key-leak assertions

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, hits

    def test_302_refused_and_target_never_hit(self) -> None:
        server, hits = self.make_redirect_server()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/redirect"
            env = {**self.KEY_ENV, "ZCODE_BIGMODEL_USAGE_QUOTA_URL": url}
            with self.assertRaises(zq.QuotaError) as cm:
                zq.fetch_api(env)  # opener=None: the real no-redirect opener
            self.assertEqual(str(cm.exception), "unexpected redirect")
            self.assertEqual(hits["target"], 0)

            srv = self.serve(dict(env))  # endpoint level, same real opener
            status, data, err = self.get_quota(srv)
            self.assertEqual(status, 200)
            self.assertEqual(data["source"], "estimate")
            self.assertEqual(data["api"], {"configured": True, "error": "unexpected redirect"})
            self.assertEqual(hits["target"], 0)
            self.assertNotIn(SENTINEL, json.dumps(data))
            self.assertNotIn(SENTINEL, err)
        finally:
            server.shutdown()
            server.server_close()


# --- local estimate -----------------------------------------------------------------------


class LocalEstimateTest(QuotaTestCase):
    def test_sums_last_5h_for_plan_providers_only(self) -> None:
        self.add_model_usage()
        self.mu(NOW - 1000, tokens=1000)
        self.mu(NOW - 2000, provider="builtin:zai-coding-plan", tokens=250)
        self.mu(NOW - 2000, provider="openai", tokens=5555)          # other provider
        self.mu(NOW - zq.WINDOW_5H_MS - 1, tokens=999_999)           # just outside
        self.mu(NOW - 3000, tokens=None)                             # in window, no tokens
        out = zq.local_estimate(self.db, NOW)
        self.assertEqual(out, {"tokens_5h": 1250, "requests_5h": 3,
                               "limited": False, "limit_reset_ms": None})

    def test_newest_rate_limited_row_wins_and_limited_flag(self) -> None:
        self.add_model_usage()
        # An older row with a much later reset must not beat the newest row.
        self.mu(NOW - 900_000, status="error", error_type="rate_limited",
                error_message="[x][Usage limit reached for 5 hour. "
                              "Your limit will reset at 2027-01-20 00:00:00][y]")
        self.mu(NOW - 600_000, status="error", error_type="rate_limited",
                error_message=REAL_LIMIT_MESSAGE)  # newest row wins
        now = beijing_ms(2026, 9, 25, 12, 0, 0)  # 19:32:20 Beijing still lies ahead
        out = zq.local_estimate(self.db, now)
        self.assertEqual(out["limit_reset_ms"], REAL_LIMIT_RESET_MS)
        self.assertTrue(out["limited"])
        # The same reset, once passed, no longer counts as limited.
        out = zq.local_estimate(self.db, beijing_ms(2026, 9, 25, 21, 0, 0))
        self.assertEqual(out["limit_reset_ms"], REAL_LIMIT_RESET_MS)
        self.assertFalse(out["limited"])

    def test_unparseable_newest_falls_through_to_older_row(self) -> None:
        self.add_model_usage()
        older_reset = beijing_ms(2027, 1, 16, 0, 0, 0)  # 8 h after NOW: limited
        self.mu(NOW - 900_000, status="error", error_type="rate_limited",
                error_message=f"[x][Usage limit reached for 5 hour. "
                              f"Your limit will reset at 2027-01-16 00:00:00][y]")
        self.mu(NOW - 600_000, status="error", error_type="rate_limited",
                error_message="something else entirely")  # newest, unparseable
        out = zq.local_estimate(self.db, NOW)
        self.assertEqual(out["limit_reset_ms"], older_reset)
        self.assertTrue(out["limited"])

    def test_missing_table_and_missing_db_give_zeros(self) -> None:
        self.assertEqual(zq.local_estimate(self.db, NOW),
                         {"tokens_5h": 0, "requests_5h": 0, "limited": False,
                          "limit_reset_ms": None})
        missing = Path(self._tmp.name) / "nope" / "db.sqlite"
        self.assertEqual(zq.local_estimate(missing, NOW)["requests_5h"], 0)
        self.assertFalse(missing.exists())


# --- endpoint -----------------------------------------------------------------------------


class QuotaEndpointTest(QuotaTestCase):
    def test_no_key_is_estimate_and_never_calls_network(self) -> None:
        self.add_model_usage()
        t0 = int(time.time() * 1000)
        self.mu(t0 - 2000, tokens=4200)
        self.mu(t0 - 1000, tokens=800)
        srv = self.serve(opener=BombedOpener())
        status, data, err = self.get_quota(srv)
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "estimate")
        self.assertEqual(data["api"], {"configured": False, "error": None})
        self.assertIsNone(data["level"])
        self.assertIsNone(data["tools"])
        self.assertEqual(data["windows"], [{"id": "5h", "remaining_pct": None,
                                            "used_pct": None, "reset_ms": None,
                                            "used": 5000, "total": None}])
        self.assertEqual(data["estimate"], {"tokens_5h": 5000, "requests_5h": 2,
                                            "limited": False, "limit_reset_ms": None})
        self.assertIsInstance(data["fetched_ms"], int)
        self.assertEqual(err, "")

    def test_key_success_serves_api_source_alongside_estimate(self) -> None:
        self.add_model_usage()
        self.mu(int(time.time() * 1000) - 1000, tokens=4200)
        opener = FakeOpener(envelope_body(ENVELOPE))
        srv = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=opener)
        status, data, err = self.get_quota(srv)
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "api")
        self.assertEqual(data["api"], {"configured": True, "error": None})
        self.assertEqual(data["level"], "Individual")
        self.assertEqual([w["id"] for w in data["windows"]], ["5h", "week", "other"])
        self.assertEqual(data["tools"]["remaining_pct"], 90.0)
        self.assertEqual(data["estimate"]["tokens_5h"], 4200)
        self.assertEqual(opener.requests[0].get_header("Authorization"), SENTINEL)
        self.assertNotIn(SENTINEL, json.dumps(data))
        self.assertNotIn(SENTINEL, err)

    def test_key_failures_fall_back_to_estimate_with_generic_error(self) -> None:
        self.add_model_usage()
        trap = {"code": 200, "success": False, "msg": "bad key " + SENTINEL}
        cases = [
            (FakeOpener(error=urllib.error.HTTPError("https://q", 401, "x", None, None)),
             "HTTP 401"),
            (FakeOpener(error=TimeoutError("timed out")), "timeout"),
            (FakeOpener(error=urllib.error.URLError("refused")), "network error"),
            (FakeOpener(b"<html>"), "unexpected response"),
            (FakeOpener(envelope_body(trap)), "unexpected response"),
        ]
        for opener, expected in cases:
            with self.subTest(expected=expected):
                srv = self.serve({"BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=opener)
                status, data, err = self.get_quota(srv)
                self.assertEqual(status, 200)
                self.assertEqual(data["source"], "estimate")
                self.assertEqual(data["api"], {"configured": True, "error": expected})
                self.assertEqual(data["estimate"]["requests_5h"], 0)
                self.assertNotIn(SENTINEL, json.dumps(data))
                self.assertNotIn(SENTINEL, err)

    def test_api_result_cached_60s_success_and_failure_alike(self) -> None:
        self.add_model_usage()
        opener = FakeOpener(envelope_body(ENVELOPE))
        srv = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=opener)
        for _ in range(2):
            status, data, _ = self.get_quota(srv)
            self.assertEqual(data["source"], "api")
        self.assertEqual(opener.calls, 1)
        failing = FakeOpener(error=urllib.error.HTTPError("https://q", 500, "x", None, None))
        srv2 = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=failing)
        for _ in range(2):
            _, data, _ = self.get_quota(srv2)
            self.assertEqual(data["api"]["error"], "HTTP 500")
        self.assertEqual(failing.calls, 1)  # failures cached too: no hammering

    def test_stale_api_cache_refetches(self) -> None:
        self.add_model_usage()
        opener = FakeOpener(envelope_body(ENVELOPE))
        srv = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=opener)
        srv.state.quota.api_ttl = 0.0  # every snapshot is stale
        for _ in range(2):
            self.get_quota(srv)
        self.assertEqual(opener.calls, 2)

    def test_estimate_cached_10s_then_refreshes(self) -> None:
        self.add_model_usage()
        t0 = int(time.time() * 1000)
        self.mu(t0 - 1000, tokens=100)
        srv = self.serve()
        _, data, _ = self.get_quota(srv)
        self.assertEqual(data["estimate"]["requests_5h"], 1)
        self.mu(t0 - 500, tokens=100)
        _, data, _ = self.get_quota(srv)
        self.assertEqual(data["estimate"]["requests_5h"], 1)  # still cached
        srv.state.quota.estimate_ttl = 0.0
        _, data, _ = self.get_quota(srv)
        self.assertEqual(data["estimate"]["requests_5h"], 2)

    def test_concurrent_requests_single_flight_the_api_call(self) -> None:
        self.add_model_usage()
        opener = FakeOpener(envelope_body(ENVELOPE), delay=0.2)
        srv = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL}, opener=opener)
        results: list[tuple[int, dict]] = []

        def hit() -> None:
            results.append(srv.get_json("/api/quota"))

        threads = [threading.Thread(target=hit) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 3)
        for status, data in results:
            self.assertEqual(status, 200)
            self.assertEqual(data["source"], "api")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.max_concurrent, 1)

    def test_insecure_url_reports_estimate_with_error_and_no_network(self) -> None:
        self.add_model_usage()
        srv = self.serve({"ZCODE_BIGMODEL_USAGE_API_KEY": SENTINEL,
                          "ZCODE_BIGMODEL_USAGE_QUOTA_URL": "http://example.com/q"},
                         opener=BombedOpener())
        status, data, err = self.get_quota(srv)
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "estimate")
        self.assertEqual(data["api"], {"configured": True, "error": "insecure quota url"})
        self.assertEqual(data["windows"][0]["id"], "5h")  # estimate window stands in
        self.assertNotIn(SENTINEL, json.dumps(data))
        self.assertNotIn(SENTINEL, err)

    def test_response_shape_keys_exact(self) -> None:
        self.add_model_usage()
        srv = self.serve()
        _, data, _ = self.get_quota(srv)
        self.assertEqual(set(data), {"source", "fetched_ms", "api", "level",
                                     "windows", "tools", "estimate"})
        self.assertEqual(set(data["api"]), {"configured", "error"})
        self.assertEqual(set(data["estimate"]),
                         {"tokens_5h", "requests_5h", "limited", "limit_reset_ms"})


if __name__ == "__main__":
    unittest.main()
