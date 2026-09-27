# zcrew — คู่มือภาษาไทย (v0.7.0)

> English: [README.md](../README.md)

คุยกับ **Claude Code** คนเดียวก็พอ เรื่องลงมือเขียนโค้ด Claude จะส่งต่อให้ **ZCode (GLM-5.3)** ทำ ระหว่างทาง Claude ตรวจงานเองทุกชิ้น สั่งแก้ใน session เดิมของ ZCode ซ้ำจนผ่าน แล้วค่อยรายงานคุณ

เปรียบเทียบง่ายๆ คือบริษัทรับเหมา:

| บทบาท | ใคร | ทำอะไร |
|---|---|---|
| ลูกค้า | คุณ | บอกว่าอยากได้อะไร อนุมัติแผน |
| เจ้าของบริษัท (หัวหน้า) | **Claude Code หรือ Codex** (เลือกราย project หรือใช้ทั้งคู่) | ถามให้ชัด → วางแผน → แจกงาน → ตรวจงาน → สั่งแก้ → รายงาน |
| ช่าง (สูงสุด 5 คน) | ZCode (GLM-5.3, คิดระดับ max) | ลงมือเขียนโค้ด รัน test และแก้ตามที่สั่ง |

> ช่างตัดสินเองไม่ได้ว่างานของตัวเองผ่าน Claude ต้องดู `git diff` และรัน test เองทุกครั้ง

---

## สารบัญ

1. [ทำงานยังไง](#1-ทำงานยังไง)
2. [สิ่งที่ต้องมีก่อนติดตั้ง](#2-สิ่งที่ต้องมีก่อนติดตั้ง)
3. [ติดตั้งบนเครื่องใหม่ (คำสั่งเดียว)](#3-ติดตั้งบนเครื่องใหม่-คำสั่งเดียว)
4. [เปิดใช้กับ project](#4-เปิดใช้กับ-project)
5. [ใช้งานประจำวัน](#5-ใช้งานประจำวัน)
6. [ตั้งค่า model และขีดจำกัด](#6-ตั้งค่า-model-และขีดจำกัด)
7. [อ่านผล doctor](#7-อ่านผล-doctor)
8. [อัปเดต](#8-อัปเดต)
9. [ถอนการติดตั้ง](#9-ถอนการติดตั้ง)
10. [แก้ปัญหา](#10-แก้ปัญหา)
11. [ข้อจำกัดที่รู้แล้ว](#11-ข้อจำกัดที่รู้แล้ว)
12. [โครงสร้าง repo และการพัฒนาต่อ](#12-โครงสร้าง-repo-และการพัฒนาต่อ)

---

## 1. ทำงานยังไง

```text
คุณ
 │  "เพิ่มปุ่ม export รายงาน"
 ▼
Claude Code ──(อ่าน policy: ~/.claude/zcode-commander/COMMANDER.md)
 │  1. ถามให้ชัด (ภาษาง่าย ทีละ 1-2 ข้อ)
 │  2. ส่งแผน 200-300 คำ แบ่ง Phase/Task พร้อม DoD → รอคุณ approve
 │  3. เขียน Worker Contract ต่อ task
 ▼  MCP: zcode_executor (agent-start / agent-wait / agent-close)
coder-mcp-bridge  ◄── bridge_compat.py (shim ที่ทำให้ใช้กับ ZCode 3.14 ได้)
 ▼
ZCode app-server (GLM-5.3 @ max) ── แก้โค้ด, รัน test
 ▼
Claude ตรวจเอง: git diff + รัน test/build เอง
 ├─ ไม่ผ่าน → สั่งแก้กลับไปที่ threadId เดิม (สูงสุด 4 รอบ)
 └─ ผ่าน   → ปิด run → รายงานคุณ
```

งานอิสระที่ไม่แตะไฟล์ร่วมกัน Claude จะแยก **git worktree** ให้ ZCode หลายตัวทำขนานกัน (สูงสุด 5 ตัว) แล้ว merge กลับทีละ branch โดยรัน test ซ้ำทุกครั้งหลัง merge

ดูแบบสดว่า ZCode แต่ละตัวกำลังทำอะไรได้ด้วย `zcrew watch` (terminal) หรือ `zcrew dashboard` (หน้าเว็บในเครื่อง) ดูรายละเอียดในข้อ 5

---

## 2. สิ่งที่ต้องมีก่อนติดตั้ง

| ของ | เวอร์ชัน | เช็คด้วย |
|---|---|---|
| Windows | 10/11 | — |
| ZCode Desktop | **3.14.0** (เวอร์ชันที่ทดสอบแล้ว) และ login ด้วย **Z.ai individual GLM Coding Plan** แล้ว | เปิดแอป ZCode แล้วลองสั่งงานได้ |
| Claude Code | ติดตั้งและ login แล้ว | `claude --version` |
| Python | 3.10 ขึ้นไป อยู่ใน PATH ในชื่อ `python` | `python --version` |
| Git | อะไรก็ได้ที่ไม่เก่ามาก | `git --version` |
| อินเทอร์เน็ต | ใช้ตอนติดตั้ง เพื่อ clone `coder-mcp-bridge` จาก GitHub | — |

ZCode ติดตั้งได้ทั้ง `C:\Program Files\ZCode` (ติดตั้งให้ทั้งเครื่อง) และ `%LOCALAPPDATA%\Programs\ZCode` (ติดตั้งเฉพาะ user) ตัวติดตั้งหาเจอเองทั้งสองแบบ

> **แพลนอื่น** (Start / Team / Off-peak): ยังใช้กับโหมดเบื้องหลังไม่ได้ ต้องใช้ผ่านแอป ZCode เท่านั้น (ดูข้อ 11)

---

## 3. ติดตั้งบนเครื่องใหม่ (คำสั่งเดียว)

### ขั้นที่ 1: รันคำสั่งติดตั้ง

เปิด **PowerShell ธรรมดา** (ไม่ต้อง Run as Administrator) แล้ววางคำสั่งนี้:

```powershell
irm https://raw.githubusercontent.com/b9b4ymiN/zcrew/main/get.ps1 | iex
```

คำสั่งนี้ทำให้ครบในครั้งเดียว:

1. เช็คว่ามีของครบ ได้แก่ git, Python 3.10 ขึ้นไป, Claude Code และ ZCode Desktop ถ้าขาดอะไรจะบอกเป็นรายการ พร้อมบอกว่าไปหาได้จากที่ไหน
2. ดึง repo ไปไว้ที่ `~/.zcrew/src`
3. ถ้า ZCode CLI ยังไม่ได้เลือก model จะคัดลอกการตั้งค่า Coding Plan (รวม API key) จากแอป ZCode ให้เอง ไฟล์เดิมจะถูกสำรองเป็น `.bak` และ key อยู่แค่ในเครื่องคุณ
4. clone `coder-mcp-bridge` ไปที่ `~/.zcode-commander/coder-mcp-bridge` โดย **pin ไว้ที่ commit ที่ทดสอบแล้ว**
5. ติดตั้ง policy และสร้าง `~/.zcode-commander/config.json` (ถ้ามีไฟล์อยู่แล้ว จะไม่เขียนทับ)
6. ลงทะเบียน MCP server `zcode_executor` ใน **Claude Code และ/หรือ Codex** ที่เจอในเครื่อง ถ้าต้องการเลือกเอง ให้ตั้ง `$env:ZCREW_COMMANDER = 'codex'` (หรือ `claude` / `both`) ก่อนรันคำสั่งติดตั้ง
7. สร้างคำสั่ง `zcrew` และเพิ่ม `~/.zcrew/bin` เข้า PATH ของ user (ไม่แตะ PATH ของเครื่อง)
8. รัน doctor แล้วบอกขั้นต่อไป

**ไม่แตะ** `~/.claude/CLAUDE.md` (คู่มือกลางของคุณ) เลย

> ถ้ายังไม่ได้ login แอป ZCode ตัวติดตั้งจะเตือนไว้ ให้ login ก่อนแล้วรัน `zcrew update`

### ขั้นที่ 2: อ่านผล doctor

ต้องจบด้วย `Result: READY` ถ้ายังไม่ได้ ให้ดู [ข้อ 7](#7-อ่านผล-doctor) และ [ข้อ 10](#10-แก้ปัญหา) รันซ้ำได้ทุกเมื่อ (ไม่เสีย quota model):

```powershell
zcrew doctor
```

### ขั้นที่ 3: Restart Claude Code

ปิดแอป Claude **ให้สนิท** (ถ้ามีไอคอนค้างใน system tray ให้คลิกขวาแล้วเลือก Quit) แล้วเปิดใหม่ เช็คได้ด้วย `claude mcp get zcode_executor` ต้องขึ้น `Status: √ Connected`

> คำสั่ง `zcrew` ใช้ได้ใน PowerShell หน้าต่างใหม่ทันที ถ้าหน้าต่างเก่ายังไม่รู้จักคำสั่ง ให้เปิดหน้าต่างใหม่

**ติดตั้งแบบไม่ใช้คำสั่งเดียว** (เช่นเครื่องที่ไม่อยากรันสคริปต์จาก URL):

```powershell
git clone https://github.com/b9b4ymiN/zcrew.git; cd zcrew
powershell -ExecutionPolicy Bypass -File .\get.ps1
```

---

## 4. เปิดใช้กับ project

policy **ไม่ทำงานทั้งเครื่อง** เปิดใช้เป็นราย project เท่านั้น project ต้องเป็น git repo เพราะ Claude ตรวจงานด้วย `git diff`

```powershell
cd C:\path\to\project
zcrew enable        # เปิดใช้
zcrew status        # เปิดอยู่ไหม, ใช้ config จากไหน, ค่าถูกต้องไหม
zcrew disable       # ปิดใช้ (CLAUDE.md กลับเป็นเหมือนเดิมทุกไบต์)
```

ถ้าไม่อยาก `cd` ใส่ path ต่อท้ายได้ เช่น `zcrew enable C:\path\to\project`

`enable` เพิ่มบล็อกนี้ต่อท้าย `<project>\CLAUDE.md` (ถ้ายังไม่มีไฟล์ จะสร้างให้ และตอน `disable` จะลบไฟล์ที่สร้างไว้ทิ้ง):

```markdown
<!-- zcode-commander:begin -->
@~/.claude/zcode-commander/COMMANDER.md
<!-- zcode-commander:end -->
```

ตัวเลือกเพิ่มเติม:
- `--with-templates`: สร้าง `CLAUDE.md` + `AGENTS.md` ตัวอย่างให้ (ดูหัวข้อถัดไป)
- `--with-project-config`: สร้าง `.claude/zcode-commander.json` ให้ project นี้ใช้ model/ค่าต่างจากค่ากลาง
- `--force`: เปิดใช้แม้โฟลเดอร์ไม่ใช่ git repo (ไม่แนะนำ)

หลัง enable ให้เปิด **session ใหม่** ของ Claude Code หรือ Codex ใน project นั้น

### ดูว่า context หมดไปกับอะไร

```powershell
cd C:\path\to\project
zcrew context                      # session ล่าสุดของ Claude Code ใน project นี้
zcrew context --commander codex    # session ล่าสุดของ Codex
```

รายงานนี้บอก:
- context ตอนเริ่ม session, ตอนสูงสุด และตอนล่าสุด
- output ของ tool แต่ละตัวกินไปกี่ %

พร้อมคำแนะนำ เช่น "Bash กิน 46% ให้เก็บ log ลงไฟล์" ในรายงานมีแค่ตัวเลขกับชื่อ tool ไม่แสดงเนื้อหาของบทสนทนา และคำสั่งนี้ไม่เขียนอะไรเลย ใช้เทียบผลก่อนและหลังปรับวิธีทำงานได้

### เลือกหัวหน้า: Claude Code, Codex หรือทั้งคู่

```powershell
zcrew enable                      # Claude Code (ค่าเริ่มต้น)
zcrew enable --commander codex    # Codex
zcrew enable --commander both     # ทั้งคู่ เปิดแอปไหนมา แอปนั้นเป็นหัวหน้า
```

ตัวอย่างการแบ่งงาน: ใช้ Claude คุมงาน core logic และใช้ Codex คุมงาน design ใน project เดียวกัน ทั้งสองสั่งช่าง ZCode ชุดเดียวกัน ถ้าสั่งงานพร้อมกัน bridge จะจองโฟลเดอร์ไว้ ช่างอีกฝั่งต้องรอคิว ไม่เขียนไฟล์ทับกัน

**Codex รู้ได้ยังไงว่าตัวเองเป็นหัวหน้า:** Codex อ่านไฟล์ `AGENTS.md` ซึ่งเป็นไฟล์เดียวกับที่ช่าง ZCode อ่าน zcrew จึงเขียน policy หัวหน้าไว้ใน **`AGENTS.override.md`** แทน Codex จะอ่านไฟล์นี้**แทน** `AGENTS.md` ส่วน ZCode ไม่อ่านไฟล์นี้ บทบาทจึงไม่ปนกัน (ทดสอบกับ Codex จริงแล้ว)

Codex ดึงไฟล์อื่นเข้ามาด้วย `@` แบบ Claude ไม่ได้ policy จึงถูกคัดลอกลงไฟล์ทั้งก้อน หลัง `zcrew update` ถ้า `zcrew status` ขึ้นว่า `policy STALE` ให้รัน `zcrew enable --commander codex` อีกครั้งเพื่อรีเฟรช

`zcrew disable` จะเอาทุกอย่างออก ถ้าต้องการเอาออกแค่หัวหน้าคนเดียว ใช้ `zcrew disable --commander codex` (หรือ `claude`)

### ไฟล์คำสั่งของ project: ไฟล์ไหน ใครอ่าน

| ไฟล์ | ใครอ่าน | ควรใส่อะไร |
|---|---|---|
| `CLAUDE.md` | **Claude** (หัวหน้า) | project นี้คืออะไร, สถาปัตยกรรม, สิ่งที่ห้ามแตะ, **คำสั่ง VERIFY** (test/typecheck/lint/build), บล็อก zcrew |
| `AGENTS.md` | **ZCode** (ช่าง) อ่านเองอัตโนมัติจากโฟลเดอร์งานและโฟลเดอร์แม่ | กฎของช่าง (ห้าม commit, ห้ามแก้นอก SCOPE, ห้ามลด test, ห้ามแตะ secret), **รูปแบบรายงาน**, คำสั่งและ style ของ project |

เริ่มจากไฟล์ตัวอย่าง:

```powershell
zcrew enable --with-templates
```

- สร้าง `CLAUDE.md` และ `AGENTS.md` จากโฟลเดอร์ `templates/` ถ้ามีไฟล์ชื่อนั้นอยู่แล้ว จะไม่เขียนทับ
- เติมช่อง `<...>` ให้ครบ แล้ว **commit `AGENTS.md`** เพราะช่างที่ทำงานขนานกันใน worktree จะเห็นเฉพาะไฟล์ที่ commit แล้ว
- `zcrew disable` จะลบไฟล์ตัวอย่างที่ zcrew สร้างให้ก็ต่อเมื่อคุณยังไม่ได้แก้ไฟล์นั้นเลย ถ้าแก้แล้วจะเก็บไว้ให้
- `zcrew status` บอกได้ว่า `AGENTS.md` ยังเป็นไฟล์ตัวอย่างเดิมอยู่หรือถูกแก้แล้ว

---

## 5. ใช้งานประจำวัน

คุยกับ Claude ตามปกติ ไม่ต้องพิมพ์ `/zcode` และไม่ต้องสั่ง ZCode เอง ตัวอย่าง:

```text
คุณ:    เพิ่ม endpoint /health ที่คืน version ของแอป
Claude: (ค้น brain → ถาม 1-2 ข้อแบบง่าย) "อยากให้ตอบเป็น JSON หรือข้อความธรรมดา? เช่น {"version":"1.2.0"}"
คุณ:    JSON
Claude: (ส่งแผน 200-300 คำ: Phase/Task/DoD + ตัวอย่างผลลัพธ์) "approve ไหม?"
คุณ:    approve
Claude: → ส่ง ZCode ทำ → ตรวจ diff + รัน test เอง → ไม่ผ่านก็สั่งแก้ใน thread เดิม
        → "ZCode แก้ routes.py แล้ว, test ผ่าน 12/12" (รายงานความคืบหน้าสั้นๆ ระหว่างทาง)
        → รายงานสรุปต่อ task
```

คำสั่งลัดที่ใช้ได้ (ตามคู่มือ THP):
- `go` / `do it` / `ship` / `YOLO`: ข้ามการถามและ approve แล้วลงมือเลย
- `ทำต่อ`: ทำงานที่ approve ไว้แล้วต่อ

เปลี่ยน model เฉพาะงานนี้ได้ในแชท เช่น `งานนี้ใช้ Flash พอ` (Claude จะใช้ `GLM-5.3-Flash` แค่ task นั้น)

### ดูว่า ZCode กำลังทำอะไร (แบบสด)

เปิด terminal อีกหน้าต่างใน project แล้วรัน:

```powershell
zcrew watch                 # ดูสดใน terminal (Ctrl+C เพื่อหยุด)
zcrew dashboard             # เปิดหน้าเว็บในเครื่อง http://127.0.0.1:8765
zcrew runs                  # รายการงานของ worker ล่าสุด
zcrew show sess_83cb        # ดูรายละเอียดงานเดียว (ใส่ต้น id ก็พอ)
```

เพิ่ม `--all` ถ้าอยากดูทุก project พร้อมกัน ตัวอย่างที่เห็นใน `zcrew watch`:

```text
14:02:11 w1 sess_83cb1cd2 > started: เพิ่ม login API
14:02:15 w1 E src/auth.ts (+24 -3)          ← แก้ไฟล์
14:02:31 w1 $ npm test -> 2 failed          ← รันคำสั่ง + ผลลัพธ์
14:03:40 w1 # turn completed: 9 tool call(s), 0 error(s), 1m29s
```

worker แต่ละตัวได้ป้าย `w1`, `w2`, … ข้อมูลอ่านจากฐานข้อมูลของ ZCode ในเครื่อง (`~/.zcode/cli/db/db.sqlite`) แบบอ่านอย่างเดียว จึงเห็นงานที่สั่งจากแอป ZCode หรือรันใน worktree ด้วย ไม่แสดงความคิดของ model หรือ output เต็มๆ หน้า dashboard เปิดได้เฉพาะจากเครื่องตัวเอง ถ้าวันหนึ่ง ZCode เปลี่ยนวิธีเก็บข้อมูล จะขึ้นข้อความเตือน 1 บรรทัดและเปลี่ยนไปใช้โหมดช้าแต่แม่นยำ แทนที่จะแสดงตัวเลขผิด

---

## 6. ตั้งค่า model และขีดจำกัด

ลำดับการอ่านค่า (ค่าที่อยู่บนกว่าชนะ):

1. `<project>\.claude\zcode-commander.json`: เฉพาะ project
2. `~/.zcode-commander/config.json`: ค่ากลางของเครื่อง
3. ค่า default ในตัวโปรแกรม

```json
{
  "model": {"providerId": "account:zai-individual-coding-plan", "modelId": "GLM-5.3"},
  "thoughtLevel": "max",
  "maxWorkers": 5,
  "maxCorrectionRounds": 4,
  "timeoutSeconds": 1800
}
```

| ค่า | ความหมาย | ค่าที่รับได้ |
|---|---|---|
| `model.providerId` | แพลนใน ZCode (รูปแบบ `account:...`) | ต้องเป็นแพลนที่คุณมีสิทธิ์ใช้ |
| `model.modelId` | ชื่อ model | ต้องอยู่ในแพลนนั้น เช่น `GLM-5.3`, `GLM-5.3-Flash` |
| `thoughtLevel` | ระดับการคิด | `high`, `max` |
| `maxWorkers` | จำนวน ZCode ที่รันพร้อมกันได้ | 1-5 |
| `maxCorrectionRounds` | จำนวนรอบสั่งแก้ต่อ task ก่อนหยุดมาถามคุณ | 1-10 |
| `timeoutSeconds` | เวลาสูงสุดต่อ run | 60-86400 |

ถ้าไม่มี `model` ใน project config ระบบจะใช้ `model` จากชั้นถัดไปทั้งก้อน

เช็คว่าตั้งถูกไหม:

```powershell
zcrew config                              # ค่ากลาง
zcrew config --project C:\path\to\project
```

**เปลี่ยน model ในอนาคต** เช่นมี GLM-6: แก้ `modelId` ใน `~/.zcode-commander/config.json` แล้วรัน `zcrew config` ถ้า model ยังไม่อยู่ในรายการของ ZCode เวอร์ชันนี้ จะขึ้นเตือนทันที หรือบอก Claude ว่า "เปลี่ยน default เป็น GLM-6" ก็ได้ Claude จะแก้ไฟล์และเช็คให้

---

## 7. อ่านผล doctor

| บรรทัด | ถ้าไม่ OK |
|---|---|
| `python` / `git` / `claude` | ติดตั้งให้อยู่ใน PATH |
| `bridge` | รัน `zcrew update` |
| `zcode bundle` / `zcode runtime` | ยังไม่ได้ติดตั้ง ZCode หรือติดตั้งไว้ที่แปลก ให้ตั้ง env `ZCODE_CLI_BUNDLE` และ `ZCODE_BINARY` |
| `zcode version` `[WARN]` | ZCode ไม่ใช่ 3.14.0 ที่ทดสอบไว้ ใช้ต่อได้ แต่ควรลองงานเล็กๆ ก่อน (ดูข้อ 8) |
| `ZCode desktop config` | เปิดแอป ZCode แล้ว login ให้เรียบร้อยสักครั้ง |
| `[INFO] ZCode desktop providers` | แสดงแพลนที่มี key (ไม่แสดง key) |
| `ZCode CLI config: model.main is not set` | login แอป ZCode แล้วรัน `zcrew update` |
| `zcode app-server` | เครื่องยนต์ ZCode เปิดไม่ขึ้น ดูข้อความ error แล้วเทียบกับข้อ 10 |
| `bridge probe` | bridge หา ZCode ไม่เจอ ดูข้อ 10 |
| `Claude MCP registration` | รัน `zcrew update` แล้ว restart Claude |
| `Commander policy` | รัน `zcrew update` |
| `commander config` | ค่าใน config.json ผิด ข้อความจะบอกว่าผิดตรงไหน |

---

## 8. อัปเดต

**อัปเดต zcrew:**

```powershell
zcrew update
```

คำสั่งนี้ดึงเวอร์ชันล่าสุดแล้วติดตั้งซ้ำ ไฟล์ config ของคุณจะไม่ถูกเขียนทับ ถ้าเคยแก้ไฟล์ใน `~/.zcrew/src` เอง คำสั่งจะหยุดไม่อัปเดตให้ หลังอัปเดตให้ restart Claude Code

**อัปเดตแอป ZCode:** ⚠️ ระวัง ZCode เปลี่ยนวิธีทำงานเบื้องหลังได้โดยไม่ประกาศ (เคยเปลี่ยนมาแล้วในรุ่น 3.12) หลังอัปเดตให้ทำ 2 ขั้นนี้:

1. รัน `zcrew doctor` ถ้าขึ้น `[WARN] zcode version` แปลว่ายังไม่เคยทดสอบกับเวอร์ชันนี้
2. ให้ Claude ลองงานเล็กๆ ใน repo ทดสอบก่อนใช้กับงานจริง ถ้าพัง ให้ดูข้อ 10 หรือกลับไปใช้ ZCode 3.14.0

---

## 9. ถอนการติดตั้ง

```powershell
zcrew disable C:\path\to\each\enabled\project   # ทำกับทุก project ที่เคย enable
zcrew uninstall                                  # ถามยืนยันก่อน; --yes ข้ามการถาม, --keep-config เก็บ config ไว้
```

`uninstall` จะเอา MCP, policy, bridge, คำสั่ง `zcrew` และรายการใน PATH ออก ถ้าใส่ `--yes` จะลบ `~/.zcrew` ให้ด้วย ถ้าไม่ใส่ จะบอกคำสั่งให้ลบเอง

`~/.claude/CLAUDE.md` จะถูกแก้ก็ต่อเมื่อเจอบรรทัด import แบบเก่าของ v0.1.x เท่านั้น

---

## 10. แก้ปัญหา

ปัญหาทั้งหมดนี้เจอจริงบนเครื่อง Windows + ZCode 3.14 ตอนพัฒนา และแก้ไว้แล้วใน v0.2.0 ถ้าเจออีก แปลว่าอะไรบางอย่างเปลี่ยนไป

| อาการ | สาเหตุ | ทางแก้ |
|---|---|---|
| `install.ps1` หยุดที่ `No MCP server named "zcode_executor"` | Windows PowerShell 5.1 หยุดสคริปต์เมื่อคำสั่งภายนอกพิมพ์ stderr | แก้ไว้แล้วใน v0.2.0 ให้รันด้วยเวอร์ชันล่าสุด |
| probe: `ZCode runtime not found` | bridge ต้องมี `ZCODE_APP_PATH` ก่อน | launcher ตั้งให้อัตโนมัติ ให้รัน `zcrew update` |
| app-server ปิดตัวทันที: `无法定位 CLI ZCode Built-in Provider Config` | ZCode หา `zcode-builtin.json` ผิดที่ | launcher ตั้ง `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` ให้ ถ้ายังพัง ให้เช็คว่ามีไฟล์ `<ZCode>\resources\config\provider\zcode-builtin.json` |
| `Unrecognized key: "runtimeModel"` | ZCode 3.12 ขึ้นไปไม่รับค่านี้แล้ว | shim ปิดให้แล้ว ให้เช็คว่า `~/.zcode-commander/bridge_compat.py` มีอยู่ |
| `Select a model before continuing` / `Provider Registry 中不存在 Model` | ZCode แบบเบื้องหลังไม่มีสิทธิ์ใช้ Coding Plan | shim ยื่นสิทธิ์ให้แล้ว ให้เช็คว่าแอป ZCode login ด้วย individual Coding Plan และ `model.providerId` เป็น `account:zai-individual-coding-plan` |
| `Reasoning level is required for ...` | ZCode บังคับให้ระบุระดับการคิด | shim ใส่ค่าจาก `thoughtLevel` ให้ (default `max`) |
| ลบ worktree ไม่ได้: `Permission denied` / `busy` | app-server ยังเปิดโฟลเดอร์ค้างอยู่ | `git worktree prune` แล้วปล่อยโฟลเดอร์ว่างไว้ ลบได้หลังปิด Claude |
| งานไม่ขึ้นในแอป ZCode | แอปโหลดรายการงานเฉพาะตอนเปิด | ปิดแล้วเปิดแอป ZCode ใหม่ |
| Claude บอกว่ารัน `install.ps1` ให้ไม่ได้ | ระบบความปลอดภัยของ Claude Code บล็อกการติดตั้งของถาวรนอก project | รันคำสั่งใน PowerShell ของคุณเอง |
| session ใหม่ของ Claude ไม่เห็น `zcode_executor` | ยังไม่ได้ restart หลังติดตั้ง | Quit แอป Claude ให้สนิทแล้วเปิดใหม่ |
| อยากปิดความสามารถบางส่วนของ shim | ใช้ทดสอบหรือหาสาเหตุ | env: `ZCODE_COMMANDER_ACCOUNT_PROVIDER=off`, `ZCODE_COMMANDER_RUNTIME_MODEL=on`, `ZCODE_COMMANDER_DEFAULT_REASONING=high` |

ดู log ของ ZCode เองได้ที่ `~/.zcode/cli/log/`

---

## 11. ข้อจำกัดที่รู้แล้ว

- ใช้ได้บน **Windows เท่านั้น** และทดสอบกับ **ZCode 3.14.0** + **Z.ai individual GLM Coding Plan** เท่านั้น
- แพลน Start / Team / Off-peak ใช้แบบเบื้องหลังไม่ได้ เพราะต้องยืนยันผ่านแอป (เช่น captcha หรือ key ที่เข้ารหัสไว้)
- **ดูสดในแอป ZCode ไม่ได้** เพราะแอปคุมเครื่องยนต์ของตัวเอง ให้ใช้ `zcrew watch` / `zcrew dashboard` แทน (แอป ZCode ดูย้อนหลังได้หลังเปิดแอปใหม่)
- `zcrew watch` / `dashboard` อ่านโครงสร้างฐานข้อมูลภายในของ ZCode ซึ่งไม่ใช่ API ทางการ ถ้า ZCode อัปเดตแล้วโครงสร้างเปลี่ยน คำสั่งจะแจ้ง "schema changed" แทนการ crash
- ยังไม่เคยทดสอบว่า Claude ใน session ใหม่ที่เปิด policy ไว้ ทำตาม flow ครบเองทั้งหมด (ครั้งแรกที่ใช้จริงนับเป็นการทดสอบ) ถ้า Claude ข้ามขั้นถามหรือขั้น approve ให้ปรับ `policy/COMMANDER.md`

---

## 12. โครงสร้าง repo และการพัฒนาต่อ

```text
zcrew/
├─ get.ps1                ← ตัวติดตั้งคำสั่งเดียว
├─ LICENSE                ← Apache-2.0
├─ README.md               ← คู่มือภาษาอังกฤษ (docs/README.th.md = ไฟล์นี้)
├─ SPEC-RFD.md             ← ทำไมถึงออกแบบแบบนี้ + ผลทดสอบจริง (§26)
├─ CHANGELOG.md            ← การเปลี่ยนแปลงแต่ละเวอร์ชัน
├─ RESEARCH-NOTES.md       ← prior art ที่ใช้อ้างอิง
├─ NOTICE                  ← เครดิตโค้ดของบุคคลที่สาม
├─ manifest.json           ← เวอร์ชัน, ZCode ที่ทดสอบแล้ว, bridge commit ที่ pin ไว้
├─ config/default-config.json
├─ policy/COMMANDER.md     ← คำสั่งที่ Claude อ่าน (หัวใจของระบบ)
├─ templates/              ← CLAUDE.md + AGENTS.md ตัวอย่างสำหรับ project
├─ examples/worker-contract.md
├─ scripts/
│  ├─ zcrew.py                    ← คำสั่ง zcrew
│  ├─ install.ps1 / uninstall.ps1
│  ├─ zcode_bridge_launcher.py   ← หา ZCode, ตั้ง env, เรียก shim
│  ├─ bridge_compat.py           ← แก้ให้ bridge ใช้กับ ZCode 3.14 ได้โดยไม่ fork
│  ├─ commander.py               ← enable/disable/status/config
│  └─ doctor.py                  ← ตรวจความพร้อม (ไม่เสีย quota)
└─ tests/                  ← unittest (stdlib ล้วน)
```

รัน test:

```powershell
python -m unittest discover -s tests -v
```

**กฎเวอร์ชัน** (SPEC ข้อ 24): ถ้าเปลี่ยนเรื่องที่กระทบการออกแบบ ต้องอัปเดต `SPEC-RFD.md` และ `CHANGELOG.md` ด้วยเสมอ เช่น การแบ่งหน้าที่ Claude/ZCode, สถาปัตยกรรม MCP, วิธีตรวจรับงาน, วงจรสั่งแก้, ขอบเขตสิทธิ์ หรือสมมติฐานเรื่อง Windows

เครดิต: [coder-mcp-bridge](https://github.com/Deslord319/coder-mcp-bridge) (MIT) คือ MCP control plane ส่วนวิธีจัดการ account provider ของ ZCode 3.12+ port มาจาก [zcode-acp](https://github.com/william0wang/zcode-acp) (Apache-2.0) ดูรายละเอียดใน `NOTICE`
