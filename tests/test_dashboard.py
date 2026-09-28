from __future__ import annotations

import http.client
import importlib.util
import io
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import quote

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


dash = _load("dashboard")


class ServerHandle:
    """A dashboard server bound to an ephemeral port, served from a daemon thread."""

    def __init__(self, db: Path, scope_dir: str | None) -> None:
        self.state = dash.DashboardState({"ZCREW_ZCODE_DB": str(db)}, scope_dir)
        self.state.za.LAYOUT_DEGRADED = False  # sticky per process; reset per server
        self.httpd = dash.DashboardServer(("127.0.0.1", 0), dash.DashboardHandler, self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path: str, method: str = "GET", host: str | None = None
                ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers={"Host": host} if host is not None else {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        finally:
            conn.close()

    def get_json(self, path: str, **kw: object) -> tuple[int, dict[str, str], dict]:
        status, headers, body = self.request(path, **kw)  # type: ignore[arg-type]
        return status, headers, json.loads(body.decode("utf-8"))


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.db = self.fx.path
        self._servers: list[ServerHandle] = []

    def tearDown(self) -> None:
        for srv in self._servers:
            srv.close()
        self._tmp.cleanup()

    def serve(self, scope: str | None = SANDBOX, db: Path | None = None) -> ServerHandle:
        srv = ServerHandle(db or self.db, scope)
        self._servers.append(srv)
        return srv

    # --- fixtures ---------------------------------------------------------------

    def seed_completed(self, sid: str = "sess_aaaa1111-done", directory: str = SANDBOX,
                       at: int = NOW - 60_000) -> None:
        fx = self.fx
        fx.session(sid, directory, "Fix the calc", at, at + 900)
        fx.message(f"{sid}_u", sid, "user", at)
        fx.message(f"{sid}_a", sid, "assistant", at + 10)
        fx.part(sid, f"{sid}_a", tool("Read", "completed", {"file_path": directory + r"\src\calc.py"}),
                at + 100, at + 150)
        fx.part(sid, f"{sid}_a", tool("Bash", "completed", {"command": "make check"},
                                      output="all good"), at + 200, at + 400)
        fx.turn(sid, "turn_1", "completed", at, at + 900, tools=2, errors=0)

    # --- page + security headers --------------------------------------------------

    def test_root_serves_html_with_security_headers(self) -> None:
        srv = self.serve()
        status, headers, body = srv.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")
        text = body.decode("utf-8")
        self.assertIn("<!doctype html>", text.lower())
        self.assertEqual(text, (SCRIPTS / "dashboard.html").read_text(encoding="utf-8"))

    def test_api_responses_carry_security_headers_and_no_cors(self) -> None:
        self.seed_completed()
        srv = self.serve()
        status, headers, _ = srv.get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertFalse(any(k.lower() == "access-control-allow-origin" for k in headers))

    def test_server_binds_loopback_only(self) -> None:
        srv = self.serve()
        self.assertEqual(srv.httpd.server_address[0], "127.0.0.1")

    def test_bad_host_403_and_loopback_hosts_ok(self) -> None:
        self.seed_completed()
        srv = self.serve()
        status, _, data = srv.get_json("/api/sessions", host="evil.example")
        self.assertEqual(status, 403)
        status, _, _ = srv.get_json("/api/sessions", host=f"localhost:{srv.port}")
        self.assertEqual(status, 200)
        status, _, _ = srv.get_json("/api/sessions", host=f"127.0.0.1:{srv.port}")
        self.assertEqual(status, 200)

    def test_non_get_methods_405(self) -> None:
        srv = self.serve()
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            status, headers, _ = srv.request("/api/sessions", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("Allow"), "GET", method)

    def test_unknown_path_404(self) -> None:
        srv = self.serve()
        status, _, data = srv.get_json("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)
        status, _, _ = srv.get_json("/api/other")
        self.assertEqual(status, 404)

    # --- /api/sessions --------------------------------------------------------------

    def test_sessions_shape_and_dir_scope_vs_all(self) -> None:
        self.seed_completed("sess_aaaa1111-done", SANDBOX, at=NOW - 60_000)
        self.seed_completed("sess_eeee5-elsewhere", OTHER_DIR, at=NOW - 50_000)
        scoped = self.serve(scope=SANDBOX)
        status, _, data = scoped.get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"scope", "now_ms", "layoutDegraded", "sessions"})
        self.assertEqual(data["scope"], SANDBOX)
        self.assertFalse(data["layoutDegraded"])
        self.assertIsInstance(data["now_ms"], int)
        self.assertEqual([s["id"] for s in data["sessions"]], ["sess_aaaa1111-done"])
        row = data["sessions"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["tool_calls"], 2)
        self.assertEqual(row["directory"], SANDBOX)
        self.assertIn("created_ms", row)
        self.assertIn("last_context_tokens", row)

        every = self.serve(scope=None)  # --all
        status, _, data = every.get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(data["scope"], "all")
        self.assertEqual([s["id"] for s in data["sessions"]],
                         ["sess_eeee5-elsewhere", "sess_aaaa1111-done"])

    def test_sessions_limit_default_and_cap(self) -> None:
        for i in range(250):
            self.fx.session(f"sess_{i:04d}-bulk", SANDBOX, f"bulk {i}", i, i)
        srv = self.serve()
        _, _, data = srv.get_json("/api/sessions")
        self.assertEqual(len(data["sessions"]), 30)  # default
        _, _, data = srv.get_json("/api/sessions?limit=1000")
        self.assertEqual(len(data["sessions"]), 200)  # capped
        _, _, data = srv.get_json("/api/sessions?limit=abc")
        self.assertEqual(len(data["sessions"]), 30)  # junk falls back to the default

    def test_sessions_missing_db_503_json(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "db.sqlite"
        srv = self.serve(db=missing)
        status, _, data = srv.get_json("/api/sessions")
        self.assertEqual(status, 503)
        self.assertIn("ZCode database not found", data["error"])
        self.assertFalse(missing.exists())

    # --- /api/sessions/<id>/activity --------------------------------------------------

    def test_activity_incremental_cursor_same_key_running_then_completed(self) -> None:
        fx = self.fx
        sid = "sess_incr1"
        fx.session(sid, SANDBOX, "inc", 1_000, 1_000)
        fx.message("ma", sid, "assistant", 1_000)
        running = tool("Bash", "running", {"command": "python -m pytest"})
        pid = fx.part(sid, "ma", running, 2_000)
        srv = self.serve()
        # unique prefix resolves through the library
        status, _, data = srv.get_json("/api/sessions/sess_incr/activity")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"summary", "activity", "cursor"})
        self.assertEqual(data["summary"]["id"], sid)
        row = data["activity"][0]
        self.assertEqual(len(data["activity"]), 1)
        self.assertEqual((row["key"], row["status"]), (pid, "running"))
        self.assertEqual(row["kind"], "command")
        self.assertEqual(row["icon"], "$")
        self.assertEqual(row["label"], "cmd")
        self.assertEqual(row["summary"], "python -m pytest")
        self.assertRegex(row["time"], r"^\d\d:\d\d:\d\d$")
        cursor = data["cursor"]

        _, _, again = srv.get_json(f"/api/sessions/{sid}/activity?after={quote(cursor, safe='')}")
        self.assertEqual(again["activity"], [])
        self.assertEqual(again["cursor"], cursor)

        fx.update_part(pid, tool("Bash", "completed", {"command": "python -m pytest"},
                                 output="2 failed, 5 passed in 1s"), 3_000)
        _, _, done = srv.get_json(f"/api/sessions/{sid}/activity?after={quote(cursor, safe='')}")
        row = done["activity"][0]
        self.assertEqual((row["key"], row["status"]), (pid, "completed"))  # same key, new status
        self.assertEqual(row["detail"], "2 failed, 5 passed in 1s")

    def test_activity_rows_carry_category(self) -> None:
        fx = self.fx
        sid = "sess_cats"
        fx.session(sid, SANDBOX, "cats", 1_000, 1_000)
        fx.message("ma", sid, "assistant", 1_000)
        t = 1_000
        for name in ("Read", "Bash", "Edit", "TodoWrite", "WebFetch", "Agent",
                     "mcp__brain__wiki_search", "mcp__playwright__browser_navigate"):
            t += 10
            fx.part(sid, "ma", tool(name, "completed", {"file_path": SANDBOX + r"\x.py"}), t)
        t += 10
        fx.part(sid, "ma", {"type": "text", "text": "done", "time": {"start": 1, "end": 2}}, t)
        fx.turn(sid, "turn_1", "completed", 1_000, t + 10, tools=8, errors=0)
        srv = self.serve()
        status, _, data = srv.get_json(f"/api/sessions/{sid}/activity")
        self.assertEqual(status, 200)
        cats = srv.state.za.CATEGORIES
        rows = data["activity"]
        self.assertEqual(len(rows), 10)
        for row in rows:
            with self.subTest(key=row["key"]):
                self.assertIn(row["category"], cats)
        by_tool = {row["tool"]: row["category"] for row in rows if row.get("tool")}
        self.assertEqual(by_tool["Read"], "read")
        self.assertEqual(by_tool["Edit"], "edit")
        self.assertEqual(by_tool["Bash"], "run")
        self.assertEqual(by_tool["TodoWrite"], "agent")
        self.assertEqual(by_tool["WebFetch"], "web")
        self.assertEqual(by_tool["Agent"], "agent")
        self.assertEqual(by_tool["mcp__brain__wiki_search"], "agent")
        self.assertEqual(by_tool["mcp__playwright__browser_navigate"], "web")
        by_kind = {row["kind"]: row["category"] for row in rows}
        self.assertEqual(by_kind["message"], "msg")
        self.assertEqual(by_kind["turn_end"], "msg")

    def test_activity_out_of_scope_session_404(self) -> None:
        self.seed_completed("sess_eeee5-elsewhere", OTHER_DIR, at=NOW - 50_000)
        self.seed_completed("sess_aaaa1111-done", SANDBOX, at=NOW - 60_000)
        srv = self.serve(scope=SANDBOX)
        status, _, data = srv.get_json("/api/sessions/sess_eeee5-elsewhere/activity")
        self.assertEqual(status, 404)
        self.assertIn("error", data)
        # the same server still serves in-scope sessions
        status, _, data = srv.get_json("/api/sessions/sess_aaaa1111-done/activity")
        self.assertEqual(status, 200)
        self.assertEqual(data["summary"]["id"], "sess_aaaa1111-done")

    def test_activity_unknown_id_404(self) -> None:
        self.seed_completed()
        srv = self.serve()
        status, _, data = srv.get_json("/api/sessions/sess_zzzzz/activity")
        self.assertEqual(status, 404)
        self.assertIn("no ZCode session", data["error"])

    def test_activity_missing_db_503_json(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "db.sqlite"
        srv = self.serve(db=missing)
        status, _, data = srv.get_json("/api/sessions/sess_x/activity")
        self.assertEqual(status, 503)
        self.assertIn("ZCode database not found", data["error"])

    # --- page source rules -------------------------------------------------------------

    def test_page_has_no_innerhtml_and_no_external_urls(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", text)
        self.assertNotIn("outerHTML", text)
        self.assertNotIn("insertAdjacentHTML", text)
        self.assertNotIn("eval(", text)
        low = text.lower()
        self.assertNotIn("http://", low)
        self.assertNotIn("https://", low)

    def test_page_css_has_mobile_overflow_guards(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("min-width: 0", text)            # grid/flex children may shrink
        self.assertIn("overflow-wrap: anywhere", text)  # no-space strings wrap
        self.assertIn("text-overflow: ellipsis", text)  # single-line titles clip cleanly

    # --- page source rules: dev-tool terminal --------------------------------------

    def test_page_direction_contract_is_first_body_child(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        m = re.search(r"<body>\s*<!--\s*DIRECTION CONTRACT\b", text)
        self.assertIsNotNone(m, "the direction contract comment must open <body>")
        self.assertIn("seed key ce25968b", text)

    def test_page_size_within_budget(self) -> None:
        size = (SCRIPTS / "dashboard.html").stat().st_size
        self.assertLessEqual(size, 48 * 1024)  # single self-contained file, <= 48 KB

    def test_page_is_dark_only_and_motion_safe(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("prefers-color-scheme", text)  # one palette: dark
        self.assertIn("prefers-reduced-motion", text)   # pulse opt-out exists

    def test_page_has_listbox_rows_and_keyboard_selection(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('role="listbox"', text)   # the session list is a listbox
        self.assertIn('"role", "option"', text)  # rows become keyboard options
        self.assertIn("tabIndex = 0", text)      # rows are keyboard focusable
        self.assertIn('"aria-selected"', text)   # selection is exposed

    def test_page_type_scale_is_four_steps(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        sizes = set(re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", text))
        self.assertTrue(sizes, "no font sizes found")
        self.assertTrue(sizes <= {"11", "13", "16", "20"},
                        f"unexpected font sizes: {sorted(sizes)}")

    def test_page_layout_and_mobile_flow(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: 320px 1fr", text)  # fixed sidebar + fluid main
        self.assertIn("max-height: 38vh", text)                  # session list keeps its own scroll
        self.assertIn("max-width: 430px", text)                  # narrow phones drop the time column

    # --- page source rules: categories, filters, quota ------------------------------

    def test_page_has_six_category_chips(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        for cat in ("read", "edit", "run", "web", "agent", "msg"):
            self.assertIn(f'data-cat="{cat}"', text)
            self.assertIn(f'"n-{cat}"', text)  # per-category count element

    def test_page_filters_rows_via_container_attributes(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('data-f-read="0"', text)  # hiding rows is CSS on the log container
        self.assertIn('data-errs="1"', text)    # errors-only filter likewise

    def test_page_polls_quota_endpoint(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("/api/quota", text)
        self.assertIn("30000", text)  # 30 s quota poll interval

    def test_page_has_search_and_shortcut_keys(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('placeholder="Filter sessions"', text)
        self.assertIn("ArrowDown", text)   # j/k or arrows move the selection
        self.assertIn("ArrowUp", text)
        self.assertIn('"?"', text)         # shortcuts popover key

    # --- page source rules: T2 polish (scrollbars, tints, sidebar, quota) ---------

    def test_page_has_themed_scrollbars(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("scrollbar-width: thin", text)
        self.assertIn("scrollbar-color: #2a303a transparent", text)
        self.assertIn("::-webkit-scrollbar-thumb { background: #2a303a", text)
        self.assertIn("::-webkit-scrollbar-thumb:hover { background: #3a424e", text)

    def test_page_log_rows_tinted_by_category(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn(".a-read { --tc: 88 166 255; }", text)
        self.assertIn(".a-agent { --tc: 57 197 207; }", text)
        self.assertIn("background: rgb(var(--tc) / .055);", text)   # row tint
        self.assertIn("background: rgba(88,166,255,.14);", text)    # tag tint stays
        self.assertIn(".act.err { --tc: 248 81 73; background: rgb(var(--tc) / .09); }", text)
        self.assertIn("margin-bottom: 1px", text)  # rows read as separate lines

    def test_page_has_collapsible_sidebar(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="side-btn"', text)
        self.assertIn('aria-controls="side"', text)
        self.assertIn('k === "b"', text)        # keyboard shortcut
        self.assertIn("body.noside #shell", text)
        self.assertIn("<kbd>b</kbd>", text)     # documented in the popover

    def test_page_quota_button_and_popover(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<button id="quota" type="button" aria-expanded="false"', text)
        self.assertIn('aria-controls="qpop"', text)
        self.assertIn('id="qpop"', text)
        self.assertIn("ZCode quota", text)
        self.assertIn("max-width: calc(100vw - 32px)", text)

    def test_page_localstorage_access_is_guarded(self) -> None:
        text = (SCRIPTS / "dashboard.html").read_text(encoding="utf-8")
        # every localStorage touch sits inside a try/catch helper, one per line
        self.assertIn("try { return localStorage.getItem(", text)
        self.assertIn("try { localStorage.setItem(", text)
        unguarded = [line.strip() for line in text.splitlines()
                     if "localStorage." in line and "try {" not in line]
        self.assertEqual(unguarded, [])

    # --- entry point --------------------------------------------------------------------

    def test_run_port_in_use_message(self) -> None:
        srv = self.serve()
        args = SimpleNamespace(all=False, dir=None, port=srv.port, no_open=True)
        out, err = io.StringIO(), io.StringIO()
        code = dash.run(args, env={"ZCREW_ZCODE_DB": str(self.db)}, out=out, err=err, open_browser=None)
        self.assertEqual(code, 1)
        self.assertIn(f"zcrew: port {srv.port} is in use; try --port", err.getvalue())


if __name__ == "__main__":
    unittest.main()
