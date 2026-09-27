#!/usr/bin/env python3
"""Commander config and per-project activation.

    python commander.py config  [--project DIR]
    python commander.py enable  [DIR] [--commander claude|codex|both] [--force]
                                [--with-project-config] [--with-templates]
    python commander.py disable [DIR] [--commander claude|codex|both]
    python commander.py status  [DIR]

Config precedence (key by key): project ``.claude/zcode-commander.json`` over
user ``~/.zcode-commander/config.json`` over built-in defaults. ``model`` is
replaced as a whole object, never merged field by field.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
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
TEMPLATES_SIDECAR_REL = Path(".claude") / "zcode-commander.templates.json"
TEMPLATE_FILES = ("CLAUDE.md", "AGENTS.md")

# Codex has no @file imports, so the policy is inlined into AGENTS.override.md
# (which Codex reads instead of AGENTS.md in the same directory).
COMMANDERS = ("claude", "codex", "both")
CODEX_FILE = "AGENTS.override.md"
CODEX_BEGIN_PREFIX = "<!-- zcrew-codex:begin"
CODEX_END_MARKER = "<!-- zcrew-codex:end -->"
CODEX_SIDECAR_REL = Path(".claude") / "zcode-commander.codex.json"
CODEX_SIZE_WARN_BYTES = 28 * 1024
POLICY_REL = Path("policy") / "COMMANDER.md"
INSTALLED_POLICY_REL = Path(".claude") / "zcode-commander" / "COMMANDER.md"
_CODEX_BEGIN_RE = re.compile(r"<!-- zcrew-codex:begin policy-sha256=([0-9a-f]{64}) -->")


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


def _append_block(text: str, lines: tuple[str, ...] | list[str]) -> str:
    """Append a marked block. A file without a trailing newline gets the block
    with no trailing newline either, so _remove_span can restore it."""
    nl = _newline(text)
    block = nl.join(lines)
    if text == "":
        return block + nl
    if text.endswith(nl) or text.endswith("\n"):
        return text + nl + block + nl
    return text + nl + nl + block


def _remove_span(text: str, span: tuple[int, int] | None) -> str:
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


def add_block(text: str) -> str:
    """Append the Claude import block."""
    return _append_block(text, BLOCK_LINES)


def remove_block(text: str) -> str:
    return _remove_span(text, _block_span(text))


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def is_enabled(project_dir: Path) -> bool:
    claude_md = Path(project_dir) / "CLAUDE.md"
    return claude_md.is_file() and _block_span(_read_text(claude_md)) is not None


# --- starter templates (enable --with-templates) ----------------------------


def find_templates_dir(script_dir: Path | None = None) -> Path | None:
    """<scripts>/../templates (repo or ~/.zcrew/src), then <scripts>/templates
    (installed copy in ~/.zcode-commander). A candidate must hold every template."""
    base = Path(script_dir) if script_dir is not None else HERE
    for candidate in (base.parent / "templates", base / "templates"):
        if all((candidate / name).is_file() for name in TEMPLATE_FILES):
            return candidate
    return None


def _read_template(path: Path) -> str:
    """UTF-8 without BOM, LF newlines (a CRLF checkout must not change the hash)."""
    text = path.read_bytes().decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_templates_sidecar(path: Path) -> tuple[dict[str, str], bool]:
    """(recorded {relpath: sha256}, createdClaudeDir). Only known template names
    are accepted, so a damaged sidecar can never make disable delete other files."""
    if not path.is_file():
        return {}, False
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    files = data.get("files")
    recorded = {
        rel: digest
        for rel, digest in (files.items() if isinstance(files, dict) else ())
        if rel in TEMPLATE_FILES and isinstance(digest, str)
    }
    return recorded, bool(data.get("createdClaudeDir"))


def _templates_sidecar_text(recorded: Mapping[str, str], created_dir: bool) -> str:
    ordered = {rel: recorded[rel] for rel in TEMPLATE_FILES if rel in recorded}
    return json.dumps({"createdClaudeDir": created_dir, "files": ordered}, indent=2) + "\n"


def template_state(project_dir: Path, name: str) -> str:
    """'missing', 'template' (unedited zcrew template), 'edited' (created from the
    template, changed since) or 'user' (not created by zcrew). For CLAUDE.md the
    commander block is ignored."""
    project_dir = Path(project_dir)
    path = project_dir / name
    if not path.is_file():
        return "missing"
    recorded, _ = _read_templates_sidecar(project_dir / TEMPLATES_SIDECAR_REL)
    if name not in recorded:
        return "user"
    data = path.read_bytes()
    if name == "CLAUDE.md":
        data = remove_block(data.decode("utf-8")).encode("utf-8")
    return "template" if _sha256(data) == recorded[name] else "edited"


TEMPLATE_STATE_LABELS = {
    "missing": "none",
    "template": "from the zcrew template (unedited; disable deletes it)",
    "edited": "from the zcrew template, edited (disable keeps it)",
    "user": "yours (not created by zcrew; disable keeps it)",
}


# --- Codex activation (AGENTS.override.md with the inlined policy) -----------


def find_policy_file(script_dir: Path | None = None, home: Path | None = None) -> Path | None:
    """<scripts>/../policy/COMMANDER.md (repo or ~/.zcrew/src), then the copy
    install.ps1 puts in ~/.claude/zcode-commander/COMMANDER.md."""
    base = Path(script_dir) if script_dir is not None else HERE
    for candidate in (base.parent / POLICY_REL, (home or Path.home()) / INSTALLED_POLICY_REL):
        if candidate.is_file():
            return candidate
    return None


def read_policy(path: Path) -> tuple[str, str]:
    """(normalized text, sha256). UTF-8 without BOM, LF newlines, so a CRLF
    checkout and the installed copy hash the same."""
    text = _read_template(Path(path))
    if CODEX_END_MARKER in text or CODEX_BEGIN_PREFIX in text:
        raise ValueError(f"{path} contains a zcrew-codex marker; it cannot be inlined")
    return text, _sha256(text.encode("utf-8"))


def codex_block_lines(policy: str, digest: str) -> list[str]:
    body = policy.rstrip("\n").split("\n")
    return [f"{CODEX_BEGIN_PREFIX} policy-sha256={digest} -->", *body, CODEX_END_MARKER]


def _codex_span(text: str) -> tuple[int, int] | None:
    start = text.find(CODEX_BEGIN_PREFIX)
    if start == -1:
        return None
    end = text.find(CODEX_END_MARKER, start)
    if end == -1:
        return None
    return start, end + len(CODEX_END_MARKER)


def codex_block_sha(text: str) -> str | None:
    """None without a block; "" when the begin line carries no readable hash."""
    span = _codex_span(text)
    if span is None:
        return None
    match = _CODEX_BEGIN_RE.match(text, span[0])
    return match.group(1) if match else ""


def add_codex_block(text: str, policy: str, digest: str) -> str:
    return _append_block(text, codex_block_lines(policy, digest))


def replace_codex_block(text: str, policy: str, digest: str) -> str:
    span = _codex_span(text)
    if span is None:
        return text
    start, end = span
    return text[:start] + _newline(text).join(codex_block_lines(policy, digest)) + text[end:]


def remove_codex_block(text: str) -> str:
    return _remove_span(text, _codex_span(text))


def is_codex_enabled(project_dir: Path) -> bool:
    path = Path(project_dir) / CODEX_FILE
    return path.is_file() and _codex_span(_read_text(path)) is not None


def codex_state(project_dir: Path, policy_path: Path | None) -> str:
    """'off', 'current', 'stale', or 'unknown' (policy source not found)."""
    path = Path(project_dir) / CODEX_FILE
    if not path.is_file():
        return "off"
    digest = codex_block_sha(_read_text(path))
    if digest is None:
        return "off"
    if policy_path is None or not Path(policy_path).is_file():
        return "unknown"
    try:
        _, current = read_policy(Path(policy_path))
    except ValueError:
        return "unknown"
    return "current" if digest == current else "stale"


CODEX_STATE_LABELS = {
    "off": "not enabled",
    "current": "enabled (policy current)",
    "stale": "enabled (policy STALE - run: zcrew enable --commander codex)",
    "unknown": "enabled (cannot check: policy source COMMANDER.md not found)",
}


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _enable_codex(project_dir: Path, policy: str, digest: str) -> tuple[list[str], bool]:
    """(messages, changed)."""
    path = project_dir / CODEX_FILE
    if path.is_file():
        text = _read_text(path)
        current = codex_block_sha(text)
        if current == digest:
            return [f"Codex: already enabled in {path}, policy current (no change)"], False
        if current is not None:
            _write_text(path, replace_codex_block(text, policy, digest))
            messages = [f"Codex: refreshed Codex policy in {path} (the policy changed since it was enabled)"]
        else:
            _write_text(path, add_codex_block(text, policy, digest))
            messages = [f"Codex: enabled, added the commander policy block to {path}"]
    else:
        claude_dir = project_dir / ".claude"
        created_dir = not claude_dir.exists()
        claude_dir.mkdir(parents=True, exist_ok=True)
        sidecar = {"createdClaudeDir": created_dir, "createdFile": True}
        _write_text(project_dir / CODEX_SIDECAR_REL, json.dumps(sidecar) + "\n")
        _write_text(path, add_codex_block("", policy, digest))
        messages = [f"Codex: enabled, created {path} with the commander policy"]
    size = path.stat().st_size
    if size > CODEX_SIZE_WARN_BYTES:
        messages.append(
            f"warning: {path} is {size // 1024} KiB; Codex reads at most 32 KiB of project instructions "
            "(shared with the other AGENTS files on the way from the git root), so the end may be cut off"
        )
    return messages, True


def enable(
    project_dir: Path,
    force: bool = False,
    with_project_config: bool = False,
    with_templates: bool = False,
    templates_dir: Path | None = None,
    commander: str = "claude",
    policy_path: Path | None = None,
) -> tuple[bool, str]:
    """Returns (success, message). commander: 'claude', 'codex' or 'both'."""
    project_dir = Path(project_dir)
    if commander not in COMMANDERS:
        return False, f"unknown commander {commander!r}; use one of: {', '.join(COMMANDERS)}"
    use_claude = commander in ("claude", "both")
    use_codex = commander in ("codex", "both")
    if not project_dir.is_dir():
        return False, f"{project_dir} is not a directory"
    if not force and find_git_root(project_dir) is None:
        return False, (
            f"{project_dir} is not inside a git repository. Commander reviews every worker result "
            "with git diff, so it needs git. Run 'git init' first, or pass --force to enable anyway."
        )
    templates: dict[str, str] = {}
    if with_templates:
        if templates_dir is None:
            templates_dir = find_templates_dir()
        if templates_dir is None or not all((Path(templates_dir) / n).is_file() for n in TEMPLATE_FILES):
            where = templates_dir or f"{HERE.parent / 'templates'} or {HERE / 'templates'}"
            return False, (
                f"cannot find the zcrew templates ({', '.join(TEMPLATE_FILES)}) in {where}; "
                "reinstall zcrew (zcrew update) and retry. Nothing was changed."
            )
        templates = {name: _read_template(Path(templates_dir) / name) for name in TEMPLATE_FILES}
    policy = digest = ""
    if use_codex:
        if policy_path is None:
            policy_path = find_policy_file()
        if policy_path is None or not Path(policy_path).is_file():
            where = policy_path or f"{HERE.parent / POLICY_REL} or ~/{INSTALLED_POLICY_REL.as_posix()}"
            return False, (
                f"cannot find the commander policy (COMMANDER.md) in {where}; "
                "reinstall zcrew (zcrew update) and retry. Nothing was changed."
            )
        try:
            policy, digest = read_policy(Path(policy_path))
        except ValueError as exc:
            return False, f"{exc}. Nothing was changed."
        if (project_dir / CODEX_FILE).is_file():
            _read_text(project_dir / CODEX_FILE)  # non-UTF-8 fails here, before any write

    claude_dir_existed = (project_dir / ".claude").exists()
    templates_sidecar = project_dir / TEMPLATES_SIDECAR_REL
    recorded, templates_created_dir = _read_templates_sidecar(templates_sidecar)
    recorded_before = dict(recorded)
    messages: list[str] = []
    notes: list[str] = []
    claude_changed = False
    claude_md = project_dir / "CLAUDE.md"
    if not use_claude:
        if with_templates:
            if claude_md.is_file():
                if "CLAUDE.md" in recorded:
                    notes.append("CLAUDE.md came from the zcrew template earlier (no change)")
                else:
                    notes.append("kept your existing CLAUDE.md (template not applied)")
            else:
                base = templates["CLAUDE.md"]
                _write_text(claude_md, base)
                recorded["CLAUDE.md"] = _sha256(base.encode("utf-8"))
                notes.append(f"created {claude_md} from the zcrew template (project facts; Codex reads it at start)")
    elif claude_md.is_file():
        text = _read_text(claude_md)
        if _block_span(text) is not None:
            messages.append(f"already enabled in {claude_md} (no change)")
        elif IMPORT_LINE in text:
            messages.append(f"{claude_md} already imports the policy without markers (no change)")
        else:
            _write_text(claude_md, add_block(text))
            messages.append(f"enabled: added the commander import block to {claude_md}")
            claude_changed = True
        if with_templates:
            if "CLAUDE.md" in recorded:
                notes.append("CLAUDE.md came from the zcrew template earlier (no change)")
            else:
                notes.append("kept your existing CLAUDE.md (template not applied)")
    else:
        claude_dir = project_dir / ".claude"
        created_dir = not claude_dir.exists()
        claude_dir.mkdir(parents=True, exist_ok=True)
        sidecar = project_dir / CREATED_SIDECAR_REL
        _write_text(sidecar, json.dumps({"createdClaudeDir": created_dir}) + "\n")
        if with_templates:
            base = templates["CLAUDE.md"]
            _write_text(claude_md, add_block(base))
            recorded["CLAUDE.md"] = _sha256(base.encode("utf-8"))
            messages.append(f"enabled: created {claude_md} from the zcrew template with the commander import block")
        else:
            _write_text(claude_md, add_block(""))
            messages.append(f"enabled: created {claude_md} with the commander import block")
        claude_changed = True
    codex_changed = False
    if use_codex:
        codex_messages, codex_changed = _enable_codex(project_dir, policy, digest)
        messages.extend(codex_messages)
    if with_templates:
        agents_md = project_dir / "AGENTS.md"
        if agents_md.is_file():
            if "AGENTS.md" in recorded and _sha256(agents_md.read_bytes()) == recorded["AGENTS.md"]:
                notes.append("AGENTS.md came from the zcrew template earlier (no change)")
            else:
                notes.append("kept your existing AGENTS.md (template not applied)")
        else:
            data = templates["AGENTS.md"].encode("utf-8")
            agents_md.write_bytes(data)
            recorded["AGENTS.md"] = _sha256(data)
            notes.append(f"created {agents_md} from the zcrew template (fill in its 'Project specifics')")
        if recorded != recorded_before:
            templates_created_dir = templates_created_dir or not claude_dir_existed
            templates_sidecar.parent.mkdir(parents=True, exist_ok=True)
            _write_text(templates_sidecar, _templates_sidecar_text(recorded, templates_created_dir))
    messages.extend(notes)
    if with_project_config:
        target = project_config_path(project_dir)
        if target.exists():
            messages.append(f"project config {target} already exists (left unchanged)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text(target, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
            messages.append(f"created project config {target} (copy of defaults)")
    if claude_changed:
        messages.append("restart Claude Code in this project to load the policy")
    if codex_changed:
        messages.append("start a new Codex session in this project to load the policy")
    return True, "\n".join(messages)


def _pass_created_dir(project_dir: Path) -> None:
    """A partial disable removed the sidecar that said zcrew created .claude while
    other zcrew sidecars remain: record the fact in one of them so the last
    disable can still remove the folder."""
    for rel in (CREATED_SIDECAR_REL, CODEX_SIDECAR_REL):
        path = project_dir / rel
        if path.is_file():
            data = _read_json_dict(path)
            data["createdClaudeDir"] = True
            _write_text(path, json.dumps(data) + "\n")
            return
    templates_sidecar = project_dir / TEMPLATES_SIDECAR_REL
    if templates_sidecar.is_file():
        recorded, _ = _read_templates_sidecar(templates_sidecar)
        _write_text(templates_sidecar, _templates_sidecar_text(recorded, True))


def disable(project_dir: Path, commander: str | None = None) -> tuple[bool, str]:
    """Remove what enable added. commander None (default) or 'both' removes
    everything, including unedited template files; 'claude' or 'codex' removes
    only that commander's activation."""
    project_dir = Path(project_dir)
    if commander is not None and commander not in COMMANDERS:
        return False, f"unknown commander {commander!r}; use one of: {', '.join(COMMANDERS)}"
    everything = commander in (None, "both")
    use_claude = everything or commander == "claude"
    use_codex = everything or commander == "codex"
    claude_md = project_dir / "CLAUDE.md"
    sidecar = project_dir / CREATED_SIDECAR_REL
    templates_sidecar = project_dir / TEMPLATES_SIDECAR_REL
    override = project_dir / CODEX_FILE
    codex_sidecar = project_dir / CODEX_SIDECAR_REL
    enabled = use_claude and claude_md.is_file() and _block_span(_read_text(claude_md)) is not None
    codex_on = use_codex and override.is_file() and _codex_span(_read_text(override)) is not None
    cleanup_templates = everything and templates_sidecar.is_file()
    if not enabled and not codex_on and not cleanup_templates:
        return True, f"not enabled in {project_dir} (no change)"
    messages: list[str] = []
    created_dir = False
    if enabled:
        _write_text(claude_md, remove_block(_read_text(claude_md)))
        messages.append(f"disabled: removed the commander import block from {claude_md}")
        if sidecar.is_file():
            created_dir = bool(_read_json_dict(sidecar).get("createdClaudeDir"))
            if claude_md.read_bytes() == b"":
                claude_md.unlink()
                messages.append(f"deleted {claude_md} (enable had created it and it is now empty)")
            sidecar.unlink()
    elif use_claude and not codex_on:
        messages.append(f"not enabled in {project_dir} (no import block to remove)")
    if codex_on:
        _write_text(override, remove_codex_block(_read_text(override)))
        messages.append(f"disabled Codex: removed the commander policy block from {override}")
        if codex_sidecar.is_file():
            data = _read_json_dict(codex_sidecar)
            created_dir = created_dir or bool(data.get("createdClaudeDir"))
            if data.get("createdFile") and override.read_bytes() == b"":
                override.unlink()
                messages.append(f"deleted {override} (enable had created it and it is now empty)")
            codex_sidecar.unlink()
    if cleanup_templates:
        recorded, templates_created_dir = _read_templates_sidecar(templates_sidecar)
        created_dir = created_dir or templates_created_dir
        for rel, digest in recorded.items():
            path = project_dir / rel
            if not path.is_file():
                continue
            if _sha256(path.read_bytes()) == digest:
                path.unlink()
                messages.append(f"deleted {path} (created from the zcrew template, unedited)")
            else:
                messages.append(f"kept {rel} (you edited it)")
        templates_sidecar.unlink()
    claude_dir = sidecar.parent
    if created_dir and claude_dir.is_dir():
        if not any(claude_dir.iterdir()):
            claude_dir.rmdir()
        else:
            _pass_created_dir(project_dir)
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
    policy_path: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="commander.py", description="ZCode commander config and activation")
    sub = parser.add_subparsers(dest="command", required=True)
    p_config = sub.add_parser("config", help="print the effective config and validate it")
    p_config.add_argument("--project", default=None, help="project directory (adds its .claude/zcode-commander.json)")
    p_enable = sub.add_parser("enable", help="import the commander policy in DIR/CLAUDE.md")
    p_enable.add_argument("dir", nargs="?", default=".")
    p_enable.add_argument("--force", action="store_true", help="enable even if DIR is not a git repository")
    p_enable.add_argument("--with-project-config", action="store_true", help="also create .claude/zcode-commander.json")
    p_enable.add_argument(
        "--with-templates", action="store_true",
        help="also create CLAUDE.md and AGENTS.md from the zcrew templates when the project has none",
    )
    p_enable.add_argument(
        "--commander", choices=COMMANDERS, default="claude",
        help="claude (CLAUDE.md import, default), codex (AGENTS.override.md with the policy inlined) or both",
    )
    p_disable = sub.add_parser("disable", help="remove everything enable added to DIR")
    p_disable.add_argument("dir", nargs="?", default=".")
    p_disable.add_argument(
        "--commander", choices=COMMANDERS, default=None,
        help="remove only this commander's activation (default: everything, including unedited templates)",
    )
    p_status = sub.add_parser("status", help="show whether DIR is enabled and its effective config")
    p_status.add_argument("dir", nargs="?", default=".")
    args = parser.parse_args(argv)

    try:
        if args.command == "enable":
            ok, message = enable(
                Path(args.dir), force=args.force, with_project_config=args.with_project_config,
                with_templates=args.with_templates, commander=args.commander,
                policy_path=policy_path or find_policy_file(home=home),
            )
            print(message)
            return 0 if ok else 1
        if args.command == "disable":
            ok, message = disable(Path(args.dir), commander=args.commander)
            print(message)
            return 0 if ok else 1
    except UnicodeDecodeError:
        print(
            f"CLAUDE.md, {CODEX_FILE} or a zcrew template is not UTF-8 text; "
            "convert it to UTF-8 and retry (nothing was changed)"
        )
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
        claude_on = is_enabled(project_dir)
        try:
            codex = codex_state(project_dir, policy_path or find_policy_file(home=home))
        except UnicodeDecodeError:
            codex = "off"
        print(f"enabled: {'yes' if claude_on or codex != 'off' else 'no'}")
        print(f"claude: {'enabled' if claude_on else 'not enabled'}")
        print(f"codex: {CODEX_STATE_LABELS[codex]}")
        for name in TEMPLATE_FILES:
            try:
                state = template_state(project_dir, name)
            except UnicodeDecodeError:
                state = "user"
            if name == "CLAUDE.md" and state in ("missing", "user"):
                continue
            print(f"{name}: {TEMPLATE_STATE_LABELS[state]}")
        if find_git_root(project_dir) is None:
            print("warning: not a git repository; commander review needs git diff")
    project_path = project_config_path(project_dir) if project_dir is not None else None
    cfg, problems = check_config(user_path, project_path, table_path, v2_dir)
    _print_config(cfg, config_sources(user_path, project_path), problems)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
