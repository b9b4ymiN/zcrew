/** One worker run as the mod sees it. Times are epoch milliseconds. */
export type RunView = {
  runId: string
  threadId?: string
  title: string
  cwd?: string
  status: string
  phase?: string
  tools: string[]
  startedAt: number
  runningSince?: number
  endedAt?: number
  round: number
  gate?: { mechanical: string; summary: string }
  verdict?: string
  woke?: boolean
}

declare module 'claude-code' {
  interface PluginState {
    zcrew: { runs: Record<string, RunView> }
  }
}
