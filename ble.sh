#!/usr/bin/env bash
# ble.sh — build, flash, and monitor hub-ble (nRF5340 on Thingy:91X)
#
# DK switch SW2 must be set to the nRF53 position for jtag/rtt subcommands.
#
# Usage:
#   ./tracker-utils/ble.sh                  build (pristine) + flash
#   ./tracker-utils/ble.sh pristine         build pristine only
#   ./tracker-utils/ble.sh ordinary         incremental build only
#   ./tracker-utils/ble.sh flash            flash last build
#   ./tracker-utils/ble.sh usb              USB CDC-ACM console (if00) via monitor.py
#   ./tracker-utils/ble.sh rtt              RTT log viewer  (J-Link → nRF5340, DK SW2=nRF53)
#   ./tracker-utils/ble.sh jtag             interactive J-Link session for nRF5340
#   ./tracker-utils/ble.sh monitor          three-pane GUI log viewer + CSV recorder

set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNR="${SNR_HUB:-1051217937}"   # nRF9151 DK → Thingy:91X Debug In (SW2: nRF53)
DEVICE="NRF5340_XXAA_APP"

build() {
    python3 "${WORKSPACE}/tracker-utils/build.py" build ble ${1:+--pristine}
}

flash() {
    python3 "${WORKSPACE}/tracker-utils/build.py" flash ble
}

usb() {
    python3 "${WORKSPACE}/tracker-utils/monitor.py" --ble-only
}

rtt() {
    # Requires: DK SW2 at nRF53 position; hub-ble built with CONFIG_LOG_BACKEND_RTT=y
    JLinkRTTViewer \
        -device "${DEVICE}" \
        -if SWD \
        -speed 4000 \
        -RTTChannel 0 \
        -SelectEmuBySN "${SNR}"
}

jtag() {
    JLinkExe \
        -device "${DEVICE}" \
        -if SWD \
        -speed 4000 \
        -autoconnect 1 \
        -SelectEmuBySN "${SNR}"
}

monitor() {
    python3 "${WORKSPACE}/tracker-utils/logviewer.py"
}

case "${1:-}" in
    "")        build pristine; flash ;;
    pristine)  build pristine ;;
    ordinary)  build "" ;;
    flash)     flash ;;
    usb)       usb ;;
    screen)    usb ;;           # legacy alias
    rtt)       rtt ;;
    jtag)      jtag ;;
    monitor)   monitor ;;
    *)
        echo "Usage: $0 [pristine|ordinary|flash|usb|rtt|jtag|monitor]"
        exit 1
        ;;
esac
