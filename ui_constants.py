"""
ui_constants.py — display constants, regex patterns, and text utilities.

Contains everything that describes how the UI looks and how log lines are
classified. No subprocess, no serial, no filesystem access.
"""

import re

# ── Panel / source names ─────────────────────────────────────────────────────────

BAUD            = 115200
SOURCES         = ["BLE", "LTE", "Thingy53", "Server"]
_DEVICE_SOURCES = ["BLE", "LTE", "Thingy53"]

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
}

_RESET_LABEL = {
    "BLE":      "Thingy:91X (BLE)",
    "LTE":      "Thingy:91X (LTE)",
    "Thingy53": "Thingy:53",
    "Server":   "Server",
}

LOG_LEVEL_COLOR = {
    "dbg": "#79c0ff",   # blue
    "inf": "#e6edf3",   # near-white
    "err": "#ff7b72",   # red
    "wrn": "#e3b341",   # amber
}

# ── Regex patterns ────────────────────────────────────────────────────────────────

_ANSI_RE      = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|[A-Za-z])")
_PROMPT_RE    = re.compile(r"^(?:uart:~\$\s*)+")
_LOG_LEVEL_RE = re.compile(r"<(dbg|inf|err|wrn)>")

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

# ── Text utilities ────────────────────────────────────────────────────────────────

def strip_ansi(s: str) -> str:
    return _PROMPT_RE.sub("", _ANSI_RE.sub("", s))


def _dev_tag(msg: str) -> str:
    m = _LOG_LEVEL_RE.search(msg)
    return m.group(1) if m else "msg"
