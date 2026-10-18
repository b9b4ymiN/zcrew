# Plan: Issue #2 — TIMEBOX, gate unicode, SCOPE heading, scope diff after merge

สถานะ: **รอ approve** · ผู้เขียนแผน: commander session 2026-10-18 · ผู้ลงมือ: Sonnet (session ถัดไป)
Base: `main` @ `8f20010` (v0.11.0)

---

## 0. กติกาสำหรับผู้ลงมือ (อ่านก่อนเริ่ม)

1. ทำทีละ PR ตามลำดับในข้อ 2 แต่ละ PR แตก branch จาก `main` **ล่าสุด** (หลัง PR ก่อนหน้า merge แล้ว) ชื่อ branch ตามตาราง
2. ขอบเขตของแต่ละ PR = ไฟล์ที่ระบุใน "Files" เท่านั้น ถ้าต้องแตะไฟล์อื่น **หยุดและถามผู้ใช้**
3. ห้ามแตะ (ตาม `CLAUDE.md`): dashboard JSON API, `SessionSummary`/`Activity`, security posture ของ dashboard, dependency ใหม่ทุกชนิด, `AGENTS.override.md`, `get.ps1`, `scripts/install.ps1`
   - `policy/COMMANDER.md` → **ผู้ใช้อนุญาตแล้ว** เฉพาะจุดที่ระบุในแผนนี้
   - `manifest.json` → **ผู้ใช้อนุญาตแล้ว** เฉพาะ PR-R (version 0.11.1)
4. Python stdlib เท่านั้น, `from __future__ import annotations`, type hints, ฟังก์ชันเล็ก, comment เฉพาะ "why" ที่ไม่ชัด, Conventional Commits
5. ก่อน push ทุกครั้งต้องรัน VERIFY (ข้อ 1) และผลต้องตรงกับ baseline + test ใหม่
6. ห้าม skip/disable test, ห้ามแก้ test เดิมให้ผ่านโดยเปลี่ยนความหมาย (แก้ได้เฉพาะเมื่อ test นั้นตรวจค่า default ที่แผนนี้เปลี่ยน และต้องบอกใน PR body)

## 1. VERIFY (รันทุก PR)

```text
python -m unittest discover -s tests
python -m py_compile scripts/*.py
claude plugin validate mod/zcrew && claude plugin test mod/zcrew      # เฉพาะ PR ที่แตะ mod/
```

**Baseline (Linux cloud, Python 3.13, 2026-10-18):** `Ran 523 tests … FAILED (failures=1, skipped=1)`
- failure เดียวคือ `test_zcrew.PathStringTests.test_add_expands_variables_for_comparison` — ใช้ `%VAR%` ของ Windows จึง fail บน Linux เท่านั้น **ไม่เกี่ยวกับงานนี้ ห้ามแก้** (บน Windows ต้องผ่าน)
- mod: `42 pass, 0 fail`

DoD ทุก PR: จำนวน test เพิ่มขึ้น, failure มีแค่ตัวเดิมตัวเดียว (บน Linux), mod ผ่านทั้งหมด

## 2. ลำดับ PR

| # | Branch | หัวข้อ | ขึ้นกับ |
|---|---|---|---|
| A1 | `fix/timebox-parse` | TIMEBOX inline + echo ค่าจริง + warning + เวลาในประกาศ | — |
| C  | `fix/contract-heading-suffix` | `SCOPE (…)` + warning near-miss | A1 (ไฟล์เดียวกัน) |
| A2 | `feat/per-run-timebox` | mod ใช้ time box ต่องาน + default ใหม่ 60/50 นาที | A1 |
| B  | `fix/cli-utf8-output` | crash `UnicodeEncodeError` บน Windows | — |
| D  | `fix/gate-moved-head` | scope FAIL หลัง HEAD ขยับ | — |
| R  | `chore/release-0.11.1` | CHANGELOG + manifest 0.11.1 | ทุกตัว |

> หมายเหตุ: A2 เปลี่ยนค่า default (เปลี่ยนพฤติกรรม) ตาม semver ควรเป็น 0.12.0 แต่ผู้ใช้เลือก 0.11.1 — ใช้ 0.11.1 ตามที่สั่ง

---

## PR-A1 — TIMEBOX อ่านได้ทุกตำแหน่ง + บอกค่าที่ใช้จริง

**ปัญหา:** `_TIMEBOX_RE` (`scripts/bridge_compat.py:581`) anchored `^…$` → `TASK X — … TIMEBOX: 60m` ไม่ match → ใช้ `wrapUpSeconds` 1200 เงียบ ๆ

**Files:** `scripts/bridge_compat.py`, `tests/test_bridge_compat.py`, `policy/COMMANDER.md` (บรรทัด 63 เท่านั้น)

**Design:**
1. Regex ใหม่ (case-**sensitive**, ไม่มี `re.IGNORECASE`):
   `(?<![\w-])TIMEBOX\s*:\s*(\d+)\s*(sec|s|min|m|h)?(?![\w])`
   หน่วยไม่ใส่ = นาที (เหมือนเดิม) ใช้ match **ตัวแรก**
2. แยก `parse_timebox` ให้คืนข้อมูลพอสำหรับ warning — เพิ่มฟังก์ชันใหม่ (เก็บ `parse_timebox(prompt) -> int | None` ไว้เหมือนเดิมเพื่อไม่ทำให้ caller/test เดิมพัง):
   ```python
   @dataclass
   class Timebox:
       seconds: int | None        # ค่าที่ใช้จริง (None = ปิด time box)
       source: str                # "contract" | "config" | "off"
       warnings: list[str]
   def resolve_timebox(args, env) -> Timebox
   ```
   - `wrap_up_delay(args, env)` ให้เรียก `resolve_timebox(...).seconds` (พฤติกรรมเดิมทุกกรณีที่ match อยู่แล้วต้องเหมือนเดิม)
3. Warnings (ข้อความภาษาอังกฤษ สั้น ขึ้นต้น `TIMEBOX`):
   - มีคำ `TIMEBOX` (regex `\bTIMEBOX\b`, ตัวพิมพ์ใดก็ได้) แต่ parse ไม่ได้ → `"TIMEBOX found but not understood (write e.g. TIMEBOX: 60m); using wrapUpSeconds=<n>"`
   - match มากกว่า 1 ค่าและค่าต่างกัน → `"several TIMEBOX values; using the first (<n>m)"`
   - ค่าที่ขอ > `timeout - WRAP_UP_GRACE_SECONDS` → `"TIMEBOX <x>m is longer than the run timeout <t>m; wrap-up at <d>m instead (raise timeout)"`
   - ค่าที่ได้ < `WRAP_UP_MIN_SECONDS` → `"TIMEBOX too short; no time box"` และ `source="off"`
4. ผล `agent-start` (ใน `patch_run_hooks.start`):
   - ถ้าเป็น run snapshot และ **ไม่ใช่** correction (`threadId` ไม่มี) หรือเป็น correction ก็ได้ → ใส่ `result["gate"]["timebox"] = {"seconds": <int|None>, "source": <str>}` **ทุกครั้ง** แม้ `gateMode == "off"` (กรณี off: `result["gate"] = {"timebox": …}` อย่างเดียว ไม่มี `verify` hint)
   - warnings ของ timebox ต่อท้าย `gate.warnings` (สร้าง list ถ้ายังไม่มี)
   - `ZCODE_COMMANDER_RUN_HOOKS=off` → ไม่เพิ่มอะไรเลย (เหมือนเดิม)
   - ตรวจว่า `compact_snapshot` ไม่ตัด key `gate` (`_DROP_KEYS` ไม่มี `gate` — ห้ามเพิ่ม)
5. ประกาศที่ต่อท้าย prompt (`TIMEBOX_NOTICE`) เพิ่มเวลานาฬิกาท้องถิ่นของเครื่อง bridge:
   `"[zcrew] Time box: about %d minutes (wrap-up near %s local time). Run the VERIFY commands before then; if you are told to wrap up, stop and report immediately. …"` — `%s` = `time.strftime("%H:%M", time.localtime(time.time() + delay))` คงประโยค "at most 3 approaches" เดิมไว้
   - ทำให้ test ได้: รับ `clock` เป็น parameter หรือ format ผ่านฟังก์ชันเล็กที่ inject เวลาได้
6. Gate record: เพิ่ม key `"timebox": {"seconds", "source"}` ใน dict ที่ `record_start` เขียน (round แรกเท่านั้น; correction round อัปเดตค่าใน record เป็นของ round ล่าสุด) — **A2 ใช้ค่านี้ไม่ได้โดยตรง** (mod อ่านจากผล agent-start) แต่เก็บไว้ให้ `zcrew gate`/debug
7. Docs: docstring บรรทัด ~32 → `"A ``TIMEBOX: 40m`` anywhere in the prompt overrides the default."`; `policy/COMMANDER.md:63` → `` `TIMEBOX: 40m` (anywhere in the contract, uppercase) overrides the default time box for one task; the agent-start result echoes the time box used in `gate.timebox`.``

**Tests ที่ต้องเพิ่ม (`tests/test_bridge_compat.py`):**
- inline ท้ายประโยค `TASK X — do it. TIMEBOX: 60m` → 3600
- บรรทัดเดี่ยวยังได้ (`TIMEBOX: 40m` → 2400), `TIMEBOX:90s`, `TIMEBOX: 2h`, `TIMEBOX: 45 min`, `TIMEBOX: 45` (ไม่มีหน่วย = นาที)
- ไม่ match: `timebox: 60m` (ตัวเล็ก) → None + warning "not understood"; `MYTIMEBOX: 5m` → None; `TIMEBOX: soon` → warning
- 2 ค่าต่างกัน → ค่าแรก + warning
- เกิน timeout (`TIMEBOX: 60m`, `timeout: 1800`) → seconds = 1680, warning "longer than the run timeout"
- `agent-start` result มี `gate.timebox` ทั้ง `gateMode` warn และ off; source `contract`/`config`/`off` ถูก
- prompt มี "wrap-up near HH:MM" ตามเวลาที่ inject

**DoD A1:**
- [ ] Repro จาก issue (`TASK X — do something. TIMEBOX: 60m`, `timeout: 5400`) → `wrap_up_delay == 3600` และ `gate.timebox == {"seconds": 3600, "source": "contract"}` (มี test ตรงนี้ชื่อบอกว่าเป็น issue #2)
- [ ] test เดิม `test_timebox`, `WrapUpDelayTests`, `PatchRunHooksTests` ผ่านโดย**ไม่แก้**
- [ ] VERIFY ผ่านตามข้อ 1

---

## PR-C — หัวข้อ section มีวงเล็บต่อท้าย + เตือนเมื่อใกล้เคียง

**ปัญหา:** `_SECTION_RE` (`bridge_compat.py:578`) ไม่รับ `SCOPE (may change)` → `contract is missing: SCOPE`

**Files:** `scripts/bridge_compat.py`, `tests/test_bridge_compat.py`

**Design:**
1. Regex รับวงเล็บต่อท้ายชื่อ (เนื้อในวงเล็บถูกทิ้ง):
   `^\s*(?:#{1,6}\s*)?\**(NAMES)\**(?:\s*\([^)\n]*\))?\**\s*(?::\s*\**\s*(.*?))?\s*$`
   ต้องรับ: `SCOPE (may change)`, `SCOPE (may change):`, `**SCOPE (files):** \`src/**\``, `## SCOPE (files)`; และทุกแบบเดิม
2. Near-miss: บรรทัดที่ขึ้นต้น (หลัง `#`/`*` ที่เป็น optional) ด้วยชื่อ section ตัวพิมพ์ใหญ่ + `\b` แต่ไม่ match regex หลัก → จำไว้ `{name: line_text}` (ตัวแรกต่อชื่อ)
   - `parse_contract` คืน key ใหม่ `"near_misses": {name: line}` **เฉพาะชื่อที่อยู่ใน `missing`** (กันเสียงรบกวนจากประโยค เช่น `DO NOT touch x` ในส่วนอื่น)
   - `record_start` warning เดิม `contract is missing: …` ต่อท้ายด้วย `; heading not recognised: "<line>" — write "SCOPE:" or "SCOPE (note):"` สำหรับแต่ละ near-miss (clip 60 ตัวอักษร)

**Tests:** แต่ละรูปแบบข้อ 1 → section ถูกอ่าน, bullets ถัดไปเข้า section; `SCOPE - files` (near-miss) → missing SCOPE + near_misses มี; `DO NOT touch x` ในขณะที่มี `DO NOT:` ปกติ → ไม่มี near_miss; ทุก test `ContractParseTests` เดิมผ่าน

**DoD C:**
- [ ] ตัวอย่างจาก issue (`SCOPE (may change)` + bullets) → `missing` ไม่มี SCOPE และ `scope` มี path ใน backticks
- [ ] warning ของ near-miss มีข้อความ heading ที่ผิดและรูปแบบที่ถูก
- [ ] VERIFY ผ่าน

---

## PR-A2 — time box ต่องานใน mod + ค่า default ใหม่

**ปัญหา:** mod ใช้ `ctx.boxMs` ค่าเดียว (จาก config, default 1200 s) กับทุกงาน → งาน `TIMEBOX: 60m` แสดง "21/20 min ⏰"; และ 20 นาทีไม่พอสำหรับงาน ZCode

**Files:** `mod/zcrew/types/*.ts` (RunView), `mod/zcrew/hooks/register.tsx`, `mod/zcrew/hooks/model.ts`, `mod/zcrew/tests/*.test.ts`, `scripts/commander.py` (DEFAULT_CONFIG), `scripts/bridge_compat.py` (`RUN_HOOKS_DEFAULTS`, `BRIDGE_TIMEOUT_DEFAULT` **ไม่แตะ** — นั่นคือ default ของ upstream), `config/default-config.json`, `tests/test_commander.py`, `tests/test_bridge_compat.py`, `policy/COMMANDER.md` (block defaults บรรทัด ~33–42 และบรรทัด 65)

**Design:**
1. Default ใหม่: `timeoutSeconds` 1800 → **3600**, `wrapUpSeconds` 1200 → **3000** ใน `commander.DEFAULT_CONFIG`, `config/default-config.json`, `bridge_compat.RUN_HOOKS_DEFAULTS["wrapUpSeconds"]`, mod `DEFAULT_BOX_MS = 3000 * 1000`
2. `COMMANDER.md`: block defaults → `"timeoutSeconds": 3600` และเพิ่ม `"wrapUpSeconds": 3000`; บรรทัด 65 `default 20 min` → `default 50 min`
3. RunView เพิ่ม `boxMs?: number`; `rememberStart` อ่าน `snap.gate?.timebox?.seconds` → ถ้าเป็นเลข > 0 ใส่ `boxMs = seconds*1000`; correction round ใช้ค่าจาก snapshot ใหม่ (ไม่ใช่ของ earlier)
4. ทุกที่ที่ใช้ `boxMs` ต่อ run (`stageOf`, `detailOf`, `sorted`, `bandRows`, `statusText`, pane ใน `register.tsx` บรรทัด ~402–413) ใช้ `run.boxMs ?? boxMs` — วิธีที่แนะนำ: helper `boxOf(run, fallback)` ใน `model.ts` แล้วแทนที่ใน `stageOf`/`detailOf` และ fill/label ใน pane; signature ของ function เดิมคงไว้ (fallback)
5. ผู้ใช้เดิม: `install.ps1` seed config ครั้งเดียว → ผู้ใช้เดิมมี 1800/1200 เขียนตายตัวในไฟล์ **ห้ามแก้ไฟล์ผู้ใช้อัตโนมัติ** แต่ `zcrew config` (ฟังก์ชัน `validate_config` หรือ `_print_config` ใน `commander.py`) แสดง note เมื่อ `wrapUpSeconds == 1200 and timeoutSeconds == 1800` จากไฟล์ผู้ใช้: `"note: wrapUpSeconds/timeoutSeconds are the pre-0.11.1 defaults (20/30 min); the new defaults are 3000/3600"` — เป็น note ไม่ใช่ problem (ไม่ทำให้ exit code เปลี่ยน)

**Tests:**
- `DefaultsFileTests` ปรับค่าคาดหวัง 1800 → 3600 (ระบุใน PR body ว่าเปลี่ยนเพราะ default ใหม่)
- model.test.ts: run ที่มี `boxMs` 60 นาที ที่ 21 นาที → stage `running` (ไม่ใช่ `near`), detail มี `21/60`; run ไม่มี `boxMs` ใช้ fallback
- hooks.test.ts: agent-start ที่ snapshot มี `gate.timebox.seconds: 3600` → run ที่เก็บมี `boxMs: 3600000`
- `zcrew config` note ปรากฏเมื่อค่าเก่า, ไม่ปรากฏเมื่อค่าใหม่

**DoD A2:**
- [ ] `grep -rn "1200" scripts mod config policy` ไม่เหลือค่า default 1200 ของ wrap-up (ยกเว้นข้อความ note ของข้อ 5 และ test ที่ตั้งใจใช้ค่าเก่า)
- [ ] `claude plugin validate mod/zcrew && claude plugin test mod/zcrew` ผ่าน, จำนวน test mod > 42
- [ ] VERIFY Python ผ่าน

---

## PR-B — `zcrew` crash เมื่อพิมพ์อักขระนอก code page (Windows)

**ปัญหา:** `zcrew gate` render ส่วน JUDGMENT ที่มี ACCEPTANCE CRITERIA (`scripts/gate.py:418`) เช่น `≥`; stdout เป็น pipe ใช้ cp1252 → `UnicodeEncodeError` (`--json` ไม่โดนเพราะ `json.dumps` เป็น ASCII)

**Files:** `scripts/zcrew.py` (บล็อก `if __name__ == "__main__":`), `tests/test_zcrew.py` (หรือ test ไฟล์ใหม่ `tests/test_cli_encoding.py`)

**Design:** ใน `__main__` (ที่มี `reconfigure(line_buffering=True)` อยู่แล้ว) ทำกับทั้ง `sys.stdout` และ `sys.stderr`:
`stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)` (stderr ไม่ต้อง line_buffering) — ห่อด้วย `hasattr(…, "reconfigure")` และ `try/except (ValueError, OSError)`
- ใช้ utf-8 เพราะผู้อ่านหลักคือ Claude Code/Codex ที่ decode เป็น utf-8; `errors="replace"` กันกรณีสุดท้าย
- ไม่แก้ `gate.py` (ทุก subcommand ได้ประโยชน์จากจุดเดียว)

**Tests:** subprocess `sys.executable scripts/zcrew.py <cmd>` ด้วย env `PYTHONIOENCODING=cp1252` ที่ทำให้พิมพ์ `≥` ได้โดยไม่ต้องมี ZCode — วิธีที่แนะนำ: สร้าง gate record ปลอมใน temp `ZCODE_COMMANDER_STATE` + git repo temp (ดู `RepoFixture` ใน `tests/test_gate.py`) ที่ ACCEPTANCE CRITERIA มี `≥` แล้วรัน `zcrew gate <run> --no-verify` → returncode ไม่ใช่ crash (0 หรือ 1), stderr ไม่มี `UnicodeEncodeError`, stdout (decode utf-8) มี `≥`
- ถ้า `zcrew.py` เรียก gate ผ่าน subprocess ใน test ไม่ได้เพราะ path/ENV ของ Windows → ใช้ `ZCODE_COMMANDER_STATE` และ `HOME` ใน temp

**DoD B:**
- [ ] test ข้างบน fail บน `main` (พิสูจน์ก่อนแก้: รันบน main ต้องเห็น `UnicodeEncodeError`) และผ่านหลังแก้
- [ ] VERIFY ผ่าน

---

## PR-D — `zcrew gate` scope FAIL เพราะไฟล์จาก commit ที่ไม่ใช่ของ worker

**สาเหตุ (ยืนยันด้วย repro ใน git จริง):** record เก็บ `base = HEAD` ตอน agent-start; gate ใช้ `git diff --name-only <base>` (`scripts/gate.py:215`) ถ้ามีคน merge/commit เข้า **working copy ของ worker** หลัง agent-start (เช่น merge main เข้า worktree เพื่อแก้ conflict หรือ worker ทำงานใน checkout ของ main แล้ว commander merge branch อื่นเข้ามา) diff จะรวมไฟล์เหล่านั้น — fast-forward ไม่มี merge commit ให้แยก. แค่ main ขยับเอง (ไม่ merge เข้า worktree) **ไม่**ทำให้ผิด

**หลักที่ใช้:** worker ห้าม commit (`AGENTS.md` ข้อ 3) ดังนั้นงานของ worker = การเปลี่ยนแปลงที่ยังไม่ commit เทียบกับ HEAD ปัจจุบัน; commit ใดหลัง base เป็นของ commander

**Files:** `scripts/gate.py`, `scripts/zcrew.py` (argparse ของ gate: เพิ่ม `--base`), `tests/test_gate.py`

**Design:**
1. ใน `build_report`: `head = git rev-parse HEAD` ใน `work_dir`
   - `head == base` → พฤติกรรมเดิมทุกอย่าง
   - `head != base` → `diff_base = "HEAD"` สำหรับ `changed_files` และ `check_tests`; เพิ่ม check ใหม่ `check("history", "WARN", f"HEAD moved since agent-start ({n} commits); files they changed are not checked as worker changes", committed[:8])` โดย `committed = git diff --name-only <base> HEAD`, `n = git rev-list --count <base>..HEAD`
   - ถ้า `git merge-base --is-ancestor <base> HEAD` ไม่จริง (history ถูก rewrite/reset) → `diff_base` = base เดิม + check `history` WARN `"base commit is not an ancestor of HEAD; diffing against the recorded base"`
2. flag `zcrew gate --base <rev>`: บังคับ diff_base (ข้ามข้อ 1) — ทางออกเมื่อ commander commit งานของ worker เองก่อนรัน gate (เช่น `--base <record base>` เพื่อได้พฤติกรรมเดิม)
3. `WARN` ไม่ทำให้ `mechanical` เป็น FAIL (เหมือน `tests` WARN เดิม) — ตรวจกับโค้ดบรรทัด 347/387
4. `render()` แสดง check ใหม่ด้วยรูปแบบเดียวกับ check อื่น (ไม่ต้องแก้ถ้าวน `checks` อยู่แล้ว)
5. JSON ของ `zcrew gate --json`: เพิ่ม item ใน `checks` เท่านั้น ไม่เปลี่ยน key เดิม (mod และ `codex_hook.py` อ่าน `mechanical`/`summary` — ตรวจว่าไม่พัง)

**Tests (`tests/test_gate.py`, ใช้ `RepoFixture`):**
- worktree scenario: base → commit ไฟล์ `other.txt` (จำลอง merge/ff จาก branch อื่น) → worker แก้ไฟล์ใน SCOPE → scope PASS, check `history` WARN มี `other.txt`, mechanical PASS
- ไม่มี commit ใหม่ → ไม่มี check `history` (ผลเหมือนเดิมทุก test)
- worker แก้ไฟล์นอก SCOPE หลัง HEAD ขยับ → scope ยัง FAIL (กัน false PASS)
- `--base <base>` → พฤติกรรมเดิม (ไฟล์จาก commit ถูกนับ)
- base ไม่เป็น ancestor (reset ไป orphan/commit อื่น) → WARN + ใช้ base เดิม

**DoD D:**
- [ ] repro test ของ issue (merge แล้ว scope FAIL) fail บน main และผ่านหลังแก้
- [ ] test D ทุกตัวผ่าน + test gate เดิมทั้งหมดผ่านโดยไม่แก้
- [ ] `codex_hook` tests และ mod tests ยังผ่าน
- [ ] VERIFY ผ่าน

---

## PR-R — release v0.11.1

**Files:** `CHANGELOG.md`, `manifest.json` (`"version": "0.11.1"` เท่านั้น) — รูปแบบตาม commit `8f20010`
- CHANGELOG มีหัวข้อ Fixed (A1, B, C, D) / Changed (A2 default 60/50 นาที, time box ต่องานใน mod) และ **Upgrade note**: config ผู้ใช้เดิมยังเป็น 1800/1200 — แก้ด้วยมือใน `~/.zcode-commander/config.json` (ระบุ key), `zcrew update` เพื่อรับ `bridge_compat.py` ใหม่
- อ้าง `Closes #2`

**DoD R:**
- [ ] `manifest.json` เปลี่ยนแค่ version; test ที่อ่าน manifest ผ่าน
- [ ] VERIFY ผ่าน
- [ ] issue #2 ปิดเมื่อ PR-R merge (หรือเมื่อ PR สุดท้ายของ fix merge)

---

## DoD รวม (ทั้งงาน)

- [ ] 6 PR merge ตามลำดับ, CI/VERIFY เขียวทุกตัว
- [ ] ทุกข้อใน issue #2 มี test ที่ fail บน `8f20010` และผ่านหลังแก้: TIMEBOX inline, echo ค่าจริง, warning, UnicodeEncodeError, `SCOPE (…)`, scope FAIL หลัง merge
- [ ] ไม่มี dependency ใหม่, ไม่แตะไฟล์ต้องห้าม
- [ ] (ผู้ใช้ทดสอบบน Windows หลัง release) contract ที่มี `TIMEBOX: 60m` ท้ายบรรทัด → ถูกสั่งสรุปราว 60 นาที, แถบ mod แสดง `/60 min`
