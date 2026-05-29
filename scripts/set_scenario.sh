#!/usr/bin/env bash
# set_scenario.sh — Write consistent scenario config to all local.conf files.
#
# Usage:
#   ./scripts/set_scenario.sh --ble <B> [--coap <S>] [--server] [--restart]
#   ./scripts/set_scenario.sh --coap <S> [--server] [--restart]
#
# BLE scenarios (sensor-node ↔ hub ble-central must match):
#   B0  Broadcast plain         APP_BLE_SECURITY_BROADCAST / APP_SENSOR_MODE_BROADCAST
#   B1  GATT plain              APP_BLE_SECURITY_GATT       / APP_SENSOR_MODE_GATT
#   B3  GATT + OSCORE E2E       APP_BLE_SECURITY_GATT_OSCORE / APP_SENSOR_MODE_GATT_OSCORE
#
# CoAP scenarios (hub-lte → server):
#   S1  Plain CoAP, port 5683   APP_COAP_SECURITY_NONE
#   S2  CoAP + OSCORE, port 5683  APP_COAP_SECURITY_OSCORE
#   S3  CoAP + DTLS, port 5684  APP_COAP_SECURITY_DTLS
#   S4  DTLS + OSCORE, port 5684  APP_COAP_SECURITY_DTLS_OSCORE   ← thesis target
#
# Options:
#   --server   Also write scenario config to tracker-server/.env.testing (committed to git).
#              Push the result to deploy via CI (self-hosted runner on docker-server).
#   --restart  --server + docker compose up -d locally (skip if deploying via CI)
#
# After running, rebuild affected targets:
#   BLE change  → ./scripts/sensor.sh pristine && ./scripts/lte.sh
#   CoAP change → ./scripts/lte.sh
#
# Secrets (OSCORE keys, DTLS PSK) are read from the existing local.conf so they
# survive scenario switches. Set them once with generate_oscore_psk.py /
# generate_dtls_psk.py; this script never overwrites them.

set -euo pipefail

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

HUB_LTE="${WORKSPACE}/tracker-hub/apps/tracker-hub-lte/local.conf"
SENSOR="${WORKSPACE}/tracker-sensor-node/tracker-node/local.conf"
SERVER_TESTING_ENV="${WORKSPACE}/tracker-server/.env.testing"
DOCKER_OSCORE_PATH="/app/oscore-context"

BLE_SCENARIO=""
COAP_SCENARIO=""
UPDATE_SERVER=false
RESTART_SERVER=false

usage() {
    sed -n '/^# /s/^# //p' "$0" | head -30
    exit 1
}

[[ $# -ge 1 ]] || usage

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ble)     BLE_SCENARIO="${2:-}";  shift 2 ;;
        --coap)    COAP_SCENARIO="${2:-}"; shift 2 ;;
        --server)  UPDATE_SERVER=true;  shift ;;
        --restart) UPDATE_SERVER=true; RESTART_SERVER=true; shift ;;
        -h|--help) usage ;;
        *) echo "error: unknown option '$1'"; exit 1 ;;
    esac
done

[[ -n "$BLE_SCENARIO" || -n "$COAP_SCENARIO" ]] || usage

# Read a single value from a kconfig-style file, returns empty string if absent.
get_cfg() {
    local file="$1" key="$2"
    grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- || true
}

# ── CoAP scenario (hub-lte local.conf) ───────────────────────────────────────
if [[ -n "$COAP_SCENARIO" ]]; then
    # Preserve secrets and BLE mode line from existing file
    HOST="$(get_cfg "$HUB_LTE" CONFIG_APP_COAP_SERVER_HOST)"
    OSCORE_SECRET="$(get_cfg "$HUB_LTE" CONFIG_APP_OSCORE_MASTER_SECRET)"
    OSCORE_SALT="$(get_cfg "$HUB_LTE" CONFIG_APP_OSCORE_MASTER_SALT)"
    OSCORE_SENDER="$(get_cfg "$HUB_LTE" CONFIG_APP_OSCORE_SENDER_ID)"
    OSCORE_RECIPIENT="$(get_cfg "$HUB_LTE" CONFIG_APP_OSCORE_RECIPIENT_ID)"
    DTLS_PSK="$(get_cfg "$HUB_LTE" CONFIG_APP_DTLS_PSK_HEX)"
    DTLS_ID="$(get_cfg "$HUB_LTE" CONFIG_APP_DTLS_PSK_IDENTITY)"
    BLE_MODE_LINE="$(grep '^CONFIG_APP_BLE_SENSOR_MODE_' "$HUB_LTE" 2>/dev/null | head -1 || true)"

    HOST="${HOST:-\"klosteret.duckdns.org\"}"
    OSCORE_SECRET="${OSCORE_SECRET:-\"<not set — run generate_oscore_psk.py --name hub>\"}"
    OSCORE_SALT="${OSCORE_SALT:-\"<not set>\"}"
    OSCORE_SENDER="${OSCORE_SENDER:-\"01\"}"
    OSCORE_RECIPIENT="${OSCORE_RECIPIENT:-\"00\"}"
    DTLS_PSK="${DTLS_PSK:-\"<not set — run generate_dtls_psk.py>\"}"
    DTLS_ID="${DTLS_ID:-\"tracker-hub\"}"

    case "$COAP_SCENARIO" in
        S1)
            cat > "$HUB_LTE" <<EOF
# Scenario ${COAP_SCENARIO}: plain CoAP, port 5683 — no transport or app-layer security
CONFIG_APP_COAP_SERVER_HOST=${HOST}
CONFIG_APP_COAP_SECURITY_NONE=y
EOF
            SERVER_MODE="none"
            ;;
        S2)
            cat > "$HUB_LTE" <<EOF
# Scenario ${COAP_SCENARIO}: CoAP + OSCORE, port 5683 — hub applies OSCORE over plain UDP
CONFIG_APP_COAP_SERVER_HOST=${HOST}
CONFIG_APP_COAP_SECURITY_OSCORE=y
CONFIG_APP_OSCORE_MASTER_SECRET=${OSCORE_SECRET}
CONFIG_APP_OSCORE_MASTER_SALT=${OSCORE_SALT}
CONFIG_APP_OSCORE_SENDER_ID=${OSCORE_SENDER}
CONFIG_APP_OSCORE_RECIPIENT_ID=${OSCORE_RECIPIENT}
EOF
            SERVER_MODE="oscore"
            ;;
        S3)
            cat > "$HUB_LTE" <<EOF
# Scenario ${COAP_SCENARIO}: CoAP + DTLS, port 5684 — DTLS transport only, plain CoAP inside
CONFIG_APP_COAP_SERVER_HOST=${HOST}
CONFIG_APP_COAP_SECURITY_DTLS=y
CONFIG_APP_DTLS_PSK_IDENTITY=${DTLS_ID}
CONFIG_APP_DTLS_PSK_HEX=${DTLS_PSK}
EOF
            SERVER_MODE="dtls"
            ;;
        S4)
            cat > "$HUB_LTE" <<EOF
# Scenario ${COAP_SCENARIO}: DTLS + OSCORE, port 5684 — maximum security (thesis full stack)
CONFIG_APP_COAP_SERVER_HOST=${HOST}
CONFIG_APP_COAP_SECURITY_DTLS_OSCORE=y
CONFIG_APP_OSCORE_MASTER_SECRET=${OSCORE_SECRET}
CONFIG_APP_OSCORE_MASTER_SALT=${OSCORE_SALT}
CONFIG_APP_OSCORE_SENDER_ID=${OSCORE_SENDER}
CONFIG_APP_OSCORE_RECIPIENT_ID=${OSCORE_RECIPIENT}
CONFIG_APP_DTLS_PSK_IDENTITY=${DTLS_ID}
CONFIG_APP_DTLS_PSK_HEX=${DTLS_PSK}
EOF
            SERVER_MODE="dtls_oscore"
            ;;
        *)
            echo "error: unknown CoAP scenario '${COAP_SCENARIO}' — expected S1 S2 S3 S4"
            exit 1
            ;;
    esac

    # Re-append BLE mode line so it survives a CoAP-only update
    if [[ -n "$BLE_MODE_LINE" ]]; then
        printf '\n# BLE sensor mode — sent to nRF5340 hub-ble over UART at boot\n%s\n' \
               "$BLE_MODE_LINE" >> "$HUB_LTE"
    fi

    echo "CoAP ${COAP_SCENARIO} → ${HUB_LTE#"${WORKSPACE}/"}"
fi

# ── BLE scenario (hub-lte local.conf for mode + sensor local.conf for keys) ──
# The hub-ble image receives the mode over UART at boot — no hub-ble reflash needed.
if [[ -n "$BLE_SCENARIO" ]]; then
    # Preserve sensor OSCORE keys
    S_SECRET="$(get_cfg "$SENSOR" CONFIG_APP_OSCORE_MASTER_SECRET)"
    S_SALT="$(get_cfg "$SENSOR" CONFIG_APP_OSCORE_MASTER_SALT)"
    S_SENDER="$(get_cfg "$SENSOR" CONFIG_APP_OSCORE_SENDER_ID)"
    S_RECIPIENT="$(get_cfg "$SENSOR" CONFIG_APP_OSCORE_RECIPIENT_ID)"

    S_SECRET="${S_SECRET:-\"<not set — run generate_oscore_psk.py --name sensor>\"}"
    S_SALT="${S_SALT:-\"<not set>\"}"
    S_SENDER="${S_SENDER:-\"02\"}"
    S_RECIPIENT="${S_RECIPIENT:-\"00\"}"

    case "$BLE_SCENARIO" in
        B0) BLE_MODE_KV="CONFIG_APP_BLE_SENSOR_MODE_BROADCAST=y" ;;
        B1) BLE_MODE_KV="CONFIG_APP_BLE_SENSOR_MODE_GATT=y" ;;
        B3) BLE_MODE_KV="CONFIG_APP_BLE_SENSOR_MODE_GATT_OSCORE=y" ;;
        *)
            echo "error: unknown BLE scenario '${BLE_SCENARIO}' — expected B0 B1 B3"
            exit 1
            ;;
    esac

    # Selective update of hub-lte local.conf: replace BLE mode line, keep everything else
    HUB_LTE_REST=""
    if [[ -f "$HUB_LTE" ]]; then
        HUB_LTE_REST="$(grep -v '^CONFIG_APP_BLE_SENSOR_MODE_' "$HUB_LTE" || true)"
    fi
    {
        [[ -n "$HUB_LTE_REST" ]] && printf '%s\n' "$HUB_LTE_REST"
        echo ""
        echo "# BLE sensor mode — sent to nRF5340 hub-ble over UART at boot"
        echo "${BLE_MODE_KV}"
    } > "${HUB_LTE}.tmp" && mv "${HUB_LTE}.tmp" "$HUB_LTE"

    # Sensor local.conf still needs the security mode + OSCORE keys (firmware-level config)
    case "$BLE_SCENARIO" in
        B0)
            cat > "$SENSOR" <<'EOF'
# Scenario B0: Broadcast plain — non-connectable ADV, plain binary manufacturer data
CONFIG_APP_BLE_SECURITY_BROADCAST=y
CONFIG_APP_SAMPLING_MODE_PERIODIC=y
CONFIG_APP_SAMPLING_INTERVAL_SEC=30
EOF
            ;;
        B1)
            cat > "$SENSOR" <<'EOF'
# Scenario B1: GATT plain — connectable ADV, binary env notify, no encryption
CONFIG_APP_BLE_SECURITY_GATT=y
CONFIG_APP_SAMPLING_MODE_PERIODIC=y
CONFIG_APP_SAMPLING_INTERVAL_SEC=30
EOF
            ;;
        B3)
            cat > "$SENSOR" <<EOF
# Scenario B3: GATT + OSCORE E2E — sensor encrypts with OSCORE before BLE notify
CONFIG_APP_BLE_SECURITY_GATT_OSCORE=y
CONFIG_APP_OSCORE_MASTER_SECRET=${S_SECRET}
CONFIG_APP_OSCORE_MASTER_SALT=${S_SALT}
CONFIG_APP_OSCORE_SENDER_ID=${S_SENDER}
CONFIG_APP_OSCORE_RECIPIENT_ID=${S_RECIPIENT}
CONFIG_APP_SAMPLING_MODE_PERIODIC=y
CONFIG_APP_SAMPLING_INTERVAL_SEC=30
EOF
            ;;
    esac
    echo "BLE ${BLE_SCENARIO} → ${HUB_LTE#"${WORKSPACE}/"} + ${SENSOR#"${WORKSPACE}/"}"
fi

# ── Server .env.testing update ────────────────────────────────────────────────
if $UPDATE_SERVER; then
    if [[ -z "${SERVER_MODE:-}" && -n "$COAP_SCENARIO" ]]; then
        echo "error: SERVER_MODE not set (internal bug)"; exit 1
    fi
    if [[ -z "${SERVER_MODE:-}" ]]; then
        echo "warning: --server given but no --coap scenario specified — skipping server update"
    else
        {
            echo "# Active test scenario — committed to git."
            echo "# Updated by set_scenario.sh on $(date -u '+%Y-%m-%d %H:%M UTC')"
            echo ""
            echo "SECURITY_MODE=${SERVER_MODE}"
            echo "OSCORE_CONTEXT_DIR=${DOCKER_OSCORE_PATH}"
            if [[ "$SERVER_MODE" == *dtls* ]]; then
                RAW_PSK="$(get_cfg "$HUB_LTE" CONFIG_APP_DTLS_PSK_HEX | tr -d '"')"
                RAW_ID="$(get_cfg "$HUB_LTE" CONFIG_APP_DTLS_PSK_IDENTITY | tr -d '"')"
                echo "DTLS_PSK_KEY_HEX=${RAW_PSK}"
                echo "DTLS_PSK_IDENTITY=${RAW_ID}"
            else
                echo "DTLS_PSK_IDENTITY=tracker-hub"
                echo "DTLS_PSK_KEY_HEX="
            fi
        } > "${SERVER_TESTING_ENV}"

        echo "Server scenario → ${SERVER_TESTING_ENV#"${WORKSPACE}/"} (SECURITY_MODE=${SERVER_MODE})"
        echo "Commit and push to deploy: git add tracker-server/.env.testing && git push"
    fi

    if $RESTART_SERVER; then
        echo "Restarting tracker-server locally…"
        (cd "${WORKSPACE}/tracker-server" && docker compose up -d)
        echo "Done."
    fi
fi

echo ""
echo "Next: rebuild affected targets"
if [[ -n "$BLE_SCENARIO" && -n "$COAP_SCENARIO" ]]; then
    echo "  ./scripts/sensor.sh pristine   # sensor BLE mode changed"
    echo "  ./scripts/lte.sh               # hub-lte CoAP + BLE mode changed"
    echo "  (hub-ble does not need reflashing — mode is sent over UART at boot)"
elif [[ -n "$BLE_SCENARIO" ]]; then
    echo "  ./scripts/sensor.sh pristine   # sensor BLE mode changed"
    echo "  ./scripts/lte.sh               # hub-lte BLE mode command changed"
    echo "  (hub-ble does not need reflashing — mode is sent over UART at boot)"
elif [[ -n "$COAP_SCENARIO" ]]; then
    echo "  ./scripts/lte.sh               # hub-lte cloud mode changed"
fi
