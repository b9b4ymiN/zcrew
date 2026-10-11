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
    expect(await band.find({ type: 'Text', text: /zcrew {2}🔧 1/ } as never)).toBeDefined()
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

test('when Claude ends a turn with ready plan tasks and free workers, it is reminded once', { timeoutMs: 20000 }, async ($, on) => {
  mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/u' })
  const prompts: string[] = []
  let ready = ['T2', 'T3']
  on('process.run', ($, e) => ({ value: { exitCode: 0, stderr: '', stdout: e.argv.includes('plan')
    ? JSON.stringify({ ready, accepted: 1, total: 3, max_workers: 2 })
    : '{}' } }) as never)
  on('fs.read', () => ({ deny: 'absent' }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: {} }) as never)
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } as never })
  on('turn.complete', ($, e) => ({ text: (e as { text?: string }).text ?? '' }) as never)
  for (const op of ['ui.status', 'ui.toast', 'ui.open', 'ui.close'] as const) on(op, () => ({ value: undefined }) as never)
  await $.session.start({ cwd: '/work' } as never)
  const turn = { answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer', text: 'ok' }
  await $.turn.complete(turn as never)
  await $.turn.complete({ ...turn, turnId: 't2' } as never)
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('Start T2, T3 now')
  ready = ['T3']
  await $.turn.complete({ ...turn, turnId: 't3' } as never)
  expect(prompts.length).toBe(2)
  await $.turn.complete({ ...turn, turnId: 't4', isAborted: true } as never)
  expect(prompts.length).toBe(2)
})
