# Claude Commander → ZCode Executor Kit (Windows) — v0.1.1

Purpose: keep **Claude Code** as the only agent you talk to, while **ZCode** performs implementation work behind the scenes. Claude plans, delegates, independently reviews real diffs/tests, sends corrections back to the same ZCode thread, and only then reports completion.

This kit intentionally does **not** reimplement the ZCode protocol. It uses the existing `Deslord319/coder-mcp-bridge` MCP server and adds a Windows launcher plus a persistent Claude Commander policy.


## Design baseline

This package now includes [`SPEC-RFD.md`](SPEC-RFD.md), the authoritative design baseline explaining **why this project exists, the Claude/ZCode responsibility split, prior-art decisions, the autonomous review/correction loop, Windows constraints, success criteria, and the phased validation plan**.

For version history, see [`CHANGELOG.md`](CHANGELOG.md).

## Architecture

```text
You
  ↓
Claude Code / Opus   (conversation, requirements, architecture, review)
  ↓ automatic MCP delegation
zcode_executor / coder-mcp-bridge
  ↓
ZCode                (implementation executor)
  ↓
Claude review → correction on same thread → re-review → PASS
```

No `/zcode` command is required in the normal flow.

## Install on Windows

Prerequisites:

- ZCode Desktop installed and logged in.
- Claude Code installed and working.
- Git.
- Python 3.
- A compatible ZCode/Node runtime. Standard ZCode Desktop installs are expected at `%LOCALAPPDATA%\Programs\ZCode`.

Open PowerShell in this extracted folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

The installer:

1. clones `Deslord319/coder-mcp-bridge` to `~/.zcode-commander/coder-mcp-bridge`;
2. installs a Windows-aware launcher that finds `ZCode.exe` + `resources\glm\zcode.cjs`;
3. registers it in Claude Code as MCP server `zcode_executor`;
4. installs `COMMANDER.md` under `~/.claude/zcode-commander/`;
5. imports that policy from `~/.claude/CLAUDE.md` so it is automatically active in normal Claude Code sessions;
6. runs zero-model-cost diagnostics.

### If headless ZCode config is missing

The upstream bridge can bootstrap `~/.zcode/cli/config.json` from your local ZCode Desktop provider configuration. Because this can copy local credential material into the CLI config, the kit leaves it opt-in:

```powershell
.\scripts\install.ps1 -EnsureZCodeCliConfig
```

## Usage

Restart Claude Code after installation. Then talk normally:

```text
> We agreed on the Plan Inspector direction. Implement it completely and verify it.
```

Expected behavior:

1. Claude investigates the repository.
2. Claude writes a bounded worker contract internally.
3. Claude invokes the ZCode MCP executor automatically.
4. ZCode edits/runs implementation verification.
5. Claude independently checks diff/tests/architecture.
6. If something fails, Claude sends a correction to the same ZCode thread and reviews again.
7. Claude reports completion only after its own acceptance checks pass.

## Diagnostics

```powershell
python "$HOME\.zcode-commander\doctor.py"
```

Useful Claude commands after install:

```text
/mcp
```

or from PowerShell:

```powershell
claude mcp get zcode_executor
```

## Updating the bridge

The normal installer deliberately does not `git pull` an existing bridge, so a working setup does not drift unexpectedly. Update explicitly:

```powershell
.\scripts\install.ps1 -ForceBridgeUpdate
```

## Uninstall

```powershell
.\scripts\uninstall.ps1
```

To also remove the cloned bridge and launcher:

```powershell
.\scripts\uninstall.ps1 -RemoveBridge
```

## Design choices

- **Transport:** reuse `coder-mcp-bridge`; no new MCP/ZCode protocol implementation.
- **Windows:** explicit launcher because upstream bridge's ZCode auto-discovery currently targets macOS/Linux, although its stdio handling includes Windows support.
- **Orchestration:** based on the task-contract/evidence-first ideas proven by `zcode-executor`: worker self-report is never acceptance evidence.
- **Session reuse:** corrections stay on the same ZCode thread whenever they belong to the same task.
- **Runaway control:** four worker/correction turns by default before Claude escalates a persistent blocker.
- **User UX:** one conversation with Claude; no manual relay or slash-command babysitting.

## Important limitation

This package can be structurally validated here, but the final end-to-end ZCode model turn must run on **your Windows machine** because it depends on your installed ZCode Desktop runtime, local ZCode credentials/provider, Claude Code installation, and project filesystem.
