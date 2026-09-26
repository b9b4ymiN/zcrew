#!/usr/bin/env python3
"""zcrew - command line front end for the Claude Commander -> ZCode kit.

    zcrew enable  [DIR] [--force] [--with-project-config] [--with-templates]
    zcrew disable [DIR]
    zcrew status  [DIR]
    zcrew config  [--project DIR]
    zcrew doctor
    zcrew update
    zcrew uninstall [--keep-config] [--yes]
    zcrew version

Layout written by get.ps1 (ZCREW_HOME defaults to ~/.zcrew):

    ZCREW_HOME/src/          git checkout of the kit (this file is src/scripts/zcrew.py)
    ZCREW_HOME/bin/zcrew.cmd shim on the user PATH

Internal names are unchanged: MCP server ``zcode_executor``, runtime files in
``~/.zcode-commander``, policy in ``~/.claude/zcode-commander/COMMANDER.md``.

Environment switches (mainly for sandboxed testing):
    ZCREW_HOME      override the install root (default: derived from this file)
    ZCREW_NO_PATH=1 never modify the user PATH in the registry
    ZCREW_SKIP_MCP=1 pass -SkipMcpRegistration to install.ps1 / uninstall.ps1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, NamedTuple, Sequence

HERE = Path(__file__).resolve().parent
PROG = "zcrew"

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


# --- paths --------------------------------------------------------------------


class ZcrewPaths(NamedTuple):
    home: Path
    src: Path
    scripts: Path
    bin: Path
    derived_home: Path


def resolve_paths(script: Path | None = None, env: Mapping[str, str] | None = None) -> ZcrewPaths:
    """src is always the checkout this script lives in; home comes from
    ZCREW_HOME when set, otherwise it is the parent of that checkout."""
    env = os.environ if env is None else env
    scripts = Path(script or __file__).resolve().parent
    src = scripts.parent
    derived = src.parent
    explicit = (env.get("ZCREW_HOME") or "").strip()
    home = Path(explicit).expanduser().resolve() if explicit else derived
    return ZcrewPaths(home=home, src=src, scripts=scripts, bin=home / "bin", derived_home=derived)


def read_version(src: Path) -> str:
    try:
        data = json.loads((Path(src) / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "unknown"
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version else "unknown"


def env_flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# --- ZCode CLI config bootstrap decision ----------------------------------------


def needs_cli_bootstrap(config_path: Path) -> bool:
    """True when ~/.zcode/cli/config.json is missing, unreadable, or has no
    non-empty model.main. Only then should install.ps1 get -EnsureZCodeCliConfig:
    running it again on a configured CLI renames the provider to '...-1'."""
    path = Path(config_path)
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return True
    model = data.get("model") if isinstance(data, dict) else None
    main = model.get("main") if isinstance(model, dict) else None
    return not (isinstance(main, str) and main.strip())


def bootstrap_decision(home: Path) -> str:
    """'needed', 'not-needed', or 'desktop-missing' (bootstrap needed but the
    ZCode Desktop config it copies from does not exist yet)."""
    home = Path(home)
    if not needs_cli_bootstrap(home / ".zcode" / "cli" / "config.json"):
        return "not-needed"
    if not (home / ".zcode" / "v2" / "config.json").is_file():
        return "desktop-missing"
    return "needed"


def install_args(install_ps1: Path, decision: str, env: Mapping[str, str]) -> list[str]:
    argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(install_ps1), "-SkipDoctor"]
    if decision == "needed":
        argv.append("-EnsureZCodeCliConfig")
    if env_flag(env, "ZCREW_SKIP_MCP"):
        argv.append("-SkipMcpRegistration")
    return argv


def uninstall_args(uninstall_ps1: Path, keep_config: bool, env: Mapping[str, str]) -> list[str]:
    argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(uninstall_ps1)]
    if not keep_config:
        argv.append("-RemoveBridge")
    if env_flag(env, "ZCREW_SKIP_MCP"):
        argv.append("-SkipMcpRegistration")
    return argv


# --- PATH string helpers (pure) -------------------------------------------------


def _norm_entry(entry: str) -> str:
    value = os.path.expandvars(entry.strip().strip('"').strip())
    value = value.replace("/", "\\").rstrip("\\")
    return value.casefold()


def _split_path(value: str) -> list[str]:
    return [part for part in (value or "").split(";") if part.strip()]


def path_contains(value: str, entry: str) -> bool:
    target = _norm_entry(entry)
    return any(_norm_entry(part) == target for part in _split_path(value))


def path_add(value: str, entry: str) -> tuple[str, bool]:
    """Append entry unless an equivalent one is present. Empty segments are
    dropped only when something changes, so an untouched PATH is returned as is."""
    if path_contains(value, entry):
        return value, False
    return ";".join([*_split_path(value), entry]), True


def path_remove(value: str, entry: str) -> tuple[str, bool]:
    target = _norm_entry(entry)
    parts = _split_path(value)
    kept = [part for part in parts if _norm_entry(part) != target]
    if len(kept) == len(parts):
        return value, False
    return ";".join(kept), True


# --- user PATH in the registry (HKCU\Environment, same store as [Environment]) ----


def read_user_path() -> tuple[str, int]:
    """Raw (unexpanded) user PATH and its registry type."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return str(value or ""), kind


def write_user_path(value: str, kind: int) -> None:
    """Writes keep REG_EXPAND_SZ so entries like %USERPROFILE%\\bin survive,
    then broadcast WM_SETTINGCHANGE so new Explorer-launched shells see it."""
    import ctypes
    import winreg

    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        kind = winreg.REG_EXPAND_SZ
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


def update_user_path(
    entry: str,
    add: bool,
    env: Mapping[str, str],
    read: Callable[[], tuple[str, int]] = read_user_path,
    write: Callable[[str, int], None] = write_user_path,
) -> str:
    if env_flag(env, "ZCREW_NO_PATH"):
        return f"ZCREW_NO_PATH=1: user PATH not modified (would {'add' if add else 'remove'} {entry})"
    current, kind = read()
    new, changed = (path_add if add else path_remove)(current, entry)
    if not changed:
        return f"user PATH already {'contains' if add else 'lacks'} {entry} (no change)"
    write(new, kind)
    return f"{'added' if add else 'removed'} {entry} {'to' if add else 'from'} the user PATH"


# --- shim -----------------------------------------------------------------------


def shim_text(zcrew_py: Path) -> str:
    """Absolute path when it is plain ASCII; cmd.exe reads .cmd files in the OEM
    code page, so a non-ASCII profile path falls back to a %~dp0-relative one."""
    target = str(zcrew_py)
    try:
        target.encode("ascii")
    except UnicodeEncodeError:
        target = r"%~dp0..\src\scripts\zcrew.py"
    return f'@echo off\r\npython "{target}" %*\r\n'


def write_shim(paths: ZcrewPaths) -> Path:
    paths.bin.mkdir(parents=True, exist_ok=True)
    shim = paths.bin / "zcrew.cmd"
    shim.write_bytes(shim_text(paths.scripts / "zcrew.py").encode("ascii"))
    return shim


# --- helpers ----------------------------------------------------------------------


def _load_sibling(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_zcrew_{name}", HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}.py next to {HERE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(src: Path, *args: str, run: Runner) -> "subprocess.CompletedProcess[Any]":
    return run(["git", "-C", str(src), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _run_doctor(paths: ZcrewPaths, run: Runner) -> int:
    return run([sys.executable, str(paths.scripts / "doctor.py")]).returncode


# --- commands -----------------------------------------------------------------------


def cmd_commander(args: argparse.Namespace) -> int:
    argv: list[str] = [args.command]
    if args.command == "config":
        if args.project:
            argv += ["--project", args.project]
    else:
        argv.append(args.dir or os.getcwd())
    if args.command == "enable":
        argv += ["--force"] if args.force else []
        argv += ["--with-project-config"] if args.with_project_config else []
        argv += ["--with-templates"] if args.with_templates else []
    return int(_load_sibling("commander").main(argv))


def cmd_setup(paths: ZcrewPaths, env: Mapping[str, str], run: Runner, home: Path | None = None) -> int:
    """install.ps1 + shim + PATH + doctor. Used by get.ps1 and by update."""
    decision = bootstrap_decision(home or Path.home())
    if decision == "needed":
        print(
            "ZCode CLI has no model configured: copying the Coding Plan provider (including its API key) "
            "from ZCode Desktop into ~/.zcode/cli/config.json (the old file is kept as .bak)."
        )
    elif decision == "desktop-missing":
        print(
            "[WARN] ZCode CLI has no model configured and ZCode Desktop has no config yet "
            "(~/.zcode/v2/config.json). Open ZCode Desktop, sign in with the GLM Coding Plan, "
            "then run: zcrew update"
        )
    if env_flag(env, "ZCREW_SKIP_MCP"):
        print("ZCREW_SKIP_MCP=1: Claude MCP registration is skipped.")
    code = run(install_args(paths.scripts / "install.ps1", decision, env)).returncode
    if code != 0:
        print(f"install.ps1 failed (exit {code}); fix the error above and run it again.")
        return code
    shim = write_shim(paths)
    print(f"created {shim}")
    print(update_user_path(str(paths.bin), True, env))
    print("\nRunning zero-model-cost doctor...")
    doctor_code = _run_doctor(paths, run)
    print(f"doctor: {'READY' if doctor_code == 0 else 'NEEDS ATTENTION (see [FAIL] lines above)'}")
    return 0


def cmd_update(paths: ZcrewPaths, run: Runner) -> int:
    if not (paths.src / ".git").exists():
        print(f"{paths.src} is not a git checkout; reinstall with get.ps1 to enable updates.")
        return 1
    status = _git(paths.src, "status", "--porcelain", run=run)
    if status.returncode != 0:
        print(f"git status failed in {paths.src}: {status.stderr.strip()}")
        return 1
    if status.stdout.strip():
        print(
            f"{paths.src} has local changes; refusing to update.\n"
            f"Inspect with: git -C \"{paths.src}\" status  (commit, stash or discard them, then retry)"
        )
        return 1
    for step in (("fetch", "--quiet"), ("pull", "--ff-only", "--quiet")):
        result = _git(paths.src, *step, run=run)
        if result.returncode != 0:
            print(f"git {step[0]} failed: {result.stderr.strip()}")
            return 1
    print(f"updated {paths.src} to version {read_version(paths.src)}")
    # Run setup from the freshly pulled code, not this already-loaded module.
    code = run([sys.executable, str(paths.scripts / "zcrew.py"), "_setup"]).returncode
    if code == 0:
        print("\nRestart Claude Code (quit it fully) so it reloads the zcode_executor MCP server.")
    return code


def _confirm(prompt: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def can_self_delete(paths: ZcrewPaths) -> bool:
    """Only the standard get.ps1 layout: ZCREW_HOME/src/scripts/zcrew.py with a
    ZCREW_HOME/bin/zcrew.cmd shim, and no ZCREW_HOME override pointing elsewhere."""
    return (
        paths.home == paths.derived_home
        and paths.src.name.lower() == "src"
        and (paths.bin / "zcrew.cmd").is_file()
    )


def self_delete_command(home: Path) -> str:
    """A raw cmd.exe command line (passed as a string, so Python does not
    backslash-escape the quotes, which cmd.exe would not understand)."""
    return f'cmd /d /s /c "ping -n 3 127.0.0.1 >nul & rmdir /s /q "{home}""'


def cmd_uninstall(
    paths: ZcrewPaths, args: argparse.Namespace, env: Mapping[str, str], run: Runner,
    confirm: Callable[[str], bool] = _confirm,
) -> int:
    what = "MCP registration and policy" if args.keep_config else "MCP registration, policy, bridge and ~/.zcode-commander"
    if not args.yes and not confirm(f"Remove zcrew ({what})? [y/N] "):
        print("aborted (nothing changed). Pass --yes to skip this question.")
        return 1
    code = run(uninstall_args(paths.scripts / "uninstall.ps1", args.keep_config, env)).returncode
    if code != 0:
        print(f"uninstall.ps1 failed (exit {code}); the zcrew files were left in place.")
        return code
    try:
        print(update_user_path(str(paths.bin), False, env))
    except OSError as exc:
        print(f"[WARN] could not update the user PATH ({exc}); remove {paths.bin} from it by hand.")
    if args.yes and can_self_delete(paths):
        # This process (and zcrew.cmd) is running from inside ZCREW_HOME, so the
        # delete runs detached after a short delay. ping is used as the delay
        # because timeout.exe refuses to run without a console.
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            self_delete_command(paths.home), creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"{paths.home} will be deleted in a few seconds.")
    else:
        print(
            "zcrew's own files were kept. To delete them, close this window and run in PowerShell:\n"
            f"  Remove-Item -Recurse -Force \"{paths.home}\""
        )
    print("Restart Claude Code so it drops the zcode_executor MCP server.")
    return 0


# --- CLI ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Let Claude Code command ZCode workers (GLM Coding Plan) as a crew.",
        epilog=(
            "Typical use: cd your-project; zcrew enable (or zcrew enable --with-templates for starter "
            "CLAUDE.md/AGENTS.md); then open a new Claude Code session there."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("enable", help="turn the commander policy on for a project (default: current dir)")
    p.add_argument("dir", nargs="?", default=None, metavar="DIR")
    p.add_argument("--force", action="store_true", help="enable even if DIR is not a git repository")
    p.add_argument("--with-project-config", action="store_true", help="also create .claude/zcode-commander.json")
    p.add_argument(
        "--with-templates", action="store_true",
        help="also create starter CLAUDE.md and AGENTS.md from the zcrew templates (existing files are kept)",
    )
    p = sub.add_parser("disable", help="turn the commander policy off (and remove unedited template files)")
    p.add_argument("dir", nargs="?", default=None, metavar="DIR")
    p = sub.add_parser("status", help="show whether a project is enabled, its AGENTS.md and its effective config")
    p.add_argument("dir", nargs="?", default=None, metavar="DIR")
    p = sub.add_parser("config", help="print and validate the effective config")
    p.add_argument("--project", default=None, metavar="DIR", help="include DIR/.claude/zcode-commander.json")
    sub.add_parser("doctor", help="zero-model-cost health check of the whole setup")
    sub.add_parser("update", help="pull the latest zcrew and re-run the installer")
    p = sub.add_parser("uninstall", help="remove the MCP registration, policy and PATH entry")
    p.add_argument("--keep-config", action="store_true", help="keep ~/.zcode-commander (bridge and config.json)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    sub.add_parser("version", help="print the zcrew version")
    # Internal: called by get.ps1 and by 'update' after pulling new code.
    sub.add_parser("_setup")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
    paths: ZcrewPaths | None = None,
) -> int:
    env = os.environ if env is None else env
    paths = paths or resolve_paths(env=env)
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.command in ("enable", "disable", "status", "config"):
        return cmd_commander(args)
    if args.command == "version":
        print(f"zcrew {read_version(paths.src)}")
        return 0
    if args.command == "doctor":
        return _run_doctor(paths, run)
    if args.command == "update":
        return cmd_update(paths, run)
    if args.command == "uninstall":
        return cmd_uninstall(paths, args, env, run)
    if args.command == "_setup":
        return cmd_setup(paths, env, run)
    raise AssertionError(args.command)


if __name__ == "__main__":
    # Line-buffer so our messages stay in order with child process output
    # when stdout is a pipe (get.ps1 pipes it through Out-Host).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
