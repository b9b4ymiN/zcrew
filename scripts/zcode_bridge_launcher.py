#!/usr/bin/env python3
"""Windows-friendly launcher for Deslord319/coder-mcp-bridge.

This file deliberately does not implement the ZCode protocol. It only locates
an existing ZCode Desktop installation, injects the paths expected by the
upstream bridge, and execs upstream server.py over the same stdio streams.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

HOME = Path.home()
DEFAULT_BRIDGE = HOME / ".zcode-commander" / "coder-mcp-bridge"
DEFAULT_MAX_CONCURRENCY = "5"


def _existing(*paths: Path) -> Path | None:
    for path in paths:
        if path and path.is_file():
            return path.resolve()
    return None


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _program_files_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for name in ("ProgramFiles", "ProgramW6432"):
        root = _env_path(name)
        if root is None:
            continue
        key = os.path.normcase(os.path.normpath(str(root)))
        if key not in seen:
            seen.add(key)
            roots.append(root)
    if not roots:
        roots.append(Path(r"C:\Program Files"))
    return roots


def find_zcode_bundle() -> Path:
    explicit = os.environ.get("ZCODE_CLI_BUNDLE") or os.environ.get("ZCODE_BIN")
    if explicit and Path(explicit).is_file() and Path(explicit).name.lower() == "zcode.cjs":
        return Path(explicit).resolve()

    local = _env_path("LOCALAPPDATA")
    appdata = _env_path("APPDATA")
    program_files = _program_files_roots()

    install_dirs = [
        *([local / "Programs" / "ZCode"] if local else []),
        *([appdata / "Programs" / "ZCode"] if appdata else []),
        *(root / "ZCode" for root in program_files),
    ]
    direct = _existing(*(d / "resources" / "glm" / "zcode.cjs" for d in install_dirs))
    if direct:
        return direct

    # Bounded fallbacks. Avoid scanning an entire drive.
    roots = [
        *([local / "Programs" / "ZCode"] if local else []),
        *([appdata / "ZCode"] if appdata else []),
        *(root / "ZCode" for root in program_files),
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            for path in root.rglob("zcode.cjs"):
                normalized = str(path).replace("\\", "/").lower()
                if "/resources/glm/" in normalized:
                    return path.resolve()
        except OSError:
            pass

    raise FileNotFoundError(
        "ZCode CLI bundle not found. Expected e.g. "
        r"%LOCALAPPDATA%\Programs\ZCode\resources\glm\zcode.cjs or "
        r"%ProgramFiles%\ZCode\resources\glm\zcode.cjs. "
        "Set ZCODE_CLI_BUNDLE explicitly if ZCode is installed elsewhere."
    )


def find_zcode_runtime(bundle: Path) -> Path:
    explicit = os.environ.get("ZCODE_BINARY")
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()

    # bundle: <install>/resources/glm/zcode.cjs
    install = bundle.parent.parent.parent
    candidates = [
        install / "ZCode.exe",
        install / "zcode.exe",
    ]
    runtime = _existing(*candidates)
    if runtime:
        return runtime

    # Last-resort fallback: use system Node if present. Upstream bridge normally
    # uses the Electron binary as Node via ELECTRON_RUN_AS_NODE=1, but ZCode's
    # bundled CLI can also be launched by a compatible Node runtime.
    node = shutil.which("node")
    if node:
        return Path(node).resolve()

    raise FileNotFoundError(
        f"Could not locate ZCode.exe next to {bundle}. Set ZCODE_BINARY to "
        "ZCode.exe (preferred) or a compatible Node executable."
    )


def build_env(base_env: Mapping[str, str], bundle: Path, runtime: Path) -> dict[str, str]:
    env = dict(base_env)
    env.setdefault("AGENT_MCP_DEFAULT_BACKEND", "zcode")
    env.setdefault("AGENT_MCP_TIMEOUT", "1800")
    env.setdefault("AGENT_MCP_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
    env["ZCODE_BINARY"] = str(runtime)
    env["ZCODE_CLI_BUNDLE"] = str(bundle)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> int:
    bridge_root = Path(os.environ.get("CODER_MCP_BRIDGE_ROOT", DEFAULT_BRIDGE)).expanduser().resolve()
    server = bridge_root / "server.py"
    if not server.is_file():
        raise FileNotFoundError(
            f"coder-mcp-bridge not found at {bridge_root}. Run install.ps1 first "
            "or set CODER_MCP_BRIDGE_ROOT."
        )

    bundle = find_zcode_bundle()
    runtime = find_zcode_runtime(bundle)

    env = build_env(os.environ, bundle, runtime)

    # Use subprocess rather than importing upstream so updates remain isolated.
    proc = subprocess.run([sys.executable, str(server)], env=env)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
