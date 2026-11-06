"""End-to-end simulation of the Linux `run` loop.

Fakes evdev devices and the SG_IO transport (both are thin shims), then drives
the real cmd_run() with a scripted event stream and inspects every packet that
would have gone down the cable.  Runs on Windows.
"""
import importlib.util
import os
import sys
from types import SimpleNamespace

import os
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "km_link_switch_linux.py")
spec = importlib.util.spec_from_file_location("kml", PATH)
kml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kml)

os.geteuid = lambda: 0                      # pretend to be root

EV_KEY, EV_REL = kml.EV_KEY, kml.EV_REL


class FakeEv:
    def __init__(self, fd, name, events, keyboard=False, mouse=False):
        self.fd, self.name, self.path = fd, name, name
        self.is_keyboard, self.is_mouse = keyboard, mouse
        self.grabbed = False
        self.pending = list(events)
        self.log = []

    def grab(self, on=True):
        if on == self.grabbed:          # same short-circuit as EvDev.grab
            return
        self.grabbed = on
        self.log.append(("grab", on))

    def events(self):
        # deliver one event per read, like the kernel really does
        if not self.pending:
            return []
        return [self.pending.pop(0)]

    def close(self):
        pass


inner = FakeEv(7, "Fake Keyboard", [
    (EV_KEY, 56, 1),        # Alt down          (local: must not be forwarded)
    (EV_KEY, 31, 1),        # Alt+S  -> REMOTE
    (EV_KEY, 30, 1),        # a down
    (EV_KEY, 30, 0),        # a up
    (EV_REL, kml.REL_X, 20),
    (EV_REL, kml.REL_Y, -5),
    (EV_KEY, 0x110, 1),     # left button down
    (EV_KEY, 56, 0),        # Alt up
    (EV_KEY, 56, 1),        # Alt down again
    (EV_KEY, 31, 1),        # Alt+S  -> LOCAL again
], keyboard=True)
mouse_dev = FakeEv(9, "Fake Mouse", [], mouse=True)
power_dev = FakeEv(11, "Fake Power Button", [])       # neither keyboard nor mouse
devices = [inner, mouse_dev, power_dev]

SENT = []


class FakeSg:
    def __init__(self, path, timeout_ms=5000):
        self.path = path
        self.timeout_ms = timeout_ms

    def inquiry(self):
        return "TEST"

    def send(self, packet, timeout_ms=5000):
        SENT.append(packet.hex(" "))
        return True

    def close(self):
        pass


kml.open_input_devices = lambda keep_oti=False: devices
kml.open_oti_devices = lambda: []
kml.find_oti_sg = lambda: [("/dev/sg9", "Virtual Link")]
kml.SgTransport = FakeSg


def fake_select(fds, w, x, timeout):
    if not any(d.pending for d in devices):
        raise KeyboardInterrupt                      # event script exhausted
    return (list(fds), [], [])


kml.select.select = fake_select

# auto=False on purpose: this test is about the plain Alt+S / forward / grab
# state machine.  The heartbeat and the presence gating have their own test
# (tests/test_linux_presence.py), so keep this one hardware-deterministic.
rc = kml.cmd_run(SimpleNamespace(device=None, rate=125, keys=False,
                                 start_remote=False, auto=False,
                                 config=os.path.join(PATH + ".none")))

print("kept the release packet inside the script: yes (remote toggled)")
print()
for i, pkt in enumerate(SENT, 1):
    print("%2d. %s" % (i, pkt))

fails = []


def check(name, got, want):
    if got != want:
        fails.append(name)
    print("%-52s %s" % (name, "OK" if got == want else "!="))


RELEASE = "d9 34 00 00 00 00 00 00 00 00 00 00 00 00 4f 54"
MOUSE0 = "d9 33 00 00 00 00 00 00 00 00 00 00 00 00 4f 54"
A_DOWN = "d9 34 00 00 04 00 00 00 00 00 00 00 00 00 4f 54"
MOVE_X = "d9 33 00 14 00 00 00 00 00 00 00 00 00 00 4f 54"
MOVE_Y = "d9 33 00 00 fb 00 00 00 00 00 00 00 00 00 4f 54"
BTN = "d9 33 01 00 00 00 00 00 00 00 00 00 00 00 4f 54"
ALT_DOWN = "d9 34 04 00 00 00 00 00 00 00 00 00 00 00 4f 54"

print()
check(" 1 startup queues a release packet", SENT[0], RELEASE)
check(" 2 startup queues a zeroed mouse packet", SENT[1], MOUSE0)
check(" 3 Alt+S -> remote: release packet", SENT[2], RELEASE)
check(" 4 Alt+S -> remote: zeroed mouse", SENT[3], MOUSE0)
check(" 5 'a' press forwarded", SENT[4], A_DOWN)
check(" 6 'a' release forwarded", SENT[5], RELEASE)
check(" 7 REL_X +20 forwarded", SENT[6], MOVE_X)
check(" 8 REL_Y -5 forwarded (sign kept)", SENT[7], MOVE_Y)
check(" 9 left button down forwarded", SENT[8], BTN)
check("10 Alt release forwarded", SENT[9], RELEASE)
check("11 Alt held alone forwarded (mod bit 0x04)",
      SENT[10], ALT_DOWN)
check("12 second Alt+S -> local: release packet", SENT[11], RELEASE)
check("13 second Alt+S -> local: button force-freed", SENT[12], MOUSE0)
check("14 the Alt+S keystroke itself never reaches the other PC",
      [p for p in SENT if p[6:8] == "04" and p.startswith("d9 34 04")],
      [ALT_DOWN])
check("packet count", len(SENT), 13)
check("keyboard+pointer grabbed while remote",
      inner.log, [("grab", True), ("grab", False)])
check("mouse device grabbed/un-grabbed too",
      mouse_dev.log, [("grab", True), ("grab", False)])
check("non-keyboard/mouse devices are never grabbed (power button, lid)",
      power_dev.log, [])
check("run() returned cleanly on Ctrl+C", rc, 0)

if fails:
    print("\nFAILED:", ", ".join(fails))
    sys.exit(1)
print("\nall run-loop checks passed")
