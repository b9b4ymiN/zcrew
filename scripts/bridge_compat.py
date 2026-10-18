#!/usr/bin/env python3
"""Compatibility shim that runs upstream coder-mcp-bridge server.py unchanged.

ZCode >= 3.14 app-server validates session/create strictly and rejects the
``runtimeModel`` key the bridge derives from ~/.zcode/cli/config.json. The
app-server resolves providers itself, so the shim disables that derivation;
the explicit ``model`` and ``thoughtLevel`` params still reach ZCode.

ZCode >= 3.12 also builds its model registry from an ACCOUNT snapshot that the
desktop host pushes over ``provider/updateAccountConfig``; a headless
app-server has no host, so every ``account:*`` coding-plan model is missing
("Select a model before continuing"). The shim pushes that snapshot itself
right after spawning app-server and answers the per-request
``interaction/requestProviderRuntimeHeaders`` ask with the plan's API key.
Approach ported from zcode-acp (Apache-2.0): src/config/account-provider.ts and
answerProviderRuntimeHeaders in src/handlers/server-requests.ts.

Set ZCODE_COMMANDER_RUNTIME_MODEL=on to restore upstream runtimeModel
behaviour; set ZCODE_COMMANDER_ACCOUNT_PROVIDER=off to disable the account
provider push and runtime-headers answer.

Run snapshots returned by agent-start/wait/observe/control/close/recover are
compacted (timestamps, resource paths, permission policy and empty sections
dropped) to save commander context; set ZCODE_COMMANDER_COMPACT=off to restore
upstream output exactly.

Run hooks (``patch_run_hooks``) add two things around agent-start/agent-close:

- Time box: a run that is still working after ``wrapUpSeconds`` (config; 0 turns
  it off; capped below the run's own timeout) gets one ``interrupt`` asking the
  worker to stop and write its REPORT, instead of being cancelled with nothing
  to show. A ``TIMEBOX: 40m`` anywhere in the prompt overrides the default;
  the agent-start result reports the one in effect as ``gate.timebox``.
- Gate (mode ``gateMode``: ``warn`` or ``off``): the first agent-start of a thread
  records its worker contract (SCOPE, DO NOT, VERIFY, ...) and the git state to
  ~/.zcode-commander/gate/ for ``zcrew gate``; the result carries ``gate`` hints,
  and agent-close warns while no gate verdict exists. Nothing is blocked.
Set ZCODE_COMMANDER_RUN_HOOKS=off to disable both.

Worker slots (``patch_worker_slots``) cap running workers machine-wide, across
every commander's bridge: ``concurrencyTarget`` percent of the account's
measured ``concurrencyLimit`` (``zcrew capacity``), never above ``maxWorkers``,
one lower for 10 min after a 1302 concurrency error. Runs over the cap stay
queued. Set ZCODE_COMMANDER_WORKER_SLOTS=off to disable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

BUILTIN_PROVIDER_ENV = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
UPDATE_ACCOUNT_METHOD = "provider/updateAccountConfig"
RUNTIME_HEADERS_METHOD = "interaction/requestProviderRuntimeHeaders"
METHOD_NOT_FOUND = -32601
PUSH_TIMEOUT_SECONDS = 60
HEADERS_UNAVAILABLE = (
    "The headless ZCode bridge can only serve API keys for an entitled GLM "
    "individual Coding Plan; Start/team/off-peak plans need the ZCode desktop app."
)


def default_zcode_v2_dir() -> Path:
    return Path.home() / ".zcode" / "v2"


def runtime_model_enabled(env: Mapping[str, str]) -> bool:
    return env.get("ZCODE_COMMANDER_RUNTIME_MODEL", "off").strip().lower() in {"1", "on", "true", "yes"}


def account_provider_enabled(env: Mapping[str, str]) -> bool:
    return env.get("ZCODE_COMMANDER_ACCOUNT_PROVIDER", "on").strip().lower() not in {"0", "off", "false", "no"}


# --- pure helpers -----------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def builtin_table_path(env: Mapping[str, str]) -> str | None:
    """The provider-table path exactly as handed to the app-server spawn env."""
    value = (env.get(BUILTIN_PROVIDER_ENV) or "").strip()
    return value or None


def read_builtin_table(path: str | None) -> dict | None:
    if not path:
        return None
    table = _read_json(Path(path))
    return table if isinstance(table, dict) else None


def builtin_revision(path: str, revision: Any) -> str:
    """``zcode-builtin:<revision>:<sha256(resolved PATH)>`` - the hash covers the
    path string (Node ``path.resolve``), not the file bytes."""
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()
    return "zcode-builtin:%s:%s" % (0 if revision is None else revision, digest)


def account_rules(table: dict | None) -> list[dict]:
    rules = (((table or {}).get("config") or {}).get("providerConfigRules") or {}).get("providerRules") or []
    return [
        r for r in rules
        if isinstance(r, dict) and ((r.get("config") or {}).get("access") or {}).get("type") == "zhipu-account"
    ]


def _find_rule(table: dict | None, provider_id: str) -> dict | None:
    for rule in account_rules(table):
        if rule.get("providerId") == provider_id:
            return rule
    return None


def legacy_provider_id(table: dict | None, account_id: str) -> str:
    """``account:zai-individual-coding-plan`` -> ``builtin:zai-coding-plan``."""
    if not account_id.startswith("account:"):
        return account_id
    access = ((_find_rule(table, account_id) or {}).get("config") or {}).get("access") or {}
    family, mode = access.get("accountType"), access.get("mode")
    if not family or not mode:
        return account_id
    plan = "coding-plan" if mode == "individual-coding-plan" else mode
    return "builtin:%s-%s" % (family, plan)


def account_provider_id(table: dict | None, legacy_id: str) -> str:
    """``builtin:zai-coding-plan`` -> ``account:zai-individual-coding-plan``."""
    if not legacy_id.startswith("builtin:"):
        return legacy_id
    slug = legacy_id[len("builtin:"):]
    dash = slug.find("-")
    if dash <= 0:
        return legacy_id
    family, plan = slug[:dash], slug[dash + 1:]
    if plan == "coding-plan":
        plan = "individual-coding-plan"
    for rule in account_rules(table):
        access = (rule.get("config") or {}).get("access") or {}
        if access.get("accountType") == family and access.get("mode") == plan:
            return rule.get("providerId") or legacy_id
    return legacy_id


def _legacy_providers(v2_dir: Path) -> dict:
    cfg = _read_json(Path(v2_dir) / "config.json")
    providers = (cfg or {}).get("provider") if isinstance(cfg, dict) else None
    return providers if isinstance(providers, dict) else {}


def entitled_legacy_ids(v2_dir: Path) -> set[str]:
    """Plans the user holds, as legacy ``builtin:*`` ids.

    Union of coding-plan-cache.json ``available`` items and config.json enabled
    builtin providers with an apiKey; setting.json's selection record is only a
    last resort when both are empty (it is a pick, not an entitlement).
    """
    v2_dir = Path(v2_dir)
    out: set[str] = set()
    cache = _read_json(v2_dir / "coding-plan-cache.json")
    items = ((cache or {}).get("entryStatus") or {}).get("items") if isinstance(cache, dict) else None
    for pid, entry in (items or {}).items():
        if isinstance(entry, dict) and entry.get("status") == "available":
            out.add(pid)
    for pid, provider in _legacy_providers(v2_dir).items():
        if not pid.startswith("builtin:") or not isinstance(provider, dict):
            continue
        if provider.get("enabled") is True and (provider.get("options") or {}).get("apiKey"):
            out.add(pid)
    if out:
        return out
    setting = _read_json(v2_dir / "setting.json")
    selected = (setting or {}).get("modelProviderFamilySelectedKeys") if isinstance(setting, dict) else None
    for value in (selected or {}).values():
        if isinstance(value, str) and "builtin:" in value:
            candidate = value[value.index("builtin:"):]
            if candidate:
                out.add(candidate)
    return out


def build_account_payload(
    table_path: str | None, v2_dir: Path, now_ms: Callable[[], int] | None = None
) -> dict | None:
    """The ``provider/updateAccountConfig`` payload, or None if nothing to push."""
    table = read_builtin_table(table_path)
    rules = account_rules(table)
    if not table_path or not rules:
        return None
    entitled_ids = entitled_legacy_ids(v2_dir)
    providers: dict[str, dict] = {}
    states: dict[str, dict] = {}
    for rule in rules:
        pid = rule.get("providerId")
        if not pid:
            continue
        entitled = legacy_provider_id(table, pid) in entitled_ids
        providers[pid] = {
            "builtinModelIds": list((rule.get("config") or {}).get("builtinModelIds") or []),
            "access": {"type": "zhipu-account", "entitled": entitled},
        }
        states[pid] = {
            "availability": "available" if entitled else "unavailable",
            "entitled": entitled,
            "current": entitled,
        }
    stamp = (now_ms or (lambda: int(time.time() * 1000)))()
    return {
        "revision": "account:bridge:%d" % stamp,
        "basedOnZCodeBuiltinRevision": builtin_revision(table_path, table.get("revision")),
        "providers": providers,
        "states": states,
    }


def request_auth_for(table_path: str | None, v2_dir: Path, provider_id: str | None) -> dict | None:
    """``{"apiKey": ...}`` for an entitled individual coding plan, else None."""
    if not provider_id or not provider_id.startswith("account:"):
        return None
    table = read_builtin_table(table_path)
    rule = _find_rule(table, provider_id)
    if (((rule or {}).get("config") or {}).get("access") or {}).get("mode") != "individual-coding-plan":
        return None
    legacy = legacy_provider_id(table, provider_id)
    if legacy == provider_id or legacy not in entitled_legacy_ids(v2_dir):
        return None
    provider = _legacy_providers(v2_dir).get(legacy) or {}
    key = ((provider.get("options") or {}).get("apiKey") or "").strip() if isinstance(provider, dict) else ""
    return {"apiKey": key} if key else None


def runtime_headers_result(table_path: str | None, v2_dir: Path, params: Mapping[str, Any] | None) -> dict:
    params = params or {}
    selection = params.get("modelSelection") if isinstance(params.get("modelSelection"), dict) else {}
    provider_id = selection.get("providerId") or params.get("providerId")
    auth = request_auth_for(table_path, v2_dir, provider_id)
    if auth:
        return {"headersApplied": True, "requestAuth": auth}
    return {"headersApplied": False, "errorMessage": HEADERS_UNAVAILABLE}


MODEL_METHODS = ("session/create", "session/setModel")


def default_reasoning_level(env: Mapping[str, str]) -> str:
    return (env.get("ZCODE_COMMANDER_DEFAULT_REASONING") or "max").strip() or "max"


def with_reasoning_level(method: str, params: Any, default_level: str) -> Any:
    """ZCode 3.14 rejects an object ``model`` without ``options.reasoningLevel``
    for level-bearing models; the bridge's MCP schema cannot pass options, so
    derive it from ``thoughtLevel`` (else the default). Returns new params."""
    if method not in MODEL_METHODS or not isinstance(params, dict):
        return params
    model = params.get("model")
    if not isinstance(model, dict) or (model.get("options") or {}).get("reasoningLevel"):
        return params
    level = params.get("thoughtLevel") or default_level
    options = {**(model.get("options") or {}), "reasoningLevel": level}
    return {**params, "model": {**model, "options": options}}


# --- patches on ZCodeProtocolClient -----------------------------------------

_LOCK_GUARD = threading.Lock()


def _instance_push_lock(client: Any) -> threading.RLock:
    with _LOCK_GUARD:
        lock = client.__dict__.get("_commander_push_lock")
        if lock is None:
            lock = threading.RLock()
            client.__dict__["_commander_push_lock"] = lock
        return lock


def _log(client: Any, message: str) -> None:
    logger = getattr(client, "logger", None)
    if callable(logger):
        try:
            logger(message)
        except Exception:  # noqa: BLE001 - logging must never break transport
            pass


def push_account_config(client: Any, table_path: str | None, v2_dir: Path) -> bool:
    payload = build_account_payload(table_path, v2_dir)
    if payload is None:
        _log(client, "account-provider: no zhipu-account providers to push (table missing?)")
        return False
    try:
        result = client.request(UPDATE_ACCOUNT_METHOD, payload, timeout=PUSH_TIMEOUT_SECONDS) or {}
    except Exception as exc:  # noqa: BLE001 - push is best effort; logged below
        if getattr(exc, "code", None) == METHOD_NOT_FOUND:
            _log(client, "account-provider: app-server has no %s (old ZCode) - skipped" % UPDATE_ACCOUNT_METHOD)
        else:
            _log(client, "account-provider: push failed: %s" % exc)
        return False
    entitled = sum(1 for s in payload["states"].values() if s["entitled"])
    _log(
        client,
        "account-provider: pushed %s provider(s), %d entitled (%s)"
        % (result.get("providerCount", len(payload["providers"])), entitled, result.get("status", "ok")),
    )
    return True


def patch_protocol_client(
    cls: Any, env: Mapping[str, str], v2_dir: Path | None = None
) -> None:
    """Wrap ``start`` (push account snapshot once per new app-server process)
    and ``_handle_server_request`` (answer provider runtime headers)."""
    if getattr(cls, "_commander_account_patched", False):
        return
    v2 = Path(v2_dir) if v2_dir is not None else default_zcode_v2_dir()
    table_path = builtin_table_path(env)
    original_start = cls.start
    original_handle = cls._handle_server_request
    original_request = cls.request
    reasoning_default = default_reasoning_level(env)

    def start(self: Any) -> Any:
        # RLock: the push's request() re-enters start() on this thread, and
        # other threads wait here so session/create cannot overtake the push.
        with _instance_push_lock(self):
            with self._state_lock:
                before = self._proc
            outcome = original_start(self)
            with self._state_lock:
                after = self._proc
            if after is not None and after is not before and self.__dict__.get("_commander_pushed_proc") is not after:
                self.__dict__["_commander_pushed_proc"] = after
                push_account_config(self, table_path, v2)
            return outcome

    def _handle_server_request(self: Any, message: Any) -> Any:
        if isinstance(message, dict) and message.get("method") == RUNTIME_HEADERS_METHOD:
            result = runtime_headers_result(table_path, v2, message.get("params") or {})
            _log(self, "account-provider: runtime headers requested, applied=%s" % result["headersApplied"])
            self._send({"id": message.get("id"), "result": result})
            return None
        return original_handle(self, message)

    def request(self: Any, method: str, params: Any = None, timeout: Any = 30) -> Any:
        return original_request(self, method, with_reasoning_level(method, params, reasoning_default), timeout=timeout)

    cls.start = start
    cls.request = request
    cls._handle_server_request = _handle_server_request
    cls._commander_account_patched = True


# --- compact run snapshots ----------------------------------------------------
#
# Upstream server.py serializes each tool result twice: ``content[0].text`` is
# the JSON string and ``structuredContent`` is the same dict. The commander
# model sees one of them (Claude Code: the text; structuredContent goes to
# transcript metadata), so the cost is the snapshot itself. We compact the dict
# that BackendManager returns, before server.py serializes it, so both copies
# shrink together and stay identical; required runId/status/revision remain.

COMPACT_TOOLS = {
    "agent-start": "start",
    "agent-wait": "wait",
    "agent-observe": "observe",
    "agent-control": "control",
    "agent-close": "close_run",
    "agent-recover": "recover",
}
DETAIL_MAX_CHARS = 300
_ERRORISH = re.compile(r"error|warning|fail|blocked|reason(?!ing)", re.IGNORECASE)
_DROP_KEYS = {
    "createdAtMs", "startedAtMs", "finishedAtMs", "resources", "native",
    "sessionUsage", "permissionPolicy", "backend",
}
_USAGE_KEYS = ("totalTokens", "modelRequests")  # + modelErrors when non-zero (error-ish)


def compact_output_enabled(env: Mapping[str, str]) -> bool:
    return env.get("ZCODE_COMMANDER_COMPACT", "on").strip().lower() not in {"0", "off", "false", "no"}


def _is_errorish(key: Any) -> bool:
    return bool(_ERRORISH.search(str(key)))


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (list, dict, str)) and not value)


def _errorish_items(section: Any) -> dict:
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if _is_errorish(k) and not _empty(v) and v is not False and v != 0}


def _truncate(value: Any, limit: int = DETAIL_MAX_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[+%d chars]" % (len(value) - limit)
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    return value


def _compact_model(model: Any) -> dict:
    if not isinstance(model, dict):
        return {}
    inner = model.get("model") if isinstance(model.get("model"), dict) else {}
    out = {
        "modelId": inner.get("modelId") if inner else model.get("modelId"),
        "thoughtLevel": model.get("thoughtLevel"),
        "status": model.get("status"),
    }
    if model.get("reasoningActive") is True:
        out["reasoningActive"] = True
    out.update(_errorish_items(model))
    return {k: v for k, v in out.items() if not _empty(v)}


def _compact_event(event: Any) -> Any:
    if not isinstance(event, dict):
        return _truncate(event)
    out = {k: v for k, v in event.items() if k != "atMs"}
    if "detail" in out:
        out["detail"] = _truncate(out["detail"])
    return out


def _tool_names(tools: Any) -> list:
    names = []
    for tool in tools if isinstance(tools, list) else []:
        name = tool.get("name") if isinstance(tool, dict) else tool
        if name:
            names.append(name)
    return names


def is_run_snapshot(data: Any) -> bool:
    return isinstance(data, dict) and all(k in data for k in ("runId", "status", "revision"))


def compact_snapshot(tool_name: str, data: Any) -> Any:
    """Compact one run snapshot for the commander; anything else passes through."""
    if tool_name not in COMPACT_TOOLS or not is_run_snapshot(data):
        return data
    out: dict = {}
    for key, value in data.items():
        if key in ("runId", "status", "revision", "result", "resultTruncated", "changed"):
            out[key] = value
        elif key in _DROP_KEYS:
            if key == "sessionUsage" and isinstance(value, dict) and value.get("totalTokens") is not None:
                out["sessionTokens"] = value["totalTokens"]
            elif _is_errorish(key) and not _empty(value):
                out[key] = value
        elif key == "model":
            model = _compact_model(value)
            if model:
                out[key] = model
        elif key == "usage":
            if isinstance(value, dict):
                usage = {k: value[k] for k in _USAGE_KEYS if value.get(k) is not None}
                usage.update(_errorish_items(value))
                if usage:
                    out[key] = usage
            elif not _empty(value):
                out[key] = value
        elif key == "counts":
            if isinstance(value, dict):
                counts = {k: v for k, v in value.items() if v != 0 and not _empty(v)}
                if counts:
                    out[key] = counts
            elif not _empty(value):
                out[key] = value
        elif key == "activeTools":
            names = _tool_names(value)
            if names:
                out[key] = names
        elif key == "lastCheckpoint":
            if isinstance(value, dict) and value.get("fileCount") is not None:
                out[key] = {"fileCount": value["fileCount"]}
        elif key == "resourceLease":
            if isinstance(value, dict) and (value.get("acquired") is False or value.get("blockers")):
                out[key] = {k: v for k, v in value.items() if k != "scope"}
        elif key == "subagents":
            if isinstance(value, dict):
                sub = {"running": value["running"]} if value.get("running") else {}
                sub.update(_errorish_items(value))
                if sub:
                    out[key] = sub
            elif not _empty(value):
                out[key] = value
        elif key == "context":
            if isinstance(value, dict):
                ctx = {}
                ratio = value.get("usedRatio")
                if isinstance(ratio, (int, float)) and ratio > 0:
                    ctx["usedRatio"] = ratio
                ctx.update(_errorish_items(value))
                if ctx:
                    out[key] = ctx
            elif not _empty(value):
                out[key] = value
        elif key == "events":
            if isinstance(value, list):
                out[key] = [_compact_event(e) for e in value]
            else:
                out[key] = value
        elif key == "eventsDropped":
            if value:
                out[key] = value
        elif not _empty(value):
            out[key] = value
    return out


def patch_backend_manager(cls: Any) -> None:
    """Wrap the BackendManager methods server.py calls per tool, so the dict
    it serializes (text and structuredContent alike) is already compact."""
    if getattr(cls, "_commander_compact_patched", False):
        return

    def compacting(tool_name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(self, *args, **kwargs)
            try:
                if tool_name != "agent-close":
                    note_snapshot(result, os.environ)
            except Exception:  # noqa: BLE001 - bookkeeping must never lose a result
                pass
            try:
                return compact_snapshot(tool_name, result)
            except Exception:  # noqa: BLE001 - compaction must never lose a result
                return result

        wrapper.__name__ = original.__name__
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    for tool_name, method_name in COMPACT_TOOLS.items():
        original = getattr(cls, method_name, None)
        if callable(original):
            setattr(cls, method_name, compacting(tool_name, original))
    cls._commander_compact_patched = True


# --- run hooks: time box and gate records --------------------------------------
#
# State lives in plain JSON files, one per thread (or run, until its thread id
# is known), so ``zcrew gate`` (a separate process) can read what the bridge saw.

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "closed"})
RUN_HOOKS_DEFAULTS = {"wrapUpSeconds": 1200, "gateMode": "warn"}
GATE_MODES = ("off", "warn")
WRAP_UP_MARK = "[zcrew:wrap-up]"
WRAP_UP_MIN_SECONDS = 60
TIMEBOX_UNITS = {"s": 1, "m": 60, "h": 3600}
WRAP_UP_GRACE_SECONDS = 120  # the report needs time before the hard timeout
WRAP_UP_POLL_SECONDS = 10.0
BRIDGE_TIMEOUT_ENVS = ("AGENT_MCP_TIMEOUT", "ZCODE_MCP_TIMEOUT")
BRIDGE_TIMEOUT_DEFAULT = 900
SECTION_NAMES = ("OBJECTIVE", "CONTEXT", "SCOPE", "DO NOT", "ACCEPTANCE CRITERIA", "VERIFY", "REPORT")
REQUIRED_SECTIONS = ("OBJECTIVE", "SCOPE", "DO NOT", "ACCEPTANCE CRITERIA", "VERIFY")
# a heading on its own line, or ``NAME: text`` with the first item inline; a note in
# parentheses after the name ("SCOPE (may change)") is allowed and dropped
_SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**(%s)\**(?:\s*\([^)\n]*\))?\**\s*(?::\s*\**\s*(.*?))?\s*$" % "|".join(SECTION_NAMES))
# a line that starts like a heading but is not one: reported when its section ends up missing
_NEAR_MISS_RE = re.compile(r"^\s*(?:#{1,6}\s*)?\**(%s)\b" % "|".join(SECTION_NAMES))
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Case-sensitive on purpose: prose such as "timebox: 5" must not set a time box. The unit may be
# lower or upper case; no unit means minutes.
_TIMEBOX_RE = re.compile(
    r"(?<![\w-])TIMEBOX\s*:\s*(\d+)\s*(?i:(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h))?(?!\w)")
_TIMEBOX_WORD_RE = re.compile(r"\bTIMEBOX\b", re.IGNORECASE)
WRAP_UP_PROMPT = (
    WRAP_UP_MARK + " Time is almost up. Stop experimenting now and write your REPORT: what you changed, "
    "what you measured or verified (real command results), what is unfinished, and what you recommend "
    "next. Do not start new work."
)
TIMEBOX_NOTICE = (
    "\n\n[zcrew] Time box: about %d minutes (wrap-up near %s local time). Run the VERIFY commands "
    "before then. If this task is open-ended (tuning, measuring, investigating), try at most 3 "
    "approaches, then stop and write the REPORT with your numbers. "
    "If you are told to wrap up, stop and report immediately."
)


def run_hooks_enabled(env: Mapping[str, str]) -> bool:
    return env.get("ZCODE_COMMANDER_RUN_HOOKS", "on").strip().lower() not in {"0", "off", "false", "no"}


def state_dir(env: Mapping[str, str]) -> Path:
    explicit = (env.get("ZCODE_COMMANDER_STATE") or "").strip()
    base = Path(explicit) if explicit else Path.home() / ".zcode-commander"
    return base / "gate"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def effective_setting(cwd: str | None, key: str, env: Mapping[str, str]) -> Any:
    """Project ``.claude/zcode-commander.json`` over user ``~/.zcode-commander/config.json``
    over the default; a wrong type falls back to the default."""
    default = RUN_HOOKS_DEFAULTS[key]
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    paths = [Path(cwd) / ".claude" / "zcode-commander.json"] if cwd else []
    paths.append(home / ".zcode-commander" / "config.json")
    for path in paths:
        value = (_read_json(path) or {}).get(key)
        if isinstance(default, int) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(default, str) and value in GATE_MODES:
            return value
    return default


def _timebox_values(prompt: str) -> list[int]:
    """Every ``TIMEBOX: 40m`` in the prompt, in seconds, in order of appearance."""
    return [int(m.group(1)) * TIMEBOX_UNITS[(m.group(2) or "m")[0].lower()]
            for m in _TIMEBOX_RE.finditer(prompt or "")]


def parse_timebox(prompt: str) -> int | None:
    """Seconds from the first ``TIMEBOX: 40m`` anywhere in the prompt, else None."""
    values = _timebox_values(prompt)
    return values[0] if values else None


def parse_contract(prompt: str) -> dict:
    """Split a worker contract into its sections.

    Returns ``sections`` (name -> bullet list; free text counts as one item),
    ``missing`` (required sections absent or empty), ``near_misses`` (for a missing
    section, the first line that looked like its heading), ``scope`` / ``do_not``
    (backticked path patterns) and ``verify`` (backticked commands). Items
    without backticks stay in ``sections`` for the reviewer's judgment.
    """
    sections: dict[str, list[str]] = {}
    near: dict[str, str] = {}
    current: str | None = None
    for line in (prompt or "").splitlines():
        head = _SECTION_RE.match(line)
        if not head:
            miss = _NEAR_MISS_RE.match(line)
            if miss:
                near.setdefault(miss.group(1), line.strip())
        if head:
            current = head.group(1)
            sections.setdefault(current, [])
            if head.group(2):
                sections[current].append(head.group(2))
            continue
        if current is None or not line.strip():
            continue
        bullet = _BULLET_RE.match(line)
        sections[current].append(bullet.group(1) if bullet else line.strip())
    def ticked(name: str) -> list[str]:
        return [t.strip() for item in sections.get(name, []) for t in _BACKTICK_RE.findall(item) if t.strip()]

    def pathlike(tokens: list[str]) -> list[str]:
        return [t for t in tokens if re.search(r"[/\\*.]", t) and " " not in t]

    missing = [n for n in REQUIRED_SECTIONS if not sections.get(n)]
    return {
        "sections": sections,
        "missing": missing,
        "near_misses": {n: near[n] for n in missing if n in near},
        "scope": pathlike(ticked("SCOPE")),
        "do_not": pathlike(ticked("DO NOT")),
        "verify": ticked("VERIFY"),
    }


def _git(cwd: str, *args: str) -> str | None:
    # stdin=DEVNULL: on Windows a child that inherits the bridge's stdin pipe cannot
    # start while the serve loop is blocked reading it, so agent-start hung until
    # the commander's next MCP message arrived.
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_snapshot(cwd: str | None) -> dict:
    """Base commit and the files already dirty before the worker starts."""
    if not cwd:
        return {"base": None, "dirty": []}
    head = _git(cwd, "rev-parse", "HEAD")
    status = _git(cwd, "status", "--porcelain", "-uall")
    dirty = []
    for line in (status or "").splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            dirty.append(path)
    return {"base": head.strip() if head else None, "dirty": dirty}


def record_files(env: Mapping[str, str]) -> list[Path]:
    folder = state_dir(env)
    try:
        return sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def find_record(env: Mapping[str, str], *, thread_id: str | None = None,
                run_id: str | None = None) -> tuple[Path, dict] | None:
    for path in record_files(env):
        data = _read_json(path)
        if not data:
            continue
        if thread_id and data.get("threadId") == thread_id:
            return path, data
        if run_id and run_id in (data.get("runIds") or []):
            return path, data
    return None


def save_record(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def note_snapshot(result: Any, env: Mapping[str, str]) -> None:
    """Remember a run's thread id as soon as any snapshot shows it."""
    if not is_run_snapshot(result) or not result.get("threadId"):
        return
    found = find_record(env, run_id=result["runId"])
    if found and found[1].get("threadId") != result["threadId"]:
        path, data = found
        data["threadId"] = result["threadId"]
        save_record(path, data)


def record_start(args: Mapping[str, Any], result: Any, env: Mapping[str, str], warnings: list[str],
                 timebox: Timebox | None = None) -> None:
    if not is_run_snapshot(result):
        return
    run_id, thread_id = result["runId"], args.get("threadId")
    if thread_id:  # a correction: same record, next round; unknown threads predate gate records
        existing = find_record(env, thread_id=thread_id)
        if existing:
            path, data = existing
            data["rounds"] = int(data.get("rounds", 1)) + 1
            data.setdefault("runIds", []).append(run_id)
            data["roundStartedAt"] = int(time.time() * 1000)
            if timebox:
                data["timebox"] = {"seconds": timebox.seconds, "source": timebox.source}
            save_record(path, data)
        return
    cwd = args.get("cwd")
    contract = parse_contract(str(args.get("prompt") or ""))
    snap = git_snapshot(cwd)
    now = int(time.time() * 1000)
    save_record(state_dir(env) / ("%s.json" % run_id), {
        "runIds": [run_id], "threadId": result.get("threadId"), "cwd": cwd,
        "startedAt": now, "roundStartedAt": now, "rounds": 1, "base": snap["base"], "dirty": snap["dirty"],
        "contract": contract, "verdicts": [], "mode": "warn",
        "timebox": {"seconds": timebox.seconds, "source": timebox.source} if timebox else None,
    })
    if contract["missing"]:
        text = "contract is missing: %s (zcrew gate cannot check what it cannot read)" % ", ".join(contract["missing"])
        for name, line in contract["near_misses"].items():
            shown = line if len(line) <= 60 else line[:59] + "..."
            text += '; heading not recognised: "%s" - write "%s:" or "%s (note):"' % (shown, name, name)
        warnings.append(text)


class Timebox(NamedTuple):
    """The wrap-up time box a run gets: seconds (None = no time box), where it came from, and warnings."""
    seconds: int | None
    source: str  # "contract" (TIMEBOX: in the prompt) | "config" (wrapUpSeconds) | "off"
    warnings: list[str]


def _minutes(seconds: int) -> str:
    return "%gm" % (seconds / 60)


def resolve_timebox(args: Mapping[str, Any], env: Mapping[str, str]) -> Timebox:
    timeout = args.get("timeout")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        timeout = next((int(env[k]) for k in BRIDGE_TIMEOUT_ENVS if str(env.get(k, "")).isdigit()),
                       BRIDGE_TIMEOUT_DEFAULT)
    prompt = str(args.get("prompt") or "")
    values = _timebox_values(prompt)
    warnings: list[str] = []
    if len(set(values)) > 1:
        warnings.append("several TIMEBOX values; using the first (%s)" % _minutes(values[0]))
    if values and values[0]:
        wanted, source = values[0], "contract"
    else:
        wanted, source = int(effective_setting(args.get("cwd"), "wrapUpSeconds", env)), "config"
        if not values and _TIMEBOX_WORD_RE.search(prompt):
            warnings.append("TIMEBOX found but not understood (write e.g. TIMEBOX: 60m); using wrapUpSeconds=%d"
                            % wanted)
    delay = min(wanted, timeout - WRAP_UP_GRACE_SECONDS)
    if wanted and delay >= WRAP_UP_MIN_SECONDS:
        if source == "contract" and delay < wanted:
            warnings.append("TIMEBOX %s is longer than the run timeout %s; wrap-up at %s instead (raise timeout)"
                            % (_minutes(wanted), _minutes(timeout), _minutes(delay)))
        return Timebox(delay, source, warnings)
    if source == "contract":
        warnings.append("TIMEBOX too short for this run's timeout; no time box applied")
    return Timebox(None, "off", warnings)


def wrap_up_delay(args: Mapping[str, Any], env: Mapping[str, str]) -> int | None:
    """Seconds into the run when the wrap-up interrupt fires, or None for no time box."""
    return resolve_timebox(args, env).seconds


def timebox_notice(delay: int, now: float | None = None) -> str:
    wrap_up_at = time.localtime((time.time() if now is None else now) + delay)
    return TIMEBOX_NOTICE % (max(1, delay // 60), time.strftime("%H:%M", wrap_up_at))


def watch_for_wrap_up(
    manager: Any, run_id: str, delay: int, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Interrupt the run once, ``delay`` seconds after it starts running. True when sent."""
    running_since: float | None = None
    give_up = clock() + 86400  # never poll forever for a run that stays in a non-terminal state
    while clock() < give_up:
        try:
            snap = manager.wait(run_id, after_revision=10**9, timeout_ms=0, result_chars=0)
        except Exception:  # noqa: BLE001 - a gone run ends the watch
            return False
        status = snap.get("status") if isinstance(snap, dict) else None
        if status in TERMINAL_STATUSES or (status in ("idle", "paused") and running_since is not None):
            return False  # finished, or the turn being timed has ended
        if status == "running" and running_since is None:
            running_since = clock()
        if running_since is not None and clock() - running_since >= delay:
            try:
                manager.control(run_id, "interrupt", prompt=WRAP_UP_PROMPT, if_status="running")
            except Exception as exc:  # noqa: BLE001 - the hard timeout stays as the safety net
                if log:
                    log("run-hooks: wrap-up interrupt failed for %s: %s" % (run_id, exc))
                return False
            return True
        sleep(WRAP_UP_POLL_SECONDS)
    return False


def patch_run_hooks(cls: Any, env: Mapping[str, str]) -> None:
    """Wrap BackendManager.start / close_run (see the module docstring). Idempotent;
    every hook is best effort and never changes what the run itself does, apart
    from the time-box sentence appended to the prompt."""
    if getattr(cls, "_commander_run_hooks_patched", False):
        return
    original_start, original_close = getattr(cls, "start", None), getattr(cls, "close_run", None)
    if not (callable(original_start) and callable(original_close)):
        return

    def start(self: Any, args: Any) -> Any:
        args = dict(args or {})
        cwd = args.get("cwd")
        mode = effective_setting(cwd, "gateMode", env)
        timebox: Timebox | None = None
        delay = None
        try:
            if isinstance(args.get("prompt"), str):
                timebox = resolve_timebox(args, env)
                delay = timebox.seconds
                if delay and not args.get("threadId"):
                    args["prompt"] += timebox_notice(delay)
        except Exception:  # noqa: BLE001
            timebox, delay = None, None
        result = original_start(self, args)
        warnings: list[str] = []
        try:
            if mode != "off":
                record_start(args, result, env, warnings, timebox)
        except Exception:  # noqa: BLE001
            pass
        if is_run_snapshot(result):
            if delay:
                threading.Thread(
                    target=watch_for_wrap_up, args=(self, result["runId"], delay), daemon=True,
                ).start()
            if isinstance(result, dict) and (mode != "off" or timebox):
                hint: dict[str, Any] = {}
                if mode != "off":
                    hint["verify"] = "before accepting, run: zcrew gate %s" % result["runId"]
                warnings += timebox.warnings if timebox else []
                if warnings:
                    hint["warnings"] = warnings
                if timebox:
                    hint["timebox"] = {"seconds": timebox.seconds, "source": timebox.source}
                result = {**result, "gate": hint}
        return result

    def close_run(self: Any, run_id: Any = None, *, thread_id: Any = None) -> Any:
        result = original_close(self, run_id, thread_id=thread_id)
        try:
            found = find_record(env, thread_id=thread_id, run_id=run_id)
            if found and effective_setting(found[1].get("cwd"), "gateMode", env) != "off" \
                    and not found[1].get("verdicts") and isinstance(result, dict):
                result = {**result, "gate": {"warnings": [
                    "no gate verdict recorded for this run; run: zcrew gate %s, then --accept or --reject"
                    % (run_id or thread_id)]}}
        except Exception:  # noqa: BLE001
            pass
        return result

    cls.start, cls.close_run = start, close_run
    cls._commander_run_hooks_patched = True


# --- worker slots: one machine-wide cap on running workers ----------------------
#
# Every worker draws on the same Coding Plan account. Z.ai does not publish its
# concurrency limit and moves it with load; past it requests fail with 1302
# "Rate limit reached for requests" / "High concurrency usage of this API".
# Each commander process runs its own bridge, so the cap lives in the bridge's
# cross-process lease db: a run needs one of ``cap`` exclusive slot leases next to
# its own resources and stays queued (``resource.waiting``) until one is free. A
# crashed bridge's slots are reclaimed with its other leases.

SLOT_PREFIX = "zcrew:worker-slot:"
# key: (default, lowest, highest); read from the user config only, because the
# limit belongs to the account, not to a project
CONCURRENCY_SETTINGS = {
    "maxWorkers": (5, 1, 10),
    "concurrencyLimit": (0, 0, 50),  # 0 = not measured yet
    "concurrencyTarget": (80, 50, 100),  # percent of concurrencyLimit to use
}
BACKOFF_WINDOW_MS = 10 * 60 * 1000
CAP_CACHE_SECONDS = 5.0
LIMIT_ERROR_ROWS = 500
_USAGE_LIMIT_RE = re.compile(r"\b13(?:0[89]|1\d|2[01])\b|usage limit reached", re.IGNORECASE)
_CONCURRENCY_RE = re.compile(r"\b1302\b|concurren|rate limit reached for requests", re.IGNORECASE)
_OVERLOAD_RE = re.compile(r"\b1305\b|overloaded", re.IGNORECASE)


def worker_slots_enabled(env: Mapping[str, str]) -> bool:
    return env.get("ZCODE_COMMANDER_WORKER_SLOTS", "on").strip().lower() not in {"0", "off", "false", "no"}


def classify_limit_error(error_type: Any, message: Any) -> str | None:
    """``usage`` (5-hour/weekly quota, 1308-1321), ``concurrency`` (1302),
    ``overload`` (1305), ``rate_limited`` (unrecognised 429) or None."""
    text = str(message or "")
    if _USAGE_LIMIT_RE.search(text):
        return "usage"
    if _CONCURRENCY_RE.search(text):
        return "concurrency"
    if _OVERLOAD_RE.search(text):
        return "overload"
    return "rate_limited" if str(error_type or "") == "rate_limited" else None


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())


def concurrency_settings(env: Mapping[str, str]) -> dict[str, int]:
    """maxWorkers / concurrencyLimit / concurrencyTarget from ~/.zcode-commander/config.json;
    a missing or out-of-range value falls back to its default."""
    data = _read_json(_home(env) / ".zcode-commander" / "config.json") or {}
    out = {}
    for key, (default, low, high) in CONCURRENCY_SETTINGS.items():
        value = data.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        out[key] = value if ok else default
    return out


def configured_cap(settings: Mapping[str, int]) -> int:
    """``concurrencyTarget`` percent of a measured ``concurrencyLimit`` (at least 1),
    never above ``maxWorkers``; ``maxWorkers`` alone while the limit is unknown."""
    cap = settings["maxWorkers"]
    if settings["concurrencyLimit"]:
        cap = min(cap, max(1, settings["concurrencyLimit"] * settings["concurrencyTarget"] // 100))
    return cap


def zcode_db_path(env: Mapping[str, str]) -> Path:
    override = (env.get("ZCREW_ZCODE_DB") or "").strip()
    return Path(override) if override else _home(env) / ".zcode" / "cli" / "db" / "db.sqlite"


def recent_limit_errors(db_path: Path, since_ms: int) -> dict[str, int]:
    """Count of model requests per limit class since ``since_ms`` (read-only; {} when unreadable)."""
    import sqlite3
    from urllib.parse import quote

    counts: dict[str, int] = {}
    try:
        uri = "file:" + quote(Path(db_path).resolve().as_posix(), safe="/:") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (OSError, sqlite3.Error):
        return counts
    try:
        rows = conn.execute(
            "SELECT error_type, error_message FROM model_usage WHERE started_at >= ?"
            " AND (COALESCE(error_type, '') != '' OR COALESCE(error_message, '') != '')"
            " ORDER BY started_at DESC LIMIT %d" % LIMIT_ERROR_ROWS, (int(since_ms),),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for error_type, message in rows:
        kind = classify_limit_error(error_type, message)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def worker_cap(env: Mapping[str, str], now_ms: int | None = None) -> dict[str, Any]:
    """The cap in force now: the configured cap, one lower (never below 1) while
    the account answered 1302 in the last ``BACKOFF_WINDOW_MS``."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    settings = concurrency_settings(env)
    configured = configured_cap(settings)
    errors = recent_limit_errors(zcode_db_path(env), now - BACKOFF_WINDOW_MS)
    backoff = bool(errors.get("concurrency")) and configured > 1
    return {"cap": configured - 1 if backoff else configured, "configured": configured,
            "backoff": backoff, "settings": settings}


def patch_worker_slots(cls: Any, env: Mapping[str, str], *,
                       cap_fn: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
                       clock: Callable[[], float] = time.monotonic) -> None:
    """Wrap ResourceLeaseStore.try_acquire so every run also takes one free worker
    slot. Idempotent. The cap is re-read every ``CAP_CACHE_SECONDS``, so a config
    change applies to queued runs without restarting the bridge; a lowered cap
    never stops a running worker, it only holds new ones back."""
    if getattr(cls, "_zcrew_slots_patched", False):
        return
    original = getattr(cls, "try_acquire", None)
    if not callable(original):
        return
    cache: dict[str, Any] = {"at": None, "cap": None}

    def current_cap() -> int:
        now = clock()
        if cache["at"] is None or now - cache["at"] >= CAP_CACHE_SECONDS:
            try:
                cache["cap"] = max(1, int((cap_fn or worker_cap)(env)["cap"]))
            except Exception:  # noqa: BLE001 - a broken config must not stop every run
                cache["cap"] = CONCURRENCY_SETTINGS["maxWorkers"][0]
            cache["at"] = now
        return int(cache["cap"])

    def try_acquire(self: Any, run_id: Any, resource_modes: Any) -> Any:
        modes = {k: v for k, v in dict(resource_modes or {}).items() if not str(k).startswith(SLOT_PREFIX)}
        blockers: list[Any] = []
        for index in range(current_cap()):
            result = original(self, run_id, {**modes, SLOT_PREFIX + str(index): "exclusive"})
            if not isinstance(result, dict) or result.get("acquired"):
                return result
            blockers = list(result.get("blockers") or [])
            # a run holding this cwd blocks every slot alike: report only that
            held = [b for b in blockers if isinstance(b, dict) and not str(b.get("key", "")).startswith(SLOT_PREFIX)]
            if held:
                return {**result, "blockers": held}
        return {"acquired": False, "blockers": blockers[:12]}

    cls.try_acquire = try_acquire
    cls._zcrew_slots_patched = True


def apply_patches(bridge_root: Path, env: Mapping[str, str]) -> list[str]:
    root = str(bridge_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    applied: list[str] = []
    if not runtime_model_enabled(env):
        import control_plane

        control_plane.resolve_runtime_model = lambda *_args, **_kwargs: None
        applied.append("runtime-model-disabled")
    if account_provider_enabled(env):
        import zcode_protocol

        patch_protocol_client(zcode_protocol.ZCodeProtocolClient, env)
        applied.append("account-provider")
    if run_hooks_enabled(env):
        import backend_manager

        patch_run_hooks(backend_manager.BackendManager, env)
        applied.append("run-hooks")
    if compact_output_enabled(env):
        import backend_manager

        patch_backend_manager(backend_manager.BackendManager)
        applied.append("compact-output")
    if worker_slots_enabled(env):
        import resource_leases

        patch_worker_slots(resource_leases.ResourceLeaseStore, env)
        applied.append("worker-slots")
    return applied


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: bridge_compat.py <path/to/server.py> [args...]", file=sys.stderr)
        return 2
    server = Path(argv[1]).resolve()
    apply_patches(server.parent, os.environ)
    sys.argv = [str(server), *argv[2:]]
    runpy.run_path(str(server), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
