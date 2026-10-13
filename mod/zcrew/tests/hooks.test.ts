import { expect, mock, test } from 'claude-code/testing'

const START = 'mcp__zcode_executor__agent-start'
const WAIT = 'mcp__zcode_executor__agent-wait'
const CONTRACT = 'OBJECTIVE\nAdd the hero section.\n\nSCOPE\n- `src/hero/**`\n'

test('agent-start is remembered and Claude is told not to poll', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"queued","revision":1}' }))
  const ran = await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  expect(ran.context?.join('\n')).toContain('zcrew mod is watching run_1')
})

test('agent-wait answers at once while the run works, then lets waits block', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  const seen: unknown[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('tool.call', { tool: WAIT }, ($, e) => {
    seen.push((e as unknown as { timeoutMs?: number }).timeoutMs)
    return { result: {}, text: '{"runId":"run_1","status":"running","revision":2}' }
  })
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  for (let i = 0; i < 3; i++) await $.tool.call({ tool: WAIT, runId: 'run_1', timeoutMs: 60000 } as never)
  expect(seen).toEqual([0, 0, 60000]) // two quick answers, then the model's own wait
})

test('a finished run passes through untouched', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('tool.call', { tool: WAIT }, () => ({ result: {}, text: '{"runId":"run_1","status":"completed","revision":9}' }))
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  const ran = await $.tool.call({ tool: WAIT, runId: 'run_1', timeoutMs: 60000 } as never)
  expect(ran.context ?? []).toEqual([])
})

test('hands-free off leaves waits alone', { options: { handsFree: false } }, async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  const seen: unknown[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('tool.call', { tool: WAIT }, ($, e) => {
    seen.push((e as unknown as { timeoutMs?: number }).timeoutMs)
    return { result: {}, text: '{"runId":"run_1","status":"running","revision":2}' }
  })
  const started = await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  expect(started.context ?? []).toEqual([])
  await $.tool.call({ tool: WAIT, runId: 'run_1', timeoutMs: 60000 } as never)
  expect(seen).toEqual([60000])
})

test('the watcher observes, gates, then wakes Claude once', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  let status = 'running'
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status, revision: 3 }) }], isError: false } }) as never)
  on('process.run', () => ({ value: { exitCode: 1, stdout: JSON.stringify({ mechanical: 'FAIL', checks: [{ name: 'verify', status: 'FAIL', detail: 'npm test -> exit 1' }] }), stderr: '' } }) as never)
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', () => ({ value: { isSent: true, channel: 'terminal_bell' } }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000)
  status = 'completed'
  await clock.advance(15_000)
  await clock.advance(25_000)
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('run_1 "Add the hero section.": completed')
  expect(prompts[0]).toContain('gate MECHANICAL FAIL (verify: npm test -> exit 1)')
  await clock.advance(60_000)
  expect(prompts.length).toBe(1) // woken once
})

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 80, scroll: { top: 0 }, view: {} } } as const
const PANE = { component: 'Pane', requestId: 'zcrew', props: { title: 'zcrew', isFocused: false, bodyColumns: 36 } } as const

test('band and pane draw on every surface once a run starts', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  for (const op of ['ui.status', 'ui.open'] as const) on(op, () => ({ value: undefined }) as never)
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const band = await $.ui.mount({ plugin: 'zcrew', surface, ...BAND } as never)
    expect(await band.find({ type: 'Text', text: /zcrew {2}/ } as never)).toBeDefined()
    const working = await band.find({ type: 'Text', text: /🔧 1 กำลังทำ/ } as never)
    expect(working).toBeDefined()
    expect(working?.props.color).toBe('claude') // the summary is drawn in colored segments
    await band.unmount()
    const pane = await $.ui.mount({ plugin: 'zcrew', surface, ...PANE } as never)
    expect(await pane.find({ type: 'Text', text: /Add the hero section\./ } as never)).toBeDefined()
    await pane.unmount()
  }
})

test('band stays out of the way with no runs', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('ui.render', { component: 'AbovePrompt' }, () => ({ type: 'engine', ref: 0 }) as never)
  const band = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...BAND } as never)
  expect(await band.find({ type: 'Text', text: /zcrew/ } as never)).toBeUndefined()
  await band.unmount()
})

test('a run held open by a background job wakes Claude after the grace, once', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({
    runId: 'run_1', status: 'background', revision: 4,
    backgroundTasks: [{ taskId: 'job_9', status: 'running', description: 'npm run dev' }],
  }) }], isError: false } }) as never)
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ mechanical: 'PASS', checks: [] }), stderr: '' } }) as never)
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', () => ({ value: { isSent: true, channel: 'terminal_bell' } }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000)
  await clock.advance(30_000)
  expect(prompts.length).toBe(0) // still inside the grace
  await clock.advance(90_000)
  await clock.advance(25_000)
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('job_9 (npm run dev)')
  await clock.advance(120_000)
  expect(prompts.length).toBe(1)
})

test('session.measure paints usage on the status line, runs or not', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  const statuses: (string | undefined)[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('session.measure', () => ({ changed: ['context'] }) as never)
  on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined } as never })
  const measured = await $.session.measure({
    context: { window: 200_000, percent: 42 },
    rateLimits: [{ kind: 'five_hour', percentUsed: 23 }, { kind: 'seven_day', percentUsed: 61 }],
    changed: ['context'],
  } as never)
  expect(measured).toEqual({ changed: ['context'] }) // next(e) passed through untouched
  expect(statuses[statuses.length - 1]).toBe('ctx ▰▰▱▱▱ 42% │ 5h ▰▱▱▱▱ 23% │ 7d ▰▰▰▱▱ 61%') // no runs: usage alone
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  expect(statuses[statuses.length - 1]).toBe('ctx ▰▰▱▱▱ 42% │ 5h ▰▱▱▱▱ 23% │ 7d ▰▰▰▱▱ 61% │ zcrew 🔧1')
})

test('session.start paints usage read from the engine', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const statuses: (string | undefined)[] = []
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200_000, percent: 15 }, rateLimits: [] } }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined } as never })
  await $.session.start({ cwd: '/work' } as never)
  expect(statuses[statuses.length - 1]).toBe('ctx ▰▱▱▱▱ 15%')
})

test('showUsage off leaves the status line as it was', { options: { showUsage: false } }, async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  const statuses: (string | undefined)[] = []
  on('session.measure', () => ({ changed: ['context'] }) as never)
  on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined } as never })
  await $.session.measure({
    context: { window: 200_000, percent: 42 },
    rateLimits: [{ kind: 'five_hour', percentUsed: 23 }],
    changed: ['context'],
  } as never)
  expect(statuses[statuses.length - 1]).toBeUndefined()
})

// process.run serves both the gate and `zcrew capacity --json`; dispatch on argv.
const CAP_JSON = (backoff: boolean) => JSON.stringify({ cap: { cap: backoff ? 2 : 5, configured: 5, backoff }, slots_in_use: 2 })

test('a run silent for 30 min is flagged stalled and wakes Claude once, without the gate', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  const gateCalls: string[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status: 'running', revision: 3 }) }], isError: false } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    gateCalls.push(argv.join(' '))
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', () => ({ value: { isSent: true, channel: 'terminal_bell' } }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  for (let i = 0; i < 125; i++) await clock.advance(15_000) // 31+ min of ticks, revision never moves
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('run_1 "Add the hero section.": STALLED, no worker activity for 30 min')
  expect(gateCalls).toEqual([]) // the gate is for finished runs; a stalled run has not finished
  for (let i = 0; i < 4; i++) await clock.advance(15_000)
  expect(prompts.length).toBe(1) // woken once
})

test('a stalled run that moves again is un-stalled, and its completion wakes Claude again', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  let revision = 3
  let status = 'running'
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status, revision }) }], isError: false } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    return { value: { exitCode: 0, stdout: JSON.stringify({ mechanical: 'PASS', checks: [] }), stderr: '' } } as never
  })
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', () => ({ value: { isSent: true, channel: 'terminal_bell' } }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  for (let i = 0; i < 125; i++) await clock.advance(15_000) // 31+ min of silent ticks: STALLED, woken once
  await clock.advance(25_000) // the debounce fires
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('STALLED, no worker activity for 30 min')
  revision = 4 // the worker moves again: no longer stalled, its end must wake Claude again
  await clock.advance(15_000)
  status = 'completed'
  revision = 5
  await clock.advance(15_000)
  await clock.advance(25_000)
  expect(prompts.length).toBe(2)
  expect(prompts[1]).toContain('run_1 "Add the hero section.": completed')
  await clock.advance(60_000)
  expect(prompts.length).toBe(2) // the completion woke Claude once, not the stall again
})

test('a 1302 backoff start toasts once', { options: { language: 'en' } }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const toasts: string[] = []
  let notified = 0
  let backoff = false
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status: 'running', revision: 2 }) }], isError: false } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (!argv.includes('capacity')) return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
    return { value: { exitCode: 0, stdout: CAP_JSON(backoff), stderr: '' } } as never
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('ui.toast', ($, e) => { toasts.push((e as unknown as { text: string }).text); return { value: undefined } as never })
  on('ui.notify', () => { notified++; return { value: { isSent: true, channel: 'terminal_bell' } } as never })
  for (const op of ['ui.status', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000) // first reading: no backoff, no toast
  expect(toasts).toEqual([])
  backoff = true
  await clock.advance(15_000) // backoff starts: toast once
  await clock.advance(15_000) // still backing off: no repeat
  expect(toasts).toEqual(['zcrew: 1302 — cap lowered to 2 for 10 min'])
  expect(notified).toBe(1)
})

test('capacity failing leaves the band drawn without the cap', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status: 'running', revision: 2 }) }], isError: false } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('capacity')) return { value: { exitCode: 1, stdout: 'zcrew: unknown command', stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000)
  const band = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...BAND } as never)
  expect(await band.find({ type: 'Text', text: /🔧 1 กำลังทำ/ } as never)).toBeDefined()
  expect(await band.find({ type: 'Text', text: /ช่าง|workers/ } as never)).toBeUndefined()
  await band.unmount()
})

test('a queued run waiting for a worker slot shows the queued row', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"queued","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: JSON.stringify({
    runId: 'run_1', status: 'queued', revision: 1,
    resourceLease: { acquired: false, blockers: [{ key: 'zcrew:worker-slot:2' }] },
  }) }], isError: false } }) as never)
  on('process.run', () => ({ value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000)
  const band = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...BAND } as never)
  expect(await band.find({ type: 'Text', text: /⏳ 1 รอคิว/ } as never)).toBeDefined()
  expect(await band.find({ type: 'Text', text: /เต็มเพดาน/ } as never)).toBeDefined()
  await band.unmount()
})
