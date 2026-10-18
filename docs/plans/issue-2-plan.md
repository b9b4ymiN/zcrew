# Plan: Issue #2 — TIMEBOX parsing, gate Unicode crash, SCOPE heading, scope diff after merge

Status: **approved by the repository owner** · Planned on 2026-10-18 · Implementer: Sonnet (separate session)
Base: `main` @ `8f20010` (v0.11.0)
Issue: https://github.com/b9b4ymiN/zcrew/issues/2

---

## 0. Rules for the implementer (read first)

1. **Language: English only** for everything that is committed or published: code comments, docstrings, test names and messages, commit messages, PR titles and bodies, CHANGELOG entries, and docs. Anyone must be able to read them. (User-facing runtime strings in the mod that already have Thai/English variants keep that pattern.)
2. Work one PR at a time, in the order of section 2. Branch each PR from the **latest `main`** (after the previous PR is merged). Use the branch names in the table.
3. A PR may only touch the files listed under "Files". If another file seems necessary, **stop and ask the owner**.
4. Never change (see `CLAUDE.md`): the dashboard JSON API, `SessionSummary` / `Activity` fields, the dashboard security posture, dependencies of any kind, `AGENTS.override.md`, `get.ps1`, `scripts/install.ps1`.
   - `policy/COMMANDER.md`: the owner **allowed** editing only the places named in this plan.
   - `manifest.json`: the owner **allowed** it only in PR-R (version 0.11.1).
5. Python standard library only; `from __future__ import annotations`; full type hints; small pure functions; comments only for non-obvious "why"; Conventional Commits.
6. Run VERIFY (section 1) before every push. Results must match the baseline plus the new tests.
7. Never skip or disable a test. Do not edit an existing test unless it asserts a default value that this plan changes; say so in the PR body.
8. Do not create a PR unless the owner asks for it. Pushing a branch is fine.

## 1. VERIFY (run for every PR)

```text
python -m unittest discover -s tests
python -m py_compile scripts/*.py
claude plugin validate mod/zcrew && claude plugin test mod/zcrew      # only when mod/ changed
```

**Baseline (Linux, Python 3.13, 2026-10-18):** `Ran 523 tests … FAILED (failures=1, skipped=1)`.
- The single failure is `test_zcrew.PathStringTests.test_add_expands_variables_for_comparison`. It uses Windows `%VAR%` syntax and fails on Linux only. It is unrelated to this work: **do not touch it**. On Windows it must pass.
- Mod: `42 pass, 0 fail`.

Common DoD for every PR: the test count goes up, the only failure on Linux is the baseline one, and the mod tests pass.

## 2. PR order

| # | Branch | Topic | Depends on |
|---|---|---|---|
| A1 | `fix/timebox-parse` | Inline TIMEBOX, echo the effective value, warnings, wrap-up clock time in the prompt | — |
| C  | `fix/contract-heading-suffix` | `SCOPE (…)` headings, near-miss warning | A1 (same file) |
| A2 | `feat/per-run-timebox` | Mod uses a per-run time box; new defaults 60 / 50 minutes | A1 |
| B  | `fix/cli-utf8-output` | `UnicodeEncodeError` on Windows consoles | — |
| D  | `fix/gate-moved-head` | `scope FAIL` after HEAD moved | — |
| R  | `chore/release-0.11.1` | CHANGELOG + manifest 0.11.1 | all |

Note: A2 changes defaults, which by semver would justify 0.12.0. The owner chose 0.11.1; use 0.11.1.

---

## PR-A1 — TIMEBOX anywhere on a line, and report what was applied

**Problem:** `_TIMEBOX_RE` (`scripts/bridge_compat.py:581`) is anchored `^…$`. A first line such as `TASK X — … TIMEBOX: 60m` never matches, so the run silently falls back to `wrapUpSeconds` (1200 s) and gets the wrap-up interrupt at ~20 minutes.

**Files:** `scripts/bridge_compat.py`, `tests/test_bridge_compat.py`, `policy/COMMANDER.md` (line 63 only).

**Design:**
1. New regex, case-**sensitive** (no `re.IGNORECASE`):
   `(?<![\w-])TIMEBOX\s*:\s*(\d+)\s*(sec|s|min|m|h)?(?!\w)`
   No unit means minutes (unchanged). The **first** match wins.
2. Keep `parse_timebox(prompt) -> int | None` with the same signature (existing callers and tests). Add:
   ```python
   @dataclass
   class Timebox:
       seconds: int | None   # value applied; None = no time box
       source: str           # "contract" | "config" | "off"
       warnings: list[str]

   def resolve_timebox(args: Mapping[str, Any], env: Mapping[str, str]) -> Timebox: ...
   ```
   `wrap_up_delay(args, env)` returns `resolve_timebox(args, env).seconds`. Every case that matched before must behave exactly as before.
3. Warnings (short English strings starting with `TIMEBOX`):
   - The word `TIMEBOX` (any case, `\bTIMEBOX\b`) appears but nothing parses → `TIMEBOX found but not understood (write e.g. TIMEBOX: 60m); using wrapUpSeconds=<n>`
   - Several matches with different values → `several TIMEBOX values; using the first (<n>m)`
   - Requested value > `timeout - WRAP_UP_GRACE_SECONDS` → `TIMEBOX <x>m is longer than the run timeout <t>m; wrap-up at <d>m instead (raise timeout)`
   - Result < `WRAP_UP_MIN_SECONDS` → `TIMEBOX too short; no time box applied` and `source = "off"`
4. `agent-start` result (inside `patch_run_hooks.start`): for a run snapshot, always add `result["gate"]["timebox"] = {"seconds": <int|None>, "source": <str>}` — even when `gateMode == "off"` (then `gate` holds only `timebox`, no `verify` hint). Append the timebox warnings to `gate.warnings` (create the list if needed). With `ZCODE_COMMANDER_RUN_HOOKS=off` add nothing (unchanged). Do **not** add `gate` to `_DROP_KEYS`.
5. `TIMEBOX_NOTICE` (appended to the prompt) gains the local wall-clock time of the wrap-up:
   `[zcrew] Time box: about %d minutes (wrap-up near %s local time). Run the VERIFY commands before then; if you are told to wrap up, stop and report immediately.` Keep the existing "at most 3 approaches" sentence. Compute the time with `time.strftime("%H:%M", time.localtime(now + delay))` through a small function that accepts an injected clock so tests are deterministic.
6. Gate record: `record_start` also stores `"timebox": {"seconds", "source"}` (a correction round overwrites it with the latest).
7. Docs: update the module docstring (~line 32) to say a `TIMEBOX: 40m` *anywhere in the prompt* overrides the default. In `policy/COMMANDER.md:63` replace the TIMEBOX sentence with: `` `TIMEBOX: 40m` (uppercase, anywhere in the contract) overrides the default time box for one task; the `agent-start` result reports the time box in effect as `gate.timebox`. ``

**Tests to add (`tests/test_bridge_compat.py`):**
- Inline: `TASK X — do it. TIMEBOX: 60m` → 3600.
- Own line still works: `TIMEBOX: 40m` → 2400; also `TIMEBOX:90s`, `TIMEBOX: 2h`, `TIMEBOX: 45 min`, `TIMEBOX: 45` (minutes).
- Not matched: `timebox: 60m`, `MYTIMEBOX: 5m`, `TIMEBOX: soon` → `None` plus the "not understood" warning where the word appears.
- Two different values → first wins plus warning.
- Longer than timeout (`TIMEBOX: 60m`, `timeout: 1800`) → 1680 s plus warning.
- `agent-start` result carries `gate.timebox` for `gateMode` warn **and** off; `source` is correct for contract / config / off.
- The prompt contains `wrap-up near HH:MM` for an injected clock.

**DoD A1:**
- [ ] The issue's repro (`TASK X — do something. TIMEBOX: 60m`, `timeout: 5400`) gives `wrap_up_delay == 3600` and `gate.timebox == {"seconds": 3600, "source": "contract"}`; the test name mentions issue #2.
- [ ] Existing `test_timebox`, `WrapUpDelayTests`, `PatchRunHooksTests` pass **unchanged**.
- [ ] VERIFY passes.

---

## PR-C — Section headings with a parenthetical suffix, plus a near-miss warning

**Problem:** `_SECTION_RE` (`bridge_compat.py:578`) rejects `SCOPE (may change)`, so the run reports `contract is missing: SCOPE`.

**Files:** `scripts/bridge_compat.py`, `tests/test_bridge_compat.py`.

**Design:**
1. Allow an optional parenthetical after the name (its text is discarded):
   `^\s*(?:#{1,6}\s*)?\**(NAMES)\**(?:\s*\([^)\n]*\))?\**\s*(?::\s*\**\s*(.*?))?\s*$`
   Must accept `SCOPE (may change)`, `SCOPE (may change):`, `**SCOPE (files):** \`src/**\``, `## SCOPE (files)` and every previously accepted form.
2. Near-miss detection: a line that starts (after optional `#` / `*`) with an uppercase section name followed by a word boundary but does not match the main regex is remembered as `{name: line}` (first one per name). `parse_contract` returns a new key `"near_misses"` containing **only names that are in `missing`** (avoids noise from ordinary sentences such as `DO NOT touch x`).
3. `record_start` appends to the existing missing-section warning: `; heading not recognised: "<line>" — write "SCOPE:" or "SCOPE (note):"` for each near miss (line clipped to 60 characters).

**Tests:** each form in item 1 is parsed and the following bullets land in the section; `SCOPE - files` produces `missing` + `near_misses`; a stray `DO NOT touch x` while a proper `DO NOT:` exists produces no near miss; every existing `ContractParseTests` test passes unchanged.

**DoD C:**
- [ ] The issue's example (`SCOPE (may change)` + bullets) no longer appears in `missing`, and `scope` contains the backticked paths.
- [ ] The near-miss warning shows the offending heading and the accepted form.
- [ ] VERIFY passes.

---

## PR-A2 — Per-run time box in the mod, and new defaults

**Problem:** the mod uses one `ctx.boxMs` (from config, default 1200 s) for every run, so a `TIMEBOX: 60m` run shows "21/20 min ⏰". Separately, 20 minutes is too short for ZCode workers.

**Files:** `mod/zcrew/types/*.ts` (RunView), `mod/zcrew/hooks/register.tsx`, `mod/zcrew/hooks/model.ts`, `mod/zcrew/tests/*.test.ts`, `scripts/commander.py` (`DEFAULT_CONFIG`, config note), `scripts/bridge_compat.py` (`RUN_HOOKS_DEFAULTS` only; leave `BRIDGE_TIMEOUT_DEFAULT` — it is the upstream default), `config/default-config.json`, `tests/test_commander.py`, `tests/test_bridge_compat.py`, `policy/COMMANDER.md` (defaults block ~lines 33–42 and line 65).

**Design:**
1. New defaults: `timeoutSeconds` 1800 → **3600** and `wrapUpSeconds` 1200 → **3000** in `commander.DEFAULT_CONFIG`, `config/default-config.json`, `bridge_compat.RUN_HOOKS_DEFAULTS["wrapUpSeconds"]`, and the mod's `DEFAULT_BOX_MS = 3000 * 1000`.
2. `policy/COMMANDER.md`: the defaults block gets `"timeoutSeconds": 3600` and a new `"wrapUpSeconds": 3000`; on line 65 change `default 20 min` to `default 50 min`.
3. `RunView` gains `boxMs?: number`. `rememberStart` reads `snap.gate?.timebox?.seconds`; when it is a number > 0 it stores `boxMs = seconds * 1000`. A correction round takes the value from its own new snapshot, not from the earlier run.
4. Every place that uses the global box for a run (`stageOf`, `detailOf`, `sorted`, `bandRows`, `statusText`, and the pane around `register.tsx:402–413`) uses `run.boxMs ?? boxMs`. Recommended: a helper `boxOf(run, fallback)` in `model.ts`; keep the existing function signatures (the global value stays as the fallback).
5. Existing users: `install.ps1` seeds the user config once, so current users keep 1800 / 1200 in their file. **Do not rewrite user files.** `zcrew config` prints a note (not a problem; exit code unchanged) when the effective user file has exactly `wrapUpSeconds == 1200` and `timeoutSeconds == 1800`: `note: wrapUpSeconds/timeoutSeconds are the pre-0.11.1 defaults (20/30 min); the new defaults are 3000/3600`.

**Tests:**
- `DefaultsFileTests`: expected `timeoutSeconds` 1800 → 3600 (mention in the PR body that this is the intentional default change).
- `model.test.ts`: a run with `boxMs` = 60 min at 21 min is stage `running` (not `near`) and its detail contains `21/60`; a run without `boxMs` uses the fallback.
- `hooks.test.ts`: an `agent-start` snapshot with `gate.timebox.seconds: 3600` stores `boxMs: 3600000`.
- `zcrew config` shows the note for the old values and not for the new ones.

**DoD A2:**
- [ ] `grep -rn "1200" scripts mod config policy` shows no remaining default of 1200 for wrap-up (except the note text from item 5 and tests that deliberately use the old value).
- [ ] `claude plugin validate mod/zcrew && claude plugin test mod/zcrew` pass and the mod test count is above 42.
- [ ] Python VERIFY passes.

---

## PR-B — `zcrew` crashes when printing characters outside the console code page (Windows)

**Problem:** `zcrew gate` prints the ACCEPTANCE CRITERIA in its JUDGMENT section (`scripts/gate.py:418`). With `≥` and a cp1252 pipe it raises `UnicodeEncodeError`. (`--json` is unaffected because `json.dumps` emits ASCII.)

**Files:** `scripts/zcrew.py` (the `if __name__ == "__main__":` block), `tests/test_zcrew.py` or a new `tests/test_cli_encoding.py`.

**Design:** in the `__main__` block, which already calls `reconfigure(line_buffering=True)`, reconfigure both `sys.stdout` and `sys.stderr` with `encoding="utf-8", errors="replace"` (line buffering stays on stdout). Guard with `hasattr(stream, "reconfigure")` and `try/except (ValueError, OSError)`. UTF-8 is correct because the readers are Claude Code and Codex. Do not change `gate.py`; every subcommand benefits from the single fix.

**Tests:** run `sys.executable scripts/zcrew.py gate <run> --no-verify` as a subprocess with `PYTHONIOENCODING=cp1252` on a fake gate record (temp `ZCODE_COMMANDER_STATE`, temp git repo, see `RepoFixture` in `tests/test_gate.py`) whose ACCEPTANCE CRITERIA contain `≥`. Assert: return code is 0 or 1 (not a crash), stderr has no `UnicodeEncodeError`, stdout decoded as UTF-8 contains `≥`.

**DoD B:**
- [ ] The test fails on `main` (show the `UnicodeEncodeError` before fixing) and passes after the fix.
- [ ] VERIFY passes.

---

## PR-D — `zcrew gate` reports `scope FAIL` for files the worker never touched

**Root cause (reproduced with real git):** the gate record stores `base = HEAD` at `agent-start`, and the gate runs `git diff --name-only <base>` (`scripts/gate.py:215`). If anything is merged or committed into the worker's working copy afterwards (for example merging `main` into the worktree to resolve a conflict, or fast-forwarding), those files show up in the diff. A fast-forward leaves no merge commit to filter out. `main` moving on its own, without a merge into the worktree, does **not** cause the problem.

**Principle:** workers must not commit (`AGENTS.md` rule 3), so the worker's change is the uncommitted difference against the current HEAD. Any commit after `base` belongs to the commander.

**Files:** `scripts/gate.py`, `scripts/zcrew.py` (gate argparse: add `--base`), `tests/test_gate.py`.

**Design:**
1. In `build_report`, run `git rev-parse HEAD` in `work_dir`.
   - `head == base` → behave exactly as today.
   - `head != base` → use `HEAD` as the diff base for both `changed_files` and `check_tests`, and add a check `check("history", "WARN", f"HEAD moved since agent-start ({n} commits); files changed by them are not checked as worker changes", committed[:8])`, where `committed = git diff --name-only <base> HEAD` and `n = git rev-list --count <base>..HEAD`.
   - If `git merge-base --is-ancestor <base> HEAD` fails (history rewritten or reset) → keep the recorded base and add a `history` WARN: `base commit is not an ancestor of HEAD; diffing against the recorded base`.
2. New flag `zcrew gate --base <rev>` forces the diff base and skips item 1. It is the escape hatch when the commander committed the worker's work before running the gate (pass the record's base to get the old behavior).
3. `WARN` must not turn `mechanical` into FAIL (same as the existing `tests` WARN; check lines 347 and 387).
4. `render()` shows the new check like any other (no change if it already loops over `checks`).
5. `zcrew gate --json` only gains an item in `checks`; no existing key changes (the mod and `codex_hook.py` read `mechanical` / `summary` — confirm they still work).

**Tests (`tests/test_gate.py`, use `RepoFixture`):**
- Base → commit `other.txt` (simulates a merge or fast-forward) → worker edits an in-SCOPE file → scope PASS, `history` WARN lists `other.txt`, mechanical PASS.
- No new commits → no `history` check; results identical to today.
- Worker edits an out-of-SCOPE file after HEAD moved → scope still FAIL (no false PASS).
- `--base <base>` → old behavior (committed files are counted).
- Base not an ancestor → WARN and the recorded base is used.

**DoD D:**
- [ ] A test reproducing the issue (merge, then scope FAIL) fails on `main` and passes after the fix.
- [ ] All new tests pass and all existing gate tests pass **unchanged**.
- [ ] `codex_hook` tests and mod tests still pass.
- [ ] VERIFY passes.

---

## PR-R — Release v0.11.1

**Files:** `CHANGELOG.md`, `manifest.json` (only `"version": "0.11.1"`), following the shape of commit `8f20010`.
- CHANGELOG has Fixed (A1, B, C, D) and Changed (A2: new 60 / 50 minute defaults, per-run time box in the mod) sections, plus an **Upgrade note**: existing user configs still hold 1800 / 1200; edit `~/.zcode-commander/config.json` (name the two keys) and run `zcrew update` to receive the new `bridge_compat.py`.
- Reference `Closes #2`.

**DoD R:**
- [ ] `manifest.json` changes only the version; tests that read the manifest pass.
- [ ] VERIFY passes.
- [ ] Issue #2 is closed when the last fix merges.

---

## Overall DoD

- [ ] All six PRs merged in order with VERIFY green.
- [ ] Every item in issue #2 has a test that fails on `8f20010` and passes afterwards: inline TIMEBOX, effective value echoed, warnings, `UnicodeEncodeError`, `SCOPE (…)`, scope FAIL after merge.
- [ ] No new dependency; no forbidden file touched; everything committed is in English.
- [ ] (Owner, on Windows after release) a contract with `TIMEBOX: 60m` at the end of a line gets its wrap-up near 60 minutes and the mod band shows `/60 min`.
