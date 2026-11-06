"""Checks the sysfs/evdev discovery helpers against a fake /sys tree.

The real paths are hard-coded for Linux, so instead of faking the filesystem we
build a tree with the same *shape* and call the helpers with an explicit path.
(They only use os.path + open(), which behave the same on Windows.)  Windows
cannot create directories containing ':', so the HID-node name test is done as a
pure string check, which is exactly how the code tests it.
"""
import importlib.util
import os
import shutil
import sys

import os
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "km_link_switch_linux.py")
spec = importlib.util.spec_from_file_location("kml", PATH)
kml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kml)

import tempfile
ROOT = os.path.join(tempfile.gettempdir(), "kmlink_fakesys")
shutil.rmtree(ROOT, ignore_errors=True)


def mk(path):
    full = os.path.join(ROOT, path)
    os.makedirs(full, exist_ok=True)
    return full


def wf(path, name, text):
    with open(os.path.join(ROOT, path, name), "w") as fh:
        fh.write(text + "\n")
    return os.path.join(ROOT, path)


# ---- the cable: one USB device, moving-disk LUN + a HID interface ----------
cable_usb = mk("devices/pci0000-00/usb1/1-2")
wf("devices/pci0000-00/usb1/1-2", "idVendor", "0ea0")
wf("devices/pci0000-00/usb1/1-2", "idProduct", "2213")

storage = mk("devices/pci0000-00/usb1/1-2/1-2-1.0/host4/target4-0-0/4-0-0-0")
wf("devices/pci0000-00/usb1/1-2/1-2-1.0/host4/target4-0-0/4-0-0-0", "vendor", "")
wf("devices/pci0000-00/usb1/1-2/1-2-1.0/host4/target4-0-0/4-0-0-0", "model",
   "Virtual Link")

cable_hid = mk("devices/pci0000-00/usb1/1-2/1-2-1.2/hid1/input/input5")
wf("devices/pci0000-00/usb1/1-2/1-2-1.2/hid1/input/input5", "name",
   "KMLink Keyboard")
cable_ev = mk("devices/pci0000-00/usb1/1-2/1-2-1.2/hid1/input/input5/event6")

# ---- an ordinary USB keyboard on the same hub ------------------------------
real_usb = mk("devices/pci0000-00/usb1/1-3")
wf("devices/pci0000-00/usb1/1-3", "idVendor", "0461")
wf("devices/pci0000-00/usb1/1-3", "idProduct", "4d81")
real_kbd = mk("devices/pci0000-00/usb1/1-3/1-3-1.0/hid2/input/input9/event10")
wf("devices/pci0000-00/usb1/1-3/1-3-1.0/hid2/input/input9", "name",
   "SEM USB Keyboard")

# ---- a PS/2 keyboard (no USB device above it at all) -----------------------
ps2 = mk("devices/platform/i8042/serio0/input/input3/event3")
wf("devices/platform/i8042/serio0/input/input3", "name",
   "AT Translated Set 2 keyboard")

fails = []


def check(name, got, want):
    if got != want:
        fails.append(name)
    print("%-56s %-24s %s" % (name, repr(got), "OK" if got == want else "!= %r" % (want,)))


print("=== the cable is recognised ===")
check("storage LUN -> usb_parent_ids", kml.usb_parent_ids(storage), ("0ea0", "2213"))
check("cable HID event -> usb_parent_ids", kml.usb_parent_ids(cable_ev),
      ("0ea0", "2213"))
check("storage LUN -> is_oti", kml.is_oti(storage), True)
check("cable HID event -> is_oti", kml.is_oti(cable_ev), True)
check("HID node *name* alone is enough (no usb walk needed)",
      kml.is_oti(".../devices/usb1/1-2/1-2-1.2/0003:0EA0:2213.0002/input/input5/event6"),
      True)

print()
print("=== ordinary devices are never mistaken for the cable ===")
check("usb keyboard -> usb_parent_ids", kml.usb_parent_ids(real_kbd), ("0461", "4d81"))
check("usb keyboard -> is_oti", kml.is_oti(real_kbd), False)
check("ps/2 keyboard -> usb_parent_ids", kml.usb_parent_ids(ps2), (None, None))
check("ps/2 keyboard -> is_oti", kml.is_oti(ps2), False)
check("missing path -> is_oti", kml.is_oti(os.path.join(ROOT, "nope")), False)
check("vendor/model readout (for `list`)",
      kml._read_text(os.path.join(storage, "model")), "Virtual Link")

print()
print("=== evdev capability bitmaps (bit N -> byte N//8, bit N%8) ===")
key_map = bytearray(96)
key_map[kml.KEY_A // 8] |= 1 << (kml.KEY_A % 8)
key_map[0x110 // 8] |= 1 << (0x110 % 8)
rel_map = bytearray(8)
rel_map[kml.REL_X // 8] |= 1 << (kml.REL_X % 8)
rel_map[kml.REL_WHEEL // 8] |= 1 << (kml.REL_WHEEL % 8)
check("KEY_A detected", kml.EvDev._bit(bytes(key_map), kml.KEY_A), True)
check("KEY_Z absent", kml.EvDev._bit(bytes(key_map), kml.KEY_Z), False)
check("BTN_LEFT detected", kml.EvDev._bit(bytes(key_map), 0x110), True)
check("BTN_RIGHT absent", kml.EvDev._bit(bytes(key_map), 0x111), False)
check("REL_X detected", kml.EvDev._bit(bytes(rel_map), kml.REL_X), True)
check("REL_Y absent", kml.EvDev._bit(bytes(rel_map), kml.REL_Y), False)
check("REL_WHEEL detected", kml.EvDev._bit(bytes(rel_map), kml.REL_WHEEL), True)
check("bit past the end is False", kml.EvDev._bit(bytes(rel_map), 4000), False)
check("empty bitmap matches nothing", kml.EvDev._bit(b"", kml.KEY_A), False)

print()
print("=== classification rule used by run() ===")
has_key_a = kml.EvDev._bit(bytes(key_map), kml.KEY_A)
has_btn = kml.EvDev._bit(bytes(key_map), 0x110)
has_rel = kml.EvDev._bit(bytes(rel_map), kml.REL_X)
check("keyboard = has KEY_A", has_key_a, True)
check("keyboard with a button -> not mouse", has_btn and not has_key_a, False)
check("rel-only device -> mouse",
      has_rel and not kml.EvDev._bit(bytes(rel_map), kml.KEY_A), True)

shutil.rmtree(ROOT, ignore_errors=True)
print()
if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("all sysfs/discovery checks passed")

