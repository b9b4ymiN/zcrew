import { describe, expect, test } from 'claude-code/testing'

import type { RunView } from '../types'
import {
  BACKGROUND_GRACE_MS, activityOf, bandLines, bandVisible, boxFromConfig, detailOf, gateSummary, liveJobs,
  nudgeMessage, parseSnapshot, planOf, planSuffix, stageOf, statusText, titleOf, turnOver, wakeMessage,
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
    expect(boxFromConfig(['{"wrapUpSeconds": 0}'])).toBe(1_200_000)
    expect(boxFromConfig([])).toBe(1_200_000)
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

describe('plan', () => {
  const plan = planOf({ ready: ['T4', 'T5', 'T6'], accepted: 3, total: 7, max_workers: 3 })

  test('reads zcrew plan --json', () => {
    expect(plan).toEqual({ ready: ['T4', 'T5', 'T6'], accepted: 3, total: 7, maxWorkers: 3 })
    expect(planOf({})).toBeUndefined()
    expect(planSuffix(plan, 'th')).toBe(' · แผน 3/7 · พร้อมเริ่ม 3')
    expect(planSuffix(planOf({ ready: [], accepted: 7, total: 7 }), 'en')).toBe(' · plan 7/7')
  })

  test('nudge names only as many tasks as there are free workers', () => {
    const text = nudgeMessage(plan, 1)
    expect(text).toContain('ready to start: T4, T5, T6. Workers busy: 1 of 3.')
    expect(text).toContain('Start T4, T5 now')
    expect(nudgeMessage(plan, 3)).toBeUndefined()
    expect(nudgeMessage(planOf({ ready: [], accepted: 0, total: 2 }), 0)).toBeUndefined()
    expect(nudgeMessage(undefined, 0)).toBeUndefined()
  })

  test('the band carries the plan', () => {
    expect(bandLines([run({})], NOW, BOX, 'en', 3, plan)[0]).toBe('zcrew  🔧 1 working · plan 3/7 · ready 3')
  })
})
