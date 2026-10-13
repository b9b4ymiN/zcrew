# zcrew mod for Claude Code

Watches the ZCode worker runs your commander starts, so Claude does not have to.

- **Hands-free waiting.** While a run works, `agent-wait` is answered at once with a note telling Claude to end its turn. The mod checks each run every 15 s through the same MCP connection (`agent-observe`, no model call). When runs end it runs `zcrew gate <run> --json`, then wakes Claude with one `[zcrew]` message carrying the results. You can talk to Claude while workers run.
- **Always in view, no commands.** A band above the prompt shows what matters (working / to review / failed / near the time box), each count and row in its own color (running in the `claude` color, attention red, near-box amber, details dimmed). A status-line entry keeps the count, and in the fullscreen layout a sidebar pane opens by itself with one card per run, its time bar going green → amber → red as the box fills. Outside fullscreen, press `[details]` on the band or type `/zcrew`.
- **Usage in the status line.** Context fill and the 5-hour / weekly rate-limit windows (`ctx ▰▰▱▱▱ 42% │ 5h ▰▱▱▱▱ 23%`, with the reset time once a window is past 80%) sit in the status line even with no runs active; `showUsage` turns the segment off.
- **Notifications** when runs end and when everything is done.
- **Forgotten background jobs.** A run whose turn ended but whose background jobs (a dev server, a watcher) keep it open is flagged, and after 90 s Claude is told which jobs to stop.
- **Queues, cap and stalls.** A run waiting because the machine's worker cap is full shows as `⏳ รอคิว (เต็มเพดาน)` (`waiting for the folder` when another commander holds the folder). While runs are active the band and status line carry the machine-wide cap as one dot per worker slot (`ช่าง ●●○○○ 2/5`; the dots turn amber while a 1302 backoff lowers the cap — one toast when the backoff starts). A run silent for 30 min is flagged STALLED and wakes Claude once to check it; if it moves again the flag lifts and its real end still wakes Claude.

## Install

```
/plugin install zcrew --marketplace b9b4ymiN/zcrew
```

Answer `y` to add the marketplace, then pick a scope. Options (`/config`): `language` (`th` / `en`), `handsFree` (on by default), `showUsage` (on by default), `python` (default `python`), `zcrewScript` (default: `ZCREW_HOME` or `~/.zcrew`, then `src/scripts/zcrew.py`).

Needs zcrew 0.9.1 or newer (`zcrew gate`) and the `zcode_executor` MCP server that `zcrew` installs. Logic stays in the `zcrew` CLI; the mod only calls it and draws, so turning the mod off returns to the policy's own waiting.

## Develop

```
claude plugin validate mod/zcrew
claude plugin test mod/zcrew
claude --plugin-dir mod/zcrew
```

No build step and no npm dependencies: Claude Code loads the TypeScript directly.
