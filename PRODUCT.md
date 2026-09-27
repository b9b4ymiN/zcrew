# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Primary: the developer running zcrew (a senior engineer acting as "client" to a Claude Code / Codex commander). While the commander delegates tasks to ZCode workers, they keep the dashboard open on a second screen and glance at it to know who is running, who failed, and whether something needs them.
- Secondary: an audience watching a demo or a screen recording of zcrew (a team, a video viewer). They have never seen the tool before and must understand what the crew is doing from the screen alone.

## Product Purpose

`zcrew dashboard` is a local, read-only live view of ZCode worker sessions for one project directory (or all directories with `--all`). It replaces tailing `zcrew watch` in a terminal with a browser view: session list with status, and a live activity timeline for the selected session. Success means a glance answers "is anything running, stuck, or failing right now?", and in a demo the crew's work reads clearly on a large screen, without making the dashboard slower or heavier than it is today.

## Positioning

It is the only view of what zcrew's ZCode workers are doing, built from ZCode's own database, opened read-only, and served offline from 127.0.0.1. There is no cloud, no telemetry and no write path. It shows the worker side that the commander's own transcript cannot show.

## Operating Context

- Launched with `zcrew dashboard [DIR] [--all] [--port N] [--no-open]`. Default URL is http://127.0.0.1:8765/.
- Polls `/api/sessions` every 3 s and the selected session's `/activity` every 1.5 s, using an incremental cursor.
- Runs next to Claude Code / Codex and the ZCode Desktop app, usually during a plan → delegate → review → correct loop.
- Data: `SessionSummary` (status running/completed/failed/idle, title, directory, created/updated, duration, tool_calls, errors, last_context_tokens, parent_id) and `Activity` (time, kind: file_read/file_edit/file_write/command/search/tool/step/turn_end/message, status, summary, detail, tool, stable key).
- Worker titles are often long contract text ("TASK T1 — ... OBJECTIVE ..."), and they are frequently in Thai as well as English.

## Capabilities and Constraints

- Read-only by design, with no actions on workers. Stdlib-only Python server, a single self-contained HTML file, no dependencies, no CDN, no web fonts, and a strict same-origin CSP.
- Performance must not drop: polling cost, server CPU and page responsiveness must stay at or better than the current baseline. This is a hard constraint, confirmed by the user.
- API change policy: **undecided**. The user is unsure. Any change must be justified by monitoring value and must not add measurable cost. The existing endpoints are shared with `zcrew runs/show/watch`.

## Brand Commitments

- Name: `zcrew`, always lowercase. Tone of the existing CLI and docs: plain, factual, engineer-to-engineer.
- The user asked for a modern terminal look designed for presenting data, one that must not look like a generic AI-made dashboard.

## Evidence on Hand

- Real session data from ZCode's local db (e.g. THP-CV worker runs). There are no screenshots, testimonials or usage metrics, and none should be invented.
- Test suite: 363 unittest tests (tests/test_dashboard.py covers the server).

## Product Principles

1. A glance is enough: status and trouble surface before detail.
2. Legible to a stranger: a demo viewer can follow what a worker is doing without narration.
3. Never heavier: every feature earns its cost against the performance baseline.
4. Truthful and read-only: show exactly what the db says, never infer or act.
5. Local and private: nothing leaves 127.0.0.1.

## Accessibility & Inclusion

Status must never be conveyed by color alone. The dashboard must stay readable at projector/recording scale and respect `prefers-reduced-motion`. Thai and English text must render correctly in the same line.
