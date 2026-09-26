# Research basis (2026-09-26)

This kit deliberately reuses prior art instead of reinventing it:

- **KyoMio/zcode-executor** — Claude plans, ZCode implements, git/tests provide acceptance evidence; correction tasks can return to the same ZCode session. Its README currently says Windows is unsupported, so this kit does not install it directly.
- **Deslord319/coder-mcp-bridge** — MCP control plane exposing `agent-config`, `agent-start`, `agent-wait`, `agent-observe`, `agent-control`, `agent-recover`, `agent-branch`, `agent-context`, and `agent-close`; supports native ZCode sessions and Windows UTF-8 stdio. Its current ZCode auto-discovery is macOS/Linux-oriented, hence our launcher.
- **william0wang/zcode-acp** — confirms the current ZCode app-server/session model and documents the standard Windows bundled CLI path `%LOCALAPPDATA%\Programs\ZCode\resources\glm\zcode.cjs`.
- **Anthropic Claude Code MCP docs** — local stdio MCP servers can be registered with `claude mcp add`; user-scope MCP is appropriate for a personal executor used across projects; `CLAUDE.md` user instructions load automatically across sessions.

The Commander policy is original glue: it tells Claude when/how to delegate, how to write worker contracts, how to review evidence, when to reuse a thread, and when to escalate.
