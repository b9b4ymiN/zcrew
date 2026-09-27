#!/usr/bin/env python3
"""Zero-model-cost local checks for the Claude -> MCP -> ZCode wiring."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "zcode_bridge_launcher.py"
COMMANDER = HERE / "commander.py"
TESTED_ZCODE_VERSIONS = {"3.14.0"}
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
HOME = Path.home()
BRIDGE = Path(os.environ.get("CODER_MCP_BRIDGE_ROOT", HOME / ".zcode-commander" / "coder-mcp-bridge"))
CLI_CONFIG_HINT = (
    "sign in to ZCode Desktop, then run `zcrew update` (or install.ps1 -EnsureZCodeCliConfig); "
    "it copies the Coding Plan provider from ZCode Desktop into the CLI config"
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


def check_app_server_output(stdout: str, stderr: str, returncode: int | None) -> CheckResult:
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("method") == "startup/storageState" and (message.get("params") or {}).get("phase") == "ready":
            return CheckResult("zcode app-server", True, "started and storage ready")
    detail = (stderr.strip() or f"no ready signal (exit {returncode})")[:600]
    return CheckResult("zcode app-server", False, detail)


def smoke_app_server(runtime: Path, bundle: Path, env: dict[str, str], timeout: float = 20) -> CheckResult:
    env = {**env, "ELECTRON_RUN_AS_NODE": "1", "NO_COLOR": "1"}
    try:
        proc = subprocess.run(
            [str(runtime), str(bundle), "app-server"],
            env=env,
            input="",
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return check_app_server_output(out, err, None)
    return check_app_server_output(proc.stdout, proc.stderr, proc.returncode)


def warn(name: str, detail: str) -> None:
    print(f"[WARN] {name}: {detail}")


def zcode_version_status(entries: Iterable[tuple[str, str]]) -> tuple[str, str]:
    """(level, detail) from uninstall (DisplayName, DisplayVersion) pairs;
    level is "OK" or "WARN" (never fatal)."""
    versions = [
        str(version).strip()
        for name, version in entries
        if isinstance(name, str) and name.startswith("ZCode") and version and str(version).strip()
    ]
    if not versions:
        return "WARN", "ZCode Desktop not found in the Windows uninstall registry; version cannot be checked"
    for version in versions:
        if version in TESTED_ZCODE_VERSIONS:
            return "OK", f"{version} (tested)"
    return "WARN", (
        f"{versions[0]} is untested; bridge compat was validated on "
        f"{', '.join(sorted(TESTED_ZCODE_VERSIONS))} - run a small task before relying on it"
    )


def read_uninstall_entries() -> list[tuple[str, str]]:
    """(DisplayName, DisplayVersion) from HKCU and HKLM (64- and 32-bit views)."""
    try:
        import winreg
    except ImportError:
        return []
    entries: list[tuple[str, str]] = []
    views = [
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    ]
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub) as key:
                        name = winreg.QueryValueEx(key, "DisplayName")[0]
                        version = winreg.QueryValueEx(key, "DisplayVersion")[0]
                except OSError:
                    continue
                if isinstance(name, str) and isinstance(version, str):
                    entries.append((name, version))
    return entries


def _load_module(name: str, path: Path) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_commander_config(commander: Any, user_path: Path, table_path: str | None, v2_dir: Path) -> CheckResult:
    cfg, problems = commander.check_config(user_path, None, table_path, v2_dir)
    if problems:
        return CheckResult("commander config", False, "; ".join(problems))
    source = str(user_path) if user_path.is_file() else "defaults (no user config file)"
    model = cfg["model"]
    return CheckResult(
        "commander config",
        True,
        f"{model['providerId']}/{model['modelId']} thoughtLevel={cfg['thoughtLevel']} from {source}",
    )


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


# --- commander MCP registrations (Claude Code and/or Codex) -------------------

SERVER = "zcode_executor"
MIN_CODEX_TOOL_TIMEOUT_SEC = 120
REGISTRATION_HINT = "run: zcrew update (or install.ps1)"
NOT_REGISTERED_HINT = "run `zcrew update --commander both` to register it"
_NOT_REGISTERED = re.compile(r"no mcp server (named|found)", re.IGNORECASE)
_TOOL_TIMEOUT = re.compile(r'tool_timeout_sec"?\s*:\s*([0-9]+(?:\.[0-9]+)?)')


class Status(NamedTuple):
    level: str  # OK, FAIL, WARN or INFO
    name: str
    detail: str
    registered: bool = False  # zcode_executor is registered with this commander


def emit(status: Status) -> None:
    if status.level in ("OK", "FAIL"):
        check(status.name, status.level == "OK", status.detail)
    elif status.level == "WARN":
        warn(status.name, status.detail)
    else:
        info(status.name, status.detail)


def cli_argv(path: str, *args: str, env: dict[str, str] | None = None) -> list[str]:
    """npm installs claude/codex as .cmd shims, which need cmd.exe to run."""
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        comspec = (env or os.environ).get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", path, *args]
    return [path, *args]


def _run_cli(run: Callable[..., Any], path: str, *args: str) -> Any:
    return run(
        cli_argv(path, *args),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )


def not_registered(cp: Any) -> bool:
    """The CLI ran fine and said the server does not exist (as opposed to a
    broken registration or a CLI error)."""
    return cp.returncode != 0 and bool(_NOT_REGISTERED.search(f"{cp.stdout or ''}\n{cp.stderr or ''}"))


def claude_registration(which: Callable[[str], str | None] = shutil.which,
                        run: Callable[..., Any] = subprocess.run) -> Status:
    name = "Claude MCP registration"
    path = which("claude")
    if not path:
        return Status("INFO", "claude", "not installed")
    try:
        cp = _run_cli(run, path, "mcp", "get", SERVER)
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        return Status("FAIL", name, str(exc))
    if not_registered(cp):
        return Status("WARN", name, f"claude is installed but {SERVER} is not registered in it; {NOT_REGISTERED_HINT}")
    detail = (cp.stdout or "").strip() or (cp.stderr or "").strip() or f"exit {cp.returncode}"
    ok = cp.returncode == 0
    return Status("OK" if ok else "FAIL", name, detail[:1000], ok)


def parse_tool_timeout(output: str) -> float | None:
    match = _TOOL_TIMEOUT.search(output or "")
    return float(match.group(1)) if match else None


def codex_registration(which: Callable[[str], str | None] = shutil.which,
                       run: Callable[..., Any] = subprocess.run) -> Status:
    name = "Codex MCP registration"
    path = which("codex")
    if not path:
        return Status("INFO", "codex", "not installed")
    try:
        cp = _run_cli(run, path, "mcp", "get", SERVER)
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        return Status("FAIL", name, str(exc))
    out = (cp.stdout or "").strip()
    if not_registered(cp):
        return Status("WARN", name, f"codex is installed but {SERVER} is not registered in it; {NOT_REGISTERED_HINT}")
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip() or out or f"exit {cp.returncode}"
        return Status("FAIL", name, f"{detail[:500]}; {REGISTRATION_HINT}")
    timeout = parse_tool_timeout(out)
    shown = "not set (Codex default 60)" if timeout is None else f"{timeout:g}"
    if timeout is None or timeout < MIN_CODEX_TOOL_TIMEOUT_SEC:
        return Status(
            "WARN", name,
            f"{SERVER} registered but tool_timeout_sec is {shown}; ZCode runs need at least "
            f"{MIN_CODEX_TOOL_TIMEOUT_SEC}s, run: zcrew update",
            True,
        )
    return Status("OK", name, f"{SERVER} registered, tool_timeout_sec={shown}", True)


def registrations_ready(statuses: Iterable[Status]) -> tuple[bool, Status | None]:
    """(ready, extra FAIL line). Ready needs no FAIL and at least one commander
    with zcode_executor registered (a low-timeout WARN still counts as registered;
    an installed-but-unregistered WARN does not)."""
    statuses = list(statuses)
    if any(s.level == "FAIL" for s in statuses):
        return False, None
    if any(s.registered for s in statuses):
        return True, None
    if all(s.level == "INFO" for s in statuses):
        detail = "neither Claude Code (claude) nor Codex (codex) is on PATH; install at least one, then run: zcrew update"
    else:
        detail = f"no commander has {SERVER} registered; {NOT_REGISTERED_HINT}"
    return False, Status("FAIL", "commander", detail)


def main() -> int:
    # Tool output (e.g. Claude's check mark) may not fit a non-UTF-8 pipe; never
    # let printing a detail turn a passing check into a failure.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ok = True
    ok &= check("python", bool(sys.executable), sys.executable)
    ok &= check("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    for tool in ("claude", "codex"):
        if shutil.which(tool):
            check(tool, True, shutil.which(tool) or tool)
    ok &= check("bridge", (BRIDGE / "server.py").is_file(), str(BRIDGE))

    # Import launcher functions without starting MCP.
    mod = _load_module("launcher", LAUNCHER)
    try:
        bundle = mod.find_zcode_bundle()
        runtime = mod.find_zcode_runtime(bundle)
        ok &= check("zcode bundle", True, str(bundle))
        ok &= check("zcode runtime", True, str(runtime))
    except Exception as exc:
        ok &= check("zcode discovery", False, str(exc))
        bundle = runtime = None

    level, detail = zcode_version_status(read_uninstall_entries())
    if level == "OK":
        check("zcode version", True, detail)
    else:
        warn("zcode version", detail)

    gui_cfg = HOME / ".zcode" / "v2" / "config.json"
    cli_cfg = HOME / ".zcode" / "cli" / "config.json"
    ok &= report(check_desktop_config(gui_cfg))
    if gui_cfg.is_file():
        info("ZCode desktop providers", describe_desktop_providers(gui_cfg))
    ok &= report(check_cli_config(cli_cfg))

    if (BRIDGE / "server.py").is_file() and bundle and runtime:
        env = mod.build_env(os.environ, bundle, runtime)
        ok &= report(smoke_app_server(runtime, bundle, env))
        try:
            probe = subprocess.run(
                [sys.executable, str(BRIDGE / "server.py"), "--probe"],
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            ok &= report(check_probe_output(probe.stdout, probe.stderr, probe.returncode))
        except Exception as exc:
            ok &= check("bridge probe", False, str(exc))

    registrations = [claude_registration(), codex_registration()]
    for status in registrations:
        emit(status)
    ready, extra = registrations_ready(registrations)
    if extra is not None:
        emit(extra)
    ok &= ready

    policy = HOME / ".claude" / "zcode-commander" / "COMMANDER.md"
    ok &= check("Commander policy", policy.is_file(), str(policy))

    try:
        commander = _load_module("commander", COMMANDER)
        table_path = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE") or (
            str(commander.builtin_table_for_bundle(bundle)) if bundle else None
        )
        ok &= report(check_commander_config(
            commander, commander.user_config_path(HOME), table_path, HOME / ".zcode" / "v2"
        ))
    except Exception as exc:
        ok &= check("commander config", False, f"{type(exc).__name__}: {exc}")

    print("\nResult:", "READY" if ok else "NEEDS ATTENTION")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
