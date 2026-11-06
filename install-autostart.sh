#!/usr/bin/env bash
#
# install-autostart.sh - keep the KM Link Alt+S switcher running on Linux.
#
#   sudo ./install-autostart.sh                    install + start it, and ask
#                                                  which machine is the main one
#   sudo ./install-autostart.sh --role secondary   answer that question here
#   sudo ./install-autostart.sh --no-auto          do not check the other PC
#   sudo ./install-autostart.sh --no-start         register at boot, don't start
#   sudo ./install-autostart.sh --status           registered / running / role
#   sudo ./install-autostart.sh --remove           stop + uninstall
#                                                  (or ./uninstall-autostart.sh)
#
# It installs a systemd system service named "kmlink" that runs
# km_link_switch_linux.py as root (root is needed: SG_IO wants write access to
# /dev/sgN, EVIOCGRAB wants read access to /dev/input/event*).  The service
# waits for the cable, so booting without it plugged in is fine.
#
# Install it on BOTH machines.  With the default `auto = yes` they tell each
# other that they are alive (one tiny heartbeat packet every 2 s), so the script
# only switches to a machine that is actually on; if the other machine goes away
# the keyboard and mouse come back here.  Use --no-auto if the other machine
# will not run this script.
#
# The answers are written to /opt/kmlink/kmlink.conf:
#
#   role = main        this machine owns the keyboard/mouse at start-up
#   role = secondary   the other machine is the one you normally drive, so this
#                      one forwards the keyboard and mouse to it at start-up

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/km_link_switch_linux.py"
DEST_DIR="/opt/kmlink"
DEST="$DEST_DIR/km_link_switch_linux.py"
CONF="$DEST_DIR/kmlink.conf"
UNIT_NAME="kmlink.service"
UNIT="/etc/systemd/system/$UNIT_NAME"

conf_get() {
  # One setting out of kmlink.conf; empty when the file or the key is missing.
  # (awk, not sed|head: a missing file must not trip `set -e`/`pipefail`.)
  [ -f "$CONF" ] || return 0
  awk -v k="$1" '$0 ~ "^[[:space:]]*" k "[[:space:]]*=" { sub(/^[^=]*=[[:space:]]*/, ""); out = $0; sub(/[[:space:]]+$/, "", out) } END { print out }' "$CONF"
}

MODE=install
ROLE=""
NO_START=0
NO_AUTO=0

usage() {
  awk 'NR >= 3 && /^#/ { sub(/^# ?/, ""); print; next } NR >= 3 { exit }' "$0"
  exit "${1:-0}"
}

ARGS=("$@")
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status)   MODE=status;  shift ;;
    --remove)   MODE=remove;  shift ;;
    --no-start) NO_START=1;   shift ;;
    --no-auto)  NO_AUTO=1;    shift ;;
    --role)     ROLE="${2:-}"; shift $(( $# > 1 ? 2 : 1 )) ;;
    --role=*)   ROLE="${1#*=}"; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "[!] unknown option: $1" >&2; usage 1 ;;
  esac
done

PY="$(command -v python3 || true)"

# ------------------------------------------------------------- status (no root)
if [ "$MODE" = status ]; then
  echo "python3     : ${PY:-(not found)}"
  echo "unit file   : $UNIT $([ -f "$UNIT" ] && echo '(present)' || echo '(missing)')"
  echo "script      : $DEST $([ -f "$DEST" ] && echo '(present)' || echo '(missing)')"
  role="$(conf_get role)"
  auto="$(conf_get auto)"
  echo "role        : ${role:-main}  ($CONF)"
  if [ "${role:-main}" = secondary ]; then
    echo "              this machine forwards the keyboard/mouse to the other one"
  else
    echo "              this machine owns the keyboard/mouse at start-up"
  fi
  if [ "${auto:-yes}" = no ]; then
    echo "other PC    : auto = no  (never checked)"
  else
    echo "other PC    : auto = yes  (switching needs its heartbeat)"
  fi
  if [ -f "$UNIT" ]; then
    echo
    echo "enabled : $(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)"
    echo "active  : $(systemctl is-active  "$UNIT_NAME" 2>/dev/null || true)"
    echo
    systemctl --no-pager --full status "$UNIT_NAME" 2>/dev/null || true
  else
    echo
    echo "Not registered."
  fi
  exit 0
fi

# ------------------------------------------------- the one question (no root)
# Which machine should own the keyboard/mouse when *both* are on?  (When only
# one is on, the script routes to that one by itself.)
if [ "$MODE" = install ] && [ -z "$ROLE" ]; then
  current="$(conf_get role)"
  [ -n "$current" ] || current="main"
  echo
  echo "Is THIS computer the main system?"
  echo "  main       - the keyboard + mouse are plugged in here, and they stay here"
  echo "  secondary  - you normally drive the OTHER machine from here, so this one"
  echo "               forwards the keyboard and mouse to it at start-up"
  printf 'main or secondary? (default %s): ' "$current"
  answer=""
  read -r answer || true
  case "${answer,,}" in
    s|secondary|2) ROLE=secondary ;;
    ""|m|main|1)   ROLE="$current" ;;
    *) echo "[i] '$answer' is neither main nor secondary - keeping '$current'."
       ROLE="$current" ;;
  esac
fi

if [ "$MODE" = install ]; then
  case "$ROLE" in
    main|secondary) ;;
    *) echo "[!] --role must be 'main' or 'secondary' (got '$ROLE')" >&2; exit 1 ;;
  esac
fi

# -------------------------------------------------------------------- root
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[!] root is required and sudo is not installed:" >&2
    echo "    sudo python3 $SRC run" >&2
    exit 1
  fi
  echo "[i] root is required; re-running with sudo..."
  # Re-pass the answer: it was not on the command line, we asked for it.
  if [ -n "$ROLE" ]; then
    exec sudo -- "$0" "${ARGS[@]}" --role "$ROLE"
  fi
  exec sudo -- "$0" "${ARGS[@]}"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[!] systemd not found.  Run the script by hand instead:"
  echo "      sudo python3 $SRC run --role $ROLE"
  exit 1
fi

if [ -z "$PY" ]; then
  echo "[!] python3 not found.  Install it with:  sudo apt install python3"
  exit 1
fi
PY="$(readlink -f "$PY")"

# ---------------------------------------------------------------- remove
if [ "$MODE" = remove ]; then
  systemctl stop "$UNIT_NAME" 2>/dev/null || true
  systemctl disable "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
  rm -rf "$DEST_DIR"
  echo "[i] removed: $UNIT_NAME is gone and $DEST_DIR is deleted."
  echo "    (your copy in $HERE is untouched)"
  exit 0
fi

# ---------------------------------------------------------------- install
if [ ! -f "$SRC" ]; then
  echo "[!] $SRC not found - run this from the folder that holds the script."
  exit 1
fi

# Do not silently run a file with a syntax error at every boot.
if ! "$PY" -m py_compile "$SRC"; then
  echo "[!] $SRC has a Python syntax error - not installing."
  exit 1
fi

echo "[i] installing $SRC -> $DEST"
install -d -m 755 "$DEST_DIR"
install -m 644 "$SRC" "$DEST"

cat > "$CONF" <<EOF
# Written by install-autostart.sh - plain text, edit freely.
#
# main      = this machine owns the keyboard/mouse at start-up
# secondary = the other machine is the one you normally drive
role = $ROLE
#
# auto = yes: the two machines send each other one tiny heartbeat packet every
# hb_interval seconds, so the script knows whether the other machine is on.
# Alt+S is then only followed while it answers, and if it disappears in mid-
# session the keyboard/mouse come back to this machine instead of pointing at
# one that is off.  This needs the script running on the OTHER machine too:
# set auto = no if it is not (then nothing is ever checked).
auto = $([ "$NO_AUTO" -eq 1 ] && echo no || echo yes)
peer_timeout = 5
hb_interval = 2
EOF
chmod 644 "$CONF"

cat > "$UNIT" <<EOF
[Unit]
Description=KM Link Alt+S switcher (OTi USB cable switcher)
Documentation="file://$HERE/README.md"
After=multi-user.target

[Service]
Type=simple
ExecStart=$PY $DEST run
WorkingDirectory=$DEST_DIR
# `role` in kmlink.conf decides which machine owns the keyboard/mouse at boot,
# and `auto` decides whether the other machine's heartbeat gates Alt+S.
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$UNIT_NAME"

if [ "$NO_START" -eq 1 ]; then
  echo "[i] enabled at boot (not started, because of --no-start)."
  echo "    role = $ROLE, auto = $([ "$NO_AUTO" -eq 1 ] && echo no || echo yes)"
  echo "    start it now with:  sudo systemctl start $UNIT_NAME"
  exit 0
fi

systemctl restart "$UNIT_NAME" || true
sleep 1
echo
echo "[i] role  : $ROLE  ($CONF)"
if [ "$NO_AUTO" -eq 1 ]; then
  echo "[i] other PC: auto = no (never checked)"
else
  echo "[i] other PC: auto = yes - checked by heartbeat; install this script"
  echo "    on the other machine too, or set auto = no in $CONF."
fi
echo "[i] state : $(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
echo "    log   : journalctl -u $UNIT_NAME -f      (Ctrl+C to leave)"
echo "    stop  : sudo systemctl stop $UNIT_NAME"
echo "    undo  : sudo $0 --remove"
echo
echo "Alt+S moves the keyboard + mouse between this machine and the other PC."
echo "Install this on the other machine too: with auto = yes each machine waits"
echo "for the other one's heartbeat before it forwards, and the OTi cable has to"
echo "be plugged into both of them."
