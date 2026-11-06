"""Simulation of the heartbeat routing in the Linux `run` loop.

No hardware and no waiting: a fake evdev set, a fake SG_IO transport and a fake
clock drive the real cmd_run(), so the rules the user asked for are checked
exactly:

  * the MAIN machine owns the keyboard no matter what the other one does, and
    it still announces itself (heartbeat) while it is local;
  * the SECONDARY forwards to the main only while the main answers;
  * Alt+S is refused while the other machine is not answering, so the input can
    never end up pointing at a machine that is off;
  * if the other machine goes silent mid-session, the input comes straight back
    home instead of staying disconnected.

Run:  python tests/test_linux_presence.py
"""
import contextlib
import importlib.util
import io
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "km_link_switch_linux.py")
spec = importlib.util.spec_from_file_location("kml_presence", SCRIPT)
kml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kml)

os.geteuid = lambda: 0                      # pretend to be root
EV_KEY = kml.EV_KEY
KEY_S, KEY_F24 = kml.KEY_S, kml.KEY_F24

STEPS = [0]                                  # the simulated loop iteration
SENT = []

checks = 0
failed = []


def check(name, cond):
    global checks
    checks += 1
    if not cond:
        failed.append(name)
        print("  FAIL  %s" % name)


def check_order(name, out, first, second):
    check(name, first in out and second in out and out.index(first) < out.index(second))


class Clock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t


clock = Clock()


class FakeEv:
    """Delivers (step, event) pairs on the steps listed, one read at a time."""

    def __init__(self, fd, name, schedule=(), keyboard=False, mouse=False):
        self.fd, self.name, self.path = fd, name, name
        self.is_keyboard, self.is_mouse = keyboard, mouse
        self.grabbed = False
        self.schedule = list(schedule)
        self.log = []

    def grab(self, on=True):
        if on == self.grabbed:
            return
        self.grabbed = on
        self.log.append(("grab", on))

    @property
    def pending(self):
        return bool(self.schedule and self.schedule[0][0] <= STEPS[0])

    def events(self):
        out = []
        while self.schedule and self.schedule[0][0] <= STEPS[0]:
            out.append(self.schedule.pop(0)[1])
        return out

    def close(self):
        self.grabbed = False


class FakeSg:
    def __init__(self, path, timeout_ms=5000):
        self.path, self.timeout_ms = path, timeout_ms

    def inquiry(self):
        return "TEST"

    def send(self, packet, timeout_ms=5000):
        SENT.append(packet.hex(" "))
        return True

    def close(self):
        pass


def fake_select(fds, w, x, timeout):
    STEPS[0] += 1
    clock.t += timeout                       # time passes, like a real tick
    if STEPS[0] > 40:
        raise KeyboardInterrupt              # end of the scripted scenario
    return ([fd for fd in fds if fds_by_fd[fd].pending], [], [])


fds_by_fd = {}
kml.select.select = fake_select
kml.SgTransport = FakeSg
kml.find_oti_sg = lambda: [("/dev/sg9", "Virtual Link")]
kml.time.monotonic = clock.monotonic


def simulate(role, local_schedule=(), oti_schedule=(), start_remote=False,
             auto=True, hb=0.2, timeout=0.5):
    """Run cmd_run() through one scripted scenario; return (stdout, devices)."""
    global fds_by_fd
    SENT.clear()
    STEPS[0] = 0
    clock.t = 100.0
    oti = FakeEv(21, "OTi cable HID", oti_schedule, keyboard=True)
    kbd = FakeEv(7, "Local Keyboard", local_schedule, keyboard=True)
    mouse = FakeEv(9, "Local Mouse", (), mouse=True)
    power = FakeEv(11, "Power Button", ())          # must never be grabbed
    fds_by_fd = {d.fd: d for d in (oti, kbd, mouse, power)}
    kml.open_input_devices = lambda keep_oti=False: [kbd, mouse, power]
    kml.open_oti_devices = lambda: [oti]
    args = SimpleNamespace(device=None, rate=10, keys=False,
                           start_remote=start_remote, role=role, auto=auto,
                           config=SCRIPT + ".none", peer_timeout=timeout,
                           hb_interval=hb)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = kml.cmd_run(args)
    return buf.getvalue(), rc, oti, kbd, mouse, power


HEARTBEAT = "d9 34 00 00 73"                 # kbd report carrying F24


print("--- A. secondary, the main answers -> it forwards, then comes home ---")
out, rc, oti, kbd, mouse, power = simulate(
    "secondary",
    oti_schedule=[(1, (EV_KEY, KEY_F24, 1)), (2, (EV_KEY, KEY_F24, 1)),
                  (3, (EV_KEY, KEY_F24, 1))])
check("run() returned cleanly", rc == 0)
check("started remote while the main was answering", "remote = True" in out)
check("it forwards to the other PC", "keyboard+mouse go to the other PC" in out)
check("the heartbeat is sent down the cable",
      any(p.startswith(HEARTBEAT) for p in SENT))
check_order("when the main goes quiet, the input comes home",
            out, "the other PC stopped answering", "remote = False")
check("no dead input: the grab is released again",
      kbd.log == [("grab", True), ("grab", False)])
check("the cable's own HID is never grabbed (it is the receive side)",
      oti.log == [])
check("the power button is never grabbed", power.log == [])
check("we announce ourselves even while waiting at start-up",
      any(p.startswith(HEARTBEAT) for p in SENT[:4]))

print()
print("--- B. secondary, the main is OFF -> local, and Alt+S is refused ---")
# steps 8/9: Alt+S on the local keyboard while nothing answers
# step 14:   the main finally sends a heartbeat
# steps 18/19: Alt+S again, this time it works
out, rc, oti, kbd, mouse, power = simulate(
    "secondary",
    local_schedule=[(8, (EV_KEY, kml.KEY_LEFTALT, 1)),
                    (9, (EV_KEY, KEY_S, 1)),
                    (18, (EV_KEY, kml.KEY_LEFTALT, 1)),
                    (18, (EV_KEY, KEY_S, 1))],
    oti_schedule=[(14, (EV_KEY, KEY_F24, 1))])
check("run() returned cleanly", rc == 0)
check("there was no heartbeat, so it checked and gave up",
      "checking the other PC" in out and "the other PC did not answer" in out)
check_order("and it starts local, not remote", out,
            "starting local instead", "remote = False")
check("never grabs the input before that Alt+S", kbd.log[:1] == [("grab", True)])
check("Alt+S while the main is off is refused",
      "the other PC is not answering" in out)
check("... and tells you how to switch it off",
      "auto = no" in out)
check("remote only appears once the main is heard from",
      out.count("remote = True") == 1
      and out.index("remote = True") > out.index("the other PC is not answering"))
check("it grabbed the input on that Alt+S, exactly once",
      kbd.log.count(("grab", True)) == 1)
check("a single stale heartbeat brings it home again",
      kbd.log[-1] == ("grab", False))

print()
print("--- C. main -> never leaves home, but still heartbeats ---")
out, rc, oti, kbd, mouse, power = simulate(
    "main", oti_schedule=[(1, (EV_KEY, KEY_F24, 1))])
check("run() returned cleanly", rc == 0)
check("starts local", "starting local" in out)
check("does not wait for the other machine", "checking the other PC" not in out)
check("never goes remote even though the other machine is answering",
      "remote = True" not in out)
check("but it does announce itself, so the other machine can see it",
      any(p.startswith(HEARTBEAT) for p in SENT))
check("and it captures nothing", kbd.log == [] and mouse.log == [])

print()
print("--- D. auto = no -> the old blind behaviour, no heartbeats ---")
out, rc, oti, kbd, mouse, power = simulate(
    "main", local_schedule=[(2, (EV_KEY, kml.KEY_LEFTALT, 1)),
                            (3, (EV_KEY, KEY_S, 1))],
    auto=False)
check("Alt+S still works with nothing checking the peer",
      "remote = True" in out)
check("no heartbeat is sent when auto is off",
      not any(p.startswith(HEARTBEAT) for p in SENT))
check("nothing about the peer is printed", "heartbeat" not in out)

print()
if failed:
    print("%d/%d checks failed" % (len(failed), checks))
    sys.exit(1)
print("all %d presence checks passed" % checks)