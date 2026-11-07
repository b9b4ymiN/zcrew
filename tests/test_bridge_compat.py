from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = _load("bridge_compat")

FAKE_CONTROL_PLANE = "def resolve_runtime_model(*args, **kwargs):\n    return {'model': 'upstream'}\n"
FAKE_PROTOCOL = (
    "import threading\n"
    "class ZCodeProtocolClient:\n"
    "    def start(self):\n        return 'orig-start'\n"
    "    def _handle_server_request(self, message):\n        return 'orig-handle'\n"
    "    def request(self, method, params=None, timeout=30):\n        return params\n"
)
FAKE_BACKEND_MANAGER = (
    "class BackendManager:\n"
    "    def wait(self, run_id, **kwargs):\n"
    "        return {'runId': run_id, 'status': 'running', 'revision': 1, 'createdAtMs': 5, 'backend': 'zcode'}\n"
    "    def configure(self, args):\n        return {'selectedBackend': 'zcode', 'createdAtMs': 5}\n"
)
FAKE_RESOURCE_LEASES = (
    "class ResourceLeaseStore:\n"
    "    def try_acquire(self, run_id, resource_modes):\n        return {'acquired': True, 'blockers': []}\n"
)
FAKE_KEY = "fake-key-not-a-secret"


class ApplyPatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "control_plane.py").write_text(FAKE_CONTROL_PLANE, encoding="utf-8")
        (self.root / "zcode_protocol.py").write_text(FAKE_PROTOCOL, encoding="utf-8")
        (self.root / "backend_manager.py").write_text(FAKE_BACKEND_MANAGER, encoding="utf-8")
        (self.root / "resource_leases.py").write_text(FAKE_RESOURCE_LEASES, encoding="utf-8")
        self._saved_path = list(sys.path)
        for name in ("control_plane", "zcode_protocol", "backend_manager", "resource_leases"):
            sys.modules.pop(name, None)

    def tearDown(self) -> None:
        for name in ("control_plane", "zcode_protocol", "backend_manager", "resource_leases"):
            sys.modules.pop(name, None)
        sys.path[:] = self._saved_path
        self._tmp.cleanup()

    def test_patches_applied_by_default(self) -> None:
        applied = compat.apply_patches(self.root, {})
        import control_plane
        import zcode_protocol

        self.assertEqual(applied, ["runtime-model-disabled", "account-provider", "run-hooks", "compact-output",
                                   "worker-slots"])
        import resource_leases

        self.assertTrue(resource_leases.ResourceLeaseStore._zcrew_slots_patched)
        import backend_manager

        manager = backend_manager.BackendManager()
        self.assertEqual(manager.wait("run_1"), {"runId": "run_1", "status": "running", "revision": 1})
        self.assertEqual(manager.configure({}), {"selectedBackend": "zcode", "createdAtMs": 5})
        self.assertIsNone(control_plane.resolve_runtime_model({"providerId": "p", "modelId": "m"}, "max"))
        self.assertTrue(zcode_protocol.ZCodeProtocolClient._commander_account_patched)

    def test_env_flags_keep_upstream(self) -> None:
        applied = compat.apply_patches(
            self.root,
            {
                "ZCODE_COMMANDER_RUNTIME_MODEL": "on",
                "ZCODE_COMMANDER_ACCOUNT_PROVIDER": "off",
                "ZCODE_COMMANDER_COMPACT": "off",
                "ZCODE_COMMANDER_RUN_HOOKS": "off",
                "ZCODE_COMMANDER_WORKER_SLOTS": "off",
            },
        )
        import control_plane
        import zcode_protocol

        self.assertEqual(applied, [])
        self.assertEqual(control_plane.resolve_runtime_model(), {"model": "upstream"})
        self.assertFalse(hasattr(zcode_protocol.ZCodeProtocolClient, "_commander_account_patched"))
        self.assertEqual(zcode_protocol.ZCodeProtocolClient().start(), "orig-start")
        import backend_manager

        self.assertIn("createdAtMs", backend_manager.BackendManager().wait("run_1"))


class FlagTests(unittest.TestCase):
    def test_runtime_model_flag_values(self) -> None:
        self.assertFalse(compat.runtime_model_enabled({}))
        self.assertFalse(compat.runtime_model_enabled({"ZCODE_COMMANDER_RUNTIME_MODEL": "off"}))
        self.assertTrue(compat.runtime_model_enabled({"ZCODE_COMMANDER_RUNTIME_MODEL": "ON"}))

    def test_account_provider_flag_values(self) -> None:
        self.assertTrue(compat.account_provider_enabled({}))
        self.assertTrue(compat.account_provider_enabled({"ZCODE_COMMANDER_ACCOUNT_PROVIDER": "on"}))
        self.assertFalse(compat.account_provider_enabled({"ZCODE_COMMANDER_ACCOUNT_PROVIDER": "OFF"}))


def _rule(pid: str, family: str, mode: str, models: list[str]) -> dict:
    return {
        "providerId": pid,
        "config": {
            "builtinModelIds": models,
            "access": {"type": "zhipu-account", "mode": mode, "accountType": family},
        },
    }


TABLE = {
    "revision": 30,
    "config": {
        "providerConfigRules": {
            "providerRules": [
                _rule("account:zai-individual-coding-plan", "zai", "individual-coding-plan", ["GLM-5.3", "GLM-5.3-Flash"]),
                _rule("account:zai-start-plan", "zai", "start-plan", ["GLM-5.2"]),
                _rule("account:bigmodel-individual-coding-plan", "bigmodel", "individual-coding-plan", ["GLM-5.3"]),
                {"providerId": "openai", "config": {"access": {"type": "api-key"}}},
            ]
        }
    },
}


class Fixture(unittest.TestCase):
    """Temp provider table + ~/.zcode/v2 files carrying a fake key."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.table_path = str(base / "zcode-builtin.json")
        Path(self.table_path).write_text(json.dumps(TABLE), encoding="utf-8")
        self.v2 = base / "v2"
        self.v2.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: dict) -> None:
        (self.v2 / name).write_text(json.dumps(data), encoding="utf-8")

    def write_defaults(self) -> None:
        self.write("coding-plan-cache.json", {"entryStatus": {"items": {
            "builtin:zai-coding-plan": {"status": "available"},
            "builtin:zai-start-plan": {"status": "unavailable"},
        }}})
        self.write("config.json", {"provider": {
            "builtin:zai-coding-plan": {"enabled": True, "options": {"apiKey": FAKE_KEY}},
            "builtin:zai-start-plan": {"enabled": False, "options": {"apiKey": FAKE_KEY}},
        }})


class PureHelperTests(Fixture):
    def test_revision_format_hashes_path(self) -> None:
        expected = hashlib.sha256(os.path.abspath(self.table_path).encode("utf-8")).hexdigest()
        self.assertEqual(compat.builtin_revision(self.table_path, 30), "zcode-builtin:30:" + expected)
        self.assertTrue(compat.builtin_revision(self.table_path, None).startswith("zcode-builtin:0:"))

    def test_revision_depends_on_path_not_bytes(self) -> None:
        other = str(Path(self.table_path).with_name("copy.json"))
        Path(other).write_text(json.dumps(TABLE), encoding="utf-8")
        self.assertNotEqual(compat.builtin_revision(self.table_path, 30), compat.builtin_revision(other, 30))

    def test_table_path_from_env(self) -> None:
        self.assertIsNone(compat.builtin_table_path({}))
        self.assertEqual(compat.builtin_table_path({compat.BUILTIN_PROVIDER_ENV: " C:/x.json "}), "C:/x.json")

    def test_id_mapping_both_ways(self) -> None:
        self.assertEqual(compat.account_provider_id(TABLE, "builtin:zai-coding-plan"), "account:zai-individual-coding-plan")
        self.assertEqual(compat.account_provider_id(TABLE, "builtin:zai-start-plan"), "account:zai-start-plan")
        self.assertEqual(compat.account_provider_id(TABLE, "builtin:nope-coding-plan"), "builtin:nope-coding-plan")
        self.assertEqual(compat.account_provider_id(TABLE, "custom:x"), "custom:x")
        self.assertEqual(compat.legacy_provider_id(TABLE, "account:zai-individual-coding-plan"), "builtin:zai-coding-plan")
        self.assertEqual(compat.legacy_provider_id(TABLE, "account:zai-start-plan"), "builtin:zai-start-plan")
        self.assertEqual(compat.legacy_provider_id(TABLE, "account:unknown"), "account:unknown")

    def test_entitlement_union(self) -> None:
        self.write("coding-plan-cache.json", {"entryStatus": {"items": {
            "builtin:zai-coding-plan": {"status": "available"},
            "builtin:bigmodel-coding-plan": {"status": "unavailable"},
        }}})
        self.write("config.json", {"provider": {
            "builtin:bigmodel-start-plan": {"enabled": True, "options": {"apiKey": FAKE_KEY}},
            "builtin:zai-start-plan": {"enabled": False, "options": {"apiKey": FAKE_KEY}},
            "builtin:bigmodel-coding-plan": {"enabled": True, "options": {}},
        }})
        self.write("setting.json", {"modelProviderFamilySelectedKeys": {"zai": "coding-plan:builtin:zai-team"}})
        self.assertEqual(
            compat.entitled_legacy_ids(self.v2), {"builtin:zai-coding-plan", "builtin:bigmodel-start-plan"}
        )

    def test_entitlement_setting_fallback_only_when_empty(self) -> None:
        self.write("setting.json", {"modelProviderFamilySelectedKeys": {"zai": "coding-plan:builtin:zai-coding-plan"}})
        self.assertEqual(compat.entitled_legacy_ids(self.v2), {"builtin:zai-coding-plan"})
        self.assertEqual(compat.entitled_legacy_ids(self.v2 / "missing"), set())

    def test_payload_shape(self) -> None:
        self.write_defaults()
        payload = compat.build_account_payload(self.table_path, self.v2, now_ms=lambda: 1234)
        self.assertEqual(payload["revision"], "account:bridge:1234")
        self.assertEqual(payload["basedOnZCodeBuiltinRevision"], compat.builtin_revision(self.table_path, 30))
        self.assertEqual(set(payload["providers"]), {
            "account:zai-individual-coding-plan",
            "account:zai-start-plan",
            "account:bigmodel-individual-coding-plan",
        })
        self.assertEqual(payload["providers"]["account:zai-individual-coding-plan"], {
            "builtinModelIds": ["GLM-5.3", "GLM-5.3-Flash"],
            "access": {"type": "zhipu-account", "entitled": True},
        })
        self.assertEqual(payload["states"]["account:zai-individual-coding-plan"],
                         {"availability": "available", "entitled": True, "current": True})
        self.assertEqual(payload["states"]["account:zai-start-plan"],
                         {"availability": "unavailable", "entitled": False, "current": False})
        self.assertNotIn(FAKE_KEY, json.dumps(payload))

    def test_payload_none_without_table(self) -> None:
        self.assertIsNone(compat.build_account_payload(None, self.v2))
        self.assertIsNone(compat.build_account_payload(str(self.v2 / "absent.json"), self.v2))

    def test_request_auth(self) -> None:
        self.write_defaults()
        auth = compat.request_auth_for(self.table_path, self.v2, "account:zai-individual-coding-plan")
        self.assertEqual(auth, {"apiKey": FAKE_KEY})
        for pid in ("account:zai-start-plan", "account:bigmodel-individual-coding-plan",
                    "account:unknown", "openai", None):
            self.assertIsNone(compat.request_auth_for(self.table_path, self.v2, pid), pid)

    def test_runtime_headers_result(self) -> None:
        self.write_defaults()
        ok = compat.runtime_headers_result(
            self.table_path, self.v2, {"modelSelection": {"providerId": "account:zai-individual-coding-plan"}})
        self.assertEqual(ok, {"headersApplied": True, "requestAuth": {"apiKey": FAKE_KEY}})
        ok2 = compat.runtime_headers_result(
            self.table_path, self.v2, {"providerId": "account:zai-individual-coding-plan"})
        self.assertTrue(ok2["headersApplied"])
        no = compat.runtime_headers_result(self.table_path, self.v2, {"providerId": "account:zai-start-plan"})
        self.assertFalse(no["headersApplied"])
        self.assertTrue(no["errorMessage"])


class _FakeProc:
    def poll(self):
        return None


class FakeClient:
    """Mimics ZCodeProtocolClient's start/request/_send contract (no real bridge)."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._proc = None
        self.spawns = 0
        self.requests: list[tuple[str, dict, int]] = []
        self.sent: list[dict] = []
        self.logs: list[str] = []
        self.logger = self.logs.append
        self.request_error: Exception | None = None

    def start(self):
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._proc = _FakeProc()
            self.spawns += 1

    def request(self, method, params=None, timeout=30):
        self.start()
        self.requests.append((method, params, timeout))
        if self.request_error is not None:
            raise self.request_error
        return {"status": "received", "providerCount": len((params or {}).get("providers", {}))}

    def _send(self, message):
        self.sent.append(message)

    def _handle_server_request(self, message):
        self.sent.append({"delegated": message.get("method")})


class _ProtocolError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class PatchTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_defaults()
        self.cls = type("Client", (FakeClient,), {})
        compat.patch_protocol_client(self.cls, {compat.BUILTIN_PROVIDER_ENV: self.table_path}, v2_dir=self.v2)

    def pushes(self, client: FakeClient) -> list:
        return [r for r in client.requests if r[0] == compat.UPDATE_ACCOUNT_METHOD]

    def test_request_injects_reasoning_level(self) -> None:
        client = self.cls()
        model = {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"}
        client.request("session/create", {"model": model, "thoughtLevel": "max"})
        sent = [r for r in client.requests if r[0] == "session/create"][0][1]
        self.assertEqual(sent["model"]["options"], {"reasoningLevel": "max"})

    def test_start_pushes_once_per_new_process(self) -> None:
        client = self.cls()
        client.start()
        client.start()
        client.request("session/create", {})
        self.assertEqual(len(self.pushes(client)), 1)
        self.assertEqual(client.requests[0][0], compat.UPDATE_ACCOUNT_METHOD)
        self.assertEqual(client.requests[0][2], 60)
        self.assertEqual(client.requests[1][0], "session/create")
        self.assertEqual(client.spawns, 1)
        client._proc = None  # app-server died: next start spawns and pushes again
        client.start()
        self.assertEqual(client.spawns, 2)
        self.assertEqual(len(self.pushes(client)), 2)
        self.assertFalse(any(FAKE_KEY in line for line in client.logs))

    def test_first_request_pushes_before_itself(self) -> None:
        client = self.cls()
        client.request("session/create", {})
        self.assertEqual([r[0] for r in client.requests], [compat.UPDATE_ACCOUNT_METHOD, "session/create"])

    def test_push_failure_is_logged_not_raised(self) -> None:
        client = self.cls()
        client.request_error = RuntimeError("boom")
        client.start()
        self.assertTrue(any("push failed" in line for line in client.logs))

    def test_method_not_found_is_quiet(self) -> None:
        client = self.cls()
        client.request_error = _ProtocolError("nope", -32601)
        client.start()
        self.assertTrue(any("old ZCode" in line for line in client.logs))
        self.assertFalse(any("push failed" in line for line in client.logs))

    def test_patch_is_idempotent(self) -> None:
        compat.patch_protocol_client(self.cls, {compat.BUILTIN_PROVIDER_ENV: self.table_path}, v2_dir=self.v2)
        client = self.cls()
        client.start()
        self.assertEqual(len(client.requests), 1)

    def test_headers_request_answered(self) -> None:
        client = self.cls()
        client._handle_server_request({
            "id": 7,
            "method": compat.RUNTIME_HEADERS_METHOD,
            "params": {"modelSelection": {"providerId": "account:zai-individual-coding-plan"}},
        })
        self.assertEqual(
            client.sent, [{"id": 7, "result": {"headersApplied": True, "requestAuth": {"apiKey": FAKE_KEY}}}]
        )
        self.assertFalse(any(FAKE_KEY in line for line in client.logs))

    def test_headers_request_declined_for_start_plan(self) -> None:
        client = self.cls()
        client._handle_server_request(
            {"id": "x", "method": compat.RUNTIME_HEADERS_METHOD, "params": {"providerId": "account:zai-start-plan"}}
        )
        self.assertEqual(client.sent[0]["id"], "x")
        self.assertFalse(client.sent[0]["result"]["headersApplied"])

    def test_other_requests_delegate(self) -> None:
        client = self.cls()
        client._handle_server_request({"id": 1, "method": "session/requestRuntimePreferences"})
        self.assertEqual(client.sent, [{"delegated": "session/requestRuntimePreferences"}])



class ReasoningLevelTests(unittest.TestCase):
    MODEL = {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"}

    def test_create_uses_thought_level(self) -> None:
        params = {"model": dict(self.MODEL), "thoughtLevel": "high"}
        out = compat.with_reasoning_level("session/create", params, "max")
        self.assertEqual(out["model"]["options"], {"reasoningLevel": "high"})
        self.assertNotIn("options", params["model"])

    def test_set_model_falls_back_to_default(self) -> None:
        out = compat.with_reasoning_level("session/setModel", {"sessionId": "s", "model": dict(self.MODEL)}, "max")
        self.assertEqual(out["model"]["options"]["reasoningLevel"], "max")

    def test_existing_level_kept(self) -> None:
        params = {"model": {**self.MODEL, "options": {"reasoningLevel": "low"}}, "thoughtLevel": "max"}
        self.assertIs(compat.with_reasoning_level("session/create", params, "max"), params)

    def test_other_methods_and_string_model_untouched(self) -> None:
        send = {"sessionId": "s", "content": "hi"}
        self.assertIs(compat.with_reasoning_level("session/send", send, "max"), send)
        string_model = {"model": "p/m"}
        self.assertIs(compat.with_reasoning_level("session/create", string_model, "max"), string_model)

    def test_default_level_env(self) -> None:
        self.assertEqual(compat.default_reasoning_level({}), "max")
        self.assertEqual(compat.default_reasoning_level({"ZCODE_COMMANDER_DEFAULT_REASONING": "high"}), "high")


LONG_PATH = "C:\\Users\\someone\\projects\\very-long-workspace-name\\.claude\\worktrees\\feature-branch-worktree"


def running_snapshot(**overrides: object) -> dict:
    usage = {
        "totalTokens": 60315, "inputTokens": 60112, "outputTokens": 203, "reasoningTokens": 0,
        "cacheReadTokens": 52288, "cacheWriteTokens": 0, "modelRequests": 2, "modelErrors": 0,
    }
    snap = {
        "runId": "run_0123456789abcdef", "status": "running", "revision": 47,
        "threadId": "sess_0123456789abcdef0123", "phase": "model", "elapsedMs": 31833,
        "createdAtMs": 1790000000000, "startedAtMs": 1790000000100, "finishedAtMs": None,
        "resources": [{"key": LONG_PATH, "mode": "exclusive"}],
        "resourceLease": {"scope": "cross-process", "acquired": True, "blockers": []},
        "native": {"status": "running", "stateRevision": 3, "eventSeq": 31,
                   "lastActivityAtMs": 1790000031000, "lastProgressAtMs": 1790000030000},
        "model": {"status": "running", "reasoningActive": False, "lastChannel": None,
                  "requestId": "req_0123456789abcdef0123456789",
                  "model": {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"},
                  "thoughtLevel": "max", "startedAt": 1790000001000},
        "usage": dict(usage),
        "sessionUsage": {**usage, "totalTokens": 120630},
        "counts": {"toolCalls": 2, "subagents": 0},
        "activeTools": [{"id": "call_0123456789abcdef", "name": "Bash", "status": "running",
                         "startedAt": 1790000030500}],
        "backgroundTasks": [], "controlFailures": [],
        "permissionPolicy": {"headless": True, "workspaceAccess": "exclusive",
                             "structuredPathsEnforced": True, "shellBoundary": "advisory",
                             "allowedRoots": [LONG_PATH], "rootModes": {LONG_PATH: "exclusive"}},
        "subagents": {"running": [], "ended": [], "endedTotal": 0},
        "context": {"window": 200000, "used": 0, "usedRatio": 0, "cacheHitRate": None},
        "goal": None,
        "lastCheckpoint": {"checkpointId": "ckpt_0123456789abcdef", "messageId": "msg_0123456789abcdef",
                           "targetMessageId": "msg_fedcba9876543210", "fileCount": 1, "scope": "workspace"},
        "lastSeq": 45, "next": "wait with afterRevision=47", "changed": True, "backend": "zcode",
    }
    snap.update(overrides)
    return snap


# Mirror of server.py RUN_OUTPUT_SCHEMA (required + property types).
RUN_OUTPUT_SCHEMA = {
    "properties": {
        "runId": str, "status": str, "revision": int, "threadId": (str, type(None)),
        "phase": str, "elapsedMs": int, "result": str,
    },
    "required": ["runId", "status", "revision"],
}


def serialize_like_upstream(result: object) -> dict:
    """Mirror of server.py AgentMcpServer._run_tool success path."""
    response = {
        "content": [{
            "type": "text",
            "text": result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False, separators=(",", ":")
            ),
        }],
        "isError": False,
    }
    if isinstance(result, dict):
        response["structuredContent"] = result
    return response


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CompactSnapshotTests(unittest.TestCase):
    def assertValidRunOutput(self, data: dict) -> None:
        for key in RUN_OUTPUT_SCHEMA["required"]:
            self.assertIn(key, data)
        for key, kind in RUN_OUTPUT_SCHEMA["properties"].items():
            if key in data:
                self.assertIsInstance(data[key], kind, key)

    def test_running_example_compacts_by_70_percent(self) -> None:
        snap = running_snapshot()
        out = compat.compact_snapshot("agent-wait", snap)
        self.assertEqual(out, {
            "runId": "run_0123456789abcdef", "status": "running", "revision": 47,
            "threadId": "sess_0123456789abcdef0123", "phase": "model", "elapsedMs": 31833,
            "model": {"modelId": "GLM-5.3", "thoughtLevel": "max", "status": "running"},
            "usage": {"totalTokens": 60315, "modelRequests": 2},
            "sessionTokens": 120630,
            "counts": {"toolCalls": 2},
            "activeTools": ["Bash"],
            "lastCheckpoint": {"fileCount": 1},
            "lastSeq": 45, "next": "wait with afterRevision=47", "changed": True,
        })
        before, after = len(dumps(snap)), len(dumps(out))
        self.assertLessEqual(after, before * 0.3, (before, after))
        self.assertNotIn("very-long-workspace-name", dumps(out))
        self.assertValidRunOutput(out)

    def test_input_not_mutated(self) -> None:
        snap = running_snapshot()
        copy = json.loads(json.dumps(snap))
        compat.compact_snapshot("agent-wait", snap)
        self.assertEqual(snap, copy)

    def test_terminal_result_kept_intact(self) -> None:
        text = "Summary line\n" + "x" * 5000
        snap = running_snapshot(status="completed", phase="completed", result=text, resultTruncated=True,
                                activeTools=[], changed=False,
                                next="observe for bounded detail, branch/compact, or close")
        out = compat.compact_snapshot("agent-wait", snap)
        self.assertEqual(out["result"], text)
        self.assertIs(out["resultTruncated"], True)
        self.assertIs(out["changed"], False)
        self.assertNotIn("activeTools", out)
        self.assertEqual(out["next"], "observe for bounded detail, branch/compact, or close")
        self.assertValidRunOutput(out)

    def test_error_run_keeps_error_fields(self) -> None:
        snap = running_snapshot(
            status="failed", error="model quota exceeded",
            model={"status": "error", "errorMessage": "429", "model": {"modelId": "GLM-5.3"}},
            usage={"totalTokens": 5, "modelRequests": 1, "modelErrors": 1, "lastError": "429", "reasoningTokens": 9},
            native={"status": "failed", "lastError": "boom"},
            futureWarning="new upstream warning", blockedReason="policy",
        )
        out = compat.compact_snapshot("agent-wait", snap)
        self.assertEqual(out["error"], "model quota exceeded")
        self.assertEqual(out["model"]["errorMessage"], "429")
        self.assertEqual(out["model"]["status"], "error")
        self.assertEqual(out["usage"]["lastError"], "429")
        self.assertEqual(out["usage"]["modelErrors"], 1)
        self.assertNotIn("reasoningTokens", out["usage"])
        self.assertEqual(out["futureWarning"], "new upstream warning")
        self.assertEqual(out["blockedReason"], "policy")
        self.assertNotIn("native", out)

    def test_non_empty_optional_sections_kept(self) -> None:
        snap = running_snapshot(
            controlFailures=[{"action": "guide", "error": "busy"}],
            backgroundTasks=[{"taskId": "t1"}],
            subagents={"running": [{"id": "a1"}], "ended": [{"id": "a0"}], "endedTotal": 1},
            goal={"objective": "ship"}, context={"usedRatio": 0.42, "window": 200000},
            model={"status": "running", "reasoningActive": True, "model": {"modelId": "m"}},
            alreadyManaged=True, counts={"toolCalls": 0, "subagents": 1},
        )
        out = compat.compact_snapshot("agent-recover", snap)
        self.assertEqual(out["controlFailures"], [{"action": "guide", "error": "busy"}])
        self.assertEqual(out["backgroundTasks"], [{"taskId": "t1"}])
        self.assertEqual(out["subagents"], {"running": [{"id": "a1"}]})
        self.assertEqual(out["goal"], {"objective": "ship"})
        self.assertEqual(out["context"], {"usedRatio": 0.42})
        self.assertIs(out["model"]["reasoningActive"], True)
        self.assertIs(out["alreadyManaged"], True)
        self.assertEqual(out["counts"], {"subagents": 1})
        idle = compat.compact_snapshot("agent-wait", running_snapshot(counts={"toolCalls": 0, "subagents": 0}))
        self.assertNotIn("counts", idle)

    def test_queued_run_keeps_resource_lease(self) -> None:
        lease = {"scope": "cross-process", "acquired": False, "blockers": [{"runId": "run_other", "pid": 42}]}
        out = compat.compact_snapshot("agent-start", running_snapshot(status="queued", resourceLease=lease))
        self.assertEqual(out["resourceLease"], {"acquired": False, "blockers": [{"runId": "run_other", "pid": 42}]})
        out = compat.compact_snapshot("agent-wait", running_snapshot(
            resourceLease={"scope": "cross-process", "acquired": True, "blockers": ["x"]}))
        self.assertEqual(out["resourceLease"], {"acquired": True, "blockers": ["x"]})

    def test_observe_events_kept_and_truncated(self) -> None:
        events = [
            {"seq": 44, "type": "tool.started", "atMs": 1, "detail": {"name": "Bash", "command": "y" * 1000}},
            {"seq": 45, "type": "text", "atMs": 2, "detail": "z" * 50},
            {"seq": 46, "type": "turn.completed", "atMs": 3},
        ]
        snap = running_snapshot(events=events, nextSeq=46, hasMoreEvents=False, eventsDropped=0,
                                nativeRefreshError="refresh failed")
        out = compat.compact_snapshot("agent-observe", snap)
        self.assertEqual([e["seq"] for e in out["events"]], [44, 45, 46])
        self.assertEqual([e["type"] for e in out["events"]], ["tool.started", "text", "turn.completed"])
        self.assertTrue(all("atMs" not in e for e in out["events"]))
        command = out["events"][0]["detail"]["command"]
        self.assertTrue(command.startswith("y" * 300))
        self.assertLess(len(command), 330)
        self.assertEqual(out["events"][0]["detail"]["name"], "Bash")
        self.assertEqual(out["events"][1]["detail"], "z" * 50)
        self.assertEqual(out["nextSeq"], 46)
        self.assertIs(out["hasMoreEvents"], False)
        self.assertNotIn("eventsDropped", out)
        self.assertEqual(out["nativeRefreshError"], "refresh failed")
        dropped = compat.compact_snapshot("agent-observe", running_snapshot(eventsDropped=3))
        self.assertEqual(dropped["eventsDropped"], 3)

    def test_revision_zero_and_null_thread_keep_required(self) -> None:
        out = compat.compact_snapshot("agent-start", running_snapshot(revision=0, threadId=None, status="queued"))
        self.assertEqual((out["revision"], out["status"]), (0, "queued"))
        self.assertNotIn("threadId", out)
        self.assertValidRunOutput(out)

    def test_passthrough_for_other_tools_and_shapes(self) -> None:
        snap = running_snapshot()
        for tool in ("agent-config", "agent-context", "agent-branch", "unknown"):
            self.assertIs(compat.compact_snapshot(tool, snap), snap)
        for value in ("plain text", None, [1, 2], {"sessions": [], "count": 0},
                      {"threadId": "t", "status": "closed", "native": {}}):
            self.assertIs(compat.compact_snapshot("agent-recover", value), value)
            self.assertIs(compat.compact_snapshot("agent-close", value), value)

    def test_duplicated_content_and_structured_content_stay_identical(self) -> None:
        compacted = compat.compact_snapshot("agent-wait", running_snapshot())
        response = serialize_like_upstream(compacted)
        self.assertEqual(json.loads(response["content"][0]["text"]), response["structuredContent"])
        self.assertValidRunOutput(response["structuredContent"])
        upstream = serialize_like_upstream(running_snapshot())
        self.assertLessEqual(len(dumps(response)), len(dumps(upstream)) * 0.3)

    def test_env_flag(self) -> None:
        self.assertTrue(compat.compact_output_enabled({}))
        self.assertTrue(compat.compact_output_enabled({"ZCODE_COMMANDER_COMPACT": "on"}))
        for value in ("off", "OFF", "0", "false", "no"):
            self.assertFalse(compat.compact_output_enabled({"ZCODE_COMMANDER_COMPACT": value}))


class _FakeManager:
    def __init__(self) -> None:
        self.snap = running_snapshot()

    def start(self, args):
        return self.snap

    def wait(self, run_id, **kwargs):
        return self.snap

    def observe(self, run_id, **kwargs):
        return self.snap

    def control(self, run_id, action, **kwargs):
        return self.snap

    def recover(self, args):
        return {"sessions": [], "count": 0}

    def close_run(self, run_id=None, *, thread_id=None):
        return self.snap

    def configure(self, args):
        return self.snap

    def context(self, run_id, **kwargs):
        return self.snap


class PatchBackendManagerTests(unittest.TestCase):
    def test_wraps_run_tools_only_and_is_idempotent(self) -> None:
        cls = type("Manager", (_FakeManager,), {})
        compat.patch_backend_manager(cls)
        first = cls.wait
        compat.patch_backend_manager(cls)
        self.assertIs(cls.wait, first)
        manager = cls()
        calls = (
            lambda: manager.start({}), lambda: manager.wait("r", after_revision=1),
            lambda: manager.observe("r"), lambda: manager.control("r", "cancel"),
            lambda: manager.close_run("r"),
        )
        for call in calls:
            out = call()
            self.assertNotIn("permissionPolicy", out)
            self.assertEqual(out["sessionTokens"], 120630)
        self.assertEqual(manager.recover({}), {"sessions": [], "count": 0})
        self.assertIs(manager.configure({}), manager.snap)
        self.assertIs(manager.context("r"), manager.snap)

    def test_compaction_failure_returns_original(self) -> None:
        cls = type("Manager", (_FakeManager,), {})
        compat.patch_backend_manager(cls)
        manager = cls()
        original = compat.compact_snapshot

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        compat.compact_snapshot = explode
        try:
            self.assertIs(manager.wait("r"), manager.snap)
        finally:
            compat.compact_snapshot = original

    def test_errors_propagate(self) -> None:
        class Failing(_FakeManager):
            def wait(self, run_id, **kwargs):
                raise LookupError("run not found")

        compat.patch_backend_manager(Failing)
        with self.assertRaises(LookupError):
            Failing().wait("r")


CONTRACT = """OBJECTIVE
Add the hero section.

SCOPE
- `src/hero/**`
- tests for Hero when needed

DO NOT
- touch `package.json` or `migrations/`

ACCEPTANCE CRITERIA
1. The title shows.

VERIFY
- `npm test -- Hero`
"""


class _HookManager:
    """Stands in for BackendManager: start() hands out run snapshots, wait() replays states."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.states: list[str] = ["running"]
        self.controls: list[tuple] = []
        self.thread = None

    def start(self, args):
        self.started.append(dict(args))
        return {"runId": "run_1", "status": "queued", "revision": 1, "threadId": self.thread}

    def wait(self, run_id, **kwargs):
        status = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"runId": run_id, "status": status, "revision": 2}

    def control(self, run_id, action, **kwargs):
        self.controls.append((run_id, action, kwargs))
        return {"runId": run_id, "status": "stopping", "revision": 3}

    def close_run(self, run_id=None, *, thread_id=None):
        return {"runId": run_id or "run_1", "status": "closed", "revision": 4}


class ContractParseTests(unittest.TestCase):
    def test_sections_and_machine_readable_parts(self) -> None:
        c = compat.parse_contract(CONTRACT)
        self.assertEqual(c["missing"], [])
        self.assertEqual(c["scope"], ["src/hero/**"])
        self.assertEqual(c["do_not"], ["package.json", "migrations/"])
        self.assertEqual(c["verify"], ["npm test -- Hero"])
        self.assertEqual(c["sections"]["SCOPE"], ["`src/hero/**`", "tests for Hero when needed"])
        self.assertEqual(c["sections"]["ACCEPTANCE CRITERIA"], ["The title shows."])

    def test_missing_sections_are_named(self) -> None:
        c = compat.parse_contract("OBJECTIVE\nDo it.\n\nSCOPE\n- src/\n")
        self.assertEqual(c["missing"], ["DO NOT", "ACCEPTANCE CRITERIA", "VERIFY"])
        self.assertEqual(c["scope"], [])  # no backticks: left to the reviewer

    def test_markdown_headings_and_bold(self) -> None:
        c = compat.parse_contract("## OBJECTIVE\nx\n**SCOPE:**\n- `a/b.py`\n### VERIFY\n- `pytest`\n")
        self.assertEqual((c["scope"], c["verify"]), (["a/b.py"], ["pytest"]))

    def test_inline_section_text(self) -> None:
        c = compat.parse_contract(
            "OBJECTIVE: Add a line.\nSCOPE: `scripts/doctor.py` only.\nDO NOT: touch other files.\n"
            "ACCEPTANCE CRITERIA: line 1 is the comment.\n**VERIFY:** `python -m py_compile scripts/doctor.py`\n"
            "CONTEXT shows how OBJECTIVE text reads.\n"
        )
        self.assertEqual(c["missing"], [])
        self.assertEqual(c["scope"], ["scripts/doctor.py"])
        self.assertEqual(c["verify"], ["python -m py_compile scripts/doctor.py"])
        self.assertEqual(c["sections"]["OBJECTIVE"], ["Add a line."])

    def test_heading_with_a_note_in_parentheses(self) -> None:
        # issue #2: "SCOPE (may change)" was not recognised
        c = compat.parse_contract("SCOPE (may change)\n- `src/a.py`\n- docs\n")
        self.assertEqual(c["sections"]["SCOPE"], ["`src/a.py`", "docs"])
        self.assertEqual(c["scope"], ["src/a.py"])
        self.assertNotIn("SCOPE", c["missing"])
        for text in (
            "SCOPE (may change):\n- `src/a.py`\n",
            "**SCOPE (files):** `src/a.py`\n",
            "**SCOPE (files)**:\n- `src/a.py`\n",
            "## SCOPE (files)\n- `src/a.py`\n",
            "SCOPE(files): `src/a.py`\n",
        ):
            self.assertEqual(compat.parse_contract(text)["scope"], ["src/a.py"], text)

    def test_near_miss_headings_are_reported_only_for_missing_sections(self) -> None:
        c = compat.parse_contract("OBJECTIVE\nx\nSCOPE - files\n- `a/b.py`\n")
        self.assertIn("SCOPE", c["missing"])
        self.assertEqual(c["near_misses"], {"SCOPE": "SCOPE - files"})
        # the section exists, so a prose line starting with its name is not a near miss
        c = compat.parse_contract(CONTRACT + "\nDO NOT touch the lockfile either\n")
        self.assertEqual((c["missing"], c["near_misses"]), ([], {}))
        # optional sections never produce near misses
        self.assertEqual(compat.parse_contract("CONTEXT shows how it reads\n")["near_misses"], {})

    def test_non_path_backticks_are_not_scope(self) -> None:
        c = compat.parse_contract("SCOPE\n- the `Hero` component\n")
        self.assertEqual(c["scope"], [])

    def test_timebox(self) -> None:
        self.assertEqual(compat.parse_timebox("a\nTIMEBOX: 40m\nb"), 2400)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 90s"), 90)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 1h"), 3600)
        self.assertIsNone(compat.parse_timebox("no limit here"))

    def test_timebox_anywhere_on_a_line(self) -> None:
        # issue #2: a TIMEBOX at the end of the first line was silently ignored
        self.assertEqual(compat.parse_timebox("TASK X \u2014 do something. TIMEBOX: 60m"), 3600)
        self.assertEqual(compat.parse_timebox("TIMEBOX:90s"), 90)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 2h and more"), 7200)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 45 min"), 2700)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 2 hours"), 7200)
        self.assertEqual(compat.parse_timebox("TIMEBOX: 45"), 2700)  # no unit: minutes
        self.assertEqual(compat.parse_timebox("TIMEBOX: 30M"), 1800)  # the unit may be any case

    def test_timebox_lookalikes_are_not_matched(self) -> None:
        for text in ("timebox: 60m", "MYTIMEBOX: 5m", "X-TIMEBOX: 5m", "TIMEBOX: soon", "TIMEBOX 60m", "TIMEBOX: 5x"):
            self.assertIsNone(compat.parse_timebox(text), text)

    def test_first_timebox_wins(self) -> None:
        self.assertEqual(compat.parse_timebox("TIMEBOX: 10m then TIMEBOX: 20m"), 600)


class RunHookFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = {"ZCODE_COMMANDER_STATE": str(self.root / "state"), "HOME": str(self.root), "USERPROFILE": str(self.root)}
        self._saved = dict(os.environ)
        os.environ.update(self.env)
        self.work = self.root / "repo"
        self.work.mkdir()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def config(self, **values: object) -> None:
        path = self.work / ".claude"
        path.mkdir(exist_ok=True)
        (path / "zcode-commander.json").write_text(json.dumps(values), encoding="utf-8")

    def patched(self) -> _HookManager:
        cls = type("Manager", (_HookManager,), {})
        compat.patch_run_hooks(cls, self.env)
        return cls()


class WrapUpDelayTests(RunHookFixture):
    def test_default_and_caps(self) -> None:
        self.assertEqual(compat.wrap_up_delay({"cwd": str(self.work), "timeout": 1800}, self.env), 1200)
        self.assertEqual(compat.wrap_up_delay({"cwd": str(self.work), "timeout": 900}, self.env), 780)  # timeout - 120
        self.assertIsNone(compat.wrap_up_delay({"cwd": str(self.work), "timeout": 150}, self.env))  # too short

    def test_bridge_default_timeout_when_none_given(self) -> None:
        env = {**self.env, "AGENT_MCP_TIMEOUT": "600"}
        self.assertEqual(compat.wrap_up_delay({"cwd": str(self.work)}, env), 480)

    def test_config_zero_turns_off_and_timebox_overrides(self) -> None:
        self.config(wrapUpSeconds=0)
        self.assertIsNone(compat.wrap_up_delay({"cwd": str(self.work), "timeout": 1800}, self.env))
        args = {"cwd": str(self.work), "timeout": 1800, "prompt": "TIMEBOX: 10m"}
        self.assertEqual(compat.wrap_up_delay(args, self.env), 600)

    def test_bad_config_value_falls_back(self) -> None:
        self.config(wrapUpSeconds="soon", gateMode="loud")
        self.assertEqual(compat.effective_setting(str(self.work), "wrapUpSeconds", self.env), 1200)
        self.assertEqual(compat.effective_setting(str(self.work), "gateMode", self.env), "warn")


class WatchForWrapUpTests(unittest.TestCase):
    def run_watch(self, states: list[str], delay: int = 100) -> tuple[bool, _HookManager, list[float]]:
        manager = _HookManager()
        manager.states = states
        now = [0.0]
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        sent = compat.watch_for_wrap_up(manager, "run_1", delay, sleep=sleep, clock=lambda: now[0])
        return sent, manager, slept

    def test_interrupts_once_after_delay_of_running_time(self) -> None:
        sent, manager, slept = self.run_watch(["queued", "queued", "running"], delay=100)
        self.assertTrue(sent)
        self.assertEqual(len(manager.controls), 1)
        run_id, action, kwargs = manager.controls[0]
        self.assertEqual((run_id, action, kwargs["if_status"]), ("run_1", "interrupt", "running"))
        self.assertTrue(kwargs["prompt"].startswith(compat.WRAP_UP_MARK))
        self.assertGreaterEqual(sum(slept), 100)  # counted from when it started running, not from queueing

    def test_finished_run_is_left_alone(self) -> None:
        sent, manager, _ = self.run_watch(["running", "running", "completed"], delay=10_000)
        self.assertFalse(sent)
        self.assertEqual(manager.controls, [])

    def test_turn_that_ended_idle_is_left_alone(self) -> None:
        sent, manager, _ = self.run_watch(["running", "idle"], delay=10_000)
        self.assertFalse(sent)
        self.assertEqual(manager.controls, [])

    def test_gives_up_after_a_day(self) -> None:
        sent, manager, slept = self.run_watch(["queued"], delay=100)
        self.assertFalse(sent)
        self.assertEqual(manager.controls, [])
        self.assertGreaterEqual(sum(slept), 86400)

    def test_failed_interrupt_is_swallowed(self) -> None:
        manager = _HookManager()

        def refuse(*_a: object, **_k: object) -> None:
            raise RuntimeError("stale")

        manager.control = refuse  # type: ignore[method-assign]
        logs: list[str] = []
        now = [0.0]
        sent = compat.watch_for_wrap_up(manager, "r", 5, sleep=lambda s: now.__setitem__(0, now[0] + s),
                                        clock=lambda: now[0], log=logs.append)
        self.assertFalse(sent)
        self.assertIn("wrap-up interrupt failed", logs[0])


class ResolveTimeboxTests(RunHookFixture):
    def resolve(self, prompt: str, **over: object) -> "compat.Timebox":
        return compat.resolve_timebox({"prompt": prompt, "cwd": str(self.work), "timeout": 1800, **over}, self.env)

    def test_issue_2_inline_timebox_is_applied_and_reported(self) -> None:
        tb = self.resolve("TASK P2-T12 \u2014 Guard rules. TIMEBOX: 60m", timeout=5400)
        self.assertEqual((tb.seconds, tb.source, tb.warnings), (3600, "contract", []))
        self.assertEqual(compat.wrap_up_delay({"prompt": "TASK X \u2014 do it. TIMEBOX: 60m", "timeout": 5400}, self.env), 3600)

    def test_without_timebox_the_config_applies(self) -> None:
        tb = self.resolve("OBJECTIVE\nx")
        self.assertEqual((tb.seconds, tb.source, tb.warnings), (1200, "config", []))
        self.config(wrapUpSeconds=0)
        self.assertEqual(self.resolve("OBJECTIVE\nx")[:2], (None, "off"))

    def test_unparsable_timebox_warns_and_falls_back_to_config(self) -> None:
        for text in ("timebox: 60m", "TIMEBOX: soon"):
            tb = self.resolve(text)
            self.assertEqual((tb.seconds, tb.source), (1200, "config"), text)
            self.assertEqual(len(tb.warnings), 1, text)
            self.assertIn("TIMEBOX found but not understood", tb.warnings[0])
            self.assertIn("wrapUpSeconds=1200", tb.warnings[0])

    def test_several_different_values_warn(self) -> None:
        tb = self.resolve("TIMEBOX: 10m\nTIMEBOX: 20m")
        self.assertEqual(tb.seconds, 600)
        self.assertEqual(tb.warnings, ["several TIMEBOX values; using the first (10m)"])
        self.assertEqual(self.resolve("TIMEBOX: 10m\nTIMEBOX: 10m").warnings, [])

    def test_timebox_longer_than_the_timeout_is_capped_with_a_warning(self) -> None:
        tb = self.resolve("TIMEBOX: 60m", timeout=1800)
        self.assertEqual((tb.seconds, tb.source), (1680, "contract"))
        self.assertEqual(tb.warnings, ["TIMEBOX 60m is longer than the run timeout 30m; wrap-up at 28m instead (raise timeout)"])

    def test_too_short_timebox_means_no_time_box(self) -> None:
        for text, over in (("TIMEBOX: 30s", {}), ("TIMEBOX: 10m", {"timeout": 150})):
            tb = self.resolve(text, **over)
            self.assertEqual((tb.seconds, tb.source), (None, "off"), text)
            self.assertIn("TIMEBOX too short", tb.warnings[0])

    def test_notice_names_the_wrap_up_clock_time(self) -> None:
        now = time.mktime((2026, 10, 18, 10, 5, 0, 0, 0, -1))
        notice = compat.timebox_notice(3600, now)
        self.assertIn("about 60 minutes (wrap-up near 11:05 local time)", notice)
        self.assertIn("Run the VERIFY commands before then", notice)


class PatchRunHooksTests(RunHookFixture):
    def start_args(self, **over: object) -> dict:
        return {"prompt": CONTRACT, "cwd": str(self.work), "timeout": 1800, **over}

    def run_start(self, manager: _HookManager, args: dict) -> dict:
        # stub only compat's Thread (the wrap-up watcher): subprocess on Windows needs real threads
        real = compat.threading
        compat.threading = SimpleNamespace(  # type: ignore[assignment]
            Thread=lambda **kw: type("T", (), {"start": lambda self: None})())
        try:
            return manager.start(args)
        finally:
            compat.threading = real  # type: ignore[assignment]

    def test_idempotent_and_skips_incomplete_managers(self) -> None:
        cls = type("Manager", (_HookManager,), {})
        compat.patch_run_hooks(cls, self.env)
        first = cls.start
        compat.patch_run_hooks(cls, self.env)
        self.assertIs(cls.start, first)
        compat.patch_run_hooks(type("Bare", (), {}), self.env)  # no start/close_run: no error

    def test_first_start_records_contract_and_adds_timebox_and_hint(self) -> None:
        manager = self.patched()
        result = self.run_start(manager, self.start_args())
        self.assertIn("[zcrew] Time box: about 20 minutes", manager.started[0]["prompt"])
        self.assertTrue(manager.started[0]["prompt"].startswith(CONTRACT))
        self.assertEqual(result["gate"]["verify"], "before accepting, run: zcrew gate run_1")
        self.assertNotIn("warnings", result["gate"])
        [record_path] = list((self.root / "state" / "gate").glob("*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["runIds"], ["run_1"])
        self.assertEqual(record["contract"]["verify"], ["npm test -- Hero"])
        self.assertEqual(record["rounds"], 1)
        self.assertEqual(record["verdicts"], [])

    def test_start_result_reports_the_effective_timebox(self) -> None:
        manager = self.patched()
        result = self.run_start(manager, self.start_args(prompt=CONTRACT + "\nTASK Z. TIMEBOX: 25m", timeout=5400))
        self.assertEqual(result["gate"]["timebox"], {"seconds": 1500, "source": "contract"})
        self.assertIn("about 25 minutes", manager.started[0]["prompt"])
        [record_path] = list((self.root / "state" / "gate").glob("*.json"))
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8"))["timebox"],
                         {"seconds": 1500, "source": "contract"})

    def test_unrecognised_heading_is_named_in_the_warning(self) -> None:
        manager = self.patched()
        prompt = CONTRACT.replace("SCOPE\n", "SCOPE - what may change\n", 1)
        result = self.run_start(manager, self.start_args(prompt=prompt))
        warning = result["gate"]["warnings"][0]
        self.assertIn("contract is missing: SCOPE", warning)
        self.assertIn('heading not recognised: "SCOPE - what may change" - write "SCOPE:" or "SCOPE (note):"', warning)

    def test_timebox_warnings_join_the_gate_warnings(self) -> None:
        manager = self.patched()
        result = self.run_start(manager, self.start_args(prompt="no sections, timebox: 5m"))
        warnings = result["gate"]["warnings"]
        self.assertIn("contract is missing", warnings[0])
        self.assertIn("TIMEBOX found but not understood", warnings[1])

    def test_gate_mode_off_still_reports_timebox_warnings(self) -> None:
        self.config(gateMode="off")
        manager = self.patched()
        result = self.run_start(manager, self.start_args(prompt=CONTRACT + "\nTIMEBOX: 60m", timeout=1800))
        self.assertEqual(result["gate"]["timebox"], {"seconds": 1680, "source": "contract"})
        self.assertIn("longer than the run timeout", result["gate"]["warnings"][0])
        self.assertNotIn("verify", result["gate"])

    def test_wrap_up_zero_reports_no_time_box(self) -> None:
        self.config(wrapUpSeconds=0)
        manager = self.patched()
        result = self.run_start(manager, self.start_args())
        self.assertEqual(result["gate"]["timebox"], {"seconds": None, "source": "off"})

    def test_incomplete_contract_warns(self) -> None:
        manager = self.patched()
        result = self.run_start(manager, self.start_args(prompt="Just fix the bug please"))
        self.assertIn("OBJECTIVE", result["gate"]["warnings"][0])
        self.assertIn("VERIFY", result["gate"]["warnings"][0])

    def test_correction_bumps_round_and_adds_no_notice(self) -> None:
        manager = self.patched()
        manager.thread = "sess_abc"
        self.run_start(manager, self.start_args())
        compat.note_snapshot({"runId": "run_1", "status": "running", "revision": 2, "threadId": "sess_abc"}, self.env)
        manager.started.clear()
        result = self.run_start(manager, self.start_args(prompt="REVIEW RESULT: FAIL", threadId="sess_abc"))
        self.assertEqual(manager.started[0]["prompt"], "REVIEW RESULT: FAIL")
        self.assertNotIn("warnings", result.get("gate", {}))
        [record_path] = list((self.root / "state" / "gate").glob("*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual((record["rounds"], record["runIds"]), (2, ["run_1", "run_1"]))

    def test_unknown_thread_creates_no_record(self) -> None:
        manager = self.patched()
        self.run_start(manager, self.start_args(prompt="continue", threadId="sess_old"))
        self.assertFalse((self.root / "state" / "gate").exists())

    def test_note_snapshot_learns_thread_id(self) -> None:
        manager = self.patched()
        self.run_start(manager, self.start_args())
        compat.note_snapshot({"runId": "run_1", "status": "running", "revision": 2, "threadId": "sess_xyz"}, self.env)
        found = compat.find_record(self.env, thread_id="sess_xyz")
        self.assertIsNotNone(found)

    def test_gate_mode_off_changes_nothing_but_the_timebox(self) -> None:
        self.config(gateMode="off")
        manager = self.patched()
        result = self.run_start(manager, self.start_args())
        self.assertEqual(result["gate"], {"timebox": {"seconds": 1200, "source": "config"}})  # no verify hint
        self.assertFalse((self.root / "state" / "gate").exists())
        self.assertEqual(manager.close_run("run_1"), {"runId": "run_1", "status": "closed", "revision": 4})

    def test_wrap_up_zero_leaves_prompt_untouched(self) -> None:
        self.config(wrapUpSeconds=0)
        manager = self.patched()
        self.run_start(manager, self.start_args())
        self.assertEqual(manager.started[0]["prompt"], CONTRACT)

    def test_close_warns_until_a_verdict_exists(self) -> None:
        manager = self.patched()
        self.run_start(manager, self.start_args())
        warned = manager.close_run("run_1")
        self.assertIn("zcrew gate run_1", warned["gate"]["warnings"][0])
        path, record = compat.find_record(self.env, run_id="run_1")  # type: ignore[misc]
        record["verdicts"].append({"verdict": "accept"})
        compat.save_record(path, record)
        self.assertNotIn("gate", manager.close_run("run_1"))

    def test_hook_failures_never_break_start(self) -> None:
        manager = self.patched()
        original = compat.git_snapshot
        compat.git_snapshot = lambda cwd: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        try:
            result = self.run_start(manager, self.start_args())
        finally:
            compat.git_snapshot = original
        self.assertEqual(result["runId"], "run_1")


class ChildStdinTests(unittest.TestCase):
    """The bridge's serve loop blocks reading stdin; on Windows a child that inherits
    that pipe cannot start until the next MCP message arrives (agent-start hung)."""

    def test_every_subprocess_call_sets_stdin(self) -> None:
        import ast

        tree = ast.parse((SCRIPTS / "bridge_compat.py").read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
            and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        ]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("stdin", {kw.arg for kw in call.keywords}, "line %d" % call.lineno)


if __name__ == "__main__":
    unittest.main()
