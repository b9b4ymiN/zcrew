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

/** The worker's turn is over: terminal, or only background jobs keep the run open past the grace. */
export function turnOver(run: RunView, now: number): boolean {
  if (!isActive(run)) return true
  return run.status === 'background' && run.backgroundSince !== undefined && now - run.backgroundSince >= BACKGROUND_GRACE_MS
}

/** Where a run stands, for ordering and icons: attention first. */
export function stageOf(run: RunView, now: number, boxMs: number): 'attention' | 'near' | 'running' | 'review' | 'accepted' | 'rejected' {
  if (run.verdict === 'accept' || run.verdict === 'closed') return 'accepted'
  if (run.verdict === 'reject') return 'rejected'
  if (run.status === 'background') return 'attention'
  if (isActive(run)) {
    const since = run.runningSince ?? run.startedAt
    return now - since >= boxMs * NEAR_BOX ? 'near' : 'running'
  }
  if (run.status !== 'completed' || run.gate?.mechanical === 'FAIL') return 'attention'
  return 'review'
}

const ICON = { attention: '❌', near: '⏰', running: '🔧', review: '👀', accepted: '✅', rejected: '↩' } as const
const ORDER = { attention: 0, near: 1, running: 2, review: 3, rejected: 4, accepted: 5 } as const

export function sorted(runs: RunView[], now: number, boxMs: number): RunView[] {
  return [...runs].sort((a, b) =>
    ORDER[stageOf(a, now, boxMs)] - ORDER[stageOf(b, now, boxMs)] || b.startedAt - a.startedAt)
}

/** Detail for one run: what it is doing, or what it waits for. */
export function detailOf(run: RunView, now: number, boxMs: number, lang: Lang): string {
  const w = words(lang)
  const stage = stageOf(run, now, boxMs)
  if (stage === 'running' || stage === 'near') {
    const since = run.runningSince ?? run.startedAt
    const left = `${minutes(now - since)}/${minutes(boxMs)} ${lang === 'th' ? w.min : 'min'}`
    return `${stage === 'near' ? w.nearBox + ' ' : ''}${left} · ${activityOf(run, lang)}`
  }
  if (stage === 'attention') {
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

/** The band: one summary line, then the runs that need someone, most urgent first. */
export function bandLines(runs: RunView[], now: number, boxMs: number, lang: Lang, maxRows: number): string[] {
  const w = words(lang)
  if (runs.length === 0) return []
  const stages = runs.map(r => stageOf(r, now, boxMs))
  const count = (...s: string[]) => stages.filter(x => s.includes(x)).length
  const working = count('running', 'near')
  const parts: string[] = []
  if (working) parts.push(`🔧 ${working} ${w.running}`)
  if (count('review')) parts.push(`👀 ${count('review')} ${w.review}`)
  if (count('accepted')) parts.push(`✅ ${count('accepted')} ${w.accepted}`)
  if (count('attention')) parts.push(`❌ ${count('attention')} ${w.failed}`)
  if (working === 0 && count('review', 'attention') === 0) {
    return [`zcrew  🎉 ${w.allDone} ${runs.length} ${w.tasks}`]
  }
  const lines = [`zcrew  ${parts.join(' · ')}`]
  for (const run of sorted(runs, now, boxMs)) {
    if (lines.length >= Math.max(1, Math.min(3, maxRows))) break
    const stage = stageOf(run, now, boxMs)
    if (stage === 'attention' || stage === 'near' || stage === 'review') {
      lines.push(`  ${iconOf(run, now, boxMs)} ${run.title} — ${detailOf(run, now, boxMs, lang)}`)
    }
  }
  return lines
}

/** The pinned status-line text, or undefined to clear it. */
export function statusText(runs: RunView[], now: number, boxMs: number): string | undefined {
  if (!bandVisible(runs, now)) return undefined
  const stages = runs.map(r => stageOf(r, now, boxMs))
  const n = (s: string) => stages.filter(x => x === s).length
  const bits = [
    n('running') + n('near') ? `🔧${n('running') + n('near')}` : '',
    n('review') ? `👀${n('review')}` : '',
    n('attention') ? `❌${n('attention')}` : '',
    n('accepted') ? `✅${n('accepted')}` : '',
  ].filter(Boolean)
  return bits.length ? `zcrew ${bits.join(' ')}` : undefined
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
  lines.push(
    'Review each with `zcrew gate <runId>` (its VERIFY results count as evidence), fetch the worker result with agent-wait (resultChars > 0) if you need its REPORT, then accept with `zcrew gate <runId> --accept "..."` or send a correction to the same thread.',
  )
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
