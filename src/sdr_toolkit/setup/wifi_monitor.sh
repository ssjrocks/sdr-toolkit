#!/usr/bin/env bash
# The Wi-Fi page's root helper (run with pkexec): a monitor-mode interface of its own on the card, hopping
# through the channels, for tshark to listen on. Your normal Wi-Fi interface is left as it is; the monitor
# interface is removed when this stops.
#
# Usage: wifi_monitor.sh PHY "CHANNELS" DWELL_S
#   Prints "IFACE <name>" once it's up, then "CHANNEL <n>" at every hop. It runs as root, so the page (your user)
#   can't signal it: it stops by itself when nothing reads its output any more (the next report fails, SIGPIPE).
set -u
phy="$1"; channels="$2"; dwell="$3"
mon="sdrtkmon0"
cleanup() {
  iw dev "$mon" del 2>/dev/null
  exit 0
}
trap cleanup TERM INT HUP PIPE EXIT
if rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
  echo "ERROR Wi-Fi is switched off (rfkill): turn it on (Settings, or rfkill unblock wifi) to use this page"
  exit 1
fi
iw dev "$mon" del 2>/dev/null
if ! err=$(iw phy "$phy" interface add "$mon" type monitor 2>&1); then
  echo "ERROR couldn't add a monitor interface on $phy: $err"
  exit 1
fi
command -v nmcli >/dev/null && nmcli dev set "$mon" managed no 2>/dev/null
ip link set "$mon" up || { echo "ERROR couldn't bring $mon up"; exit 1; }
echo "IFACE $mon"
while true; do
  for ch in $channels; do
    if iw dev "$mon" set channel "$ch" 2>/dev/null; then
      echo "CHANNEL $ch" || cleanup
      sleep "$dwell"
    fi
  done
done
