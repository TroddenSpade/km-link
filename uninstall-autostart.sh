#!/usr/bin/env bash
#
# uninstall-autostart.sh - stop and remove the KM Link Alt+S switcher.
#
#   sudo ./uninstall-autostart.sh            stop it + remove the service
#   sudo ./uninstall-autostart.sh --stop     just stop it (back at the next boot)
#   sudo ./uninstall-autostart.sh --purge    ...and delete the log file too
#   ./uninstall-autostart.sh --dry-run       say what it would do, change nothing
#   ./uninstall-autostart.sh --status        what is there right now?
#
# It removes exactly what install-autostart.sh created:
#
#   * the systemd unit kmlink.service (stopped, disabled, deleted)
#   * /opt/kmlink/ (the installed script copy and kmlink.conf)
#   * any copy of the switcher started by hand
#
# and, only with --purge, /var/log/kmlink.log.  The files in this folder are
# never touched, and neither is anything else: no packages, no kernel modules,
# no udev rules - the cable keeps working as plain USB hardware.
#
# Stopping is always safe: the keyboard and mouse come back to this machine the
# moment the process exits, because the kernel releases the EVIOCGRAB when the
# file descriptors close.
#
# Run it on every machine you installed on - the laptop's Ubuntu side.  On the
# Windows machines use uninstall-autostart.ps1.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="kmlink.service"
UNIT="/etc/systemd/system/$UNIT_NAME"
DEST_DIR="/opt/kmlink"
CONF="$DEST_DIR/kmlink.conf"
LOG_FILE="/var/log/kmlink.log"

MODE=remove
PURGE=0
DRY=0

have() { command -v "$1" >/dev/null 2>&1; }

unit_active() {
  if have systemctl; then
    out="$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
    echo "${out:-unknown}"
  else
    echo "(no systemd)"
  fi
}

unit_enabled() {
  if have systemctl; then
    out="$(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)"
    echo "${out:-unknown}"
  else
    echo "(no systemd)"
  fi
}

hand_started() {
  # A copy started by hand, outside systemd (development runs).
  pgrep -f "km_link_switch_linux.py run" >/dev/null 2>&1
}

usage() {
  awk 'NR >= 3 && /^#/ { sub(/^# ?/, ""); print; next } NR >= 3 { exit }' "$0"
  exit "${1:-0}"
}

ARGS=("$@")
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stop)    MODE=stop;   shift ;;
    --status)  MODE=status; shift ;;
    --purge)   PURGE=1;     shift ;;
    --dry-run) DRY=1;       shift ;;
    -h|--help) usage 0 ;;
    *) echo "[!] unknown option: $1" >&2; usage 1 ;;
  esac
done

# ------------------------------------------------------------ status (no root)
if [ "$MODE" = status ]; then
  echo "unit file  : $UNIT $([ -f "$UNIT" ] && echo '(present)' || echo '(missing)')"
  echo "enabled    : $(unit_enabled)"
  echo "active     : $(unit_active)"
  echo "script copy: $DEST_DIR $([ -d "$DEST_DIR" ] && echo '(present)' || echo '(gone)')"
  echo "config     : $CONF $([ -f "$CONF" ] && echo '(present)' || echo '(gone)')"
  echo "log file   : $LOG_FILE $([ -f "$LOG_FILE" ] && echo '(present)' || echo '(not used)')"
  if hand_started; then
    echo "process    : a copy started by hand is running"
    pgrep -af "km_link_switch_linux.py run" || true
  else
    echo "process    : none started by hand"
  fi
  exit 0
fi

# ----------------------------------------------------------- dry-run (no root)
if [ "$DRY" -eq 1 ]; then
  echo "This is a preview; nothing will be changed."
  echo
  if [ -f "$UNIT" ]; then
    echo "would stop    : $UNIT_NAME (now: $(unit_active))"
    if [ "$MODE" = stop ]; then
      echo "would keep    : the unit file (it comes back at the next boot)"
    else
      echo "would remove  : $UNIT (it will not start at boot again)"
    fi
  else
    echo "would stop    : nothing - $UNIT is not installed"
  fi
  if [ "$MODE" != stop ]; then
    [ -d "$DEST_DIR" ] && echo "would remove  : $DEST_DIR (the script copy + kmlink.conf)"
    if [ "$PURGE" -eq 1 ]; then
      [ -f "$LOG_FILE" ] && echo "would remove  : $LOG_FILE"
    else
      [ -f "$LOG_FILE" ] && echo "would keep    : $LOG_FILE (add --purge to delete it)"
    fi
    if hand_started; then
      echo "would kill    : the copy started by hand"
    fi
  fi
  echo "would keep    : the files in $HERE"
  exit 0
fi

# -------------------------------------------------------------------- root
if [ "$(id -u)" -ne 0 ]; then
  if ! have sudo; then
    echo "[!] root is required: run this with sudo." >&2
    exit 1
  fi
  echo "[i] root is required; re-running with sudo..."
  exec sudo -- "$0" "${ARGS[@]}"
fi

if ! have systemctl; then
  echo "[!] systemd not found.  Stop it however you started it, then delete"
  echo "    $DEST_DIR by hand."
  exit 1
fi

# ------------------------------------------------------------------------ stop
if [ "$MODE" = stop ]; then
  if [ -f "$UNIT" ]; then
    systemctl stop "$UNIT_NAME" 2>/dev/null || true
    echo "[i] stopped $UNIT_NAME.  It starts again at the next boot."
    echo "    (systemctl disable $UNIT_NAME stops that; $0 removes it.)"
  else
    echo "[i] $UNIT_NAME is not installed, so there is nothing to stop."
  fi
  if hand_started; then
    echo "[!] a copy started by hand is still running:"
    pgrep -af "km_link_switch_linux.py run" || true
    echo "    stop it with:  sudo pkill -f km_link_switch_linux.py"
  else
    echo "[i] no switcher process is running: the keyboard and mouse are yours again."
  fi
  exit 0
fi

# ---------------------------------------------------------------------- remove
DONE=()

if [ -f "$UNIT" ]; then
  systemctl stop "$UNIT_NAME" 2>/dev/null || true
  systemctl disable "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
  DONE+=("the unit $UNIT (stopped and disabled, so no boot start)")
else
  echo "[i] $UNIT was not installed."
fi

if hand_started; then
  if pkill -f "km_link_switch_linux.py run" 2>/dev/null; then
    sleep 0.3
    DONE+=("the copy started by hand")
  fi
fi

if [ -d "$DEST_DIR" ]; then
  rm -rf "$DEST_DIR"
  DONE+=("$DEST_DIR (the script copy and kmlink.conf)")
fi

if [ "$PURGE" -eq 1 ]; then
  if [ -f "$LOG_FILE" ]; then
    rm -f "$LOG_FILE"
    DONE+=("$LOG_FILE")
  fi
fi

echo
if [ "${#DONE[@]}" -gt 0 ]; then
  for item in "${DONE[@]}"; do
    echo "[i] removed: $item"
  done
else
  echo "[i] nothing was installed, so nothing was removed."
fi
if [ "$PURGE" -eq 0 ] && [ -f "$LOG_FILE" ]; then
  echo "[i] kept     : $LOG_FILE (run with --purge to delete it)"
fi
echo "[i] kept     : the files in $HERE"
echo "[i] kept     : the journal history (journalctl -u $UNIT_NAME)"
echo
echo "[i] The keyboard and mouse are yours again, now and after a reboot."
echo "    To put it back:  sudo $HERE/install-autostart.sh"
