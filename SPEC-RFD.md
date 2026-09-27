# Claude Commander × ZCode Executor
## SPEC / Request for Design (RFD)

**Version:** 0.2.0  
**Baseline implementation:** this repository (v0.2.0)  
**Previous baselines:** `zcode-claude-commander-kit-v0.1.1.zip`, `zcode-claude-commander-kit-v0.1.0.zip`  
**Date:** 2026-09-26  
**Status:** Validated on the target Windows workstation (see §26)

---

## 1. Executive Summary

This project exists to let the user work with **Claude Code / Opus as the single conversational interface and technical commander**, while delegating implementation-heavy work to **ZCode / GLM as an executor**.

The intended relationship is:

> **Claude Code = Commander / Architect / Reviewer**  
> **ZCode = Executor / Implementer**

The user should not manually relay tasks between the two agents. Claude is responsible for understanding the request, deciding what should be built, delegating implementation, independently checking the resulting code and verification evidence, sending corrective instructions back to the same ZCode session when necessary, and only then reporting completion.

The design deliberately reuses existing prior art instead of inventing a new ZCode protocol or MCP server. The initial implementation uses `coder-mcp-bridge` as the MCP control plane, draws orchestration ideas from `zcode-executor`, and uses `zcode-acp` / `zcode-open-bridge` as protocol and lifecycle references.

---

## 2. Why This Project Exists

The original requirement was not simply “let Claude call ZCode.” The actual goal is to create a **closed implementation loop** in which Claude remains the only AI the user needs to talk to.

The desired user experience is:

```text
User
  ↓
Claude Code / Opus
  ↓
ZCode Executor
  ↓
Claude Review
  ↓
ZCode Correction (if needed)
  ↓
Claude Acceptance
  ↓
User
```

The user should be able to say something natural such as:

```text
We agreed on the Plan Inspector direction.
Implement it completely and verify it.
```

From that point onward, Claude should autonomously:

1. inspect the project;
2. understand the requirement and current architecture;
3. decide the implementation approach;
4. define scope and acceptance criteria;
5. delegate implementation to ZCode;
6. wait for execution to finish;
7. inspect the actual diff and repository state;
8. run or inspect appropriate tests/build/typecheck/lint checks;
9. review architecture and requirement compliance;
10. send corrective instructions back to the same ZCode thread if the work is incomplete or incorrect;
11. repeat verification until the task passes or a genuine blocker requires human input;
12. report the result to the user.

The user should **not** have to type `/zcode`, “send this to ZCode,” or manually babysit each iteration.

---

## 3. Initial Idea and Why It Changed

The first idea was a manual Claude Code command such as:

```text
/zcode implement the plan we just agreed on
```

That would technically connect Claude and ZCode, but it still puts orchestration responsibility on the user. The user would have to decide when to invoke ZCode, when to ask Claude to review, and when to send corrections.

That does not satisfy the real requirement.

The design therefore changed from a **manual command** into an **automatic commander/executor relationship**:

```text
User talks normally to Claude
        ↓
Claude decides when implementation should be delegated
        ↓
Claude invokes ZCode through MCP
        ↓
Claude independently validates the result
        ↓
Claude controls any repair loop
```

Slash commands may remain useful for diagnostics or explicit overrides, but they are not part of the normal user experience.

---

## 4. Product Definition

> **Claude Commander × ZCode Executor is a local agent orchestration layer that allows the user to work only with Claude Code / Opus while Claude delegates implementation to ZCode, independently reviews the resulting changes, sends corrections, and loops until the agreed acceptance criteria pass.**

---

## 5. Core Responsibility Split

### 5.1 Claude Code Owns

Claude is the commander and acceptance authority. It owns:

- conversation with the user;
- requirement discovery and clarification;
- repository investigation;
- research and prior-art analysis;
- architecture and design decisions;
- task decomposition;
- implementation strategy;
- worker contract creation;
- scope control;
- acceptance criteria;
- code and diff inspection;
- test/build/typecheck/lint verification;
- architecture review;
- deciding pass/fail;
- deciding whether to reuse an existing worker session;
- corrective instructions;
- deciding when a blocker truly requires user input;
- final reporting to the user.

Claude may read and inspect code freely. The policy should bias Claude away from routine implementation when ZCode can execute it effectively.

### 5.2 ZCode Owns

ZCode is the executor. It owns:

- routine coding;
- file editing;
- component creation;
- implementation refactoring;
- writing tests;
- fixing compilation/type/lint errors;
- running implementation-related commands;
- applying corrective instructions from Claude;
- reporting what it changed and what commands it ran.

ZCode must **not** independently:

- change product requirements;
- expand scope without approval;
- make major architecture changes without Claude approval;
- perform destructive migrations without approval;
- deploy to production;
- declare its own work accepted.

---

## 6. Independent Acceptance Principle

A central rule of this design is:

> **The executor cannot accept its own work.**

Messages from ZCode such as:

```text
Done.
All tests pass.
Implementation completed.
```

are useful status signals, but they are not acceptance evidence by themselves.

Claude must independently inspect appropriate evidence, which may include:

- `git diff`;
- `git status`;
- changed files;
- tests;
- typecheck;
- build;
- lint;
- runtime/UI behavior where available;
- acceptance criteria;
- architecture constraints;
- absence of unrelated changes.

Only Claude can decide that the task is complete from the commander's perspective.

---

## 7. Autonomous Review / Repair Loop

The required control loop is:

```text
                 ┌──────────────┐
                 │     User     │
                 └──────┬───────┘
                        │
                        ▼
                 Claude Commander
                        │
                        │ Worker Contract
                        ▼
                  ZCode Executor
                        │
                        │ Code changes
                        ▼
                  Claude Review
                   ┌────┴────┐
                   │         │
                 PASS       FAIL
                   │         │
                   │         ▼
                   │   Corrective Task
                   │         │
                   │         ▼
                   │   Same ZCode Thread
                   │         │
                   │         └────────┐
                   │                  │
                   └──────────────◄───┘
                        │
                        ▼
                       User
```

The normal loop is:

```text
Plan
→ Delegate
→ Implement
→ Inspect
→ Verify
→ Review
→ Correct if necessary
→ Verify again
→ Accept
```

User intervention should be zero during routine implementation iterations.

---

## 8. Worker Contract

Claude should not normally delegate with a vague one-line prompt such as:

```text
Implement feature X.
```

Instead it should establish a bounded **Worker Contract** containing enough information for ZCode to execute without taking product authority.

Recommended structure:

```text
OBJECTIVE
Implement Scenario Comparison in Plan Inspector.

CONTEXT
Planner needs to compare current allocation with a proposed lot allocation
before committing a change.

SCOPE
- components/PlanInspector/*
- hooks/useScenario.ts

DO NOT
- change backend API contracts
- modify Memgraph schema
- introduce another UI framework
- refactor unrelated modules

ACCEPTANCE CRITERIA
1. Current and proposed scenarios are visible.
2. PDD delta is displayed.
3. Capacity violations are clearly identified.
4. Existing planner behavior remains unchanged.
5. New behavior has appropriate tests.

VERIFY
- npm test
- npm run typecheck
- npm run build
```

Working principle:

> **Task/contract = source of truth**  
> **Prompt = instruction to execute the contract**

This makes later review deterministic: Claude checks the result against the same contract it used to delegate the work.

---

## 9. Session Continuity

The executor should not be treated as stateless when a task requires multiple corrections.

A conceptual session registry may look like:

```text
frontend → ZCode thread A
backend  → ZCode thread B
graph    → ZCode thread C
tests    → ZCode thread D
```

If Claude reviews frontend work from thread A and finds three defects, the preferred behavior is to send the correction back to **thread A**, not create a fresh worker with no memory of the implementation.

Benefits:

- less repeated context;
- lower token usage;
- faster corrective turns;
- stronger continuity;
- easier iterative refinement.

Session reuse should be task-aware rather than permanent. Claude decides whether new work is a continuation of an existing thread or deserves a new executor context.

---

## 10. Prior-Art Research

The project follows the principle:

> **Do not reinvent infrastructure when mature prior art already exists.**

Research identified four especially relevant projects.

### 10.1 `KyoMio/zcode-executor`

This project is the closest prior art for the desired workflow. Its design demonstrates the pattern:

```text
Claude plans
→ ZCode implements
→ Claude reviews diff/tests
→ Claude sends corrections
→ same ZCode session fixes
```

Key ideas adopted conceptually:

- task contracts;
- evidence-first acceptance;
- correction loops;
- same-session repair;
- worker self-report is not acceptance evidence;
- worktree/isolation thinking.

Constraint for this project: its primary implementation targets macOS/Linux, while our target workstation is Windows.

Repository: <https://github.com/KyoMio/zcode-executor>

### 10.2 `Deslord319/coder-mcp-bridge`

This project supplies the control-plane architecture we need and supports coding-agent backends including ZCode.

Relevant MCP concepts include tools such as:

```text
agent-start
agent-wait
agent-observe
agent-control
agent-recover
agent-branch
agent-context
agent-close
```

Decision:

> **Reuse this bridge rather than create a new MCP server or ZCode protocol wrapper.**

Repository: <https://github.com/Deslord319/coder-mcp-bridge>

### 10.3 `william0wang/zcode-acp`

Used as a reference for ZCode session lifecycle and protocol behavior, including concepts such as:

```text
session/create
session/send
session/update
session/resume
session/cancel
session/fork
session/goal
session/compact
```

It demonstrates that ZCode already exposes sufficient session infrastructure for persistent executor workflows.

Repository: <https://github.com/william0wang/zcode-acp>

### 10.4 `tizerluo/zcode-open-bridge`

Useful as a reference for ZCode CLI/app-server integration and MCP-related patterns. Its MCP use cases are more review-oriented than the autonomous implementation loop targeted here, so it is treated as reference material rather than the primary control plane.

Repository: <https://github.com/tizerluo/zcode-open-bridge>

---

## 11. Architecture Decision

### 11.1 Architecture Rejected

Do not build this from scratch:

```text
Claude
  ↓
Our custom ZCode MCP server
  ↓
Our custom ZCode protocol implementation
  ↓
ZCode
```

This would duplicate already-solved protocol work and create unnecessary maintenance debt.

### 11.2 Selected Architecture

```text
              ┌───────────────────────────┐
              │ Claude Code / Opus        │
              │                           │
              │ Commander                 │
              │ Architect                 │
              │ Reviewer                  │
              └────────────┬──────────────┘
                           │
                    Commander Policy
                           │
                           ▼
              ┌───────────────────────────┐
              │ coder-mcp-bridge          │
              │                           │
              │ MCP Control Plane         │
              └────────────┬──────────────┘
                           │
                     ZCode app-server
                           │
                           ▼
              ┌───────────────────────────┐
              │ ZCode / GLM               │
              │                           │
              │ Executor                  │
              │ Implementer               │
              └───────────────────────────┘
```

Our custom code remains intentionally thin:

1. Windows launcher / discovery compatibility;
2. Claude Commander policy;
3. install / doctor / uninstall tooling;
4. documentation and examples.

---

## 12. Why MCP

MCP gives Claude a structured control surface rather than forcing it to generate arbitrary shell commands for each worker interaction.

Advantages:

- structured tool input/output;
- clearer semantics for the model;
- session lifecycle can be represented explicitly;
- less quoting/path/escaping complexity;
- less coupling between Claude policy and ZCode internals;
- easier substitution of another executor later;
- clearer security and permission boundaries;
- easier diagnostics.

The user still sees one conversation with Claude; MCP is an implementation detail behind that UX.

---

## 13. Windows Constraint and Compatibility Layer

The target machine is Windows.

The upstream MCP bridge contains Windows-compatible stdio handling, but its ZCode discovery logic has historically focused on macOS/Linux layouts. The kit therefore adds a thin Windows launcher that searches likely local ZCode Desktop paths, including layouts such as:

```text
%LOCALAPPDATA%\Programs\ZCode\
    ZCode.exe
    resources\
        glm\
            zcode.cjs
```

The launcher then passes explicit environment variables such as:

```text
ZCODE_BINARY
ZCODE_CLI_BUNDLE
```

to the upstream bridge.

Design rule:

> **Do not fork or duplicate the ZCode protocol just to solve path discovery.**

---

## 14. v0.1.1 Artifact Scope

The current kit is an integration baseline rather than a finished production agent platform.

### Included

- Windows-aware bridge launcher;
- installer;
- diagnostics/doctor utility;
- uninstall utility;
- Claude Commander policy;
- Worker Contract example;
- upstream bridge reuse;
- research notes;
- this SPEC/RFD baseline;
- changelog.

### Not Included Yet

- cloud orchestration;
- multi-machine workers;
- autonomous deployment;
- automatic PR merging;
- persistent worker-history database;
- GUI dashboard;
- production CI integration;
- model performance scoring;
- automatic executor/model selection;
- full multi-worker scheduler.

The first priority is to prove the smallest useful closed loop reliably on the real Windows workstation.

---

## 15. Correction Budget / Runaway Protection

An autonomous loop must be bounded.

The initial policy uses a default correction budget of approximately:

```text
MAX_CORRECTION_ROUNDS = 4
```

This is a policy-level guardrail rather than a permanent magic number.

If repeated attempts fail, Claude should stop the loop and report:

- what has already been attempted;
- which checks pass;
- which checks fail;
- the likely blocker/root cause;
- what evidence was collected;
- what decision or access is needed from the user.

The purpose is to prevent silent infinite agent loops while still avoiding unnecessary human interruption for routine fixes.

---

## 16. When Claude Should Ask the User

Claude should **not** ask questions such as:

```text
Should I send this to ZCode?
Should I review ZCode's changes now?
Should I ask ZCode to fix the test?
```

Those are internal orchestration decisions.

Claude should return to the user when the blocker is materially human-owned, for example:

- a requirement ambiguity changes product behavior;
- two architectural choices have materially different trade-offs;
- a destructive or irreversible migration is required;
- a significant security/privacy implication appears;
- required credentials/permissions are missing;
- scope must expand substantially;
- an external dependency cannot be accessed;
- repeated correction attempts fail and require a strategic decision.

---

## 17. Security / Authority Model

Use least privilege where practical.

ZCode should not automatically gain authority to:

- deploy production;
- modify credentials/secrets;
- push or merge remote branches without policy approval;
- perform destructive database migrations;
- edit unrelated workspaces;
- alter architecture outside the worker contract.

Claude remains the gatekeeper for high-impact actions, while user approval remains authoritative where required.

---

## 18. Prototype Success Criteria

The prototype is considered validated when the following real workflow succeeds on the target Windows environment.

### User request

```text
Implement feature X according to the direction we agreed on.
Finish it and verify it.
```

### Required behavior

Claude:

1. inspects the source;
2. establishes the implementation approach;
3. defines a Worker Contract;
4. invokes ZCode through MCP;
5. ZCode modifies the source;
6. Claude inspects the actual diff;
7. Claude performs independent verification;
8. Claude identifies at least one defect or intentionally simulated acceptance failure during validation testing;
9. Claude sends corrective instructions to the **same** ZCode thread;
10. ZCode applies the correction;
11. Claude re-verifies;
12. Claude accepts the work;
13. Claude reports completion to the user.

During the implementation/review loop:

```text
User intervention required = 0
```

unless a genuine blocker occurs.

---

## 19. Definition of Done for an Individual Task

A delegated implementation is done only when relevant evidence supports all required conditions:

```text
✓ Requirement satisfied
✓ Acceptance criteria satisfied
✓ Diff independently reviewed by Claude
✓ Relevant tests pass
✓ Typecheck/build/lint pass where applicable
✓ No unexplained architecture deviation
✓ No unrelated modifications
✓ Executor claims have been independently verified
```

This is explicitly **not** sufficient:

```text
ZCode says "done"
```

---

## 20. Current Validation Status

### Completed / Designed

- prior-art research;
- Commander vs Executor responsibility split;
- decision not to create a new ZCode protocol;
- decision to reuse `coder-mcp-bridge`;
- autonomous review/correction-loop design;
- Worker Contract pattern;
- same-thread correction policy;
- Windows bootstrap design;
- Commander policy;
- install/doctor/uninstall tooling;
- bounded correction-loop concept;
- initial ZIP prototype;
- documentation baseline.

### Proven End-to-End (v0.2.0)

The following passed on the real Windows workstation: integration validation (Phase 1), closed-loop validation (Phase 2), and a two-worker parallel run. Section 26 records the evidence and the compatibility fixes each one needed.

### Not Yet Proven

- A fresh Claude Code session in an enabled project following the v2 policy by itself (brief → plan approval → delegation), with no human-driven session steering it. The first real use counts as that validation.
- ZCode versions other than 3.14.0.
- Coding plans other than the individual GLM Coding Plan. Start, team and off-peak plans need the desktop host.

---

## 21. Planned Phases

### Phase 1 — Integration Validation

Prove:

```text
Claude → MCP → ZCode
```

Checks:

- MCP server registers correctly;
- Windows ZCode runtime is detected;
- ZCode app-server starts;
- executor start/wait/observe operations work;
- ZCode can modify a controlled test repository;
- result events return reliably to Claude.

### Phase 2 — Closed-Loop Validation

Prove:

```text
Claude
→ ZCode implementation
→ Claude rejection
→ same-thread ZCode correction
→ Claude acceptance
```

This is the most important functional milestone.

### Phase 3 — Commander Intelligence

Tune policy for:

- when to delegate;
- when Claude should perform tiny edits itself;
- when to reuse a worker session;
- when to create a new session;
- when to stop a failing loop;
- when to ask the user;
- how much verification is appropriate by task type.

### Phase 4 — Multi-Worker (Only If Needed)

Potential roles:

```text
frontend worker
backend worker
graph worker
test worker
```

Do not add this complexity before the single-worker closed loop is proven reliable.

---

## 22. Design Principles

1. **One conversational interface**  
   The user talks to Claude Code, not a collection of agents.

2. **Reasoning and execution have different owners**  
   Claude spends expensive reasoning on understanding, design, review, and decisions; ZCode absorbs implementation workload.

3. **No manual relay**  
   Internal agent handoffs are Claude's responsibility.

4. **Independent verification**  
   The worker cannot approve itself.

5. **Evidence over self-report**  
   Diff/tests/build/acceptance evidence matters more than agent narration.

6. **Reuse before reinvent**  
   Existing bridges/protocol implementations are preferred over custom infrastructure.

7. **Thin integration layer**  
   Custom code should remain small enough that upstream improvements can be adopted easily.

8. **Bounded autonomy**  
   Automatic correction is desirable; infinite loops are not.

9. **Session continuity when useful**  
   Corrections should normally remain on the same executor thread.

10. **Complexity must earn its place**  
    Multi-agent scheduling, dashboards, CI automation, and advanced persistence are future work only if real usage justifies them.

---

## 23. Repository / Artifact Layout

The kit currently uses a layout similar to:

```text
zcode-claude-commander-kit/
├─ README.md
├─ NOTICE
├─ SPEC-RFD.md
├─ RESEARCH-NOTES.md
├─ CHANGELOG.md
├─ manifest.json
├─ policy/
│  └─ COMMANDER.md
├─ examples/
│  └─ worker-contract.md
├─ get.ps1                (v0.3.0: one-command bootstrap)
├─ LICENSE                (v0.3.0: Apache-2.0)
├─ docs/README.th.md      (v0.3.0: Thai guide)
└─ scripts/
   ├─ zcrew.py            (v0.3.0: CLI)
   ├─ bridge_compat.py    (v0.2.0)
   ├─ commander.py        (v0.2.0)
   ├─ install.ps1
   ├─ uninstall.ps1
   ├─ doctor.py
   └─ zcode_bridge_launcher.py
```

Relationship between artifacts:

- `README.md` — setup and usage entry point;
- `SPEC-RFD.md` — why the system exists, architecture, responsibilities, decisions, boundaries, next phases;
- `RESEARCH-NOTES.md` — concise prior-art notes;
- `COMMANDER.md` — executable behavioral policy for Claude;
- `worker-contract.md` — example delegation format;
- scripts — Windows integration/bootstrap and diagnostics;
- `CHANGELOG.md` — version-to-version decisions and changes.

---

## 24. Versioning Rule Going Forward

Any version that changes one of the following should update this SPEC/RFD and the changelog:

- Commander/Executor ownership;
- MCP/control-plane architecture;
- acceptance model;
- session lifecycle;
- correction-loop behavior;
- security/authority boundaries;
- Windows integration assumptions;
- major supported workflows.

Validation findings should be recorded rather than silently changing the design. This document is intended to preserve the reasoning behind the system, not just describe its current files.

---

## 25. Immediate Next Action

The v0.1.1 action list is complete: install, doctor, one controlled task, one forced review failure, a same-thread correction, and recorded results. Section 26 has the details.

Next:

```text
1. Enable the policy in one real project and use it normally.
2. Record whether a fresh session follows the brief -> plan -> approval -> delegation flow unaided.
3. Re-run doctor after every ZCode update; treat a version warning as "validate with a small task first".
```

---

## 26. Validation Results (v0.2.0, 2026-09-26)

**Environment:**

- Windows 11, with Windows PowerShell 5.1 running the installer
- Python 3.12.4
- Claude Code 2.1.283
- ZCode Desktop 3.14.0, installed per machine under `C:\Program Files\ZCode`
- Z.ai individual GLM Coding Plan
- `coder-mcp-bridge` pinned at `23ecf0a`

### 26.1 Compatibility gates found and fixed

The v0.1.x design assumed the upstream bridge would drive ZCode unchanged. On ZCode 3.14, five gates blocked the first run.

All five were fixed in our thin layer (the launcher plus the `bridge_compat.py` shim). The bridge was not forked, and the protocol was not re-implemented.

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Probe: "ZCode runtime not found" | Bridge discovery requires `ZCODE_APP_PATH` before it honours explicit binary/bundle paths (macOS/Linux layout logic) | Launcher sets `ZCODE_APP_PATH` to the install root |
| 2 | app-server exits in <2 s: "无法定位 CLI ZCode Built-in Provider Config" | The headless CLI looks for `zcode-builtin.json` next to the bundle or five levels up; the Windows desktop layout keeps it in `resources\config\provider` | Launcher sets `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` and `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` (both are required together; otherwise the CLI re-syncs into a version-keyed copy) |
| 3 | `Unrecognized key: "runtimeModel"` | ZCode ≥3.12 validates `session/create`/`send`/`resume` strictly; the `runtimeModel` overlay was removed | Shim disables the bridge's runtime-model derivation |
| 4 | "Select a model before continuing" / "Provider Registry 中不存在 Model" | ZCode ≥3.12 builds the model registry from an account snapshot the desktop host pushes (`provider/updateAccountConfig`), and asks the host for per-request auth (`interaction/requestProviderRuntimeHeaders`); a headless app-server has no host | Shim pushes the snapshot after spawn and answers runtime-header requests with the individual plan's key (ported from `william0wang/zcode-acp`, Apache-2.0) |
| 5 | "Reasoning level is required for …/GLM-5.3" | An object `model` must carry `options.reasoningLevel`; the bridge's MCP schema cannot pass options | Shim derives `reasoningLevel` from `thoughtLevel` (default `max`) |

The registry model id on this machine is `account:zai-individual-coding-plan` / `GLM-5.3`. The desktop's `builtin:zai-coding-plan` maps to it.

### 26.2 Closed-loop evidence (sandbox repository)

- **Run 1:** worker contract "add `subtract`".
  - GLM-5.3 at `max` finished in 65 s and changed 2 files, all within scope.
  - Claude reviewed `git diff` itself and ran the tests itself: 3/3 passed.
- **Review:** FAIL (intentional, per §18) because the float case was not covered.
  - The correction went to the **same** `threadId`.
  - `sessionUsage` kept accumulating from run 1, which shows the context was reused.
- **Run 2:** changed only the test file, as instructed.
  - Claude re-ran the tests (4/4 passed), accepted the work, and closed the run.

User intervention during the loop: 0.

### 26.3 Parallel workers

Two workers ran at the same time in separate `git worktree`s, each with its own resource lease. Each finished in about 87 s.

Each worktree was reviewed on its own. The branches were then merged one at a time, with the tests re-run after every merge (7 → 11 tests), and the worktrees were cleaned up.

Lesson: on Windows, close the runs before removing their worktrees, because the bridge's app-server can keep the folder open.

### 26.4 Visibility

- **Live view inside the ZCode app is not possible.** The desktop app owns its own stdio app-servers.
- **History view works with no database writes.** Headless runs are added to the app's task list automatically. They appear under the project after the app is restarted.
- Runs scoped to a worktree were not seen in the list.

### 26.5 Decisions recorded in v0.2.0

- **Per-project activation.** A project gets the policy only when `commander.py enable` is run in it. The policy is never added to the user-global `CLAUDE.md`.
- **Approval gate.** The user chose a plan-approval gate before delegation: a brief, then a 200–300 word plan with phases, tasks and DoD, then approval, then the autonomous delegate/review/correct loop. This keeps "zero intervention during the loop" (§7) and still gives the user control over direction.
- **Account-provider path (option A) instead of a separate API-key provider (option B).** Option A uses the same entitlement path as the desktop app. The trade-off is that it can break when ZCode updates. That risk is reduced by pinning ZCode 3.14.0, a doctor version warning, and a doctor app-server smoke test.
- **Model and limits** live in `~/.zcode-commander/config.json`, and a project can override them in `.claude/zcode-commander.json`. Defaults: GLM-5.3, `max`, 5 workers, 4 correction rounds.

---

## 27. v0.5.0: Codex as a second commander, and context economy (2026-09-27)

### 27.1 Decisions

- **Two commanders, one crew.** Claude Code or OpenAI Codex can act as commander, per project or both together. ZCode is always the worker. The owner's intended split is Claude for core logic and Codex for design work.
- **Role isolation by file.** Codex loads `AGENTS.md`, and so do the ZCode workers. The commander policy for Codex therefore goes in `AGENTS.override.md`. Codex prefers that file over `AGENTS.md` in the same directory, and ZCode ignores it. Codex has no `@file` imports, so the policy is inlined with a sha256 marker. `zcrew status` flags a stale copy, and `zcrew enable` refreshes it.
- **Codex MCP settings.** `tool_timeout_sec = 180`: the default of 60 s equals `agent-wait`'s maximum block. `default_tools_approval_mode = "approve"`, so the per-plan user approval remains the only human gate.
- **Concurrency.** Workers started by two commanders in the same folder are serialized by the bridge's cross-process exclusive leases. The policy tells commanders to report `resource.waiting` and to use a separate worktree when that happens.
- **Context economy.** Two levers that zcrew controls:
  1. The policy waits until the run is terminal (`afterRevision: 999999999`, `timeoutMs: 60000`), rather than waking on every streaming event.
  2. The shim compacts run snapshots.
  
  Broader context filtering was researched but not adopted. `mksglu/context-mode` is under the Elastic License 2.0, its global hooks intercept the commander's Bash/Read, and it has no independent benchmark. arXiv 2609.22114 finds tool-schema filtering to be the only reliably positive lever.

### 27.2 Validation

| Item | Evidence | Status |
|---|---|---|
| Codex prefers `AGENTS.override.md` | Live `codex exec` answered `ROLE=COMMANDER` when both files were present | ✅ |
| Codex → zcode_executor | Live `agent-config` call returned `ZCODE_AVAILABLE=true` (also under a read-only sandbox) | ✅ |
| Codex follows the policy and delegates | Live run: read `CLAUDE.md`/`AGENTS.md`/config, then called `agent-start` itself | ✅ |
| Codex review → same-thread correction → accept | Run 1 hit the ChatGPT usage limit after 18 `agent-wait` calls, before review. Run 2 (v0.5.0 policy, compact output) went through the full loop: delegate, then own `git diff` + tests, then `REVIEW RESULT: FAIL, correction round 1`, then a correction on the same `threadId` (`sess_2a455bd0…`), then re-review and accept. That took 14/14 tests, **3 `agent-wait` calls** (was 18) and 38.5K Codex tokens in total | ✅ |
| Terminal-state waiting | One `agent-wait` covered a 70-revision run (42 s) | ✅ |
| Compact output | Replay of a real Claude Code session: `agent-wait` −70.4% (−77% non-terminal), 0/37 required-field or result losses | ✅ |
| Stale-policy refresh | Live: after the policy edit, `enable --commander codex` refreshed the sandbox copy | ✅ |
| Real-project use (Claude commander) | MSOM performance work: the commander measured, planned, got approval, then delegated to ZCode, as the owner confirmed | ✅ |

Measured fixed context of a Claude Code session in MSOM: 58.4K tokens. It breaks down as about 39K Claude Code core, 8.5K skill listings, 3.9K MCP server instructions, and 3.7K project files. Only the skill and MCP parts are reducible, and those are user-environment choices outside zcrew.

### 27.3 v0.6.0 addendum: measuring and reducing commander context

- The policy's *Context hygiene* rules target the biggest measured consumers. In the MSOM session, shell output was 45% of tool output and whole-file reads were 20–37%.
- `zcrew context` turns the manual analysis into a read-only command. Run against the MSOM session, it reported a peak of 420,915 tokens and showed `agent-wait` at 31% (72 calls). That session had started before `zcrew update`, so it still used the old waiting rule, which demonstrates the effect of economical waiting and compact output. The command's numbers were independently recomputed and matched exactly.
- Not adopted, per the owner's decision: third-party context filtering (context-mode) and trimming the user environment's skills/MCP (T4/T5).


---

## 28. v0.7.0: live view of the crew (2026-09-27)

The owner could not see what a worker was doing while it ran (§26.4: the ZCode app's own live view is impossible, and its history needs a restart). v0.7.0 adds `zcrew watch`, `zcrew dashboard`, `zcrew runs` and `zcrew show`. The owner chose a terminal view first and a browser view second, at "level 2" detail: files, commands and outcomes, but no reasoning stream.

### 28.1 Decision: read ZCode's database instead of teeing bridge events

The plan was to make `bridge_compat.py` write events to a log. A spike changed the plan:

- The bridge's tool events carry only the tool name.
- ZCode's CLI database (`~/.zcode/cli/db/db.sqlite`, WAL) already stores every part: tool inputs (file paths, commands), outputs, step context tokens and turn usage. The session id equals the bridge `threadId`.
- A live run showed a running Bash part as `running` and then `completed` about 8 s later.

So the view reads that database read-only (`mode=ro`) and never touches the bridge or the worker. It also shows runs started from the ZCode app and runs in worktrees, which the old history view missed. The trade-off is that the schema is internal to ZCode. Every call checks the tables and columns it needs and raises a clear "schema changed" error instead of crashing.

### 28.2 Performance and the layout self-check

The database was 2.1 GB, with 404k parts (1.2 GB of JSON) across about 1,900 sessions. The first reader parsed every blob with `json_extract` and needed about 6 s for all sessions.

- **Byte-prefix classification.** v0.7.0 classifies parts by byte prefix of ZCode's compact Go JSON, where `"type"` is the first key and a tool's `state.status` is the first `"status":"`. This was verified against every real row with 0 mismatches.
- **Batching.** Aggregates are computed in one grouped scan per chunk of sessions.
- **Results.** All sessions take 1.5 s, 200 sessions take 0.2 s, and a watch poll takes about 20 ms. Results are identical to the per-session reader on all 1,899 real sessions.
- **Self-check.** Each call samples the 200 most recently updated parts and compares the prefix tests (using the same SQL snippets) with `json_extract`. On any mismatch that call uses the exact per-session path, and the CLI prints a one-time note.
- **Known limit.** The self-check protects newly written rows. Old rows that a sample never reaches are trusted on the strength of the one-off full verification.

### 28.3 Validation

| Check | Evidence | Result |
|---|---|---|
| Built by ZCode workers | T2.1, T2.2, T2.2b and T3.1 were delegated to GLM-5.3, then went through independent verifier review and same-thread corrections: T2.1 took 2 rounds and 4 bugs, T3.1 took 1 UI round | ✅ |
| Live visibility | Workers were watched live with the new reader during their own runs (`zcrew show` on the running T2.2 session) | ✅ |
| Dashboard security | Independent review: loopback bind; Host-header matrix (rebinding variants → 403); CSP/nosniff/no-referrer; no CORS; XSS payloads in titles and commands are inert (`textContent` only); cross-project ids → 404; a heavy session does not block other requests | ✅ |
| Dashboard UI | Checked in a real browser at desktop (dark) and 375 px (light) widths: no horizontal scroll, and running sessions show elapsed time | ✅ |
| Tests | 363 unit tests, all against synthetic databases (the real DB is only read in smoke checks) | ✅ |
