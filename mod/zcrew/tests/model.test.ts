import { describe, expect, test } from 'claude-code/testing'

import type { RunView } from '../types'
import {
  activityOf, bandLines, bandVisible, boxFromConfig, gateSummary, parseSnapshot, stageOf, statusText,
  titleOf, wakeMessage,
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
