# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Primary: the developer running zcrew (a senior engineer acting as "client" to a Claude Code / Codex commander). While the commander delegates tasks to ZCode workers, they keep the dashboard open on a second screen to check status, read what a worker is doing, and see how much ZCode quota is left.
- Secondary (occasional): someone watching over their shoulder or a screen recording. Not a design driver; presentation-first design was tried (v0.8 seven-segment panel) and rejected on 2026-09-28 as "too presentational".

## Product Purpose

`zcrew dashboard` is a local, read-only terminal-style view of ZCode worker sessions for one project directory (or all with `--all`). Success means, at a glance: which sessions are running / failed / done, how much ZCode Coding Plan quota remains and when it resets; and on reading: a session's activity is easy to scan, with each activity's type (read, edit, run, web, agent, message) and errors clearly color-coded. Readability and status beat spectacle. Performance must not drop.

## Positioning

It is the only view of what zcrew's ZCode workers are doing, built from ZCode's own database, opened read-only, and served from 127.0.0.1. There is no telemetry and no write path; the only outbound call is the opt-in Z.ai quota lookup. It shows the worker side that the commander's own transcript cannot show.

## Operating Context

- Launched with `zcrew dashboard [DIR] [--all] [--port N] [--no-open]`. Default URL is http://127.0.0.1:8765/.
- Polls `/api/sessions` every 3 s and the selected session's `/activity` every 1.5 s, using an incremental cursor.
- Runs next to Claude Code / Codex and the ZCode Desktop app, usually during a plan → delegate → review → correct loop.
- Data: `SessionSummary` (status running/completed/failed/idle, title, directory, created/updated, duration, tool_calls, errors, last_context_tokens, parent_id) and `Activity` (time, kind: file_read/file_edit/file_write/command/search/tool/step/turn_end/message, status, summary, detail, tool, stable key).
- Worker titles are often long contract text ("TASK T1 — ... OBJECTIVE ..."), and they are frequently in Thai as well as English.

## Capabilities and Constraints

- Read-only by design, no actions on workers. Stdlib-only Python server, single self-contained HTML file, no dependencies, no CDN, no web fonts, strict same-origin CSP.
- Performance must not drop (hard constraint): unchanged polls cause zero DOM churn (keyed rendering, kept from v0.8); polling cost and API latency stay at the baseline in tests/bench_baseline.json.
- API: additive changes allowed for new data (quota, activity type). Existing endpoints stay backward compatible for `zcrew runs/show/watch`.
- Activity types (confirmed 2026-09-28): READ (Read/Grep/Glob), EDIT (Edit/Write), RUN (Bash/commands), WEB (WebFetch/WebSearch/browser & network MCP), AGENT (Agent/Todo/Task), MSG (model messages/steps); errors override with a red marker. Source: ZCode `tool_usage` (tool_name, read_only, side_effect_scope) and part tool names.
- Quota (confirmed hybrid, 2026-09-28): if env `ZCODE_BIGMODEL_USAGE_API_KEY` is set (same name ZCode itself reads; URL override `ZCODE_BIGMODEL_USAGE_QUOTA_URL`), the server calls `GET https://api.z.ai/api/monitor/usage/quota/limit` (raw key in `authorization`, no Bearer), cached ~60 s; key never reaches the browser or logs. Parse TOKENS_LIMIT/CREDIT_LIMIT unit=3 (5 h window) and unit=6 (weekly), TIME_LIMIT (monthly tools/MCP); `percentage` is USED share, remaining = 100 - percentage; `nextResetTime` epoch ms. Without the key or on API failure: local estimate from `model_usage` tokens in the last 5 h plus the last "Usage limit reached ... reset at" event from ZCode logs. The UI always labels the source (API / est.). This is the one outbound network call; it is opt-in by setting the key.

## Brand Commitments

- Name: `zcrew`, always lowercase. Tone of the existing CLI and docs: plain, factual, engineer-to-engineer.
- The user asked for a modern terminal that is easy to use and read, not a presentation piece, and not a generic AI-made dashboard. The v0.8 seven-segment instrument look is retired.
- Standing preference (2026-09-28): the category standard, executed at full craft. Quality bar = VS Code (dark), Vercel / Railway deploy logs, k9s / lazygit. Conventions embraced, no novelty skin.

## Evidence on Hand

- Real session data from ZCode's local db (e.g. THP-CV worker runs). There are no screenshots, testimonials or usage metrics, and none should be invented.
- Test suite: 373 unittest tests (tests/test_dashboard.py covers the server).

## Product Principles

1. Status first: running / failed / quota visible without scrolling or clicking.
2. Readable detail: a session's activity scans like a good terminal log; type and outcome are clear from color plus a text tag.
3. Never heavier: every feature earns its cost against the performance baseline.
4. Truthful: show exactly what the db or API says; label estimates as estimates.
5. Local by default: nothing leaves 127.0.0.1 unless the user sets the quota key.

## Accessibility & Inclusion

Status must never be conveyed by color alone. The dashboard must stay readable on a second monitor at normal viewing distance and respect `prefers-reduced-motion`. Thai and English text must render correctly in the same line.
