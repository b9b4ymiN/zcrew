#!/usr/bin/env python3
"""Codex MCP settings for the zcode_executor server.

    python codex_config.py apply [--config PATH]

``codex mcp add`` registers the server but sets no timeouts. ZCode runs take
minutes, so this adds (or corrects) these keys in the
``[mcp_servers.zcode_executor]`` table of Codex's ``config.toml``:

    startup_timeout_sec = 60
    tool_timeout_sec = 180
    default_tools_approval_mode = "approve"

Everything else in the file (other servers, comments, key order) is kept. The
old file is backed up to ``config.toml.bak-zcrew-<timestamp>`` before writing.
Default path: ``$CODEX_HOME/config.toml``, else ``~/.codex/config.toml``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path
from typing import Mapping

SERVER = "zcode_executor"
SETTINGS: tuple[tuple[str, str, object], ...] = (
    ("startup_timeout_sec", "60", 60),
    ("tool_timeout_sec", "180", 180),
    ("default_tools_approval_mode", '"approve"', "approve"),
)
MIN_TOOL_TIMEOUT_SEC = 120

_HEADER = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:zcode_executor|"zcode_executor")\s*\]\s*(?:#.*)?$')
_ANY_HEADER = re.compile(r"^\s*\[")
_CONTENT = re.compile(r"^\s*[^\s#]")


class CodexConfigError(ValueError):
    """config.toml cannot be updated safely."""


def default_config_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    codex_home = (env.get("CODEX_HOME") or "").strip()
    base = Path(codex_home).expanduser() if codex_home else (home or Path.home()) / ".codex"
    return base / "config.toml"


def _parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexConfigError(f"config.toml is not valid TOML ({exc}); fix it, then run: zcrew update") from exc


def _server(data: dict) -> dict | None:
    servers = data.get("mcp_servers")
    server = servers.get(SERVER) if isinstance(servers, dict) else None
    return server if isinstance(server, dict) else None


def settings_ok(data: dict) -> bool:
    server = _server(data) or {}
    return all(server.get(key) == value for key, _, value in SETTINGS)


def apply_settings(text: str) -> tuple[str, bool]:
    """(new_text, changed). Pure. Raises CodexConfigError when the server is not
    registered, is written in a form this cannot edit, or the result is invalid."""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    data = _parse(body)
    if _server(data) is None:
        raise CodexConfigError(f"no [mcp_servers.{SERVER}] in config.toml; run: codex mcp add {SERVER} -- ...")
    if settings_ok(data):
        return text, False

    nl = "\r\n" if "\r\n" in body else "\n"
    lines = body.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _HEADER.match(line.rstrip("\r\n"))), None)
    if start is None:
        raise CodexConfigError(
            f"[mcp_servers.{SERVER}] is written inline; zcrew only edits the table form. "
            f"Run: codex mcp remove {SERVER}, then zcrew update"
        )
    end = next((i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))

    wanted = {key: literal for key, literal, _ in SETTINGS}
    for i in range(start + 1, end):
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", lines[i])
        if match and match.group(1) in wanted:
            ending = nl if lines[i].endswith(("\n", "\r")) else ""
            lines[i] = f"{match.group(1)} = {wanted.pop(match.group(1))}{ending}"

    if wanted:
        last = start
        for i in range(start + 1, end):
            if _CONTENT.match(lines[i]):
                last = i
        if not lines[last].endswith("\n"):
            lines[last] += nl
        new = [f"{key} = {literal}{nl}" for key, literal in wanted.items()]
        lines[last + 1:last + 1] = new

    result = bom + "".join(lines)
    if not settings_ok(_parse(result[len(bom):])):
        raise CodexConfigError("internal error: the edited config.toml does not hold the expected values")
    return result, True


def backup_path(config: Path, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = config.with_name(f"{config.name}.bak-zcrew-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = config.with_name(f"{config.name}.bak-zcrew-{stamp}-{counter}")
        counter += 1
    return candidate


def update_file(config: Path, now: float | None = None) -> tuple[bool, Path | None]:
    """(changed, backup). Writes only when something changes."""
    config = Path(config)
    if not config.is_file():
        raise CodexConfigError(f"{config} not found; is Codex installed and was the server added?")
    raw = config.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexConfigError(f"{config} is not UTF-8 text") from exc
    new, changed = apply_settings(text)
    if not changed:
        return False, None
    backup = backup_path(config, now)
    shutil.copy2(config, backup)
    config.write_bytes(new.encode("utf-8"))
    return True, backup


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex_config.py", description="set zcode_executor timeouts for Codex")
    sub = parser.add_subparsers(dest="command", required=True)
    p_apply = sub.add_parser("apply", help="add the timeout/approval keys to Codex config.toml")
    p_apply.add_argument("--config", default=None, help="config.toml path (default: $CODEX_HOME or ~/.codex)")
    args = parser.parse_args(argv)
    config = Path(args.config) if args.config else default_config_path(env)
    try:
        changed, backup = update_file(config)
    except CodexConfigError as exc:
        print(f"Codex config: {exc}")
        return 1
    if changed:
        print(f"Codex config: set {SERVER} timeouts in {config} (backup: {backup.name})")
    else:
        print(f"Codex config: {SERVER} timeouts already set in {config} (no change)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
