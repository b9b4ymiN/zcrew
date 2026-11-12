# Antigravity ACP worker (experimental)

This is an opt-in worker adapter. It does not replace ZCode, Codex, or `coder-mcp-bridge`.
The adapter drives Google's official `agy acp` over the open [Agent Client Protocol](https://agentclientprotocol.com/),
using the [Python ACP SDK](https://github.com/agentclientprotocol/python-sdk) as an **optional** dependency.

## Prerequisites (Windows / Python 3.10+)

1. Install official Antigravity CLI **1.5.5 or newer** and confirm `agy acp --help` works.
2. Run `agy` interactively once to sign in. A CLI installation alone does **not** mean the account can run an ACP turn.
3. Install the optional SDK into the same Python that runs zcrew:
   `python -m pip install "agent-client-protocol>=1.0,<2"`.
   A missing SDK and an installed-but-incompatible one (wrong version or missing names) are reported separately.
4. In your git repository, run `zcrew ag doctor`. Besides the CLI/SDK checks it now performs a **live handshake
   with no model call**: it spawns `agy acp --sandbox --disable-telemetry`, runs `initialize` + `new_session(cwd=repo)`,
   and prints the agy agent version, whether session `resume` is supported, the offered session-mode ids, and the
   account tier when present. It exits 1 if resume is unsupported, the default mode `auto` is not offered, or the
   handshake errors or times out (60 s); auth-looking failures suggest signing in with `agy` interactively.
   Use `zcrew ag doctor --offline` to run only the CLI and SDK checks.
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
- The default ACP session mode is `auto`: the agent itself approves edits and "safe" commands. zcrew's noninteractive
  client still rejects any tool call that does reach it as an interactive permission request, and the round is
  marked `blocked` (observed live: a denied shell command).
- In `default` mode the agent does **not** stop for file edits — edits follow your own interactive `agy` permission
  settings, not zcrew's. Only tool calls routed to the ACP permission request (observed live: shell commands) hit
  zcrew's client, which denies them.
- `zcrew ag gate` only sees changes inside the repository. Writes outside the repo are invisible to it — run the
  pilot on a throwaway or fully committed working tree.
- The adapter does not use `--dangerously-skip-permissions` and does not grant ACP client-side filesystem or terminal services.
- Dirty files inside the contract's SCOPE block are **blocked by default**; `--allow-dirty` opts out, but the gate cannot attribute preexisting edits.
- An already accepted run cannot receive a correction, and no failed/blocked run may be accepted through `zcrew ag gate` (both `--accept TEXT` and `--accept=TEXT` forms are guarded).
- `auto` mode can still consume plan quota and may require account review. It is not a promise of zero-cost or no user interaction.

## Not yet included

- Background process/job queue, hands-free commander wakeups, dashboard support, or managed worktree cleanup.
- A generic provider scheduler; selecting Antigravity remains an explicit decision.

**Current validation:** live-tested on Windows 11 with Antigravity CLI 1.5.5 and `agent-client-protocol` 1.0.0:
run, fix via `session/resume` across separate processes, gate, status, and permission denial → `blocked`.
The offline unit tests (`python -m unittest discover -s tests -p "test_ag.py"`) also run a real-SDK contract
check whenever the SDK is installed, which is what catches SDK API drift such as removed names. Timeout handling
and Ctrl+C interruption remain untested live.

References: https://antigravity.google/docs/cli/acp and https://antigravity.google/docs/ide/extensions/acp/.
