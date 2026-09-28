"""zcrew dashboard - local read-only web view of ZCode worker sessions.

Serves the single page in dashboard.html (a sibling file) plus two JSON
endpoints backed by zcode_activity.py (also a sibling), like ``zcrew watch``
but in a browser:

    GET /          the page
    GET /api/sessions?limit=N           session summaries for the DIR scope
    GET /api/sessions/<id>/activity     one session's timeline, incremental

The server binds 127.0.0.1 only and never writes anything: the database is
only ever opened through zcode_activity (``mode=ro``). Host headers other
than 127.0.0.1/localhost:<port> are rejected (DNS-rebinding guard), no CORS
headers are sent, and every response carries nosniff, no-referrer and a
same-origin Content-Security-Policy. DIR / --all come from the command line
only, so the page cannot read another project's sessions.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent

DEFAULT_PORT = 8765
DEFAULT_LIMIT = 30
MAX_LIMIT = 200

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def _load_sibling(name: str) -> ModuleType:
    """Load a sibling script under the name zcrew.py would give it."""
    mod_name = f"_zcrew_{name}"
    module = sys.modules.get(mod_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(mod_name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}.py next to {HERE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


class DashboardState:
    """Everything a handler needs; fixed at server start, read-only afterwards."""

    def __init__(self, env: Mapping[str, str], scope_dir: str | None) -> None:
        self.za = _load_sibling("zcode_activity")
        self.cli = _load_sibling("activity_cli")  # ICONS / LABELS / time format
        self.html = (HERE / "dashboard.html").read_text(encoding="utf-8")
        self.html_bytes = self.html.encode("utf-8")
        self.db_path = self.za.default_db_path(env)
        self.scope_dir = scope_dir
        self.scope_label = "all" if scope_dir is None else str(scope_dir)


def _parse_limit(qs: Mapping[str, list[str]]) -> int:
    raw = (qs.get("limit") or [""])[0].strip()
    try:
        value = int(raw) if raw else DEFAULT_LIMIT
    except ValueError:
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _is_not_found(message: str) -> bool:
    """ActivityError messages that mean 'no such session', not 'database broken'."""
    return "no ZCode session matches" in message or "ambiguous" in message or "empty session id" in message


def _activity_dict(cli: ModuleType, za: ModuleType, act: Any) -> dict[str, Any]:
    """An Activity as a dict plus the display fields the page renders."""
    out = asdict(act)
    out["time"] = cli._hms(act.at_ms)
    out["icon"] = cli.ICONS.get(act.kind, "*")
    out["label"] = cli.LABELS.get(act.kind, act.kind)
    out["category"] = za.activity_category(act.kind, act.tool)
    return out


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "zcrew-dashboard"
    sys_version = ""  # do not advertise the Python version
    protocol_version = "HTTP/1.1"  # keep-alive; every response carries Content-Length

    # --- plumbing ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # silence the default per-request access log

    @property
    def state(self) -> DashboardState:
        return self.server.state  # type: ignore[no-any-return]

    def _send(self, status: int, body: bytes, content_type: str,
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Mapping[str, Any],
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _method_not_allowed(self) -> None:
        self._json(405, {"error": "method not allowed; GET only"}, extra=(("Allow", "GET"),))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update(("127.0.0.1", "localhost"))
        return host in allowed

    # --- routing -------------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            if not self._host_ok():
                self._json(403, {"error": "forbidden Host header"})
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/":
                self._send(200, self.state.html_bytes, "text/html; charset=utf-8")
            elif path == "/api/sessions":
                self._api_sessions(parse_qs(parsed.query))
            elif path.startswith("/api/sessions/") and path.endswith("/activity"):
                self._api_activity(path[len("/api/sessions/"):-len("/activity")], parse_qs(parsed.query))
            else:
                self._json(404, {"error": "not found"})
        except ConnectionError:
            pass  # the client hung up mid-response (covers reset/aborted/broken pipe)
        except Exception as exc:  # JSON keeps the page usable instead of a dead tab
            try:
                self._json(500, {"error": f"internal error: {exc}"})
            except OSError:
                pass

    # --- endpoints -------------------------------------------------------------------

    def _api_sessions(self, qs: Mapping[str, list[str]]) -> None:
        st = self.state
        try:
            sessions = st.za.list_sessions(st.db_path, cwd=st.scope_dir, limit=_parse_limit(qs))
        except st.za.ActivityError as exc:
            self._json(503, {"error": str(exc)})
            return
        self._json(200, {
            "scope": st.scope_label,
            "now_ms": int(time.time() * 1000),
            "layoutDegraded": bool(getattr(st.za, "LAYOUT_DEGRADED", False)),
            "sessions": [asdict(s) for s in sessions],
        })

    def _api_activity(self, raw_id: str, qs: Mapping[str, list[str]]) -> None:
        st = self.state
        sid = raw_id.strip("/")
        if not sid or "/" in sid:
            self._json(404, {"error": "not found"})
            return
        after = (qs.get("after") or [""])[0] or None
        try:
            summary = st.za.session_summary(st.db_path, sid)  # full id or unique prefix
        except st.za.ActivityError as exc:
            self._json(404 if _is_not_found(str(exc)) else 503, {"error": str(exc)})
            return
        if st.scope_dir is not None and not st.za._under(summary.directory, st.scope_dir):
            self._json(404, {"error": "not found"})  # other projects' sessions stay invisible
            return
        try:
            activities, cursor = st.za.session_activity(st.db_path, summary.id, after=after)
        except st.za.ActivityError as exc:
            self._json(503, {"error": str(exc)})
            return
        self._json(200, {
            "summary": asdict(summary),
            "activity": [_activity_dict(st.cli, st.za, a) for a in activities],
            "cursor": cursor,
        })


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # A busy port must fail loudly; SO_REUSEADDR would let a second dashboard
    # share (on Windows even hijack) the port instead of erroring out.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 state: DashboardState) -> None:
        super().__init__(address, handler)
        self.state = state

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return  # browsers open and drop connections constantly; not worth a traceback
        super().handle_error(request, client_address)


def run(
    args: Any,
    *,
    env: Mapping[str, str] | None = None,
    out: Any = None,
    err: Any = None,
    open_browser: Any = webbrowser.open,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    server = None
    try:
        try:
            state = DashboardState(env, None if args.all else (args.dir or os.getcwd()))
        except OSError as exc:
            err.write(f"zcrew: cannot read dashboard.html: {exc}\n")
            return 1
        port = int(args.port)
        try:
            server = DashboardServer(("127.0.0.1", port), DashboardHandler, state)
        except OSError:
            err.write(f"zcrew: port {port} is in use; try --port\n")
            return 1
        url = f"http://127.0.0.1:{server.server_address[1]}"
        out.write(f"dashboard: {url}  (Ctrl+C to stop)\n")
        if hasattr(out, "flush"):
            out.flush()
        if not args.no_open and open_browser is not None:
            try:
                open_browser(url)
            except Exception:
                pass  # a headless session must not kill the dashboard
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # Ctrl+C is the normal way to stop
    finally:
        if server is not None:
            server.server_close()
    return 0
