"""
build_config.py — build/flash target definitions for the tracker project.

This is the single source of truth for:
  • NCS toolchain version and nrfutil wrapper
  • Workspace / app / build-dir paths
  • Per-target build and flash commands (TARGETS dict)
  • _effective_build_cmd() — injects optional conf files at runtime

Claude: read this file to answer any build or flash question.
"""

from pathlib import Path

# ── NCS toolchain ────────────────────────────────────────────────────────────────

NCS_VERSION  = "v3.1.1"
NRFUTIL_WRAP = [
    "nrfutil", "sdk-manager", "toolchain", "launch",
    "--ncs-version", NCS_VERSION, "--",
]

# ── Workspace paths ──────────────────────────────────────────────────────────────
#
# This file lives at tracker_project/tracker-utils/build_config.py
# → parent = tracker_project/

_HERE      = Path(__file__).resolve().parent   # tracker_project/tracker-utils/core/
_WORKSPACE = _HERE.parent.parent               # tracker_project/

_LTE_APP  = _WORKSPACE / "tracker-hub"         / "apps" / "tracker-hub-lte"
_BLE_APP  = _WORKSPACE / "tracker-hub"         / "apps" / "tracker-hub-ble"
_SENS_APP = _WORKSPACE / "tracker-sensor-node" / "tracker-node"

_LTE_BLD  = _WORKSPACE / "build" / "lte"
_BLE_BLD  = _WORKSPACE / "build" / "ble"
_SENS_BLD = _WORKSPACE / "build" / "sensor"

# ── Programmer serial numbers ────────────────────────────────────────────────────

SNR_HUB    = "1051217937"   # nRF9151 DK (PCA10171) → Thingy:91X Debug In
SNR_SENSOR = "1050065248"   # nRF53  DK (PCA10095)  → Thingy:53 SWD pads

# ── Per-target build / flash definitions ─────────────────────────────────────────
#
# build_cmd / flash_cmd are bare west args — prepend NRFUTIL_WRAP at call time.
# pre_flash_cmds (optional): raw commands run WITHOUT nrfutil wrap before flash.
# optional_conf: appended as -DEXTRA_CONF_FILE= only when the file exists on disk.

TARGETS: dict = {
    "lte": {
        "label":   "LTE",
        "panels":  ["LTE"],
        "snr":     SNR_HUB,
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy91x/nrf9151/ns",
            "--build-dir", str(_LTE_BLD),
            str(_LTE_APP),
            "--sysbuild",
        ],
        "optional_conf": _LTE_APP / "local.conf",
        "flash_cmd": [
            "west", "flash",
            "--recover",
            "--build-dir", str(_LTE_BLD),
            "--snr", SNR_HUB,
        ],
    },
    "ble": {
        "label":   "BLE",
        "panels":  ["BLE"],
        "snr":     SNR_HUB,
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy91x/nrf5340/cpuapp",
            "--build-dir", str(_BLE_BLD),
            str(_BLE_APP),
            "--sysbuild",
        ],
        "optional_conf":      _BLE_APP / "local.conf",
        "security_conf_files": [_BLE_APP / "security.conf"],
        # sysbuild forwards image-specific vars — use cmake_image prefix so conf
        # files reach the BLE image (named tracker-hub-ble, matching the app dir).
        "cmake_image": "tracker-hub-ble",
        "flash_cmd": [
            "west", "flash",
            "--recover",
            "--build-dir", str(_BLE_BLD),
            "--snr", SNR_HUB,
        ],
    },
    "sensor": {
        "label":   "Sensor",
        "panels":  ["Thingy53"],
        "snr":     SNR_SENSOR,
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy53/nrf5340/cpuapp",
            "--build-dir", str(_SENS_BLD),
            str(_SENS_APP),
        ],
        "optional_conf":      _SENS_APP / "local.conf",
        "security_conf_files": [_SENS_APP / "security.conf"],
        "flash_cmd": [
            "west", "flash",
            "--recover",
            "--build-dir", str(_SENS_BLD),
            "--snr", SNR_SENSOR,
        ],
    },
}


def _effective_build_cmd(tgt: dict) -> list:
    """Return build_cmd with optional/security conf files appended if they exist."""
    cmd = list(tgt["build_cmd"])
    conf_files = []
    main_conf = tgt.get("optional_conf")
    if main_conf and Path(main_conf).exists():
        conf_files.append(str(main_conf))
    for sc in tgt.get("security_conf_files", []):
        if Path(sc).exists():
            conf_files.append(str(sc))
    if conf_files:
        cmake_image = tgt.get("cmake_image")
        var = f"{cmake_image}_EXTRA_CONF_FILE" if cmake_image else "EXTRA_CONF_FILE"
        cmd += ["--", f"-D{var}={';'.join(conf_files)}"]
    return cmd
