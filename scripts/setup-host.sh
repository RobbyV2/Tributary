#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"

USER_NAME="${SUDO_USER:-$(id -un)}"
GROUP="bluetooth"

POLKIT_RULE="/etc/polkit-1/rules.d/51-tributary-bluez.rules"
CLASS_DROPIN_DIR="/etc/bluetooth/main.conf.d"
CLASS_DROPIN="$CLASS_DROPIN_DIR/tributary-class.conf"
MAIN_CONF="/etc/bluetooth/main.conf"
AUDIO_CLASS="0x200414"

WP_SRC="$REPO/config/wireplumber/51-tributary-roles.conf"
WP_DST_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WP_DST="$WP_DST_DIR/51-tributary-roles.conf"

UNIT_SRC="$REPO/systemd/tributary.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DST_DIR/tributary.service"

VENV_PY="$REPO/.venv/bin/python"

usage() {
  cat <<EOF
Tributary host setup. Additive, reversible, idempotent. Only drop-in files; never edits global configs in place.

Usage: setup-host.sh [install|--uninstall] [options]

  install (default)   Apply every change below.
  --uninstall         Reverse every change.

Options:
  --yes               Skip confirmation for root-level changes (polkit, main.conf, bluetooth restart).
  --remove-group      Only with --uninstall: also remove $USER_NAME from the $GROUP group.
  -h, --help          This help.

Install steps:
  1. Add $USER_NAME to the '$GROUP' group (system-bus access to org.bluez).
  2. Install polkit rule $POLKIT_RULE granting non-interactive org.bluez.* to the '$GROUP' group.
  3. Install audio device-class drop-in ($AUDIO_CLASS) and restart bluetooth.service.
  4. Install WirePlumber drop-in into $WP_DST_DIR (enables both A2DP roles).
  5. Install + enable --now the user service tributary.service.

Uninstall reverses 5->1; group membership removed only with --remove-group.
EOF
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "$1 [y/N] " a
  [ "$a" = y ] || [ "$a" = Y ]
}

install_group() {
  if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$GROUP"; then
    echo "group: $USER_NAME already in '$GROUP', skipping"
  else
    echo "group: adding $USER_NAME to '$GROUP' (sudo gpasswd -a)"
    sudo gpasswd -a "$USER_NAME" "$GROUP"
    echo "group: re-login or 'newgrp $GROUP' for it to take effect"
  fi
}

install_polkit() {
  echo "polkit: installing $POLKIT_RULE (non-interactive org.bluez.* for '$GROUP' group)"
  confirm "Write $POLKIT_RULE ?" || { echo "polkit: skipped"; return 0; }
  sudo install -d -m 755 "$(dirname "$POLKIT_RULE")"
  sudo tee "$POLKIT_RULE" >/dev/null <<EOF
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.bluez.") === 0 && subject.isInGroup("$GROUP")) {
        return polkit.Result.YES;
    }
});
EOF
}

install_class() {
  echo "class: setting audio device class $AUDIO_CLASS"
  confirm "Write class drop-in and restart bluetooth.service ?" || { echo "class: skipped"; return 0; }
  if sudo test -d "$CLASS_DROPIN_DIR" || bluetoothd_supports_dropin; then
    sudo install -d -m 755 "$CLASS_DROPIN_DIR"
    echo "class: writing $CLASS_DROPIN"
    sudo tee "$CLASS_DROPIN" >/dev/null <<EOF
[General]
Class=$AUDIO_CLASS
EOF
  else
    echo "class: $CLASS_DROPIN_DIR unsupported by this bluez build"
    echo "class: add the following under [General] in $MAIN_CONF manually, then restart bluetooth.service:"
    echo "       Class=$AUDIO_CLASS"
    return 0
  fi
  echo "class: restarting bluetooth.service"
  sudo systemctl restart bluetooth.service
}

bluetoothd_supports_dropin() {
  bluetoothd --version >/dev/null 2>&1 || return 1
  local v
  v="$(bluetoothd --version 2>/dev/null | head -n1 | cut -d. -f2)"
  [ -n "$v" ] && [ "$v" -ge 41 ] 2>/dev/null
}

install_wireplumber() {
  echo "wireplumber: installing $WP_DST (both A2DP roles)"
  mkdir -p "$WP_DST_DIR"
  install -m 644 "$WP_SRC" "$WP_DST"
  systemctl --user restart wireplumber.service 2>/dev/null || echo "wireplumber: restart skipped (no user session); takes effect next login"
}

install_service() {
  echo "service: installing $UNIT_DST and enabling --now"
  mkdir -p "$UNIT_DST_DIR"
  local py="python3"
  [ -x "$VENV_PY" ] && py="$VENV_PY"
  sed "s#^ExecStart=python3 #ExecStart=$py #" "$UNIT_SRC" > "$UNIT_DST"
  echo "service: WorkingDirectory set to $REPO"
  if ! grep -q "^WorkingDirectory=" "$UNIT_DST"; then
    sed -i "/^\[Service\]/a WorkingDirectory=$REPO" "$UNIT_DST"
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now tributary.service
}

uninstall_service() {
  echo "service: disabling + removing $UNIT_DST"
  systemctl --user disable --now tributary.service 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl --user daemon-reload 2>/dev/null || true
}

uninstall_wireplumber() {
  echo "wireplumber: removing $WP_DST"
  rm -f "$WP_DST"
  systemctl --user restart wireplumber.service 2>/dev/null || true
}

uninstall_class() {
  echo "class: removing $CLASS_DROPIN and restarting bluetooth.service"
  confirm "Remove class drop-in and restart bluetooth.service ?" || { echo "class: skipped"; return 0; }
  sudo rm -f "$CLASS_DROPIN"
  sudo systemctl restart bluetooth.service 2>/dev/null || true
  echo "class: if Class= was added to $MAIN_CONF by hand, remove it there too"
}

uninstall_polkit() {
  echo "polkit: removing $POLKIT_RULE"
  confirm "Remove $POLKIT_RULE ?" || { echo "polkit: skipped"; return 0; }
  sudo rm -f "$POLKIT_RULE"
}

uninstall_group() {
  if [ "$REMOVE_GROUP" = 1 ]; then
    echo "group: removing $USER_NAME from '$GROUP'"
    sudo gpasswd -d "$USER_NAME" "$GROUP" || true
  else
    echo "group: keeping $USER_NAME in '$GROUP' (pass --remove-group to remove)"
  fi
}

ACTION=install
ASSUME_YES=0
REMOVE_GROUP=0
for arg in "$@"; do
  case "$arg" in
    install) ACTION=install ;;
    --uninstall) ACTION=uninstall ;;
    --yes) ASSUME_YES=1 ;;
    --remove-group) REMOVE_GROUP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

case "$ACTION" in
  install)
    install_group
    install_polkit
    install_class
    install_wireplumber
    install_service
    echo "install: done"
    ;;
  uninstall)
    uninstall_service
    uninstall_wireplumber
    uninstall_class
    uninstall_polkit
    uninstall_group
    echo "uninstall: done"
    ;;
esac
