# zcrew mod for Claude Code

Watches the ZCode worker runs your commander starts, so Claude does not have to.

- **Hands-free waiting.** While a run works, `agent-wait` is answered at once with a note telling Claude to end its turn. The mod checks each run every 15 s through the same MCP connection (`agent-observe`, no model call). When runs end it runs `zcrew gate <run> --json`, then wakes Claude with one `[zcrew]` message carrying the results. You can talk to Claude while workers run.
- **Always in view, no commands.** A band above the prompt shows what matters (working / to review / failed / near the time box), a status-line entry keeps the count, and in the fullscreen layout a sidebar pane opens by itself with one card per run. Outside fullscreen, press `[details]` on the band or type `/zcrew`.
- **Notifications** when runs end and when everything is done.
- **Parallel nudges.** With a `.zcrew/plan.json`, when Claude ends a turn while plan tasks are ready and workers are free, one `[zcrew]` prompt names the tasks to start; the band shows `plan 3/7 · ready 2`.
- **Forgotten background jobs.** A run whose turn ended but whose background jobs (a dev server, a watcher) keep it open is flagged, and after 90 s Claude is told which jobs to stop.

## Install

```
/plugin install zcrew --marketplace b9b4ymiN/zcrew
```

Answer `y` to add the marketplace, then pick a scope. Options (`/config`): `language` (`th` / `en`), `handsFree` (on by default), `python` (default `python`), `zcrewScript` (default: `ZCREW_HOME` or `~/.zcrew`, then `src/scripts/zcrew.py`).

Needs zcrew 0.9.1 or newer (`zcrew gate`) and the `zcode_executor` MCP server that `zcrew` installs. Logic stays in the `zcrew` CLI; the mod only calls it and draws, so turning the mod off returns to the policy's own waiting.

## Develop

```
claude plugin validate mod/zcrew
claude plugin test mod/zcrew
claude --plugin-dir mod/zcrew
```

No build step and no npm dependencies: Claude Code loads the TypeScript directly.
