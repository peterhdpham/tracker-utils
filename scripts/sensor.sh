#!/usr/bin/env bash
# sensor.sh — build, flash, and monitor tracker-sensor-node (Thingy:53 nRF5340)
#
# Usage:
#   ./tracker-utils/sensor.sh               build (pristine) + flash
#   ./tracker-utils/sensor.sh pristine      build pristine only
#   ./tracker-utils/sensor.sh ordinary      incremental build only
#   ./tracker-utils/sensor.sh flash         flash last build
#   ./tracker-utils/sensor.sh recover       recover (ERASEALL) + flash
#   ./tracker-utils/sensor.sh rtt           RTT log viewer  (J-Link → Thingy:53)
#   ./tracker-utils/sensor.sh jtag          interactive J-Link session for nRF5340
#   ./tracker-utils/sensor.sh monitor       three-pane GUI log viewer + CSV recorder

set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNR="${SNR_SENSOR:-1050065248}"   # nRF52 DK → Thingy:53 SWD pads
DEVICE="NRF5340_XXAA_APP"

build() {
    python3 "${WORKSPACE}/tracker-utils/tools/build.py" build sensor ${1:+--pristine}
}

flash() {
    python3 "${WORKSPACE}/tracker-utils/tools/build.py" flash sensor
}

recover() {
    # System nrfutil (not toolchain launcher) — more reliable ERASEPROTECT clear.
    nrfutil device recover --serial-number "${SNR}" --core Network
    nrfutil device recover --serial-number "${SNR}" --core Application
    flash
}

rtt() {
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
    recover)   recover ;;
    rtt)       rtt ;;
    screen)    rtt ;;           # legacy alias
    jlink)     jtag ;;          # legacy alias
    jtag)      jtag ;;
    monitor)   monitor ;;
    *)
        echo "Usage: $0 [pristine|ordinary|flash|recover|rtt|jtag|monitor]"
        exit 1
        ;;
    esac
