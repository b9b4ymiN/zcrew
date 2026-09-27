# Changelog

All notable changes to the Claude Commander × ZCode Executor Kit are recorded here.

## 0.5.0 — 2026-09-27

**Codex can act as commander**, alone or alongside Claude Code. ZCode is always the worker.

### Added

- `zcrew enable --commander claude|codex|both` (default `claude`, unchanged behaviour).
  - Codex gets the commander policy inlined into `AGENTS.override.md`, inside a marked block that carries the policy sha256.
  - Codex prefers `AGENTS.override.md` to `AGENTS.md` in the same folder, and ZCode reads only `AGENTS.md`, so the roles never mix.
- `zcrew status` reports Claude/Codex activation. It flags a stale Codex policy copy, and re-running `enable` refreshes it in place.
- `zcrew disable [--commander X]` restores files byte for byte.
- `scripts/codex_config.py` sets `startup_timeout_sec = 60`, `tool_timeout_sec = 180` and `default_tools_approval_mode = "approve"` for `zcode_executor` in `~/.codex/config.toml`. It backs the file up first and validates the result with tomllib.
- The installer, `get.ps1` and `zcrew update` register the MCP server in Claude Code and/or Codex (`-Commander` / `ZCREW_COMMANDER`: `auto|claude|codex|both`). `uninstall` removes it from both.
- doctor checks the Claude and Codex registrations:
  - `[INFO]` when a CLI is not installed;
  - `[WARN]` when a CLI is installed but not registered, or when its timeout is too low;
  - `[FAIL]` when a registration is broken, or when no commander is registered.

### Changed

- Policy v3 is commander-neutral. It tells Codex to read `CLAUDE.md` and `AGENTS.md` itself, and notes that the bridge's cross-process leases serialize workers from two commanders in the same folder.
- At least one of Claude Code or Codex is now required, instead of Claude Code specifically.

## 0.4.0 — 2026-09-26

### Added

- Project instruction templates in `templates/`:
  - `CLAUDE.md` is for the commander. It covers architecture, boundaries and VERIFY commands, plus the zcrew block.
  - `AGENTS.md` is for the ZCode workers. It covers the worker rules and the report format, and ZCode loads it automatically from the working directory and its parents.
- `zcrew enable --with-templates` creates the missing files from those templates and never overwrites existing ones.
  - `disable` deletes a template file only if it is unedited, which it checks with a sha256 stored in a sidecar file.
  - `status` shows the template state.
- The installer copies the templates to `~/.zcode-commander/templates`.

### Changed

- The policy tells Claude to keep contracts task-specific when the project has an `AGENTS.md`. It also says to take VERIFY commands from the project `CLAUDE.md`, and to commit `AGENTS.md` before starting parallel worktrees.

## 0.3.0 — 2026-09-26

Public release as **zcrew**.

### Added

- `get.ps1`: installs everything with one command (`irm https://raw.githubusercontent.com/b9b4ymiN/zcrew/main/get.ps1 | iex`).
  - Checks the prerequisites and lists anything missing.
  - Clones the repo into `~/.zcrew/src`.
  - Bootstraps the ZCode CLI config automatically when `model.main` is missing.
  - Adds the `zcrew` command to the user PATH, preserving the registry value type and any `%VAR%` entries.
- `zcrew` CLI (`scripts/zcrew.py`) with the commands `enable`, `disable`, `status`, `config`, `doctor`, `update`, `uninstall` and `version`.
- `install.ps1` / `uninstall.ps1`: new `-SkipMcpRegistration` switch; `install.ps1` also gains `-SkipDoctor`.
- Apache-2.0 `LICENSE`.
- An English README written for the public repo; the Thai guide moved to `docs/README.th.md`.

### Changed

- The policy no longer assumes a personal knowledge MCP; `brain` is used only when one is available.
- The doctor's CLI-config hint now points to `zcrew update`.
- Internal names are unchanged for compatibility: the MCP server `zcode_executor`, `~/.zcode-commander`, and the policy path.

## 0.2.0 — 2026-09-26

First release validated end-to-end on the target Windows workstation (ZCode 3.14.0). See `SPEC-RFD.md` §26 for evidence.

### Added

- `scripts/bridge_compat.py`: a shim that runs the upstream bridge unchanged and fixes ZCode 3.14 compatibility:
  - disables `runtimeModel`;
  - pushes the account-provider snapshot;
  - answers provider runtime-header requests;
  - derives `options.reasoningLevel` from `thoughtLevel`.

  The account-provider logic is ported from `william0wang/zcode-acp` (Apache-2.0); see `NOTICE`.
- `scripts/commander.py`:
  - `enable` / `disable` / `status`: per-project activation through a marked import block in `<project>/CLAUDE.md`, with byte-exact removal;
  - `config`: prints the effective model/limits config and validates it.
- `config/default-config.json`. The installer seeds `~/.zcode-commander/config.json` once and never overwrites it.
- doctor additions:
  - ZCode version pin (`3.14.0` tested; other versions give a non-fatal `[WARN]`);
  - an app-server smoke test that makes no model call;
  - a check that `model.main` is set in the CLI config;
  - a list of desktop providers (secrets redacted);
  - a structured bridge probe;
  - a commander config check.
- Unit tests (`tests/`, stdlib `unittest`, 119 tests).
- `NOTICE` with third-party attribution.

### Changed

- **Policy v2 (`policy/COMMANDER.md`):**
  - Per-project only.
  - The flow is brief (plain-language questions, brain first) → 200–300 word plan with phases, tasks and DoD → user approval gate → autonomous ZCode delegate/review/same-thread-correction loop → per-task report.
  - Adds config-driven model/limits, a worktree swarm of up to 5 workers with merge-back and re-verify, progress one-liners, and a pointer to the ZCode app history.
- **Installer:**
  - pins the bridge commit (`-BridgeRef`) and checks exit codes;
  - is compatible with Windows PowerShell 5.1;
  - no longer edits the user-global `~/.claude/CLAUDE.md`;
  - copies the shim and `commander.py`.
- **Launcher:**
  - sets `ZCODE_APP_PATH`, `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` and `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`;
  - discovers ZCode under `Program Files`/`ProgramW6432`;
  - defaults to 5 concurrent workers;
  - starts the bridge through the compat shim.
- **Uninstall** removes the legacy global import line only if it is present, and never rewrites the global `CLAUDE.md` otherwise.

### Known limitations

- Windows only. Validated on ZCode 3.14.0 and on the individual GLM Coding Plan only.
- ZCode runs cannot be watched live inside the ZCode app. Their history appears in the app after a restart.
- Worktree sessions may not appear in the app's task list.
- A new session in an enabled project following the v2 policy by itself has not been validated yet.

## 0.1.1 — 2026-09-26

Documentation baseline release.

### Added

- `SPEC-RFD.md` documenting the origin, goals, architecture, responsibility split, acceptance model, prior art, Windows constraints, validation criteria, phased roadmap, and design decisions.
- `CHANGELOG.md` to preserve version-level decisions and changes.

### Changed

- Updated `README.md` to point to the SPEC/RFD as the design baseline.
- Updated `manifest.json` version to `0.1.1`.

### Implementation behavior

- No intended runtime behavior change from `0.1.0`.
- The next milestone remains real Windows end-to-end validation.

## 0.1.0 — 2026-09-26

Initial prototype.

### Added

- Windows-aware ZCode bridge launcher.
- Claude Commander policy.
- Installer, diagnostics, and uninstall utilities.
- Worker Contract example.
- `coder-mcp-bridge` reuse as the MCP control plane.
- Initial research notes.
