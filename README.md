<div align="center">

# zcrew

**Claude Code plans and reviews. A crew of ZCode (GLM) agents writes the code.**

You work with one assistant, Claude Code. It hands implementation to ZCode workers and checks every diff itself. When something fails review, it sends the fix back to the same worker session, and it only reports to you once the work passes.

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](#tech-stack)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-MCP-D97757)](https://docs.anthropic.com/en/docs/claude-code)
[![ZCode](https://img.shields.io/badge/ZCode-3.14.0%20tested-6E56CF)](#compatibility)
[![Tests](https://img.shields.io/badge/tests-166%20passing-2EA44F)](#development)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [ภาษาไทย](docs/README.th.md)

</div>

---

## Why

Frontier models are strongest at understanding, design and review. Coding agents are cheaper for the bulk of the typing. zcrew assigns each role to the model that fits it, and you still talk to a single assistant.

| Role | Who | Responsibility |
|---|---|---|
| **Client** | You | State the goal, approve the plan |
| **Commander** | Claude Code (Opus) | Clarify → plan with Definition of Done → delegate → review diff and run tests → correct → report |
| **Crew** (up to 5) | ZCode · GLM-5.3 at `max` reasoning | Implement, write tests, fix what the reviewer rejects |

> **The executor never accepts its own work.** A worker saying "done" is a claim. Acceptance requires Claude to review `git diff` itself and run the tests itself.

## Features

- **One conversation.** No `/zcode` command and no copy-paste relay. Claude decides when to delegate.
- **Plan-first.** You get a short plan with phases, tasks and a Definition of Done. Nothing is delegated until you approve it.
- **Independent review loop.** Claude inspects the diff and runs your tests/build/lint itself. A rejection goes back to the same ZCode thread, so the worker keeps its context. Each task has a correction budget (default 4 rounds).
- **Parallel crew.** Up to 5 ZCode workers run on independent tasks in separate `git worktree`s. Branches are merged back one at a time, and tests are re-run after every merge.
- **Per-project opt-in.** `zcrew enable` activates the policy for one repo. `zcrew disable` restores that repo's `CLAUDE.md` byte for byte.
- **Configurable model.** GLM-5.3 at `max` by default. You can switch models globally, per project, or for a single task.
- **Worker history.** Every ZCode run is saved in the ZCode app's own history, so you can read the worker's full conversation.
- **One-command install** with a built-in `doctor` that checks readiness without making any model call.

## Quick start

**1. Install** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/b9b4ymiN/zcrew/main/get.ps1 | iex
```

**2. Restart Claude Code.** Quit it fully, including from the system tray, so it loads the `zcode_executor` MCP server.

**3. Enable a project:**

```powershell
cd C:\path\to\your-repo
zcrew enable
```

**4. Open a new Claude Code session in that repo and ask for work as usual:**

```text
> Add a /health endpoint that returns the app version as JSON.
```

Claude asks a clarifying question or two, sends a plan for your approval, and then runs the ZCode crew. It reports once every task has passed its review.

## Requirements

| | Version | Notes |
|---|---|---|
| Windows | 10 / 11 | PowerShell 5.1 or 7 |
| [ZCode Desktop](https://z.ai) | **3.14.0** (tested) | Signed in with a **Z.ai individual GLM Coding Plan** |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | recent | Installed and signed in |
| Python | 3.10+ | Must be on `PATH` as `python`. zcrew uses the standard library only |
| Git | any recent | Your projects must be git repos, because review relies on `git diff` |

## How it works

```mermaid
flowchart LR
    U([You]) -- brief / approve --> C["Claude Code<br/>Commander"]
    C -- worker contract --> M["MCP: zcode_executor<br/>coder-mcp-bridge"]
    M --- S["bridge_compat shim<br/>(ZCode 3.14 fixes)"]
    M --> Z1["ZCode worker 1<br/>GLM-5.3"]
    M --> Z2["ZCode worker 2..5"]
    Z1 -- edits --> R[("git repo /<br/>worktrees")]
    Z2 -- edits --> R
    C -- "git diff + run tests" --> R
    C -- report --> U
```

```mermaid
sequenceDiagram
    participant You
    participant Claude as Claude Code
    participant ZCode as ZCode (same thread)
    You->>Claude: Request
    Claude->>You: Plan (phases, tasks, DoD) — approve?
    You->>Claude: approve
    loop per task, up to maxCorrectionRounds
        Claude->>ZCode: Worker contract / correction
        ZCode-->>Claude: "done" (a claim, not evidence)
        Claude->>Claude: git diff + run VERIFY commands
    end
    Claude->>You: Per-task report (changes, evidence, DoD status)
```

Every delegation carries a **worker contract** with these parts:

- `OBJECTIVE`
- `CONTEXT`
- `SCOPE`
- `DO NOT`
- `ACCEPTANCE CRITERIA`
- `VERIFY`
- `REPORT`

Claude reviews the result against that same contract. The full behaviour is defined in [`policy/COMMANDER.md`](policy/COMMANDER.md), and an example contract is in [`examples/worker-contract.md`](examples/worker-contract.md).

## Tech stack

| Layer | Technology |
|---|---|
| Commander | Claude Code (Claude Opus), with its behaviour defined by a Markdown policy loaded via `CLAUDE.md` import |
| Control plane | [Model Context Protocol](https://modelcontextprotocol.io) over stdio (JSON-RPC 2.0) |
| MCP server | [`coder-mcp-bridge`](https://github.com/Deslord319/coder-mcp-bridge) (Python), pinned commit, used unmodified |
| Compatibility layer | `bridge_compat.py`: a runtime shim that adapts the bridge to ZCode 3.12+ (account-provider snapshot, runtime auth headers, reasoning level) |
| Executor | ZCode Desktop app-server (Electron/Node, headless), running GLM-5.3 on the Z.ai Coding Plan |
| Isolation | `git worktree` per parallel worker, with cross-process resource leases in the bridge |
| Tooling | PowerShell installer (5.1/7 compatible), Python 3.10+ standard-library CLI (`zcrew`), `unittest` (166 tests) |

## Usage

```text
zcrew enable [DIR] [--with-project-config] [--force]   activate the policy for a repo (default: current dir)
zcrew disable [DIR]                                    remove it; CLAUDE.md is restored byte for byte
zcrew status [DIR]                                     enabled? which config? is it valid?
zcrew config [--project DIR]                           print and validate the effective config
zcrew doctor                                           readiness check (no model call)
zcrew update                                           pull the latest release and re-run setup
zcrew uninstall [--keep-config] [--yes]                remove zcrew
zcrew version
```

Useful phrases in chat:

- `use Flash for this task` switches to `GLM-5.3-Flash` for that task only.
- `go` / `ship` skips the clarification and approval steps.
- `switch the default model to …` makes Claude edit your config and validate it.

**Watching the crew:** Claude posts short progress lines while it works. To read a worker's full conversation, open the ZCode app: runs are listed under the project. Restart the app to refresh the list, because live view inside the app isn't possible.

## Configuration

zcrew merges three layers, and the first one found wins: `<repo>/.claude/zcode-commander.json` → `~/.zcode-commander/config.json` → built-in defaults.

```json
{
  "model": { "providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3" },
  "thoughtLevel": "max",
  "maxWorkers": 5,
  "maxCorrectionRounds": 4,
  "timeoutSeconds": 1800
}
```

| Key | Meaning | Allowed |
|---|---|---|
| `model.providerId` | ZCode account provider | An account provider your plan is entitled to |
| `model.modelId` | Model name | A model offered by that provider, e.g. `GLM-5.3`, `GLM-5.3-Flash` |
| `thoughtLevel` | Reasoning effort | `high`, `max` |
| `maxWorkers` | Parallel ZCode workers | 1–5 |
| `maxCorrectionRounds` | Review/correct rounds per task before Claude escalates to you | 1–10 |
| `timeoutSeconds` | Per-run timeout | 60–86400 |

`zcrew config` validates every value, including whether the model exists and whether your plan is entitled to it.

## Compatibility

| Component | Status |
|---|---|
| ZCode 3.14.0 | ✅ Validated end-to-end: single run, review/correction on the same thread, two parallel workers |
| Other ZCode versions | ⚠️ `zcrew doctor` warns. Run a small task first, because ZCode's headless protocol changed in 3.12 |
| Z.ai individual GLM Coding Plan | ✅ |
| Start / Team / Off-peak plans | ❌ These need the desktop host (captcha or encrypted keys) |
| macOS / Linux | ❌ Not yet. The bridge supports them; zcrew's installer and launcher are Windows-only |

ZCode 3.14 needed five fixes in the headless path: provider-table env vars, the `runtimeModel` removal, the account-provider push, runtime auth headers, and a required reasoning level. They are documented, with evidence, in [SPEC-RFD §26](SPEC-RFD.md#26-validation-results-v020-2026-09-26).

## Troubleshooting

Start with `zcrew doctor`. Every line says what to fix.

| Symptom | Fix |
|---|---|
| `ZCode CLI config: model.main is not set` | Sign in to ZCode Desktop, then run `zcrew update` |
| `[WARN] zcode version … untested` | ZCode was updated. Run a small task before relying on it |
| `Select a model before continuing` / `Provider Registry 中不存在 Model` | Check that the ZCode app is signed in with an individual Coding Plan, and that `model.providerId` is `account:zai-individual-coding-plan` |
| `Reasoning level is required …` | Make sure `~/.zcode-commander/bridge_compat.py` exists, then run `zcrew update` |
| Claude doesn't see `zcode_executor` | Quit Claude Code completely and start it again. Check with `claude mcp get zcode_executor` |
| Runs don't appear in the ZCode app | Restart the ZCode app, because it loads its task list at startup |
| Can't remove a worktree ("busy") | Run `git worktree prune`. The folder frees up once Claude Code exits |

ZCode's own logs are in `~/.zcode/cli/log/`. Diagnostic switches (environment variables):

- `ZCODE_COMMANDER_ACCOUNT_PROVIDER=off`
- `ZCODE_COMMANDER_RUNTIME_MODEL=on`
- `ZCODE_COMMANDER_DEFAULT_REASONING=high`

## Uninstall

```powershell
zcrew disable C:\path\to\each\enabled\repo
zcrew uninstall
```

zcrew never edits your global `~/.claude/CLAUDE.md`. The one exception is removing the import line left by a v0.1.x install, and only if that line is present.

## Development

```powershell
git clone https://github.com/b9b4ymiN/zcrew.git
cd zcrew
python -m unittest discover -s tests -v
```

```text
get.ps1                     one-command bootstrap (irm | iex)
scripts/zcrew.py            CLI
scripts/install.ps1         installer: bridge clone (pinned), MCP registration, policy, config seed
scripts/zcode_bridge_launcher.py   finds ZCode, sets env, starts the shim
scripts/bridge_compat.py    ZCode 3.12+ compatibility shim for coder-mcp-bridge
scripts/commander.py        enable/disable/status/config
scripts/doctor.py           readiness checks
policy/COMMANDER.md         the commander policy Claude follows
SPEC-RFD.md                 design rationale, decisions, validation results
```

If a change affects roles, the MCP architecture, acceptance, the correction loop, security boundaries or Windows assumptions, update `SPEC-RFD.md` and `CHANGELOG.md` as well (SPEC §24).

## Roadmap

- [ ] Validate policy adherence in fresh sessions on real projects
- [ ] macOS / Linux installer
- [ ] Track upstream ZCode releases, with a compatibility matrix in CI
- [ ] Optional OpenCode / Pi executors (already supported by the bridge)

## Credits

- [`coder-mcp-bridge`](https://github.com/Deslord319/coder-mcp-bridge) (MIT): the MCP control plane.
- [`zcode-acp`](https://github.com/william0wang/zcode-acp) (Apache-2.0): its account-provider approach for ZCode 3.12+ is ported in `bridge_compat.py`.
- [`zcode-executor`](https://github.com/KyoMio/zcode-executor): the commander/executor pattern with evidence-first acceptance.

See [NOTICE](NOTICE). zcrew is not affiliated with Anthropic, Zhipu AI / Z.ai, or the projects above.

## License

[Apache-2.0](LICENSE)
