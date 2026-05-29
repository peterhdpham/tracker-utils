"""
milestones.py — canonical E2E milestone strings for the tracker system.

Single source of truth for every observable checkpoint in the end-to-end flow.
The test runner and logviewer pattern-match against these substrings.
Firmware (C/Zephyr) and server (Python) must print the exact string listed here
somewhere in their log line — the surrounding log prefix is irrelevant.

Format:  [MODULE] Verb phrase
Modules: Sensor, BLE, LTE, Server, Test
"""

# ── Test infrastructure (emitted by runtest.py) ───────────────────────────────
T_SCENARIO           = "[Test] Scenario"           # suffix: " N/total: ble + coap"
T_WRITING_CONFIGS    = "[Test] Writing configs"
T_RESETTING_LTE      = "[Test] Resetting LTE"
T_PASS               = "[Test] PASS:"              # suffix: " ble + coap"
T_FAIL               = "[Test] FAIL:"              # suffix: " ble + coap"
T_FAIL_REASON        = "[Test] FAIL reason:"       # suffix: " <human-readable cause>"

# ── Build / flash (emitted by runtest.py) ─────────────────────────────────────
SENSOR_BUILD_STARTED  = "[Sensor] Build started"
SENSOR_BUILD_COMPLETE = "[Sensor] Build complete"
SENSOR_BUILD_FAILED   = "[Sensor] Build FAILED"
SENSOR_FLASH_STARTED  = "[Sensor] Flash started"
SENSOR_FLASHED        = "[Sensor] Flashed"

BLE_BUILD_STARTED     = "[BLE] Build started"
BLE_BUILD_COMPLETE    = "[BLE] Build complete"
BLE_BUILD_FAILED      = "[BLE] Build FAILED"
BLE_FLASH_STARTED     = "[BLE] Flash started"
BLE_FLASHED           = "[BLE] Flashed"

SERVER_RESTARTED      = "[Server] Restarted"

# ── Boot (emitted by firmware via UART) ──────────────────────────────────────
SENSOR_BOOTED         = "[Sensor] Booted"
BLE_BOOTED            = "[BLE] Booted"
LTE_BOOTED            = "[LTE] Booted"

# ── Config propagation (firmware UART) ───────────────────────────────────────
BLE_SECURITY_CONTEXT         = "[BLE] Security context:"        # suffix: " ble=X coap=Y"
BLE_SENT_SECURITY_CONFIG     = "[BLE] Sent security config to LTE"
LTE_RECEIVED_SECURITY_CONFIG = "[LTE] Received security config:"  # suffix: " coap=X"
LTE_ATTACHED                 = "[LTE] Attached to network"
LTE_CONNECTED_TO_SERVER      = "[LTE] Connected to server"
BLE_READY                    = "[BLE] Ready"                     # emitted by runtest.py / viewer after flash+boot
LTE_TIMESYNC_SENT            = "[LTE] Timesync sent to BLE"      # already emitted by LTE firmware

# ── BLE sensor link (firmware UART) ──────────────────────────────────────────
BLE_SCANNING            = "[BLE] Scanning for sensor"
BLE_SENSOR_CONNECTED    = "[BLE] Sensor connected"
BLE_SENSOR_DISCONNECTED = "[BLE] Sensor disconnected"

# ── E2E data flow per sample (firmware UART + server stdout) ─────────────────
SENSOR_SAMPLE_SENT      = "[Sensor] Sample sent"
BLE_SAMPLE_RECEIVED     = "[BLE] Sample received from sensor"
BLE_SAMPLE_FORWARDED    = "[BLE] Sample forwarded to LTE"
LTE_SAMPLE_RECEIVED     = "[LTE] Sample received from BLE"
LTE_COAP_POST_SENT      = "[LTE] CoAP POST sent"
SERVER_REQUEST_RECEIVED = "[Server] Request received"
SERVER_OSCORE_DECRYPTED = "[Server] OSCORE decrypted"
SERVER_INFLUXDB_OK      = "[Server] InfluxDB write OK"
SERVER_ACK_SENT         = "[Server] CoAP ACK sent"
LTE_ACK_RECEIVED        = "[LTE] CoAP ACK received"
LTE_RELAY_ACK           = "[LTE] OSCORE relay ACK"

# ── Location (firmware UART) ──────────────────────────────────────────────────
LTE_LOCATION_SEARCH_STARTED = "[LTE] Location search started"
LTE_LOCATION_FIX_WIFI       = "[LTE] Location fix: Wi-Fi"
LTE_LOCATION_FIX_GNSS       = "[LTE] Location fix: GNSS"
LTE_LOCATION_FIX_CELL       = "[LTE] Location fix: Cell"
LTE_LOCATION_FAILED         = "[LTE] Location fix: failed"
LTE_LOCATION_ACK_RECEIVED   = "[LTE] Location CoAP ACK received"  # hub got 2.xx for location POST

# ── Server-side location confirmation ─────────────────────────────────────────
SERVER_INFLUXDB_LOCATION_OK = "[Server] InfluxDB location write OK"  # server TODO: emit this

# ── Errors (firmware UART + server stdout) ───────────────────────────────────
BLE_ERROR_SENSOR_TIMEOUT    = "[BLE] ERROR: Sensor connection timeout"
LTE_ERROR_NETWORK           = "[LTE] ERROR: Network attach failed"
LTE_ERROR_COAP_TIMEOUT      = "[LTE] ERROR: CoAP timeout"
SERVER_ERROR_OSCORE_REPLAY  = "[Server] ERROR: OSCORE replay rejected"
SERVER_ERROR_OSCORE_DECRYPT = "[Server] ERROR: OSCORE decryption failed"
SERVER_ERROR_INFLUXDB       = "[Server] ERROR: InfluxDB write failed"

# ── Convenience: all firmware-sourced milestone substrings ───────────────────
# Used by the logviewer to highlight milestone lines in any panel.
FIRMWARE_MILESTONES: list[str] = [
    SENSOR_BOOTED, BLE_BOOTED, LTE_BOOTED,
    BLE_SECURITY_CONTEXT, BLE_SENT_SECURITY_CONFIG,
    LTE_RECEIVED_SECURITY_CONFIG, LTE_CONNECTED_TO_SERVER,
    BLE_READY, LTE_TIMESYNC_SENT,
    BLE_SCANNING, BLE_SENSOR_CONNECTED, BLE_SENSOR_DISCONNECTED,
    SENSOR_SAMPLE_SENT,
    BLE_SAMPLE_RECEIVED, BLE_SAMPLE_FORWARDED,
    LTE_SAMPLE_RECEIVED, LTE_COAP_POST_SENT, LTE_RELAY_ACK, LTE_ACK_RECEIVED,
    SERVER_REQUEST_RECEIVED, SERVER_OSCORE_DECRYPTED,
    SERVER_INFLUXDB_OK, SERVER_INFLUXDB_LOCATION_OK, SERVER_ACK_SENT,
    LTE_LOCATION_SEARCH_STARTED,
    LTE_LOCATION_FIX_WIFI, LTE_LOCATION_FIX_GNSS,
    LTE_LOCATION_FIX_CELL, LTE_LOCATION_FAILED,
    LTE_LOCATION_ACK_RECEIVED,
    BLE_ERROR_SENSOR_TIMEOUT, LTE_ERROR_NETWORK,
    LTE_ERROR_COAP_TIMEOUT,
    SERVER_ERROR_OSCORE_REPLAY, SERVER_ERROR_OSCORE_DECRYPT,
    SERVER_ERROR_INFLUXDB,
]
