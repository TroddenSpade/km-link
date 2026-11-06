"""Offline checks for the `status` presence probe and for kmlink.conf.

The probe itself needs the cable *and* Administrator rights, so these drive the
code with a fake transport.  That covers everything that is logic rather than
hardware:

  * the bookkeeping of every reply (control / INQUIRY / vendor queries),
  * --save writes them and --diff compares two runs,
  * a flip in a reply is reported as CHANGED, and no flip says so plainly,
  * kmlink.conf is parsed the way both installers write it.

Run:  python tests/test_status_probe.py
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "km_link_switch.py")

checks = 0
failed = []


def check(cond, what):
    global checks
    checks += 1
    if not cond:
        failed.append(what)
        print("  FAIL  %s" % what)


def load_script():
    spec = importlib.util.spec_from_file_location("kml_status", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    if not os.path.exists(SCRIPT):
        print("[!] %s not found" % SCRIPT)
        return 1
    try:
        mod = load_script()
    except Exception as exc:                      # ctypes.wintypes etc.
        print("[i] cannot import the Windows script here (%s) - skipped." % exc)
        return 0

    print("--- kmlink.conf (the round trip both installers write) ---")
    with tempfile.TemporaryDirectory() as tmp:
        conf = os.path.join(tmp, "kmlink.conf")
        # exactly what install-autostart.ps1 writes (Ascii + CRLF, no BOM)
        with open(conf, "wb") as fh:
            fh.write(b"# Written by install-autostart.ps1 - plain text, edit freely.\r\n"
                     b"# main      = this machine owns the keyboard/mouse at start-up\r\n"
                     b"role = secondary\r\n")
        check(mod.load_config(conf)["role"] == "secondary",
              "role=secondary read back from the installer's file")
        check(mod.pick_role(argparse.Namespace(role="Main", config=conf)) == "main",
              "--role overrides the file")
        check(mod.pick_role(argparse.Namespace(role=None, config=conf)) == "secondary",
              "the file is used when --role is absent")
        missing = os.path.join(tmp, "nope.conf")
        check(mod.load_config(missing)["role"] == "main",
              "a missing config defaults to main (no crash)")
        check(mod.pick_role(argparse.Namespace(role=None, config=missing)) == "main",
              "a missing config defaults to main here too")

        # the routing settings: --flags win, the file second, sane defaults third
        ns = argparse.Namespace(role=None, config=missing, auto=None,
                                peer_timeout=None, hb_interval=None)
        st = mod.pick_settings(ns)
        check(st == {"role": "main", "auto": True,
                     "peer_timeout": 5.0, "hb_interval": 2.0},
              "defaults: main / auto on / 5 s timeout / 2 s heartbeat")
        ns = argparse.Namespace(role=None, config=conf, auto=None,
                                peer_timeout=None, hb_interval=None)
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("# comment\n\nrole = MAIN\nauto = no\n"
                     "peer_timeout = 9\nhb_interval = 3\n")
        st = mod.pick_settings(ns)
        check(st == {"role": "main", "auto": False,
                     "peer_timeout": 9.0, "hb_interval": 3.0},
              "comments, blanks and every key are read from the file")
        st = mod.pick_settings(argparse.Namespace(role="secondary", config=conf,
                                                 auto=True, peer_timeout=12.0,
                                                 hb_interval=None))
        check(st["auto"] is True and st["peer_timeout"] == 12.0
              and st["role"] == "secondary",
              "--no-auto/--auto and --peer-timeout override the file")
        with open(conf, "w", encoding="utf-8") as fh:      # junk must not crash
            fh.write("role = nowhere\nauto = maybe\npeer_timeout = abc\n")
        st = mod.pick_settings(ns)
        check(st == {"role": "main", "auto": False,
                     "peer_timeout": 5.0, "hb_interval": 2.0},
              "junk values fall back instead of crashing")

    print("--- status: replies, --save and --diff ---")

    class FakeTransport:
        """Answers every query the same way, unless `flip` says otherwise."""

        answers = {}
        flip = set()

        def __init__(self, dev, mode="cdb"):
            self.dev = dev
            self.last_status = 0
            self.last_sense = bytes(0x18)
            self.sent = []

        def send(self, report):
            self.sent.append(report)
            return True

        def xfer(self, cdb, data_len, data_in):
            key = bytes(cdb[:2]).hex(" ")
            data = bytes(self.answers.get(key, bytes(data_len)))
            if key in self.flip:
                data = bytes([0x01]) + data[1:]
            return True, data, self.last_sense

        def close(self):
            pass

    mod.ensure_admin = lambda *a, **k: None
    mod.oti_candidate_devices = lambda: ["FAKE"]
    mod.time.sleep = lambda _s: None
    mod.ScsiTransport = FakeTransport

    # INQUIRY answers with a normal 36-byte response, one query answers with 1s
    FakeTransport.answers = {"12 00": bytes(36), "d8 00": bytes([0, 0] + 6 * [0])}

    def probe(**kw):
        opts = dict(device=None, drive=None, save=None, diff=None)
        opts.update(kw)
        args = argparse.Namespace(**opts)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.cmd_status(args)
        return rc, buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        saved = os.path.join(tmp, "on.json")
        rc, out = probe(save=saved)
        check(rc == 0, "status exits 0 when everything answers")
        check("device : FAKE" in out, "the probed device is printed")
        check(os.path.exists(saved), "--save wrote the file")
        data = json.load(open(saved, encoding="utf-8"))
        check("control" in data and data["control"]["ok"] is True,
              "the control packet is recorded and accepted")
        check(data["control"]["data"] == "", "the control packet has no reply data")
        check("inquiry" in data, "INQUIRY is recorded (device identity)")
        for label, _op, _sub, _n in mod.STATUS_QUERIES:
            check(label in data, "query %s is recorded" % label)
        check(len(mod.STATUS_QUERIES) >= 8,
              "at least both 0xD8 and 0xF0 are probed")

        # same state again -> nothing changed
        _, out = probe(diff=saved)
        check("nothing changed" in out,
              "an identical second run reports 'nothing changed'")
        check("CHANGED" not in out, "and prints no CHANGED line")

        # the other machine disappears: one reply flips
        FakeTransport.flip = {"d8 00"}
        _, out = probe(diff=saved)
        check("CHANGED" in out, "a flipped reply is reported as CHANGED")
        check("D8.00/8" in out, "and the changed query is named")
        check("nothing changed" not in out,
              "'nothing changed' is not printed when something did")
        FakeTransport.flip = set()

        # an unreadable --diff file must not traceback
        rc, out = probe(diff=os.path.join(tmp, "missing.json"))
        check(rc == 1, "an unreadable --diff file exits 1")
        check("cannot read" in out, "and says so")

        # a run with no cable at all
        mod.oti_candidate_devices = lambda: []
        rc, out = probe()
        check(rc == 1 and "cable not found" in out,
              "no cable -> exits 1 with a clear message")
        mod.oti_candidate_devices = lambda: ["FAKE"]

    print()
    if failed:
        print("%d/%d checks failed" % (len(failed), checks))
        return 1
    print("all %d checks passed" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
