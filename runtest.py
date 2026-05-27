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

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial  # pyserial — only used for brief command sends

# ── Module resolution ─────────────────────────────────────────────────────────

_HERE = Path(__file__).parent.resolve()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from build_config import (
    TARGETS, NRFUTIL_WRAP, _BLE_APP, _SENS_APP, _effective_build_cmd,
)
from serial_io import find_thingy91x_ports, find_thingy53_port
from server_io import SSH_SERVER_HOST
from ui_constants import _OSCORE_BLE_MODES, _OSCORE_COAP_MODES, _HUB_BLE_RELAY_FLAGS

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

_STATE_FILE   = _HERE / ".runtest_state.json"
_TESTRUNS_DIR = _HERE / "testruns"

# Timing constants (seconds)
_WAIT_LTE_REBOOT   = 5    # after reset lte: brief pause before starting builds
_WAIT_LTE_READY    = 120  # after BLE flash: time for BLE boot → security_config → LTE network attach
_WAIT_SAMPLE       = 15   # after att_sample: time for CoAP POST to reach server

# ── Event log ─────────────────────────────────────────────────────────────────

class EventLog:
    def __init__(self):
        self._t0    = time.time()
        self._lines: list[str] = []

    def log(self, msg: str):
        elapsed = time.time() - self._t0
        entry   = f"[T+{elapsed:6.1f}s] {msg}"
        print(entry, flush=True)
        self._lines.append(entry)

    def box(self, lines: list[str]):
        width  = max((len(l) for l in lines), default=4) + 4
        border = "─" * width
        print(f"          ┌{border}┐")
        for line in lines:
            pad = width - len(line) - 2
            print(f"          │ {line}{' ' * pad} │")
        print(f"          └{border}┘", flush=True)
        self._lines.extend([f"          {l}" for l in lines])

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
    """Per-run output directory under testruns/."""
    def __init__(self, ble_mode: str, coap_mode: str, wrong_flags: frozenset):
        ts     = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        suffix = ("_" + "_".join(sorted(wrong_flags))) if wrong_flags else ""
        name   = f"{ts}_b-{ble_mode}_c-{coap_mode}{suffix}"
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


# ── Kconfig helpers ───────────────────────────────────────────────────────────

def _conf_hash(*paths: Path) -> str:
    h = hashlib.md5()
    for p in paths:
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()

def _set_kconfig_mode(path: Path, prefix: str, new_line: str):
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(prefix)}\w+=y", re.M)
    text = pat.sub(new_line, text) if pat.search(text) else (text.rstrip("\n") + "\n" + new_line + "\n")
    path.write_text(text)

def _set_kconfig_value(path: Path, key: str, value: bool):
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(key)}=.*\n?", re.M)
    text = pat.sub("", text)
    # Always write explicit state so prj.conf defaults are overridden.
    text = text.rstrip("\n") + f"\n{key}={'y' if value else 'n'}\n"
    path.write_text(text)

def _update_kconfig_key(path: Path, line: str):
    key  = line.split("=")[0]
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(key)}=.*", re.M)
    text = pat.sub(line, text) if pat.search(text) else (text.rstrip("\n") + "\n" + line + "\n")
    path.write_text(text)

def _read_kconfig_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    m = re.search(rf'^{re.escape(key)}="?(.*?)"?\s*$', path.read_text(), re.M)
    return m.group(1) if m else ""


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", SSH_SERVER_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )

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
    log.log("Server: restarting coap-server...")
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
    log.log(f"Server: restart {'OK' if ok else 'FAILED'}")
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

def _build(key: str, log: EventLog, pristine: bool = True) -> bool:
    tgt = TARGETS[key]
    cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
    if pristine:
        wi  = cmd.index("west")
        cmd = cmd[:wi + 2] + ["--pristine"] + cmd[wi + 2:]
    label = tgt["label"]
    log.log(f"{label}: {'pristine ' if pristine else ''}build started")
    result = subprocess.run(cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(f"{label}: build {'OK' if ok else 'FAILED'}")
    return ok

def _flash(key: str, log: EventLog) -> bool:
    tgt = TARGETS[key]
    for pre_cmd in tgt.get("pre_flash_cmds", []):
        subprocess.run(pre_cmd, capture_output=True)
    cmd   = NRFUTIL_WRAP + tgt["flash_cmd"]
    label = tgt["label"]
    log.log(f"{label}: flashing...")
    result = subprocess.run(cmd, cwd=tgt["cwd"])
    ok = result.returncode == 0
    log.log(f"{label}: flash {'OK' if ok else 'FAILED'}")
    return ok


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


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text()) if _STATE_FILE.exists() else {}
    except Exception:
        return {}

def _save_state(state: dict):
    _STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Core scenario runner ──────────────────────────────────────────────────────

def run_scenario(ble_mode: str, coap_mode: str, wrong_flags: frozenset,
                 log: EventLog) -> bool:
    """
    Headless test runner.  Ports are only opened briefly for commands.
    For live log visibility, run alongside the logviewer.
    """
    print("\n" + "=" * 64)
    label = f"{ble_mode} + {coap_mode}"
    if wrong_flags:
        label += f"  [{', '.join(wrong_flags)}]"
    log.log(f"TEST: {label}")
    print("=" * 64)

    t_start = time.time()
    run_dir = RunDir(ble_mode, coap_mode, wrong_flags)
    log.log(f"Results → testruns/{run_dir.path.name}")

    # Validate pairing constraint
    if (ble_mode in _OSCORE_BLE_MODES) != (coap_mode in _OSCORE_COAP_MODES):
        print(f"ERROR: invalid pairing — {ble_mode} requires "
              f"{'an OSCORE' if ble_mode in _OSCORE_BLE_MODES else 'a non-OSCORE'} CoAP mode")
        return False

    # Load state for smart sensor rebuild
    state           = _load_state()
    last_ble        = state.get("last_ble", "")
    sensor_hash_old = state.get("sensor_conf_hash", "")

    # Write configs (including any key corruption for negative tests)
    log.log("Setting up config files...")
    _setup_configs(ble_mode, coap_mode, wrong_flags, log)
    sensor_hash_new = _conf_hash(_SENS_APP / "security.conf")
    rebuild_sensor  = (ble_mode != last_ble) or (sensor_hash_new != sensor_hash_old)

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

    # ── Phase 1: Reset LTE + parallel build + server restart ──────────────

    log.log("BLE→LTE: sending reset lte (brief port open)")
    _send_brief(ble_port, "reset lte", log)

    build_ok: dict[str, bool] = {}

    def _do_build_ble():
        build_ok["ble"] = _build("ble", log, pristine=True)

    def _do_build_sensor():
        build_ok["sensor"] = _build("sensor", log, pristine=True)

    def _do_restart_server():
        build_ok["server"] = _ssh_restart_server(log)

    build_threads = [threading.Thread(target=_do_build_ble, daemon=True)]
    if rebuild_sensor:
        log.log(f"Sensor: rebuild needed (BLE mode {last_ble!r} → {ble_mode!r} or config changed)")
        build_threads.append(threading.Thread(target=_do_build_sensor, daemon=True))
    else:
        build_ok["sensor"] = True
        log.log("Sensor: skipping rebuild (mode and config unchanged)")
    build_threads.append(threading.Thread(target=_do_restart_server, daemon=True))

    for t in build_threads:
        t.start()

    log.log(f"Waiting {_WAIT_LTE_REBOOT} s for LTE reboot while builds run…")
    time.sleep(_WAIT_LTE_REBOOT)

    for t in build_threads:
        t.join()

    if not build_ok.get("ble"):
        log.log("BLE build FAILED — aborting")
        return False
    if not build_ok.get("sensor"):
        log.log("Sensor build FAILED — aborting")
        return False

    # ── Phase 2: Flash ────────────────────────────────────────────────────

    if rebuild_sensor and sensor_port:
        if not _flash("sensor", log):
            log.log("Sensor flash FAILED — aborting")
            return False

    if not _flash("ble", log):
        log.log("BLE flash FAILED — aborting")
        return False

    _print_ble_ready(ble_mode, coap_mode, log)

    # ── Phase 3: Fixed delay for device readiness ─────────────────────────

    log.log(f"Waiting {_WAIT_LTE_READY} s for BLE boot → security_config → LTE network attach…")
    log.log("(If logviewer is open, watch BLE / LTE panels for live progress)")
    time.sleep(_WAIT_LTE_READY)

    _print_server_ready(log)

    env = _ssh_read_env()
    run_dir.config_path("server_env.txt").write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(env.items())), encoding="utf-8"
    )

    # ── Phase 4: Trigger location + sampling ─────────────────────────────
    # Watch LTE serial directly — faster than SSH polling, no case-sensitivity issues.
    #   env_seen:      OSCORE modes: "OSCORE relay.*queued" (hub relays raw encrypted bytes)
    #                  plain modes:  "BLE env"              (hub decodes and logs env values)
    #   location_seen: "location: Wi-Fi" → location module started WiFi scan
    #   coap_ok:       "CoAP response"  → server acknowledged at least one payload

    if ble_port:
        log.log("BLE: sending att_location search → nRF9151 IPC")
        _send_brief(ble_port, "att_location search", log)
    else:
        log.log("BLE: no port — skipping att_location search")

    if sensor_port:
        log.log("Sensor: sending att_sample (brief port open)")
        _send_brief(sensor_port, "att_sample", log)
    else:
        log.log("Sensor: no port — skipping att_sample")

    env_pat = r"OSCORE relay.*forwarded to server" if ble_mode in _OSCORE_BLE_MODES else r"BLE env\b"

    lte_results = _wait_lte_patterns(lte_port, [
        (env_pat,             30),   # Thingy53 data at hub
        (r"location: Wi-Fi",  30),   # location module active
        (r"CoAP response",    60),   # server acknowledged payload
    ], log)

    env_seen      = lte_results[env_pat]
    location_seen = lte_results[r"location: Wi-Fi"]
    coap_ok       = lte_results[r"CoAP response"]

    log.log(f"Thingy53 env at hub: {'✓' if env_seen      else '✗ timeout'}")
    log.log(f"Location search:     {'✓' if location_seen else '✗ timeout'}")
    log.log(f"CoAP response OK:    {'✓' if coap_ok       else '✗ timeout'}")

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
    # payload, the server logs ReplayErrorWithEcho even though the hub gets 2.04
    # for the outer JSON batch.  Count as env failure.
    if ble_mode in _OSCORE_BLE_MODES and "ReplayErrorWithEcho" in server_log:
        log.log("WARNING: Server rejected OSCORE payload (ReplayErrorWithEcho) — sensor data not stored")
        env_seen = False

    if wrong_flags:
        passed = True
        log.log("MANUAL CHECK required — negative test: verify server logs show expected failure")
    else:
        passed = env_seen and location_seen and coap_ok

    _save_state({
        "last_ble":         ble_mode,
        "last_coap":        coap_mode,
        "sensor_conf_hash": _conf_hash(_SENS_APP / "security.conf"),
        "ble_conf_hash":    _conf_hash(_BLE_APP  / "security.conf"),
    })

    status = "✓ PASS" if passed else "✗ FAIL"
    log.log(f"{status}  ({label})")

    run_dir.write_info({
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
        "ble_mode":        ble_mode,
        "coap_mode":       coap_mode,
        "wrong_flags":     list(wrong_flags),
        "result":          "PASS" if passed else "FAIL",
        "elapsed_seconds": round(time.time() - t_start, 1),
        "rebuild_sensor":  rebuild_sensor,
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
        scenarios = _load_suite(args[1])
        if not scenarios:
            print("No scenarios in suite file")
            sys.exit(1)
    elif len(args) < 2:
        print(__doc__)
        sys.exit(1)
    else:
        scenarios = _parse_args(args)
    if not scenarios:
        print("No valid scenario pairs found in arguments")
        sys.exit(1)

    log        = EventLog()
    all_passed = True

    for ble_mode, coap_mode, wrong_flags, rotate_flags in scenarios:
        if "dtls" in rotate_flags:
            _rotate_dtls_keys(log)
        oscore_targets = []
        if "oscore_hub"    in rotate_flags or "oscore_both" in rotate_flags:
            oscore_targets.append("hub")
        if "oscore_sensor" in rotate_flags or "oscore_both" in rotate_flags:
            oscore_targets.append("sensor")
        if oscore_targets:
            _rotate_oscore_keys(oscore_targets, log)

        passed     = run_scenario(ble_mode, coap_mode, wrong_flags, log)
        all_passed = all_passed and passed

    log.report()
    print("\n" + "=" * 64)
    print(f"OVERALL: {'✓ ALL PASSED' if all_passed else '✗ SOME FAILED'}")
    print("=" * 64)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
