#!/usr/bin/env python3
"""
km_link_switch_linux.py

Ubuntu/Linux twin of km_link_switch.py: reproduces Mac KM Link's Alt+S switching
by talking to the OTi "Smart KM Link" USB bridge cable (USB\\VID_0EA0&PID_2213)
directly.  No vendor app, no network, stdlib only.

Right port of the Windows script:

    Windows                        Linux
    ------------------------------------------------------------------
    IOCTL_SCSI_PASS_THROUGH_DIRECT  SG_IO on /dev/sgN
    WH_KEYBOARD_LL / WH_MOUSE_LL    evdev on /dev/input/eventN
    "swallow" by returning 1        EVIOCGRAB (kernel-level grab)
    Raw Input (WM_INPUT)            EV_REL deltas (already raw)
    Task Scheduler at logon         systemd unit (install-autostart.sh)

The 16-byte OTi packet is byte-for-byte the same as the Windows version:

    byte 0        0xD9
    byte 1        0x33 mouse / 0x34 keyboard
    bytes 2..13   12-byte payload
    bytes 14,15   0x4F 0x54

WHAT IT NEEDS
  * root  (SG_IO wants /dev/sgN write access, EVIOCGRAB wants /dev/input
           read access) -- so: sudo, or the systemd unit from
           install-autostart.sh.
  * the cable plugged in.

QUICK START
  sudo python3 km_link_switch_linux.py list       # find the cable
  sudo python3 km_link_switch_linux.py diag       # check SG_IO + input devices
  sudo python3 km_link_switch_linux.py key a      # type "a" on the other PC
  sudo python3 km_link_switch_linux.py mousetest  # mouse-packet walkthrough
  python3 km_link_switch_linux.py watch           # read-only, no root needed
  sudo python3 km_link_switch_linux.py run        # Alt+S switcher
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import select
import struct
import sys
import time

try:
    import fcntl
except ImportError:          # Windows: lets the packet/table code be unit-tested
    fcntl = None             # type: ignore

OTI_VID = "0ea0"
OTI_PID = "2213"

# ---------------------------------------------------------------- packets
OP_MOUSE = 0x33
OP_KEYBOARD = 0x34


def oti_packet(sub: int, payload: bytes = b"", op: int = 0xD9) -> bytes:
    """Build a 16-byte OTi packet: <op><sub><12 payload bytes>4F 54."""
    if len(payload) > 12:
        raise ValueError("payload is at most 12 bytes")
    body = payload + b"\x00" * (12 - len(payload))
    return bytes([op, sub]) + body + b"\x4f\x54"


def oti_keyboard(mod: int = 0, keys=()) -> bytes:
    """Keyboard payload (same layout as the Windows script / the vendor DLL):
      p0     modifier byte
      p1     0
      p2..7  up to 6 USB-HID usage codes
    """
    keys = list(keys)[:6]
    p = bytearray(12)
    p[0] = mod & 0xFF
    for i, k in enumerate(keys):
        p[2 + i] = k & 0xFF
    return oti_packet(OP_KEYBOARD, bytes(p))


def oti_mouse(buttons: int = 0, dx: int = 0, dy: int = 0,
              wheel: int = 0, hwheel: int = 0) -> bytes:
    """Mouse payload (layout verified on the real cable):
      p0  button bitmask (1=left, 2=right, 4=middle)
      p1  X delta, signed 8-bit
      p2  Y delta, signed 8-bit (positive = down)
      p3  vertical wheel, signed 8-bit
      p4  horizontal wheel
    """
    def s8(v: int) -> int:
        return max(-128, min(127, int(v))) & 0xFF

    p = bytearray(12)
    p[0] = buttons & 0xFF
    p[1] = s8(dx)
    p[2] = s8(dy)
    p[3] = s8(wheel)
    p[4] = s8(hwheel)
    return oti_packet(OP_MOUSE, bytes(p))


def mouse_move_chunks(buttons: int, dx: int, dy: int):
    """One packet carries +/-127 px, so a bigger move is split over packets."""
    steps = max(1, (max(abs(dx), abs(dy)) + 126) // 127)
    base_x, rem_x = divmod(dx, steps)
    base_y, rem_y = divmod(dy, steps)
    for i in range(steps):
        yield oti_mouse(buttons,
                        base_x + (1 if i < rem_x else 0),
                        base_y + (1 if i < rem_y else 0))


# ------------------------------------------------------- evdev definitions
EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02

REL_X, REL_Y, REL_HWHEEL, REL_DIAL, REL_WHEEL = 0x00, 0x01, 0x06, 0x07, 0x08

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114

KEY_ESC, KEY_1, KEY_0 = 1, 2, 11
KEY_A, KEY_Z, KEY_S = 30, 44, 31
KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126
# F24: nothing on either OS uses it.  The heartbeat packet carries this usage,
# and a received KEY_F24 on the cable's own HID keyboard means "that machine is
# up".  (See the README: "is the other machine on?".)
KEY_F24 = 194
HID_F24 = 0x73

# evdev key code -> USB HID usage id (the cable wants HID usage codes)
KEY_TO_HID = {
    # letters
    30: 0x04, 48: 0x05, 46: 0x06, 32: 0x07, 18: 0x08, 33: 0x09, 34: 0x0A,
    35: 0x0B, 23: 0x0C, 36: 0x0D, 37: 0x0E, 38: 0x0F, 50: 0x10, 49: 0x11,
    24: 0x12, 25: 0x13, 16: 0x14, 19: 0x15, 31: 0x16, 20: 0x17, 22: 0x18,
    47: 0x19, 17: 0x1A, 45: 0x1B, 21: 0x1C, 44: 0x1D,
    # digits
    2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22, 7: 0x23, 8: 0x24,
    9: 0x25, 10: 0x26, 11: 0x27,
    # punctuation and control keys
    28: 0x28, 1: 0x29, 14: 0x2A, 15: 0x2B, 57: 0x2C, 12: 0x2D, 13: 0x2E,
    26: 0x2F, 27: 0x30, 43: 0x31, 39: 0x33, 40: 0x34, 41: 0x35, 51: 0x36,
    52: 0x37, 53: 0x38, 58: 0x39,
    # F1..F12
    59: 0x3A, 60: 0x3B, 61: 0x3C, 62: 0x3D, 63: 0x3E, 64: 0x3F, 65: 0x40,
    66: 0x41, 67: 0x42, 68: 0x43, 87: 0x44, 88: 0x45,
    # navigation / system
    99: 0x46, 70: 0x47, 119: 0x48, 110: 0x49, 102: 0x4A, 104: 0x4B,
    111: 0x4C, 107: 0x4D, 109: 0x4E, 106: 0x4F, 105: 0x50, 108: 0x51,
    103: 0x52, 69: 0x53, 98: 0x54, 55: 0x55, 74: 0x56, 78: 0x57, 96: 0x58,
    79: 0x59, 80: 0x5A, 81: 0x5B, 75: 0x5C, 76: 0x5D, 77: 0x5E, 71: 0x5F,
    72: 0x60, 73: 0x61, 82: 0x62, 83: 0x63, 86: 0x64, 127: 0x65,
    116: 0x66, 117: 0x67,
}

# evdev modifier code -> HID modifier bit
MOD_BITS = {
    29: 0x01,   # left ctrl
    97: 0x10,   # right ctrl
    42: 0x02,   # left shift
    54: 0x20,   # right shift
    56: 0x04,   # left alt
    100: 0x40,  # right alt
    125: 0x08,  # left meta
    126: 0x80,  # right meta
}
ALT_MASK = 0x04 | 0x40

# evdev codes for a..z (not contiguous in evdev: A=30 but B=48)
LETTER_CODES = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
}

# mouse button code -> OTi button bitmask
BTN_BITS = {BTN_LEFT: 1, BTN_RIGHT: 2, BTN_MIDDLE: 4, BTN_SIDE: 8, BTN_EXTRA: 16}

# struct input_event (64-bit): timeval(16) + u16 type + u16 code + s32 value
INPUT_EVENT = struct.Struct("<qqHHi")

# ioctl numbers (asm-generic _IOC encoding)
def EVIOCGBIT(ev: int, nbytes: int) -> int:      # _IOC(_IOC_READ,'E',0x20+ev,len)
    return 0x80000000 | (ord("E") << 8) | (0x20 + ev) | (nbytes << 16)


EVIOCGNAME_256 = 0x80000000 | (ord("E") << 8) | 0x06 | (256 << 16)
EVIOCGRAB = 0x40000000 | (ord("E") << 8) | 0x90 | (4 << 16)   # _IOW('E',0x90,int)


# ------------------------------------------------------------- sysfs helpers
def _read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return default


def usb_parent_ids(path: str):
    """Walk up a sysfs path to the USB device node that owns it."""
    p = os.path.realpath(path)
    for _ in range(12):
        vid = _read_text(os.path.join(p, "idVendor"))
        pid = _read_text(os.path.join(p, "idProduct"))
        if vid and pid:
            return vid.lower(), pid.lower()
        parent = os.path.dirname(p)
        if parent == p or parent == "/":
            break
        p = parent
    return None, None


def is_oti(path: str) -> bool:
    """Belongs to the cable?  Two independent tests, because getting this wrong
    in `run` would capture the cable's own keyboard/mouse (a feedback loop).
    """
    real = os.path.realpath(path).lower()
    if "%s:%s" % (OTI_VID, OTI_PID) in real:
        # the HID nodes are named like .../0003:0EA0:2213.0002/input/input5
        return True
    vid, pid = usb_parent_ids(real)
    return vid == OTI_VID and pid == OTI_PID


def find_oti_sg():
    """[(/dev/sgN, model), ...] belonging to the cable, LUN0 (disk) first."""
    found = []
    for sg in sorted(glob.glob("/dev/sg*"),
                     key=lambda s: int(s[7:]) if s[7:].isdigit() else 999):
        sysdev = "/sys/class/scsi_generic/%s/device" % os.path.basename(sg)
        if not os.path.exists(sysdev) or not is_oti(sysdev):
            continue
        model = _read_text(os.path.join(sysdev, "model"))
        found.append((sg, model))
    found.sort(key=lambda t: 0 if "virtual" in t[1].lower() else 1)
    return found


def pick_sg(args) -> str:
    """The device to send on: --device, else the cable, else a clear error."""
    if getattr(args, "device", None):
        return args.device
    found = find_oti_sg()
    if not found:
        raise SystemExit("[!] cable not found (usb %s:%s).  Plug it in, or pass "
                         "--device /dev/sgN (see `list`)." % (OTI_VID, OTI_PID))
    return found[0][0]


def all_sg():
    out = []
    for sg in sorted(glob.glob("/dev/sg*"),
                     key=lambda s: int(s[7:]) if s[7:].isdigit() else 999):
        sysdev = "/sys/class/scsi_generic/%s/device" % os.path.basename(sg)
        vid, pid = usb_parent_ids(sysdev)
        out.append((sg, _read_text(os.path.join(sysdev, "vendor")) + " " +
                    _read_text(os.path.join(sysdev, "model")),
                    "%s:%s" % (vid, pid)))
    return out


# --------------------------------------------------------------- sg transport
class SgTransport:
    """Send the OTi packet as a 16-byte SCSI CDB through SG_IO."""

    SG_IO = 0x2285
    SG_DXFER_NONE = -1
    SG_INTERFACE_ID = 0x53          # 'S'
    SENSE_LEN = 32

    class sg_io_hdr(ctypes.Structure):
        _fields_ = [
            ("interface_id", ctypes.c_int32),
            ("dxfer_direction", ctypes.c_int32),
            ("cmd_len", ctypes.c_uint8),
            ("mx_sb_len", ctypes.c_uint8),
            ("iovec_count", ctypes.c_uint16),
            ("dxfer_len", ctypes.c_uint32),
            ("dxferp", ctypes.c_void_p),
            ("cmdp", ctypes.c_void_p),
            ("sbp", ctypes.c_void_p),
            ("timeout", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("pack_id", ctypes.c_int32),
            ("usr_ptr", ctypes.c_void_p),
            ("status", ctypes.c_uint8),
            ("masked_status", ctypes.c_uint8),
            ("msg_status", ctypes.c_uint8),
            ("sb_len_wr", ctypes.c_uint8),
            ("host_status", ctypes.c_uint16),
            ("driver_status", ctypes.c_uint16),
            ("resid", ctypes.c_int32),
            ("duration", ctypes.c_uint32),
            ("info", ctypes.c_uint32),
        ]

    def __init__(self, path: str, timeout_ms: int = 5000):
        if fcntl is None:
            raise OSError("fcntl is unavailable: this build only runs on Linux")
        self.path = path
        self.timeout_ms = timeout_ms
        self.last_status = None
        self.last_sense = b""
        # No O_NONBLOCK: a blocking SG_IO waits for the command to finish, which
        # is what the Windows IOCTL_SCSI_PASS_THROUGH_DIRECT call did too.
        self.fd = os.open(path, os.O_RDWR)
        if ctypes.sizeof(self.sg_io_hdr) != 88:
            raise OSError("sg_io_hdr is %d bytes, expected 88 (64-bit build)"
                          % ctypes.sizeof(self.sg_io_hdr))

    def send(self, packet: bytes, timeout_ms=None) -> bool:
        if len(packet) != 16:
            raise ValueError("the OTi packet must be exactly 16 bytes")
        cdb = ctypes.create_string_buffer(packet, 16)
        sense = ctypes.create_string_buffer(self.SENSE_LEN)
        hdr = self.sg_io_hdr()
        hdr.interface_id = self.SG_INTERFACE_ID
        hdr.dxfer_direction = self.SG_DXFER_NONE
        hdr.cmd_len = 16
        hdr.mx_sb_len = self.SENSE_LEN
        hdr.dxfer_len = 0
        hdr.dxferp = None
        hdr.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
        hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
        hdr.timeout = int(self.timeout_ms if timeout_ms is None else timeout_ms)
        try:
            fcntl.ioctl(self.fd, self.SG_IO, hdr)
        except OSError as exc:
            self.last_status = str(exc)
            return False
        self.last_status = hdr.status
        self.last_sense = sense.raw[:hdr.sb_len_wr] if hdr.sb_len_wr else b""
        return hdr.status == 0

    def inquiry(self) -> str:
        """A normal INQUIRY, to prove the SG_IO plumbing works."""
        cdb = ctypes.create_string_buffer(bytes([0x12, 0, 0, 0, 36, 0]), 16)
        data = ctypes.create_string_buffer(36)
        sense = ctypes.create_string_buffer(self.SENSE_LEN)
        hdr = self.sg_io_hdr()
        hdr.interface_id = self.SG_INTERFACE_ID
        hdr.dxfer_direction = -3            # SG_DXFER_FROM_DEV
        hdr.cmd_len = 6
        hdr.mx_sb_len = self.SENSE_LEN
        hdr.dxfer_len = 36
        hdr.dxferp = ctypes.cast(data, ctypes.c_void_p)
        hdr.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
        hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
        hdr.timeout = 5000
        fcntl.ioctl(self.fd, self.SG_IO, hdr)
        if hdr.status != 0:
            return "<status %d, sense %s>" % (hdr.status, sense.raw[:8].hex(" "))
        raw = data.raw
        return "%s %s %s" % (raw[8:16].decode("latin-1").strip(),
                             raw[16:32].decode("latin-1").strip(),
                             raw[32:36].decode("latin-1").strip())

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ------------------------------------------------------------- evdev wrapper
class EvDev:
    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.name = self._name()
        self.key_bits = self._bits(EV_KEY, 96)
        self.rel_bits = self._bits(EV_REL, 8)
        self.grabbed = False
        self.buf = b""
        self.is_keyboard = self.has_key(KEY_A)
        self.is_mouse = (self.has_rel(REL_X) or self.has_rel(REL_Y)
                         or self.has_key(BTN_LEFT)) and not self.is_keyboard

    def _name(self) -> str:
        buf = ctypes.create_string_buffer(256)
        try:
            fcntl.ioctl(self.fd, EVIOCGNAME_256, buf)
        except OSError:
            return os.path.basename(self.path)
        return buf.value.decode("latin-1", "replace")

    def _bits(self, ev: int, nbytes: int) -> bytes:
        buf = ctypes.create_string_buffer(nbytes)
        try:
            fcntl.ioctl(self.fd, EVIOCGBIT(ev, nbytes), buf)
        except OSError:
            return b""
        return buf.raw

    @staticmethod
    def _bit(bitmap: bytes, code: int) -> bool:
        idx = code // 8
        if idx >= len(bitmap):
            return False
        return bool(bitmap[idx] >> (code % 8) & 1)

    def has_key(self, code: int) -> bool:
        return self._bit(self.key_bits, code)

    def has_rel(self, code: int) -> bool:
        return self._bit(self.rel_bits, code)

    def grab(self, on: bool = True):
        if on == self.grabbed:
            return
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)
            self.grabbed = on
        except OSError as exc:
            print("[!] cannot grab %s (%s): %s" % (self.path, self.name, exc))

    def events(self):
        """Non-blocking read -> [(type, code, value), ...]."""
        try:
            data = os.read(self.fd, INPUT_EVENT.size * 64)
        except BlockingIOError:
            return []
        except OSError:
            return []
        out = []
        self.buf += data
        while len(self.buf) >= INPUT_EVENT.size:
            chunk = self.buf[:INPUT_EVENT.size]
            self.buf = self.buf[INPUT_EVENT.size:]
            _s, _us, etype, code, value = INPUT_EVENT.unpack(chunk)
            out.append((etype, code, value))
        return out

    def close(self):
        if self.grabbed:
            self.grab(False)
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_input_devices(keep_oti: bool = False):
    """Open every event device that is not the cable's own HID interface."""
    devs = []
    for path in sorted(glob.glob("/dev/input/event*"),
                       key=lambda s: int(s.rsplit("event", 1)[1])):
        sysname = os.path.basename(path)
        if not keep_oti and is_oti("/sys/class/input/%s/device" % sysname):
            continue
        try:
            devs.append(EvDev(path))
        except OSError as exc:
            print("[!] cannot open %s: %s" % (path, exc))
    return devs


def open_oti_devices():
    """Open the cable's *own* HID interfaces (the receive side).

    The switcher never forwards from these - they are what the other machine's
    keystrokes arrive on - but it does watch them for the other machine's
    heartbeat (F24), which is the only proof that the other machine is on.
    """
    devs = []
    for path in sorted(glob.glob("/dev/input/event*"),
                       key=lambda s: int(s.rsplit("event", 1)[1])):
        sysname = os.path.basename(path)
        if not is_oti("/sys/class/input/%s/device" % sysname):
            continue
        try:
            devs.append(EvDev(path))
        except OSError as exc:
            print("[!] cannot open %s: %s" % (path, exc))
    return devs


# ------------------------------------------------------------------ config
CONF_NAME = "kmlink.conf"
CONF_DEFAULTS = {"role": "main", "auto": "yes",
                 "peer_timeout": "5", "hb_interval": "2"}


def config_path() -> str:
    """kmlink.conf sits next to the script (or in /opt/kmlink when installed)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONF_NAME)


def load_config(path: str | None = None) -> dict:
    """Read `key = value` lines ('#' comments) from kmlink.conf.

    The installer writes it after asking you, but it is plain text on purpose:
    edit it by hand whenever you like, `run` reads it on start.

        role = main          this machine owns the keyboard/mouse by default
        role = secondary     the other machine is the one you normally drive,
                             so `run` starts out pointing at it
        auto = yes           route by the other machine's heartbeat
        peer_timeout = 5     seconds of silence and the input comes back here
        hb_interval = 2      seconds between the heartbeats we send

    "Is the other machine on?" cannot be seen over the cable directly - each end
    only sees its own end.  With `auto = yes` the two scripts keep each other
    awake with one tiny F24 report every `hb_interval` seconds, so `run` can
    tell "the other machine is off" from "the other machine is on".  That needs
    this script running on the other machine too; set `auto = no` if it is not.
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


# ------------------------------------------------------------------ commands
def cmd_list(args):
    print("== OTi cable (usb %s:%s) ==" % (OTI_VID, OTI_PID))
    sg = find_oti_sg()
    if not sg:
        print("  no /dev/sgN found for the cable.")
        print("  (is it plugged in? are you root? /dev/sg* needs root:disk)")
    for path, model in sg:
        print("  %-12s model=%r" % (path, model))
    print()
    print("== all SCSI generic devices ==")
    for path, model, ids in all_sg():
        mark = "  <-- cable" if ids == "%s:%s" % (OTI_VID, OTI_PID) else ""
        print("  %-12s %-28s usb=%s%s" % (path, model, ids, mark))
    print()
    print("== input devices ==")
    for path in sorted(glob.glob("/dev/input/event*"),
                       key=lambda s: int(s.rsplit("event", 1)[1])):
        sysname = os.path.basename(path)
        name = _read_text("/sys/class/input/%s/device/name" % sysname, "?")
        vid, pid = usb_parent_ids("/sys/class/input/%s/device" % sysname)
        oti = "  <-- cable's HID (RECEIVE side, excluded)" \
            if (vid, pid) == (OTI_VID, OTI_PID) else ""
        print("  %-18s %-38s usb=%s:%s%s" % (path, name, vid, pid, oti))
    return 0


def cmd_diag(args):
    print("euid            :", os.geteuid() if hasattr(os, "geteuid") else "?")
    print("python          :", sys.version.split()[0])
    print("sg_io_hdr size  :", ctypes.sizeof(SgTransport.sg_io_hdr), "(must be 88)")
    print("input_event size:", INPUT_EVENT.size, "(must be 24)")
    print()
    sg = find_oti_sg()
    if not sg:
        print("[!] cable not found.  Run `list` and check the USB ids.")
        return 1
    print("cable           :", ", ".join("%s (%s)" % t for t in sg))
    path = args.device or sg[0][0]
    print("trying          :", path)
    try:
        t = SgTransport(path)
    except OSError as exc:
        print("[!] open failed:", exc)
        return 1
    try:
        print("INQUIRY         :", t.inquiry())
        for label, pkt in (("kbd 'a' press", oti_keyboard(0, [0x04])),
                           ("kbd 'a' release", oti_keyboard(0, [])),
                           ("mouse +5", oti_mouse(0, 5, 0))):
            ok = t.send(pkt)
            print("  %-16s %s ok=%s status=%s sense=%s"
                  % (label, pkt.hex(" "), ok, t.last_status,
                     t.last_sense[:8].hex(" ") if t.last_sense else "-"))
            time.sleep(0.2)
    finally:
        t.close()
    print()
    print("input devices the switcher would use:")
    for d in open_input_devices():
        kind = "keyboard" if d.is_keyboard else ("mouse" if d.is_mouse else "other")
        print("  %-18s %-38s %s" % (d.path, d.name, kind))
        d.close()
    return 0


def cmd_key(args):
    if os.geteuid() != 0:
        print("[!] needs root: sudo python3 %s %s" % (sys.argv[0], args.command))
        return 1
    letter = (args.arg or "a").lower()[:1]
    ev_code = LETTER_CODES.get(letter)
    usage = KEY_TO_HID.get(ev_code, 0x04) if ev_code else 0x04
    print("key %r -> evdev %s -> HID usage 0x%02X" % (letter, ev_code, usage))
    t = SgTransport(pick_sg(args))
    try:
        for pkt in (oti_keyboard(0, [usage]), oti_keyboard(0, [])):
            print("%s ok=%s" % (pkt.hex(" "), t.send(pkt)))
            time.sleep(0.2)
    finally:
        t.close()
    return 0


def cmd_mousetest(args):
    print("Watch the OTHER computer's pointer and note each step.")
    t = SgTransport(pick_sg(args))

    def burst(label, n, dx=0, dy=0, wheel=0, delay=0.02):
        print("--- %s ---" % label, flush=True)
        for _ in range(n):
            t.send(oti_mouse(0, dx, dy, wheel))
            time.sleep(delay)
        time.sleep(0.8)

    try:
        burst("1. 10 x X=+30  (expect RIGHT)", 10, dx=30)
        burst("2. 10 x X=-30  (expect LEFT)", 10, dx=-30)
        burst("3. 10 x Y=+30  (expect DOWN)", 10, dy=30)
        burst("4. 10 x Y=-30  (expect UP)", 10, dy=-30)
        burst("5. one X=+120", 1, dx=120)
        burst("6. one X=-120", 1, dx=-120)
        burst("7. 3 x wheel +1 (expect scroll UP)", 3, wheel=1)
        burst("8. 3 x wheel -1 (expect scroll DOWN)", 3, wheel=-1)
        print("--- 9. one big move X=+900 (split into packets) ---", flush=True)
        for pkt in mouse_move_chunks(0, 900, 0):
            t.send(pkt)
            time.sleep(0.005)
        time.sleep(0.8)
    finally:
        t.close()
    return 0


def cmd_watch(args):
    """Read-only: show every key event and every raw mouse delta.  No root."""
    devs = open_input_devices()
    if not devs:
        print("[!] no input devices could be opened.")
        print("    Add yourself to the 'input' group and re-login, or use sudo.")
        return 1
    print("watching %d input device(s) - nothing is forwarded or grabbed:"
          % len(devs))
    for d in devs:
        kind = "keyboard" if d.is_keyboard else ("mouse" if d.is_mouse else "other")
        print("  %-18s %-38s %s" % (d.path, d.name, kind))
    print("Press Alt+S a few times, wiggle the mouse, then Ctrl+C.")
    fds = {d.fd: d for d in devs}
    counts = {"n": 0, "dx": 0, "dy": 0, "wheel": 0, "btn": 0}
    last = time.monotonic()
    try:
        while True:
            ready, _, _ = select.select(list(fds), [], [], 0.5)
            for fd in ready:
                dev = fds[fd]
                for etype, code, value in dev.events():
                    if etype == EV_KEY:
                        if code in MOD_BITS:
                            print("  KEY  %-38s %-10s mod-bit=0x%02X"
                                  % (dev.name[:38], "DOWN" if value else "up",
                                     MOD_BITS[code]), flush=True)
                        elif code in KEY_TO_HID:
                            print("  KEY  %-38s code=%-4d usage=0x%02X %s"
                                  % (dev.name[:38], code, KEY_TO_HID[code],
                                     "DOWN" if value else "up"), flush=True)
                        elif code in BTN_BITS:
                            counts["btn"] += 1
                            print("  BTN  %-38s code=0x%X %s"
                                  % (dev.name[:38], code,
                                     "DOWN" if value else "up"), flush=True)
                    elif etype == EV_REL:
                        counts["n"] += 1
                        if code == REL_X:
                            counts["dx"] += value
                        elif code == REL_Y:
                            counts["dy"] += value
                        elif code == REL_WHEEL:
                            counts["wheel"] += value
            now = time.monotonic()
            if now - last >= 1.5:
                last = now
                print("  [1.5s] rel events=%d dx=%d dy=%d wheel=%d buttons=%d"
                      % (counts["n"], counts["dx"], counts["dy"],
                         counts["wheel"], counts["btn"]), flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        for d in devs:
            d.close()
    return 0


def cmd_run(args):
    """Capture the local keyboard/mouse and forward while 'remote'."""
    if os.geteuid() != 0:
        print("[!] run needs root:")
        print("      sudo python3 %s run" % sys.argv[0])
        print("    or use install-autostart.sh to run it at boot.")
        return 1

    interval = 1.0 / max(10, int(args.rate))

    while True:
        try:
            # 1 s per packet: bounded stall if the cable is yanked mid-session
            # (a healthy packet takes about 0.5 ms).  The retry loop means
            # booting without the cable attached is fine.
            sg = SgTransport(args.device or find_oti_sg()[0][0], timeout_ms=1000)
            break
        except (OSError, IndexError) as exc:
            print("[!] %s" % exc, flush=True)
            print("[!] Is the cable plugged in? Retrying in 5 s "
                  "(Ctrl+C to stop).", flush=True)
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print()
                return 1

    devs = open_input_devices()
    kbd = [d for d in devs if d.is_keyboard]
    mouse = [d for d in devs if d.is_mouse]
    oti_devs = open_oti_devices()
    print("[i] SG_IO transport on %s (INQUIRY %s)" % (sg.path, sg.inquiry()))
    print("[i] keyboards: %s" % (", ".join(d.name for d in kbd) or "none"))
    print("[i] mice     : %s" % (", ".join(d.name for d in mouse) or "none"))

    # Which machine owns the keyboard/mouse at start-up?  role=main (default)
    # keeps them here; role=secondary means the other machine is the one you
    # normally drive, so start out pointing at it.  install-autostart.sh asks
    # the question once and records the answer in kmlink.conf.
    settings = pick_settings(args)
    role, auto = settings["role"], settings["auto"]
    peer_timeout, hb_interval = settings["peer_timeout"], settings["hb_interval"]
    want_remote = bool(args.start_remote) or role == "secondary"
    state = {"remote": False, "desired": False, "mods": 0}
    # "Is the other machine on?" cannot be seen over the cable directly (each
    # end only sees its own end), so the two scripts keep each other awake with
    # one F24 report - a key nothing uses - every hb_interval seconds.  Hearing
    # one on the cable's own HID keyboard is proof that the other machine is up
    # and running this script; peer_timeout of silence means it is not.
    peer = {"last": 0.0, "ever": False}
    print("[i] role = %s (%s) -> %s"
          % (role, "--role" if getattr(args, "role", None) else config_path(),
             "starting remote" if want_remote else "starting local"), flush=True)
    print("[i] auto = %s%s"
          % ("yes" if auto else "no",
             ("  (heartbeat every %.1fs, gone after %.1fs)"
              % (hb_interval, peer_timeout)) if auto
             else "  (the other PC is never checked)"), flush=True)
    pressed = []                      # HID usages currently held
    acc = {"dx": 0, "dy": 0, "wheel": 0, "hwheel": 0, "btn": 0, "dirty": False}

    def flush(force=False):
        dx, dy = acc["dx"], acc["dy"]
        wheel, hwheel = acc["wheel"], acc["hwheel"]
        btn, dirty = acc["btn"], acc["dirty"]
        acc["dx"] = acc["dy"] = acc["wheel"] = acc["hwheel"] = 0
        acc["dirty"] = False
        if not (dx or dy or wheel or hwheel or dirty or force):
            return
        if dx or dy:
            steps = max(1, (max(abs(dx), abs(dy)) + 126) // 127)
            if steps > 24:                      # cable lagging: clamp the backlog
                f = steps / 24.0
                dx, dy = int(dx / f), int(dy / f)
            for pkt in mouse_move_chunks(btn, dx, dy):
                sg.send(pkt)
            if wheel or hwheel:
                sg.send(oti_mouse(btn, 0, 0, wheel, hwheel))
        elif wheel or hwheel or dirty or force:
            sg.send(oti_mouse(btn, 0, 0, wheel, hwheel))

    def set_remote(v):
        state["remote"] = state["desired"] = bool(v)
        state["mods"] = 0
        pressed.clear()
        acc.update(dx=0, dy=0, wheel=0, hwheel=0, btn=0, dirty=True)
        if v:
            # Grab only real keyboards/pointers: grabbing a power button or a
            # lid switch would break suspend on the machine we are sitting at.
            caps = [d for d in devs if d.is_keyboard or d.is_mouse]
            if not caps:
                print("[!] no keyboard/pointer to capture - staying local.",
                      flush=True)
                state["remote"] = False
                flush(force=True)
                print("[i] remote = False  (keyboard+mouse go to this PC)",
                      flush=True)
                return
            for d in caps:
                d.grab(True)                    # swallow locally, like the
        else:                                   # Windows hook returning 1
            for d in devs:
                d.grab(False)
        # never leave the other PC holding a key or a mouse button
        sg.send(oti_keyboard(0, []))
        flush(force=True)
        print("[i] remote = %s  (keyboard+mouse go to %s)"
              % (v, "the other PC" if v else "this PC"), flush=True)

    # ------------------------------------------------- is the other PC up?
    def peer_present():
        """True while the other machine's heartbeat is recent."""
        return peer["ever"] and \
            (time.monotonic() - peer["last"]) <= peer_timeout

    def peer_gone_note(what):
        print("[!] %s: no heartbeat from the other PC for %.0f s.  Is "
              "km_link_switch running there?" % (what, peer_timeout), flush=True)

    def read_peer(fd):
        """Only KEY_F24 counts here: that is the other script's heartbeat."""
        for etype, code, value in oti_fds[fd].events():
            if etype == EV_KEY and code == KEY_F24 and value == 1:
                peer["last"] = time.monotonic()
                peer["ever"] = True
                if args.keys:
                    print("[k] heartbeat from the other PC", flush=True)

    def pump_peer(timeout):
        """Watch the cable's own HID for the peer's heartbeat while we wait.

        Our heartbeat is sent from here too: if both machines boot at the same
        moment, each one needs the other's heartbeat as much as it needs its own.
        """
        deadline = time.monotonic() + timeout
        next_hb = 0.0
        while time.monotonic() < deadline and not peer_present():
            if auto and time.monotonic() >= next_hb:
                sg.send(oti_keyboard(0, [HID_F24]))
                sg.send(oti_keyboard(0, []))
                next_hb = time.monotonic() + hb_interval
            ready, _, _ = select.select(list(oti_fds), [], [], 0.1)
            for fd in ready:
                read_peer(fd)

    def enforce_presence():
        """Never leave the keyboard/mouse pointing at a machine that is gone."""
        if auto and state["remote"] and not peer_present():
            peer_gone_note("the other PC stopped answering")
            print("[i] bringing the keyboard/mouse back to this PC "
                  "(press Alt+S to try again).", flush=True)
            set_remote(False)

    def toggle():
        if state["remote"]:
            set_remote(False)
            return
        if auto and not peer_present():
            peer_gone_note("the other PC is not answering")
            print("[i] staying here.  (Set 'auto = no' in kmlink.conf to switch "
                  "without this check.)", flush=True)
            return
        set_remote(True)

    fds = {d.fd: d for d in devs}
    if not fds:
        print("[!] no input devices found - nothing to capture.")
        return 1
    oti_fds = {d.fd: d for d in oti_devs}
    watch = dict(fds)
    watch.update(oti_fds)          # the cable's HID is watched, never captured
    if auto and not oti_fds:
        print("[!] the cable's HID keyboard is not here, so the other PC cannot "
              "be seen; this machine stays local.", flush=True)

    try:
        if auto and want_remote:
            print("[i] checking the other PC (up to %.1f s)..." % peer_timeout,
                  flush=True)
            pump_peer(peer_timeout)
        if want_remote and auto and not peer_present():
            peer_gone_note("the other PC did not answer")
            print("[i] starting local instead (press Alt+S once it is up).",
                  flush=True)
        set_remote(want_remote and (peer_present() or not auto))
        print("[i] running: Alt+S switches, Ctrl+C quits.", flush=True)
        heartbeat = 0.0
        while True:
            ready, _, _ = select.select(list(watch), [], [], interval)
            for fd in ready:
                if fd in oti_fds:
                    read_peer(fd)
                    continue
                dev = fds[fd]
                for etype, code, value in dev.events():
                    if etype == EV_KEY:
                        down = value in (1, 2)      # 2 = autorepeat
                        if code in BTN_BITS:        # mouse buttons travel in
                            if state["remote"]:     # the mouse packet, not by
                                if value:           # the keyboard path
                                    acc["btn"] |= BTN_BITS[code]
                                else:
                                    acc["btn"] &= ~BTN_BITS[code]
                                acc["dirty"] = True
                            continue
                        if code in MOD_BITS:
                            if value == 2:
                                pass
                            elif down:
                                state["mods"] |= MOD_BITS[code]
                            else:
                                state["mods"] &= ~MOD_BITS[code]
                            if args.keys:
                                print("[k] %-12s code=%-4d mods=0x%02X"
                                      % (dev.name[:12], code, state["mods"]),
                                      flush=True)
                            if state["remote"]:
                                sg.send(oti_keyboard(state["mods"],
                                                     list(pressed[:6])))
                            continue
                        alt = bool(state["mods"] & ALT_MASK)
                        if down and code == KEY_S and alt:
                            toggle()                             # Alt+S
                            continue
                        usage = KEY_TO_HID.get(code)
                        if usage is not None:
                            if value == 2:
                                pass
                            elif down and usage not in pressed:
                                pressed.append(usage)
                            elif not down and usage in pressed:
                                pressed.remove(usage)
                        if args.keys:
                            print("[k] %-12s code=%-4d usage=%s mods=0x%02X "
                                  "pressed=%s" % (dev.name[:12], code,
                                                  ("0x%02X" % usage) if usage
                                                  else "unmapped",
                                                  state["mods"], pressed),
                                  flush=True)
                        if state["remote"]:
                            sg.send(oti_keyboard(state["mods"],
                                                 list(pressed[:6])))
                    elif etype == EV_REL and state["remote"]:
                        if code == REL_X:
                            acc["dx"] += value
                        elif code == REL_Y:
                            acc["dy"] += value
                        elif code == REL_WHEEL:
                            acc["wheel"] += value
                        elif code == REL_HWHEEL:
                            acc["hwheel"] += value
            # Keep the other machine able to see that this one is on.  Skip
            # while a key is held: the heartbeat is a full keyboard report, so
            # it would briefly clear what the other PC has down.
            if auto and not pressed and time.monotonic() - heartbeat >= hb_interval:
                sg.send(oti_keyboard(0, [HID_F24]))
                sg.send(oti_keyboard(0, []))
                heartbeat = time.monotonic()
            enforce_presence()
            flush()
    except KeyboardInterrupt:
        print()
    finally:
        for d in devs:
            d.close()
        sg.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Direct OTi KM Link cable switcher for Linux "
                    "(no vendor app, no LAN)")
    ap.add_argument("command",
                    choices=["list", "diag", "key", "mousetest", "watch", "run"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--device", help="explicit /dev/sgN (default: find the cable)")
    ap.add_argument("--start-remote", action="store_true",
                    help="start in remote mode")
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
    ap.add_argument("--rate", type=int, default=125,
                    help="run: packets per second when flushing (default 125)")
    ap.add_argument("--keys", action="store_true",
                    help="run: print every key event")
    args = ap.parse_args()

    if args.command == "list":
        return cmd_list(args)
    if args.command == "diag":
        return cmd_diag(args)
    if args.command == "key":
        return cmd_key(args)
    if args.command == "mousetest":
        return cmd_mousetest(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "run":
        return cmd_run(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except OSError as exc:
        print("[!] %s" % exc)
        sys.exit(1)
