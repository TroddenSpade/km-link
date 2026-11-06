"""Host-side checks for the Linux port's pure logic.

Runs on Windows too: the module guards `import fcntl`, so every table, packet
builder and struct layout can be verified here where the real device can't.
"""
import ctypes
import importlib.util
import struct
import sys

import os
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "km_link_switch_linux.py")
spec = importlib.util.spec_from_file_location("kml", PATH)
kml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kml)

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print("%-46s %-34s %s" % (name, repr(got), "OK" if ok else "!= %r" % (want,)))


print("=== packet bytes vs the values captured from the real Windows cable ===")
check("kbd 'a' press", kml.oti_keyboard(0, [0x04]).hex(" "),
      "d9 34 00 00 04 00 00 00 00 00 00 00 00 00 4f 54")
check("kbd 'a' release", kml.oti_keyboard(0, []).hex(" "),
      "d9 34 00 00 00 00 00 00 00 00 00 00 00 00 4f 54")
check("mouse right 30", kml.oti_mouse(0, 30, 0).hex(" "),
      "d9 33 00 1e 00 00 00 00 00 00 00 00 00 00 4f 54")
check("mouse left 30 (s8 wrap)", kml.oti_mouse(0, -30, 0)[3], 0xE2)
check("mouse buttons all", kml.oti_mouse(7, 0, 0)[2], 7)
check("mouse dy + = down", kml.oti_mouse(0, 0, 1)[4], 1)
check("wheel byte 3 of payload", kml.oti_mouse(0, 0, 0, 1)[5], 1)
check("all packets are 16 bytes",
      {len(kml.oti_keyboard(0, [0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A])),
       len(kml.oti_mouse(1, 2, 3, 4, 5))}, {16})
check("kbd keeps at most 6 usages",
      kml.oti_keyboard(0, [1, 2, 3, 4, 5, 6, 7, 8])[9], 6)  # byte9 = payload[7] = 6th usage
check("MOD_ALT both sides reach the other PC",
      kml.oti_keyboard(0x44, [])[2], 0x44)

print()
print("=== mouse_move_chunks splits >127 without losing the total ===")
for dx, dy in ((0, 0), (30, 0), (300, 0), (-300, 0), (900, -450), (127, 0)):
    chunks = list(kml.mouse_move_chunks(0, dx, dy))
    sx = sum(c[3] - 256 if c[3] > 127 else c[3] for c in chunks)
    sy = sum(c[4] - 256 if c[4] > 127 else c[4] for c in chunks)
    check("dx=%d dy=%d -> %d chunk(s)" % (dx, dy, len(chunks)),
          (sx, sy), (dx, dy))

print()
print("=== kernel struct layouts (must match 64-bit Linux ABI) ===")
check("sizeof(sg_io_hdr)", ctypes.sizeof(kml.SgTransport.sg_io_hdr), 88)
off = {f: getattr(kml.SgTransport.sg_io_hdr, f).offset
       for f, _ in kml.SgTransport.sg_io_hdr._fields_}
check("offset dxfer_direction", off["dxfer_direction"], 4)
check("offset dxfer_len", off["dxfer_len"], 12)
check("offset dxferp", off["dxferp"], 16)
check("offset cmdp", off["cmdp"], 24)
check("offset sbp", off["sbp"], 32)
check("offset sb_len_wr", off["sb_len_wr"], 67)
check("offset resid", off["resid"], 72)
check("sizeof(input_event)", kml.INPUT_EVENT.size, 24)
check("input_event fields",
      kml.INPUT_EVENT.pack(*kml.INPUT_EVENT.unpack(b"\0" * 24)), b"\0" * 24)
# -> (sec, usec, type, code, value) really is the kernel order
sample = struct.pack("<qqHHi", 1234, 5678, 1, 30, 1)
check("input_event round-trip", kml.INPUT_EVENT.unpack(sample),
      (1234, 5678, 1, 30, 1))

print()
print("=== ioctl request numbers (_IOC encoding) ===")
check("EVIOCGRAB", kml.EVIOCGRAB, 0x40044590)
check("EVIOCGNAME(256)", kml.EVIOCGNAME_256, 0x81004506)
check("EVIOCGBIT(EV_KEY,96)", kml.EVIOCGBIT(kml.EV_KEY, 96), 0x80604521)
check("EVIOCGBIT(EV_REL,8)", kml.EVIOCGBIT(kml.EV_REL, 8), 0x80084522)

print()
print("=== key/button tables ===")
check("letter count", len(kml.LETTER_CODES), 26)
bad = [l for l, c in kml.LETTER_CODES.items()
       if kml.KEY_TO_HID.get(c) != 0x04 + (ord(l) - ord("a"))]
check("every letter maps to its HID usage", bad, [])
check("KEY_S is 's'", kml.KEY_S, kml.LETTER_CODES["s"])
check("alt mask covers both alts",
      kml.MOD_BITS[56] | kml.MOD_BITS[100], kml.ALT_MASK)
check("digit 0", kml.KEY_TO_HID[11], 0x27)
check("F12", kml.KEY_TO_HID[88], 0x45)
check("KEY_ENTER", kml.KEY_TO_HID[28], 0x28)
check("BTN_LEFT bitmask", kml.BTN_BITS[0x110], 1)
check("BTN_RIGHT bitmask", kml.BTN_BITS[0x111], 2)
check("BTN_MIDDLE bitmask", kml.BTN_BITS[0x112], 4)
check("BTN bits are distinct", len(set(kml.BTN_BITS.values())), 5)
check("BTN codes are all >= 0x110 (never mistaken for keys)",
      [c for c in kml.BTN_BITS if c < 0x110], [])

print()
if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("all %d checks passed" % 46)
