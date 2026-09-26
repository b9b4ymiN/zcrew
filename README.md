# Claude Commander × ZCode Executor (Windows) — v0.2.0

คุยกับ **Claude Code** คนเดียวก็พอ เรื่องลงมือเขียนโค้ด Claude จะส่งต่อให้ **ZCode (GLM-5.3)** ทำ ระหว่างทาง Claude ตรวจงานเองทุกชิ้น สั่งแก้ใน session เดิมของ ZCode ซ้ำจนผ่าน แล้วค่อยรายงานคุณ

เปรียบเทียบง่ายๆ คือบริษัทรับเหมา:

| บทบาท | ใคร | ทำอะไร |
|---|---|---|
| ลูกค้า | คุณ | บอกว่าอยากได้อะไร อนุมัติแผน |
| เจ้าของบริษัท | Claude Code (Opus) | ถามให้ชัด → วางแผน → แจกงาน → ตรวจงาน → สั่งแก้ → รายงาน |
| ช่าง (สูงสุด 5 คน) | ZCode (GLM-5.3, คิดระดับ max) | ลงมือเขียนโค้ด รัน test และแก้ตามที่สั่ง |

> ช่างตัดสินเองไม่ได้ว่างานของตัวเองผ่าน Claude ต้องดู `git diff` และรัน test เองทุกครั้ง

---

## สารบัญ

1. [ทำงานยังไง](#1-ทำงานยังไง)
2. [สิ่งที่ต้องมีก่อนติดตั้ง](#2-สิ่งที่ต้องมีก่อนติดตั้ง)
3. [ติดตั้งบนเครื่องใหม่ (ทีละขั้น)](#3-ติดตั้งบนเครื่องใหม่-ทีละขั้น)
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

ดูประวัติทุกงานที่ ZCode ทำได้ในแอป ZCode ใต้ชื่อ project (ต้องปิดแล้วเปิดแอปใหม่ รายการถึงจะอัปเดต)

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

## 3. ติดตั้งบนเครื่องใหม่ (ทีละขั้น)

### ขั้นที่ 1: เอา repo นี้ลงเครื่อง

```powershell
cd C:\Programing\PersonalAI
git clone <URL ของ repo นี้> Claude2zcode
cd Claude2zcode
```

(ถ้าไม่ได้ใช้ git ให้ copy ทั้งโฟลเดอร์มาวางแทนได้)

### ขั้นที่ 2: รันตัวติดตั้ง

เปิด **PowerShell ธรรมดา** (ไม่ต้อง Run as Administrator) ที่โฟลเดอร์ repo แล้วรัน:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1 -EnsureZCodeCliConfig
```

`-EnsureZCodeCliConfig` **ใส่แค่ครั้งแรกบนเครื่องใหม่เท่านั้น** มันคัดลอก provider ของ Coding Plan (รวม API key) จากการตั้งค่าของแอป ZCode ไปไว้ใน `~/.zcode/cli/config.json` และสำรองไฟล์เดิมไว้เป็น `.bak` API key อยู่ในเครื่องคุณ ไม่ถูกส่งไปไหน

> ถ้ารันซ้ำโดยใส่ flag นี้อีก ชื่อ provider จะกลายเป็น `...-1` ใช้งานได้ปกติแต่ชื่อไม่สวย รอบถัดไปให้รันโดย**ไม่ใส่** flag

ตัวติดตั้งทำสิ่งเหล่านี้:

1. เช็คว่ามี `git`, `python` (3.10 ขึ้นไป) และ `claude`
2. clone `coder-mcp-bridge` ไปที่ `~/.zcode-commander/coder-mcp-bridge` แล้ว **pin ไว้ที่ commit ที่ทดสอบแล้ว** (`23ecf0a`)
3. copy `zcode_bridge_launcher.py`, `bridge_compat.py`, `doctor.py`, `commander.py` ไปที่ `~/.zcode-commander/`
4. copy policy ไปที่ `~/.claude/zcode-commander/COMMANDER.md` (copy ไว้เฉยๆ ยังไม่เปิดใช้ที่ไหน)
5. สร้าง `~/.zcode-commander/config.json` จากค่า default (ถ้ามีไฟล์อยู่แล้ว จะไม่เขียนทับ)
6. ลงทะเบียน MCP server `zcode_executor` ใน Claude Code (scope `user`)
7. รัน doctor

**ไม่แตะ** `~/.claude/CLAUDE.md` (คู่มือกลางของคุณ) เลย

### ขั้นที่ 3: อ่านผล doctor

ต้องจบด้วย `Result: READY` ถ้ายังไม่ได้ ให้ดู [ข้อ 7](#7-อ่านผล-doctor) และ [ข้อ 10](#10-แก้ปัญหา)

รัน doctor ซ้ำได้ทุกเมื่อ (ไม่เสีย quota model):

```powershell
python "$HOME\.zcode-commander\doctor.py"
```

### ขั้นที่ 4: Restart Claude Code

ปิดแอป Claude **ให้สนิท** (ถ้ามีไอคอนค้างใน system tray ให้คลิกขวาแล้วเลือก Quit) แล้วเปิดใหม่ Claude จะโหลดเครื่องมือ `zcode_executor` ตอนเริ่ม session

เช็คได้จาก PowerShell:

```powershell
claude mcp get zcode_executor
```

ต้องขึ้น `Status: √ Connected`

---

## 4. เปิดใช้กับ project

policy **ไม่ทำงานทั้งเครื่อง** เปิดใช้เป็นราย project เท่านั้น project ต้องเป็น git repo เพราะ Claude ตรวจงานด้วย `git diff`

```powershell
# เปิดใช้
python "$HOME\.zcode-commander\commander.py" enable C:\path\to\project

# ดูสถานะ (เปิดอยู่ไหม, ใช้ config จากไหน, ค่าถูกต้องไหม)
python "$HOME\.zcode-commander\commander.py" status C:\path\to\project

# ปิดใช้ (ไฟล์ CLAUDE.md กลับเป็นเหมือนเดิมทุกไบต์)
python "$HOME\.zcode-commander\commander.py" disable C:\path\to\project
```

`enable` เพิ่มบล็อกนี้ต่อท้าย `<project>\CLAUDE.md` (ถ้ายังไม่มีไฟล์ จะสร้างให้ และตอน `disable` จะลบไฟล์ที่สร้างไว้ทิ้ง):

```markdown
<!-- zcode-commander:begin -->
@~/.claude/zcode-commander/COMMANDER.md
<!-- zcode-commander:end -->
```

ตัวเลือกเพิ่มเติม:
- `--with-project-config`: สร้าง `.claude/zcode-commander.json` ให้ project นี้ใช้ model/ค่าต่างจากค่ากลาง
- `--force`: เปิดใช้แม้โฟลเดอร์ไม่ใช่ git repo (ไม่แนะนำ)

หลัง enable ให้เปิด **session ใหม่** ของ Claude Code ใน project นั้น

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
        → รายงานสรุปต่อ task + บอกว่าดูประวัติเต็มได้ในแอป ZCode
```

คำสั่งลัดที่ใช้ได้ (ตามคู่มือ THP):
- `go` / `do it` / `ship` / `YOLO`: ข้ามการถามและ approve แล้วลงมือเลย
- `ทำต่อ`: ทำงานที่ approve ไว้แล้วต่อ

เปลี่ยน model เฉพาะงานนี้ได้ในแชท เช่น `งานนี้ใช้ Flash พอ` (Claude จะใช้ `GLM-5.3-Flash` แค่ task นั้น)

**ดูว่า ZCode ทำอะไรไปบ้าง:** เปิดแอป ZCode แล้วดูใต้ชื่อ project ถ้าเพิ่งมีงานใหม่ ต้องปิดแล้วเปิดแอปใหม่ก่อน ดูสดระหว่างทำงานไม่ได้ (ดูข้อ 11)

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
python "$HOME\.zcode-commander\commander.py" config                       # ค่ากลาง
python "$HOME\.zcode-commander\commander.py" config --project C:\path\to\project
```

**เปลี่ยน model ในอนาคต** เช่นมี GLM-6: แก้ `modelId` ใน `~/.zcode-commander/config.json` แล้วรัน `commander.py config` ถ้า model ยังไม่อยู่ในรายการของ ZCode เวอร์ชันนี้ จะขึ้นเตือนทันที หรือบอก Claude ว่า "เปลี่ยน default เป็น GLM-6" ก็ได้ Claude จะแก้ไฟล์และเช็คให้

---

## 7. อ่านผล doctor

| บรรทัด | ถ้าไม่ OK |
|---|---|
| `python` / `git` / `claude` | ติดตั้งให้อยู่ใน PATH |
| `bridge` | รัน `install.ps1` ใหม่ |
| `zcode bundle` / `zcode runtime` | ยังไม่ได้ติดตั้ง ZCode หรือติดตั้งไว้ที่แปลก ให้ตั้ง env `ZCODE_CLI_BUNDLE` และ `ZCODE_BINARY` |
| `zcode version` `[WARN]` | ZCode ไม่ใช่ 3.14.0 ที่ทดสอบไว้ ใช้ต่อได้ แต่ควรลองงานเล็กๆ ก่อน (ดูข้อ 8) |
| `ZCode desktop config` | เปิดแอป ZCode แล้ว login ให้เรียบร้อยสักครั้ง |
| `[INFO] ZCode desktop providers` | แสดงแพลนที่มี key (ไม่แสดง key) |
| `ZCode CLI config: model.main is not set` | รัน `install.ps1 -EnsureZCodeCliConfig` |
| `zcode app-server` | เครื่องยนต์ ZCode เปิดไม่ขึ้น ดูข้อความ error แล้วเทียบกับข้อ 10 |
| `bridge probe` | bridge หา ZCode ไม่เจอ ดูข้อ 10 |
| `Claude MCP registration` | รัน `install.ps1` ใหม่ แล้ว restart Claude |
| `Commander policy` | รัน `install.ps1` ใหม่ |
| `commander config` | ค่าใน config.json ผิด ข้อความจะบอกว่าผิดตรงไหน |

---

## 8. อัปเดต

**อัปเดต kit นี้** (หลัง `git pull` repo นี้):

```powershell
.\scripts\install.ps1
```

ไฟล์ config ของคุณจะไม่ถูกเขียนทับ หลังรันเสร็จให้ restart Claude Code

**อัปเดต bridge** ให้ใช้ commit ที่ต้องการ (ปกติไม่ต้องทำ):

```powershell
.\scripts\install.ps1 -ForceBridgeUpdate -BridgeRef <commit>
```

**อัปเดตแอป ZCode:** ⚠️ ระวัง ZCode เปลี่ยนวิธีทำงานเบื้องหลังได้โดยไม่ประกาศ (เคยเปลี่ยนมาแล้วในรุ่น 3.12) หลังอัปเดตให้ทำ 2 ขั้นนี้:

1. รัน doctor ถ้าขึ้น `[WARN] zcode version` แปลว่ายังไม่เคยทดสอบกับเวอร์ชันนี้
2. ให้ Claude ลองงานเล็กๆ ใน repo ทดสอบก่อนใช้กับงานจริง ถ้าพัง ให้ดูข้อ 10 หรือกลับไปใช้ ZCode 3.14.0

---

## 9. ถอนการติดตั้ง

1. `disable` ทุก project ที่เคย enable ไว้ก่อน (ข้อ 4)
2. รัน:

```powershell
.\scripts\uninstall.ps1                 # เอา MCP กับ policy ออก
.\scripts\uninstall.ps1 -RemoveBridge   # เอา ~/.zcode-commander ออกด้วย รวม bridge และ config
```

`uninstall.ps1` จะแก้ `~/.claude/CLAUDE.md` ก็ต่อเมื่อเจอบรรทัด import แบบเก่าของ v0.1.x เท่านั้น ถ้าไม่เจอ จะไม่แตะไฟล์นี้เลย

---

## 10. แก้ปัญหา

ปัญหาทั้งหมดนี้เจอจริงบนเครื่อง Windows + ZCode 3.14 ตอนพัฒนา และแก้ไว้แล้วใน v0.2.0 ถ้าเจออีก แปลว่าอะไรบางอย่างเปลี่ยนไป

| อาการ | สาเหตุ | ทางแก้ |
|---|---|---|
| `install.ps1` หยุดที่ `No MCP server named "zcode_executor"` | Windows PowerShell 5.1 หยุดสคริปต์เมื่อคำสั่งภายนอกพิมพ์ stderr | แก้ไว้แล้วใน v0.2.0 ให้รันด้วยเวอร์ชันล่าสุด |
| probe: `ZCode runtime not found` | bridge ต้องมี `ZCODE_APP_PATH` ก่อน | launcher ตั้งให้อัตโนมัติ ให้รัน `install.ps1` ใหม่ |
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
- **ดูสดในแอป ZCode ไม่ได้** เพราะแอปคุมเครื่องยนต์ของตัวเอง ดูย้อนหลังได้หลังเปิดแอปใหม่
- งานที่รันใน worktree (โหมดขนาน) อาจไม่ขึ้นในรายการของแอป ZCode
- ยังไม่เคยทดสอบว่า Claude ใน session ใหม่ที่เปิด policy ไว้ ทำตาม flow ครบเองทั้งหมด (ครั้งแรกที่ใช้จริงนับเป็นการทดสอบ) ถ้า Claude ข้ามขั้นถามหรือขั้น approve ให้ปรับ `policy/COMMANDER.md`

---

## 12. โครงสร้าง repo และการพัฒนาต่อ

```text
Claude2zcode/
├─ README.md               ← ไฟล์นี้
├─ SPEC-RFD.md             ← ทำไมถึงออกแบบแบบนี้ + ผลทดสอบจริง (§26)
├─ CHANGELOG.md            ← การเปลี่ยนแปลงแต่ละเวอร์ชัน
├─ RESEARCH-NOTES.md       ← prior art ที่ใช้อ้างอิง
├─ NOTICE                  ← เครดิตโค้ดของบุคคลที่สาม
├─ manifest.json           ← เวอร์ชัน, ZCode ที่ทดสอบแล้ว, bridge commit ที่ pin ไว้
├─ config/default-config.json
├─ policy/COMMANDER.md     ← คำสั่งที่ Claude อ่าน (หัวใจของระบบ)
├─ examples/worker-contract.md
├─ scripts/
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
