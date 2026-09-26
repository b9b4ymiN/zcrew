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
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

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
