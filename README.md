<div align="center">

# zcrew

**Claude Code or Codex plans and reviews. A crew of ZCode (GLM) agents writes the code.**

You work with one assistant, either Claude Code or OpenAI Codex. It hands implementation to ZCode workers and checks every diff itself. When something fails review, it sends the fix back to the same worker session, and it only reports to you once the work passes.

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](#tech-stack)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-commander-D97757)](https://docs.anthropic.com/en/docs/claude-code)
[![Codex](https://img.shields.io/badge/Codex-commander-111111?logo=openai)](https://github.com/openai/codex)
[![ZCode](https://img.shields.io/badge/ZCode-3.14.0%20tested-6E56CF)](#compatibility)
[![Tests](https://img.shields.io/badge/tests-294%20passing-2EA44F)](#development)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [ภาษาไทย](docs/README.th.md)

</div>

---

## Why

Frontier models are strongest at understanding, design and review. Coding agents are cheaper for the bulk of the typing. zcrew assigns each role to the model that fits it, and you still talk to a single assistant.

| Role | Who | Responsibility |
|---|---|---|
| **Client** | You | State the goal, approve the plan |
| **Commander** | Claude Code **or** Codex, per project, or both | Clarify → plan with Definition of Done → delegate → review diff and run tests → correct → report |
| **Crew** (up to 5) | ZCode · GLM-5.3 at `max` reasoning | Implement, write tests, fix what the reviewer rejects |

> **The executor never accepts its own work.** A worker saying "done" is a claim. Acceptance requires the commander to review `git diff` itself and run the tests itself.

## Features

- **One conversation.** No `/zcode` command and no copy-paste relay. The commander decides when to delegate.
- **Two commanders, one crew.** Use Claude Code for core logic and Codex for design work, or either one alone. Both drive the same ZCode crew, and the bridge's cross-process leases keep them from writing into the same folder at once.
- **Plan-first.** You get a short plan with phases, tasks and a Definition of Done. Nothing is delegated until you approve it.
- **Independent review loop.** The commander inspects the diff and runs your tests/build/lint itself. A rejection goes back to the same ZCode thread, so the worker keeps its context. Each task has a correction budget (default 4 rounds).
- **Parallel crew.** Up to 5 ZCode workers run on independent tasks in separate `git worktree`s. Branches are merged back one at a time, and tests are re-run after every merge.
- **Per-project opt-in.** `zcrew enable --commander claude|codex|both` activates the policy for one repo. `zcrew disable` restores its files byte for byte.
- **Context-aware.** Compact worker output (−70%), wait-until-done polling, context-hygiene rules for the commander, and `zcrew context` to measure what a session actually spent.
- **Configurable model.** GLM-5.3 at `max` by default. You can switch models globally, per project, or for a single task.
- **Live worker view.** `zcrew watch` (terminal) and `zcrew dashboard` (local browser page) show what each worker is doing as it happens: files read and edited, commands and their outcome, context size. Read-only, straight from ZCode's own database.
- **One-command install** with a built-in `doctor` that checks readiness without making any model call.

## Quick start

**1. Install** (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/b9b4ymiN/zcrew/main/get.ps1 | iex
```

The installer registers the `zcode_executor` MCP server in every commander it finds: Claude Code, Codex, or both. To choose explicitly, set `$env:ZCREW_COMMANDER = 'codex'` (or `claude` / `both`) before running it.

**2. Restart your commander app.** Quit Claude Code and/or the Codex app fully (including from the system tray) so it loads the `zcode_executor` MCP server.

**3. Enable a project:**

```powershell
cd C:\path\to\your-repo
zcrew enable                      # Claude Code (default)
zcrew enable --commander codex    # Codex
zcrew enable --commander both     # either one, whichever you open
```

**4. Open a new Claude Code or Codex session in that repo and ask for work as usual:**

```text
> Add a /health endpoint that returns the app version as JSON.
```

The commander asks a clarifying question or two, sends a plan for your approval, and then runs the ZCode crew. It reports once every task has passed its review.

## Requirements

| | Version | Notes |
|---|---|---|
| Windows | 10 / 11 | PowerShell 5.1 or 7 |
| [ZCode Desktop](https://z.ai) | **3.14.0** (tested) | Signed in with a **Z.ai individual GLM Coding Plan** |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and/or [Codex](https://github.com/openai/codex) | recent (Codex CLI 0.157 tested) | At least one installed and signed in |
| Python | 3.10+ | Must be on `PATH` as `python`. zcrew uses the standard library only |
| Git | any recent | Your projects must be git repos, because review relies on `git diff` |

## How it works

```mermaid
flowchart LR
    U([You]) -- brief / approve --> C["Commander<br/>Claude Code or Codex"]
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
    participant Claude as Commander (Claude Code / Codex)
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

The commander reviews the result against that same contract. The full behaviour is defined in [`policy/COMMANDER.md`](policy/COMMANDER.md), and an example contract is in [`examples/worker-contract.md`](examples/worker-contract.md).

## Tech stack

| Layer | Technology |
|---|---|
| Commander | Claude Code (policy imported via `CLAUDE.md`) or OpenAI Codex (policy inlined in `AGENTS.override.md`). One Markdown policy serves both |
| Control plane | [Model Context Protocol](https://modelcontextprotocol.io) over stdio (JSON-RPC 2.0) |
| MCP server | [`coder-mcp-bridge`](https://github.com/Deslord319/coder-mcp-bridge) (Python), pinned commit, used unmodified |
| Compatibility layer | `bridge_compat.py`: a runtime shim that adapts the bridge to ZCode 3.12+ (account-provider snapshot, runtime auth headers, reasoning level) |
| Executor | ZCode Desktop app-server (Electron/Node, headless), running GLM-5.3 on the Z.ai Coding Plan |
| Isolation | `git worktree` per parallel worker, with cross-process resource leases in the bridge |
| Tooling | PowerShell installer (5.1/7 compatible), Python 3.10+ standard-library CLI (`zcrew`), `unittest` (294 tests) |

## Usage

```text
zcrew enable [DIR] [--commander claude|codex|both] [--with-templates] [--with-project-config] [--force]
                                                       activate the policy for a repo (default: current dir, claude)
zcrew disable [DIR] [--commander claude|codex]         remove it (default: everything); files restored byte for byte
zcrew status [DIR]                                     enabled? which config? is it valid?
zcrew config [--project DIR]                           print and validate the effective config
zcrew doctor                                           readiness check (no model call)
zcrew context [DIR] [--session ID|PATH] [--json]       where the last commander session's context went (per tool)
zcrew watch [DIR] [--all] [--since MIN]                follow ZCode workers live in the terminal
zcrew dashboard [DIR] [--all] [--port N] [--no-open]   the same in a local browser page (127.0.0.1 only)
zcrew runs [DIR] [--all] [--limit N] [--json]          list worker runs, newest first
zcrew show SESSION [--json]                            one run's activity (id or prefix, e.g. sess_83cb)
zcrew stats [DIR] [--all] [--days N] [--json]          time, correction turns, errors and parallelism of recent tasks
zcrew gate [RUN|THREAD] [--self] [--accept|--reject]   mechanical review gate: scope, DO NOT, tests, VERIFY
zcrew capacity [--days N] [--apply [--limit N]] [--json]   how many workers the plan carries at once; set the cap
zcrew codex-hook install|uninstall [DIR]               Codex Stop hook that wakes the commander (enable/disable do it)
```

**Parallel workers and the plan's concurrency limit.** Z.ai does not publish how many requests a Coding Plan runs at once, and it moves the limit with load. Past the limit a request fails with `1302` ("Rate limit reached for requests" / "High concurrency usage of this API"). `zcrew capacity` measures the limit from your own history: it matches every 1302 in ZCode's database with the number of workers that were busy at that moment. `zcrew capacity --apply` stores the result as `concurrencyLimit`. The bridge then lets `concurrencyTarget` percent of it run at once (default 80%, never above `maxWorkers`), across every Claude Code and Codex session on the machine. Runs over the cap wait in the queue (`resource.waiting` on a `zcrew:worker-slot` key). After a fresh 1302 the cap drops by one for 10 minutes. Until a 1302 has happened, the limit is unknown and `maxWorkers` is the cap. GLM Coding Pro users report about 3 sessions at once; this is unofficial, so `zcrew capacity --apply --limit 3` is a starting point to replace with your own measurement.

**Codex commanders** get the same hands-free waiting as the Claude Code mod through a Codex `Stop` hook. `zcrew enable --commander codex` adds it to `.codex/hooks.json`, and Codex asks you to trust it on the next start (or use `/hooks`). After starting workers, the commander ends its turn with `[zcrew:wait]`. The hook waits for up to 45 minutes and then wakes the commander with what ended or stalled, plus the `zcrew gate` result. The hook never blocks a turn that asks you a question.

Claude Code users can add the zcrew mod: Claude stops polling workers (the mod watches them and wakes Claude when a run ends, with its gate result), and a band above the prompt shows what the workers are doing. See [mod/zcrew/README.md](mod/zcrew/README.md).

```
/plugin install zcrew --marketplace b9b4ymiN/zcrew
zcrew update [--commander auto|claude|codex|both]     pull the latest release and re-run setup
zcrew uninstall [--keep-config] [--yes]                remove zcrew
zcrew version
```

Useful phrases in chat:

- `use Flash for this task` switches to `GLM-5.3-Flash` for that task only.
- `go` / `ship` skips the clarification and approval steps.
- `switch the default model to …` makes the commander edit your config and validate it.

**Context budget:** `zcrew context` reads the latest Claude Code session for the project (`--commander codex` for Codex) and reports the first, peak and last context size, plus tool-output share per tool and per MCP server, with hints. It shows only numbers and tool names, and it never writes anything. The policy's *Context hygiene* rules (log long output to a file, read ranges, delegate exploration, review diffs) target the biggest consumers it finds.

**Watching the crew:** open a second terminal in the project and run `zcrew watch`, or `zcrew dashboard` for a browser view. Each worker gets a label (`w1`, `w2`, …) and every line is one action:

```text
14:02:11 w1 sess_83cb1cd2 > started: Add login API
14:02:13 w1 R src/auth.ts
14:02:15 w1 E src/auth.ts (+24 -3)
14:02:31 w1 $ npm test -> 2 failed
14:03:40 w1 # turn completed: 9 tool call(s), 0 error(s), 1m29s
```

Both read ZCode's local database (`~/.zcode/cli/db/db.sqlite`) read-only, so they also show runs started from the ZCode app or in parallel worktrees. They never show the model's reasoning or full tool output. The dashboard binds to `127.0.0.1` only and rejects other Host headers. If ZCode ever changes how it stores data, a self-check switches to a slower exact mode and prints a one-line note instead of showing wrong numbers.

## Project instruction files

Each role reads its own file, so every agent gets only the instructions meant for it:

| File | Read by | Put here |
|---|---|---|
| `CLAUDE.md` | **Claude Code** (commander); Codex reads it too when told by its policy | What the project is, its architecture and boundaries, the **VERIFY commands** (test/typecheck/lint/build), what never changes without asking, and the zcrew import block |
| `AGENTS.override.md` | **Codex** (commander). Codex prefers it over `AGENTS.md` in the same folder | The zcrew commander policy, inlined by `zcrew enable --commander codex` (Codex has no file imports) |
| `AGENTS.md` | **ZCode** (workers). ZCode loads it automatically from the working directory and its parents | Standing worker rules (no git commits, stay in SCOPE, never weaken tests, no secrets), the **report format**, and project commands and style |

Keeping the Codex policy in `AGENTS.override.md` is what lets Codex act as commander while ZCode, which reads `AGENTS.md`, stays a worker. After `zcrew update`, `zcrew status` flags a Codex policy copy that is out of date. Re-run `zcrew enable --commander codex` to refresh it.

Start from the templates:

```powershell
zcrew enable --with-templates
```

- Creates `CLAUDE.md` and `AGENTS.md` from [`templates/`](templates/). A file that already exists is never overwritten.
- Fill in the `<...>` placeholders, then **commit `AGENTS.md`**. Parallel workers run in `git worktree`s and only see committed files.
- `zcrew disable` deletes a template file only if you never edited it. Edited files are kept.
- `zcrew status` shows whether `AGENTS.md` is still the unedited template.

## Configuration

zcrew merges three layers, and the first one found wins: `<repo>/.claude/zcode-commander.json` → `~/.zcode-commander/config.json` → built-in defaults.

```json
{
  "model": { "providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3" },
  "thoughtLevel": "max",
  "maxWorkers": 5,
  "maxCorrectionRounds": 4,
  "timeoutSeconds": 1800,
  "concurrencyLimit": 0,
  "concurrencyTarget": 80
}
```

| Key | Meaning | Allowed |
|---|---|---|
| `model.providerId` | ZCode account provider | An account provider your plan is entitled to |
| `model.modelId` | Model name | A model offered by that provider, e.g. `GLM-5.3`, `GLM-5.3-Flash` |
| `thoughtLevel` | Reasoning effort | `high`, `max` |
| `maxWorkers` | Most ZCode workers running at once, machine-wide | 1–10 |
| `concurrencyLimit` | The plan's measured concurrency limit (`zcrew capacity --apply`); 0 = not measured. Read from the user file only | 0–50 |
| `concurrencyTarget` | Percent of `concurrencyLimit` to use at once (read from the user file only) | 50–100 |
| `maxCorrectionRounds` | Review/correct rounds per task before the commander escalates to you | 1–10 |
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
| Claude Code / Codex doesn't see `zcode_executor` | Quit the app completely and start it again. Check with `claude mcp get zcode_executor` or `codex mcp get zcode_executor`. Run `zcrew update --commander both` to register it in both |
| Codex acts as a worker instead of a commander | Run `zcrew status`; if Codex is not enabled or the policy is STALE, run `zcrew enable --commander codex`, then start a new Codex session |
| Runs don't appear in the ZCode app | Restart the ZCode app, because it loads its task list at startup |
| Can't remove a worktree ("busy") | Run `git worktree prune`. The folder frees up once the commander app exits |

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
scripts/context_report.py   zcrew context
scripts/zcode_activity.py   read-only reader over ZCode's database (sessions + activity)
scripts/activity_cli.py     zcrew runs / show / stats / watch
scripts/gate.py             zcrew gate (reads the contract the bridge recorded at agent-start)
scripts/capacity.py         zcrew capacity (concurrency limit from 1302 errors; the cap lives in bridge_compat)
scripts/codex_hook.py       Codex Stop hook: wakes a Codex commander when workers end
scripts/dashboard.py        zcrew dashboard server (+ dashboard.html)
templates/                  CLAUDE.md + AGENTS.md starters for projects
policy/COMMANDER.md         the commander policy (Claude Code and Codex)
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
