"""zcrew quota - Coding Plan usage quota: Z.ai's API when configured, else a local estimate.

Two sources, one JSON contract (served by dashboard.py as /api/quota):

- ``fetch_api`` calls the Z.ai quota endpoint ZCode itself uses (default
  https://api.z.ai/api/monitor/usage/quota/limit; override with
  $ZCODE_BIGMODEL_USAGE_QUOTA_URL / $BIGMODEL_USAGE_QUOTA_URL). The key comes
  from $ZCODE_BIGMODEL_USAGE_API_KEY / $BIGMODEL_USAGE_API_KEY and is sent as a
  raw ``authorization`` header (no "Bearer " prefix). No key means no network
  call at all. URL overrides must be https (http:// is allowed only for
  loopback test servers), and redirects are refused, so the key is never
  forwarded anywhere but the configured URL. Every failure raises QuotaError
  with a short generic message ("HTTP 401", "timeout", "network error",
  "unexpected response", "unexpected redirect", "insecure quota url"); the key
  can never appear in one, and exception chaining is suppressed for the same
  reason.
- ``local_estimate`` reads the model_usage table of ZCode's own database
  (read-only): tokens and requests of the last 5 h for Coding Plan providers,
  plus the newest "Usage limit reached" rate-limit message. That message's
  reset timestamp is Beijing time (UTC+8), not UTC.

``parse_quota_envelope`` maps the API envelope: TOKENS_LIMIT and CREDIT_LIMIT
are equivalent token/credit quotas (unit 3 = rolling 5-hour window, unit 6 =
weekly), TIME_LIMIT is the monthly tools/MCP quota, ``percentage`` is the used
share (0-100), and unknown types pass through as "other" windows.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

KEY_ENVS = ("ZCODE_BIGMODEL_USAGE_API_KEY", "BIGMODEL_USAGE_API_KEY")
URL_ENVS = ("ZCODE_BIGMODEL_USAGE_QUOTA_URL", "BIGMODEL_USAGE_QUOTA_URL")
DEFAULT_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
API_TIMEOUT_S = 5.0

TOKEN_LIMIT_TYPES = frozenset({"TOKENS_LIMIT", "CREDIT_LIMIT"})
TOOLS_LIMIT_TYPE = "TIME_LIMIT"

WINDOW_5H_MS = 5 * 60 * 60 * 1000
BEIJING_TZ = timezone(timedelta(hours=8))

# ZCode rate-limit messages look like
# "[1308][Usage limit reached for 5 hour. Your limit will reset at 2026-09-25 19:32:20][id]".
RESET_MESSAGE_RE = re.compile(
    r"Usage limit reached for (\d+) hour\. "
    r"Your limit will reset at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
)

# How many newest rate_limited rows to inspect for a parseable message; the
# index covers started_at, so scanning a handful from the top is cheap.
RATE_LIMIT_CANDIDATES = 20

Opener = Callable[..., Any]


class QuotaError(Exception):
    """Quota API failure; the message is generic and can never contain the key."""


# --- configuration ------------------------------------------------------------------------


def api_key(env: Mapping[str, str]) -> str | None:
    """The configured API key, or None when unset/empty (then: no network)."""
    for name in KEY_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def quota_url(env: Mapping[str, str]) -> str:
    for name in URL_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return DEFAULT_QUOTA_URL


# --- parsing ------------------------------------------------------------------------------


def parse_limit_message(text: Any) -> int | None:
    """Epoch-ms reset time from a rate-limit message (Beijing time), or None."""
    m = RESET_MESSAGE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _used_pct(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else float(number)


def _remaining_pct(used_pct: float | None) -> float | None:
    if used_pct is None:
        return None
    return min(100.0, max(0.0, 100.0 - used_pct))


def _reset_ms(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _window_id(limit: Mapping[str, Any]) -> str:
    unit = limit.get("unit")
    if isinstance(unit, str) and unit.isdigit():
        unit = int(unit)
    if unit == 3:
        return "5h"
    if unit == 6:
        return "week"
    return "other"


def parse_quota_envelope(obj: Any, now_ms: int) -> dict[str, Any]:
    """Level/windows/tools pieces from a parsed quota JSON body.

    Raises QuotaError("unexpected response") for anything that is not a
    successful payload (non-dict body, code != 200, success false, no data).
    """
    if not isinstance(obj, dict):
        raise QuotaError("unexpected response")
    code = obj.get("code")
    if code is not None and code != 200:
        raise QuotaError("unexpected response")
    if obj.get("success") is False:
        raise QuotaError("unexpected response")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise QuotaError("unexpected response")

    level = data.get("level")
    windows: list[dict[str, Any]] = []
    tools: dict[str, Any] | None = None
    limits = data.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict):
            continue
        ltype = limit.get("type")
        if not isinstance(ltype, str) or not ltype:
            continue
        used = _used_pct(limit.get("percentage"))
        if ltype == TOOLS_LIMIT_TYPE:
            tools = {
                "remaining_pct": _remaining_pct(used),
                "reset_ms": _reset_ms(limit.get("nextResetTime")),
            }
            continue
        windows.append({
            "id": _window_id(limit) if ltype in TOKEN_LIMIT_TYPES else "other",
            "remaining_pct": _remaining_pct(used),
            "used_pct": used,
            "reset_ms": _reset_ms(limit.get("nextResetTime")),
            "used": _number(limit.get("usage")),
            "total": _number(limit.get("number")),
        })
    order = {"5h": 0, "week": 1, "other": 2}
    windows.sort(key=lambda w: order[w["id"]])  # stable: API order within an id
    return {"level": level if isinstance(level, str) and level else None,
            "windows": windows, "tools": tools}


# --- network ------------------------------------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A quota request must land on the configured URL only: a 3xx would copy
    the authorization header to the redirect target, even cross-host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        raise QuotaError("unexpected redirect")


# Built once, used only by fetch_api below; never installed as the global opener.
_DIRECT_OPENER = urllib.request.build_opener(_RefuseRedirects)

# Plain http is tolerated for loopback only (local test/dev servers).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def url_allowed(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        return (parsed.hostname or "").strip("[]").lower() in LOOPBACK_HOSTS
    return False


def fetch_api(
    env: Mapping[str, str],
    timeout: float = API_TIMEOUT_S,
    opener: Opener | None = None,
) -> dict[str, Any]:
    """Call the quota API and return parse_quota_envelope's pieces.

    ``opener`` is injectable for tests (None = a private opener that refuses
    redirects). Raises QuotaError with a key-free message on any failure.
    """
    key = api_key(env)
    if key is None:
        raise QuotaError("no api key")
    url = quota_url(env)
    if not url_allowed(url):
        raise QuotaError("insecure quota url")
    request = urllib.request.Request(url, headers={"authorization": key}, method="GET")
    try:
        if opener is None:
            resp = _DIRECT_OPENER.open(request, timeout=timeout)
        else:
            resp = opener(request, timeout=timeout)
        try:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            body = resp.read()
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        # `from None`: the chained traceback would show the request with the key header.
        raise QuotaError(f"HTTP {exc.code}") from None
    except TimeoutError:
        raise QuotaError("timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise QuotaError("timeout") from None
        raise QuotaError("network error") from None
    except OSError:
        raise QuotaError("network error") from None
    if status != 200:
        raise QuotaError(f"HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise QuotaError("unexpected response") from None
    return parse_quota_envelope(payload, int(time.time() * 1000))


# --- local estimate -----------------------------------------------------------------------


def _connect_ro(path: str | os.PathLike[str]) -> sqlite3.Connection:
    p = Path(path)
    uri = "file:" + quote(p.resolve().as_posix(), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _last_limit_reset(conn: sqlite3.Connection) -> int | None:
    """Reset time of the newest rate-limited request with a parseable message."""
    try:
        rows = conn.execute(
            "SELECT error_message FROM model_usage WHERE error_type = 'rate_limited'"
            f" ORDER BY started_at DESC LIMIT {RATE_LIMIT_CANDIDATES}"
        ).fetchall()
    except sqlite3.Error:
        return None
    for (message,) in rows:
        reset = parse_limit_message(message)
        if reset is not None:
            return reset
    return None


def local_estimate(db_path: str | os.PathLike[str], now_ms: int) -> dict[str, Any]:
    """Db-only estimate of the last 5 h; zeros when the db/table is unreadable.

    The estimate has no percentage (the plan's size is unknown), so it reports
    tokens used, request count, and the last hit limit's reset time.
    """
    out = {"tokens_5h": 0, "requests_5h": 0, "limited": False, "limit_reset_ms": None}
    try:
        conn = _connect_ro(db_path)
    except (OSError, sqlite3.Error):
        return out
    try:
        try:
            total, count = conn.execute(
                "SELECT COALESCE(SUM(computed_total_tokens), 0), COUNT(*) FROM model_usage"
                " WHERE started_at >= ?"
                "   AND (provider_id LIKE 'account:zai%' OR provider_id LIKE '%coding-plan%')",
                (int(now_ms) - WINDOW_5H_MS,),
            ).fetchone()
            out["tokens_5h"], out["requests_5h"] = int(total or 0), int(count or 0)
        except sqlite3.Error:
            pass  # no model_usage table (older ZCode build): zeros stand
        reset = _last_limit_reset(conn)
    finally:
        conn.close()
    if reset is not None:
        out["limit_reset_ms"] = reset
        out["limited"] = reset > int(now_ms)
    return out
