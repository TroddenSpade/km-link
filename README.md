# km_link_switch

A dependency-free Python script that reproduces **Mac KM Link's Alt+S switching**
by talking to the OTi "Smart KM Link" USB cable directly — no vendor app, no
network.

- Script: [`km_link_switch.py`](km_link_switch.py) (Windows, Python 3.9+, stdlib only)
- Script: [`km_link_switch_linux.py`](km_link_switch_linux.py) (Ubuntu/Linux, Python 3.8+, stdlib only)
- Autostart: `install-autostart.ps1` (Task Scheduler) / `install-autostart.sh` (systemd)
- Undo it all: `uninstall-autostart.ps1` / `uninstall-autostart.sh`

Quick start, and the map of the whole project: [`../README.md`](../README.md).
This file is the detail: every command, and every measurement behind it.

| file | what it is |
|---|---|
| `km_link_switch.py` | Windows: hooks + Raw Input, `IOCTL_SCSI_PASS_THROUGH_DIRECT` |
| `km_link_switch_linux.py` | Ubuntu: `evdev` + `EVIOCGRAB`, `SG_IO` |
| `install-autostart.ps1` / `.sh` | run it at logon / boot; asks the `role` question |
| `uninstall-autostart.ps1` / `.sh` | stop and remove it again (`-Stop`, `-Purge`, `-DryRun`, `-Status`) |
| `kmlink.conf` | written by the installer: `role`, `auto`, `peer_timeout`, `hb_interval` |
| `kmlink-run.cmd` / `kmlink-run.sh` | launcher for `run`, for when you want it in a console |
| `tests/` | six suites, run on any OS, about a second each |

---

## Using it

**Start it** (needs Administrator; it asks for it itself):

```powershell
python km_link_switch.py run          # or double-click: kmlink-run.cmd
```

A console window opens and prints `[i] remote = False`. The cable must be
plugged in; if it is not, the script says so and retries every 5 s instead of
exiting.

**Switch** — `Alt+S`, exactly like KM Link:

| | |
|---|---|
| `Alt+S` | toggle which PC your keyboard + mouse drive |
| `Ctrl+Alt+F12` | backup hotkey for the same toggle |
| `Ctrl+C`, or closing the console | stop (hooks go with the process) |

While *remote*, your keys and mouse are swallowed by this PC (the other PC's own
keyboard still works, and the local pointer deliberately stays put). The console
prints `[i] remote = True` / `False` on every switch. Leftover modifiers and
mouse buttons are released automatically on the way out, so the other PC never
ends up with a stuck `Alt`.

Useful flags: `--start-remote` (boot straight into remote), `--rate 125`
(packets/second), `--keys` (print every key the hook sees).

**Start it automatically at logon** (no UAC prompt at all, because the task runs
elevated, and *no window at all*, because it runs `pythonw.exe`):

```powershell
cd C:\Users\Parsa\Desktop\code\kmlink
.\install-autostart.ps1              # register + start now
.\install-autostart.ps1 -Status      # registered / running? which role? log path
.\install-autostart.ps1 -Log         # the last 40 lines of the log
.\install-autostart.ps1 -Remove      # undo
```

Run that once per Windows install — the desktop PC, and the laptop's Windows
side (copy the folder there first). It registers a Task Scheduler entry named
*"KM Link Alt+S switcher"*: trigger *at logon*, *run with highest privileges*,
no time limit, restarted for good if it ever dies.

**Where the output goes.** `pythonw.exe` has no console at all, so everything
`run` prints — the transport it picked, each `remote = True/False`, why a
switch was refused, the peer's heartbeats with `--keys` — is appended to

```
%LOCALAPPDATA%\kmlink\kmlink.log          (C:\Users\<you>\AppData\Local\kmlink\kmlink.log)
```

with a timestamped marker at the start of every run, and rotated once at 2 MB
(the previous log is kept as `kmlink.log.1`). The installer creates the folder
and passes this path to the task (`--log`), so the same flag works by hand too:
`python km_link_switch.py run --log D:\somewhere\kmlink.log`.

Because there is no console to look at, a refused `Alt+S` (the other machine is
not answering) also **beeps** once and says why in the log; when the other
machine disappears mid-session and the input comes home, that beeps too.

## Ubuntu / Linux

[`km_link_switch_linux.py`](km_link_switch_linux.py) is the same switcher for
Ubuntu: identical packets, identical `Alt+S`, no vendor app, stdlib only. The
only thing that changes is which kernel API does each job:

| Windows | Ubuntu / Linux |
|---|---|
| `IOCTL_SCSI_PASS_THROUGH_DIRECT` | `SG_IO` on `/dev/sg*` |
| `WH_KEYBOARD_LL` / `WH_MOUSE_LL` hooks | `evdev` on `/dev/input/event*` |
| hook returns 1 to swallow a key | `EVIOCGRAB` (the kernel's own grab) |
| Raw Input (`WM_INPUT`) deltas | `EV_REL` deltas (already raw) |
| Task Scheduler at logon, elevated | systemd service, `root` |
| Administrator | `root` (`sudo`) |

Root is needed twice over: `SG_IO` wants write access to `/dev/sgN`
(`root:disk`) and `EVIOCGRAB` wants read access to `/dev/input/event*`.

```bash
cd ~/kmlink                     # wherever you copied the files
sudo python3 km_link_switch_linux.py list    # find the cable's /dev/sgN
sudo python3 km_link_switch_linux.py diag    # SG_IO + input-device report
sudo python3 km_link_switch_linux.py key a   # type "a" on the other PC
sudo python3 km_link_switch_linux.py mousetest
python3 km_link_switch_linux.py watch        # read-only, no root, no cable
sudo python3 km_link_switch_linux.py run     # the Alt+S switcher
```

`run` prints the transport it picked, then `[i] remote = False`. While *remote*
it grabs the keyboards and pointers with `EVIOCGRAB`, so this machine sees
nothing (that is the equivalent of the Windows hook returning 1); releasing
`Alt+S` again gives them straight back. Only real keyboards/pointers are
grabbed — a power button or lid switch is left alone, because grabbing those
breaks suspend. If the process is killed the grab dies with it, so the keyboard
can never stay dead. Same flags as Windows: `--start-remote`, `--rate 125`,
`--keys`, `--device /dev/sgN`.

**Start it automatically at boot** (as root, restarted if it ever exits):

```bash
sudo ./install-autostart.sh            # install + start now
sudo ./install-autostart.sh --status   # registered / running / last log lines
sudo ./install-autostart.sh --remove   # undo
./kmlink-run.sh                        # or just run it in a terminal
```

It copies the script to `/opt/kmlink/` and installs `kmlink.service`
(`Restart=always`, so booting with the cable unplugged is fine — `run` waits for
it and retries every 5 s). **Logs**: `journalctl -u kmlink -f` for the live
stream, `journalctl -u kmlink --since today` for the day — that is Linux's
equivalent of the Windows log file, and systemd keeps it bounded for you. If you
would rather have a plain file, add
`StandardOutput=append:/var/log/kmlink.log` and
`StandardError=append:/var/log/kmlink.log` to the `[Service]` section.
**Re-run the installer after editing the script**, because the service runs the
copy in `/opt/kmlink/`.

## Turning it off again

Both platforms ship a matching uninstaller; it removes exactly what its installer
created and nothing else.

```powershell
# Windows (run on every machine you installed on)
.\uninstall-autostart.ps1                # stop it + unregister the task
.\uninstall-autostart.ps1 -Stop          # only stop it; back at the next logon
.\uninstall-autostart.ps1 -Purge         # ...and delete the log file too
.\uninstall-autostart.ps1 -DryRun        # say what it would do, change nothing
.\uninstall-autostart.ps1 -Status        # task / role / process / log, right now
```

```bash
# Ubuntu
sudo ./uninstall-autostart.sh            # stop it + remove the service
sudo ./uninstall-autostart.sh --stop     # only stop it; back at the next boot
sudo ./uninstall-autostart.sh --purge    # ...and delete /var/log/kmlink.log
./uninstall-autostart.sh --dry-run       # say what it would do, change nothing
./uninstall-autostart.sh --status        # unit / script / config / log, right now
```

What they remove: the Task Scheduler entry (or `kmlink.service`), `kmlink.conf`,
and any copy of the switcher started by hand. `-Purge` / `--purge` additionally
deletes the log. Your scripts, `README.md` and the log (without `-Purge`) are
left alone, and so is everything else on the machine — no packages, no drivers,
no registry keys, no udev rules.

Stopping is always enough to get your keyboard and mouse back, on both platforms:
on Windows the hooks live in the process, and on Linux the kernel releases
`EVIOCGRAB` when the process's file descriptors close. Because `role = main`
never takes the devices anyway, on the main machine even the running service is
invisible until you press `Alt+S`.

Notes specific to Linux:

- The cable's own HID keyboard/mouse are excluded by USB id (`0ea0:2213`), so
  there is no feedback loop.
- Nothing here talks to X11 or Wayland, so it behaves the same on both.
- While this machine is *local* the script only *reads* the devices (that is how
  it sees `Alt+S`), so that keystroke also reaches the focused window here —
  Linux cannot swallow a key without grabbing the whole device, and grabbing it
  while local would freeze your own keyboard. While *remote* nothing leaks: the
  devices are grabbed, so the other PC gets every key and this one gets none.
- `watch` needs read access to `/dev/input/event*`: run it with `sudo`, or add
  yourself to the `input` group (`sudo usermod -aG input $USER`, then log out
  and back in).
- The packets, the `sg_io_hdr`/`input_event` ABI layouts, the key→HID table and
  the whole Alt+S / forward / grab state machine were verified off-line (unit
  tests plus a simulated event loop). The sysfs discovery and the `SG_IO` call
  itself have not yet been run on real Linux hardware — `diag` is written to
  report exactly what it finds there.
- Keyboards/pointers are enumerated once at start-up, so a device plugged in
  *later* is not captured. Re-run `run` (or `sudo systemctl restart kmlink`)
  after adding hardware.
- The port ships with the checks used while writing it — they run anywhere,
  Windows included, and take a second each:

  ```bash
  python3 tests/test_linux_logic.py     # packet bytes, struct ABI, key tables
  python3 tests/test_linux_sysfs.py     # cable discovery, device classification
  python3 tests/test_linux_runloop.py   # Alt+S / forward / grab state machine
  python3 tests/test_linux_presence.py  # heartbeat, presence, "never a dead input"
  python3 tests/test_status_probe.py    # status replies, kmlink.conf round trip
  python3 tests/test_shell_scripts.py   # LF endings, shell keyword balance, paths
                                        # (the .sh files cannot run on Windows)
  ```

---

## Which machine gets the keyboard/mouse?  (`role`, `auto`)

Both machines run the script, and each one keeps a small text file next to
itself, `kmlink.conf`:

```
# main      = this machine owns the keyboard/mouse at start-up
# secondary = the other machine is the one you normally drive
role = main
#
# auto = yes: the two machines send each other one tiny heartbeat packet every
# hb_interval seconds, so the script knows whether the other machine is on.
auto = yes
peer_timeout = 5
hb_interval = 2
```

**Both installers ask the role question** while installing (`auto` defaults to
`yes`):

```powershell
.\install-autostart.ps1                     # Windows: asks; -Role Secondary, -NoAuto
```
```bash
sudo ./install-autostart.sh                 # Linux: asks; --role secondary, --no-auto
```

`run` reads the file at start-up; `--role`, `--auto` / `--no-auto`,
`--peer-timeout`, `--hb-interval` and `--config FILE` override it.

### What `run` does with it

| this machine | the other machine | keyboard/mouse |
|---|---|---|
| `role = main` | on **or** off | stay here, always |
| `role = secondary` | on | are forwarded to it at start-up (that is the machine you drive) |
| `role = secondary` | off | stay here — this machine keeps its own input instead of pointing it at one that is off |
| either | `Alt+S` | move to the other machine, but only while it answers |
| either | the other machine powers off mid-session | come straight back here |

The last two rows are the point: **the keyboard and mouse are never left pointing
at a machine that is not there.** Press `Alt+S` while the other machine is off and
nothing happens (the console says why); forward to it and then switch it off and
the keyboard and mouse come back here by themselves.

### "is the other machine even on?"

Each end of this cable only sees its own end, so the two scripts tell each other:
every `hb_interval` seconds (2 s) each machine sends one **F24** keyboard report
down the cable. F24 is a key nothing on either OS uses, and the receiving side
swallows it (Windows: the low-level hook; Linux: the cable's own HID interfaces
are watched but never captured), so it is invisible on both machines.

That heartbeat *is* the presence signal:

* the other machine answered within `peer_timeout` (5 s) → it is on, `Alt+S` works;
* silence for longer → it is off: `Alt+S` is refused, and any forwarding is undone.

**This needs the script installed and running on both machines** — that is what the
installers are for. If you would rather not install it on the other side, set
`auto = no`: the old behaviour comes back, `Alt+S` switches blindly, and no
heartbeat is sent or checked at all.

Two machines booting at the same instant are fine: each one keeps heartbeating
while it waits for the other. Run with `--keys` to see `[k] heartbeat from the
other PC` arrive, and watch the transitions in the log:

```
[i] role = secondary (C:\...\kmlink.conf) -> starting remote
[i] auto = yes  (heartbeat every 2.0s, gone after 5.0s)
[i] remote = True  (keyboard+mouse go to the other PC)
[!] the other PC stopped answering: no heartbeat from the other PC for 5 s.  Is km_link_switch running there?
[i] bringing the keyboard/mouse back to this PC (press Alt+S to try again).
```

### The other way of asking (measured, not used)

The vendor DLL exports the same question as `OTiDll_GetRemotePlugInStatus`, which
asks the cable with two opcodes keyboard/mouse traffic never uses:

| opcode | reply | the vendor reads |
|--------|-------|------------------|
| `0xD8` | 8 bytes | byte 0 (as a boolean) |
| `0xF0` | 16 bytes | byte 14 |

`status` measures exactly that, so you can look for a reply byte that tracks the
other machine's power:

```powershell
python km_link_switch.py status --save on.json     # other PC ON
python km_link_switch.py status --diff on.json     # other PC OFF (or unplugged)
```

Nothing has been *seen* to flip so far, which is why `run` uses the heartbeat —
but if a reply ever does flip it is a nice second opinion that works even when
the other machine runs nothing at all.

---

## What KM Link actually does (verified on your machine)

The cable (`USB\VID_0EA0&PID_2213`) is a USB **composite** device with three
functions. Confirmed with `Get-PnpDevice` and the HID caps of each interface:

| USB MI | Class | Role |
|--------|-------|------|
| `MI_00` | USB Mass Storage — `Virtual_Link` disk + `Android Mac` CD-ROM | **SEND** channel |
| `MI_01` | HID **Mouse** (`UsagePage 1 / Usage 2`, In=5, **Out=0**) | **RECEIVE** (input from the other PC) |
| `MI_02` | HID **Keyboard** (`UsagePage 1 / Usage 6`, In=9, **Out=0**) | **RECEIVE** (input from the other PC) |

Two consequences:

1. **Receiving is free.** The cable already appears to Windows as a normal
   keyboard and mouse. Whatever the other PC forwards simply shows up here —
   no software needed on this side.
2. **Sending is the hard part.** The HID interfaces have
   `OutputReportByteLength = 0`, so you *cannot* push data to the cable over
   HID. The vendor app sends over **MI_00** using SCSI pass-through
   (`IOCTL_SCSI_PASS_THROUGH` = `0x4D004`, and its `_DIRECT` sibling `0x4D014`).

## The packet (decoded from `2208KM_HID.dll`)

The worker functions at `0x10009ac0` (mouse) and `0x10009bf0` (keyboard) build a
16-byte buffer:

| byte | value | meaning |
|------|-------|---------|
| 0 | `0xD9` | fixed |
| 1 | `0x33` mouse / `0x34` keyboard | type |
| 2..13 | payload | 12 bytes |
| 14 | `0x4F` `'O'` | magic |
| 15 | `0x54` `'T'` | magic |

Export → packet mapping:

- `OTiDll_SendHIDPacket1` → `K_SendHIDPacket_1` → `0x10009ac0` → **mouse** `D9 33`
- `OTiDll_SendHIDPacket2` → `K_SendHIDPacket_2` → `0x10009bf0` → **keyboard** `D9 34`
- `OTiDll_SendHIDPacket4/5` → other sub-functions
- `OTiDll_SendHIDPacket3, 6..10` → stubs

The vendor SCSI helper (`0x10007db0`, "DoScsiCmd_direct") builds a
`SCSI_PASS_THROUGH` (Length `0x2c`, SenseInfoLength `0x18`, DataBufferOffset
`0x50`, SenseInfoOffset `0x30`, CDB up to 16 bytes) and calls `0x4D004`; the
"no_direct" twin calls `0x4D014`.

**How the vendor transmits (found in `0x10006f00`):** it
`LoadLibraryA("wnaspi32.dll")` + `GetProcAddress("SendASPI32Command")`, then
builds a 32-bit **ASPI `SRB_ExecSCSICmd`** (`SRB_Cmd = 2`, `SRB_HaId`/
`SRB_Target`/`SRB_Lun`, `SRB_Flags` = `SRB_DIR_IN`/`SRB_DIR_OUT` |
`SRB_EVENT_NOTIFY`, `SRB_BufPointer`, `SRB_CDBLen`, `CDBByte[16]`) and submits
the 16-byte OTi packet as the **CDB**. The CDB parser at `0x1000a8b0` accepts
vendor opcodes in the range **`0xD8`..`0xF0`**, which is how the packet types
fit. So `--mode cdb` is the correct mode.

There is no `wnaspi32.dll` on this machine (the ASPI layer belonged to the KM
Link install, which is gone), so the script drives the same device with the
equivalent user-mode SCSI pass-through.

**Which pass-through works (measured on your machine):** the *buffered*
`IOCTL_SCSI_PASS_THROUGH` (`0x4D004`) is refused with
`ERROR_PRIVILEGE_NOT_HELD` (1306) machine-wide — for `\\.\C:`,
`\\.\PhysicalDrive0` and the cable alike — **even with a full Administrator
token** (Administrators group enabled, High integrity, `SeDebugPrivilege`).
`IOCTL_STORAGE_QUERY_PROPERTY` and `IOCTL_SCSI_PASS_THROUGH_DIRECT` (`0x4D014`)
both succeed, and DIRECT returns real cable data:

| handle | INQUIRY product |
|--------|-----------------|
| `\\.\D:` (CD-ROM `MacKMLink`) | `Android+Mac` |
| `\\.\E:` (FAT volume) | `Virtual Link` |
| `\\.\PhysicalDrive1` | `Virtual Link` |

So the script uses **`IOCTL_SCSI_PASS_THROUGH_DIRECT`**, which is what the
vendor helper literally is named after (`DoScsiCmd_direct`).

## Commands

```powershell
python km_link_switch.py list    # the cable's HID + storage device paths
python km_link_switch.py caps    # HID capabilities (shows Out=0, i.e. HID can't send)
python km_link_switch.py diag    # does SCSI pass-through work at all here? (needs admin)
python km_link_switch.py probe   # type a different letter over each handle (needs admin)
python km_link_switch.py key a   # press "a" on the other PC (needs admin)
python km_link_switch.py mousetest  # step through mouse packets (needs admin)
python km_link_switch.py status  # is the other machine there? (needs admin)
python km_link_switch.py watch   # read-only: show every key + raw mouse delta (no admin)
python km_link_switch.py raw D9 34 00 00 04 00 00 00 00 00 00 00 00 00 4F 54
python km_link_switch.py run     # capture local input; Alt+S toggles to the other PC
```

Options:

| option | meaning |
|--------|---------|
| `--device "\\?\usbstor#disk&ven_&prod_virtual_link..."` | exact send device (see `list`) |
| `--drive X:` | drive letter form |
| `--mode cdb` (default) / `--mode data` | is the 16-byte packet the CDB, or the CDB's data? |
| `--cdb "D8 00 00 00 10 ..."` | vendor CDB used when `--mode data` |
| `--start-remote` | start `run` already pointed at the other PC |
| `--role main\|secondary` | `run`: do the keyboard/mouse belong here or on the other PC? |
| `--config FILE` | `run`: use another `kmlink.conf` |
| `--save FILE` / `--diff FILE` | `status`: record / compare the replies |
| `--rate 125` | `run`: how many packets/second the sender thread flushes |
| `--keys` | `run`: print every key event the hook sees (with its modifier bit) |

The Linux script takes the same options and these commands:

```bash
sudo python3 km_link_switch_linux.py list | diag | key a | mousetest | watch | run
```

(`caps`, `probe` and `raw` are Windows-only: they exist to pick a SCSI handle and
inspect HID caps, neither of which exists on Linux.)

## Status

**Keyboard is confirmed working end-to-end on real hardware.**

Transport: `IOCTL_SCSI_PASS_THROUGH_DIRECT` — the cable accepts the vendor
packet as the CDB and returns `ok=True err=0 status=0 sense=00..`:

```
d9 34 00 00 04 00 00 00 00 00 00 00 00 00 4f 54   kbd 'a' press    ok=True err=0 status=0
d9 34 00 00 00 00 00 00 00 00 00 00 00 00 4f 54   kbd 'a' release  ok=True err=0 status=0
d9 33 00 05 00 00 00 00 00 00 00 00 00 00 4f 54   mouse +5         ok=True err=0 status=0
```

`probe` types a **different letter through each candidate handle**, and all of
them arrived on the other computer in one run — the observed result was
**`depcv`** (`d`=`\\.\D:`, `e`=`\\.\E:`, `p`=`\\.\PhysicalDrive1`,
`c`=`...cdrom...`, `v`=`...disk...`). So:

- the packet layout (modifier, reserved, 6 keycodes) is correct;
- **all** handles drive the same OTi firmware — both mass-storage LUNs forward
  the vendor CDB, so no specific drive letter is required;
- **no "start"/control packet is needed** to make the other computer accept
  input.

`oti_candidate_devices()` therefore prefers the `\\?\usbstor#...disk...`
SetupDi path (re-discovered by VID/PID each run) so nothing depends on a drive
letter or on the `PhysicalDriveN` number, which differ per machine.

### Mouse: encoding resolved on hardware

`mousetest` was run against the cable, and the pointer behaviour identified
the layout exactly (my first guess of 16-bit X/Y was wrong):

| step | payload bytes sent | observed | conclusion |
|------|--------------------|----------|------------|
| `dx=+30` | `p1=1E p2=00 p3=00` | pointer right | `p1` = X |
| `dx=-30` | `p1=E2 p2=FF p3=00` | pointer left | X is signed 8-bit, `p2` = Y |
| `dy=+30` | `p1=00 p2=00 p3=1E` | **scrolled up** | `p3` = wheel |
| `dy=-30` | `p1=00 p2=00 p3=E2` | **scrolled down** | wheel sits one byte earlier than assumed |

So the mouse payload is the plain HID report, one byte per field:

| payload byte | meaning |
|---|---|
| `p0` | button bitmask (1=left, 2=right, 4=middle) |
| `p1` | X delta, signed 8-bit |
| `p2` | Y delta, signed 8-bit (positive = down) |
| `p3` | vertical wheel, signed 8-bit |
| `p4` | horizontal wheel (unobserved guess) |

A single packet can therefore only carry +/-127 px, so `mouse_move_chunks()`
splits a larger move into several packets (`dx=900` becomes 8 packets that sum
to exactly 900) instead of letting the value wrap.

### Direction: no inversion anywhere in the vendor code

The complaint that "left/right and up/down are reversed" was the *capture* bug,
not the encoding. The vendor app was checked directly:

- its relative-mouse packet is built as `s[0]=0, s[1]=X, s[2]=Y` (8-bit), i.e.
  exactly the layout in the table above;
- the helper at `0x429c02` that "normalises" X/Y looks like it negates, but it
  writes the coded value back at `0x429c2d`, so it is an identity — the app sends
  Windows screen deltas unchanged (`dx>0` right, `dy>0` down);
- it sends with `Sleep(10)` between packets (100 Hz) — a paced sender, which is
  now what `run` does too.

`mousetest` writes packets straight to the cable with no hook involved, so it is
the ground truth for the byte layout: step 1 must move the other pointer one way
and step 2 the other way.

## Why Alt+S did not toggle (and what fixed it)

A low-level keyboard hook does **not** report `VK_MENU` (0x12) for the Alt key.
Like Shift and Ctrl, the modifier keys arrive as their *left/right specific*
codes:

| key | hook reports |
|-----|--------------|
| Alt | `VK_LMENU` **0xA4** / `VK_RMENU` **0xA5** |
| Ctrl | `VK_LCONTROL` **0xA2** / `VK_RCONTROL` **0xA3** |
| Shift | `VK_LSHIFT` **0xA0** / `VK_RSHIFT` **0xA1** |

The first version only knew `0x10/0x11/0x12`, so the Alt bit was never set and
`Alt+S` never matched — while ordinary letters kept working, which is exactly
the symptom. `mod_bit_for()` now accepts both spellings (and uses
`LLKHF_EXTENDED` to disambiguate the generic Ctrl/Alt codes), and it maps
left/right to the correct HID bits:

```
0xA0 -> 0x02  left shift      0xA1 -> 0x20  right shift
0xA2 -> 0x01  left ctrl       0xA3 -> 0x10  right ctrl
0xA4 -> 0x04  left alt        0xA5 -> 0x40  right alt
0x5B -> 0x08  left GUI        0x5C -> 0x80  right GUI   (was 0x10, wrong)
```

Verified with `watch` while injecting the keys: the hook reported
`vk=0xA4 VK_LMENU DOWN mod-bit=0x04`, then `vk=0x53 'S' DOWN` — so the toggle
condition is satisfied now.

Two escape hatches were added at the same time:

- **`Ctrl+Alt+F12`** is registered with `RegisterHotKey`, so it switches even if
  the keyboard hook is having a bad day. The hook deliberately does not swallow
  that combination so the system hotkey can see it.
- **`run --keys`** prints every key event with its computed modifier bit.

`watch` is the read-only diagnostic: it installs the same hook and Raw Input
registration but forwards and swallows nothing and never touches the cable, so
it needs no Administrator rights. It prints each key (`vk`, name, up/down, the
modifier bit) and a 1.5-second summary of raw mouse reports, and it counts
*absolute* reports separately (touchpads/RDP report positions, not deltas,
which needs a different conversion).

## `run`: how forwarding is wired

`run` is the Alt+S switcher. It is not a "hook that sends" — sending from inside
a hook is what made the pointer jump around earlier:

- **Keyboard** comes from a low-level hook (`WH_KEYBOARD_LL`) because the hook
  has to *swallow* keys while you are on the other PC, and because `Alt+S` is
  detected there. Held keys are tracked as a list, so combos (Ctrl+C, ...) work.
- **Mouse movement** comes from **Raw Input** (`WM_INPUT`), which is the only
  way to get the true hardware deltas. The low-level mouse hook's `pt` is a
  screen *position*; while remote the local pointer is swallowed and therefore
  never advances, so subtracting positions produced garbage — wrong directions,
  jumping, crawling.
- A low-level mouse hook is still installed, but only to stop the local pointer
  from moving while remote.
- **No hook ever blocks on the USB transfer.** Input updates an accumulator or
  lands on a queue and a sender thread flushes it at a fixed rate
  (`--rate`, default 125 Hz). The vendor app does the same thing (`Sleep(10)`
  between its packets), and one packet measures **~0.5 ms** on this cable, so
  the link can carry far more than a mouse generates.
- Switching either way queues an **"all released"** report, so the other PC is
  never left holding a stuck `Alt` or mouse button.
- Started by hand from a console, `run` elevates itself into a **visible** console
  (`Ctrl+C` quits). Started by the logon task it runs under `pythonw.exe` with no
  console at all, so everything it would have printed goes to the log file.

## Troubleshooting

**`Alt+S` does nothing, and there is one beep.** The other machine is not
answering its heartbeat: it is off, asleep, or its copy of the script is not
running. The log names the reason:

```
[!] the other PC is not answering: no heartbeat from the other PC for 5 s.  Is km_link_switch running there?
```

That is `auto = yes` working as intended. Install the script on the other machine
too, or set `auto = no` in `kmlink.conf` to switch blindly again.

**`Alt+S` does nothing, and no beep.** The switcher is not running on this
machine at all: check `.\install-autostart.ps1 -Status` (Windows) or
`sudo ./uninstall-autostart.sh --status` (Ubuntu), then look at the log.

**The keyboard and mouse came back here by themselves.** The other machine
stopped answering mid-session (powered off, slept, cable pulled, script
stopped). Deliberate: nothing is ever left pointing at a machine that is gone.
Press `Alt+S` to go back once it is answering again.

**"Is the KM Link cable plugged in?" in the log.** The cable was not found at
start-up. `run` does not give up: it retries every 5 s, so plugging it in is
enough. Check with `python km_link_switch.py list` that the `Virtual Link` device
is there.

**Nothing arrives on the other computer.** Look for the `[i] SCSI transport on
'...'` line in the log — that is the handle it picked. Then
`python km_link_switch.py list` (the cable's device paths) and `... diag` (does
SCSI pass-through work at all here — needs Administrator). If both machines have
the script running, check that only one of them is *remote*; two ends forwarding
to each other is the one configuration that ping-pongs input, and it means one
`role` in the two `kmlink.conf` files is the wrong way round.

**The keys repeat or lag on the far machine.** The sender paces packets at
`--rate 125` (the vendor app uses 100 Hz). Both ends are plain USB at ~0.5 ms per
packet, so this is far more headroom than a keyboard needs; if something still
feels off, try `--rate 60` in a console and see.

**Ubuntu finds no cable.** Expected on the very first install: the `SG_IO` call
and the sysfs lookup have not been run on real Linux hardware yet (everything
else has). `sudo python3 km_link_switch_linux.py diag` prints exactly what it
found — if the device paths differ from what `find_oti_sg()` expects, that
output is what to adjust.

## Notes

- Close any app holding the cable (KM Link) before probing.
- `run` uses low-level keyboard/mouse hooks (`WH_KEYBOARD_LL` / `WH_MOUSE_LL`)
  plus Raw Input, and swallows `Alt+S` to toggle, exactly like KM Link.
- This cannot help before an OS boots (BIOS/GRUB): that input never reaches an
  OS.
