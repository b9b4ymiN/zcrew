// Pure logic of the zcrew mod: no `$`, so every rule here is testable on its own.
import type { RunView } from '../types'

export type Lang = 'th' | 'en'

export const SERVER = 'zcode_executor'
export const TOOL_START = `mcp__${SERVER}__agent-start`
export const TOOL_WAIT = `mcp__${SERVER}__agent-wait`
export const TOOL_CLOSE = `mcp__${SERVER}__agent-close`
export const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'timed_out', 'closed'])
export const DEFAULT_BOX_MS = 1200 * 1000
export const NEAR_BOX = 0.85 // share of the time box after which a run is flagged
export const LINGER_MS = 2 * 60 * 1000 // how long finished news stays on the band
export const HANDS_FREE_LIMIT = 2 // quick wait answers per run before letting waits block again
// A run whose turn ended but whose background jobs live on stays `background`
// (not terminal) until they stop or the run times out; after this long Claude
// is told, so a forgotten dev server does not hold the task open.
export const BACKGROUND_GRACE_MS = 90 * 1000
// A running run whose engine revision has not moved for this long is stalled.
export const STALL_MS = 30 * 60 * 1000
const LIVE_JOB = new Set(['pending', 'queued', 'starting', 'running', 'waiting', 'blocked'])

const WORDS = {
  th: {
    running: 'กำลังทำ', review: 'รอตรวจ', accepted: 'ผ่าน', rejected: 'ส่งแก้', failed: 'ล้ม',
    left: 'เหลือ ~', min: 'นาที', allDone: 'เสร็จหมด', tasks: 'งาน', nearBox: 'ใกล้หมดเวลา',
    gateFail: 'ตรวจอัตโนมัติไม่ผ่าน', gatePass: 'ตรวจอัตโนมัติผ่าน', waitClaude: 'รอ Claude ตัดสิน',
    round: 'แก้รอบ', first: 'รอบแรก', details: 'รายละเอียด', none: 'ยังไม่มีงานของช่าง',
    read: 'กำลังอ่านไฟล์', edit: 'กำลังแก้ไฟล์', run: 'กำลังรันคำสั่ง', search: 'กำลังค้นหา',
    think: 'กำลังคิด', queued: 'รอคิว', stopping: 'กำลังหยุดและเขียนรายงาน', web: 'กำลังค้นเว็บ',
    finished: 'เสร็จ', today: 'รวม', workers: 'ช่าง',
    queuedFull: 'รอคิว (เต็มเพดาน)', waitFolder: 'รอโฟลเดอร์ว่าง', silent: 'เงียบ',
    lingering: 'งานเสร็จแต่มีโปรเซสค้าง',
  },
  en: {
    running: 'working', review: 'to review', accepted: 'accepted', rejected: 'sent back', failed: 'failed',
    left: '~', min: 'min left', allDone: 'all done', tasks: 'tasks', nearBox: 'near time box',
    gateFail: 'gate failed', gatePass: 'gate passed', waitClaude: 'waiting for Claude',
    round: 'fix round', first: 'first run', details: 'details', none: 'no worker runs yet',
    read: 'reading files', edit: 'editing files', run: 'running a command', search: 'searching',
    think: 'thinking', queued: 'queued', stopping: 'stopping to report', web: 'searching the web',
    finished: 'done', today: 'total', workers: 'workers',
    queuedFull: 'queued (worker cap full)', waitFolder: 'waiting for the folder', silent: 'silent',
    lingering: 'done, but background jobs still run',
  },
} as const

export const words = (lang: Lang) => WORDS[lang] ?? WORDS.th

/** JSON object out of an MCP text result, or undefined. */
export function parseSnapshot(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== 'string') return undefined
  try {
    const data = JSON.parse(value)
    return data && typeof data === 'object' && !Array.isArray(data) ? data : undefined
  } catch {
    return undefined
  }
}

/** The task's name: the first line under OBJECTIVE, else the first line of the prompt. */
export function titleOf(prompt: unknown, limit = 48): string {
  const lines = String(prompt ?? '').split(/\r?\n/).map(l => l.trim())
  const at = lines.findIndex(l => /^(#+\s*)?\**OBJECTIVE\**:?\**$/.test(l))
  const pick = (at >= 0 ? lines.slice(at + 1) : lines).find(l => l.length > 0) ?? 'task'
  const text = pick.replace(/^[-*]\s+/, '')
  return text.length <= limit ? text : text.slice(0, limit - 1) + '…'
}

/** Plain-language activity from the run's active tools and phase. */
export function activityOf(run: Pick<RunView, 'tools' | 'status' | 'phase'>, lang: Lang): string {
  const w = words(lang)
  if (run.status === 'queued') return w.queued
  if (run.status === 'stopping' || run.phase === 'interrupting-for-guidance') return w.stopping
  const tools = run.tools.join(' ')
  if (/Edit|Write|MultiEdit|NotebookEdit/.test(tools)) return w.edit
  if (/Bash|PowerShell|Shell/.test(tools)) return w.run
  if (/Grep|Glob/.test(tools)) return w.search
  if (/Read/.test(tools)) return w.read
  if (/Web/.test(tools)) return w.web
  return w.think
}

export function bar(fraction: number, width = 10): string {
  const filled = Math.max(0, Math.min(width, Math.round(fraction * width)))
  return '▓'.repeat(filled) + '░'.repeat(width - filled)
}

/** A mini meter for the status line: 42 at width 5 -> `▰▰▱▱▱`, clamped to the width. */
export function meter(percent: number, width = 5): string {
  const filled = Math.max(0, Math.min(width, Math.round(percent / 100 * width)))
  return '▰'.repeat(filled) + '▱'.repeat(width - filled)
}

/** How full a meter or a time box reads: fine under 60, warm under 85, hot from 85. */
export function levelOf(percent: number): 'success' | 'warning' | 'error' {
  if (percent < 60) return 'success'
  if (percent < 85) return 'warning'
  return 'error'
}

/** Worker-slot dots: filled up to the cap, never past it. */
export function slots(used: number, cap: number): string {
  const filled = Math.max(0, Math.min(cap, used))
  return '●'.repeat(filled) + '○'.repeat(Math.max(0, cap - filled))
}

export const minutes = (ms: number) => Math.max(0, Math.round(ms / 60000))

export const isActive = (run: RunView) => !TERMINAL.has(run.status)

/** Live background jobs from a snapshot's `backgroundTasks`. */
export function liveJobs(value: unknown): { taskId: string; description: string }[] {
  if (!Array.isArray(value)) return []
  return value
    .filter(j => j && typeof j === 'object' && LIVE_JOB.has(String((j as Record<string, unknown>).status ?? '').toLowerCase()))
    .map(j => {
      const job = j as Record<string, unknown>
      return { taskId: String(job.taskId ?? ''), description: String(job.description ?? job.toolName ?? '').slice(0, 80) }
    })
    .filter(j => j.taskId)
}

/** Why a queued run waits: the machine's worker cap is full, or another commander holds the folder. */
export function queueReason(snap: Record<string, unknown> | undefined): 'slot' | 'folder' | undefined {
  const lease = snap?.resourceLease
  if (!lease || typeof lease !== 'object' || (lease as Record<string, unknown>).acquired === true) return undefined
  const blockers = (lease as Record<string, unknown>).blockers
  if (!Array.isArray(blockers) || blockers.length === 0) return undefined
  const held = blockers.some(b => String((b as Record<string, unknown>)?.key ?? '').startsWith('zcrew:worker-slot:'))
  return held ? 'slot' : 'folder'
}

/** A running run is stalled when its revision has not moved for STALL_MS (queued runs never are). */
export function isStalled(run: Pick<RunView, 'status' | 'lastChangeAt'>, now: number): boolean {
  return run.status === 'running' && run.lastChangeAt !== undefined && now - run.lastChangeAt >= STALL_MS
}

/** The machine's worker cap, as `zcrew capacity --json` reports it. */
export type CapView = { used: number; cap: number; configured: number; backoff: boolean }

export function capText(cap: CapView, lang: Lang): string {
  return `${words(lang).workers} ${cap.used}/${cap.cap}${cap.backoff ? ' ⚠1302' : ''}`
}

/** The worker's turn is over: terminal, stalled, or only background jobs keep the run open past the grace. */
export function turnOver(run: RunView, now: number): boolean {
  if (!isActive(run)) return true
  if (run.stalled) return true
  return run.status === 'background' && run.backgroundSince !== undefined && now - run.backgroundSince >= BACKGROUND_GRACE_MS
}

/** Where a run stands, for ordering and icons: attention first. */
export function stageOf(run: RunView, now: number, boxMs: number): 'attention' | 'near' | 'queued' | 'running' | 'review' | 'accepted' | 'rejected' {
  if (run.verdict === 'accept' || run.verdict === 'closed') return 'accepted'
  if (run.verdict === 'reject') return 'rejected'
  if (run.stalled) return 'attention'
  if (run.status === 'background') return 'attention'
  if (run.status === 'queued') return 'queued'
  if (isActive(run)) {
    const since = run.runningSince ?? run.startedAt
    return now - since >= boxMs * NEAR_BOX ? 'near' : 'running'
  }
  if (run.status !== 'completed' || run.gate?.mechanical === 'FAIL') return 'attention'
  return 'review'
}

const ICON = { attention: '❌', near: '⏰', queued: '⏳', running: '🔧', review: '👀', accepted: '✅', rejected: '↩' } as const
const ORDER = { attention: 0, near: 1, queued: 2, running: 3, review: 4, rejected: 5, accepted: 6 } as const

export function sorted(runs: RunView[], now: number, boxMs: number): RunView[] {
  return [...runs].sort((a, b) =>
    ORDER[stageOf(a, now, boxMs)] - ORDER[stageOf(b, now, boxMs)] || b.startedAt - a.startedAt)
}

/** Detail for one run: what it is doing, or what it waits for. */
export function detailOf(run: RunView, now: number, boxMs: number, lang: Lang): string {
  const w = words(lang)
  const stage = stageOf(run, now, boxMs)
  if (stage === 'queued') {
    if (run.queue === 'slot') return w.queuedFull
    if (run.queue === 'folder') return w.waitFolder
    return w.queued
  }
  if (stage === 'running' || stage === 'near') {
    const since = run.runningSince ?? run.startedAt
    const left = `${minutes(now - since)}/${minutes(boxMs)} ${lang === 'th' ? w.min : 'min'}`
    return `${stage === 'near' ? w.nearBox + ' ' : ''}${left} · ${activityOf(run, lang)}`
  }
  if (stage === 'attention') {
    if (run.stalled) return `${w.silent} ${minutes(now - (run.lastChangeAt ?? run.startedAt))} ${lang === 'th' ? w.min : 'min'}`
    if (run.status === 'background') return `${w.lingering} ${run.background?.length ?? ''}`.trim()
    if (run.status !== 'completed') return `${w.failed} (${run.status})`
    return `${w.gateFail}: ${run.gate?.summary ?? ''}`.trim()
  }
  if (stage === 'review') return run.gate ? `${w.gatePass} · ${w.waitClaude}` : w.waitClaude
  return stage === 'accepted' ? w.accepted : w.rejected
}

export function iconOf(run: RunView, now: number, boxMs: number): string {
  return ICON[stageOf(run, now, boxMs)]
}

/** Whether the band has anything to say right now. */
export function bandVisible(runs: RunView[], now: number): boolean {
  return runs.some(r => isActive(r) || !r.verdict || now - (r.endedAt ?? 0) < LINGER_MS)
}

/** One theme-key color per stage, so the band and the pane draw a run the same way. */
export function stageColor(stage: string): string | undefined {
  const colors: Record<string, string> = {
    attention: 'error', near: 'warning', queued: 'subtle', running: 'claude',
    review: 'suggestion', accepted: 'success', rejected: 'suggestion',
  }
  return colors[stage]
}

/** One span of a band row: its text plus how to draw it. */
export type Seg = { text: string; color?: string; bold?: boolean; dim?: boolean }

/** The cap the band tail and the pane header share: `ช่าง ●●○○○ 2/5` (+ ` ⚠1302` on backoff). */
export function capSegs(cap: CapView, lang: Lang): Seg[] {
  const filled = Math.max(0, Math.min(cap.cap, cap.used))
  return [
    { text: `${words(lang).workers} `, color: 'subtle' },
    { text: '●'.repeat(filled), color: cap.backoff ? 'warning' : 'success' },
    { text: '○'.repeat(Math.max(0, cap.cap - filled)), color: 'subtle' },
    { text: ` ${cap.used}/${cap.cap}`, color: 'subtle' },
    ...(cap.backoff ? [{ text: ' ⚠1302', color: 'warning' }] : []),
  ]
}

/** The band as colored rows: one summary line, then the runs that need someone, most urgent first. */
export function bandRows(runs: RunView[], now: number, boxMs: number, lang: Lang, maxRows: number, cap?: CapView): Seg[][] {
  const w = words(lang)
  if (runs.length === 0) return []
  const stages = runs.map(r => stageOf(r, now, boxMs))
  const count = (...s: string[]) => stages.filter(x => s.includes(x)).length
  const working = count('running', 'near')
  const tail: Seg[] = cap ? [{ text: '   ', color: 'subtle' }, ...capSegs(cap, lang)] : []
  const parts: Seg[] = []
  if (working) parts.push({ text: `🔧 ${working} ${w.running}`, color: stageColor('running') })
  if (count('queued')) parts.push({ text: `⏳ ${count('queued')} ${w.queued}`, color: stageColor('queued') })
  if (count('review')) parts.push({ text: `👀 ${count('review')} ${w.review}`, color: stageColor('review') })
  if (count('accepted')) parts.push({ text: `✅ ${count('accepted')} ${w.accepted}`, color: stageColor('accepted') })
  if (count('attention')) parts.push({ text: `❌ ${count('attention')} ${w.failed}`, color: stageColor('attention') })
  if (working === 0 && count('queued') === 0 && count('review', 'attention') === 0) {
    return [[{ text: 'zcrew  ' }, { text: `🎉 ${w.allDone} ${runs.length} ${w.tasks}`, color: 'success' }, ...tail]]
  }
  const head: Seg[] = [{ text: 'zcrew  ' }]
  parts.forEach((part, i) => {
    if (i > 0) head.push({ text: ' · ', color: 'subtle' })
    head.push(part)
  })
  head.push(...tail)
  const rows: Seg[][] = [head]
  for (const run of sorted(runs, now, boxMs)) {
    if (rows.length >= Math.max(1, Math.min(3, maxRows))) break
    const stage = stageOf(run, now, boxMs)
    if (stage === 'attention' || stage === 'near' || stage === 'queued' || stage === 'review') {
      rows.push([
        { text: '  ' },
        { text: `${iconOf(run, now, boxMs)} `, color: stageColor(stage) },
        { text: run.title },
        { text: ' — ', color: 'subtle' },
        { text: detailOf(run, now, boxMs, lang), dim: true },
      ])
    }
  }
  return rows
}

/** The band as plain lines: what the segments join to, for any string-only surface. */
export function bandLines(runs: RunView[], now: number, boxMs: number, lang: Lang, maxRows: number, cap?: CapView): string[] {
  return bandRows(runs, now, boxMs, lang, maxRows, cap).map(row => row.map(seg => seg.text).join(''))
}

/** The pinned status-line text, or undefined to clear it. */
export function statusText(runs: RunView[], now: number, boxMs: number, cap?: CapView, lang: Lang = 'th'): string | undefined {
  if (!bandVisible(runs, now)) return undefined
  const stages = runs.map(r => stageOf(r, now, boxMs))
  const n = (s: string) => stages.filter(x => x === s).length
  const bits = [
    n('running') + n('near') ? `🔧${n('running') + n('near')}` : '',
    n('queued') ? `⏳${n('queued')}` : '',
    n('review') ? `👀${n('review')}` : '',
    n('attention') ? `❌${n('attention')}` : '',
    n('accepted') ? `✅${n('accepted')}` : '',
  ].filter(Boolean)
  if (!bits.length) return undefined
  return `zcrew ${bits.join(' ')}${cap ? ` · ${capText(cap, lang)}` : ''}`
}

/** What the status line needs of `$.session.usage()` / `session.measure`. */
export type UsageView = {
  context?: { percent?: number }
  rateLimits?: { kind: string; percentUsed: number; resetsAt?: string }[]
}

/** Rate-limit windows worth a place on the status line. */
const WINDOW_LABEL: Record<string, string> = { five_hour: '5h', seven_day: '7d' }

/** The status line's usage segment: context fill and rate-limit windows, each with a mini meter. */
export function usageText(usage: UsageView | undefined, now: number, lang: Lang): string | undefined {
  const parts: string[] = []
  const percent = usage?.context?.percent
  if (typeof percent === 'number') parts.push(`ctx ${meter(percent)} ${Math.round(percent)}%`)
  for (const limit of usage?.rateLimits ?? []) {
    const label = WINDOW_LABEL[limit.kind]
    if (!label) continue
    let text = `${label} ${meter(limit.percentUsed)} ${Math.round(limit.percentUsed)}%`
    if (limit.percentUsed >= 80 && limit.resetsAt) {
      const at = new Date(limit.resetsAt)
      if (!Number.isNaN(at.getTime())) {
        const clock = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
        text += lang === 'th' ? ` (รีเซ็ต ${clock})` : ` (resets ${clock})`
      }
    }
    parts.push(text)
  }
  return parts.length ? parts.join(' │ ') : undefined
}

/** The whole status line: the usage segment, then the zcrew one. */
export function chromeText(usagePart: string | undefined, zcrewPart: string | undefined): string | undefined {
  const parts = [usagePart, zcrewPart].filter(p => p !== undefined)
  return parts.length ? parts.join(' │ ') : undefined
}

/** One line of the gate's JSON report for Claude. */
export function gateSummary(report: Record<string, unknown> | undefined): { mechanical: string; summary: string } | undefined {
  if (!report || typeof report.mechanical !== 'string') return undefined
  const checks = Array.isArray(report.checks) ? (report.checks as Record<string, unknown>[]) : []
  const failing = checks.filter(c => c.status === 'FAIL').map(c => `${c.name}: ${c.detail}`)
  const warns = checks.filter(c => c.status === 'WARN').map(c => `${c.name}: ${c.detail}`)
  const summary = (failing.length ? failing : warns).join('; ').slice(0, 240)
  return { mechanical: report.mechanical, summary }
}

/** The prompt that wakes Claude when runs end (English: it is for the model). */
export function wakeMessage(finished: RunView[], now: number): string {
  const lines = ['[zcrew] Worker run(s) ended while you were idle. Continue the review:']
  for (const run of finished) {
    if (run.stalled) {
      const silent = minutes(now - (run.lastChangeAt ?? run.startedAt))
      lines.push(`- ${run.runId} "${run.title}": STALLED, no worker activity for ${silent} min. Check it with agent-observe, then cancel it (agent-control) and start it again.`)
      continue
    }
    const took = minutes((run.endedAt ?? now) - run.startedAt)
    const gate = run.gate
      ? `gate MECHANICAL ${run.gate.mechanical}${run.gate.summary ? ` (${run.gate.summary})` : ''}`
      : 'gate not run (run it yourself)'
    lines.push(`- ${run.runId} "${run.title}": ${run.status} after ${took} min; ${gate}.`)
    if (run.status === 'background' && run.background?.length) {
      const jobs = run.background.map(j => `${j.taskId}${j.description ? ` (${j.description})` : ''}`).join(', ')
      lines.push(`  Its turn ended but background job(s) still run and keep the run open: ${jobs}. Unless you need them, stop each with agent-control {"runId": "${run.runId}", "action": "cancel-background", "taskId": "<id>"}.`)
    }
  }
  if (finished.some(r => !r.stalled)) {
    lines.push(
      'Review each with `zcrew gate <runId>` (its VERIFY results count as evidence), fetch the worker result with agent-wait (resultChars > 0) if you need its REPORT, then accept with `zcrew gate <runId> --accept "..."` or send a correction to the same thread.',
    )
  }
  return lines.join('\n')
}

/** The reminder added to a quick agent-wait answer while the mod watches the run. */
export function handsFreeNote(runId: string): string {
  return `[zcrew] The zcrew mod is watching ${runId}. Do not wait or poll: start any other independent task, then end your turn. A [zcrew] message will wake you when the run ends, with its gate result.`
}

/** Native notification text for runs that just ended. */
export function notifyText(finished: RunView[], lang: Lang): string {
  const w = words(lang)
  return finished
    .map(r => `${r.status === 'completed' && r.gate?.mechanical !== 'FAIL' ? '✅' : '❌'} ${r.title} — ${r.status === 'completed' ? (r.gate?.mechanical === 'FAIL' ? w.gateFail : w.waitClaude) : w.failed}`)
    .join('\n')
}

/** Time box from zcrew config files (project over user), in milliseconds. */
export function boxFromConfig(texts: (string | undefined)[]): number {
  for (const text of texts) {
    const data = parseSnapshot(text)
    const value = data?.wrapUpSeconds
    if (typeof value === 'number' && value > 0) return value * 1000
  }
  return DEFAULT_BOX_MS
}
