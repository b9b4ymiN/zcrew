// zcrew mod: watches ZCode worker runs so the model does not have to.
//
// - agent-start: remember the run (title from the contract's OBJECTIVE).
// - agent-wait: answered at once while the run works, with a note telling
//   Claude to end its turn (hands-free); a few quick answers per run at most.
// - every 15 s: agent-observe each live run through the same MCP connection,
//   no model call. When runs end: `zcrew gate <run> --json`, then one prompt
//   wakes Claude with the results, plus a native notification. A run silent
//   for 30 min is STALLED and wakes Claude once without the gate; while runs
//   are active one `zcrew capacity --json` per tick draws the machine-wide
//   cap (ช่าง 2/5) and toasts once when a 1302 backoff starts.
// - drawing: a band above the prompt and a status-line entry while there is
//   news; a sidebar pane opens by itself only in the fullscreen layout.
// - status line: Claude usage (context fill, rate-limit windows, each with a
//   mini meter) from session.measure, ahead of the run counts while there is news.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RunView } from '../types'
import {
  HANDS_FREE_LIMIT, SERVER, TERMINAL,
  bandRows, bandVisible, bar, boxFromConfig, capSegs, chromeText, detailOf, gateSummary, handsFreeNote, iconOf,
  isActive, isStalled, levelOf, liveJobs, minutes, notifyText, parseSnapshot, queueReason, sorted, stageColor, stageOf,
  statusText, titleOf, turnOver, usageText, wakeMessage, words,
} from './model'
import type { CapView, Lang, UsageView } from './model'

const PANE = 'zcrew'
const TICK_MS = 15_000
const WAKE_DEBOUNCE_MS = 20_000
const GATE_TIMEOUT_MS = 600_000
const CAP_TIMEOUT_MS = 10_000

const runs = atom({ plugin: 'zcrew', key: 'runs' } as const, {} as Record<string, RunView>)

type Options = { language?: string; handsFree?: boolean; showUsage?: boolean; python?: string; zcrewScript?: string }

// Module state: rebuilt on reload (the runs themselves live in $.state).
const ctx = {
  lang: 'th' as Lang,
  handsFree: true,
  showUsage: true,
  python: 'python',
  script: '',
  boxMs: boxFromConfig([]),
  isFullscreen: false,
  paneOpenedByUs: false,
  ticking: false,
  wakeTimer: undefined as { cancel: () => void } | undefined,
  quickWaits: new Map<string, number>(),
  gating: new Set<string>(),
  usage: undefined as UsageView | undefined,
  cap: undefined as CapView | undefined,
  capping: false,
  lastBackoff: undefined as boolean | undefined,
}

async function allRuns($: EngineInterface): Promise<RunView[]> {
  return Object.values(await read($, runs))
}

async function refreshChrome($: EngineInterface): Promise<void> {
  const list = await allRuns($)
  const now = await $.clock.now()
  $.ui.status(chromeText(ctx.showUsage ? usageText(ctx.usage, now, ctx.lang) : undefined, statusText(list, now, ctx.boxMs, ctx.cap, ctx.lang)))
}

async function patch($: EngineInterface, runId: string, change: Partial<RunView>): Promise<void> {
  await update($, runs, current => {
    const run = current[runId]
    return run ? { ...current, [runId]: { ...run, ...change } } : current
  })
}

async function homeDir($: EngineInterface): Promise<string | undefined> {
  return (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
}

async function zcrewArgv($: EngineInterface, args: string[]): Promise<string[]> {
  let script = ctx.script
  if (!script) {
    const home = (await $.env.get('ZCREW_HOME')) ?? `${(await homeDir($)) ?? '~'}/.zcrew`
    script = `${home}/src/scripts/zcrew.py`
  }
  return [ctx.python, script, ...args]
}

async function loadBox($: EngineInterface, cwd: string | undefined): Promise<void> {
  const home = await homeDir($)
  const texts: (string | undefined)[] = []
  for (const file of [cwd && `${cwd}/.claude/zcode-commander.json`, home && `${home}/.zcode-commander/config.json`]) {
    if (!file) continue
    try {
      texts.push(String(await $.fs.read(file)))
    } catch {
      // absent is fine
    }
  }
  ctx.boxMs = boxFromConfig(texts)
}

async function runGate($: EngineInterface, run: RunView): Promise<void> {
  if (ctx.gating.has(run.runId)) return
  ctx.gating.add(run.runId)
  try {
    const done = await $.process.run(await zcrewArgv($, ['gate', run.runId, '--json']), {
      cwd: run.cwd, timeoutMs: GATE_TIMEOUT_MS,
    })
    const gate = gateSummary(parseSnapshot(done.stdout))
    if (gate) await patch($, run.runId, { gate })
  } catch {
    // no gate (zcrew missing, no record, timed out): the wake message says so
  } finally {
    ctx.gating.delete(run.runId)
  }
}

async function wake($: EngineInterface): Promise<void> {
  ctx.wakeTimer = undefined
  const list = await allRuns($)
  const now = await $.clock.now()
  const ready = list.filter(r => turnOver(r, now) && !r.woke && !ctx.gating.has(r.runId))
  if (ready.length === 0) return
  for (const run of ready) await patch($, run.runId, { woke: true })
  if (ctx.handsFree) {
    try {
      await $.prompt.submit({ text: wakeMessage(ready, now) })
    } catch {
      // another plugin refused the prompt: the band and the notification still tell the person
    }
  }
  try {
    await $.ui.notify(notifyText(ready, ctx.lang), { title: 'zcrew' })
  } catch {
    // notifications turned off
  }
  if (!list.some(isActive)) $.ui.toast(`zcrew: ${words(ctx.lang).allDone} ${list.length} ${words(ctx.lang).tasks}`)
}

async function observe($: EngineInterface, runId: string): Promise<Record<string, unknown> | undefined> {
  try {
    const res = await $.mcp.call(SERVER, 'agent-observe', { runId, refresh: false, maxEvents: 0, resultChars: 0 })
    if (res.isError) return { status: 'failed' }
    const block = res.content.find(c => c.type === 'text') as { text?: string } | undefined
    return parseSnapshot(res.structuredContent) ?? parseSnapshot(block?.text)
  } catch {
    return undefined // bridge busy or restarting: try again next tick
  }
}

async function readVerdict($: EngineInterface, runId: string): Promise<string | undefined> {
  const home = await homeDir($)
  if (!home) return undefined
  try {
    const record = parseSnapshot(String(await $.fs.read(`${home}/.zcode-commander/gate/${runId}.json`)))
    const verdicts = Array.isArray(record?.verdicts) ? (record?.verdicts as Record<string, unknown>[]) : []
    const last = verdicts[verdicts.length - 1]
    return last?.verdict ? String(last.verdict) : undefined
  } catch {
    return undefined
  }
}

async function scheduleWake($: EngineInterface, run: RunView, gate = true): Promise<void> {
  if (gate) await runGate($, run)
  ctx.wakeTimer?.cancel()
  ctx.wakeTimer = $.clock.after(WAKE_DEBOUNCE_MS, () => void wake($))
}

// One `zcrew capacity --json` at a time: the machine-wide cap for the band,
// and one toast + notification when a 1302 backoff starts. Never throws.
async function readCap($: EngineInterface): Promise<void> {
  if (ctx.capping) return
  ctx.capping = true
  try {
    const res = await $.process.run(await zcrewArgv($, ['capacity', '--json']), { timeoutMs: CAP_TIMEOUT_MS })
    const data = parseSnapshot(res.stdout)
    const cap = parseSnapshot(data?.cap)
    const used = data?.slots_in_use
    if (typeof cap?.cap !== 'number' || typeof used !== 'number') {
      ctx.cap = undefined
      return
    }
    ctx.cap = { used, cap: cap.cap, configured: typeof cap.configured === 'number' ? cap.configured : cap.cap, backoff: cap.backoff === true }
    if (ctx.cap.backoff && ctx.lastBackoff !== true) {
      const text = ctx.lang === 'th'
        ? `zcrew: เจอ 1302 — ลดเพดานเหลือ ${ctx.cap.cap} ชั่วคราว 10 นาที`
        : `zcrew: 1302 — cap lowered to ${ctx.cap.cap} for 10 min`
      try { $.ui.toast(text) } catch { /* no toast surface */ }
      try { await $.ui.notify(text, { title: 'zcrew' }) } catch { /* notifications off */ }
    }
    ctx.lastBackoff = ctx.cap.backoff
  } catch {
    ctx.cap = undefined // zcrew missing, slow, or bad JSON: draw without the cap
  } finally {
    ctx.capping = false
  }
}

async function tick($: EngineInterface): Promise<void> {
  if (ctx.ticking) return
  ctx.ticking = true
  try {
    const now = await $.clock.now()
    for (const run of await allRuns($)) {
      if (!isActive(run)) continue
      const snap = await observe($, run.runId)
      if (!snap) continue
      const status = String(snap.status ?? run.status)
      const change: Partial<RunView> = {
        status,
        tools: Array.isArray(snap.activeTools) ? snap.activeTools.map(String) : [],
        phase: snap.phase ? String(snap.phase) : undefined,
        queue: queueReason(snap),
      }
      const revision = typeof snap.revision === 'number' ? snap.revision : undefined
      if (revision !== undefined && revision !== run.lastRevision) {
        change.lastRevision = revision
        change.lastChangeAt = now
        // it moved again: no longer stalled, and its real end must wake Claude again
        if (run.stalled) Object.assign(change, { stalled: false, woke: false, endedAt: undefined })
      }
      if (status === 'running' && !run.runningSince) change.runningSince = now
      if (snap.threadId && !run.threadId) change.threadId = String(snap.threadId)
      if (TERMINAL.has(status)) change.endedAt = now
      if (status === 'background') {
        change.background = liveJobs(snap.backgroundTasks)
        if (!run.backgroundSince) change.backgroundSince = now
      }
      await patch($, run.runId, change)
      const next = { ...run, ...change }
      if (TERMINAL.has(status) || (turnOver(next, now) && !run.woke && !run.endedAt)) {
        if (!TERMINAL.has(status)) await patch($, run.runId, { endedAt: now })
        void scheduleWake($, { ...next, endedAt: next.endedAt ?? now })
        continue
      }
      if (!next.stalled && isStalled(next, now)) {
        await patch($, run.runId, { stalled: true, endedAt: now })
        void scheduleWake($, { ...next, stalled: true, endedAt: now }, false)
      }
    }
    for (const run of await allRuns($)) {
      if (isActive(run) || run.verdict) continue
      const verdict = await readVerdict($, run.runId) // recorded by `zcrew gate --accept/--reject`
      if (verdict) await patch($, run.runId, { verdict })
    }
    if ((await allRuns($)).some(isActive)) await readCap($)
    await refreshChrome($)
    const list = await allRuns($)
    if (ctx.paneOpenedByUs && list.length > 0 && !bandVisible(list, now)) {
      ctx.paneOpenedByUs = false
      await $.ui.close({ id: PANE })
    }
  } finally {
    ctx.ticking = false
  }
}

async function rememberStart($: EngineInterface, args: Record<string, unknown>, snap: Record<string, unknown>): Promise<string> {
  const runId = String(snap.runId)
  const now = await $.clock.now()
  const thread = args.threadId ? String(args.threadId) : undefined
  await update($, runs, current => {
    const earlier = thread ? Object.values(current).find(r => r.threadId === thread) : undefined
    const run: RunView = {
      runId,
      threadId: thread ?? (snap.threadId ? String(snap.threadId) : undefined),
      title: earlier?.title ?? titleOf(args.prompt),
      cwd: args.cwd ? String(args.cwd) : earlier?.cwd,
      status: String(snap.status ?? 'queued'),
      tools: [],
      startedAt: now,
      round: earlier ? earlier.round + 1 : 0,
    }
    const rest = { ...current }
    if (earlier) delete rest[earlier.runId] // a correction replaces its thread's card
    return { ...rest, [runId]: run }
  })
  if (ctx.isFullscreen && !ctx.paneOpenedByUs) {
    const opened = await $.ui.open({ id: PANE, title: 'zcrew' })
    ctx.paneOpenedByUs = opened.isPlaced
  }
  await refreshChrome($)
  return runId
}

async function markClosed($: EngineInterface, runId: string): Promise<void> {
  await update($, runs, current => {
    const run = current[runId]
    return run && !run.verdict ? { ...current, [runId]: { ...run, verdict: 'closed' } } : current
  })
  await refreshChrome($)
}

export const register: Register = (on, options) => {
  const opts = (options ?? {}) as Options
  ctx.lang = opts.language === 'en' ? 'en' : 'th'
  ctx.handsFree = opts.handsFree !== false
  ctx.showUsage = opts.showUsage !== false
  ctx.python = opts.python?.trim() || 'python'
  ctx.script = opts.zcrewScript?.trim() || ''

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await loadBox($, e.cwd)
    await $.command.register({ name: 'zcrew', description: 'Show the ZCode worker runs zcrew is watching' })
    $.clock.every(TICK_MS, () => void tick($))
    try {
      const usage = await $.session.usage()
      ctx.usage = { context: usage.context, rateLimits: usage.rateLimits }
    } catch {
      // nothing measured yet: session.measure will bring it
    }
    await refreshChrome($)
    return started
  })

  on('session.measure', async ($, e, next) => {
    ctx.usage = { context: e.context, rateLimits: e.rateLimits }
    try {
      await refreshChrome($)
    } finally {
      return next(e) // the measurement passes through even if painting fails
    }
  })

  on('command.run', { command: 'zcrew' }, async $ => {
    await $.ui.open({ id: PANE, title: 'zcrew' })
    const list = await allRuns($)
    return { text: list.length ? `zcrew: ${list.length} run(s)` : words(ctx.lang).none }
  })

  on('tool.call', { tool: 'mcp__zcode_executor__agent-start' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError) return ran
    const snap = parseSnapshot(ran.text)
    if (!snap?.runId) return ran
    const runId = await rememberStart($, e as unknown as Record<string, unknown>, snap)
    return ctx.handsFree ? { ...ran, context: [...(ran.context ?? []), handsFreeNote(runId)] } : ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__zcode_executor__agent-wait' }, async ($, e, next) => {
    const runId = String((e as unknown as Record<string, unknown>).runId ?? '')
    const known = (await read($, runs))[runId]
    const used = ctx.quickWaits.get(runId) ?? 0
    if (!ctx.handsFree || !known || !isActive(known) || used >= HANDS_FREE_LIMIT) return next(e)
    const ran = await next({ ...e, timeoutMs: 0 })
    if (ran.deny !== undefined || ran.isError) return ran
    if (TERMINAL.has(String(parseSnapshot(ran.text)?.status ?? ''))) return ran
    ctx.quickWaits.set(runId, used + 1)
    return { ...ran, context: [...(ran.context ?? []), handsFreeNote(runId)] }
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__zcode_executor__agent-close' }, async ($, e, next) => {
    const ran = await next(e)
    const runId = String((e as unknown as Record<string, unknown>).runId ?? '')
    if (runId) await markClosed($, runId)
    return ran
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.viewport?.isFullscreen !== undefined) ctx.isFullscreen = e.viewport.isFullscreen
    const list = await allRuns($)
    const now = await $.clock.now()
    if (e.props.hasSurvey || !bandVisible(list, now)) return next(e)
    const rows = bandRows(list, now, ctx.boxMs, ctx.lang, e.props.maxRows, ctx.cap)
    if (rows.length === 0) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        {rows.map((segs, i) => (
          <Box key={`l${i}`} flexDirection="row">
            {segs.filter(seg => seg.text.length > 0).map((seg, j) => (
              <Text key={`s${j}`} color={seg.color} bold={i === 0 || seg.bold} dimColor={seg.dim} wrap="truncate-end">{seg.text}</Text>
            ))}
            {i === 0 && !ctx.isFullscreen
              ? <Button key="open" plain label={` [${words(ctx.lang).details}]`} onPress={() => { void $.ui.open({ id: PANE, title: 'zcrew' }) }} />
              : null}
          </Box>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const list = await allRuns($)
    const now = await $.clock.now()
    const w = words(ctx.lang)
    if (list.length === 0) return <Text dimColor>{w.none}</Text>
    const width = Math.max(10, Math.min(10, e.props.bodyColumns - 14))
    const workMs = list.reduce((sum, r) => sum + ((r.endedAt ?? now) - (r.runningSince ?? r.startedAt)), 0)
    return (
      <Box flexDirection="column">
        {ctx.cap ? (
          <Box flexDirection="row" marginBottom={1}>
            {capSegs(ctx.cap, ctx.lang).filter(seg => seg.text.length > 0).map((seg, i) => (
              <Text key={`c${i}`} color={seg.color} bold>{seg.text}</Text>
            ))}
          </Box>
        ) : null}
        {sorted(list, now, ctx.boxMs).map(run => {
          const since = run.runningSince ?? run.startedAt
          const fill = Math.min(1, (now - since) / ctx.boxMs)
          return (
            <Box key={run.runId} flexDirection="column" marginBottom={1}>
              <Text bold wrap="truncate-end" color={stageColor(stageOf(run, now, ctx.boxMs))}>
                {iconOf(run, now, ctx.boxMs)} {run.title}{run.round ? ` · ${w.round} ${run.round}` : ''}
              </Text>
              {isActive(run)
                ? <Text color={levelOf(fill * 100)}>{'   '}{bar(fill, width)} {minutes(now - since)}/{minutes(ctx.boxMs)}</Text>
                : null}
              <Text dimColor wrap="wrap">{'   '}{detailOf(run, now, ctx.boxMs, ctx.lang)}</Text>
            </Box>
          )
        })}
        <Text dimColor>{w.today} {list.length} {w.tasks} · {w.workers} {minutes(workMs)} {ctx.lang === 'th' ? w.min : 'min'}</Text>
      </Box>
    )
  })
}
