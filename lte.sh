#!/usr/bin/env bash
# lte.sh — build, flash, and monitor hub-lte (nRF9151 on Thingy:91X)
#
# DK switch SW2 must be set to the nRF91 position for jtag/rtt subcommands.
#
# Usage:
#   ./tracker-utils/lte.sh                  build (pristine) + flash
#   ./tracker-utils/lte.sh pristine         build pristine only
#   ./tracker-utils/lte.sh ordinary         incremental build only
#   ./tracker-utils/lte.sh flash            flash last build
#   ./tracker-utils/lte.sh usb              USB LTE mirror (if02) via monitor.py
#   ./tracker-utils/lte.sh rtt              RTT log viewer  (J-Link → nRF9151, DK SW2=nRF91)
#   ./tracker-utils/lte.sh jtag             interactive J-Link session for nRF9151
#   ./tracker-utils/lte.sh monitor          three-pane GUI log viewer + CSV recorder

set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNR="${SNR_HUB:-1051217937}"   # nRF9151 DK → Thingy:91X Debug In (SW2: nRF91)
DEVICE="NRF9151_XXAA"
APP="${WORKSPACE}/tracker-hub/apps/tracker-hub-lte"
BUILD_DIR="${WORKSPACE}/build/lte"

build() {
    nrfutil sdk-manager toolchain launch --ncs-version v3.1.1 -- \
        west build ${1:+--pristine} \
            -b thingy91x/nrf9151/ns \
            --build-dir "${BUILD_DIR}" \
            "${APP}" \
            --sysbuild -- \
            "-DEXTRA_CONF_FILE=${APP}/local.conf"
}

flash() {
    # TF-M writes UICR into the merged hex; --recover forces ERASE_ALL which is
    # required when UICR data is already present from a previous flash.
    nrfutil sdk-manager toolchain launch --ncs-version v3.1.1 -- \
        west flash --recover --build-dir "${BUILD_DIR}" --snr "${SNR}"
}

usb() {
    python3 "${WORKSPACE}/tracker-utils/monitor.py" --lte-only
}

rtt() {
    # Requires: DK SW2 at nRF91 position; hub-lte built with CONFIG_LOG_BACKEND_RTT=y
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
