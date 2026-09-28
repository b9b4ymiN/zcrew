---
name: zcrew dashboard
description: A calm developer console for ZCode workers — status and quota at a glance, activity readable like a deploy log.
colors:
  ground: "#0f1115"
  panel: "#15181d"
  raised: "#1b1f26"
  selected: "#1f2530"
  line: "#262b33"
  text: "#d7dce3"
  muted: "#8b94a1"
  accent: "#4c9bf5"
  status-running: "#d29922"
  status-failed: "#f85149"
  status-done: "#3fb950"
  status-idle: "#6e7681"
  cat-read: "#58a6ff"
  cat-edit: "#f0883e"
  cat-run: "#3fb950"
  cat-web: "#bc8cff"
  cat-agent: "#39c5cf"
  cat-msg: "#8b949e"
typography:
  title:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 600
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "13px"
  data:
    fontFamily: "ui-monospace, Cascadia Mono, Consolas, monospace"
    fontSize: "13px"
    lineHeight: 1.45
  label:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "11px"
    fontWeight: 600
rounded:
  tag: "3px"
  control: "4px"
  panel: "6px"
components:
  category-tag:
    textColor: "{colors.cat-read}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    width: "48px"
  filter-chip:
    textColor: "{colors.cat-read}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "2px 8px"
  session-row:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    padding: "8px 12px 9px"
  session-row-selected:
    backgroundColor: "{colors.selected}"
    textColor: "{colors.accent}"
  status-bar:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    height: "24px"
---

# zcrew dashboard — design system

Source of truth: `scripts/dashboard.html` (single self-contained page, tokens on `:root`). Product context: `PRODUCT.md`.

## Overview

**North star: the deploy log.** The dashboard is the category standard for developer tools, done carefully: VS Code (dark) for the app shell and status bar, Vercel / Railway deploy logs for the activity stream, k9s / lazygit for keyboard-first navigation. It is a tool the user keeps open on a second monitor and glances at, not a presentation piece. Readability and status beat spectacle; the v0.8 seven-segment "instrument panel" was rejected for being too presentational and must not return.

Dark only. The use scene is a second screen next to an editor and terminals, which are dark.

## Colors

Restrained strategy: neutral layered greys, one blue accent for focus and selection, and two small semantic palettes that carry all meaning.

- **Neutrals:** `ground` (app background) < `panel` (sidebar, bars) < `raised` (hover) < `selected` (selected row). `line` is the only border color, always 1px.
- **Text:** `text` for content, `muted` for metadata (≥ 4.5:1 on `panel`).
- **Accent:** `accent` is for focus rings, the selected row's title, and pressed toggles. Never decorative.
- **Session status** (dot + uppercase word, never color alone): running `status-running` (amber, dot pulses), failed `status-failed`, done `status-done`, idle `status-idle`.
- **Activity categories** (the log's tag and the filter chips): READ `cat-read`, EDIT `cat-edit`, RUN `cat-run`, WEB `cat-web`, AGENT `cat-agent`, MSG `cat-msg`. A tag is a fixed 48px cell: the category color as text on the same color at 14% alpha. Errors override the category: tag reads `ERR` in `status-failed`, the summary turns red, the row gets a faint red tint.
- Quota meter fill: green above 30% remaining, amber 10–30%, red below 10%.

## Typography

Two families: the platform UI stack for chrome and labels, the mono stack for everything that is data (ids, times, paths, commands, log lines, meta lines). Exactly three sizes in use: 11px (labels, tags, meta, status bar), 13px (body, log), 16px (session title in the pane header). Uppercase is reserved for status words and category tags.

## Layout

App shell with no page scroll on desktop; each pane scrolls on its own.

- Title bar (~36px): wordmark, scope path, status counts, shortcuts button.
- Sidebar 320px: filter input, then the session list.
- Main pane: session header, sticky filter bar (solid `ground` background, 1px `line` below), then the log.
- Status bar 24px at the bottom, full width: connection state left, quota right.
- ≤ 899px: panes stack and the page scrolls. ≤ 760px: title bar and status bar wrap; the session list is capped at ~38vh. ≤ 430px: the log's time column is hidden. No horizontal scroll at 375px.

## Elevation & Depth

Flat. Depth comes from tonal layering (`ground` → `panel` → `raised` → `selected`) and 1px `line` separators. The one exception is the shortcuts popover, the only floating element: `box-shadow: 0 6px 24px rgba(0,0,0,.45)`. No other shadows, no glass.

## Shapes

Small radii only: 3px tags, 4px controls and chips, 6px panels and popover. Rows in lists are square-edged and separated by 1px lines, not cards.

## Components

- **Session row:** line 1 status dot + STATUS word + short id (mono, muted); line 2 title, two-line clamp, full text in the `title` attribute; line 3 mono meta (started · duration · tools · errors in red when > 0 · ctx). Hover uses `raised`; selected uses `selected` with an accent title and a 1px outline. `role=option` inside a `role=listbox`.
- **Log row:** time (muted) | category tag | summary | `-> detail` (muted, omitted when empty). Running rows show a small pulsing amber dot.
- **Filter chips:** one per category with its live count; toggles with `aria-pressed`. Plus "Errors only" and "Follow running". Filtering is CSS on the log container (data attributes), never by rebuilding rows.
- **Status bar quota:** per window `5h 68% left` + 60px meter + `resets 19:32`, then `week`, then `tools`; a `[API]` or `[est.]` tag always names the source. Estimate mode shows tokens and requests in the last 5 h, and `LIMITED until HH:mm` in red when a limit is active.
- **Shortcuts popover:** non-modal, opened with `?`, closed with Esc. Keys: `j/k` or arrows move, Enter/Space select, `/` filter sessions, `1–6` toggle categories, `e` errors only, `f` follow running.
- **Focus:** 2px `accent` outline on every interactive element (`:focus-visible`).

## Do's and Don'ts

- Do keep rendering keyed and diffed: an unchanged poll must cause zero DOM mutations in the session list and the log.
- Do insert all server text with `textContent`; keep everything inline (no external URLs, fonts or CDNs) under the strict CSP; keep the page ≤ 40 KB.
- Do pair every status color with a word, and every category color with its tag text.
- Do respect `prefers-reduced-motion`: the running pulse is the only animation and it stops there.
- Don't add new font sizes, gradients, glass, shadows on in-flow elements, card grids, emoji or unicode glyphs as icons, or accent bars thicker than 1px.
- Don't reintroduce large display numerals or a presentation mode.
