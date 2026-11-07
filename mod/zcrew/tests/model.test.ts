import { describe, expect, test } from 'claude-code/testing'

import type { RunView } from '../types'
import type { Seg } from '../hooks/model'
import {
  BACKGROUND_GRACE_MS, OBSERVE_ERRORS_TO_FAIL, STALL_MS, activityOf, agoText, bandLines, bandRows, bandVisible, boxFromConfig, boxOf, capText,
  chromeText, detailOf, failureLabel, fitPath, fitText, gateSummary, iconOf, isStalled, levelOf, liveJobs, liveOf, liveTotals, mergeCodex, meter,
  notifyText, observeError, paneSections, parseFailure, parseSnapshot, queueReason, recordIdOf, slots, slotSquares, stageOf, statusText, titleOf,
  turnOver, usageText, wakeMessage,
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
    expect(lines[0]).toBe('zcrew  ▶ 1 working · ✗ 1 needs you · ✓ 1 accepted')
    expect(lines[1]).toContain('✗ Footer — gate failed: verify: npm test -> exit 1')
    expect(lines.length).toBe(2) // the running Hero is counted, not listed
    expect(bandLines(runs, NOW, BOX, 'en', 1).length).toBe(1)
  })

  test('band: all done, then gone after it lingers', () => {
    const done = [run({ status: 'completed', verdict: 'accept', endedAt: NOW - 1000 })]
    expect(bandLines(done, NOW, BOX, 'th', 3)).toEqual(['zcrew  ✓ เสร็จหมด 1 งาน'])
    expect(bandVisible(done, NOW)).toBe(true)
    expect(bandVisible(done, NOW + 3 * 60_000)).toBe(false)
    expect(bandVisible([], NOW)).toBe(false)
    expect(statusText(done, NOW + 3 * 60_000, BOX)).toBeUndefined()
    expect(statusText([run({}), run({ runId: 'b', status: 'completed' })], NOW, BOX)).toBe('▶ 1 ทำอยู่  ◎ 1 รอตรวจ')
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
    expect(boxFromConfig(['{"wrapUpSeconds": 0}'])).toBe(1_200_000)
    expect(boxFromConfig([])).toBe(1_200_000)
  })
})

describe('failure causes', () => {
  const quota = { kind: 'quota', message: 'GLM quota used up for the 5h window', advice: 'Wait for the window to reset.', resetsAt: '15:00' }

  test('parseFailure accepts only a well-formed failure', () => {
    expect(parseFailure({ failure: quota })).toEqual(quota)
    expect(parseFailure({ failure: { kind: 'other', message: 'boom', advice: 'check it' } })).toEqual({ kind: 'other', message: 'boom', advice: 'check it' })
    expect(parseFailure({ failure: { kind: 'other', message: 'boom', advice: 'check it', resetsAt: 15 } })).toEqual({ kind: 'other', message: 'boom', advice: 'check it' })
    expect(parseFailure({ failure: 'junk' })).toBeUndefined()
    expect(parseFailure({ failure: { kind: 1, message: 'boom', advice: 'check it' } })).toBeUndefined()
    expect(parseFailure({})).toBeUndefined()
    expect(parseFailure(undefined)).toBeUndefined()
  })

  test('failure labels in both languages, quota reset only when known', () => {
    expect(failureLabel(quota, 'th')).toBe('โควต้า GLM หมด · รีเซ็ต 15:00')
    expect(failureLabel(quota, 'en')).toBe('GLM quota used up · resets 15:00')
    expect(failureLabel({ ...quota, resetsAt: undefined }, 'th')).toBe('โควต้า GLM หมด')
    expect(failureLabel({ ...quota, resetsAt: undefined }, 'en')).toBe('GLM quota used up')
    expect(failureLabel({ kind: 'concurrency', message: 'm', advice: 'a' }, 'th')).toBe('worker เกินโควต้าพร้อมกัน (1302)')
    expect(failureLabel({ kind: 'concurrency', message: 'm', advice: 'a' }, 'en')).toBe('too many workers at once (1302)')
    expect(failureLabel({ kind: 'overload', message: 'm', advice: 'a' }, 'th')).toBe('z.ai ขัดข้อง')
    expect(failureLabel({ kind: 'network', message: 'm', advice: 'a' }, 'en')).toBe('z.ai error')
    expect(failureLabel({ kind: 'model_timeout', message: 'm', advice: 'a' }, 'th')).toBe('โมเดลไม่ตอบ')
    expect(failureLabel({ kind: 'model_timeout', message: 'm', advice: 'a' }, 'en')).toBe('model timed out')
    expect(failureLabel({ kind: 'app_server_exited', message: 'm', advice: 'a' }, 'th')).toBe('ZCode app-server ล่ม')
    expect(failureLabel({ kind: 'app_server_exited', message: 'm', advice: 'a' }, 'en')).toBe('ZCode app-server died')
    expect(failureLabel({ kind: 'run_lost', message: 'm', advice: 'a' }, 'th')).toBe('run หาย (bridge restart)')
    expect(failureLabel({ kind: 'run_lost', message: 'm', advice: 'a' }, 'en')).toBe('run lost (bridge restart)')
    expect(failureLabel({ kind: 'timed_out', message: 'm', advice: 'a' }, 'th')).toBe('หมดเวลา')
    expect(failureLabel({ kind: 'timed_out', message: 'm', advice: 'a' }, 'en')).toBe('timed out')
    expect(failureLabel({ kind: 'other', message: 'm', advice: 'a' }, 'th')).toBe('ล้ม')
    expect(failureLabel({ kind: 'other', message: 'm', advice: 'a' }, 'en')).toBe('failed')
    expect(failureLabel({ kind: 'surprise', message: 'm', advice: 'a' }, 'th')).toBe('ล้ม') // unknown kinds fall back
    expect(failureLabel({ kind: 'surprise', message: 'm', advice: 'a' }, 'en')).toBe('failed')
  })

  test('an unknown run is lost at once; other observe errors fail on the 3rd', () => {
    expect(observeError('run not found: run_e1f80bc58dcd4b3caf2f2509f17b7bc7', 1)).toEqual({
      failed: true,
      failure: {
        kind: 'run_lost', message: 'run not found: run_e1f80bc58dcd4b3caf2f2509f17b7bc7',
        advice: 'The bridge no longer knows this run (bridge restart); check the worktree, then start a new run.',
      },
    })
    expect(observeError('Session not found: run_1', 1).failed).toBe(true)
    expect(observeError('error code: run_not_found', 1).failed).toBe(true)
    expect(observeError('MCP error: connection closed', 1).failed).toBe(false) // transient: watch on
    expect(observeError('MCP error: connection closed', 2).failed).toBe(false)
    const third = observeError('MCP error: connection closed', OBSERVE_ERRORS_TO_FAIL)
    expect(third.failed).toBe(true)
    if (third.failed) {
      expect(third.failure.kind).toBe('other')
      expect(third.failure.advice).toBe('agent-observe kept failing; check the run with agent-observe yourself.')
    }
    const clipped = observeError('x'.repeat(300), OBSERVE_ERRORS_TO_FAIL)
    expect(clipped.failed && clipped.failure.message.length).toBe(160) // the message is capped
  })

  test('the wake message names the failure, its advice, and the quota line', () => {
    const text = wakeMessage([run({ status: 'failed', endedAt: NOW, failure: quota })], NOW)
    expect(text).toContain('run_1 "Hero section": failed after 5 min: GLM quota used up for the 5h window; gate not run')
    expect(text).toContain('  Wait for the window to reset.')
    expect(text).toContain('  Tell the user now, in their language: the GLM quota is used up and resets at 15:00; they can reset it themselves.')
    const unknownReset = wakeMessage([run({ status: 'failed', endedAt: NOW, failure: { ...quota, resetsAt: undefined } })], NOW)
    expect(unknownReset).toContain('  Tell the user now, in their language: the GLM quota is used up; they can reset it themselves.')
    const lost = wakeMessage([run({
      status: 'failed', endedAt: NOW,
      failure: { kind: 'run_lost', message: 'run not found: run_1', advice: 'The bridge no longer knows this run (bridge restart); check the worktree, then start a new run.' },
    })], NOW)
    expect(lost).toContain('failed after 5 min: run not found: run_1')
    expect(lost).toContain('  The bridge no longer knows this run (bridge restart); check the worktree, then start a new run.')
    expect(lost).not.toContain('Tell the user') // only quota says that
    expect(wakeMessage([run({ status: 'completed', endedAt: NOW })], NOW)).not.toContain('Tell the user') // no failure: unchanged shape
  })

  test('the notification and the band label the failure instead of the bare word', () => {
    const failed = run({ status: 'failed', endedAt: NOW, failure: quota })
    expect(notifyText([failed], 'th')).toBe('❌ Hero section — โควต้า GLM หมด · รีเซ็ต 15:00')
    expect(notifyText([failed], 'en')).toBe('❌ Hero section — GLM quota used up · resets 15:00')
    expect(notifyText([run({ status: 'failed', endedAt: NOW })], 'en')).toBe('❌ Hero section — failed') // no failure: the bare word stays
    expect(bandLines([failed], NOW, BOX, 'th', 3)[1]).toBe('  ✗ Hero section — โควต้า GLM หมด · รีเซ็ต 15:00')
    expect(bandLines([failed], NOW, BOX, 'en', 3)[1]).toBe('  ✗ Hero section — GLM quota used up · resets 15:00')
  })
})

describe('a run\'s own time box', () => {
  const hour = run({ boxMs: 60 * 60_000, runningSince: NOW - 25 * 60_000 })

  test('boxOf prefers the run\'s box, else the default', () => {
    expect(boxOf(hour, BOX)).toBe(60 * 60_000)
    expect(boxOf(run({ boxMs: 0 }), BOX)).toBe(BOX)
    expect(boxOf(run({ boxMs: -1 }), BOX)).toBe(BOX)
    expect(boxOf(run({}), BOX)).toBe(BOX)
  })

  test('25 min of a 60-min box is running, not near; the default box still flags it', () => {
    expect(stageOf(hour, NOW, BOX)).toBe('running')
    expect(stageOf(run({ runningSince: NOW - 25 * 60_000 }), NOW, BOX)).toBe('near')
    expect(detailOf(hour, NOW, BOX, 'en')).toBe('25/60 min · thinking')
    expect(detailOf(hour, NOW, BOX, 'th')).toBe('25/60 นาที · กำลังคิด')
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
    expect(capText({ used: 2, cap: 5, configured: 5, backoff: false }, 'th')).toBe('◼◼◻◻◻ 2/5')
    expect(capText({ used: 2, cap: 5, configured: 5, backoff: false }, 'en')).toBe('◼◼◻◻◻ 2/5')
    expect(capText({ used: 1, cap: 2, configured: 5, backoff: true }, 'th')).toBe('◼◻ 1/2 ⚠1302')
    expect(capText({ used: 1, cap: 2, configured: 5, backoff: true }, 'en')).toBe('◼◻ 1/2 ⚠1302')
  })

  test('band counts queued runs, lists them, and appends the cap with slot dots', () => {
    const lines = bandLines(
      [run({ runId: 'a', title: 'Hero' }), run({ runId: 'q', title: 'Nav', status: 'queued', queue: 'slot' })],
      NOW, BOX, 'th', 3, { used: 2, cap: 5, configured: 5, backoff: false },
    )
    expect(lines[0]).toBe('zcrew  ▶ 1 ทำอยู่ · ⏸ 1 รอคิว   ◼◼◻◻◻ 2/5')
    expect(lines[1]).toBe('  ⏸ Nav — รอคิว (เต็มเพดาน)')
    expect(bandLines([run({ runId: 'q', title: 'Nav', status: 'queued', queue: 'folder' })], NOW, BOX, 'en', 3))
      .toEqual(['zcrew  ⏸ 1 queued', '  ⏸ Nav — waiting for the folder'])
    expect(bandLines([run({ runId: 'q', title: 'Nav', status: 'queued' })], NOW, BOX, 'en', 3))
      .toEqual(['zcrew  ⏸ 1 queued', '  ⏸ Nav — queued'])
  })

  test('status line counts queued and carries the cap', () => {
    const queued = run({ runId: 'q', status: 'queued', queue: 'slot' })
    expect(statusText([run({ runId: 'a' }), queued], NOW, BOX, { used: 2, cap: 5, configured: 5, backoff: false }, 'en'))
      .toBe('▶ 1 working  ⏸ 1 queued  ◼◼◻◻◻ 2/5')
    expect(statusText([run({ runId: 'a' }), queued], NOW, BOX)).toBe('▶ 1 ทำอยู่  ⏸ 1 รอคิว')
  })

  test('a stalled run draws as attention and wakes with STALLED', () => {
    const stalled = run({ stalled: true, lastChangeAt: NOW - 35 * 60_000, endedAt: NOW })
    expect(stageOf(stalled, NOW, BOX)).toBe('attention')
    expect(iconOf(stalled, NOW, BOX)).toBe('✗')
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
    expect(head.find(s => s.text.startsWith('▶'))?.color).toBe('claude')
    expect(head.find(s => s.text.startsWith('⏸'))?.color).toBe('subtle')
    expect(head.find(s => s.text.startsWith('✗'))?.color).toBe('error')
    expect(head.filter(s => /^[◼◻]+$/.test(s.text))).toEqual([
      { text: '◼◼', color: 'success' }, { text: '◻◻◻', color: 'subtle' },
    ])
    const byTitle = (word: string): Seg[] | undefined => rows.find(r => r.some(s => s.text === word))
    expect(byTitle('Migrate')?.find(s => s.text === '✗ ')?.color).toBe('error')
    expect(byTitle('Migrate')?.find(s => s.dim)?.text).toBe('silent 35 min')
    expect(byTitle('Nav')?.find(s => s.text === '⏸ ')?.color).toBe('subtle')
    expect(byTitle('Nav')?.find(s => s.dim)?.text).toBe('queued (worker cap full)')
  })

  test('a review row is a suggestion; backoff squares and the marker warn', () => {
    const review = bandRows([run({ runId: 'r', status: 'completed', gate: { mechanical: 'PASS', summary: '' } })], NOW, BOX, 'en', 3)
    expect(review[0].find(s => s.text.startsWith('◎'))?.color).toBe('suggestion')
    expect(review[1].find(s => s.text === '◎ ')?.color).toBe('suggestion')
    const backoff = bandRows([run({ runId: 'a' })], NOW, BOX, 'th', 3, { used: 1, cap: 2, configured: 5, backoff: true })
    expect(backoff[0].filter(s => /^[◼◻]+$/.test(s.text))).toEqual([
      { text: '◼', color: 'warning' }, { text: '◻', color: 'subtle' },
    ])
    expect(backoff[0].find(s => s.text === ' ⚠1302')).toEqual({ text: ' ⚠1302', color: 'warning' })
  })
})

describe('codex runs', () => {
  const ID = 'codex_20261107101500_ab12'
  const cxEntry = (over: Record<string, unknown> = {}) => ({
    runId: ID, worker: 'codex', threadId: 'th_9', jobId: 'task-7', cwd: 'C:\\repo',
    title: 'Fix the gate', status: 'running', startedAt: NOW - 10 * 60_000, roundStartedAt: NOW - 5 * 60_000,
    endedAt: null, round: 1, report: `C:\\repo\\.cx\\${ID}.report.md`, verdict: null, warning: null, ...over,
  })
  const cxFeed = (runs: unknown[]) => ({ runs })

  test('the feed adds a card with the mod\'s rounds and the report path', () => {
    const card = mergeCodex({}, cxFeed([cxEntry()]), NOW)[ID]
    expect(card).toBeDefined()
    expect(card.worker).toBe('codex')
    expect(card.title).toBe('Fix the gate')
    expect(card.cwd).toBe('C:\\repo')
    expect(card.threadId).toBe('th_9')
    expect(card.status).toBe('running')
    expect(card.startedAt).toBe(NOW - 5 * 60_000) // roundStartedAt, not the run's first start
    expect(card.round).toBe(0) // feed rounds are 1-based, the mod's are 0-based
    expect(card.report).toBe(`C:\\repo\\.cx\\${ID}.report.md`)
    expect(card.endedAt).toBeUndefined()
  })

  test('a correction replaces its base card; an older round never wins', () => {
    let merged = mergeCodex({}, cxFeed([cxEntry()]), NOW)
    merged = mergeCodex(merged, cxFeed([cxEntry({ runId: `${ID}.r2`, round: 2, roundStartedAt: NOW - 60_000, status: 'queued' })]), NOW)
    expect(Object.keys(merged)).toEqual([`${ID}.r2`])
    expect(merged[`${ID}.r2`].round).toBe(1)
    merged = mergeCodex(merged, cxFeed([cxEntry({ status: 'completed', endedAt: NOW - 1000 })]), NOW)
    expect(Object.keys(merged)).toEqual([`${ID}.r2`]) // the base round id is stale, not a second card
  })

  test('unknown keeps the previous status; a terminal status ends the card once', () => {
    let merged = mergeCodex({}, cxFeed([cxEntry()]), NOW)
    merged = mergeCodex(merged, cxFeed([cxEntry({ status: 'unknown' })]), NOW)
    expect(merged[ID].status).toBe('running')
    merged = mergeCodex(merged, cxFeed([cxEntry({ status: 'completed', endedAt: NOW - 123 })]), NOW)
    expect(merged[ID].status).toBe('completed')
    expect(merged[ID].endedAt).toBe(NOW - 123)
    merged = mergeCodex(merged, cxFeed([cxEntry({ status: 'completed', endedAt: NOW })]), NOW)
    expect(merged[ID].endedAt).toBe(NOW - 123) // set once
    expect(mergeCodex({}, cxFeed([cxEntry({ status: 'failed', endedAt: null })]), NOW)[ID].endedAt).toBe(NOW) // feed endedAt missing: now
  })

  test('a feed verdict is copied; absent cards and bad or foreign entries change nothing', () => {
    const accepted = mergeCodex({}, cxFeed([cxEntry({ verdict: 'accept' })]), NOW)
    expect(accepted[ID].verdict).toBe('accept')
    const kept = mergeCodex(accepted, cxFeed([]), NOW) // the feed filters by age/verdict: absence is not an end
    expect(Object.keys(kept)).toEqual([ID])
    expect(kept[ID].verdict).toBe('accept')
    const zcode = run({ runId: 'run_1' })
    const none = mergeCodex({ run_1: zcode }, cxFeed([
      cxEntry({ runId: '' }), cxEntry({ runId: 'other_1', worker: 'zcode' }), 'junk', null, 7,
    ]), NOW)
    expect(none).toEqual({ run_1: zcode })
    expect(mergeCodex(accepted, 'not json', NOW)).toBe(accepted)
    expect(mergeCodex(accepted, undefined, NOW)).toBe(accepted)
  })

  test('the record of a verdict is the base file for a Codex correction, else the runId', () => {
    expect(recordIdOf(run({ runId: `${ID}.r2`, worker: 'codex' }))).toBe(ID)
    expect(recordIdOf(run({ runId: `${ID}.r3`, worker: 'codex' }))).toBe(ID)
    expect(recordIdOf(run({ runId: ID, worker: 'codex' }))).toBe(ID) // a first round is its own record
    expect(recordIdOf(run({ runId: `${ID}.r2` }))).toBe(`${ID}.r2`) // a ZCode run is never re-based
    expect(recordIdOf(run({}))).toBe('run_1')
  })

  test('band and wake text carry the Codex tag and the report path, ZCode rows unchanged', () => {
    const cx = run({ runId: 'codex_x', worker: 'codex', title: 'Nav', status: 'queued', report: 'C:\\repo\\.cx\\codex_x.report.md' })
    expect(bandLines([cx], NOW, BOX, 'en', 3)).toEqual(['zcrew  ⏸ 1 queued', '  ⏸ Codex Nav — queued'])
    const text = wakeMessage([run({
      runId: 'codex_x', worker: 'codex', status: 'completed', endedAt: NOW, report: 'C:\\repo\\.cx\\codex_x.report.md',
    })], NOW)
    expect(text).toContain('codex_x "Hero section" [Codex]: completed after 5 min; gate not run (run it yourself).')
    expect(text).toContain('Its worker report (read it instead of agent-wait): C:\\repo\\.cx\\codex_x.report.md')
    expect(text).not.toContain('fetch the worker result with agent-wait') // the ZCode-only advice line
    expect(bandLines([run({ runId: 'q', title: 'Nav', status: 'queued' })], NOW, BOX, 'en', 3))
      .toEqual(['zcrew  ⏸ 1 queued', '  ⏸ Nav — queued'])
    expect(wakeMessage([run({ status: 'completed', endedAt: NOW })], NOW)).not.toContain('Codex')
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

  test('slot squares fill up to the cap, never past it', () => {
    expect(slotSquares(2, 5)).toBe('◼◼◻◻◻')
    expect(slotSquares(0, 3)).toBe('◻◻◻')
    expect(slotSquares(7, 5)).toBe('◼◼◼◼◼')
  })
})

describe('live worker card', () => {
  const feed = (over: Record<string, unknown> = {}) => ({
    id: 'th_7', status: 'running',
    activity: [
      { at_ms: NOW - 60_000, kind: 'file_read', status: 'completed', summary: 'README.md', tool: 'Read', detail: '' },
      { at_ms: NOW - 45_000, kind: 'command', status: 'completed', summary: 'python -m py_compile scripts/*.py', tool: 'Bash', detail: 'ok' },
      { at_ms: NOW - 40_000, kind: 'file_edit', status: 'completed', summary: 'scripts/gate.py (+12 -3)', tool: 'Edit', detail: '' },
      { at_ms: NOW - 20_000, kind: 'file_write', status: 'error', summary: 'mod/zcrew/hooks/model.ts (210 lines) !! disk full', tool: 'Write', detail: 'disk full' },
      { at_ms: NOW - 10_000, kind: 'command', status: 'running', summary: 'python -m unittest discover', tool: 'Bash', detail: '' },
    ],
    counts: { files_edited: 3, commands: 5, errors: 1 },
    last_at_ms: NOW - 10_000,
    ...over,
  })

  test('liveOf takes the newest file and command, notes stripped, counts whole-session', () => {
    expect(liveOf(feed(), NOW)).toEqual({
      editing: 'mod/zcrew/hooks/model.ts', // the write wins over the older edit, its size note and error gone
      command: { text: 'python -m unittest discover', state: 'running' },
      lastAt: NOW - 10_000,
      files: 3, commands: 5, errors: 1,
    })
  })

  test('command outcomes and a feed without counts or last_at_ms', () => {
    const only = (item: Record<string, unknown>) => feed({ activity: [item] })
    expect(liveOf(only({ at_ms: NOW, kind: 'command', status: 'error', summary: 'npm test', tool: 'Bash', detail: 'exit 1' }), NOW)?.command)
      .toEqual({ text: 'npm test', state: 'fail' })
    expect(liveOf(only({ at_ms: NOW, kind: 'command', status: 'completed', summary: 'npm test', tool: 'Bash', detail: 'ok' }), NOW)?.command)
      .toEqual({ text: 'npm test', state: 'ok' })
    const bare = liveOf(feed({ activity: [], counts: undefined, last_at_ms: null }), NOW)
    expect(bare).toEqual({ files: 0, commands: 0, errors: 0 }) // well-formed but empty: no rows to draw
  })

  test('bad input is undefined; a future last_at_ms is clamped to now', () => {
    expect(liveOf('not json', NOW)).toBeUndefined()
    expect(liveOf({ activity: 'no' }, NOW)).toBeUndefined()
    expect(liveOf({ status: 'running' }, NOW)).toBeUndefined() // no activity array
    expect(liveOf(undefined, NOW)).toBeUndefined()
    expect(liveOf(feed({ last_at_ms: NOW + 5_000 }), NOW)?.lastAt).toBe(NOW)
  })

  test('paths trim from the front after a slash, text from the end', () => {
    expect(fitPath('mod/zcrew/hooks/model.ts', 30)).toBe('mod/zcrew/hooks/model.ts')
    expect(fitPath('mod/zcrew/hooks/model.ts', 16)).toBe('…/hooks/model.ts')
    expect(fitPath('verylongname.py', 10)).toBe('…ngname.py') // no slash: the end of the name survives
    expect(fitText('python -m unittest discover', 40)).toBe('python -m unittest discover')
    expect(fitText('python -m unittest discover', 12)).toBe('python -m u…')
  })

  test('sections in draw order, sorted inside, empty ones gone', () => {
    const groups = paneSections([
      run({ runId: 'ok', status: 'completed', verdict: 'accept', endedAt: NOW }),
      run({ runId: 'bad', status: 'failed', endedAt: NOW }),
      run({ runId: 'rev', status: 'completed' }),
      run({ runId: 'q', status: 'queued' }),
      run({ runId: 'go' }),
    ], NOW, BOX)
    expect(groups.map(g => g.key)).toEqual(['active', 'attention', 'review', 'done'])
    expect(groups[0].runs.map(r => r.runId)).toEqual(['q', 'go']) // sorted(): queued ahead of running
    expect(groups[1].runs.map(r => r.runId)).toEqual(['bad'])
    expect(groups[3].runs.map(r => r.runId)).toEqual(['ok'])
    expect(paneSections([run({ runId: 'go' })], NOW, BOX).map(g => g.key)).toEqual(['active'])
  })

  test('ages and totals in plain words', () => {
    expect(agoText(12_000, 'th')).toBe('12 วิที่แล้ว')
    expect(agoText(3 * 60_000, 'th')).toBe('3 นาทีที่แล้ว')
    expect(agoText(12_000, 'en')).toBe('12s ago')
    expect(agoText(3 * 60_000, 'en')).toBe('3 min ago')
    expect(liveTotals({ files: 3, commands: 5 }, 'th')).toBe(' · แก้ 3 ไฟล์ · รัน 5 ครั้ง')
    expect(liveTotals({ files: 3, commands: 5 }, 'en')).toBe(' · 3 files · 5 runs')
    expect(liveTotals({ files: 0, commands: 0 }, 'th')).toBe('')
  })
})

describe('usage segment', () => {
  const RESETS_AT = new Date(2026, 0, 2, 14, 30).toISOString() // 14:30 local

  test('context and windows lead with the number, unknown kinds ignored', () => {
    expect(usageText({
      context: { percent: 42 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 23 },
        { kind: 'seven_day', percentUsed: 61 },
        { kind: 'spend_limit', percentUsed: 90 },
      ],
    }, NOW, 'en')).toBe('◆ 42% ctx   ◆ 23% 5h   ◆ 61% 7d')
  })

  test('missing pieces are omitted, nothing known is undefined', () => {
    expect(usageText({ context: { percent: 7 }, rateLimits: [] }, NOW, 'en')).toBe('◆ 7% ctx') // off-subscription
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 10 }] }, NOW, 'en')).toBe('◆ 10% 5h')
    expect(usageText({ context: { window: 200000 } }, NOW, 'en')).toBeUndefined() // no fill yet
    expect(usageText({}, NOW, 'en')).toBeUndefined()
    expect(usageText(undefined, NOW, 'en')).toBeUndefined()
  })

  test('a window past 80% says when it resets', () => {
    expect(usageText({ rateLimits: [{ kind: 'seven_day', percentUsed: 85, resetsAt: RESETS_AT }] }, NOW, 'en'))
      .toBe('◆ 85% 7d ⟳14:30')
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 92, resetsAt: RESETS_AT }] }, NOW, 'th'))
      .toBe('◆ 92% 5h ⟳14:30')
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 79, resetsAt: RESETS_AT }] }, NOW, 'en'))
      .toBe('◆ 79% 5h') // below 80%: no reset time even with resetsAt
    expect(usageText({ rateLimits: [{ kind: 'five_hour', percentUsed: 85 }] }, NOW, 'en')).toBe('◆ 85% 5h') // no resetsAt
  })

  test('chrome joins the parts that exist', () => {
    expect(chromeText('◆ 42% ctx   ◆ 23% 5h', '▶ 1 ทำอยู่')).toBe('◆ 42% ctx   ◆ 23% 5h  ┃  ▶ 1 ทำอยู่')
    expect(chromeText('◆ 42% ctx', undefined)).toBe('◆ 42% ctx')
    expect(chromeText(undefined, '▶ 1 working')).toBe('▶ 1 working')
    expect(chromeText(undefined, undefined)).toBeUndefined()
  })
})

describe('HUD status line', () => {
  const RESETS_AT = new Date(2026, 0, 2, 18, 10).toISOString()

  test('each stage count reads as glyph, number, word', () => {
    const one = (over: Partial<RunView>) => [run(over)]
    expect(statusText(one({}), NOW, BOX, undefined, 'th')).toBe('▶ 1 ทำอยู่')
    expect(statusText(one({}), NOW, BOX, undefined, 'en')).toBe('▶ 1 working')
    expect(statusText(one({ status: 'queued' }), NOW, BOX, undefined, 'th')).toBe('⏸ 1 รอคิว')
    expect(statusText(one({ status: 'queued' }), NOW, BOX, undefined, 'en')).toBe('⏸ 1 queued')
    expect(statusText(one({ status: 'completed' }), NOW, BOX, undefined, 'th')).toBe('◎ 1 รอตรวจ')
    expect(statusText(one({ status: 'completed' }), NOW, BOX, undefined, 'en')).toBe('◎ 1 to review')
    expect(statusText(one({ stalled: true, lastChangeAt: NOW - 35 * 60_000 }), NOW, BOX, undefined, 'th')).toBe('✗ 1 ต้องดู')
    expect(statusText(one({ stalled: true, lastChangeAt: NOW - 35 * 60_000 }), NOW, BOX, undefined, 'en')).toBe('✗ 1 needs you')
    expect(statusText(one({ status: 'completed', verdict: 'accept', endedAt: NOW }), NOW, BOX, undefined, 'th')).toBe('✓ 1 ผ่าน')
    expect(statusText(one({ status: 'completed', verdict: 'accept', endedAt: NOW }), NOW, BOX, undefined, 'en')).toBe('✓ 1 accepted')
  })

  test('the cap reads as slot squares, warning on backoff', () => {
    expect(statusText([run({})], NOW, BOX, { used: 1, cap: 5, configured: 5, backoff: false }, 'th'))
      .toBe('▶ 1 ทำอยู่  ◼◻◻◻◻ 1/5')
    expect(statusText([run({})], NOW, BOX, { used: 1, cap: 2, configured: 5, backoff: true }, 'en'))
      .toBe('▶ 1 working  ◼◻ 1/2 ⚠1302')
  })

  test('the whole line matches the chosen design', () => {
    const usage = {
      context: { percent: 81 },
      rateLimits: [
        { kind: 'five_hour' as const, percentUsed: 85, resetsAt: new Date(2026, 0, 2, 18, 10).toISOString() },
        { kind: 'seven_day' as const, percentUsed: 86, resetsAt: new Date(2026, 0, 2, 18, 0).toISOString() },
      ],
    }
    const runs = [
      run({ runId: 'a' }),
      run({ runId: 'b', status: 'completed', verdict: 'accept', endedAt: NOW }),
      run({ runId: 'c', status: 'completed', verdict: 'accept', endedAt: NOW }),
    ]
    const cap = { used: 1, cap: 5, configured: 5, backoff: false }
    expect(chromeText(usageText(usage, NOW, 'th'), statusText(runs, NOW, BOX, cap, 'th')))
      .toBe('◆ 81% ctx   ◆ 85% 5h ⟳18:10   ◆ 86% 7d ⟳18:00  ┃  ▶ 1 ทำอยู่  ✓ 2 ผ่าน  ◼◻◻◻◻ 1/5')
  })

  test('no emoji in the status line or the band', () => {
    const EMOJI = /🔧|👀|❌|✅|⏳|🎉/
    const runs = [
      run({ runId: 'w' }),
      run({ runId: 'q', status: 'queued', queue: 'slot' }),
      run({ runId: 'r', status: 'completed' }),
      run({ runId: 's', title: 'Migrate', stalled: true, lastChangeAt: NOW - 35 * 60_000, endedAt: NOW }),
      run({ runId: 'ok', status: 'completed', verdict: 'accept', endedAt: NOW }),
    ]
    const cap = { used: 2, cap: 5, configured: 5, backoff: true }
    const status = chromeText(
      usageText({ context: { percent: 81 }, rateLimits: [{ kind: 'five_hour', percentUsed: 85, resetsAt: RESETS_AT }] }, NOW, 'th'),
      statusText(runs, NOW, BOX, cap, 'th'),
    )
    expect(status).not.toMatch(EMOJI)
    for (const line of bandLines(runs, NOW, BOX, 'th', 3, cap)) expect(line).not.toMatch(EMOJI)
  })
})
