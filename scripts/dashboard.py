"""zcrew dashboard - local read-only web view of ZCode worker sessions.

Serves the single page in dashboard.html (a sibling file) plus JSON endpoints
backed by zcode_activity.py and zcode_quota.py (also siblings), like ``zcrew
watch`` but in a browser:

    GET /          the page
    GET /api/sessions?limit=N           session summaries for the DIR scope
    GET /api/sessions/<id>/activity     one session's timeline, incremental
    GET /api/quota                      Coding Plan quota (Z.ai API or estimate)

The server binds 127.0.0.1 only and never writes anything: the database is
only ever opened through zcode_activity / zcode_quota (``mode=ro``). Host
headers other than 127.0.0.1/localhost:<port> are rejected (DNS-rebinding
guard), no CORS headers are sent, and every response carries nosniff,
no-referrer and a same-origin Content-Security-Policy. DIR / --all come from
the command line only, so the page cannot read another project's sessions.

/api/quota may make one server-side outbound GET to Z.ai's quota API when an
API key is configured ($ZCODE_BIGMODEL_USAGE_API_KEY / $BIGMODEL_USAGE_API_KEY);
without a key it stays fully local (an estimate from the db). The key is only
ever put on the request's authorization header - it never appears in a
response, error text or log line (see zcode_quota.fetch_api). Quota URL
overrides must be https (http:// is accepted for loopback test servers only),
redirects are refused, and any API failure falls back to the local estimate.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
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
        self.zq = _load_sibling("zcode_quota")
        self.html = (HERE / "dashboard.html").read_text(encoding="utf-8")
        self.html_bytes = self.html.encode("utf-8")
        self.db_path = self.za.default_db_path(env)
        self.scope_dir = scope_dir
        self.scope_label = "all" if scope_dir is None else str(scope_dir)
        self.quota = QuotaCache(self.zq, self.db_path, env)


class QuotaCache:
    """/api/quota's server-side cache; the only mutable part of DashboardState.

    The API answer - success AND failure alike - is kept for ``api_ttl`` so a
    dead network is not hammered and concurrent requests never fire parallel
    calls (the lock covers the outbound attempt itself, on the request thread,
    only when the cache is stale). The local estimate is a cheap read-only db
    query refreshed every ``estimate_ttl``. ``opener`` exists so tests can
    replace the network call.
    """

    def __init__(self, zq: ModuleType, db_path: Any, env: Mapping[str, str],
                 opener: Any = None) -> None:
        self.zq = zq
        self.db_path = db_path
        self.env = env
        self.opener = opener
        self.lock = threading.Lock()
        self.api_ttl = 60.0
        self.estimate_ttl = 10.0
        self.api_timeout = 5.0
        self._api_tried = False
        self._api_ok: dict[str, Any] | None = None
        self._api_error: str | None = None
        self._api_at = 0.0
        self._api_fetched_ms = 0
        self._estimate: dict[str, Any] | None = None
        self._estimate_at = 0.0
        self._estimate_ms = 0

    def _refresh_estimate(self, now_ms: int, mono: float) -> None:
        self._estimate = self.zq.local_estimate(self.db_path, now_ms)
        self._estimate_at = mono
        self._estimate_ms = int(time.time() * 1000)

    def snapshot(self, now_ms: int | None = None) -> dict[str, Any]:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        mono = time.monotonic()
        with self.lock:
            if self._estimate is None or mono - self._estimate_at >= self.estimate_ttl:
                self._refresh_estimate(now, mono)
            assert self._estimate is not None
            error: str | None = None
            level: str | None = None
            windows: list[dict[str, Any]] = []
            tools: dict[str, Any] | None = None
            configured = self.zq.api_key(self.env) is not None
            if configured:
                if not self._api_tried or mono - self._api_at >= self.api_ttl:
                    try:
                        self._api_ok = self.zq.fetch_api(
                            self.env, timeout=self.api_timeout, opener=self.opener)
                        self._api_error = None
                    except self.zq.QuotaError as exc:
                        self._api_ok = None
                        self._api_error = str(exc) or "network error"
                    except Exception:
                        # Generic text only: an arbitrary exception's message is
                        # not trusted to be key-free.
                        self._api_ok = None
                        self._api_error = "network error"
                    self._api_tried = True
                    self._api_at = mono
                    self._api_fetched_ms = int(time.time() * 1000)
                if self._api_ok is not None:
                    # fetched_ms reflects the primary source's production time
                    level, windows, tools = (self._api_ok["level"],
                                             self._api_ok["windows"], self._api_ok["tools"])
                    return self._payload("api", configured, None, level, windows, tools,
                                         self._api_fetched_ms)
                error = self._api_error
            window = {
                "id": "5h",
                "remaining_pct": None,  # the estimate has no plan size to
                "used_pct": None,       # compute a percentage from
                "reset_ms": self._estimate["limit_reset_ms"] if self._estimate["limited"] else None,
                "used": self._estimate["tokens_5h"],
                "total": None,
            }
            return self._payload("estimate", configured, error, None, [window], None,
                                 self._estimate_ms)

    def _payload(self, source: str, configured: bool, error: str | None,
                 level: str | None, windows: list[dict[str, Any]],
                 tools: dict[str, Any] | None, fetched_ms: int) -> dict[str, Any]:
        assert self._estimate is not None
        return {
            "source": source,
            "fetched_ms": fetched_ms,
            "api": {"configured": configured, "error": error},
            "level": level,
            "windows": windows,
            "tools": tools,
            "estimate": self._estimate,
        }


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
            elif path == "/api/quota":
                self._api_quota()
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

    def _api_quota(self) -> None:
        try:
            payload = self.state.quota.snapshot()
        except Exception:
            # Fixed text: an exception message is not trusted to be key-free.
            self._json(503, {"error": "quota unavailable"})
            return
        self._json(200, payload)

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
