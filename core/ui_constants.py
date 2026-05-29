"""
ui_constants.py — display constants, regex patterns, and text utilities.

Contains everything that describes how the UI looks and how log lines are
classified. No subprocess, no serial, no filesystem access.
"""

import re

# ── Panel / source names ─────────────────────────────────────────────────────────

BAUD            = 115200
SOURCES         = ["LTE", "BLE", "Thingy53", "Server", "Events"]
_DEVICE_SOURCES = ["LTE", "BLE", "Thingy53"]

# Sentinel: queue item whose payload[2] is a callable to run on the main thread.
_UI_ = "__ui__"

# ── Colour palette ───────────────────────────────────────────────────────────────

PALETTE = {
    "BG":   "#0d1117",
    "BG2":  "#161b22",
    "BG3":  "#21262d",
    "FG":   "#c9d1d9",
    "MUTE": "#8b949e",
    "RED":  "#ff7b72",
    "GRN":  "#57ab5a",
}

SOURCE_COLOR = {
    "BLE":      "#58c4dd",
    "LTE":      "#e6a817",
    "Thingy53": "#57ab5a",
    "Server":   "#a371f7",
    "Events":   "#9ecbff",
}

_RESET_LABEL = {
    "BLE":      "Thingy:91X (BLE)",
    "LTE":      "Thingy:91X (LTE)",
    "Thingy53": "Thingy:53",
    "Server":   "Server",
}

LOG_LEVEL_COLOR = {
    "dbg":       "#79c0ff",   # blue
    "inf":       "#e6edf3",   # near-white
    "err":       "#ff7b72",   # red
    "wrn":       "#e3b341",   # amber
    "milestone": "#d2a8ff",   # purple — [Module] milestone line
}

# ── Regex patterns ────────────────────────────────────────────────────────────────

_ANSI_RE      = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|[A-Za-z])")
_PROMPT_RE    = re.compile(r"^(?:uart:~\$\s*)+")
_LOG_LEVEL_RE = re.compile(r"<(dbg|inf|err|wrn)>")
_MILESTONE_RE = re.compile(r"\[(Sensor|BLE|LTE|Server|Test)\] ")

# ── Security mode constants ───────────────────────────────────────────────────────

_COAP_LABELS = [
    ("none",        "None"),
    ("oscore",      "OSCORE"),
    ("dtls",        "DTLS"),
    ("dtls_oscore", "DTLS+OSCORE"),
]
_BLE_LABELS = [
    ("gatt",             "GATT"),
    ("gatt_oscore",      "GATT+OSCORE"),
    ("lesc",             "LESC"),
    ("broadcast",        "Broadcast"),
    ("broadcast_oscore", "Broadcast+OSCORE"),
]

_COAP_MODE_PAT = re.compile(r"^CONFIG_APP_COAP_SECURITY_(\w+)=y", re.M)
_BLE_MODE_PAT  = re.compile(r"^CONFIG_APP_BLE_SECURITY_(\w+)=y",  re.M)

# BLE modes whose payload the hub cannot decode (encrypted blobs — hub relays opaquely)
_OSCORE_BLE_MODES  = frozenset({"gatt_oscore", "broadcast_oscore"})
# CoAP modes that require server-side OSCORE decryption
_OSCORE_COAP_MODES = frozenset({"oscore", "dtls_oscore"})
# Hub BLE relay Kconfig flags required for each sensor BLE mode
_HUB_BLE_RELAY_FLAGS: dict[str, dict[str, bool]] = {
    "gatt":             {},
    "lesc":             {},
    "broadcast":        {"CONFIG_APP_SENSOR_RELAY_BROADCAST": True},
    "gatt_oscore":      {"CONFIG_APP_SENSOR_RELAY_OSCORE":    True},
    "broadcast_oscore": {"CONFIG_APP_SENSOR_RELAY_OSCORE":    True},
}

# ── Event pattern matching ────────────────────────────────────────────────────────
# Patterns matched against raw log lines from any device UART source.
# When matched, the label is forwarded to the Events panel as "[source] label".
# Milestone strings ([Module] ...) are handled separately in _append — they are
# forwarded verbatim to Events so the [Module] prefix is preserved exactly.
# These legacy patterns cover old firmware that has not yet been updated.

_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\*\*\* Booting"),                         "rebooted"),
    (re.compile(r"Security config sent to nRF9151"),        "BLE: security_config sent"),
    (re.compile(r"security_config: mode="),                 "LTE: security_config received"),
    (re.compile(r"[Nn]etwork connected"),                   "LTE: network connected"),
    (re.compile(r"DTLS.*[Oo][Kk]|dtls.*handshake.*done"),  "LTE: DTLS handshake OK"),
    (re.compile(r"Timesync sent to BLE"),                   "LTE: timesync sent to BLE"),
    (re.compile(r"[Cc]onnected to|BLE connected"),          "BLE: sensor connected"),
    (re.compile(r"OSCORE relay.*queued"),                   "LTE: OSCORE relay queued"),
    (re.compile(r"Waiting for security config"),            "LTE: waiting for security config"),
    (re.compile(r"Security config received.*proceeding"),   "LTE: security config applied"),
    (re.compile(r"Security config timeout"),                "LTE: security config timeout"),
]

# ── Text utilities ────────────────────────────────────────────────────────────────

def strip_ansi(s: str) -> str:
    return _PROMPT_RE.sub("", _ANSI_RE.sub("", s))


def _dev_tag(msg: str) -> str:
    if _MILESTONE_RE.search(msg):
        return "milestone"
    m = _LOG_LEVEL_RE.search(msg)
    return m.group(1) if m else "msg"
