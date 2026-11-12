# Antigravity ACP worker (experimental)

This is an opt-in worker adapter. It does not replace ZCode, Codex, or `coder-mcp-bridge`.
The adapter drives Google's official `agy acp` over the open [Agent Client Protocol](https://agentclientprotocol.com/),
using the [Python ACP SDK](https://github.com/agentclientprotocol/python-sdk) as an **optional** dependency.

## Prerequisites (Windows / Python 3.10+)

1. Install official Antigravity CLI **1.5.4 or newer** and confirm `agy acp --help` works.
2. Run `agy` interactively once to sign in. A CLI installation alone does **not** mean the account can run an ACP turn.
3. Install the optional SDK into the same Python that runs zcrew: `python -m pip install agent-client-protocol`.
4. In your git repository, run `zcrew ag doctor` (no model call).
5. Check that your account's quota, organization data policy and local sandbox settings allow the intended task.

## Foreground pilot

Create a normal zcrew worker contract with `OBJECTIVE`, `SCOPE`, `DO NOT`,
`ACCEPTANCE CRITERIA`, and `VERIFY`. On a clean committed test repository:

```powershell
zcrew ag doctor
zcrew ag run .\worker-contract.md
zcrew ag status
zcrew ag gate AGY_RUN_ID
zcrew ag gate AGY_RUN_ID --accept "Diff reviewed and tests pass"
# If review fails, preserve the same ACP session:
zcrew ag fix AGY_RUN_ID .\correction.md
zcrew ag gate AGY_RUN_ID
```

Each `run` or `fix` is one foreground ACP turn. The initial `session/new` ID is
persisted as `threadId` in the existing gate record; `fix` uses `session/resume`.
Results and reduced event traces stay outside the project under
`~/.zcode-commander/ag/`; the normal `zcrew gate` checks scope, DO NOT and VERIFY.
`zcrew ag status` reads recorded runs and reports, not live agent activity.

## Permission and safety model

- zcrew launches `agy acp --sandbox --disable-telemetry`.
- The default ACP session mode is `auto` (available in Antigravity CLI 1.5.4 if offered by the account).
- If a tool still asks for permission, **the noninteractive ACP client rejects it**. The round is marked `blocked`, not successful.
- The adapter does not use `--dangerously-skip-permissions` and does not grant ACP client-side filesystem or terminal services.
- Use an isolated throwaway Git working directory for the pilot. The agent's own runtime tools still have the permissions that Google's CLI sandbox allows.
- Dirty files inside the contract's SCOPE block dispatch unless you explicitly supply `--allow-dirty`; the normal gate cannot attribute preexisting edits.
- An already accepted run cannot receive a correction, and no failed/blocked run may be accepted through `zcrew ag gate`.
- `auto` mode can still consume plan quota and may require account review. It is not a promise of zero-cost or no user interaction.

## Not yet included

- Background process/job queue, hands-free commander wakeups, dashboard support, or managed worktree cleanup.
- End-to-end authenticated testing on a Windows machine running Antigravity CLI.
- A generic provider scheduler; selecting Antigravity remains an explicit decision.

**Current validation:** Python unit tests with mocked ACP transport (`python -m unittest tests/test_ag.py`).
Before production adoption, test a real `agy acp` session on Windows: login, new session, tool call,
permission rejection, cancel/timeout, resume after subprocess exit, and independent gate.

References: https://antigravity.google/docs/cli/acp and https://antigravity.google/docs/ide/extensions/acp/.
