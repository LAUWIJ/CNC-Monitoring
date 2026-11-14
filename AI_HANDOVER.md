# AI handover - CNC Machine Monitoring

**For an AI assistant helping someone install, adapt or extend this project. Read this first,
then README.md (user-level documentation).** Written at handover (Nov 2026), after the system had been
running in a real job shop with 13 machines (Siemens/SINUMERIK, HEIDENHAIN TNC, FANUC).

---

## 1. What it is

A small, self-hosted machine monitoring system for CNC machines:

- Polls each control once per second (Siemens via **OPC UA**, Heidenhain via **LSV2** = the DNC option,
  Fanuc via **FOCAS2**).
- Derives per machine a **state timeline** (Running, Setup/MDI, Paused, Waiting for operator, Waiting for
  robot, Stationary, Alarm, Offline) and **part cycles** (program name, start, end, cycle time, running /
  paused / waiting time, completed/aborted, sub-programs + time spent in each).
- Web dashboard (no internet/CDN needed): live machine cards, timeline (today / 8 h / 24 h / 7 days / a day),
  per-machine detail view (donut + bars of time per status, parts per program), cycle-time statistics and
  trend per program, cycle log, program search over the whole history, CSV export, JSON API.
- Pure Python (3.11+) + SQLite + standard library HTTP server. Runs on Windows or a Raspberry Pi 4.

## 2. The core idea (do not break this)

**The cycle is never derived from "the program name changed".** Many controls report the program that is
executing *right now* - during a sub-program (M98, CALL PGM, L6), an M-code/G-code macro (Fanuc O9xxx), a
cycle (CYCLE800) that name changes, but it is the same part. All cycle logic lives in one control-independent
place: `machine_monitor/engine.py` (`CycleEngine.update(sample)`). Drivers only translate their control into a
normalized `Sample` (`machine_monitor/model.py`).

- **Start**: channel RUNNING in AUTO (or UNKNOWN mode) while no cycle is open.
- **Stays open**: sub-programs, macros, feed hold, M0/M1, alarms, mode switch to JOG, network drops < 120 s.
- **End**, strongest signal first: part counter rises / program-end counter rises / program status FINISHED /
  CANCELLED (-> aborted) / NC end marker / wait rule "end" / channel in RESET for 5 s (fallback; classed
  ABORTED if this machine has ever produced a real completion signal, else COMPLETED) / offline > 120 s.
- **Main program name**: from the control when it reports one (preferred), else the first non-sub-program
  name seen at cycle start is "latched" (`subprogram_patterns` in config).
- **Counter before program end** (very common: part counted, then door opens, then M30): the cycle stays open
  until the program really ends. If the program keeps running for `counter_end_window_s` (30 s) after the
  count (M99 loop / bar feeder), the cycle is split exactly at the counter moment (`_split_at`).
- **Wait rules** (`[[machine.wait]]`): program names or blocks that mean "waiting for robot/operator".
  `action="end"`: part done when the wait starts (robot load program between parts). `action="pause"`: wait
  inside the cycle, excluded from cycle time (`Cycle.duration_s = wall time - wait_s`).
- **Partial**: a cycle opened on the first sample after (re)start or after OFFLINE while already running
  never saw its start -> result `PARTIAL` (counts as a part, excluded from cycle-time statistics).
- **Wait "end" after an idle moment**: the cycle ends when the control went idle, not when the wait program
  starts (DVF BMO: ~1 s idle between part program and robot load program showed up as "pause").
- **Noise**: cycles with < `min_run_s` (3 s) actual running time are dropped.

**Alarms are display only** (deliberate choice of the user): drivers implement `read_alarms()` ->
list of `{code, text}` or None (can't read). The collector polls it every `alarm_poll_s` (5 s), diffs it
against the open set and writes rows to the `alarms` table (`start_ts`/`end_ts`; closed on clear, on
offline and at restart). Shown only in the machine detail view (`/api/alarms?machine=`). They are NOT fed to
the engine and must not influence states or cycles. Fanuc: `cnc_rdalmmsg2`/`cnc_rdalmmsg` + `cnc_rdopmsg`;
Heidenhain: `get_error_messages()`; Siemens: A&C event subscription on the Server object +
ConditionRefresh (verified on the DNM6700: ConditionName is always `SystemCondition`, the alarm number is
at the start of the message, `700209 | [M,Door] ...` -> `split_alarm_text()` makes it the code). Tests:
`tests/test_alarms.py`. DB schema version in `PRAGMA user_version` (v1: removed unverified PLC rows,
v2: split Siemens alarm numbers).

There are 19 scenario tests in `tests/test_engine.py` that encode real situations from the shop floor.
**Run `python -m unittest discover tests` after every engine change.** When you fix a new real-world case,
add a test with the observed pattern first.

## 3. Files

| path | role |
|---|---|
| `run.py` | entry point: `python run.py`, `--demo`, `--probe <id>`, `--probe-files <id>` (read-only list of the control's program folders), `--fetch-plc <id>`, `--merge-split-cycles` (history repair: merge counter fragments + mark partial cycles) |
| `machine_monitor/transfer.py` | program transfer (see 5b): one `FileSession` per control (`FanucFiles`, `HeidenhainFiles`, `SiemensFiles`, `DemoFiles`), `TransferService` with all safety rules, background jobs, history in table `transfers` |
| `machine_monitor/model.py` | `Sample`, `ExecState`, `Mode`, `MachineState`, `display_name()` |
| `machine_monitor/engine.py` | cycle/state detection (see 2) |
| `machine_monitor/collector.py` | one thread per machine: driver -> engine -> store, reconnect with backoff |
| `machine_monitor/store.py` | SQLite: `machines`, `state_segments`, `cycles` (auto-migrates new columns). Live status in memory, persisted once a minute (SD card friendly) |
| `machine_monitor/web.py` | HTTP server + JSON API (`/api/overview`, `/timeline`, `/cycles`, `/programs`, `/search`, `/cycles.csv`) |
| `machine_monitor/static/index.html` | the whole dashboard: vanilla JS + SVG, no external libraries |
| `machine_monitor/drivers/*.py` | `siemens_opcua.py`, `heidenhain_lsv2.py`, `fanuc_focas.py` (ctypes), `simulator.py` |
| `config.example.toml` | all options with comments; copy to `config.toml` |
| `config.demo.toml` | 4 simulated machines for `--demo` |
| `start.bat`, `probe.bat` | Windows run / test one machine |
| `setup_pi.bat`, `deploy.bat`, `rollback_pi.bat`, `logs_pi.bat`, `probe_pi.bat`, `probe_files_pi.bat`, `opcua_rights_pi.bat`, `backup_pi.bat`, `fix_history_pi.bat` | Raspberry Pi installation and maintenance from the Windows PC |
| `pi/` | Pi side: `setup_pi.sh`, `after_deploy.sh`, `rollback.sh`, `backup.py`, systemd user units; `pi_settings.bat` = Pi address |

## 4. Setup for a new site

1. Python 3.11+ on the PC (`python --version`; on Windows maybe `py`). `pip install -r requirements.txt`
   (asyncua, pyLSV2). Fanuc needs no pip package.
2. `python run.py --demo` -> http://localhost:8080 to see it working without machines.
3. Copy `config.example.toml` to `config.toml`; one `[[machine]]` block per machine. Give each machine a
   stable `id` (it keys the history - changing it later splits the history); `name` can change any time.
   `enabled = false` skips a machine.
4. Per control - checklist below. Test each machine with `python run.py --probe <id>`: it prints and logs
   (`data/monitor.log`) the connection, security, and every value it reads.
5. `python run.py` (or `start.bat`). For permanent use: the Raspberry Pi route (section 6) or Windows Task
   Scheduler/NSSM.

**Only one monitor instance per machine at a time** (SINUMERIK allows very few OPC UA sessions).

### Siemens SINUMERIK (OPC UA)
- Needs the OPC UA option licensed and activated on the control, a user with read rights
  (HMI: Commissioning > Network > OPC UA), port 4840. `BadUserAccessDenied` = wrong user/password
  (or no rights); "connection refused" = OPC UA not running/licensed or wrong IP/port.
- Unencrypted login worked at our site. Encrypted: `security = "Basic256Sha256,SignAndEncrypt"` - the client
  certificate is created in `certs/` and must be moved to "trusted" on the control once.
- Paths verified on DN Solutions (Doosan) machines with SINUMERIK - may differ per software version, the
  probe shows it:
  - `/Channel/State/chanStatus`: **0 RESET, 1 ACTIVE (running), 2 INTERRUPTED**. (Some docs suggest other
    numbering - this was verified on a running machine.)
  - `/Channel/State/progStatus`: 1 interrupted, 2 stopped, 3 in progress, 4 waiting, 5 aborted/ended.
  - Main program: `/Channel/ProgramInfo/selectedWorkPProg` (full path). **`workPNameLong` is only the
    workpiece folder** - do not use it.
  - Current program: `/Channel/ProgramInfo/progName`. Current block text: `/Channel/ProgramInfo/actBlock`.
  - Parts: `actParts` and `totalParts` are enabled independently per machine (MD27880) - the driver reads
    both and counts a part when either rises (jumps > 50 = counter set by hand, ignored).
    `oldProgNetTimeCounter` rises at M30 on some machines, stays 0 on others.
  - umati / OPC UA for Machine Tools was **not** offered by these controls (driver supports it:
    `profile = "umati"`).
  - User variables from SGUD.DEF (GUDs) were **not** visible over OPC UA (`BadNodeIdUnknown`).
- The driver uses a 60 s session timeout so broken connections don't block the control's few session slots.

### HEIDENHAIN TNC (LSV2 / DNC option)
- `pip install pyLSV2`, port 19000, DNC option (#18) needed for status/program stack.
- **pyLSV2's `safe_mode` must be off** (`safe_mode=False`, done in the driver): safe mode blocks the `DNC`
  login that is required to read run status and the program stack. The driver only calls read functions.
- The control reports main and current program separately, and FINISHED / CANCELLED status.
  **In the robot cells the robot selects the next program right away, so FINISHED can be too short for the
  1 s poll** (CLEAN_VISE.H was logged ABORTED via the RESET fallback). A real abort always shows CANCELLED,
  so the driver treats STARTED -> IDLE as a normal end too. DB v3 corrected old Heidenhain cycles
  (`end_reason='RESET'`, ABORTED, no pause time -> COMPLETED).
- **Error list (alarms) is NOT readable over LSV2 on the DVF BMO's TNC**: `get_error_messages()` (R_RI
  first/next error) is answered with `T_ER_UNKNOWN_TELE` (22). pyLSV2 then returns `[]` as if there were no
  errors. The driver now only asks iTNC 530 controls (`versions.is_itnc()`, same as the commercial Chatter
  connector does) and reports "not readable" for the others; `last_error` is checked as a second guard. Alternatives:
  a PLC marker for e.g. "door open" via pyLSV2 `read_plc_memory` (address from the machine builder), or the
  TNC OPC UA option. `--probe <id>` prints `raw: ... | LSV2 status: <code> <name>` for this.
- **PLC messages on TNC 640 (built)**: detail view button "Copy from control" (PLC code number, read-only,
  `plc_fetch.py`) copies the PLC files to `data/plc_<id>/` on the monitor. The control hides `plc\language`
  and `plc\program` from the listing (and reports folders as files of 4096 bytes), but they open directly;
  their paths come from `config/plc.cfg` (`errorTable`, `mainPgm`). `plc_messages.py` builds
  `messages.json` from `PlcError.pet` (NR | ERROR text | MARKER symbol) + `plc/program/*.def`
  (`PN_error 4800[500]`, `/c PN_error_xxx &PN_error + nnn`): DN Solutions DVF BMO = 280 messages on
  M4800..M5198. The driver reads that marker range once per alarm poll (`read_plc_memory`, PLCDEBUG login)
  and shows set markers as `PLC <nr>`. Only fixed-address markers work (symbols like `A_PLC_win [150]` are
  placed by the PLC compiler and are skipped). On automatically once `messages.json` exists; `plc_messages = false` switches a machine off.
  After building the list, `plc_messages.prune()` deletes the copied files except `messages.json`, `*.pet`,
  `plc/program/*.def`, `config/plc.cfg`, `config/oem.cfg` and `filelist.txt` (also run once at start).
  **Read markers in blocks of max 128** (`MARKER_BLOCK`): one 399-marker `read_plc_memory` call is split by
  pyLSV2 into several telegrams and on this TNC 640 the groups after the first returned the wrong markers
  (phantom PT100/fixture messages, real PLC 279 door message missed). Verified with the door: M5079 = PLC 279.
  `--probe` saves a snapshot of all markers and prints which went on/off since the previous probe run.

### FANUC (FOCAS2)
- Needs the FANUC FOCAS2 library (FANUC part number **A02B-0207-K737**; not on pip, licensed - from FANUC /
  the machine builder; **not included in the repository**). Put the files in a `FOCAS/` folder in the project.
- Windows: `fwlib_path` -> `Fwlib64.dll` for 64-bit Python. **All** `fwlib*64.dll` series DLLs
  (`fwlibe64.dll`, `fwlib30i64.dll`, `fwlib0iD64.dll`, ...) must be in the same folder. `Fwlib64.dll` loads
  them by name at connect time and Windows does not search its own folder - the driver pre-loads them. Error
  `-15` = series DLL not found.
- Linux/Pi: the right `libfwlib32-linux-<arch>.so*` is picked automatically from `FOCAS/` (a Windows
  `fwlib_path` in config is ignored on Linux) -> one config.toml serves both.
- The **main** program comes from `cnc_pdf_rdmain` / `cnc_rdprgnum.mdata`; `cnc_exeprgname` is the program
  executing now (may be a macro). Part counter: macro #3901 (`part_count_macro`), must count at M30.
- **DNC (aut 10 = RMT)**: `cnc_rdprgnum.mdata` / `cnc_pdf_rdmain` keep reporting the program SELECTED in memory
  mode (a Puma 2600LY kept showing the memory-mode program while running a DNC file from the memory card; a 2600LM the same). The driver drops
  the main program in DNC (`extra.dnc`) and the engine latches the name from `cnc_exeprgname` at cycle start
  (macros like O9015 skipped) - verified 2026-11-11: the 2600LY then shows the `<...>` name from the DNC file.
- **Alarms**: on the Puma 2600LM `cnc_rdalmmsg2(type=-1 "all")` returned OK but no alarms while
  `cnc_statinfo.alarm` was set. The driver then asks per alarm type (only while the alarm flag is set), never
  gives up permanently, and adds `EMG Emergency stop` from `statinfo.emergency`. Verified working.
  `--probe` prints the return codes (`raw: statinfo alarm=.. | rdalmmsg2(all)=ret/count, ...`).

## 5. Site-specific findings worth knowing (patterns you will see elsewhere)

- **Pallet machine** (NHP5000): program ends with **M60**, which waits until the outside pallet is cleared.
  That wait must not count as cycle time. Solution: `[machine.cycle_signal] block_regex = '\bM0?60\b'` -
  the cycle ends when M60 becomes the current block, the wait shows as *Waiting for operator*.
- **Robot cells**: (a) Heidenhain + Erowa robot runs `LOAD_PRODUCT_0.H` as its own main program between
  parts -> wait rule `program='^LOAD_PRODUCT', reason="robot", action="end"`. (b) Fanuc lathe with robot
  handshake macro (O9022 suspected) inside the cycle -> `action="pause"`. Use the "sub-programs (time)" column
  in the cycle log to identify which macro is the robot wait before enabling a rule. The O9022 rule is active
  on the Puma 2600MB. **Wait rules only apply in automatic mode**: the program was held inside O9022 while the
  operator switched to JOG (`aut=5`, `run=2` hold) and it showed 3 h of "waiting for robot".
- **Counter before program end** (Lynx, GT3100, Puma 5100): counted ~4 s before M30 while the door opens.
  Before the fix every part was split into 88 s + 4 s; `python run.py --merge-split-cycles` repairs old
  history (idempotent).
- Machines being set up produce long cycles with lots of pause time; they count in the median (open idea:
  exclude setup cycles).

## 5b. Selected-machine view: enlarged card + program transfer

- **Card** (only when one machine is selected): progress of the current part against the median of the last
  10 COMPLETED cycles of that program (`store.typical()`, cached 60 s, fields `typical_s`/`last_part_ts` in
  `/api/overview`) with "ready ≈ HH:MM"; mode badge (AUTO/MDI/JOG) and "now in / held in <sub>" + line.
  Line: Heidenhain from the program stack (free); Siemens `act_block` only if already read for `block_regex`;
  FANUC N-number only with `read_sequence_number = true` (one extra `cnc_rdseqnum` per poll - off by default
  on purpose, the user does not want extra control load for display).
- **Transfer panel** next to the card (`#topRow.single`). It is a persistent element: `renderCards()` runs every
  second and must NOT rebuild it (it only calls `xferMachine()` which reacts to a machine change).
- Every operation opens its own connection and closes it (never the monitoring connection); one operation per
  machine at a time (lock). Fetch/send run as jobs; the page polls `/api/transfer/job`. Upload is a raw body
  (`POST /api/transfer/send?machine&folder&name`, PIN in header `X-Transfer-Pin`) streamed to `data/transfer_tmp/`.
- Send flow: name rules -> send allowed (`transfer = "send"`) -> PIN -> protected? -> selected/running? ->
  (job) exists? -> state `exists` -> user confirms -> `/api/transfer/replace` -> fetch old version to
  `data/transfer_backup/<id>/` -> delete -> send. The in-use check runs again right before writing.
- **INCIDENT 2026-11-11: a file probe crashed a FANUC Series 0i-D (Puma 2600LY, D6G1) with SYS_ALM198
  (software system error, power cycle needed).** The probe called `cnc_rdpdf_alldir` on drive names the control
  had NOT reported (`//CF_TEXT/`, `//HDD/` ...) and `cnc_rdpdf_drive` with `max_num` preset to 16. Both were
  removed. Rules since then: FANUC transfer is OFF unless the machine has `transfer = "read"` or `"send"`;
  only drives the control reports via `cnc_rdpdf_drive` are ever opened; never "try" paths on a FANUC.
  The 2600LY's memory card (CF_TEXT in FANUC's Program Transfer Tool) is not in its drive list - never open it
  through the program-folder functions. **It CAN be listed with the memory card functions** (verified 2026-11-11,
  0i-D, no alarm): `cnc_rdmcdfinfo(h, 1, ODBFILESTATUS*)` for the first file, then **`-1` = next file**, until an
  entry with an empty name (asking for entry 2 directly answers ret 5 - that's how FANUC's own tool does it, seen in
  the capture). `cnc_canmcdfinfo` before and after (ret 5 = nothing open, harmless).
  **Fetch / send to that card WORK (verified 2026-11-11 on the 2600LY, no alarm)** - not via a FOCAS library call but
  with the exact messages FANUC's Program Transfer Tool sends (Wireshark `cf_text.pcapng`), in
  `machine_monitor/fanuc_card.py`: own TCP connection, open `0101` body `0001`; fetch `1501` body
  `80 09 00 01` + `"0:" + name` zero-padded to 516 bytes -> `1502`, data in `1604` blocks, `1701` -> answer `1702`;
  send `1101` same body -> `1102`, data in `1204` blocks (file starts with LF, ends with `%`), `1301` -> `1302`;
  close `0201`. Machine setting `memory_card = "dnc"` shows it in the panel as **"Memory card" (`card:/`)**,
  removes the reported `CF_TEXT` drive and blocks the folder functions on it. **Overwrite** (capture
  `cf_overwrite.pcapng`): `0:` for an existing name is answered `1103`; FANUC's tool then sends the same request
  with **`10000:` instead of `0:`** -> `1102`, file replaced in one step - no delete (verified 2026-11-11 on the
  2600LY: O7778.NC 34 B -> 46 B, listed once, read back identical, no alarm). The panel's replace does the
  same (old version first copied to `data/transfer_backup/`). Delete on the card: not supported. Test commands:
  `fanuc_card_test_pi.bat` (list), `fanuc_card_fetch_pi.bat m5 0001.NC`, `fanuc_card_send_pi.bat m5 O7778.NC`.
  Do NOT use cnc_mcdp_mount/unmount or the
  async data read/punch functions (they change the control's state).
- **Verified 2026-11-11 on the Puma 2600MB (FANUC, reports `CNC_MEM` + `MEMCARD`)**: drive list, listing,
  fetch, send to `//CNC_MEM/USER/PATH1/` and to `//MEMCARD/` (DNC from the card), replace (backup + `cnc_pdf_del`
  + send). Sizes in the listing come back as round numbers (500, 1000, 12000) - probably allocation units, not
  bytes. **Verified on the Heidenhain DVF BMO**: listing (folders reported as 4 kB files are detected), send,
  replace dialog. Switch FANUC machines on one at a time: `transfer = "read"`, probe, fetch test, then `"send"`.
- **Verified 2026-11-11 on the DNM6700 (SINUMERIK 828D)**: listing, fetch, send, replace - via the standard
  OPC UA file model (Part 20), NOT the Siemens CopyFile* methods (this control has no `Sinumerik/Methods/`):
  - Browse `Sinumerik/FileSystem/...`; entries are Siemens subtypes, so file/folder is decided by members
    (Open/Read/Size = file, CreateFile/CreateDirectory = folder); the folder's methods are skipped.
  - Fetch: file node Open(1) -> Read(handle, 16384)... -> Close. Send: folder CreateFile(NAME, True) ->
    [nodeId, handle]; **the returned node id uses the name as sent ('TEST.mpf') and has no members - use the
    file as the folder lists it (upper case)**; names are upper-cased for SINUMERIK. Delete: folder Delete(nodeId).
  - Writing needs the OPC UA right **FsWrite**: the user had only `SinuReadAll` (`BadUserAccessDenied`).
    `OpcUaClient` is the OPC UA admin on these 828Ds and may grant itself rights:
    `opcua_rights_pi.bat m10` shows them, `opcua_rights_pi.bat m10 FsWrite` grants (GetMyAccessRights /
    GiveUserAccess, found at `/Methods/...` node ids, not in the browse tree).
- **Still unverified**: other FANUC series; other SINUMERIK versions.

## 5b2. Tools and tool life (step 1, Nov 2026)

- `Sample.tool` (key = tool number as the control knows it), `tool_name` (HEIDENHAIN NAME / SINUMERIK ident;
  shown FIRST - never a bare number on those controls), `tool_group` (FANUC tool life group in use).
  `Cycle.tool_times` = running seconds per tool, `tool_info` = name/group; stored in `cycles.tool_times/tool_info`.
- **FANUC** (verified on the Puma 2600LY / 0i-D and 5100LY with `probe_tools_pi.bat`, read-only,
  `machine_monitor/tool_probe.py`): `#4120` = the tool in the spindle. After a group call **T0199** it holds the
  tool the control PICKED (e.g. 101), so a group call is recognised by `cnc_rdtlusegrp().use` (0 = direct call
  like T0101). Groups: `cnc_rdngrp`, `cnc_rdtlgrp` (one group per call), `cnc_rdtltool` (buffer of exactly the
  asked size) - functions/structs from FANUC's `fwlib32.h`. Per tool status: 1 available, 2 life expired,
  3 skipped. `use_tool` = POSITION in the group. Left = (life - count) on the tool in use + life x other available
  tools (`tool_life.group_left`; 2600LY: life 9, count 1, T101 expired, T1212 skipped -> 17 parts).
  Driver reads `#4120` each poll (`read_tool`, default on), group in use on a tool change + every 10 s, group
  life at connect and after every cycle that used a group -> table `tool_life` (one row per CHANGE).
- **SINUMERIK**: `/Channel/State/actTNumber` + `actToolIdent` read with the other values (driver, `read_tool`).
  Catalogue entries (`/Tool/Catalogue/...[u1,i]`, i = 1..numTools) are POSITIONS, not tool numbers - map them with
  `toolNo[u1,i]`; `/Tool/Data/toolMon[u1,T]` / `toolState[u1,T]` are per TOOL NUMBER. On the DNM6700 only ~3 of
  237 tools have monitoring on - tool life is barely used there.
- **HEIDENHAIN (DVF BMO, TNC 640, pyLSV2 1.8)**: `spindle_tool_status()` and `read_data_path` give nothing, the API
  symbols have no tool number. **The PLC has it**: HEIDENHAIN's example PLC keeps the tool in the spindle in the
  double word `<Global>\SPINDLE[0].DG_TOOL_IN_SPINDLE` - D16356 or D17032 depending on the PLC version (address
  placed by the PLC compiler, so it is NOT in the .def files, only in the compiler output `MainPgm.map`). Verified
  2026-11-12: D17032 = 59 = T59 LANGFAN in the spindle. "Copy from control" also copies `MainPgm.map` (own size
  limit, ~3 MB, kept after prune). On the TNC 640 it is BINARY (`PLCCOMP V 4.10 MAPFILE`): `plc_messages.map_address`
  finds the struct member record (`D` + offset 400 for DG_TOOL_IN_SPINDLE) and the struct variable record (`SPINDLE`,
  type `s`, base 16632; the variable name comes from the sources, `Spindle[KG_S1].DG_tool_in_spindle`, default
  SPINDLE) -> 16632 + 400 = D17032, matches. A first, too loose text search picked "B01" - fixed.
  **DVF Erowa (TNC 640, other PLC version): no MainPgm.map on the control**, only the compiler LISTING
  `MainPgm.lst` (`PLCCOMP V6.21`, ~7 MB; copied since the .map/.lst size limit is 40 MB, kept after prune). The
  listing gives struct members WITHOUT an address (`1638 DG_tool_in_spindle D`; the struct `Spindle` is at IS15968),
  but where the code takes the symbol's address (`PS K&SPINDLE[0].DG_TOOL_IN_SPINDLE`) the compiled bytes that follow
  hold it as a constant: `C7 04 02 E4 3F 00 00` = 0x3FE4 = **16356** -> `plc_messages.listing_addresses`. Probe
  confirmed D16356 = 3 = T3 VF-8.0-UNI. Several different addresses -> stored as `candidates`; the driver keeps the
  one holding the tool with the newest LAST_USE in tool.t (only if exactly one matches) - the user's idea. A fixed
  list of guessed addresses was rejected by the user ("the addresses will be different on any machine").
  `plc_messages.build_symbols()` writes `plc_symbols.json` (kept after prune, rebuilt at start); machine setting
  `tool_in_spindle = "D17032"` overrides, `false` switches off. The driver reads it every poll (one PLC read, like
  the PLC messages); 0 = empty spindle. Names + life come from `TNC:\table\tool.t` (LSV2 only transfers whole
  files, ~30 kB; one-field `read_data_path` doesn't work on the TNC 640). **When the table is read (agreed with the
  user - polling it was rejected):** at connect; at the START of a part only after a setup (manual/MDI mode seen,
  or another main program) - tool numbers are often reused with new names at setup; at the END of every part
  (collector -> `read_tool_life`, driver flag `tool_life_after_every_cycle`; min 20 s between reads); and for an
  unknown tool number (max once a minute). Tools with TIME1 (else TIME2) set are stored in `tool_life` as a
  one-tool group with number **-T** (count_type minutes, life = TIME1, count = CUR_TIME, TL = L -> expired).
  `store.tool_status` turns minutes into parts with the tool's median running time per part and takes off the
  tool's running time in the current part (`cycle_tool_times` in the live snapshot) until the end-of-part read
  corrects it. On the BMO only T9 has a life time (and it was far past it: CUR_TIME 1722 of 32 min).
- **HEIDENHAIN iTNC 530 (VC630)**: the control's own LSV2 functions work here (not on the TNC 640):
  `spindle_tool_status()` answers (ToolInformation; tool number read from `.number`) and `read_data_path` reads
  ONE tool table field (`/TABLE/TOOL/T/9/NAME` -> 'VLAKFREES-44'; TIME1 -> 0; CUR_TIME -> None, so `CUR.TIME` is
  tried too). `TNC:\table\tool.t` can't be read there (other path) and isn't needed: number every poll, NAME once
  per tool (cache cleared at the start of a part after a setup), TIME1/TIME2/CUR at the end of a part for the tools
  it used. No PLC address involved. Verified 2026-11-12 (probe m3): spindle_tool_status ->
  {_number: 39, _index: 0, _axis: 'Z', _length, _radius, _name: ''} (name always empty -> NAME field); CUR_TIME ->
  None but **CUR.TIME -> 1687**; TIME1/TIME2 -> 0; TL -> False (a bool).
  **Route choice by capability, not by type name (user's choice):** at connect the driver asks
  `spindle_tool_status()` once; tool data back -> direct route, None/error -> PLC route (`_spindle_status_works`).
  A `tool_in_spindle` setting forces the PLC route. Logged at connect and shown by the tool probe.
- Dashboard: card line "Tool life N parts left · group g (T...)" = the group with the least left of the groups
  the running program used in its last 10 cycles (`store.tool_status`); detail view "Tools in <program>":
  median running time per tool per part, sister tools of a group combined into one row.
- No manual tool life counting (user's decision): tools without control-side life show "not managed".
- Planned: step 3 - "can these jobs run tonight" check against group life.

## 5b2b. FANUC M00/M01 stop (2026-11-13, not yet verified)

- Puma 2600: waiting at M01 (orange light on the machine) still showed green - `cnc_statinfo.run` stays 3 (STRT)
  there. The driver now reads PMC F9 (`pmc_rdpmcrng`, type F, byte) when run = 3 and motion = 0: DM00 = F9.7,
  DM01 = F9.6 -> PAUSED, `extra.m_stop`. Read-only; switched off after 5 failed reads; setting
  `read_m00_signals = false`. `extra` also shows `mstb` now (for the probe).

## 5b3. MTConnect driver (Nov 2026, being tried on the DMG MORI CLX 450 TC, m13)

- `machine_monitor/drivers/mtconnect.py`, `control = "mtconnect"`: standard library only (urllib + ElementTree), GET
  `http://host:port[/device]/current` every poll, namespace-agnostic parse (`parse_current` -> rows), mapping in
  `sample_from_rows` (see the module docstring). Path choice: `path = "..."` else the first component with an
  Execution. Conditions Fault/Warning -> `read_alarms` (display only). `probe()` tries ports 5000/7878/5001/8082/80,
  prints the header, every data item and 20 s of samples. Tests: `tests/test_mtconnect.py` (made-up agent XML).
- Not verified on a machine yet: which data items DMG MORI's agent offers (e.g. whether PartCount and the MAIN
  program are there) decides how good cycle detection is; without PartCount the engine falls back to
  Execution PROGRAM_COMPLETED / READY.

## 5c. Month / year views: daily summary

- Tables `daily_state` (machine, local day, state, seconds), `daily_prog` (machine, day, program, cycles /
  completed / aborted / parts) and `daily_done` (days summarised). **Derived data** - can always be dropped and
  rebuilt (`store.invalidate_daily(since)`; `merge_split_cycles`/`mark_partial_cycles` already call it).
- A background thread in `run.py` runs `store.refresh_daily()` at start and every 10 min: every complete local
  day (15 min after midnight) that isn't summarised yet. The first run after an update summarises all history.
- `store.daily(t0, t1)` / `store.summary(t0, t1)`: whole summarised days from the tables, the partial first day
  and today live from `state_segments`; days before the first recorded data are skipped. `/api/overview` uses
  `summary()` for every range (identical result to the old per-segment sum - tested), `/api/daily` feeds the
  per-day timeline. Results for periods > 2 days are cached 60 s in `web.cached()` (page polls every 3 s).
- **A cycle belongs to the day it ENDS** (`program_stats` now also selects by end time - counts add up per day).
- Dashboard period picker (`#per`, next to the quick buttons Today / 8 h / 24 h / 7 days): one "📅 Period"
  button with Rolling (last 7/30/90 days, 12 months - whole local days incl. today), Calendar (this/last week,
  month, quarter, year; weeks start Monday, ISO week numbers) and Custom (from-to, same date = one day).
  ◀ ▶ step by the size of the selected period (from "Today" by whole days). The period is in the URL
  (`?period=2026-10`, `2026-Q4`, `2026`, `week-2026-11-09`, `last-30d`, `2026-11-10`, `2026-11-01_2026-11-05`)
  so it can be bookmarked or used on a shop-floor screen; rolling periods keep rolling.
- Periods > 8 days draw one stacked
  bar per day (RUNNING at the bottom), cycle log limited to 300, data refresh every 5 min instead of 30 s.
- Demo (desktop): 1 year of 13 machines / 4.8 M segments took ~3 s per query without the summary (Pi ~15-25 s);
  with it, a year is ~0.03 s.

## 5d. Storage and when to add a time series database (decision, Nov 2026)

- Current data (state periods, cycles, alarms, transfers) is small and irregular: ~300-400 kB/day for 13
  machines, ~150 MB/year. SQLite fits it; a TSDB would not be meaningfully smaller (text-heavy, irregular rows)
  and TimescaleDB/PostgreSQL alone takes ~50 MB empty. Long periods are made fast by the daily summary (5c).
- **Add a TSDB once continuous numeric signals are logged** (feed rate, spindle speed, spindle load, overrides,
  axis loads, power) several times per second: SQLite needs ~30-50 bytes per value, a TSDB ~1-3 bytes, plus
  built-in downsampling, retention and time functions. Add it NEXT TO SQLite for those signals; keep SQLite as
  the default so the system stays install-free to try (`python run.py --demo`).
  - TimescaleDB (PostgreSQL extension): if you want one SQL database for everything long-term. Windows installer
    available for testing; tests would then need a PostgreSQL instance.
  - VictoriaMetrics: if you only want signals beside SQLite; light, single binary, also has 32-bit ARM builds.
  - The Pi runs **32-bit** Raspberry Pi OS because FANUC only ships a 32-bit ARM FOCAS library
    (`libfwlib32-linux-armv7`); the 64-bit FOCAS libraries are x86-64 only. Most TSDBs need 64-bit. A 64-bit route:
    64-bit Pi OS with the monitor in a 32-bit container, or an x86 mini PC (then `linux-x64` / `Fwlib64.dll` work).
- Compression in these databases is lossless; detail is only lost by choice (downsampling + retention).
  Compressed old data is slower/limited to UPDATE - mind history repairs.
- Planned next: storage watch - warn in the dashboard from ~80 % disk use, offer an export per month of the
  oldest detail (state periods first, cycles later - medians need them), then remove detail of days that are
  already in the daily summary (never automatic), VACUUM while there is still room. Count backups too.

## 6. Raspberry Pi (permanent installation)

- **Raspberry Pi 4, Raspberry Pi OS Lite (32-bit)** - in Raspberry Pi Imager under "Raspberry Pi OS (other)".
  32-bit is required because the FANUC Linux library only exists as `armv7` (32-bit). The Pi 4 runs a 64-bit
  kernel even on the 32-bit OS: `uname -m` says `aarch64`, check `dpkg --print-architecture` = `armhf`.
- Imager: hostname, user, SSH on (password is fine, `setup_pi.bat` installs a key), wired network, Pi
  Connect not needed. A USB SSD/HDD instead of an SD card is recommended (powered drive if it is a 2.5" HDD).
- Set the Pi's address in `pi/pi_settings.local.bat` (`set PI=user@ip`; not in Git - `pi_settings.bat` only holds a
  placeholder and calls the local file), then run `setup_pi.bat` once from the PC:
  SSH key, upload, optional copy of the PC's history database, apt packages (cryptography etc. from Debian -
  no compiling), venv with `--system-site-packages`, systemd **user** service (+ linger, starts on boot,
  restarts on crash), nightly DB backup 02:15 (14 days).
- Workflow afterwards: the Windows folder is the master copy. Edit there, then `deploy.bat` (packs code +
  `config.toml`, never `data/`, keeps the previous version, restarts, shows the log). `rollback_pi.bat`,
  `logs_pi.bat` (copies the log to `data\monitor_pi.log`), `probe_pi.bat <id>`, `backup_pi.bat`.
- A pip warning "pyopenssl requires cryptography<45" appeared during setup; OPC UA worked regardless.
- Dashboard on a shop-floor screen (e.g. a Chromebox): just open `http://<pi-ip>:8080` full screen.
  `?machine=<id>` opens a machine's detail view, `?q=<text>` a program search.

## 7. Version control

The project is published under the **Apache License 2.0** (`LICENSE`; until 2026-11-14 it was AGPL-3.0-or-later).
`NOTICE.md` covers the FANUC FOCAS2 library, third-party packages and trademarks. The dashboard footer links to
the source (`/api/about`, `SOURCE_URL` in `machine_monitor/__init__.py`, override with `[server] source_url`). `.gitignore` keeps out
everything site-specific or licensed: `config.toml`, `pi/pi_settings.local.bat`, `data/` (database, copied PLC
files - those belong to the machine builder), `backups/`, `certs/`, `*.pcapng` recordings, `FOCAS/*` (only
`FOCAS/README.txt` is tracked). `.gitattributes` forces LF for `*.sh`, `*.service`, `*.timer`, `*.py` (they run
on the Pi - CRLF would break bash) and CRLF for `*.bat`. New sites: `GETTING_STARTED.md`. Keep site details
(IP addresses, customer program names, real NC programs) out of code, tests and docs.

## 8. Working agreements that proved useful

- **Ask for (or read) the probe output / log instead of guessing** control variable paths. Controls differ
  per software version and builder.
- Verify with real data: copy `data/monitor.db` and query it; most bugs were found by looking at actual cycle
  rows (e.g. the 88 s + 4 s split).
- When editing files on someone's PC through tools, make the edit, *then* copy, *then* verify the copy -
  never in parallel (a copy that ran before the edit finished caused confusing "reverted" files).
- Dashboard design: state colours carry meaning and always come with a text label; the small bar on each
  card is a proportion of the period, not a timeline; avoid pies for close values (the donut in the detail
  view is deliberately paired with exact bars).

## 9. Open ideas (not built)

- Show alarms on the timeline / cards (now only in the detail view, by choice).
- Exclude setup cycles from statistics.
- Settings page in the dashboard (machines, wait rules) instead of editing `config.toml`.
- Shop-floor TV view (large tiles).
