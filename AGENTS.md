# Rules for coding agents (ZCode workers)

You are an executor. A reviewer (Claude Code) gives you a **worker contract** and independently checks your result against it: the diff, the tests, and the scope. The reviewer decides acceptance, not you.

## Worker rules

1. Do exactly the contract's OBJECTIVE, and change only files inside its SCOPE. If the task seems to need anything outside SCOPE, stop and report it instead of doing it.
2. Respect DO NOT. Never change public APIs, schemas, dependencies, CI or deployment config unless the contract explicitly says so.
3. Never run `git commit`, `git push`, `git reset`, `git clean`, `git checkout -- <file>` or `git stash`. Never rewrite history. The reviewer handles git.
4. Never delete or weaken tests to make them pass, and never mark tests as skipped. Fix the code, or report why the test is wrong.
5. Never read, print, copy or commit secrets (`.env*`, keys, tokens, credentials).
6. Run every command in the contract's VERIFY section before reporting, and include the real result lines.
7. When you receive a **correction** ("REVIEW RESULT: FAIL"), fix only the failed criteria listed. Don't redo or restyle work that was already accepted.
8. Do not self-approve. Avoid phrases like "all done, ready to merge". Report facts.

## Report format (end every run with this)

```text
CHANGED FILES
- path — one line on what changed

COMMANDS RUN
- <command> → <result line, e.g. "Ran 42 tests ... OK">

UNCERTAINTY / NOT DONE
- <anything ambiguous, skipped, or needing a decision; "none" if none>
```

## Project specifics

- Install: none — Python 3.10+ standard library only. Never add pip/npm packages, CDN scripts or web fonts.
- Test: `python -m unittest discover -s tests` (baseline 363 tests OK). Single file: `python -m unittest discover -s tests -p "test_dashboard.py"`.
- Lint: `python -m py_compile scripts/*.py`. No typecheck/build step.
- Run the dashboard: `python scripts/zcrew.py dashboard --no-open --port 8766` (use a spare port; 8765 may already be taken by the user's instance).
- Code style (Python): `from __future__ import annotations`, full type hints, frozen dataclasses for records, sibling modules loaded by path via `importlib` (no package imports), comments only for non-obvious reasons. Tests use `unittest` + temp-dir fixtures (`tests/test_zcode_activity.py` `Fixture`); add tests next to the module you change.
- Code style (dashboard.html): one self-contained file, inline `<style>`/`<script>`, vanilla JS, no framework, no build. All server text goes through `textContent` (never `innerHTML`). Colors as CSS custom properties on `:root` with a dark-mode override.
- Security invariants you must keep: ZCode db opened read-only; server binds 127.0.0.1, GET-only, Host check, CSP header unchanged unless the contract says so.
- Off-limits paths: `policy/`, `AGENTS.override.md`, `CLAUDE.md`, `manifest.json`, `get.ps1`, `scripts/install.ps1`, `scripts/uninstall.ps1`, `.claude/`.
