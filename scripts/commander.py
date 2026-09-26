#!/usr/bin/env python3
"""Commander config and per-project activation.

    python commander.py config  [--project DIR]
    python commander.py enable  [DIR] [--force] [--with-project-config]
    python commander.py disable [DIR]
    python commander.py status  [DIR]

Config precedence (key by key): project ``.claude/zcode-commander.json`` over
user ``~/.zcode-commander/config.json`` over built-in defaults. ``model`` is
replaced as a whole object, never merged field by field.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent

DEFAULT_CONFIG: dict[str, Any] = {
    "model": {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"},
    "thoughtLevel": "max",
    "maxWorkers": 5,
    "maxCorrectionRounds": 4,
    "timeoutSeconds": 1800,
}
THOUGHT_LEVELS = ("high", "max")
INT_LIMITS = {
    "maxWorkers": (1, 5),
    "maxCorrectionRounds": (1, 10),
    "timeoutSeconds": (60, 86400),
}

BEGIN_MARKER = "<!-- zcode-commander:begin -->"
END_MARKER = "<!-- zcode-commander:end -->"
IMPORT_LINE = "@~/.claude/zcode-commander/COMMANDER.md"
BLOCK_LINES = (BEGIN_MARKER, IMPORT_LINE, END_MARKER)
PROJECT_CONFIG_REL = Path(".claude") / "zcode-commander.json"
CREATED_SIDECAR_REL = Path(".claude") / "zcode-commander.created-claude-md"


class ConfigError(ValueError):
    """A config file exists but cannot be used."""


# --- sibling modules (installed side by side in ~/.zcode-commander) --------


def _load_sibling(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_commander_{name}", HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}.py next to {HERE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bridge_compat: ModuleType | None = None


def bridge_compat() -> ModuleType:
    global _bridge_compat
    if _bridge_compat is None:
        _bridge_compat = _load_sibling("bridge_compat")
    return _bridge_compat


# --- config: load + merge ---------------------------------------------------


def user_config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".zcode-commander" / "config.json"


def project_config_path(project_dir: Path) -> Path:
    return Path(project_dir) / PROJECT_CONFIG_REL


def read_config_file(path: Path | None) -> dict[str, Any] | None:
    """None when the file is absent; ConfigError when present but unusable."""
    if path is None or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object like {{\"maxWorkers\": 3}}")
    return data


def load_effective_config(
    user_path: Path | None, project_path: Path | None, defaults: Mapping[str, Any]
) -> dict[str, Any]:
    cfg = copy.deepcopy(dict(defaults))
    for layer in (read_config_file(user_path), read_config_file(project_path)):
        for key, value in (layer or {}).items():
            cfg[key] = copy.deepcopy(value)
    return cfg


def config_sources(user_path: Path | None, project_path: Path | None) -> list[str]:
    sources = ["defaults"]
    for label, path in (("user", user_path), ("project", project_path)):
        if path is not None and Path(path).is_file():
            sources.append(f"{label} {path}")
    return sources


# --- config: validation -----------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_config(cfg: Mapping[str, Any], table: dict | None, v2_dir: Path | None) -> list[str]:
    problems: list[str] = []
    for key in cfg:
        if key not in DEFAULT_CONFIG:
            problems.append(
                f"unknown key {key!r}; allowed keys are {', '.join(DEFAULT_CONFIG)} (check spelling)"
            )

    level = cfg.get("thoughtLevel")
    if level not in THOUGHT_LEVELS:
        problems.append(f"thoughtLevel is {level!r}; set it to \"high\" or \"max\"")

    for key, (low, high) in INT_LIMITS.items():
        value = cfg.get(key)
        if not _is_int(value) or not low <= value <= high:
            problems.append(f"{key} is {value!r}; set it to a whole number from {low} to {high}")

    model = cfg.get("model")
    provider_id = model.get("providerId") if isinstance(model, dict) else None
    model_id = model.get("modelId") if isinstance(model, dict) else None
    if not isinstance(provider_id, str) or not provider_id or not isinstance(model_id, str) or not model_id:
        problems.append(
            'model must be an object like {"providerId": "account:zai-individual-coding-plan", '
            '"modelId": "GLM-5.3"}'
        )
        return problems

    bc = bridge_compat()
    rules = bc.account_rules(table)
    if not rules:
        problems.append(
            "cannot check model: ZCode built-in provider table not found or has no account plans "
            "(is ZCode Desktop installed? run doctor.py)"
        )
        return problems
    rule = next((r for r in rules if r.get("providerId") == provider_id), None)
    if rule is None:
        known = ", ".join(sorted(str(r.get("providerId")) for r in rules if r.get("providerId")))
        problems.append(f"model.providerId {provider_id!r} is not a ZCode account plan; use one of: {known}")
        return problems
    model_ids = list((rule.get("config") or {}).get("builtinModelIds") or [])
    if model_id not in model_ids:
        problems.append(
            f"model.modelId {model_id!r} is not offered by {provider_id}; use one of: "
            f"{', '.join(model_ids) or '(none listed)'}"
        )
    if v2_dir is None or bc.legacy_provider_id(table, provider_id) not in bc.entitled_legacy_ids(Path(v2_dir)):
        problems.append(
            f"your ZCode account is not entitled to {provider_id}; sign in to that plan in ZCode Desktop "
            "or pick a providerId you are subscribed to"
        )
    return problems


# --- environment lookup (non-pure; CLI and doctor only) ----------------------


def builtin_table_for_bundle(bundle: Path) -> Path:
    install = Path(bundle).parent.parent.parent
    return install / "resources" / "config" / "provider" / "zcode-builtin.json"


def discover_builtin_table(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    explicit = (env.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE") or "").strip()
    if explicit:
        return explicit
    try:
        bundle = _load_sibling("zcode_bridge_launcher").find_zcode_bundle()
    except Exception:  # noqa: BLE001 - reported as a validation problem instead
        return None
    path = builtin_table_for_bundle(bundle)
    return str(path) if path.is_file() else None


def check_config(
    user_path: Path | None, project_path: Path | None, table_path: str | None, v2_dir: Path | None
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        cfg = load_effective_config(user_path, project_path, DEFAULT_CONFIG)
    except ConfigError as exc:
        return None, [str(exc)]
    return cfg, validate_config(cfg, bridge_compat().read_builtin_table(table_path), v2_dir)


# --- per-project enable / disable -------------------------------------------


def find_git_root(start: Path) -> Path | None:
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _newline(text: str) -> str:
    crlf, lf = text.find("\r\n"), text.find("\n")
    return "\r\n" if crlf != -1 and crlf == lf - 1 else "\n"


def _block(nl: str) -> str:
    return nl.join(BLOCK_LINES)


def _block_span(text: str) -> tuple[int, int] | None:
    start = text.find(BEGIN_MARKER)
    if start == -1:
        return None
    end = text.find(END_MARKER, start)
    if end == -1:
        return None
    return start, end + len(END_MARKER)


def add_block(text: str) -> str:
    """Append the import block. A file without a trailing newline gets the
    block with no trailing newline either, so remove_block can restore it."""
    nl = _newline(text)
    if text == "":
        return _block(nl) + nl
    if text.endswith(nl) or text.endswith("\n"):
        return text + nl + _block(nl) + nl
    return text + nl + nl + _block(nl)


def remove_block(text: str) -> str:
    span = _block_span(text)
    if span is None:
        return text
    start, end = span
    nl = _newline(text)
    before, after = text[:start], text[end:]
    if after.startswith(nl):
        after = after[len(nl):]
        if before.endswith(nl + nl):
            before = before[: -len(nl)]
    elif after == "" and before.endswith(nl + nl):
        before = before[: -2 * len(nl)]
    return before + after


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def is_enabled(project_dir: Path) -> bool:
    claude_md = Path(project_dir) / "CLAUDE.md"
    return claude_md.is_file() and _block_span(_read_text(claude_md)) is not None


def enable(project_dir: Path, force: bool = False, with_project_config: bool = False) -> tuple[bool, str]:
    """Returns (success, message)."""
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return False, f"{project_dir} is not a directory"
    if not force and find_git_root(project_dir) is None:
        return False, (
            f"{project_dir} is not inside a git repository. Commander reviews every worker result "
            "with git diff, so it needs git. Run 'git init' first, or pass --force to enable anyway."
        )
    messages: list[str] = []
    claude_md = project_dir / "CLAUDE.md"
    if claude_md.is_file():
        text = _read_text(claude_md)
        if _block_span(text) is not None:
            messages.append(f"already enabled in {claude_md} (no change)")
        elif IMPORT_LINE in text:
            messages.append(f"{claude_md} already imports the policy without markers (no change)")
        else:
            _write_text(claude_md, add_block(text))
            messages.append(f"enabled: added the commander import block to {claude_md}")
    else:
        claude_dir = project_dir / ".claude"
        created_dir = not claude_dir.exists()
        claude_dir.mkdir(parents=True, exist_ok=True)
        sidecar = project_dir / CREATED_SIDECAR_REL
        _write_text(sidecar, json.dumps({"createdClaudeDir": created_dir}) + "\n")
        _write_text(claude_md, add_block(""))
        messages.append(f"enabled: created {claude_md} with the commander import block")
    if with_project_config:
        target = project_config_path(project_dir)
        if target.exists():
            messages.append(f"project config {target} already exists (left unchanged)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text(target, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
            messages.append(f"created project config {target} (copy of defaults)")
    if messages[0].startswith("enabled:"):
        messages.append("restart Claude Code in this project to load the policy")
    return True, "\n".join(messages)


def disable(project_dir: Path) -> tuple[bool, str]:
    project_dir = Path(project_dir)
    claude_md = project_dir / "CLAUDE.md"
    sidecar = project_dir / CREATED_SIDECAR_REL
    if not claude_md.is_file() or _block_span(_read_text(claude_md)) is None:
        return True, f"not enabled in {project_dir} (no change)"
    _write_text(claude_md, remove_block(_read_text(claude_md)))
    messages = [f"disabled: removed the commander import block from {claude_md}"]
    if sidecar.is_file():
        try:
            created_dir = bool(json.loads(_read_text(sidecar)).get("createdClaudeDir"))
        except (OSError, ValueError, AttributeError):
            created_dir = False
        if claude_md.read_bytes() == b"":
            claude_md.unlink()
            messages.append(f"deleted {claude_md} (enable had created it and it is now empty)")
        sidecar.unlink()
        claude_dir = sidecar.parent
        if created_dir and claude_dir.is_dir() and not any(claude_dir.iterdir()):
            claude_dir.rmdir()
    return True, "\n".join(messages)


# --- CLI ----------------------------------------------------------------------


def _print_config(cfg: dict[str, Any] | None, sources: list[str], problems: list[str]) -> None:
    print("config sources: " + " < ".join(sources))
    if cfg is not None:
        print(json.dumps(cfg, indent=2))
    if problems:
        print("validation: FAIL")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("validation: OK")


def main(
    argv: list[str] | None = None,
    *,
    home: Path | None = None,
    table_path: str | None = None,
    v2_dir: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="commander.py", description="ZCode commander config and activation")
    sub = parser.add_subparsers(dest="command", required=True)
    p_config = sub.add_parser("config", help="print the effective config and validate it")
    p_config.add_argument("--project", default=None, help="project directory (adds its .claude/zcode-commander.json)")
    p_enable = sub.add_parser("enable", help="import the commander policy in DIR/CLAUDE.md")
    p_enable.add_argument("dir", nargs="?", default=".")
    p_enable.add_argument("--force", action="store_true", help="enable even if DIR is not a git repository")
    p_enable.add_argument("--with-project-config", action="store_true", help="also create .claude/zcode-commander.json")
    p_disable = sub.add_parser("disable", help="remove the commander import block from DIR/CLAUDE.md")
    p_disable.add_argument("dir", nargs="?", default=".")
    p_status = sub.add_parser("status", help="show whether DIR is enabled and its effective config")
    p_status.add_argument("dir", nargs="?", default=".")
    args = parser.parse_args(argv)

    try:
        if args.command == "enable":
            ok, message = enable(Path(args.dir), force=args.force, with_project_config=args.with_project_config)
            print(message)
            return 0 if ok else 1
        if args.command == "disable":
            ok, message = disable(Path(args.dir))
            print(message)
            return 0 if ok else 1
    except UnicodeDecodeError:
        print("CLAUDE.md is not UTF-8 text; convert it to UTF-8 and retry (nothing was changed)")
        return 1

    user_path = user_config_path(home)
    if table_path is None:
        table_path = discover_builtin_table()
    if v2_dir is None:
        v2_dir = (home or Path.home()) / ".zcode" / "v2"
    project_dir = Path(args.project) if args.command == "config" and args.project else None
    if args.command == "status":
        project_dir = Path(args.dir)
        print(f"project: {project_dir.resolve()}")
        print(f"enabled: {'yes' if is_enabled(project_dir) else 'no'}")
        if find_git_root(project_dir) is None:
            print("warning: not a git repository; commander review needs git diff")
    project_path = project_config_path(project_dir) if project_dir is not None else None
    cfg, problems = check_config(user_path, project_path, table_path, v2_dir)
    _print_config(cfg, config_sources(user_path, project_path), problems)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
