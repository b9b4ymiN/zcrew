import { describe, expect, test } from 'claude-code/testing'

import type { RunView } from '../types'
import type { Seg } from '../hooks/model'
import {
  BACKGROUND_GRACE_MS, STALL_MS, activityOf, bandLines, bandRows, bandVisible, boxFromConfig, boxFromStart, boxOf, capText, chromeText, detailOf,
  gateSummary, iconOf, isStalled, levelOf, liveJobs, meter, parseSnapshot, queueReason, slots, stageOf, statusText, titleOf, turnOver,
  usageText, wakeMessage,
} from '../hooks/model'

const BOX = 20 * 60_000
const NOW = 10_000_000

function run(over: Partial<RunView>): RunView {
  return { runId: 'run_1', title: 'Hero section', status: 'running', tools: [], startedAt: NOW - 5 * 60_000, round: 0, ...over }
}

describe('model', () => {
  test('titles come from OBJECTIVE', () => {
    expect(titleOf('OBJECTIVE\nAdd the hero section.\n\nSCOPE\n- `src/**`')).toBe('Add the hero section.')
    expect(titleOf('## OBJECTIVE:\n\n- Fix tax rounding')).toBe('Fix tax rounding')
    expect(titleOf('You are the implementer. Do X')).toBe('You are the implementer. Do X')
    expect(titleOf('x'.repeat(80)).length).toBe(48)
  })

  test('snapshots parse from text or objects', () => {
    expect(parseSnapshot('{"runId":"r","status":"running"}')?.status).toBe('running')
    expect(parseSnapshot({ status: 'completed' })?.status).toBe('completed')
    expect(parseSnapshot('not json')).toBeUndefined()
    expect(parseSnapshot('[1]')).toBeUndefined()
  })

  test('stages order attention before progress', () => {
    expect(stageOf(run({}), NOW, BOX)).toBe('running')
    expect(stageOf(run({ runningSince: NOW - 18 * 60_000 }), NOW, BOX)).toBe('near')
    expect(stageOf(run({ status: 'completed' }), NOW, BOX)).toBe('review')
    expect(stageOf(run({ status: 'completed', gate: { mechanical: 'FAIL', summary: 'x' } }), NOW, BOX)).toBe('attention')
    expect(stageOf(run({ status: 'failed' }), NOW, BOX)).toBe('attention')
    expect(stageOf(run({ status: 'completed', verdict: 'accept' }), NOW, BOX)).toBe('accepted')
    expect(stageOf(run({ status: 'completed', verdict: 'closed' }), NOW, BOX)).toBe('accepted')
    expect(stageOf(run({ status: 'completed', verdict: 'reject' }), NOW, BOX)).toBe('rejected')
  })

  test('activity in plain words', () => {
    expect(activityOf({ tools: ['Bash'], status: 'running' }, 'th')).toBe('กำลังรันคำสั่ง')
    expect(activityOf({ tools: ['Read', 'Edit'], status: 'running' }, 'en')).toBe('editing files')
    expect(activityOf({ tools: [], status: 'queued' }, 'en')).toBe('queued')
    expect(activityOf({ tools: [], status: 'stopping' }, 'en')).toBe('stopping to report')
  })

  test('band: summary first, then only what needs someone', () => {
    const runs = [
      run({ runId: 'a', title: 'Hero' }),
      run({ runId: 'b', title: 'Footer', status: 'completed', gate: { mechanical: 'FAIL', summary: 'verify: npm test -> exit 1' } }),
      run({ runId: 'c', title: 'Nav', status: 'completed', verdict: 'accept' }),
    ]
    const lines = bandLines(runs, NOW, BOX, 'en', 3)
    expect(lines[0]).toBe('zcrew  🔧 1 working · ✅ 1 accepted · ❌ 1 failed')
    expect(lines[1]).toContain('❌ Footer — gate failed: verify: npm test -> exit 1')
    expect(lines.length).toBe(2) // the running Hero is counted, not listed
    expect(bandLines(runs, NOW, BOX, 'en', 1).length).toBe(1)
  })

  test('band: all done, then gone after it lingers', () => {
    const done = [run({ status: 'completed', verdict: 'accept', endedAt: NOW - 1000 })]
    expect(bandLines(done, NOW, BOX, 'th', 3)).toEqual(['zcrew  🎉 เสร็จหมด 1 งาน'])
    expect(bandVisible(done, NOW)).toBe(true)
    expect(bandVisible(done, NOW + 3 * 60_000)).toBe(false)
    expect(bandVisible([], NOW)).toBe(false)
    expect(statusText(done, NOW + 3 * 60_000, BOX)).toBeUndefined()
    expect(statusText([run({}), run({ runId: 'b', status: 'completed' })], NOW, BOX)).toBe('zcrew 🔧1 👀1')
  })

  test('gate summary and wake message', () => {
    const gate = gateSummary({ mechanical: 'FAIL', checks: [
      { name: 'scope', status: 'PASS', detail: 'ok' },
      { name: 'verify', status: 'FAIL', detail: 'npm test -> exit 1' },
    ] })
    expect(gate).toEqual({ mechanical: 'FAIL', summary: 'verify: npm test -> exit 1' })
    expect(gateSummary({})).toBeUndefined()
    const text = wakeMessage([run({ status: 'completed', endedAt: NOW, gate })], NOW)
    expect(text).toContain('[zcrew]')
    expect(text).toContain('run_1 "Hero section": completed after 5 min; gate MECHANICAL FAIL (verify: npm test -> exit 1).')
    expect(wakeMessage([run({ status: 'failed', endedAt: NOW })], NOW)).toContain('gate not run')
  })

  test('time box from config, project first', () => {
    expect(boxFromConfig(['{"wrapUpSeconds": 600}', '{"wrapUpSeconds": 900}'])).toBe(600_000)
    expect(boxFromConfig([undefined, '{"wrapUpSeconds": 900}'])).toBe(900_000)
    expect(boxFromConfig(['{"wrapUpSeconds": 0}'])).toBe(3_000_000)
    expect(boxFromConfig([])).toBe(3_000_000)
  })

  test('a run keeps the time box the bridge gave it', () => {
    expect(boxFromStart({ gate: { timebox: { seconds: 3600, source: 'contract' } } })).toBe(3_600_000)
    expect(boxFromStart({ gate: { timebox: { seconds: null, source: 'off' } } })).toBeUndefined()
    expect(boxFromStart({ gate: { verify: 'x' } })).toBeUndefined()
    expect(boxFromStart({ gate: 'nope' })).toBeUndefined()
    expect(boxFromStart(undefined)).toBeUndefined()
    expect(boxOf(run({ boxMs: 60 * 60_000 }), BOX)).toBe(60 * 60_000)
    expect(boxOf(run({}), BOX)).toBe(BOX)
  })

  test('a run with a longer time box is not "near" at 21 minutes (issue #2)', () => {
    const long = run({ runningSince: NOW - 21 * 60_000, boxMs: 60 * 60_000 })
    expect(stageOf(long, NOW, BOX)).toBe('running')
    expect(detailOf(long, NOW, BOX, 'en')).toContain('21/60 min')
    const plain = run({ runningSince: NOW - 21 * 60_000 })
    expect(stageOf(plain, NOW, BOX)).toBe('near')
    expect(detailOf(plain, NOW, BOX, 'en')).toContain('21/20 min')
  })
})

describe('background jobs', () => {
  test('only live jobs count', () => {
    expect(liveJobs([
      { taskId: 'a', status: 'running', description: 'npm run dev' },
      { taskId: 'b', status: 'completed', description: 'npm test' },
      { status: 'running' },
    ])).toEqual([{ taskId: 'a', description: 'npm run dev' }])
    expect(liveJobs(undefined)).toEqual([])
  })

  test('a run held open by background jobs is flagged, then counts as over', () => {
    const held = run({ status: 'background', background: [{ taskId: 'a', description: 'npm run dev' }], backgroundSince: NOW - 10_000 })
    expect(stageOf(held, NOW, BOX)).toBe('attention')
    expect(detailOf(held, NOW, BOX, 'en')).toBe('done, but background jobs still run 1')
    expect(turnOver(held, NOW)).toBe(false)
    expect(turnOver(held, NOW - 10_000 + BACKGROUND_GRACE_MS)).toBe(true)
    expect(turnOver(run({}), NOW)).toBe(false)
    expect(turnOver(run({ status: 'completed' }), NOW)).toBe(true)
  })

  test('the wake message says how to stop them', () => {
    const held = run({ status: 'background', endedAt: NOW, background: [{ taskId: 'a', description: 'npm run dev' }] })
    const text = wakeMessage([held], NOW)
    expect(text).toContain('background job(s) still run and keep the run open: a (npm run dev)')
    expect(text).toContain('"action": "cancel-background", "taskId": "<id>"')
  })
})

describe('queue, stall and capacity', () => {
  test('queue reason comes from the lease blockers', () => {
    const snap = (lease: unknown) => ({ resourceLease: lease })
    expect(queueReason(snap({ acquired: false, blockers: [{ key: 'zcrew:worker-slot:2' }, { key: 'other' }] }))).toBe('slot')
    expect(queueReason(snap({ acquired: false, blockers: [{ key: 'C:/work/site' }] }))).toBe('folder')
    expect(queueReason(snap({ acquired: true, blockers: [{ key: 'zcrew:worker-slot:2' }] }))).toBeUndefined()
    expect(queueReason(snap({ acquired: false, blockers: [] }))).toBeUndefined()
    expect(queueReason({})).toBeUndefined()
    expect(queueReason(undefined)).toBeUndefined()
  })

  test('a run stalls after 30 min of silence while running', () => {
    expect(isStalled(run({ lastChangeAt: NOW - STALL_MS }), NOW)).toBe(true)
    expect(isStalled(run({ lastChangeAt: NOW - 29 * 60_000 }), NOW)).toBe(false)
    expect(isStalled(run({ status: 'queued', lastChangeAt: NOW - 31 * 60_000 }), NOW)).toBe(false)
    expect(isStalled(run({}), NOW)).toBe(false)
  })

  test('cap text in both languages, backoff flagged', () => {
    expect(capText({ used: 2, cap: 5, configured: 5, backoff: false }, 'th')).toBe('ช่าง 2/5')
    expect(capText({ used: 2, cap: 5, configured: 5, backoff: false }, 'en')).toBe('workers 2/5')
    expect(capText({ used: 1, cap: 2, configured: 5, backoff: true }, 'th')).toBe('ช่าง 1/2 ⚠1302')
    expect(capText({ used: 1, cap: 2, configured: 5, backoff: true }, 'en')).toBe('workers 1/2 ⚠1302')
  })

  test('band counts queued runs, lists them, and appends the cap with slot dots', () => {
    const lines = bandLines(
      [run({ runId: 'a', title: 'Hero' }), run({ runId: 'q', title: 'Nav', status: 'queued', queue: 'slot' })],
      NOW, BOX, 'th', 3, { used: 2, cap: 5, configured: 5, backoff: false },
    )
    expect(lines[0]).toBe('zcrew  🔧 1 กำลังทำ · ⏳ 1 รอคิว   ช่าง ●●○○○ 2/5')
    expect(lines[1]).toBe('  ⏳ Nav — รอคิว (เต็มเพดาน)')
    expect(bandLines([run({ runId: 'q', title: 'Nav', status: 'queued', queue: 'folder' })], NOW, BOX, 'en', 3))
      .toEqual(['zcrew  ⏳ 1 queued', '  ⏳ Nav — waiting for the folder'])
    expect(bandLines([run({ runId: 'q', title: 'Nav', status: 'queued' })], NOW, BOX, 'en', 3))
      .toEqual(['zcrew  ⏳ 1 queued', '  ⏳ Nav — queued'])
  })

  test('status line counts queued and carries the cap', () => {
    const queued = run({ runId: 'q', status: 'queued', queue: 'slot' })
    expect(statusText([run({ runId: 'a' }), queued], NOW, BOX, { used: 2, cap: 5, configured: 5, backoff: false }, 'en'))
      .toBe('zcrew 🔧1 ⏳1 · workers 2/5')
    expect(statusText([run({ runId: 'a' }), queued], NOW, BOX)).toBe('zcrew 🔧1 ⏳1')
  })

  test('a stalled run draws as attention and wakes with STALLED', () => {
    const stalled = run({ stalled: true, lastChangeAt: NOW - 35 * 60_000, endedAt: NOW })
    expect(stageOf(stalled, NOW, BOX)).toBe('attention')
    expect(iconOf(stalled, NOW, BOX)).toBe('❌')
    expect(detailOf(stalled, NOW, BOX, 'th')).toBe('เงียบ 35 นาที')
    expect(detailOf(stalled, NOW, BOX, 'en')).toBe('silent 35 min')
    expect(turnOver(stalled, NOW)).toBe(true)
    expect(wakeMessage([stalled], NOW))
      .toContain('run_1 "Hero section": STALLED, no worker activity for 35 min. Check it with agent-observe, then cancel it (agent-control) and start it again.')
  })

  test('band rows carry stage colors, dim details and the cap dots', () => {
    const cap = { used: 2, cap: 5, configured: 5, backoff: false }
    const runs = [
      run({ runId: 'w', title: 'Work' }),
      run({ runId: 'q', title: 'Nav', status: 'queued', queue: 'slot' }),
      run({ runId: 's', title: 'Migrate', stalled: true, lastChangeAt: NOW - 35 * 60_000, endedAt: NOW }),
    ]
    const rows = bandRows(runs, NOW, BOX, 'en', 3, cap)
    const head = rows[0]
    expect(head.find(s => s.text.startsWith('🔧'))?.color).toBe('claude')
    expect(head.find(s => s.text.startsWith('⏳'))?.color).toBe('subtle')
    expect(head.find(s => s.text.startsWith('❌'))?.color).toBe('error')
    expect(head.filter(s => /^[●○]+$/.test(s.text))).toEqual([
      { text: '●●', color: 'success' }, { text: '○○○', color: 'subtle' },
    ])
    const byTitle = (word: string): Seg[] | undefined => rows.find(r => r.some(s => s.text === word))
    expect(byTitle('Migrate')?.find(s => s.text === '❌ ')?.color).toBe('error')
    expect(byTitle('Migrate')?.find(s => s.dim)?.text).toBe('silent 35 min')
    expect(byTitle('Nav')?.find(s => s.text === '⏳ ')?.color).toBe('subtle')
    expect(byTitle('Nav')?.find(s => s.dim)?.text).toBe('queued (worker cap full)')
  })

  test('a review row is a suggestion; backoff dots and the marker warn', () => {
    const review = bandRows([run({ runId: 'r', status: 'completed', gate: { mechanical: 'PASS', summary: '' } })], NOW, BOX, 'en', 3)
    expect(review[0].find(s => s.text.startsWith('👀'))?.color).toBe('suggestion')
    expect(review[1].find(s => s.text === '👀 ')?.color).toBe('suggestion')
    const backoff = bandRows([run({ runId: 'a' })], NOW, BOX, 'th', 3, { used: 1, cap: 2, configured: 5, backoff: true })
    expect(backoff[0].filter(s => /^[●○]+$/.test(s.text))).toEqual([
      { text: '●', color: 'warning' }, { text: '○', color: 'subtle' },
    ])
    expect(backoff[0].find(s => s.text === ' ⚠1302')).toEqual({ text: ' ⚠1302', color: 'warning' })
  })
})

describe('meters, levels and slots', () => {
  test('the usage meter fills by percent, clamped to the width', () => {
    expect(meter(0)).toBe('▱▱▱▱▱')
    expect(meter(42)).toBe('▰▰▱▱▱')
    expect(meter(100)).toBe('▰▰▰▰▰')
    expect(meter(150)).toBe('▰▰▰▰▰')
  })

  test('levels flip at 60 and 85', () => {
    expect(levelOf(59)).toBe('success')
    expect(levelOf(60)).toBe('warning')
    expect(levelOf(84)).toBe('warning')
    expect(levelOf(85)).toBe('error')
  })

  test('slot dots fill up to the cap, never past it', () => {
    expect(slots(2, 5)).toBe('●●○○○')
    expect(slots(0, 3)).toBe('○○○')
    expect(slots(7, 5)).toBe('●●●●●')
  })
})

describe('usage segment', () => {
  const RESETS_AT = new Date(2026, 0, 2, 14, 30).toISOString() // 14:30 local

  test('context and windows render with meters, unknown kinds ignored', () => {
    expect(usageText({
      context: { percent: 42 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 23 },
        { kind: 'seven_day', percentUsed: 61 },
        { kind: 'spend_limit', percentUsed: 90 },
      ],
    }, NOW, 'en')).toBe('ctx ▰▰▱▱▱ 42% │ 5h ▰▱▱▱▱ 23% │ 7d ▰▰▰▱▱ 61%')
  })

  test('missing pieces are omitted, nothing known is undefined', () => {
    expect(usageText({ context: { percent: 7 }, rateLimits: [] }, NOW, 'en')).toBe('ctx ▱▱▱▱▱ 7%') // off-subscription
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 10 }] }, NOW, 'en')).toBe('5h ▰▱▱▱▱ 10%')
    expect(usageText({ context: { window: 200000 } }, NOW, 'en')).toBeUndefined() // no fill yet
    expect(usageText({}, NOW, 'en')).toBeUndefined()
    expect(usageText(undefined, NOW, 'en')).toBeUndefined()
  })

  test('a window past 80% says when it resets', () => {
    expect(usageText({ rateLimits: [{ kind: 'seven_day', percentUsed: 85, resetsAt: RESETS_AT }] }, NOW, 'en'))
      .toBe('7d ▰▰▰▰▱ 85% (resets 14:30)')
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 92, resetsAt: RESETS_AT }] }, NOW, 'th'))
      .toBe('5h ▰▰▰▰▰ 92% (รีเซ็ต 14:30)')
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 79, resetsAt: RESETS_AT }] }, NOW, 'en'))
      .toBe('5h ▰▰▰▰▱ 79%') // below 80%: no reset time even with resetsAt
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 85 }] }, NOW, 'en')).toBe('5h ▰▰▰▰▱ 85%') // no resetsAt
  })

  test('chrome joins the parts that exist', () => {
    expect(chromeText('ctx 42% · 5h 23%', 'zcrew 🔧1')).toBe('ctx 42% · 5h 23% │ zcrew 🔧1')
    expect(chromeText('ctx 42%', undefined)).toBe('ctx 42%')
    expect(chromeText(undefined, 'zcrew 🔧1')).toBe('zcrew 🔧1')
    expect(chromeText(undefined, undefined)).toBeUndefined()
  })
})
