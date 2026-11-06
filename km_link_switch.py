#!/usr/bin/env python3
"""
km_link_switch.py

A dependency-free script that talks to the OTi "Smart KM Link" USB bridge cable
(USB\\VID_0EA0&PID_2213) directly, WITHOUT the vendor app (Mac KM Link /
LinkEngKM).  It reproduces the same thing the vendor software does when you press
Alt+S: it forwards your keyboard/mouse to the *other* PC over that cable.

This is a script, not an app.  There is no network involved.

------------------------------------------------------------
WHAT "SWITCHING" ACTUALLY IS (decoded from 2208KM_HID.dll)
------------------------------------------------------------
The cable is a USB-to-USB bridge.  The vendor app does NOT send a "switch PC"
command.  Instead:

  * the PC that physically owns the keyboard/mouse captures them, builds a
    16-byte OTi "HID packet" and writes it to the cable;
  * the cable relays that packet to the other PC, which injects it locally.

The 16-byte packet format (confirmed by disassembly of the two worker
functions at 0x10009ac0 / 0x10009bf0 in 2208KM_HID.dll):

    byte 0       0xD9           (fixed)
    byte 1       0x33 mouse / 0x34 keyboard
    bytes 2..13  12 payload bytes
    byte 14      0x4F 'O'
    byte 15      0x54 'T'

    Mouse    : D9 33 <p0..p11> 4F 54     (OTiDll_SendHIDPacket1)
    Keyboard : D9 34 <p0..p11> 4F 54     (OTiDll_SendHIDPacket2)
    There are also packet types 4 and 5 (other sub-functions).

The 16-byte report is handed to the cable as the CDB of a SCSI pass-through
command sent to the cable's mass-storage function.  We use
IOCTL_SCSI_PASS_THROUGH_DIRECT (0x4D014): the buffered IOCTL_SCSI_PASS_THROUGH
(0x4D004) is refused with ERROR_PRIVILEGE_NOT_HELD (1306) on current Windows
even for a full Administrator token.

USAGE
  python km_link_switch.py list
  python km_link_switch.py probe
  python km_link_switch.py key a
  python km_link_switch.py raw D9 35 01 00 ... (16 hex bytes)
  python km_link_switch.py run          # capture local input, Alt+S toggles

  Common options:
    --transport hid|scsi|auto   (default auto: try HID, fall back to SCSI)
    --drive  X:                 (mass-storage drive of the cable, for SCSI)

If something doesn't reach the other PC, run `probe` and read the notes in
README.md - the payload layout for mouse/keyboard is reconstructed from
disassembly and may need one confirming capture (see README).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import struct
import sys
import threading
import time

VID = 0x0EA0
PID = 0x2213

# ---------------------------------------------------------------- packets
OP_MOUSE = 0x33
OP_KEYBOARD = 0x34


def oti_packet(sub: int, payload: bytes = b"", op: int = 0xD9) -> bytes:
    """Build a 16-byte OTi packet: <op><sub><12 payload bytes>4F 54."""
    if len(payload) > 12:
        raise ValueError("payload is at most 12 bytes")
    body = payload + b"\x00" * (12 - len(payload))
    return bytes([op, sub]) + body + b"\x4f\x54"


def oti_keyboard(mod: int = 0, keys=(), extra: bytes = b"") -> bytes:
    """
    Keyboard payload (best reconstruction from the vendor caller):
      p0    modifier byte
      p1    0
      p2..7 up to 6 USB-HID usage codes
      p8..11 extra / reserved
    """
    keys = list(keys)[:6]
    p = bytearray(12)
    p[0] = mod & 0xFF
    for i, k in enumerate(keys):
        p[2 + i] = k & 0xFF
    if extra:
        p[8:8 + len(extra)] = extra[:4]
    return oti_packet(OP_KEYBOARD, bytes(p))


def oti_mouse(buttons: int = 0, dx: int = 0, dy: int = 0,
              wheel: int = 0, hwheel: int = 0, extra: bytes = b"") -> bytes:
    """
    Mouse payload (layout verified on the real cable).
      p0      button bitmask (1=left, 2=right, 4=middle)
      p1      X delta, signed 8-bit
      p2      Y delta, signed 8-bit  (positive = down)
      p3      vertical wheel, signed 8-bit
      p4      horizontal wheel (best guess; not yet observed)
      p5..11  reserved / extra
    """
    def s8(v: int) -> int:
        return max(-128, min(127, int(v))) & 0xFF

    p = bytearray(12)
    p[0] = buttons & 0xFF
    p[1] = s8(dx)
    p[2] = s8(dy)
    p[3] = s8(wheel)
    p[4] = s8(hwheel)
    if extra:
        p[5:5 + len(extra)] = extra[:7]
    return oti_packet(OP_MOUSE, bytes(p))


def mouse_move_chunks(buttons: int, dx: int, dy: int):
    """Yield mouse packets for a large move.

    A single packet carries the delta in one signed byte, so anything beyond
    +/-127 must be split over several packets or the receiver sees it wrap.
    """
    steps = max(1, (max(abs(dx), abs(dy)) + 126) // 127)
    base_x, rem_x = divmod(dx, steps)
    base_y, rem_y = divmod(dy, steps)
    for i in range(steps):
        x = base_x + (1 if i < rem_x else 0)
        y = base_y + (1 if i < rem_y else 0)
        yield oti_mouse(buttons, x, y)


# ---------------------------------------------------------------- transports
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

# Handles and pointers are 64-bit on modern Windows; the default c_int return
# type would truncate them.
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD,
                            ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.DeviceIoControl.restype = wt.BOOL
k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wt.BOOL
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.GetLogicalDrives.restype = wt.DWORD
k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                         ctypes.c_void_p, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_void_p, wt.DWORD,
                                                 ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                      ctypes.c_void_p, wt.DWORD,
                                                      ctypes.POINTER(wt.DWORD),
                                                      ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_SCSI_PASS_THROUGH = 0x4D004
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
SCSI_IOCTL_DATA_OUT = 0
SCSI_IOCTL_DATA_IN = 1
SCSI_IOCTL_DATA_UNSPECIFIED = 2


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def guid(s: str) -> GUID:
    import uuid
    return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


GUID_DEVINTERFACE_HID = guid("{4D1E55B2-F16F-11CF-88CB-001111000030}")
GUID_DEVINTERFACE_CDROM = guid("{53F56308-B6BF-11D0-94F2-00A0C91EFB8B}")
GUID_DEVINTERFACE_DISK = guid("{53F56307-B6BF-11D0-94F2-00A0C91EFB8B}")


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD),
                ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


class SP_DEVICE_INTERFACE_DETAIL_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("DevicePath", ctypes.c_wchar * 1)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("ClassGuid", GUID),
                ("DevInst", wt.DWORD),
                ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


def list_hid_paths() -> list[str]:
    """Return every present HID device interface path (strings)."""
    return _enum_interfaces(GUID_DEVINTERFACE_HID)


def list_storage_paths() -> list[str]:
    """Return present CD-ROM / disk device interface paths (the cable's MI_00)."""
    return _enum_interfaces(GUID_DEVINTERFACE_CDROM) + _enum_interfaces(GUID_DEVINTERFACE_DISK)


def _enum_interfaces(guid_val) -> list[str]:
    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid_val), None,
                                      None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if h == INVALID_HANDLE_VALUE:
        return []
    paths = []
    try:
        i = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(h, None,
                                                        ctypes.byref(guid_val),
                                                        i, ctypes.byref(did)):
                break
            i += 1
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(did), None, 0,
                                                      ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            cb = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA)
            ctypes.memmove(buf, ctypes.byref(wt.DWORD(cb)), 4)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(did), buf,
                                                             need.value,
                                                             ctypes.byref(need), None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf)
                                     + SP_DEVICE_INTERFACE_DETAIL_DATA.DevicePath.offset)
            paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)
    return paths


def find_oti_hid_paths() -> list[str]:
    out = []
    needle = f"vid_{VID:04x}&pid_{PID:04x}"
    for p in list_hid_paths():
        if needle in p.lower():
            out.append(p)
    return out


def find_oti_drive() -> str | None:
    """Look for the cable's virtual CD drive letter (heuristic: removable CD-ROM)."""
    DRIVE_CDROM = 5
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_CDROM:
                # The OTi virtual CD usually contains an "Oti Inside" / autorun file.
                try:
                    for f in os.listdir(root):
                        if "oti" in f.lower():
                            return f"{letter}:"
                except OSError:
                    pass
    return None


class HidTransport:
    def __init__(self, path: str):
        self.path = path
        # The OTi HID collections accept write access only (read is denied),
        # which is all we need to push reports at the cable.
        self.h = k32.CreateFileW(ctypes.c_wchar_p(path), GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                 OPEN_EXISTING, 0, None)
        if self.h == INVALID_HANDLE_VALUE:
            raise OSError(f"CreateFile failed ({ctypes.get_last_error()}): {path}")

    def send(self, report: bytes) -> bool:
        wrote = wt.DWORD(0)
        ok = k32.WriteFile(self.h, report, len(report), ctypes.byref(wrote), None)
        return bool(ok)

    def close(self):
        if self.h and self.h != INVALID_HANDLE_VALUE:
            k32.CloseHandle(self.h)


class SP_THROUGH(ctypes.Structure):
    # Natural alignment matches SCSI_PASS_THROUGH (DataTransferLength at 0x0C).
    _fields_ = [("Length", ctypes.c_ushort),
                ("ScsiStatus", ctypes.c_ubyte),
                ("PathId", ctypes.c_ubyte),
                ("TargetId", ctypes.c_ubyte),
                ("Lun", ctypes.c_ubyte),
                ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte),
                ("DataIn", ctypes.c_ubyte),
                ("DataTransferLength", ctypes.c_ulong),
                ("TimeOutValue", ctypes.c_ulong),
                ("DataBufferOffset", ctypes.c_ulong),
                ("SenseInfoOffset", ctypes.c_ulong),
                ("Cdb", ctypes.c_ubyte * 16)]


class SP_THROUGH_DIRECT(ctypes.Structure):
    # SCSI_PASS_THROUGH_DIRECT: like SP_THROUGH but DataBuffer is a real pointer.
    _fields_ = [("Length", ctypes.c_ushort),
                ("ScsiStatus", ctypes.c_ubyte),
                ("PathId", ctypes.c_ubyte),
                ("TargetId", ctypes.c_ubyte),
                ("Lun", ctypes.c_ubyte),
                ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte),
                ("DataIn", ctypes.c_ubyte),
                ("DataTransferLength", ctypes.c_ulong),
                ("TimeOutValue", ctypes.c_ulong),
                ("DataBuffer", ctypes.c_void_p),
                ("SenseInfoOffset", ctypes.c_ulong),
                ("Cdb", ctypes.c_ubyte * 16)]


class ScsiTransport:
    """Sends the 16-byte OTi packet over the cable's mass-storage function
    (MI_00) using SCSI pass-through.  Requires administrator rights.

    mode="cdb"  : the 16-byte packet is the SCSI CDB itself (no data phase).
    mode="data" : the packet is the data-out buffer of a vendor CDB.
    """

    def __init__(self, device: str, cdb: bytes | None = None, mode: str = "cdb"):
        self.device = device
        self.mode = mode
        # device is either a full \\?\ path or a drive letter like "X:"
        if device.startswith("\\\\?\\") or device.startswith("\\\\.\\"):
            path = device
        elif len(device) >= 2 and device[1] == ":":
            path = _local_dev(device[:2])
        else:
            path = _local_dev(device)
        self.cdb = cdb or bytes([0xD8, 0x00, 0, 0, 0x10, 0, 0, 0, 0, 0])
        self.h = k32.CreateFileW(ctypes.c_wchar_p(path), GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                 OPEN_EXISTING, 0, None)
        if self.h == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            hint = " (run as Administrator)" if err == 1306 else ""
            raise OSError(f"CreateFile failed ({err}) on {device}{hint}")

    def send(self, report: bytes) -> bool:
        """Submit the 16-byte OTi packet.

        Uses IOCTL_SCSI_PASS_THROUGH_DIRECT, because on current Windows the
        buffered IOCTL_SCSI_PASS_THROUGH is refused with ERROR_PRIVILEGE_NOT_HELD
        even for a full Administrator token.
        """
        if self.mode == "cdb":
            cdb, data = report[:16], b""
        else:
            cdb, data = self.cdb, report
        sz = ctypes.sizeof(SP_THROUGH_DIRECT)
        sense_len = 0x18
        spt = SP_THROUGH_DIRECT()
        spt.Length = sz
        spt.CdbLength = len(cdb)
        spt.SenseInfoLength = sense_len
        spt.DataIn = SCSI_IOCTL_DATA_OUT if data else SCSI_IOCTL_DATA_UNSPECIFIED
        spt.DataTransferLength = len(data)
        spt.TimeOutValue = 5
        spt.SenseInfoOffset = sz
        dbuf = ctypes.create_string_buffer(max(len(data), 1))
        if data:
            ctypes.memmove(dbuf, data, len(data))
        spt.DataBuffer = ctypes.cast(dbuf, ctypes.c_void_p)
        for i, b in enumerate(cdb[:16]):
            spt.Cdb[i] = b
        raw = ctypes.create_string_buffer(sz + sense_len)
        ctypes.memmove(raw, ctypes.byref(spt), sz)
        ret = wt.DWORD(0)
        ok = k32.DeviceIoControl(self.h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                 raw, sz + sense_len, raw, sz + sense_len,
                                 ctypes.byref(ret), None)
        self.last_error = ctypes.get_last_error()
        self.last_status = SP_THROUGH_DIRECT.from_buffer(raw).ScsiStatus
        self.last_sense = bytes(raw[sz:sz + sense_len])
        return bool(ok)

    def xfer(self, cdb: bytes, data_len: int, data_in: bool) -> tuple[bool, bytes, bytes]:
        """Submit an arbitrary CDB with a data phase, and read the answer back.

        `send()` covers the packets we already know; this covers the *queries*.
        Returns (ok, data, sense).  The sense bytes are what tell us whether the
        cable rejected an opcode (sense key 5 / ASC 0x20 = invalid command).
        """
        sz = ctypes.sizeof(SP_THROUGH_DIRECT)
        sense_len = 0x18
        spt = SP_THROUGH_DIRECT()
        spt.Length = sz
        spt.CdbLength = len(cdb[:16])
        spt.SenseInfoLength = sense_len
        if data_in:
            spt.DataIn = SCSI_IOCTL_DATA_IN
        else:
            spt.DataIn = SCSI_IOCTL_DATA_OUT if data_len else SCSI_IOCTL_DATA_UNSPECIFIED
        spt.DataTransferLength = data_len
        spt.TimeOutValue = 5
        spt.SenseInfoOffset = sz
        dbuf = ctypes.create_string_buffer(max(data_len, 1))
        spt.DataBuffer = ctypes.cast(dbuf, ctypes.c_void_p)
        for i, b in enumerate(cdb[:16]):
            spt.Cdb[i] = b
        raw = ctypes.create_string_buffer(sz + sense_len)
        ctypes.memmove(raw, ctypes.byref(spt), sz)
        ret = wt.DWORD(0)
        ok = k32.DeviceIoControl(self.h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                 raw, sz + sense_len, raw, sz + sense_len,
                                 ctypes.byref(ret), None)
        self.last_error = ctypes.get_last_error()
        self.last_status = SP_THROUGH_DIRECT.from_buffer(raw).ScsiStatus
        self.last_sense = bytes(raw[sz:sz + sense_len])
        return bool(ok), bytes(dbuf.raw[:data_len]), self.last_sense

    def close(self):
        if self.h and self.h != INVALID_HANDLE_VALUE:
            k32.CloseHandle(self.h)


# ---------------------------------------------------------------- HID usage map
# Windows VK -> USB HID usage id (the cable expects HID usage codes).
VK_TO_HID = {
    0x08: 0x2A, 0x09: 0x2B, 0x0D: 0x28, 0x1B: 0x29, 0x20: 0x2C,
    0x25: 0x50, 0x26: 0x52, 0x27: 0x4F, 0x28: 0x51,
    0x30: 0x27, 0x31: 0x1E, 0x32: 0x1F, 0x33: 0x20, 0x34: 0x21, 0x35: 0x22,
    0x36: 0x23, 0x37: 0x24, 0x38: 0x25, 0x39: 0x26,
    0x41: 0x04, 0x42: 0x05, 0x43: 0x06, 0x44: 0x07, 0x45: 0x08, 0x46: 0x09,
    0x47: 0x0A, 0x48: 0x0B, 0x49: 0x0C, 0x4A: 0x0D, 0x4B: 0x0E, 0x4C: 0x0F,
    0x4D: 0x10, 0x4E: 0x11, 0x4F: 0x12, 0x50: 0x13, 0x51: 0x14, 0x52: 0x15,
    0x53: 0x16, 0x54: 0x17, 0x55: 0x18, 0x56: 0x19, 0x57: 0x1A, 0x58: 0x1B,
    0x59: 0x1C, 0x5A: 0x1D,
}
MOD_BIT = {0x10: 0x02, 0x11: 0x01, 0x12: 0x04, 0x5B: 0x08, 0x5C: 0x80}  # shift ctrl alt gui

LLKHF_EXTENDED = 0x01
MOD_ALT_MASK = 0x04 | 0x40          # left or right Alt


def mod_bit_for(vk: int, flags: int = 0) -> int:
    """HID modifier bit for a Windows VK, or 0 if it is not a modifier.

    A low-level keyboard hook reports the *left/right specific* VK codes for the
    modifier keys: Alt arrives as VK_LMENU (0xA4) or VK_RMENU (0xA5), NOT as
    VK_MENU (0x12), and the same goes for Shift and Ctrl.  Looking up only
    0x10/0x11/0x12 is why Alt+S never toggled while ordinary letters worked.
    Both spellings are accepted, and LLKHF_EXTENDED disambiguates the generic
    Ctrl/Alt codes when a driver does use them.
    """
    ext = bool(flags & LLKHF_EXTENDED)
    if vk == 0xA0:                                    # VK_LSHIFT
        return 0x02
    if vk == 0xA1:                                    # VK_RSHIFT
        return 0x20
    if vk == 0x10:                                    # VK_SHIFT
        return 0x20 if ext else 0x02
    if vk == 0xA2:                                    # VK_LCONTROL
        return 0x01
    if vk == 0xA3:                                    # VK_RCONTROL
        return 0x10
    if vk == 0x11:                                    # VK_CONTROL
        return 0x10 if ext else 0x01
    if vk == 0xA4:                                    # VK_LMENU
        return 0x04
    if vk == 0xA5:                                    # VK_RMENU
        return 0x40
    if vk == 0x12:                                    # VK_MENU
        return 0x40 if ext else 0x04
    if vk == 0x5B:                                    # VK_LWIN
        return 0x08
    if vk == 0x5C:                                    # VK_RWIN
        return 0x80
    return 0


def normalize_vk(vk: int) -> int:
    """Left/right modifier VKs -> their generic VK, for mapping purposes."""
    return {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11,
            0xA4: 0x12, 0xA5: 0x12}.get(vk, vk)


# ---------------------------------------------------------------- elevation
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def _quote(a: str) -> str:
    return '"%s"' % a if (" " in a or a == "") else a


LOG_MAX_BYTES = 2 * 1024 * 1024


def default_log_path() -> str:
    """Where a windowless run (pythonw.exe, i.e. the logon task) writes output.

    The script itself lives on the Desktop, so keep the log with the user's app
    data instead:  %LOCALAPPDATA%\\kmlink\\kmlink.log  (install-autostart.ps1
    prints and tails this same file.)
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "kmlink", "kmlink.log")


def open_log(path: str):
    """Open `path` for appending, line buffered, rotating once at 2 MB."""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")       # keep exactly one previous log
    except OSError:
        pass
    return open(path, "a", encoding="utf-8", buffering=1)


def ensure_admin(show_window: bool = False) -> None:
    """Re-launch this script elevated (UAC) and relay its output here.

    show_window=True is used by the interactive ``run`` command: the elevated
    copy then gets its own *visible* console (so you can watch the status and
    stop it with Ctrl+C) and this process exits instead of waiting for it.
    """
    if os.name != "nt" or is_admin():
        return
    import tempfile
    log = os.path.join(tempfile.gettempdir(), "kmlink_elevated.log")
    extra = [] if show_window else ["--log", log]
    if not show_window:
        try:
            os.remove(log)
        except OSError:
            pass
    params = " ".join(_quote(a) for a in
                      [os.path.abspath(sys.argv[0])] + sys.argv[1:] + extra)

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    SW_SHOWNORMAL = 1

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                    ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                    ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", wt.DWORD), ("hIcon", ctypes.c_void_p),
                    ("hProcess", ctypes.c_void_p)]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = params
    sei.lpDirectory = os.getcwd()
    sei.nShow = SW_SHOWNORMAL if show_window else SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise OSError("Elevation failed (error %d). Open an Administrator "
                      "terminal and retry." % ctypes.get_last_error())
    if show_window:
        print("[i] Started the elevated window -- use that one. "
              "This window can be closed.")
        sys.exit(0)
    print("[i] Waiting for the elevated run to finish...")
    if sei.hProcess:
        k32.WaitForSingleObject(ctypes.c_void_p(sei.hProcess), 0xFFFFFFFF)
        k32.CloseHandle(ctypes.c_void_p(sei.hProcess))
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            print(fh.read(), end="")
    sys.exit(0)


# ---------------------------------------------------------------- config
CONF_NAME = "kmlink.conf"
CONF_DEFAULTS = {"role": "main", "auto": "yes",
                 "peer_timeout": "5", "hb_interval": "2"}


def config_path() -> str:
    """kmlink.conf sits next to this script."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONF_NAME)


def load_config(path: str | None = None) -> dict:
    """Read `key = value` lines ('#' comments) from kmlink.conf.

    The installer writes it after asking you, but it is plain text on purpose -
    edit it by hand whenever you like; `run` reads it on start.

        role = main          this machine owns the keyboard/mouse by default
        role = secondary     the other machine is the one you normally drive,
                             so `run` starts out pointing at it
        auto = yes           route by the other machine's heartbeat
        peer_timeout = 5     seconds of silence and the input comes back here
        hb_interval = 2      seconds between the heartbeats we send

    "Is the other machine on?" cannot be seen over the cable directly - each
    end only sees its own end.  With `auto = yes` the two scripts keep each
    other awake with one tiny F24 report every `hb_interval` seconds, so `run`
    can tell "the other machine is off" from "the other machine is on".  That
    needs this script running on the other machine too; set `auto = no` if it
    is not (then nothing is ever checked).  See the README.
    """
    cfg = dict(CONF_DEFAULTS)
    try:
        with open(path or config_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                cfg[key.strip().lower()] = val.strip().lower()
    except OSError:
        pass
    return cfg


def _as_bool(v) -> bool:
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def _as_secs(v) -> float:
    try:
        return max(0.5, float(v))
    except (TypeError, ValueError):
        return 0.0


def pick_settings(args) -> dict:
    """role / auto / peer_timeout / hb_interval: --flags win, then kmlink.conf."""
    cfg = load_config(getattr(args, "config", None))
    role = (getattr(args, "role", None) or cfg.get("role") or "main").lower()
    if role not in ("main", "secondary"):
        role = "main"
    auto = getattr(args, "auto", None)
    if auto is None:
        auto = _as_bool(cfg.get("auto"))

    def secs(attr, key):
        v = getattr(args, attr, None)
        if v is None:
            v = cfg.get(key)
        return _as_secs(v) or _as_secs(CONF_DEFAULTS[key])

    return {"role": role, "auto": bool(auto),
            "peer_timeout": secs("peer_timeout", "peer_timeout"),
            "hb_interval": secs("hb_interval", "hb_interval")}


def pick_role(args) -> str:
    """--role wins, then kmlink.conf, then 'main'."""
    return pick_settings(args)["role"]


# ---------------------------------------------------------------- CLI
def find_oti_storage_paths() -> list[str]:
    """Storage device interface paths that belong to the OTi cable (MI_00)."""
    out = []
    for p in list_storage_paths():
        low = p.lower()
        if "android+mac" in low or "virtual_link" in low:
            out.append(p)
    return out


def build_transport(args):
    if args.transport == "hid":
        raise SystemExit(
            "The cable's HID interfaces (MI_01 mouse, MI_02 keyboard) are INPUT "
            "only (OutputReportByteLength = 0), so HID cannot be used to send. "
            "Use --transport scsi (the cable's mass-storage function).")
    cdb = bytes(int(b, 16) for b in args.cdb.split()) if args.cdb else None
    if args.device or args.drive:
        cands = [args.device or args.drive]
    else:
        cands = oti_candidate_devices()
    last = None
    for dev in cands:
        try:
            t = ScsiTransport(dev, cdb=cdb, mode=args.mode)
        except OSError as e:
            last = e
            continue
        print(f"[i] SCSI transport on {dev!r} mode={args.mode}")
        return t
    raise SystemExit("Could not open the cable's send channel (%s). Run 'diag' "
                     "to see which handle works." % (last,))


def cmd_list(args):
    print("HID interfaces (VID_%04X&PID_%04X):  <- INPUT (received from the other PC)" % (VID, PID))
    for p in find_oti_hid_paths():
        tag = "keyboard" if p.lower().endswith("\\kbd") else "mouse/other"
        print("   %-8s %s" % (tag, p))
    print()
    print("Storage device paths (the cable's MI_00 send channel):")
    for p in list_storage_paths():
        print("   ", p)
    d = find_oti_drive()
    print("heuristic drive:", d or "(none found)")


def cmd_caps(args):
    """Print HID capabilities of the OTi interfaces (proves Out=0)."""
    hid = ctypes.WinDLL("hid", use_last_error=True)

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                    ("InputReportByteLength", ctypes.c_ushort),
                    ("OutputReportByteLength", ctypes.c_ushort),
                    ("FeatureReportByteLength", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ushort * 17),
                    ("NumberLinkCollectionNodes", ctypes.c_ushort),
                    ("NumberInputButtonCaps", ctypes.c_ushort),
                    ("NumberInputValueCaps", ctypes.c_ushort),
                    ("NumberInputDataIndices", ctypes.c_ushort),
                    ("NumberOutputButtonCaps", ctypes.c_ushort),
                    ("NumberOutputValueCaps", ctypes.c_ushort),
                    ("NumberOutputDataIndices", ctypes.c_ushort),
                    ("NumberFeatureButtonCaps", ctypes.c_ushort),
                    ("NumberFeatureValueCaps", ctypes.c_ushort),
                    ("NumberFeatureDataIndices", ctypes.c_ushort)]
    hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    for p in find_oti_hid_paths():
        h = k32.CreateFileW(ctypes.c_wchar_p(p), 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE_VALUE):
            print("no handle:", p)
            continue
        pp = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            caps = HIDP_CAPS()
            hid.HidP_GetCaps(pp, ctypes.byref(caps))
            kind = {0x2: "Mouse", 0x6: "Keyboard", 0x1: "Pointer"}.get(caps.Usage, "?")
            print("%-9s In=%d Out=%d Feat=%d  %s" % (kind, caps.InputReportByteLength,
                                                     caps.OutputReportByteLength,
                                                     caps.FeatureReportByteLength, p))
            hid.HidD_FreePreparsedData(pp)
        k32.CloseHandle(h)


def _spt_once(dev_path, cdb, data_len=0, data_in=0, payload=b""):
    """One raw SCSI pass-through.
    Returns (stage, ok, gle, scsi_status, data, sense); stage is 'open'|'ioctl'."""
    h = k32.CreateFileW(ctypes.c_wchar_p(dev_path), GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        return "open", False, ctypes.get_last_error(), 0, b"", b""
    try:
        spt = SP_THROUGH_DIRECT()
        spt.Length = ctypes.sizeof(SP_THROUGH_DIRECT)
        spt.CdbLength = len(cdb[:16])
        spt.SenseInfoLength = 0
        spt.DataIn = data_in
        spt.DataTransferLength = data_len
        spt.TimeOutValue = 5
        spt.SenseInfoOffset = 0
        for i, b in enumerate(cdb[:16]):
            spt.Cdb[i] = b
        buf = ctypes.create_string_buffer(max(data_len, 1))
        if payload:
            ctypes.memmove(buf, payload, len(payload))
        spt.DataBuffer = ctypes.cast(buf, ctypes.c_void_p)
        ret = wt.DWORD(0)
        sz = ctypes.sizeof(SP_THROUGH_DIRECT)
        ok = k32.DeviceIoControl(h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                 ctypes.byref(spt), sz, ctypes.byref(spt), sz,
                                 ctypes.byref(ret), None)
        gle = ctypes.get_last_error()
        return "ioctl", bool(ok), gle, spt.ScsiStatus, bytes(buf.raw[:data_len]), b""
    finally:
        k32.CloseHandle(h)


def _local_dev(name: str) -> str:
    r"""Build a DOS device path like \\.\C: without escaping ambiguity."""
    return chr(92) * 2 + "." + chr(92) + name


def oti_candidate_devices() -> list[str]:
    """Working handles for the cable's mass-storage function, best first.

    Every one of these drives the same OTi firmware (verified on hardware), so
    the order only matters for stability: the SetupDi paths are re-discovered by
    VID/PID on each run, whereas drive letters and PhysicalDriveN numbers can
    change between machines/boots.
    """
    found = find_oti_storage_paths()
    disk = [p for p in found if "disk" in p.lower()]
    rest = [p for p in found if p not in disk]
    out = disk + rest
    if not out:
        # fall back to whatever CD-ROM / removable drives exist
        m = k32.GetLogicalDrives()
        for i in range(26):
            letter = chr(ord("A") + i)
            if not (m & (1 << i)):
                continue
            dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(letter + ":\\"))
            if dtype in (5, 2):
                out.append(_local_dev(letter + ":"))
        out.append(_local_dev("PhysicalDrive1"))
    return out


def cmd_diag(args):
    """Check whether SCSI pass-through works at all, and on which handle."""
    ensure_admin()
    print("IsUserAnAdmin =", is_admin())
    targets = [("volume C:", _local_dev("C:")),
               ("PhysicalDrive0", _local_dev("PhysicalDrive0")),
               ("OTi D: CD", _local_dev("D:")),
               ("OTi E: vol", _local_dev("E:")),
               ("OTi PhysicalDrive1", _local_dev("PhysicalDrive1"))]
    for sp in find_oti_storage_paths():
        label = "OTi " + (sp.split("prod_")[1].split("&")[0] if "prod_" in sp else sp[:20])
        targets.append((label, sp))
    for name, path in targets:
        stage, ok, gle, st, data, sense = _spt_once(
            path, bytes([0x12, 0, 0, 0, 36, 0]), 36, 1)
        extra = ""
        if ok and data:
            extra = "  product=%r" % (data[16:32],)
        print("  %-20s path=%r open=%s ioctl_ok=%s err=%s status=%s%s" %
              (name, path, "FAIL" if stage == "open" else "ok", ok, gle, st, extra))


# The vendor DLL asks the chip about the other side of the cable with two
# opcodes that are never used for keyboard/mouse input: 0xD8 (8-byte answer,
# boolean in byte 0) and 0xF0 (16-byte answer, status in byte 14).  These are
# the candidates worth measuring - one of them is likely the presence flag.
STATUS_QUERIES = [
    ("D8.00/8", 0xD8, 0x00, 8),
    ("D8.00/16", 0xD8, 0x00, 16),
    ("D8.01/8", 0xD8, 0x01, 8),
    ("D8.02/8", 0xD8, 0x02, 8),
    ("D8.03/8", 0xD8, 0x03, 8),
    ("D8.03/16", 0xD8, 0x03, 16),
    ("F0.00/16", 0xF0, 0x00, 16),
    ("F0.00/8", 0xF0, 0x00, 8),
    ("F0.03/16", 0xF0, 0x03, 16),
]


def cmd_status(args):
    """Ask the cable about the OTHER machine (the vendor's own question).

    2208KM_HID.dll exports OTiDll_GetRemotePlugInStatus: it queries the chip
    with vendor opcodes that keyboard/mouse traffic never uses - 0xD8 and 0xF0 -
    and reads a flag out of the reply.  If that reply changes when the other
    machine is powered off, this script can route the keyboard/mouse by itself,
    with no software on the other machine at all.

    Measure it twice and compare:

        python km_link_switch.py status --save on.json     # other PC on
        python km_link_switch.py status --diff on.json     # other PC off

    Everything goes through SCSI pass-through to the cable, so this needs
    Administrator rights (the script asks for them itself).
    """
    ensure_admin()
    if args.device or args.drive:
        dev = args.device or args.drive
    else:
        cands = oti_candidate_devices()
        if not cands:
            print("[!] cable not found - is it plugged in?")
            return 1
        dev = cands[0]
    try:
        t = ScsiTransport(dev, mode="cdb")
    except OSError as exc:
        print("[!] cannot open %s: %s" % (dev, exc))
        return 1

    results = {}

    def record(key, ok, data=b"", note=""):
        results[key] = {"ok": bool(ok), "scsi_status": t.last_status,
                        "sense": t.last_sense[:8].hex(" "),
                        "data": data.hex(" ")}
        print("  %-10s ok=%-5s status=%-3s sense=%-23s data=%-26s %s"
              % (key, bool(ok), t.last_status, t.last_sense[:8].hex(" ") or "-",
                 data.hex(" ") or "-", note))

    try:
        print("device : %s" % dev)
        print()
        print("--- the send path we already know (silent release packet) ---")
        for _ in range(2):
            ok = t.send(oti_keyboard(0, []))
            record("control", ok)
            time.sleep(0.2)
        print()
        print("--- the device identity (does the cable look different?) ---")
        ok, data, _ = t.xfer(bytes([0x12, 0, 0, 0, 36, 0]), 36, True)
        note = ""
        if ok and len(data) >= 36:
            note = "vendor=%r product=%r" % (data[8:16], data[16:32])
        record("inquiry", ok, data, note)
        ok, data, _ = t.xfer(bytes([0x00, 0, 0, 0, 0, 0]), 0, False)
        record("test_unit", ok, data)
        print()
        print("--- vendor status queries (opcode.sub / reply length) ---")
        for label, op, sub, n in STATUS_QUERIES:
            pkt = oti_packet(sub, b"", op=op)
            ok, data, _ = t.xfer(pkt, n, True)
            record(label, ok, data, "cdb=%s" % pkt[:4].hex(" "))
            time.sleep(0.05)
    finally:
        t.close()

    if args.diff:
        try:
            with open(args.diff, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError) as exc:
            print("[!] cannot read %s: %s" % (args.diff, exc))
            return 1
        print()
        print("--- compared with %s ---" % args.diff)
        changed = []
        for key in results:
            was, now = old.get(key, {}), results[key]
            if was.get("data") != now["data"] or was.get("ok") != now["ok"]:
                changed.append(key)
                print("  CHANGED %-10s was ok=%s data=%s  ->  now ok=%s data=%s"
                      % (key, was.get("ok"), was.get("data", "-"),
                         now["ok"], now["data"] or "-"))
        if not changed:
            print("  nothing changed: the chip does not report the other "
                  "machine through these queries")
        else:
            print()
            print("  ^ these are the presence signal(s) - send this output back")
    if args.save:
        try:
            with open(args.save, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=1)
            print()
            print("saved -> %s" % args.save)
        except OSError as exc:
            print("[!] cannot write %s: %s" % (args.save, exc))
    return 0


def _probe_letter(dev: str, idx: int) -> str:
    """A letter the user can watch for on the other computer, per handle."""
    d = dev.upper()
    if "PHYSICALDRIVE" in d:
        return "p"
    if d.endswith("D:"):
        return "d"
    if d.endswith("E:"):
        return "e"
    if "CDROM" in d:
        return "c"
    if "DISK" in d:
        return "v"
    return "12345"[idx % 5]


def cmd_probe(args):
    """Send HID packets to every candidate handle, so the other computer
    shows which one is actually live (each handle types a different letter)."""
    ensure_admin()
    print("IsUserAnAdmin =", is_admin())
    if args.device or args.drive:
        devs = [args.device or args.drive]
    else:
        devs = oti_candidate_devices()
    for idx, dev in enumerate(devs):
        try:
            t = ScsiTransport(dev, mode=args.mode)
        except OSError as e:
            print("[!] %-20s cannot open: %s" % (dev, e))
            continue
        letter = _probe_letter(dev, idx)
        usage = 0x04 + (ord(letter) - ord("a"))
        print("=== %s : expect %r on the other computer ===" % (dev, letter))
        try:
            for name, pkt in [("kbd %r down" % letter, oti_keyboard(0, [usage])),
                              ("kbd %r up" % letter, oti_keyboard(0, [])),
                              ("mouse +40 then -40", oti_mouse(0, 40, 0))]:
                ok = t.send(pkt)
                sense = getattr(t, "last_sense", b"")
                print("  %-18s %s  ok=%s err=%s status=%s sense=%s" %
                      (name, pkt.hex(" "), ok, getattr(t, "last_error", "-"),
                       getattr(t, "last_status", "-"),
                       sense[:8].hex(" ") if sense else "-"))
                time.sleep(0.2)
            t.send(oti_mouse(0, -40, 0))
        finally:
            t.close()


def cmd_mousetest(args):
    """Step through mouse packets so the delta encoding can be read off the
    other computer's pointer.

    This writes the 16-byte packets straight to the cable - no hook, no Raw
    Input - so it measures what the *device* does with each byte.  Steps 1-4
    send both signs of both axes, which is what makes the direction question
    unambiguous: whatever moves on step 1 must move the other way on step 2.
    """
    ensure_admin()
    print("IsUserAnAdmin =", is_admin())
    print("Put the OTHER computer's pointer in the middle of its screen first.")
    print("Watch that pointer and note which way each step moves it.")
    t = build_transport(args)

    def burst(label, n, dx=0, dy=0, wheel=0, delay=0.02):
        print("--- %s ---" % label, flush=True)
        for _ in range(n):
            t.send(oti_mouse(0, dx, dy, wheel))
            time.sleep(delay)
        time.sleep(0.8)

    try:
        burst("1. 10 x X=+30  -> which way? (expect RIGHT)", 10, dx=30)
        burst("2. 10 x X=-30  -> expect the opposite of step 1", 10, dx=-30)
        burst("3. 10 x Y=+30  -> expect DOWN", 10, dy=30)
        burst("4. 10 x Y=-30  -> expect the opposite of step 3", 10, dy=-30)
        burst("5. one X=+120  -> expect 120 px right", 1, dx=120)
        burst("6. one X=-120  -> expect 120 px left", 1, dx=-120)
        burst("7. 3 x wheel +1 -> expect scroll UP", 3, wheel=1)
        burst("8. 3 x wheel -1 -> expect scroll DOWN", 3, wheel=-1)
        print("--- 9. one big move X=+900 (split into packets) ---", flush=True)
        for pkt in mouse_move_chunks(0, 900, 0):
            t.send(pkt)
            time.sleep(0.005)
        time.sleep(0.8)
        print("--- 10. 100 x X=+2 (slow drag) ---", flush=True)
        for _ in range(100):
            t.send(oti_mouse(0, 2, 0))
            time.sleep(0.008)
        time.sleep(0.8)
    finally:
        t.close()


def cmd_raw(args):
    data = bytes(int(b, 16) for b in args.bytes)
    if len(data) != 16:
        raise SystemExit("raw needs exactly 16 hex bytes")
    ensure_admin()
    t = build_transport(args)
    try:
        print("sent ok =", t.send(data))
    finally:
        t.close()


def cmd_key(args):
    vk = ord(args.key.upper()) if len(args.key) == 1 else int(args.key, 0)
    usage = VK_TO_HID.get(vk)
    if usage is None:
        raise SystemExit(f"no HID usage for {args.key!r}")
    ensure_admin()
    t = build_transport(args)
    try:
        for pkt in (oti_keyboard(0, [usage]), oti_keyboard(0, [])):
            ok = t.send(pkt)
            print("%s  ok=%s err=%s status=%s" %
                  (pkt.hex(" "), ok, getattr(t, "last_error", "-"),
                   getattr(t, "last_status", "-")))
            time.sleep(0.05)
    finally:
        t.close()


# ------------------------------------------------------ Raw Input parsing
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x01


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", ctypes.c_void_p), ("wParam", ctypes.c_void_p)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", ctypes.c_ushort), ("_pad", ctypes.c_ushort),
                ("usButtonFlags", ctypes.c_ushort), ("usButtonData", ctypes.c_ushort),
                ("ulRawButtons", wt.DWORD), ("lLastX", wt.LONG),
                ("lLastY", wt.LONG), ("ulExtraInformation", wt.DWORD)]


def raw_mouse_report(buf) -> "tuple[int, int, int, int] | None":
    """Parse a RAWINPUT buffer -> (dx, dy, wheel_delta, button_flags).

    Returns None if this is not a relative mouse report.  Kept at module level
    so the field offsets can be unit-tested without any hardware.
    """
    if len(buf) < ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWMOUSE):
        return None
    if RAWINPUTHEADER.from_buffer(buf).dwType != RIM_TYPEMOUSE:
        return None
    m = RAWMOUSE.from_buffer(buf, ctypes.sizeof(RAWINPUTHEADER))
    if m.usFlags & MOUSE_MOVE_ABSOLUTE:
        return None
    return (m.lLastX, m.lLastY, ctypes.c_short(m.usButtonData).value,
            m.usButtonFlags)


def raw_mouse_is_absolute(buf) -> bool:
    """True if this RAWINPUT buffer is a mouse report flagged ABSOLUTE.

    Touchpads, tablets and remote-desktop sessions can report positions instead
    of deltas; that needs a different conversion, so it is worth saying so out
    loud rather than sitting there doing nothing.
    """
    if len(buf) < ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWMOUSE):
        return False
    if RAWINPUTHEADER.from_buffer(buf).dwType != RIM_TYPEMOUSE:
        return False
    m = RAWMOUSE.from_buffer(buf, ctypes.sizeof(RAWINPUTHEADER))
    return bool(m.usFlags & MOUSE_MOVE_ABSOLUTE)


def cmd_run(args):
    """Capture the local keyboard/mouse and forward while 'remote'.

    Mouse movement comes from Raw Input (the real hardware deltas), NOT from the
    low-level hook's cursor position: while 'remote' the local pointer is
    swallowed, so it never advances and cursor-position deltas are meaningless
    (that is what made the pointer jump, crawl and appear reversed).

    No hook ever blocks on the USB transfer.  Input only updates an accumulator
    or lands on a queue; a sender thread flushes it at a fixed rate.  The
    vendor app does the same thing at ~100 Hz, and one packet takes ~0.5 ms.
    """
    # A visible console is only useful when there *is* a console: the logon task
    # starts us with pythonw.exe, where a UAC relaunch would flash a window.
    ensure_admin(show_window=getattr(args, "console", True))
    # At logon the cable is usually already there, but if it was unplugged when
    # the task fired, wait for it instead of silently doing nothing all day.
    while True:
        try:
            forward = _windows_forwarder(args)
            break
        except OSError as exc:
            print("[!] %s" % exc, flush=True)
            print("[!] Is the KM Link cable plugged in? Retrying in 5 s "
                  "(stop the scheduled task, or press Ctrl+C here, to give up).",
                  flush=True)
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                return
    try:
        forward()
    except KeyboardInterrupt:
        pass


def _windows_forwarder(args):
    """Build the raw-input forwarder.  Returns the function that runs it."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
    LLKHF_INJECTED = 0x10

    WM_INPUT = 0x00FF
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 0xB1
    MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
    RIDEV_INPUTSINK = 0x00000100
    RID_INPUT = 0x10000003
    PM_REMOVE = 0x0001
    # The heartbeat key: F24 (HID usage 0x73).  F24 does nothing on either OS,
    # and the hook swallows it anyway, so the heartbeat is invisible here.
    VK_F24 = 0x87
    HID_F24 = 0x73

    RI_MOUSE_LEFT_DOWN, RI_MOUSE_LEFT_UP = 0x0001, 0x0002
    RI_MOUSE_RIGHT_DOWN, RI_MOUSE_RIGHT_UP = 0x0004, 0x0008
    RI_MOUSE_MIDDLE_DOWN, RI_MOUSE_MIDDLE_UP = 0x0010, 0x0020
    RI_MOUSE_B4_DOWN, RI_MOUSE_B4_UP = 0x0040, 0x0080
    RI_MOUSE_B5_DOWN, RI_MOUSE_B5_UP = 0x0100, 0x0200
    RI_MOUSE_WHEEL, RI_MOUSE_HWHEEL = 0x0400, 0x0800

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                    ("flags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
                    ("dwFlags", wt.DWORD), ("hwndTarget", ctypes.c_void_p)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, wt.UINT, WPARAM, LPARAM)

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.GetMessageW.restype = wt.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
    user32.PeekMessageW.restype = wt.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p,
                                    wt.UINT, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wt.UINT, WPARAM, LPARAM]
    user32.RegisterClassW.restype = wt.WORD
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [wt.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.RegisterRawInputDevices.restype = wt.BOOL
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.UINT, wt.UINT]
    user32.GetRawInputData.restype = wt.UINT
    user32.GetRawInputData.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
                                       ctypes.POINTER(wt.UINT), wt.UINT]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    transport = build_transport(args)
    # Which machine should own the keyboard/mouse at start-up?  role=main (the
    # default) keeps them here; role=secondary means the other machine is the
    # one you normally drive, so start out pointing at it.  The installer asks
    # the question once and records the answer in kmlink.conf.
    settings = pick_settings(args)
    role, auto = settings["role"], settings["auto"]
    peer_timeout, hb_interval = settings["peer_timeout"], settings["hb_interval"]
    want_remote = bool(args.start_remote) or role == "secondary"
    state = {"remote": False, "desired": False, "mods": 0}
    # "Is the other machine on?" cannot be seen over the cable directly (each
    # end only sees its own end), so the two scripts keep each other awake with
    # one F24 report - a key nothing uses - every hb_interval seconds.  Hearing
    # one is proof that the other machine is up and running this script;
    # peer_timeout seconds of silence means it is not.
    peer = {"last": 0.0, "ever": False}
    print("[i] role = %s (%s) -> %s"
          % (role, "--role" if getattr(args, "role", None) else config_path(),
             "starting remote" if want_remote else "starting local"), flush=True)
    print("[i] auto = %s%s"
          % ("yes" if auto else "no",
             ("  (heartbeat every %.1fs, gone after %.1fs)"
              % (hb_interval, peer_timeout)) if auto
             else "  (the other PC is never checked)"), flush=True)
    pressed = []                      # HID usages currently held down
    acc = {"dx": 0, "dy": 0, "wheel": 0, "btn": 0, "dirty": False}
    alock = threading.Lock()
    stop = threading.Event()
    kq = queue.Queue()
    interval = 1.0 / max(10, int(args.rate))

    def set_remote(v):
        state["remote"] = state["desired"] = bool(v)
        # The other PC must never be left holding a key or a mouse button: on
        # the way out, the release events happen while we are local and would
        # never be forwarded.  Queue an "all released" report instead.
        state["mods"] = 0
        pressed.clear()
        with alock:
            acc["btn"] = 0
            acc["dx"] = acc["dy"] = acc["wheel"] = 0
            acc["dirty"] = True
        kq.put((0, []))
        print("[i] remote = %s  (keyboard+mouse go to %s)"
              % (v, "the other PC" if v else "this PC"), flush=True)

    # ---------------------------------------------------- is the other PC up?
    def peer_present():
        """True while the other machine's heartbeat is recent."""
        return peer["ever"] and \
            (time.monotonic() - peer["last"]) <= peer_timeout

    def peer_gone_note(what):
        print("[!] %s: no heartbeat from the other PC for %.0f s.  Is "
              "km_link_switch running there?" % (what, peer_timeout), flush=True)

    def beep(error=False):
        """Make refusals and "your input just came home" audible.

        There is no console any more (the logon task runs pythonw.exe), so
        without this a refused Alt+S would look like a dead key.  MessageBeep
        only queues a sound, so the hook callback never blocks.
        """
        try:
            user32.MessageBeep(0x10 if error else 0x40)  # MB_ICONHAND/ASTERISK
        except Exception:
            pass

    def enforce_presence():
        """Never leave the keyboard/mouse pointing at a machine that is gone."""
        if auto and state["remote"] and not peer_present():
            peer_gone_note("the other PC stopped answering")
            print("[i] bringing the keyboard/mouse back to this PC "
                  "(press Alt+S to try again).", flush=True)
            beep()
            set_remote(False)

    def toggle():
        if state["remote"]:
            set_remote(False)
            return
        if auto and not peer_present():
            peer_gone_note("the other PC is not answering")
            print("[i] staying here.  (Set 'auto = no' in kmlink.conf to switch "
                  "without this check.)", flush=True)
            beep(error=True)
            return
        set_remote(True)

    # -------------------------------------------------------------- sender
    def flush():
        try:
            while True:
                mod, keys = kq.get_nowait()
                transport.send(oti_keyboard(mod, keys))
        except queue.Empty:
            pass
        with alock:
            dx, dy, wheel = acc["dx"], acc["dy"], acc["wheel"]
            btn, dirty = acc["btn"], acc["dirty"]
            acc["dx"] = acc["dy"] = acc["wheel"] = 0
            acc["dirty"] = False
        if dx or dy:
            steps = max(1, (max(abs(dx), abs(dy)) + 126) // 127)
            if steps > 24:                 # cable is lagging: clamp instead of
                f = steps / 24.0           # queueing a huge backlog
                dx, dy = int(dx / f), int(dy / f)
            for pkt in mouse_move_chunks(btn, dx, dy):
                transport.send(pkt)
            if wheel:
                transport.send(oti_mouse(btn, 0, 0, wheel))
        elif wheel or dirty:
            transport.send(oti_mouse(btn, 0, 0, wheel))

    def worker():
        hb = 0.0
        while not stop.is_set():
            try:
                flush()
                # Keep the other machine able to see that this one is on.  Skip
                # while a key is held: the heartbeat is a full keyboard report,
                # so it would briefly clear what the other PC has down.
                if auto and not pressed and time.monotonic() - hb >= hb_interval:
                    kq.put((0, [HID_F24]))
                    kq.put((0, []))
                    flush()
                    hb = time.monotonic()
            except OSError as exc:
                print("[!] send failed: %s" % exc, flush=True)
            time.sleep(interval)

    # ----------------------------------------------------------- raw input
    def on_raw_input(lparam):
        enforce_presence()
        if not state["remote"]:
            return
        buf = ctypes.create_string_buffer(64)
        size = wt.UINT(ctypes.sizeof(buf))
        got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                     ctypes.sizeof(RAWINPUTHEADER))
        if got in (0, 0xFFFFFFFF):
            return
        rep = raw_mouse_report(buf)
        if rep is None:
            if raw_mouse_is_absolute(buf) and not state.get("warned_abs"):
                state["warned_abs"] = True
                print("[!] your pointing device sends ABSOLUTE raw input; "
                      "forwarding it needs a different conversion - tell me",
                      flush=True)
            return
        dx, dy, data, flags = rep
        with alock:
            if dx or dy:
                acc["dx"] += dx
                acc["dy"] += dy
            if flags & RI_MOUSE_WHEEL:
                acc["wheel"] += data // 120
                acc["dirty"] = True
            elif flags & RI_MOUSE_HWHEEL:
                acc["dirty"] = True
            for down_f, up_f, bit in ((RI_MOUSE_LEFT_DOWN, RI_MOUSE_LEFT_UP, 1),
                                      (RI_MOUSE_RIGHT_DOWN, RI_MOUSE_RIGHT_UP, 2),
                                      (RI_MOUSE_MIDDLE_DOWN, RI_MOUSE_MIDDLE_UP, 4),
                                      (RI_MOUSE_B4_DOWN, RI_MOUSE_B4_UP, 8),
                                      (RI_MOUSE_B5_DOWN, RI_MOUSE_B5_UP, 16)):
                if flags & down_f:
                    acc["btn"] |= bit
                    acc["dirty"] = True
                elif flags & up_f:
                    acc["btn"] &= ~bit
                    acc["dirty"] = True

    hwnd_holder = {"hwnd": None}

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                on_raw_input(lparam)
            except Exception:
                pass
        elif msg == WM_HOTKEY and wparam == HOTKEY_ID:
            toggle()                             # fallback Ctrl+Alt+F12
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # --------------------------------------------------------------- hooks
    def kbd_proc(ncode, wparam, lparam):
        if ncode == 0:
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not (k.flags & LLKHF_INJECTED):
                if k.vkCode == VK_F24 and auto:
                    # The other machine's heartbeat, not something a person
                    # pressed: swallow it, and note that it is alive.
                    peer["last"] = time.monotonic()
                    peer["ever"] = True
                    if args.keys:
                        print("[k] heartbeat from the other PC", flush=True)
                    return 1
                down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                enforce_presence()
                alt = bool(state["mods"] & MOD_ALT_MASK)
                if down and k.vkCode == 0x53 and alt:
                    toggle()                                 # Alt+S
                    return 1
                if state["remote"] and (k.vkCode == 0x7B         # F12:
                                        and state["mods"] & 0x11  # ctrl
                                        and alt):                # alt
                    # Let the registered Ctrl+Alt+F12 fallback hotkey see it.
                    return user32.CallNextHookEx(None, ncode, wparam, lparam)
                bit = mod_bit_for(k.vkCode, k.flags)
                if bit:
                    if down:
                        state["mods"] |= bit
                    else:
                        state["mods"] &= ~bit
                    if args.keys:
                        print("[k] vk=0x%02X %s MODIFIER bit=0x%02X -> mods=0x%02X"
                              % (k.vkCode, "DOWN" if down else "up  ", bit,
                                 state["mods"]), flush=True)
                else:
                    usage = VK_TO_HID.get(k.vkCode)
                    if usage is not None:
                        if down and usage not in pressed:
                            pressed.append(usage)
                        elif not down and usage in pressed:
                            pressed.remove(usage)
                    if args.keys:
                        print("[k] vk=0x%02X %s usage=%s mods=0x%02X pressed=%s"
                              % (k.vkCode, "DOWN" if down else "up  ",
                                 ("0x%02X" % usage) if usage else "unmapped",
                                 state["mods"], pressed), flush=True)
                if state["remote"]:
                    kq.put((state["mods"], list(pressed[:6])))
                    return 1        # swallow: this PC must not act on it
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def mouse_proc(ncode, wparam, lparam):
        # Only here to keep the local pointer still while 'remote'; the actual
        # movement is read from Raw Input (WM_INPUT) above.
        if ncode == 0 and state["remote"]:
            return 1
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    kbd_cb = HOOKPROC(kbd_proc)
    mouse_cb = HOOKPROC(mouse_proc)
    wnd_cb = WNDPROC(wnd_proc)

    def run():
        hinst = kernel32.GetModuleHandleW(None)
        cls = "kmlink_forward_%d" % os.getpid()
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(wnd_cb, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise SystemExit("RegisterClassW failed: %d" % ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(0, cls, "kmlink", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            raise SystemExit("CreateWindowExW failed: %d" % ctypes.get_last_error())
        hwnd_holder["hwnd"] = hwnd

        rid = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd)   # generic desktop / mouse
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            print("[!] RegisterRawInputDevices failed: %d" % ctypes.get_last_error(),
                  flush=True)

        hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kbd_cb, None, 0)
        hm = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_cb, None, 0)
        if not hk or not hm:
            raise SystemExit("SetWindowsHookEx failed: %d" % ctypes.get_last_error())

        if not user32.RegisterHotKey(hwnd, HOTKEY_ID, MOD_CONTROL | MOD_ALT, 0x7B):
            print("[!] fallback hotkey Ctrl+Alt+F12 unavailable (error %d)"
                  % ctypes.get_last_error(), flush=True)

        threading.Thread(target=worker, daemon=True).start()

        # Both machines can boot at the same instant, so the worker above is
        # already sending our heartbeat while we wait for theirs.  The hooks
        # only fire inside a message loop, hence the PeekMessage pumping.
        msg = wt.MSG()
        if auto and want_remote:
            print("[i] checking the other PC (up to %.1f s)..." % peer_timeout,
                  flush=True)
            deadline = time.monotonic() + peer_timeout
            while time.monotonic() < deadline and not peer_present():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0,
                                          PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.05)
        if want_remote and auto and not peer_present():
            peer_gone_note("the other PC did not answer")
            print("[i] starting local instead (press Alt+S once it is up).",
                  flush=True)
        set_remote(want_remote and (peer_present() or not auto))
        print("[i] running: Alt+S switches (fallback Ctrl+Alt+F12), "
              "Ctrl+C quits.", flush=True)

        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            user32.UnhookWindowsHookEx(hk)
            user32.UnhookWindowsHookEx(hm)
            transport.close()

    return run


def cmd_watch(args):
    """Show what the input layer actually reports.  Read-only.

    It installs the same keyboard hook and Raw Input registration that `run`
    uses, but it forwards nothing, swallows nothing and never touches the cable
    -- so it needs no Administrator rights and cannot disturb either PC.  Use it
    to confirm that Alt+S is seen (the hook reports VK_LMENU/VK_RMENU, not
    VK_MENU) and that Raw Input delivers mouse deltas.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WPARAM, LPARAM, LRESULT = ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_ssize_t
    WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
    WM_KEYDOWN, WM_SYSKEYDOWN, WM_INPUT = 0x100, 0x104, 0x00FF
    RIDEV_INPUTSINK, RID_INPUT = 0x00000100, 0x10000003

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                    ("flags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
                    ("dwFlags", wt.DWORD), ("hwndTarget", ctypes.c_void_p)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, wt.UINT, WPARAM, LPARAM)

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wt.UINT, WPARAM, LPARAM]
    user32.RegisterClassW.restype = wt.WORD
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [wt.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.RegisterRawInputDevices.restype = wt.BOOL
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
    user32.GetRawInputData.restype = wt.UINT
    user32.GetRawInputData.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
                                       ctypes.POINTER(wt.UINT), wt.UINT]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    vk_name = {0x53: "'S'", 0x12: "VK_MENU", 0xA4: "VK_LMENU", 0xA5: "VK_RMENU",
               0x10: "VK_SHIFT", 0xA0: "VK_LSHIFT", 0xA1: "VK_RSHIFT",
               0x11: "VK_CONTROL", 0xA2: "VK_LCONTROL", 0xA3: "VK_RCONTROL",
               0x5B: "VK_LWIN", 0x5C: "VK_RWIN", 0x7B: "F12"}

    def kbd_proc(ncode, wparam, lparam):
        if ncode == 0:
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            print("  KEY  vk=0x%02X %-11s %-4s mod-bit=0x%02X%s" % (
                k.vkCode, vk_name.get(k.vkCode, ""), "DOWN" if down else "up",
                mod_bit_for(k.vkCode, k.flags),
                "   <injected>" if k.flags & 0x10 else ""), flush=True)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    counts = {"n": 0, "dx": 0, "dy": 0, "wheel": 0, "btn": 0, "abs": 0}

    def on_raw(lparam):
        buf = ctypes.create_string_buffer(64)
        size = wt.UINT(ctypes.sizeof(buf))
        if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                  ctypes.sizeof(RAWINPUTHEADER)) in (0, 0xFFFFFFFF):
            return
        rep = raw_mouse_report(buf)
        if rep is None:
            if raw_mouse_is_absolute(buf):
                counts["abs"] += 1
            return
        dx, dy, data, flags = rep
        counts["n"] += 1
        counts["dx"] += dx
        counts["dy"] += dy
        if flags & 0x0400:
            counts["wheel"] += data // 120
        if flags & 0x05FF:
            counts["btn"] += 1
            print("  BTN  flags=0x%04X" % flags, flush=True)

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                on_raw(lparam)
            except Exception:
                pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    kbd_cb, wnd_cb = HOOKPROC(kbd_proc), WNDPROC(wnd_proc)

    hinst = kernel32.GetModuleHandleW(None)
    cls = "kmlink_watch_%d" % os.getpid()
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wnd_cb, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = cls
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise SystemExit("RegisterClassW failed: %d" % ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, cls, "kmlink watch", 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    if not hwnd:
        raise SystemExit("CreateWindowExW failed: %d" % ctypes.get_last_error())

    rid = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd)
    ok_raw = user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                            ctypes.sizeof(RAWINPUTDEVICE))
    hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kbd_cb, None, 0)
    hm = user32.SetWindowsHookExW(WH_MOUSE_LL, HOOKPROC(lambda *a: user32.CallNextHookEx(
        None, a[0], a[1], a[2])), None, 0)

    print("watching - nothing is forwarded or swallowed.")
    print("  keyboard hook : %s" % ("installed" if hk else "FAILED"))
    print("  mouse hook    : %s" % ("installed" if hm else "FAILED"))
    print("  raw input     : %s" % ("registered" if ok_raw else "FAILED"))
    print("Press Alt+S a few times, wiggle the mouse, then Ctrl+C.")

    def reporter():
        while True:
            time.sleep(1.5)
            print("  [1.5s] raw mouse reports=%d  dx=%d dy=%d wheel=%d buttons=%d  absolute=%d"
                  % (counts["n"], counts["dx"], counts["dy"], counts["wheel"],
                     counts["btn"], counts["abs"]), flush=True)

    threading.Thread(target=reporter, daemon=True).start()

    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser(description="Direct OTi KM Link cable switcher (no vendor app, no LAN)")
    ap.add_argument("command",
                    choices=["list", "caps", "diag", "probe", "key", "raw",
                             "mousetest", "watch", "status", "run"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--transport", choices=["scsi"], default="scsi",
                    help="send channel (the cable only accepts writes over SCSI)")
    ap.add_argument("--device", help="storage device path (\\\\?\\...) or drive letter X:")
    ap.add_argument("--drive", help="mass storage drive of the cable, e.g. X:")
    ap.add_argument("--cdb", help="vendor CDB for mode=data (default 'D8 00 00 00 10 ...')")
    ap.add_argument("--mode", choices=["cdb", "data"], default="cdb",
                    help="cdb: 16-byte packet IS the CDB; data: packet is the CDB's data")
    ap.add_argument("--start-remote", action="store_true", help="start in remote mode")
    ap.add_argument("--rate", type=int, default=125,
                    help="run: mouse/keyboard packets per second (default 125)")
    ap.add_argument("--keys", action="store_true",
                    help="run: print every key event the hook sees")
    ap.add_argument("--role", choices=["main", "secondary"],
                    help="this machine owns the keyboard/mouse (main, default) "
                         "or the other one is the machine you drive (secondary); "
                         "run reads it from kmlink.conf unless given here")
    ap.add_argument("--config", help="path to kmlink.conf (default: next to this script)")
    peer = ap.add_mutually_exclusive_group()
    peer.add_argument("--auto", dest="auto", action="store_true", default=None,
                      help="run: switch only while the other PC answers its "
                           "heartbeat (this is the default)")
    peer.add_argument("--no-auto", dest="auto", action="store_false",
                      help="run: do not check the other PC at all (switch blindly)")
    ap.add_argument("--peer-timeout", type=float, dest="peer_timeout",
                    help="run: seconds without a heartbeat before the "
                         "keyboard/mouse come back here (default 5)")
    ap.add_argument("--hb-interval", type=float, dest="hb_interval",
                    help="run: seconds between the heartbeats sent to the "
                         "other PC (default 2)")
    ap.add_argument("--save", help="status: write the probe results to a JSON file")
    ap.add_argument("--diff", help="status: compare against a JSON file saved earlier")
    ap.add_argument("--log", help="write the output to this file as well "
                                  "(and, with no console at all, write it only "
                                  "there); default for the logon task: "
                                  "%%LOCALAPPDATA%%\\kmlink\\kmlink.log")
    args = ap.parse_args()
    # True when this process really has a console (pythonw.exe has none, so
    # nothing here may rely on a window or on someone reading it).
    args.console = sys.__stdout__ is not None

    # Output plumbing.  Two cases:
    #   * ensure_admin() relays an elevated run through --log;
    #   * pythonw.exe (how the logon task starts us) has no stdout at all, so
    #     the log file *becomes* stdout and nothing is lost.
    if args.log is None and sys.stdout is None:
        args.log = default_log_path()
    if args.log:
        try:
            fh = open_log(args.log)
        except OSError:
            fh = None
        if fh is not None:
            if sys.__stdout__ is None:               # windowless: log == console
                sys.stdout = sys.stderr = fh
            else:
                sys.stdout = _Tee(sys.__stdout__, fh)
                sys.stderr = _Tee(sys.__stderr__, fh)
            # Mark where this run starts: the log is appended to, so a session
            # boundary is the first thing you want when reading it back.
            print("[i] %s  km_link_switch.py %s"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), args.command), flush=True)

    if args.command == "list":
        return cmd_list(args)
    if args.command == "caps":
        return cmd_caps(args)
    if args.command == "diag":
        return cmd_diag(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "key":
        args.key = args.arg or "a"
        return cmd_key(args)
    if args.command == "raw":
        args.bytes = (args.arg or "").split()
        return cmd_raw(args)
    if args.command == "mousetest":
        return cmd_mousetest(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "run":
        return cmd_run(args)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except OSError as e:
        print(f"[!] {e}")
        if "failed (5)" in str(e) or "failed (1306)" in str(e):
            print("[!] The cable's send channel needs Administrator rights. "
                  "Re-run this command from an elevated prompt.")
        sys.exit(1)
