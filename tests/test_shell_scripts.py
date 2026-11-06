"""Sanity checks for the shell script pair (install / uninstall).

The Windows development machine has no bash and no WSL, so the .sh files cannot
be executed - let alone executed once on Ubuntu by the person who needs them.
What *can* be checked is what bites hardest:

  * CRLF line endings, which turn `#!/usr/bin/env bash` into a "bad interpreter"
    error and make every line end in a stray token;
  * a missing `fi` / `done` / `esac`, the classic shell typo;
  * the installer and the uninstaller disagreeing about the unit name, the
    install directory or the file names, so that "remove" silently misses;
  * the uninstaller touching the user's own copy of the scripts.

It also pins the same agreements on the PowerShell pair.  Run it because a real
shell is not available, not as a substitute for running `bash -n` on Linux:

    python tests/test_shell_scripts.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASSED = 0
FAILED = []


def check(cond, what):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(what)


def read(name):
    with open(os.path.join(ROOT, name), "rb") as f:
        return f.read()


def text_of(name):
    return read(name).decode("utf-8")


def strip_comments(src):
    """Drop comments, keeping quotes honest, so keywords can be counted.

    A `#` only starts a comment at the start of a word (bash rule), and never
    inside '...' or "..." - which the file uses a lot ("#1" in a message would
    otherwise hide half a line).
    """
    out = []
    for line in src.splitlines():
        if line.lstrip().startswith("#"):
            out.append("")
            continue
        res, i, n, quote = [], 0, len(line), None
        while i < n:
            c = line[i]
            if quote:
                if c == "\\" and quote == '"':
                    res.append(c)
                    i += 1
                    if i < n:
                        res.append(line[i])
                        i += 1
                    continue
                if c == quote:
                    quote = None
                res.append(c)
                i += 1
                continue
            if c in ("'", '"'):
                quote = c
                res.append(c)
                i += 1
                continue
            if c == "#" and (i == 0 or line[i - 1].isspace()):
                break
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def strip_strings(src):
    """Blank the *contents* of '...' and "..." as well.

    Bash keywords never live inside quotes - but words that merely look like
    them do, and the messages in these scripts are full of them ("for the other
    one's heartbeat...").  Keeping the quotes themselves keeps the balance sane.
    """
    out = []
    for line in src.splitlines():
        res, i, n, quote = [], 0, len(line), None
        while i < n:
            c = line[i]
            if quote:
                if c == "\\" and quote == '"':
                    i += 2
                    continue
                if c == quote:
                    quote = None
                    res.append(c)
                i += 1
                continue
            if c in ("'", '"'):
                quote = c
                res.append(c)
                i += 1
                continue
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


def word_count(src, word):
    return len(re.findall(r"(?<![\w-])%s(?![\w-])" % re.escape(word), src))


def get_literal(src, var):
    m = re.search(r'^%s="([^"]*)"' % re.escape(var), src, re.M)
    return m.group(1) if m else None


# ------------------------------------------------------------------ bash files
SH = ["install-autostart.sh", "uninstall-autostart.sh"]

for name in SH:
    path = os.path.join(ROOT, name)
    check(os.path.isfile(path), "%s exists" % name)
    if not os.path.isfile(path):
        continue

    raw = read(name)
    src = raw.decode("utf-8")
    body = strip_strings(strip_comments(src))

    n_if, n_fi = word_count(body, "if"), word_count(body, "fi")
    n_do, n_done = word_count(body, "do"), word_count(body, "done")
    n_case, n_esac = word_count(body, "case"), word_count(body, "esac")
    n_loops = sum(word_count(body, w) for w in ("for", "while", "until"))

    check(raw.startswith(b"#!/usr/bin/env bash"), "%s starts with the bash shebang" % name)
    check(b"\r" not in raw, "%s has no CR bytes (LF only, so Linux can run it)" % name)
    check("set -euo pipefail" in src, "%s sets -euo pipefail" % name)
    check(n_fi == n_if, "%s balances if (%d) with fi (%d)" % (name, n_if, n_fi))
    check(n_done == n_loops and n_do == n_done,
          "%s balances its %d loop(s) with do (%d) / done (%d)"
          % (name, n_loops, n_do, n_done))
    check(n_esac == n_case,
          "%s balances case (%d) with esac (%d)" % (name, n_case, n_esac))
    thens = word_count(body, "then")
    check(thens >= n_if + word_count(body, "elif"),
          "%s has a then for every if/elif (%d >= %d)"
          % (name, thens, n_if + word_count(body, "elif")))
    check(re.search(r"^usage\(\)", src, re.M) is not None, "%s has usage()" % name)
    check("-h|--help" in src, "%s handles -h/--help" % name)
    check("km_link_switch_linux.py" in src, "%s refers to the Linux script" % name)

# the two must agree on every path they share, or uninstall misses files
install_sh = text_of("install-autostart.sh")
uninstall_sh = text_of("uninstall-autostart.sh")
for var in ("UNIT_NAME", "UNIT", "DEST_DIR", "CONF"):
    a, b = get_literal(install_sh, var), get_literal(uninstall_sh, var)
    check(a is not None and a == b,
          "$%s agrees between installer (%r) and uninstaller (%r)" % (var, a, b))
dest_dir = get_literal(uninstall_sh, "DEST_DIR")
check(bool(dest_dir) and dest_dir.startswith("/"),
      "the uninstaller only ever rms an absolute DEST_DIR (%r)" % dest_dir)

check('--role' in install_sh and '--no-auto' in install_sh and '--remove' in install_sh,
      "the Linux installer documents --role, --no-auto and --remove")
check(all(flag in uninstall_sh for flag in ("--stop", "--status", "--purge", "--dry-run")),
      "the Linux uninstaller documents --stop, --status, --purge and --dry-run")
check('rm -rf "$HERE"' not in uninstall_sh and "rm -rf $HERE" not in uninstall_sh,
      "the Linux uninstaller never deletes the user's own folder")
check('rm -rf "$DEST_DIR"' in uninstall_sh,
      "the Linux uninstaller removes the installed copy")

# ------------------------------------------------------------------ PowerShell
install_ps = text_of("install-autostart.ps1")
uninstall_ps = text_of("uninstall-autostart.ps1")

for name, src in (("install-autostart.ps1", install_ps),
                  ("uninstall-autostart.ps1", uninstall_ps)):
    check(os.path.isfile(os.path.join(ROOT, name)), "%s exists" % name)
    check("$TaskName = " in src, "%s names the task in one variable" % name)
    check("kmlink.conf" in src, "%s refers to kmlink.conf" % name)
    check("kmlink.log" in src or "LogDir" in src, "%s refers to the log file" % name)


def ps_literal(src, var):
    m = re.search(r'^\$%s\s*=\s*"([^"]*)"' % re.escape(var), src, re.M)
    return m.group(1) if m else None


check(ps_literal(install_ps, "TaskName") == ps_literal(uninstall_ps, "TaskName"),
      "the two PowerShell scripts use the same task name")
check("Register-ScheduledTask" in install_ps and "Unregister-ScheduledTask" in uninstall_ps,
      "one registers the task and the other unregisters it")
check("Remove-Item $Conf" in uninstall_ps,
      "the PowerShell uninstaller deletes the config it created")
check("pythonw.exe" in install_ps and "$LogFile" in install_ps,
      "the Windows task is registered windowless and with a log file")
check("Test-Admin" in uninstall_ps and "RunAs" in uninstall_ps,
      "the PowerShell uninstaller elevates itself for the task removal")
check("Remove-Item $Dir" not in uninstall_ps and 'Remove-Item "$Dir"' not in uninstall_ps,
      "the PowerShell uninstaller never deletes the user's own folder")

# ------------------------------------------------------------------------ done
if FAILED:
    print("%d of %d shell-script checks FAILED:" % (len(FAILED), PASSED + len(FAILED)))
    for what in FAILED:
        print("  - %s" % what)
    sys.exit(1)

print("all %d shell-script checks passed" % PASSED)
