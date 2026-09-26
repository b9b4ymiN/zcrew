#!/usr/bin/env python3
"""Zero-model-cost local checks for the Claude -> MCP -> ZCode wiring."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "zcode_bridge_launcher.py"
HOME = Path.home()
BRIDGE = Path(os.environ.get("CODER_MCP_BRIDGE_ROOT", HOME / ".zcode-commander" / "coder-mcp-bridge"))
CLI_CONFIG_HINT = (
    "run install.ps1 -EnsureZCodeCliConfig "
    "(copies provider credentials from ZCode Desktop into the CLI config)"
)


class CheckResult(NamedTuple):
    name: str
    ok: bool
    detail: str


def check(name: str, ok: bool, detail: str) -> bool:
    print(("[OK]   " if ok else "[FAIL] ") + f"{name}: {detail}")
    return ok


def report(result: CheckResult) -> bool:
    return check(result.name, result.ok, result.detail)


def check_probe_output(stdout: str, stderr: str, returncode: int) -> CheckResult:
    try:
        payload = json.loads(stdout)
        zcode = payload["availableBackends"]["zcode"]
    except (ValueError, KeyError, TypeError):
        detail = (stderr.strip() or stdout.strip() or f"exit {returncode}")[:600]
        return CheckResult("bridge probe", False, detail)
    if zcode.get("available") is True:
        return CheckResult("bridge probe", returncode == 0, f"zcode backend available (exit {returncode})")
    return CheckResult("bridge probe", False, f"zcode backend unavailable: {zcode.get('reason') or 'no reason given'}")


def info(name: str, detail: str) -> None:
    print(f"[INFO] {name}: {detail}")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _providers(config: Any) -> dict[str, Any]:
    providers = config.get("provider") if isinstance(config, dict) else None
    return providers if isinstance(providers, dict) else {}


def check_desktop_config(path: Path) -> CheckResult:
    name = "ZCode desktop config"
    if not path.is_file():
        return CheckResult(name, False, f"{path} not found")
    try:
        load_json(path)
    except (OSError, ValueError) as exc:
        return CheckResult(name, False, f"{path} unreadable ({type(exc).__name__})")
    return CheckResult(name, True, str(path))


def check_cli_config(path: Path) -> CheckResult:
    name = "ZCode CLI config"
    if not path.is_file():
        return CheckResult(name, False, f"{path} not found; {CLI_CONFIG_HINT}")
    try:
        config = load_json(path)
    except (OSError, ValueError) as exc:
        return CheckResult(name, False, f"{path} unreadable ({type(exc).__name__}); {CLI_CONFIG_HINT}")

    model = config.get("model") if isinstance(config, dict) else None
    main = model.get("main") if isinstance(model, dict) else None
    if not isinstance(main, str) or not main.strip():
        return CheckResult(name, False, f"model.main is not set in {path}; {CLI_CONFIG_HINT}")
    provider_id, sep, model_id = main.partition("/")
    if not sep or not provider_id or not model_id:
        return CheckResult(
            name, False, f"model.main={main!r} is not in 'provider/model' form; {CLI_CONFIG_HINT}"
        )
    if provider_id not in _providers(config):
        return CheckResult(
            name, False, f"model.main={main} but provider {provider_id!r} is not configured; {CLI_CONFIG_HINT}"
        )
    return CheckResult(name, True, f"model.main={main} ({path})")


def _model_ids(models: Any) -> list[str]:
    if isinstance(models, dict):
        return [str(key) for key in models]
    if isinstance(models, list):
        ids = []
        for item in models:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
        return ids
    return []


def _has_api_key(provider: dict[str, Any]) -> bool:
    options = provider.get("options")
    key = options.get("apiKey") if isinstance(options, dict) else None
    return bool(key or provider.get("apiKey"))


def describe_desktop_providers(path: Path) -> str:
    try:
        config = load_json(path)
    except (OSError, ValueError) as exc:
        return f"cannot read {path} ({type(exc).__name__})"
    lines = [
        f"{provider_id}: {', '.join(_model_ids(provider.get('models'))) or '(no models)'}"
        for provider_id, provider in _providers(config).items()
        if isinstance(provider, dict) and provider.get("enabled") is True and _has_api_key(provider)
    ]
    return "; ".join(lines) if lines else "none enabled with an API key"


def main() -> int:
    ok = True
    ok &= check("python", bool(sys.executable), sys.executable)
    ok &= check("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    ok &= check("claude", shutil.which("claude") is not None, shutil.which("claude") or "not found")
    ok &= check("bridge", (BRIDGE / "server.py").is_file(), str(BRIDGE))

    # Import launcher functions without starting MCP.
    import importlib.util
    spec = importlib.util.spec_from_file_location("launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    try:
        bundle = mod.find_zcode_bundle()
        runtime = mod.find_zcode_runtime(bundle)
        ok &= check("zcode bundle", True, str(bundle))
        ok &= check("zcode runtime", True, str(runtime))
    except Exception as exc:
        ok &= check("zcode discovery", False, str(exc))
        bundle = runtime = None

    gui_cfg = HOME / ".zcode" / "v2" / "config.json"
    cli_cfg = HOME / ".zcode" / "cli" / "config.json"
    ok &= report(check_desktop_config(gui_cfg))
    if gui_cfg.is_file():
        info("ZCode desktop providers", describe_desktop_providers(gui_cfg))
    ok &= report(check_cli_config(cli_cfg))

    if (BRIDGE / "server.py").is_file() and bundle and runtime:
        env = mod.build_env(os.environ, bundle, runtime)
        try:
            probe = subprocess.run(
                [sys.executable, str(BRIDGE / "server.py"), "--probe"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            ok &= report(check_probe_output(probe.stdout, probe.stderr, probe.returncode))
        except Exception as exc:
            ok &= check("bridge probe", False, str(exc))

    if shutil.which("claude"):
        try:
            claude_cmd = shutil.which("claude") or "claude"
            argv = [claude_cmd, "mcp", "get", "zcode_executor"]
            if os.name == "nt" and claude_cmd.lower().endswith((".cmd", ".bat")):
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", claude_cmd, "mcp", "get", "zcode_executor"]
            cp = subprocess.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            detail = cp.stdout.strip() or cp.stderr.strip() or f"exit {cp.returncode}"
            ok &= check("Claude MCP registration", cp.returncode == 0, detail[:1000])
        except Exception as exc:
            ok &= check("Claude MCP registration", False, str(exc))

    policy = HOME / ".claude" / "zcode-commander" / "COMMANDER.md"
    ok &= check("Commander policy", policy.is_file(), str(policy))

    print("\nResult:", "READY" if ok else "NEEDS ATTENTION")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
