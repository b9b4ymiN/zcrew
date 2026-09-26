# Project guide for Claude Code (commander)

<!-- zcrew template: replace every <...> placeholder, delete what does not apply. -->

## What this project is

<One or two sentences: what it does, who uses it.>

## Architecture

- <Main modules/layers and where they live, e.g. `src/api` HTTP handlers, `src/core` business rules>
- <Key boundaries that must hold, e.g. "core never imports from api">
- <External systems: databases, queues, third-party APIs>

## Stack and conventions

- Language/runtime: <e.g. TypeScript 5 on Node 22 / .NET 8 / Python 3.12>
- Package manager: <npm / pnpm / dotnet / uv>
- Style: <formatter + linter, naming rules, anything a reviewer should enforce>

## VERIFY — commands the reviewer runs on every worker result

Put these straight into each worker contract's VERIFY section, and run them yourself before you accept any result.

```text
<install>      e.g. npm ci
<test>         e.g. npm test
<typecheck>    e.g. npm run typecheck
<lint>         e.g. npm run lint
<build>        e.g. npm run build
```

## Never change without asking the user

- <Public API contracts / DB schema / migrations>
- <Auth, secrets, CI/CD, deployment config>
- <Generated or vendored code: paths>

## Delegation notes

- Workers read `AGENTS.md` in this repo. Keep the rules there, so each contract only has to describe the task.
- Parallel workers use `git worktree`s. They only see files that are committed, so commit `AGENTS.md` first.
- Model, reasoning level, worker count and correction budget come from `.claude/zcode-commander.json` if it exists, and from `~/.zcode-commander/config.json` otherwise.
