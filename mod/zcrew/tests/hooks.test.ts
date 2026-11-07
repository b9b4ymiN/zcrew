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
    const working = await band.find({ type: 'Text', text: /▶ 1 ทำอยู่/ } as never)
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
  expect(statuses[statuses.length - 1]).toBe('◆ 42% ctx   ◆ 23% 5h   ◆ 61% 7d') // no runs: usage alone
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  expect(statuses[statuses.length - 1]).toBe('◆ 42% ctx   ◆ 23% 5h   ◆ 61% 7d  ┃  ▶ 1 ทำอยู่')
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
  expect(statuses[statuses.length - 1]).toBe('◆ 15% ctx')
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

// process.run serves the gate, `zcrew capacity --json` and `zcrew cx status`; dispatch on argv.
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
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: '{"runs":[]}', stderr: '' } } as never // no Codex work in this test
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
  expect(await band.find({ type: 'Text', text: /▶ 1 ทำอยู่/ } as never)).toBeDefined()
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
  expect(await band.find({ type: 'Text', text: /⏸ 1 รอคิว/ } as never)).toBeDefined()
  expect(await band.find({ type: 'Text', text: /เต็มเพดาน/ } as never)).toBeDefined()
  await band.unmount()
})

test('a run the bridge no longer knows fails as run_lost and wakes Claude', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  const notes: string[] = []
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: 'run not found: run_e1f80bc58dcd4b3caf2f2509f17b7bc7' }], isError: true } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: '{"runs":[]}', stderr: '' } } as never
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', ($, e) => { notes.push((e as unknown as { text: string }).text); return { value: { isSent: true, channel: 'terminal_bell' } } as never })
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await $.tool.call({ tool: START, prompt: CONTRACT, cwd: '/work' } as never)
  await clock.advance(15_000) // the first observe already knows: failed at once, not after three errors
  await clock.advance(25_000) // the wake debounce fires
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('run_1 "Add the hero section.": failed after 0 min: run not found: run_e1f80bc58dcd4b3caf2f2509f17b7bc7; gate not run')
  expect(prompts[0]).toContain('  The bridge no longer knows this run (bridge restart); check the worktree, then start a new run.')
  expect(notes).toEqual(['❌ Add the hero section. — run หาย (bridge restart)'])
})

test('transient observe errors do not fail a run; three in a row do', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  let error: string | undefined = 'MCP error: connection closed'
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1}' }))
  on('mcp.call', () => (error === undefined
    ? { value: { content: [{ type: 'text', text: JSON.stringify({ runId: 'run_1', status: 'running', revision: 3 }) }], isError: false } }
    : { value: { content: [{ type: 'text', text: error }], isError: true } }) as never)
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: '{"runs":[]}', stderr: '' } } as never
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
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
  await clock.advance(15_000) // 1st error: still watching
  await clock.advance(15_000) // 2nd error: still watching
  error = undefined
  await clock.advance(15_000) // a good observe resets the count
  error = 'MCP error: connection closed'
  await clock.advance(15_000) // 1st error again
  await clock.advance(15_000) // 2nd error again
  await clock.advance(25_000)
  expect(prompts.length).toBe(0) // two errors after a success never failed the run
  await clock.advance(15_000) // 3rd in a row: failed
  await clock.advance(25_000)
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('failed after 2 min: MCP error: connection closed')
  expect(prompts[0]).toContain('  agent-observe kept failing; check the run with agent-observe yourself.')
})

test('a run started with its own time box gets a 60-minute bar', async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  on('tool.call', { tool: START }, () => ({ result: {}, text: '{"runId":"run_1","status":"running","revision":1,"gate":{"timebox":{"seconds":3600,"source":"contract"}}}' }))
  for (const op of ['ui.status', 'ui.open'] as const) on(op, () => ({ value: undefined }) as never)
  await $.tool.call({ tool: START, prompt: CONTRACT } as never)
  const pane = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...PANE } as never)
  expect(await pane.find({ type: 'Text', text: /0\/60/ } as never)).toBeDefined() // the config box would say 0/20
  await pane.unmount()
})

// The Codex feed of `zcrew cx status --json --active`, one run shaped as the CLI contract prints it.
const CX_ID = 'codex_20261107101500_ab12'
const CX_FEED = (status: string, over: Record<string, unknown> = {}) => JSON.stringify({ runs: [{
  runId: CX_ID, worker: 'codex', threadId: 'th_9', jobId: 'task-7', cwd: '/work',
  title: 'Fix the gate', status, startedAt: 900_000, roundStartedAt: 950_000,
  endedAt: status === 'running' ? null : 1_030_000, round: 1,
  report: `/work/.cx/${CX_ID}.report.md`, verdict: null, warning: null, ...over,
}] })

test('a codex run appears from the feed unobserved, and its end gates and wakes Claude once', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  const gateCalls: string[] = []
  const observed: string[] = []
  let status = 'running'
  on('mcp.call', ($, e) => {
    observed.push(String((e as unknown as { args?: { runId?: string } }).args?.runId ?? ''))
    return { value: { content: [{ type: 'text', text: '{}' }], isError: false } } as never
  })
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: CX_FEED(status), stderr: '' } } as never
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    gateCalls.push(argv.join(' '))
    return { value: { exitCode: 0, stdout: JSON.stringify({ mechanical: 'PASS', checks: [] }), stderr: '' } } as never
  })
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('ui.notify', () => ({ value: { isSent: true, channel: 'terminal_bell' } }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await clock.advance(15_000) // first tick: the feed carries the running Codex run
  expect(observed).toEqual([]) // never agent-observed: it is not a ZCode run
  const pane = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...PANE } as never)
  expect(await pane.find({ type: 'Text', text: /Codex · Fix the gate/ } as never)).toBeDefined()
  await pane.unmount()
  status = 'completed'
  await clock.advance(15_000)
  await clock.advance(25_000) // the wake debounce fires
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain(`${CX_ID} "Fix the gate" [Codex]: completed after 1 min; gate MECHANICAL PASS.`)
  expect(prompts[0]).toContain(`Its worker report (read it instead of agent-wait): /work/.cx/${CX_ID}.report.md`)
  expect(gateCalls.length).toBe(1) // `zcrew gate <runId> --json` ran for the Codex run
  expect(gateCalls[0]).toContain(`gate ${CX_ID} --json`)
  await clock.advance(60_000)
  expect(prompts.length).toBe(1) // woken once
})

test('a codex correction replaces its base card', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: CX_FEED('queued', { runId: `${CX_ID}.r2`, round: 2, roundStartedAt: 1_005_000 }), stderr: '' } } as never
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await clock.advance(15_000)
  const pane = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...PANE } as never)
  expect(await pane.find({ type: 'Text', text: /Codex · Fix the gate · แก้รอบ 1/ } as never)).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /รวม 1 งาน/ } as never)).toBeDefined() // the base card is gone, not stacked
  await pane.unmount()
})

test('a codex correction learns its verdict from the base record file', { options: { language: 'en' }, timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const reads: string[] = []
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode: 0, stdout: CX_FEED('completed', { runId: `${CX_ID}.r2`, round: 2, roundStartedAt: 1_005_000 }), stderr: '' } } as never
    if (argv.includes('capacity')) return { value: { exitCode: 0, stdout: CAP_JSON(false), stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('fs.read', ($, e) => {
    const path = String((e as unknown as { path: string }).path).replace(/\\/g, '/').toLowerCase()
    reads.push(path)
    if (path.endsWith(`/.zcode-commander/gate/${CX_ID.toLowerCase()}.json`)) {
      return { value: JSON.stringify({ verdicts: [{ verdict: 'accept' }] }) } as never // `zcrew gate <base> --accept` wrote the base record
    }
    return { deny: 'absent' } as never // among others, the nonexistent <base>.r2.json
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => ({ text: e.text }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await clock.advance(15_000) // the feed ends the correction; the verdict loop reads the record file
  await clock.advance(25_000) // flush the wake debounce
  expect(reads.some(p => p.endsWith(`/.zcode-commander/gate/${CX_ID.toLowerCase()}.json`))).toBe(true)
  expect(reads.some(p => p.endsWith('.r2.json'))).toBe(false) // the correction has no record of its own
  const pane = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...PANE } as never)
  expect(await pane.find({ type: 'Text', text: /✓ Codex · Fix the gate/ } as never)).toBeDefined()
  expect(await pane.find({ type: 'Text', text: 'accepted' } as never)).toBeDefined() // the verdict draws, not "waiting for Claude"
  await pane.unmount()
})

test('the codex feed is polled in the session folder', { timeoutMs: 20000 }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const calls: string[][] = []
  on('process.run', ($, e) => {
    const argv = [...(e as unknown as { argv: readonly string[] }).argv]
    if (argv.includes('cx')) calls.push(argv)
    return { value: { exitCode: 0, stdout: '{"runs":[]}', stderr: '' } } as never
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await clock.advance(15_000) // eager window after session start: the first tick polls the feed
  expect(calls.length >= 1).toBe(true)
  const at = calls[0].indexOf('status')
  expect(at !== -1).toBe(true)
  expect(calls[0][at + 1]).toBe('/work') // the positional DIR, not the mod process's own working directory
})

test('a failing or garbage codex feed changes nothing and throws nothing', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  let exitCode = 1
  let stdout = 'zcrew: cx: nothing here'
  on('process.run', ($, e) => {
    const argv = (e as unknown as { argv: readonly string[] }).argv
    if (argv.includes('cx')) return { value: { exitCode, stdout, stderr: '' } } as never
    return { value: { exitCode: 0, stdout: '{}', stderr: '' } } as never
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('ui.render', { component: 'AbovePrompt' }, () => ({ type: 'engine', ref: 0 }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close', 'ui.notify'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  await clock.advance(15_000) // non-zero exit: no data this tick
  exitCode = 0
  stdout = '{"runs": [garbage'
  await clock.advance(15_000) // unparsable output: same
  stdout = JSON.stringify({ runs: 'no' })
  await clock.advance(15_000) // a feed without a runs array: same
  const band = await $.ui.mount({ plugin: 'zcrew', surface: 'terminal', ...BAND } as never)
  expect(await band.find({ type: 'Text', text: /zcrew/ } as never)).toBeUndefined() // no card appeared
  await band.unmount()
})
