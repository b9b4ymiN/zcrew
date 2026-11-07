/**
 * What a running worker is really doing, from `zcrew show <threadId> --json
 * --last N`. Every field is optional except the whole-session counts: a feed
 * without file or command activity still carries its totals.
 */
export type Live = {
  /** The file the worker last edited or wrote, size notes stripped. */
  editing?: string
  /** The last command it ran, and how that went. */
  command?: { text: string; state: 'running' | 'ok' | 'fail' }
  /** When the worker last acted, epoch ms. */
  lastAt?: number
  /** Whole-session totals (never limited by --last). */
  files: number
  commands: number
  errors: number
}

/** One worker run as the mod sees it. Times are epoch milliseconds. */
export type RunView = {
  runId: string
  /** Which worker owns the run: missing means ZCode (seen via MCP); Codex runs arrive from the CLI feed. */
  worker?: 'zcode' | 'codex'
  threadId?: string
  title: string
  cwd?: string
  status: string
  phase?: string
  tools: string[]
  startedAt: number
  runningSince?: number
  /** The run's own time box (its contract's TIMEBOX, from agent-start), in ms; missing means the config box applies. */
  boxMs?: number
  endedAt?: number
  round: number
  gate?: { mechanical: string; summary: string }
  /** Why the run failed: the bridge's `failure` (kind, message, advice) or repeated agent-observe errors. */
  failure?: { kind: string; message: string; advice: string; resetsAt?: string }
  verdict?: string
  /** Codex runs: the worker's report file, named in the wake message. */
  report?: string
  /** Codex runs: whatever the feed flagged about the run. */
  warning?: string
  woke?: boolean
  /** Why a queued run waits: the machine's worker cap, or the folder another commander holds. */
  queue?: 'slot' | 'folder'
  /** Last observed engine revision, and when it last moved (stall detection). */
  lastRevision?: number
  lastChangeAt?: number
  /** A running run silent for STALL_MS: flagged, and Claude is woken once. */
  stalled?: boolean
  /** Background jobs still alive after the worker's turn ended (status `background`). */
  background?: { taskId: string; description: string }[]
  backgroundSince?: number
  /** Live activity of a running worker, refreshed by the tick from `zcrew show`. */
  live?: Live
}

declare module 'claude-code' {
  interface PluginState {
    zcrew: { runs: Record<string, RunView> }
  }
}
