#!/usr/bin/env python3
"""
runtest.py — Automated scenario tester for tracker_project.

Usage:
    python3 runtest.py <ble> <coap> [modifiers...]
    python3 runtest.py b0 c0                          # broadcast + none
    python3 runtest.py b3 c3                          # gatt_oscore + dtls_oscore
    python3 runtest.py b3 c3 --wrong-oscore-hub       # negative test
    python3 runtest.py b0 c0 b3 c3                    # chain two runs
    python3 runtest.py b3 c3 --rotate-oscore-both     # rotate keys first

Scenario codes:
    b0=broadcast  b1=gatt  b2=lesc  b3=gatt_oscore  b4=broadcast_oscore
    c0=none  c1=oscore  c2=dtls  c3=dtls_oscore

Modifiers (applied to the next scenario pair):
    --rotate-dtls             Regenerate DTLS PSK before test
    --rotate-oscore-hub       Regenerate hub OSCORE keys before test
    --rotate-oscore-sensor    Regenerate sensor OSCORE keys before test
    --rotate-oscore-both      Regenerate both OSCORE key sets before test
    --wrong-dtls              Corrupt DTLS PSK (expect DTLS handshake failure)
    --wrong-oscore-hub        Corrupt hub OSCORE secret (expect CoAP 4.01)
    --wrong-oscore-sensor     Corrupt sensor OSCORE secret (expect CoAP 4.01)

NOTE: The logviewer can run alongside this script. Serial ports are only opened
briefly for commands (reset lte, att_sample) — the logviewer reconnects within 50ms.
Phase readiness is detected via fixed delays, not serial monitoring; watch the
logviewer's Events panel and LTE/BLE panels for live progress.

Results are saved under tracker-utils/testruns/<timestamp>_<scenario>/
    run_info.json       — scenario, configs, key values, PASS/FAIL
    event_timeline.txt  — full timestamped event timeline
    configs/
        ble_security.conf   — BLE security.conf snapshot (after key setup)
        sensor_security.conf
        server_env.txt      — server .env snapshot after restart
    logs/
        server.txt          — docker compose logs from coap-server
    (UART logs: visible in the logviewer's BLE / LTE / Thingy53 panels)
"""

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import serial  # pyserial — only used for brief command sends

# ── Module resolution ─────────────────────────────────────────────────────────
# (sys.path insertion happens below, before local imports)

_HERE = Path(__file__).parent.resolve()
_CORE = _HERE / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from build_config import (
    TARGETS, NRFUTIL_WRAP, _BLE_APP, _SENS_APP, _WORKSPACE, _effective_build_cmd,
)
from kconfig_utils import (
    _update_kconfig_key, _set_kconfig_mode, _set_kconfig_value, _read_kconfig_value,
)
from serial_io import find_thingy91x_ports, find_thingy53_port
from server_io import SSH_SERVER_HOST, _ssh
from ui_constants import _OSCORE_BLE_MODES, _OSCORE_COAP_MODES, _HUB_BLE_RELAY_FLAGS
import milestones as MS

# ── Scenario definitions ──────────────────────────────────────────────────────

_BLE_CODES: dict[str, str] = {
    "b0": "broadcast",
    "b1": "gatt",
    "b2": "lesc",
    "b3": "gatt_oscore",
    "b4": "broadcast_oscore",
}
_COAP_CODES: dict[str, str] = {
    "c0": "none",
    "c1": "oscore",
    "c2": "dtls",
    "c3": "dtls_oscore",
}

_TESTRUNS_DIR = _HERE / "testruns"

# Timing constants (seconds)
_WAIT_LTE_REBOOT   = 5    # after reset lte: brief pause before starting builds

# ── Event log ─────────────────────────────────────────────────────────────────

class EventLog:
    def __init__(self, live_path: Path | None = None):
        self._t0        = time.time()
        self._lines: list[str] = []
        self._live_fh   = None
        self._lock      = threading.Lock()
        if live_path is not None:
            self._live_fh = live_path.open("a", encoding="utf-8", buffering=1)

    def log(self, msg: str):
        elapsed  = time.time() - self._t0
        wall_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry    = f"{wall_str}  [T+{elapsed:6.1f}s] {msg}"
        print(entry, flush=True)
        with self._lock:
            self._lines.append(entry)
            if self._live_fh:
                self._live_fh.write(entry + "\n")
                self._live_fh.flush()

    def box(self, lines: list[str]):
        width  = max((len(l) for l in lines), default=4) + 4
        border = "─" * width
        print(f"          ┌{border}┐")
        for line in lines:
            pad = width - len(line) - 2
            print(f"          │ {line}{' ' * pad} │")
        print(f"          └{border}┘", flush=True)
        self._lines.extend([f"          {l}" for l in lines])

    def close(self):
        with self._lock:
            if self._live_fh:
                self._live_fh.close()
                self._live_fh = None

    def report(self):
        print("\n" + "=" * 64)
        print("EVENT TIMELINE")
        print("=" * 64)
        for entry in self._lines:
            print(entry)

    def save(self, path: Path):
        path.write_text("\n".join(self._lines), encoding="utf-8")


# ── Run directory ─────────────────────────────────────────────────────────────

class RunDir:
    """Per-run output directory, either inside a suite folder or flat under testruns/."""
    def __init__(self, ble_mode: str, coap_mode: str, wrong_flags: frozenset,
                 suite_path: Path | None = None, idx: int | None = None):
        suffix = ("_" + "_".join(sorted(wrong_flags))) if wrong_flags else ""
        if suite_path is not None:
            if idx is not None:
                name = f"{idx:02d}_b-{ble_mode}_c-{coap_mode}{suffix}"
            else:
                ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                name = f"{ts}_b-{ble_mode}_c-{coap_mode}{suffix}"
            self.path = suite_path / name
        else:
            ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            name = f"{ts}_b-{ble_mode}_c-{coap_mode}{suffix}"
            self.path = _TESTRUNS_DIR / name
        (self.path / "logs").mkdir(parents=True, exist_ok=True)
        (self.path / "configs").mkdir(exist_ok=True)

    def log_path(self, name: str) -> Path:
        return self.path / "logs" / name

    def config_path(self, name: str) -> Path:
        return self.path / "configs" / name

    def write_info(self, info: dict):
        (self.path / "run_info.json").write_text(
            json.dumps(info, indent=2, default=str), encoding="utf-8"
        )

    def write_timeline(self, log: EventLog):
        log.save(self.path / "event_timeline.txt")


# ── Serial command sender ─────────────────────────────────────────────────────

def _send_brief(port: str | None, cmd: str, log: EventLog):
    """
    Open *port* briefly, send *cmd*, close immediately.
    The logviewer reconnects within ~50 ms after we release the port.
    Safe to call while the logviewer is running — pyserial does not set
    TIOCEXCL by default, so a brief second open works on Linux.
    """
    if not port:
        log.log(f"WARNING: cannot send '{cmd}' — port not found")
        return
    try:
        with serial.Serial(port, 115200, timeout=1) as s:
            s.write(f"{cmd}\r\n".encode())
            time.sleep(0.15)
        log.log(f"→ '{cmd}' sent on {port}")
    except serial.SerialException as e:
        log.log(f"WARNING: send '{cmd}' failed: {e}")


# ── Run-state persistence ─────────────────────────────────────────────────────

_STATE_FILE = _HERE / ".runstate.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(info: dict):
    existing = _load_state()
    existing.update(info)
    _STATE_FILE.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")


def _conf_hash(path) -> str:
    import hashlib
    p = Path(path)
    if not p.exists():
        return ""
    return hashlib.md5(p.read_bytes()).hexdigest()


# ── Kconfig helpers ───────────────────────────────────────────────────────────

# ── SSH helpers ───────────────────────────────────────────────────────────────

def _wait_lte_patterns(lte_port: str | None,
                       patterns_and_timeouts: list[tuple[str, float]],
                       log: EventLog) -> dict[str, bool]:
    """Watch the LTE UART for multiple patterns simultaneously.

    Opens *lte_port* once and reads until every pattern has either matched or
    its individual timeout expired.  Returns {pattern: matched_bool}.
    """
    results  = {p: False for p, _ in patterns_and_timeouts}
    if not lte_port:
        log.log("LTE: no port — skipping LTE pattern watch")
        return results

    import re as _re
    compiled  = {p: _re.compile(p) for p, _ in patterns_and_timeouts}
    deadlines = {p: time.time() + t  for p, t in patterns_and_timeouts}

    try:
        with serial.Serial(lte_port, 115200, timeout=0.5) as s:
            buf = b""
            while True:
                now    = time.time()
                active = [p for p, _ in patterns_and_timeouts
                          if not results[p] and now < deadlines[p]]
                if not active:
                    break
                chunk = s.read(256)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    for p in list(active):
                        if compiled[p].search(text):
                            results[p] = True
                            log.log(f"LTE ✓ {p!r} — {text[:100]}")
    except Exception as e:
        log.log(f"LTE serial watch error: {e}")

    return results

def _ssh_update_env(key: str, value: str):
    cmd = (
        f"python3 -c \""
        f"import re, pathlib; "
        f"p = pathlib.Path('/root/tracker-server/.env'); "
        f"t = p.read_text(); "
        f"t = re.sub(r'^{key}=.*', r'{key}={value}', t, flags=re.M); "
        f"p.write_text(t)\""
    )
    _ssh(cmd)

def _ssh_restart_server(log: EventLog) -> bool:
    log.log("[Server] Restarting…")
    # Delete sequence.json before restart so aiocoap starts with an empty
    # replay window — otherwise SSN=0 from a freshly flashed sensor is rejected
    # as a replay of a previously seen sequence number (ReplayErrorWithEcho).
    r = _ssh(
        "cd tracker-server"
        " && find oscore-context -name 'sequence.json' -delete"
        " && docker compose up --force-recreate -d coap-server 2>&1",
        timeout=60,
    )
    ok = r.returncode == 0
    log.log(MS.SERVER_RESTARTED if ok else "[Server] Restart FAILED")
    return ok

def _ssh_read_env() -> dict[str, str]:
    r = _ssh("cat /root/tracker-server/.env")
    result: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


# ── Build helpers ─────────────────────────────────────────────────────────────

_BUILD_MS  = {"ble": (MS.BLE_BUILD_STARTED,    MS.BLE_BUILD_COMPLETE,    MS.BLE_BUILD_FAILED),
              "sensor": (MS.SENSOR_BUILD_STARTED, MS.SENSOR_BUILD_COMPLETE, MS.SENSOR_BUILD_FAILED)}
_FLASH_MS  = {"ble": (MS.BLE_FLASH_STARTED, MS.BLE_FLASHED),
              "sensor": (MS.SENSOR_FLASH_STARTED, MS.SENSOR_FLASHED)}


def _build(key: str, log: EventLog, pristine: bool = True) -> bool:
    tgt = TARGETS[key]
    cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
    if pristine:
        wi  = cmd.index("west")
        cmd = cmd[:wi + 2] + ["--pristine"] + cmd[wi + 2:]
    ms_start, ms_ok, ms_fail = _BUILD_MS.get(key, (None, None, None))
    if ms_start:
        log.log(ms_start)
    result = subprocess.run(cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(ms_ok if ok else ms_fail or f"{tgt['label']}: build FAILED")
    return ok


def _flash(key: str, log: EventLog) -> bool:
    tgt = TARGETS[key]
    for pre_cmd in tgt.get("pre_flash_cmds", []):
        subprocess.run(pre_cmd, capture_output=True)
    cmd = NRFUTIL_WRAP + tgt["flash_cmd"]
    ms_start, ms_ok = _FLASH_MS.get(key, (None, None))
    if ms_start:
        log.log(ms_start)
    result = subprocess.run(cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(ms_ok if ok else f"{tgt['label']}: flash FAILED")
    return ok


# ── Prebuild infrastructure ───────────────────────────────────────────────────

_PREBUILD_DIR = _WORKSPACE / "build" / "prebuilt"


def _sensor_variant_key(ble_mode: str) -> str:
    return ble_mode


def _ble_variant_key(ble_mode: str, coap_mode: str) -> str:
    flags = _HUB_BLE_RELAY_FLAGS.get(ble_mode, {})
    if flags.get("CONFIG_APP_SENSOR_RELAY_OSCORE"):
        relay = "oscore"
    elif flags.get("CONFIG_APP_SENSOR_RELAY_BROADCAST"):
        relay = "broadcast"
    else:
        relay = "none"
    return f"{relay}_{coap_mode}"


def _sensor_prebuild_dir(ble_mode: str) -> Path:
    return _PREBUILD_DIR / f"sensor_{_sensor_variant_key(ble_mode)}"


def _ble_prebuild_dir(ble_mode: str, coap_mode: str) -> Path:
    return _PREBUILD_DIR / f"ble_{_ble_variant_key(ble_mode, coap_mode)}"


def _prebuild_artifact_ready(build_dir: Path) -> bool:
    return (build_dir / "BUILD_OK").exists()


_STAGED_CONF_DIR = _PREBUILD_DIR / "staged"


def _write_sensor_staged_conf(ble_mode: str) -> Path:
    """Write sensor security.conf outside any build dir so --pristine cannot delete it."""
    _STAGED_CONF_DIR.mkdir(parents=True, exist_ok=True)
    base   = _SENS_APP / "security.conf"
    staged = _STAGED_CONF_DIR / f"sensor_{ble_mode}.conf"
    staged.write_text(base.read_text() if base.exists() else "")
    _set_kconfig_mode(staged, "CONFIG_APP_BLE_SECURITY_",
                      f"CONFIG_APP_BLE_SECURITY_{ble_mode.upper()}=y")
    return staged


def _write_ble_staged_conf(ble_mode: str, coap_mode: str) -> Path:
    """Write hub BLE security.conf outside any build dir so --pristine cannot delete it."""
    _STAGED_CONF_DIR.mkdir(parents=True, exist_ok=True)
    base   = _BLE_APP / "security.conf"
    staged = _STAGED_CONF_DIR / f"ble_{_ble_variant_key(ble_mode, coap_mode)}.conf"
    staged.write_text(base.read_text() if base.exists() else "")
    _set_kconfig_mode(staged, "CONFIG_APP_COAP_SECURITY_",
                      f"CONFIG_APP_COAP_SECURITY_{coap_mode.upper()}=y")
    flags = _HUB_BLE_RELAY_FLAGS.get(ble_mode, {})
    for flag in ("CONFIG_APP_SENSOR_RELAY_BROADCAST", "CONFIG_APP_SENSOR_RELAY_OSCORE"):
        _set_kconfig_value(staged, flag, flags.get(flag, False))
    return staged


def _build_to_dir(target_key: str, build_dir: Path,
                  staged_conf: Path, log: EventLog) -> bool:
    """Pristine build of target_key into build_dir using staged_conf for security config."""
    tgt = TARGETS[target_key]
    cmd = list(tgt["build_cmd"])

    bd_idx = cmd.index("--build-dir")
    cmd[bd_idx + 1] = str(build_dir)

    conf_files = []
    main_conf = tgt.get("optional_conf")
    if main_conf and Path(main_conf).exists():
        conf_files.append(str(main_conf))
    conf_files.append(str(staged_conf))

    cmake_image = tgt.get("cmake_image")
    var = f"{cmake_image}_EXTRA_CONF_FILE" if cmake_image else "EXTRA_CONF_FILE"
    cmd += ["--", f"-D{var}={';'.join(conf_files)}"]

    wi  = cmd.index("west")
    cmd = cmd[:wi + 2] + ["--pristine"] + cmd[wi + 2:]

    ms_start, ms_ok, ms_fail = _BUILD_MS.get(target_key, (None, None, None))
    if ms_start:
        log.log(ms_start)
    result = subprocess.run(NRFUTIL_WRAP + cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(ms_ok if ok else ms_fail or f"{tgt['label']}: build FAILED")
    if ok:
        (build_dir / "BUILD_OK").touch()
    return ok


def _flash_from_dir(target_key: str, build_dir: Path, log: EventLog) -> bool:
    """Flash target_key from build_dir instead of the TARGETS-default build dir."""
    tgt = TARGETS[target_key]
    for pre_cmd in tgt.get("pre_flash_cmds", []):
        subprocess.run(pre_cmd, capture_output=True)
    flash_cmd = list(tgt["flash_cmd"])
    bd_idx = flash_cmd.index("--build-dir")
    flash_cmd[bd_idx + 1] = str(build_dir)
    ms_start, ms_ok = _FLASH_MS.get(target_key, (None, None))
    if ms_start:
        log.log(ms_start)
    result = subprocess.run(NRFUTIL_WRAP + flash_cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(ms_ok if ok else f"{tgt['label']}: flash FAILED")
    return ok


class PrebuildSession:
    """Tracks parallel prebuild state for one suite invocation."""

    def __init__(self):
        self.sensor_status: dict[str, str]              = {}
        self.ble_status:    dict[str, str]              = {}
        self.sensor_events: dict[str, threading.Event]  = {}
        self.ble_events:    dict[str, threading.Event]  = {}
        self.sensor_dirs:   dict[str, Path]             = {}
        self.ble_dirs:      dict[str, Path]             = {}
        self._lock = threading.Lock()

    def _set_status(self, kind: str, key: str, status: str):
        with self._lock:
            if kind == "sensor":
                self.sensor_status[key] = status
            else:
                self.ble_status[key] = status

    def get_sensor_status(self, ble_mode: str) -> str:
        with self._lock:
            return self.sensor_status.get(ble_mode, "not_in_session")

    def get_ble_status(self, ble_mode: str, coap_mode: str) -> str:
        key = _ble_variant_key(ble_mode, coap_mode)
        with self._lock:
            return self.ble_status.get(key, "not_in_session")

    def wait_for_sensor(self, ble_mode: str, timeout: float | None = None) -> str:
        ev = self.sensor_events.get(ble_mode)
        if ev:
            ev.wait(timeout)
        return self.get_sensor_status(ble_mode)

    def wait_for_ble(self, ble_mode: str, coap_mode: str,
                     timeout: float | None = None) -> str:
        key = _ble_variant_key(ble_mode, coap_mode)
        ev  = self.ble_events.get(key)
        if ev:
            ev.wait(timeout)
        return self.get_ble_status(ble_mode, coap_mode)


def prebuild_all(
    scenarios: list[dict],
    log: EventLog,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> PrebuildSession:
    """
    Build all unique firmware variants from scenarios in parallel.
    Returns a PrebuildSession immediately — builds run in daemon threads.
    Scenarios with 'rotate' fields are excluded (key rotation can't happen ahead of time).
    """
    session = PrebuildSession()

    sensor_variants: set[str]                   = set()
    ble_variants:    dict[str, tuple[str, str]] = {}

    for s in scenarios:
        if s.get("skip", False) or s.get("rotate"):
            continue
        ble  = s["ble"]
        coap = s["coap"]
        sensor_variants.add(ble)
        bk = _ble_variant_key(ble, coap)
        if bk not in ble_variants:
            ble_variants[bk] = (ble, coap)

    for ble_mode in sensor_variants:
        session.sensor_status[ble_mode] = "pending"
        session.sensor_events[ble_mode] = threading.Event()
        session.sensor_dirs[ble_mode]   = _sensor_prebuild_dir(ble_mode)

    for bk, (ble_mode, coap_mode) in ble_variants.items():
        session.ble_status[bk] = "pending"
        session.ble_events[bk] = threading.Event()
        session.ble_dirs[bk]   = _ble_prebuild_dir(ble_mode, coap_mode)

    def _build_sensor(ble_mode: str):
        build_dir = session.sensor_dirs[ble_mode]
        session._set_status("sensor", ble_mode, "building")
        if on_progress:
            on_progress("sensor", ble_mode, "building")
        staged = _write_sensor_staged_conf(ble_mode)
        ok     = _build_to_dir("sensor", build_dir, staged, log)
        status = "done" if ok else "failed"
        session._set_status("sensor", ble_mode, status)
        session.sensor_events[ble_mode].set()
        if on_progress:
            on_progress("sensor", ble_mode, status)

    def _build_ble(bk: str, ble_mode: str, coap_mode: str):
        build_dir = session.ble_dirs[bk]
        session._set_status("ble", bk, "building")
        if on_progress:
            on_progress("ble", bk, "building")
        staged = _write_ble_staged_conf(ble_mode, coap_mode)
        ok     = _build_to_dir("ble", build_dir, staged, log)
        status = "done" if ok else "failed"
        session._set_status("ble", bk, status)
        session.ble_events[bk].set()
        if on_progress:
            on_progress("ble", bk, status)

    for ble_mode in sensor_variants:
        threading.Thread(target=_build_sensor, args=(ble_mode,), daemon=True).start()

    for bk, (ble_mode, coap_mode) in ble_variants.items():
        threading.Thread(target=_build_ble, args=(bk, ble_mode, coap_mode), daemon=True).start()

    log.log(f"Prebuild: spawned {len(sensor_variants)} sensor + {len(ble_variants)} BLE build threads")
    return session


# ── Key rotation ──────────────────────────────────────────────────────────────

def _rotate_oscore_keys(targets: list[str], log: EventLog) -> bool:
    conf_map = {
        "hub":    _BLE_APP  / "security.conf",
        "sensor": _SENS_APP / "security.conf",
    }
    for name in targets:
        log.log(f"Rotating OSCORE keys for {name}...")
        r = _ssh(f"cd ~/tracker-server && python3 generate_oscore_psk.py --name {name} 2>&1", 30)
        if r.returncode != 0:
            print(r.stdout)
            log.log(f"OSCORE rotation FAILED for {name}")
            return False
        conf_path = conf_map[name]
        for ln in r.stdout.splitlines():
            if ln.startswith("CONFIG_APP_OSCORE_"):
                _update_kconfig_key(conf_path, ln)
        log.log(f"  → {conf_path.name} updated")
    _ssh_update_env("OSCORE_CONTEXT_DIR", "/app/oscore-context")
    return True

def _rotate_dtls_keys(log: EventLog) -> bool:
    log.log("Rotating DTLS keys...")
    r = _ssh("cd ~/tracker-server && python3 generate_dtls_psk.py 2>&1", 30)
    if r.returncode != 0:
        print(r.stdout)
        log.log("DTLS rotation FAILED")
        return False
    conf_path = _BLE_APP / "security.conf"
    for ln in r.stdout.splitlines():
        if ln.startswith("CONFIG_APP_DTLS_"):
            _update_kconfig_key(conf_path, ln)
    for ln in r.stdout.splitlines():
        if ln.startswith("DTLS_PSK_IDENTITY=") or ln.startswith("DTLS_PSK_KEY_HEX="):
            k, v = ln.split("=", 1)
            _ssh_update_env(k, v)
    log.log(f"  → {conf_path.name} + server .env updated")
    return True


# ── Context display ───────────────────────────────────────────────────────────

def _print_ble_ready(ble_mode: str, coap_mode: str, log: EventLog):
    sec   = _BLE_APP / "security.conf"
    lines = [f"BLE READY  ({ble_mode} / {coap_mode})", ""]
    for label, key in [
        ("CoAP mode",     "CONFIG_APP_COAP_SECURITY_"),
        ("OSCORE secret", "CONFIG_APP_OSCORE_MASTER_SECRET"),
        ("OSCORE salt",   "CONFIG_APP_OSCORE_MASTER_SALT"),
        ("OSCORE SID",    "CONFIG_APP_OSCORE_SENDER_ID"),
        ("OSCORE RID",    "CONFIG_APP_OSCORE_RECIPIENT_ID"),
        ("DTLS identity", "CONFIG_APP_DTLS_PSK_IDENTITY"),
        ("DTLS PSK",      "CONFIG_APP_DTLS_PSK_HEX"),
    ]:
        if key == "CONFIG_APP_COAP_SECURITY_":
            m = re.search(
                r"^CONFIG_APP_COAP_SECURITY_(\w+)=y",
                sec.read_text() if sec.exists() else "", re.M,
            )
            val = m.group(1).lower() if m else ""
        else:
            val = _read_kconfig_value(sec, key)
        if val:
            lines.append(f"  {label:<14} {val}")
    log.log(MS.BLE_READY)
    log.log("BLE ready — security context:")
    log.box(lines)

def _print_lte_ready(line: str, log: EventLog):
    log.log("LTE ready — security config received from BLE:")
    log.box(["LTE READY", "", f"  {line.strip()}"])

def _print_server_ready(log: EventLog):
    env   = _ssh_read_env()
    lines = ["SERVER READY", ""]
    for k in ("SECURITY_MODE", "DTLS_PSK_IDENTITY", "DTLS_PSK_KEY_HEX", "OSCORE_CONTEXT_DIR"):
        v = env.get(k, "")
        if v:
            lines.append(f"  {k:<22} {v}")
    log.log("Server ready — context:")
    log.box(lines)


# ── Config setup ──────────────────────────────────────────────────────────────

def _setup_configs(ble_mode: str, coap_mode: str, wrong_flags: frozenset, log: EventLog):
    _set_kconfig_mode(
        _SENS_APP / "security.conf",
        "CONFIG_APP_BLE_SECURITY_",
        f"CONFIG_APP_BLE_SECURITY_{ble_mode.upper()}=y",
    )
    log.log(f"  sensor security.conf: BLE_SECURITY_{ble_mode.upper()}")

    _set_kconfig_mode(
        _BLE_APP / "security.conf",
        "CONFIG_APP_COAP_SECURITY_",
        f"CONFIG_APP_COAP_SECURITY_{coap_mode.upper()}=y",
    )
    flags = _HUB_BLE_RELAY_FLAGS.get(ble_mode, {})
    for flag in ("CONFIG_APP_SENSOR_RELAY_BROADCAST", "CONFIG_APP_SENSOR_RELAY_OSCORE"):
        _set_kconfig_value(_BLE_APP / "security.conf", flag, flags.get(flag, False))
    log.log(f"  hub BLE security.conf: COAP_SECURITY_{coap_mode.upper()}, relay={list(flags)}")

    if "wrong_oscore_sensor" in wrong_flags:
        _update_kconfig_key(_SENS_APP / "security.conf",
                            'CONFIG_APP_OSCORE_MASTER_SECRET="deadbeefdeadbeefdeadbeefdeadbeef"')
        log.log("  [WRONG] sensor OSCORE secret corrupted")
    if "wrong_oscore_hub" in wrong_flags:
        _update_kconfig_key(_BLE_APP / "security.conf",
                            'CONFIG_APP_OSCORE_MASTER_SECRET="deadbeefdeadbeefdeadbeefdeadbeef"')
        log.log("  [WRONG] hub OSCORE secret corrupted")
    if "wrong_dtls" in wrong_flags:
        _update_kconfig_key(_BLE_APP / "security.conf",
                            'CONFIG_APP_DTLS_PSK_HEX="deadbeefdeadbeefdeadbeefdeadbeef"')
        log.log("  [WRONG] hub DTLS PSK corrupted")


# ── Core scenario runner ──────────────────────────────────────────────────────

def run_scenario(ble_mode: str, coap_mode: str, wrong_flags: frozenset,
                 log: EventLog, session: "PrebuildSession | None" = None,
                 suite_path: Path | None = None, idx: int | None = None) -> bool:
    """
    Headless test runner.  Ports are only opened briefly for commands.
    For live log visibility, run alongside the logviewer.
    """
    label = f"{ble_mode} + {coap_mode}"
    if wrong_flags:
        label += f"  [{', '.join(wrong_flags)}]"

    t_start = time.time()
    run_dir = RunDir(ble_mode, coap_mode, wrong_flags, suite_path=suite_path, idx=idx)
    log.log(f"Results → testruns/{run_dir.path.name}")

    # Validate pairing constraint
    if (ble_mode in _OSCORE_BLE_MODES) and (coap_mode not in _OSCORE_COAP_MODES):
        print(f"ERROR: invalid pairing — {ble_mode} requires an OSCORE CoAP mode")
        return False

    # Write configs (including any key corruption for negative tests)
    log.log(MS.T_WRITING_CONFIGS)
    _setup_configs(ble_mode, coap_mode, wrong_flags, log)

    # Snapshot configs to run dir
    for src, dst in [
        (_BLE_APP  / "security.conf", "ble_security.conf"),
        (_SENS_APP / "security.conf", "sensor_security.conf"),
    ]:
        if src.exists():
            run_dir.config_path(dst).write_bytes(src.read_bytes())

    # Find ports (brief use only — logviewer may also be open)
    try:
        ble_port, lte_port = find_thingy91x_ports()
    except Exception as e:
        log.log(f"ERROR: Thingy:91X ports not found: {e}")
        return False
    sensor_port: str | None = None
    try:
        sensor_port = find_thingy53_port()
    except Exception:
        log.log("WARNING: Thingy:53 port not found — sensor sampling will be skipped")

    # Update server env before restart
    _ssh_update_env("SECURITY_MODE", coap_mode)

    # ── Phase 1: Reset LTE + build/wait + server restart ─────────────────

    log.log(MS.T_RESETTING_LTE)
    _send_brief(ble_port, "reset lte", log)

    use_prebuild = (
        session is not None
        and not wrong_flags
        and ble_mode in session.sensor_dirs
        and _ble_variant_key(ble_mode, coap_mode) in session.ble_dirs
    )

    if use_prebuild:
        server_ok: list[bool] = [False]

        def _do_restart_server_pb():
            server_ok[0] = _ssh_restart_server(log)

        t_srv = threading.Thread(target=_do_restart_server_pb, daemon=True)
        t_srv.start()

        log.log(f"Waiting {_WAIT_LTE_REBOOT} s for LTE reboot…")
        time.sleep(_WAIT_LTE_REBOOT)

        sensor_status = session.wait_for_sensor(ble_mode)
        ble_status    = session.wait_for_ble(ble_mode, coap_mode)
        t_srv.join()

        if sensor_status != "done":
            log.log(f"Sensor prebuild {sensor_status} — aborting")
            return False
        if ble_status != "done":
            log.log(f"BLE prebuild {ble_status} — aborting")
            return False
        if not server_ok[0]:
            log.log("Server restart FAILED — aborting")
            return False
    else:
        build_ok: dict[str, bool] = {}

        def _do_build_ble():
            build_ok["ble"] = _build("ble", log, pristine=True)

        def _do_build_sensor():
            build_ok["sensor"] = _build("sensor", log, pristine=True)

        def _do_restart_server():
            build_ok["server"] = _ssh_restart_server(log)

        build_threads = [
            threading.Thread(target=_do_build_ble,      daemon=True),
            threading.Thread(target=_do_build_sensor,   daemon=True),
            threading.Thread(target=_do_restart_server, daemon=True),
        ]
        for t in build_threads:
            t.start()

        log.log(f"Waiting {_WAIT_LTE_REBOOT} s for LTE reboot while builds run…")
        time.sleep(_WAIT_LTE_REBOOT)

        for t in build_threads:
            t.join()

        if not build_ok.get("ble"):
            log.log("BLE build FAILED — aborting")
            return False
        if not build_ok.get("server"):
            log.log("Server restart FAILED — aborting")
            return False
        if not build_ok.get("sensor"):
            log.log("Sensor build FAILED — aborting")
            return False

    # ── Phase 2: Flash (sensor + BLE in parallel — different programmers) ────

    flash_ok: dict[str, bool] = {}

    if use_prebuild:
        sensor_dir = session.sensor_dirs[ble_mode]
        ble_dir    = session.ble_dirs[_ble_variant_key(ble_mode, coap_mode)]

        def _do_flash_sensor_pb():
            flash_ok["sensor"] = _flash_from_dir("sensor", sensor_dir, log) if sensor_port else True

        def _do_flash_ble_pb():
            flash_ok["ble"] = _flash_from_dir("ble", ble_dir, log)

        ts = threading.Thread(target=_do_flash_sensor_pb, daemon=True)
        tb = threading.Thread(target=_do_flash_ble_pb,    daemon=True)
        ts.start(); tb.start()
        ts.join();  tb.join()
    else:
        def _do_flash_sensor_live():
            flash_ok["sensor"] = _flash("sensor", log) if sensor_port else True

        def _do_flash_ble_live():
            flash_ok["ble"] = _flash("ble", log)

        ts = threading.Thread(target=_do_flash_sensor_live, daemon=True)
        tb = threading.Thread(target=_do_flash_ble_live,    daemon=True)
        ts.start(); tb.start()
        ts.join();  tb.join()

    if not flash_ok.get("sensor", True):
        log.log("Sensor flash FAILED — aborting")
        return False
    if not flash_ok.get("ble"):
        log.log("BLE flash FAILED — aborting")
        return False

    _print_ble_ready(ble_mode, coap_mode, log)

    # ── Phase 3: Wait for LTE to connect (event-driven, up to 90 s) ─────────
    log.log("Waiting for LTE: Connected to Cloud (up to 90 s)…")
    lte_up = _wait_lte_patterns(lte_port, [
        ("Connected to Cloud|Custom CoAP connection successful", 90),
    ], log)
    if lte_up.get("Connected to Cloud|Custom CoAP connection successful"):
        log.log("LTE: connected to cloud")
    else:
        log.log("WARNING: LTE cloud connection not seen within 90 s (continuing)")

    _print_server_ready(log)

    env = _ssh_read_env()
    run_dir.config_path("server_env.txt").write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(env.items())), encoding="utf-8"
    )

    # ── Phase 4: Trigger location + sampling ─────────────────────────────
    # Watch LTE UART for milestone strings emitted by the firmware.
    # OSCORE BLE scenarios (4-5): relay queued → relay ACK → location fix → location ACK
    # Non-OSCORE scenarios  (1-3): location fix → location ACK (no relay)

    if ble_port:
        log.log("[BLE] Sending att_location search")
        _send_brief(ble_port, "att_location search", log)
    else:
        log.log("[BLE] No port — skipping att_location search")

    if sensor_port:
        log.log("[Sensor] Sending att_sample")
        _send_brief(sensor_port, "att_sample", log)
    else:
        log.log("[Sensor] No port — skipping att_sample")

    has_relay = ble_mode in _OSCORE_BLE_MODES

    patterns = [
        (MS.LTE_LOCATION_FIX_WIFI, 30),   # location fix returned
        (MS.LTE_ACK_RECEIVED,      60),   # location ACK received
    ]
    if has_relay:
        patterns = [
            (MS.LTE_SAMPLE_RECEIVED, 30),   # OSCORE relay queued at hub
            (MS.LTE_RELAY_ACK,       60),   # sensor relay ACK from server
        ] + patterns

    lte_results = _wait_lte_patterns(lte_port, patterns, log)

    relay_queued  = lte_results.get(MS.LTE_SAMPLE_RECEIVED, True)
    relay_ok      = lte_results.get(MS.LTE_RELAY_ACK,       True)
    location_seen = lte_results[MS.LTE_LOCATION_FIX_WIFI]
    coap_ok       = lte_results[MS.LTE_ACK_RECEIVED]

    if has_relay:
        log.log(f"[LTE] OSCORE relay queued:  {'✓' if relay_queued  else '✗ timeout'}")
        log.log(f"[LTE] OSCORE relay ACK:     {'✓' if relay_ok      else '✗ timeout'}")
    log.log(f"[LTE] Location fix Wi-Fi:   {'✓' if location_seen else '✗ timeout'}")
    log.log(f"[LTE] CoAP ACK received:    {'✓' if coap_ok       else '✗ timeout'}")

    # ── Phase 5: Capture server logs for the record ───────────────────────

    elapsed = int(time.time() - t_start) + 5
    r = _ssh(
        f"cd tracker-server && docker compose logs --since {elapsed}s coap-server 2>&1",
        timeout=15,
    )
    server_log = r.stdout.strip()
    run_dir.log_path("server.txt").write_text(server_log, encoding="utf-8")

    # ── Determine PASS/FAIL ───────────────────────────────────────────────

    # Server-side backstop: if OSCORE replay protection rejected the sensor
    # payload, the server logs ReplayErrorWithEcho — count as relay failure.
    if has_relay and "ReplayErrorWithEcho" in server_log:
        log.log(f"{MS.SERVER_ERROR_OSCORE_REPLAY} (ReplayErrorWithEcho in server log)")
        relay_ok = False

    if wrong_flags:
        passed = True
        log.log("[Test] MANUAL CHECK — negative test: verify server logs show expected failure")
    else:
        passed = relay_queued and relay_ok and location_seen and coap_ok

    if passed:
        log.log(f"{MS.T_PASS} {label}")
    else:
        reasons = []
        if not relay_queued:
            reasons.append("relay not queued (no BLE sample)")
        if not relay_ok:
            reasons.append("relay ACK timeout")
        if not location_seen:
            reasons.append("location timeout")
        if not coap_ok:
            reasons.append("CoAP timeout")
        log.log(f"{MS.T_FAIL} {label} — {', '.join(reasons) or 'unknown'}")

    run_dir.write_info({
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
        "ble_mode":        ble_mode,
        "coap_mode":       coap_mode,
        "wrong_flags":     list(wrong_flags),
        "result":          "PASS" if passed else "FAIL",
        "elapsed_seconds": round(time.time() - t_start, 1),
    })
    run_dir.write_timeline(log)

    return passed


# ── Argument parsing ──────────────────────────────────────────────────────────

_ROTATION_FLAGS = {
    "--rotate-dtls":          "dtls",
    "--rotate-oscore-hub":    "oscore_hub",
    "--rotate-oscore-sensor": "oscore_sensor",
    "--rotate-oscore-both":   "oscore_both",
}
_WRONG_FLAGS = {
    "--wrong-dtls":          "wrong_dtls",
    "--wrong-oscore-hub":    "wrong_oscore_hub",
    "--wrong-oscore-sensor": "wrong_oscore_sensor",
}

def _parse_args(argv: list[str]) -> list[tuple]:
    """Returns list of (ble_mode, coap_mode, wrong_flags, rotate_flags) tuples."""
    scenarios: list[tuple] = []
    pending_wrong:  set[str] = set()
    pending_rotate: set[str] = set()
    current_ble: str | None  = None

    for token in argv:
        if token in _ROTATION_FLAGS:
            pending_rotate.add(_ROTATION_FLAGS[token])
        elif token in _WRONG_FLAGS:
            pending_wrong.add(_WRONG_FLAGS[token])
        elif token in _BLE_CODES:
            current_ble = _BLE_CODES[token]
        elif token in _COAP_CODES:
            if current_ble is None:
                print(f"ERROR: CoAP code '{token}' without preceding BLE code")
                sys.exit(1)
            scenarios.append((
                current_ble, _COAP_CODES[token],
                frozenset(pending_wrong), frozenset(pending_rotate),
            ))
            current_ble    = None
            pending_wrong  = set()
            pending_rotate = set()
        else:
            print(f"ERROR: unknown token '{token}'")
            sys.exit(1)

    if current_ble is not None:
        print(f"ERROR: BLE code '{current_ble}' has no matching CoAP code")
        sys.exit(1)
    return scenarios


# ── Entry point ───────────────────────────────────────────────────────────────

def _load_suite(path: str) -> list[tuple]:
    """Load scenarios from a JSON suite file. Returns (ble, coap, wrong, rotate) tuples."""
    import json
    with open(path) as f:
        suite = json.load(f)
    scenarios = []
    for s in suite.get("scenarios", []):
        if s.get("skip", False):
            continue
        ble    = s["ble"]
        coap   = s["coap"]
        wrong  = frozenset(s.get("wrong",  []))
        rotate = frozenset(s.get("rotate", []))
        scenarios.append((ble, coap, wrong, rotate))
    return scenarios


def main():
    args = sys.argv[1:]

    if args and args[0] == "--suite":
        if len(args) < 2:
            print("ERROR: --suite requires a file path")
            sys.exit(1)
        scenarios  = _load_suite(args[1])
        suite_name = Path(args[1]).stem
        if not scenarios:
            print("No scenarios in suite file")
            sys.exit(1)
    elif len(args) < 2:
        print(__doc__)
        sys.exit(1)
    else:
        scenarios  = _parse_args(args)
        suite_name = "single"
    if not scenarios:
        print("No valid scenario pairs found in arguments")
        sys.exit(1)

    total      = len(scenarios)
    ts         = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suite_path = _TESTRUNS_DIR / f"{ts}_suite-{suite_name}"
    suite_path.mkdir(parents=True, exist_ok=True)

    live_file = suite_path / "milestone_live.txt"
    live_file.write_text(
        f"[Suite] {total}  {ts}  {suite_name}\n", encoding="utf-8"
    )

    subprocess.Popen(
        [sys.executable, str(_CORE / "milestone_window.py"), str(live_file)],
        start_new_session=True,
    )

    log        = EventLog(live_path=live_file)
    all_passed = True
    suite_t0   = time.time()
    results: list[dict] = []

    log.log(f"[Test] Suite started: {total} scenario{'s' if total != 1 else ''}")

    # Kick off all prebuilds in background threads so firmware is ready by the
    # time each scenario reaches its flash step.  Scenarios that involve key
    # rotation are excluded — their config can't be staged ahead of time.
    prebuild_scenarios = [
        {"ble": ble, "coap": coap, "rotate": list(rotate)}
        for ble, coap, _wrong, rotate in scenarios
    ]
    prebuild_session = prebuild_all(prebuild_scenarios, log)

    for idx, (ble_mode, coap_mode, wrong_flags, rotate_flags) in enumerate(scenarios, 1):
        log.log(f"{MS.T_SCENARIO} {idx}/{total}: {ble_mode} + {coap_mode}")
        if "dtls" in rotate_flags:
            _rotate_dtls_keys(log)
        oscore_targets = []
        if "oscore_hub"    in rotate_flags or "oscore_both" in rotate_flags:
            oscore_targets.append("hub")
        if "oscore_sensor" in rotate_flags or "oscore_both" in rotate_flags:
            oscore_targets.append("sensor")
        if oscore_targets:
            _rotate_oscore_keys(oscore_targets, log)

        sc_t0  = time.time()
        passed = run_scenario(ble_mode, coap_mode, wrong_flags, log, prebuild_session,
                              suite_path=suite_path, idx=idx)
        sc_elapsed = round(time.time() - sc_t0, 1)
        all_passed = all_passed and passed

        suffix = ("_" + "_".join(sorted(wrong_flags))) if wrong_flags else ""
        results.append({
            "index":           idx,
            "ble":             ble_mode,
            "coap":            coap_mode,
            "result":          "PASS" if passed else "FAIL",
            "elapsed_seconds": sc_elapsed,
            "dir":             f"{idx:02d}_b-{ble_mode}_c-{coap_mode}{suffix}",
        })

    log.close()

    (suite_path / "suite_summary.json").write_text(
        json.dumps({
            "timestamp":       datetime.now().isoformat(timespec="seconds"),
            "suite_name":      suite_name,
            "total":           total,
            "passed":          sum(1 for r in results if r["result"] == "PASS"),
            "failed":          sum(1 for r in results if r["result"] == "FAIL"),
            "elapsed_seconds": round(time.time() - suite_t0, 1),
            "scenarios":       results,
        }, indent=2),
        encoding="utf-8",
    )

    log.report()
    print("\n" + "=" * 64)
    print(f"OVERALL: {'✓ ALL PASSED' if all_passed else '✗ SOME FAILED'}")
    print(f"Results: {suite_path}")
    print("=" * 64)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
