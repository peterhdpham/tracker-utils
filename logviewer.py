#!/usr/bin/env python3
"""
logviewer.py — tracker project dev console.

Features:
  • 3-panel log viewer: BLE (if00), LTE (if02), Thingy:53 RTT
  • Auto-spawns JLinkGDBServer for Thingy:53 RTT (logs its output to Thingy53 panel)
  • Per-device: Build, Build Pristine, Flash, Build & Flash
  • CSV recording, clipboard copy

Usage:
    python3 tracker-utils/logviewer.py        # from tracker_project/ or tracker-utils/

Dependencies:
    pip install pyserial
"""

import argparse
import csv
import glob
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

import serial

def _dbg(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[logviewer] {ts}  {msg}", file=sys.stderr, flush=True)


# ── Workspace paths ─────────────────────────────────────────────────────────────
#
# Matches the WORKSPACE variable in ble.sh / lte.sh / sensor.sh:
#   WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Since this script lives at tracker_project/tracker-utils/, parent = tracker_project/.

_HERE      = Path(__file__).resolve().parent   # tracker_project/tracker-utils/
_WORKSPACE = _HERE.parent                      # tracker_project/

# ── NCS toolchain version ────────────────────────────────────────────────────────
NCS_VERSION = "v3.1.1"
NRFUTIL_WRAP = [
    "nrfutil", "sdk-manager", "toolchain", "launch",
    "--ncs-version", NCS_VERSION, "--",
]

# ── J-Link server definitions ───────────────────────────────────────────────────

JLINK_SERVERS = [
    # All three devices now log via USB CDC-ACM (serial_reader), not RTT.
]

# ── Per-target build / flash definitions ────────────────────────────────────────
#
# build_cmd / flash_cmd are bare west args — wrapped with NRFUTIL_WRAP at runtime.
# pre_flash_cmds (optional): raw commands run WITHOUT nrfutil wrap before flash.
# Matches ble.sh / lte.sh / sensor.sh exactly.

_LTE_APP   = _WORKSPACE / "tracker-hub" / "apps" / "tracker-hub-lte"
_BLE_APP   = _WORKSPACE / "tracker-hub" / "apps" / "tracker-hub-ble"
_SENS_APP  = _WORKSPACE / "tracker-sensor-node" / "tracker-node"
_LTE_BLD   = _WORKSPACE / "build" / "lte"
_BLE_BLD   = _WORKSPACE / "build" / "ble"
_SENS_BLD  = _WORKSPACE / "build" / "sensor"

TARGETS = {
    "lte": {
        "label":   "LTE",
        "panels":  ["LTE"],
        "snr":     "1051217937",
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy91x/nrf9151/ns",
            "--build-dir", str(_LTE_BLD),
            str(_LTE_APP),
            "--sysbuild",
        ],
        "optional_conf": _LTE_APP / "local.conf",
        "security_conf_files": [_LTE_APP / "oscore.conf", _LTE_APP / "dtls.conf"],
        "flash_cmd": [
            "west", "flash",
            "--recover",
            "--build-dir", str(_LTE_BLD),
            "--snr", "1051217937",
        ],
    },
    "ble": {
        "label":   "BLE",
        "panels":  ["BLE"],
        "snr":     "1051217937",
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy91x/nrf5340/cpuapp",
            "--build-dir", str(_BLE_BLD),
            str(_BLE_APP),
            "--sysbuild",
        ],
        "optional_conf": _BLE_APP / "local.conf",
        # Recover Application core before programming (clears ERASEPROTECT/APPROTECT).
        # Network core recovery is skipped — the Thingy:91X Debug In connector does
        # not expose the nRF5340 Network core SWD pins separately.
        "pre_flash_cmds": [
            ["nrfutil", "device", "recover",
             "--serial-number", "1051217937", "--core", "Application"],
        ],
        "flash_cmd": [
            "west", "flash",
            "--recover",
            "--build-dir", str(_BLE_BLD),
            "--snr", "1051217937",
        ],
    },
    "sensor": {
        "label":   "Sensor",
        "panels":  ["Thingy53"],
        "snr":     "1050065248",
        "cwd":     str(_WORKSPACE),
        "build_cmd": [
            "west", "build",
            "-b", "thingy53/nrf5340/cpuapp",
            "--build-dir", str(_SENS_BLD),
            str(_SENS_APP),
        ],
        "optional_conf": _SENS_APP / "local.conf",
        "security_conf_files": [_SENS_APP / "oscore.conf"],
        "flash_cmd": [
            "west", "flash",
            "--build-dir", str(_SENS_BLD),
            "--snr", "1050065248",
        ],
    },
}


def _effective_build_cmd(tgt: dict) -> list:
    cmd = list(tgt["build_cmd"])
    conf_files = []
    main_conf = tgt.get("optional_conf")
    if main_conf and Path(main_conf).exists():
        conf_files.append(str(main_conf))
    for sc in tgt.get("security_conf_files", []):
        if Path(sc).exists():
            conf_files.append(str(sc))
    if conf_files:
        cmd += ["--", f"-DEXTRA_CONF_FILE={';'.join(conf_files)}"]
    return cmd

# ── Display ──────────────────────────────────────────────────────────────────────

BAUD    = 115200
SOURCES         = ["BLE", "LTE", "Thingy53", "Server"]
_DEVICE_SOURCES = ["BLE", "LTE", "Thingy53"]

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

_ANSI_RE     = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|[A-Za-z])")
_PROMPT_RE   = re.compile(r"^(?:uart:~\$\s*)+")
_LOG_LEVEL_RE = re.compile(r"<(dbg|inf|err|wrn)>")
_UI_          = "__ui__"   # sentinel: msg is a callable, run on main thread

LOG_LEVEL_COLOR = {
    "dbg": "#79c0ff",   # blue
    "inf": "#e6edf3",   # near-white
    "err": "#ff7b72",   # red
    "wrn": "#e3b341",   # amber
}


SSH_SERVER_HOST = "root@10.10.10.10"
SSH_SERVER_CMD  = "cd tracker-server && docker compose logs -f"

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


def strip_ansi(s: str) -> str:
    return _PROMPT_RE.sub("", _ANSI_RE.sub("", s))


def _dev_tag(msg: str) -> str:
    m = _LOG_LEVEL_RE.search(msg)
    return m.group(1) if m else "msg"


# ── Port auto-detection ─────────────────────────────────────────────────────────

def find_thingy91x_ports():
    pattern = "/dev/serial/by-id/usb-Nordic_Semiconductor_Thingy*91*"
    ports   = sorted(glob.glob(pattern))
    if00    = next((p for p in ports if p.endswith("-if00")), None)
    if02    = next((p for p in ports if p.endswith("-if02")), None)
    return if00, if02


def find_thingy53_port():
    # Thingy:53 CDC-ACM may enumerate as either a board-named or generic Zephyr device.
    for pattern in ("/dev/serial/by-id/*Thingy*53*",
                    "/dev/serial/by-id/*Zephyr*CDC_ACM*"):
        ports = sorted(glob.glob(pattern))
        port  = next((p for p in ports if p.endswith("-if00")), None)
        if port:
            return port
    return None


# ── J-Link process management ───────────────────────────────────────────────────

def _find_jlink_server() -> str | None:
    for name in ("JLinkGDBServerCL", "JLinkGDBServer", "JLinkGDBServerExe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _pipe_jlink_stderr(srv: dict, proc: subprocess.Popen, q: queue.Queue):
    """Forward JLinkGDBServer stderr lines to the panel in real time."""
    if proc.stderr:
        for raw in proc.stderr:
            line = strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
            if line:
                q.put((srv["label"], time.time(), f"  [jlink] {line}", "build"))
    # Process exited — log the return code
    q.put((srv["label"], time.time(),
           f"[JLinkGDBServer exited (code {proc.returncode})]", "build"))


def spawn_jlink_servers(servers: list[dict], q: queue.Queue) -> list[subprocess.Popen]:
    _dbg(f"spawn_jlink_servers: starting {len(servers)} server(s)")
    exe = _find_jlink_server()
    if not exe:
        _dbg("spawn_jlink_servers: JLinkGDBServer not found in PATH")
        for srv in servers:
            q.put((srv["label"], time.time(),
                   "[JLinkGDBServer not found in PATH — RTT unavailable]", "build"))
        return []
    procs = []
    for srv in servers:
        cmd = [
            exe,
            "-device",        srv["device"],
            "-if",            "SWD",
            "-speed",         "4000",
            "-select",        f"USB={srv['serial']}",   # GDBServer flag (not -SelectEmuBySN)
            "-port",          str(srv["gdb_port"]),
            "-rtttelnetport", str(srv["rtt_port"]),
            "-nogui",
            "-autoconnect",   "1",   # suppress probe-selector dialog with multiple J-Links
        ]
        q.put((srv["label"], time.time(),
               f"[spawning: {' '.join(cmd)}]", "build"))
        try:
            _dbg(f"  spawning: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)
            _dbg(f"  {srv['label']} JLinkGDBServer PID {proc.pid}")
            q.put((srv["label"], time.time(),
                   f"[JLinkGDBServer PID {proc.pid} — RTT telnet on :{srv['rtt_port']}]", "build"))
            threading.Thread(target=_pipe_jlink_stderr, args=(srv, proc, q),
                             daemon=True).start()
        except Exception as e:
            _dbg(f"  failed to spawn {srv['label']} JLinkGDBServer: {e}")
            q.put((srv["label"], time.time(),
                   f"[Failed to start JLinkGDBServer: {e}]", "build"))
    _dbg(f"spawn_jlink_servers: done, {len(procs)} started")
    return procs


def stop_jlink_servers(procs: list[subprocess.Popen]):
    _dbg(f"stop_jlink_servers: terminating {len(procs)} process(es)")
    for proc in procs:
        try:
            proc.terminate()
            _dbg(f"  terminate → PID {proc.pid}")
        except Exception as e:
            _dbg(f"  terminate PID {proc.pid} failed: {e}")
    for proc in procs:
        try:
            proc.wait(timeout=3)
            _dbg(f"  PID {proc.pid} exited (code {proc.returncode})")
        except subprocess.TimeoutExpired:
            _dbg(f"  PID {proc.pid} timeout — sending SIGKILL")
            try:
                proc.kill()
                proc.wait(timeout=2)
                _dbg(f"  PID {proc.pid} killed")
            except Exception as e:
                _dbg(f"  kill PID {proc.pid} failed: {e}")
        except Exception as e:
            _dbg(f"  wait PID {proc.pid} failed: {e}")
    _dbg("stop_jlink_servers: done")


# ── Serial reader ───────────────────────────────────────────────────────────────

def serial_reader(source: str, port: str, q: queue.Queue, stop: threading.Event,
                  quiet: threading.Event | None = None, reconnect_cb=None,
                  write_q: queue.Queue | None = None):
    _dbg(f"serial_reader [{source}]: started on {port}")
    was_connected = False
    first_ever    = True
    while not stop.is_set():
        try:
            with serial.Serial(port, BAUD, timeout=0.1) as ser:
                if not first_ever and reconnect_cb:
                    _dbg(f"serial_reader [{source}]: reconnected — triggering JLink respawn")
                    reconnect_cb()
                first_ever    = False
                was_connected = True
                _dbg(f"serial_reader [{source}]: connected")
                q.put((source, time.time(), f"[connected → {port}]", "status"))
                buf = b""
                while not stop.is_set():
                    if write_q:
                        try:
                            while True:
                                ser.write(write_q.get_nowait())
                        except queue.Empty:
                            pass
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = strip_ansi(
                            line.decode("utf-8", errors="replace").rstrip("\r"))
                        if text:
                            q.put((source, time.time(), text, "dev"))
        except serial.SerialException as e:
            if not stop.is_set():
                if was_connected:
                    _dbg(f"serial_reader [{source}]: disconnected: {e}")
                if not (quiet and quiet.is_set()) and was_connected:
                    q.put((source, time.time(), f"[disconnected: {e}]", "status"))
                was_connected = False
                time.sleep(0.05)  # fast retry — catch device reboots quickly
        except Exception as e:
            if not stop.is_set():
                if was_connected:
                    _dbg(f"serial_reader [{source}]: error: {e}")
                if not (quiet and quiet.is_set()) and was_connected:
                    q.put((source, time.time(), f"[error: {e}]", "status"))
                was_connected = False
                time.sleep(0.05)


# ── RTT reader ──────────────────────────────────────────────────────────────────

def rtt_reader(source: str, host: str, port: int,
               q: queue.Queue, stop: threading.Event,
               startup_delay: float = 2.5):
    _dbg(f"rtt_reader [{source}]: started, waiting {startup_delay}s before connecting to :{port}")
    time.sleep(startup_delay)
    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                _dbg(f"rtt_reader [{source}]: connected to {host}:{port}")
                q.put((source, time.time(), f"[RTT connected → {host}:{port}]", "status"))
                sock.settimeout(1.0)
                buf = b""
                while not stop.is_set():
                    try:
                        chunk = sock.recv(1024)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = strip_ansi(
                                line.decode("utf-8", errors="replace").rstrip("\r"))
                            if text:
                                q.put((source, time.time(), text, "dev"))
                    except socket.timeout:
                        continue
        except (ConnectionRefusedError, OSError) as e:
            if not stop.is_set():
                _dbg(f"rtt_reader [{source}]: connection failed ({e}) — retrying in 3 s")
                q.put((source, time.time(),
                       f"[RTT not ready ({e}) — retrying in 3 s]", "status"))
                time.sleep(3)


# ── SSH log reader ──────────────────────────────────────────────────────────────

def ssh_log_reader(source: str, host: str, remote_cmd: str,
                   q: queue.Queue, stop: threading.Event,
                   retry_delay: float = 5.0):
    _dbg(f"ssh_log_reader [{source}]: connecting to {host}")
    while not stop.is_set():
        q.put((source, time.time(), f"[SSH connecting → {host}]", "status"))
        try:
            proc = subprocess.Popen(
                ["ssh",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ServerAliveInterval=10",
                 "-o", "ConnectTimeout=10",
                 host, remote_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _dbg(f"ssh_log_reader [{source}]: PID {proc.pid}")
            for raw in proc.stdout:
                if stop.is_set():
                    break
                line = strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
                if line:
                    q.put((source, time.time(), line, "dev"))
            proc.wait()
            if not stop.is_set():
                q.put((source, time.time(),
                       f"[SSH closed (exit {proc.returncode}) — retrying in {retry_delay:.0f}s]",
                       "status"))
                time.sleep(retry_delay)
        except Exception as e:
            if not stop.is_set():
                _dbg(f"ssh_log_reader [{source}]: error: {e}")
                q.put((source, time.time(),
                       f"[SSH error: {e} — retrying in {retry_delay:.0f}s]", "status"))
                time.sleep(retry_delay)


# ── Device actions ──────────────────────────────────────────────────────────────

def _stream_action(tag: str, panels: list[str], cwd: str,
                   cmd: list[str], q: queue.Queue, done_cb=None):
    _dbg(f"_stream_action [{tag}]: $ {' '.join(cmd)}")
    for p in panels:
        q.put((p, time.time(), f"[{tag}] $ {' '.join(cmd[-5:])}", "build"))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        _dbg(f"_stream_action [{tag}]: PID {proc.pid}")
        for line in (proc.stdout or []):
            line = line.rstrip()
            if line:
                for p in panels:
                    q.put((p, time.time(), f"  {line}", "build"))
        proc.wait()
        ok  = proc.returncode == 0
        _dbg(f"_stream_action [{tag}]: exit {proc.returncode} ({'OK' if ok else 'FAILED'})")
        msg = f"[{tag}: {'OK' if ok else f'FAILED (exit {proc.returncode})'}]"
        for p in panels:
            q.put((p, time.time(), msg, "build"))
        if done_cb:
            done_cb(ok)
    except Exception as e:
        _dbg(f"_stream_action [{tag}]: exception: {e}")
        for p in panels:
            q.put((p, time.time(), f"[{tag}: error: {e}]", "build"))
        if done_cb:
            done_cb(False)


# ── GUI ─────────────────────────────────────────────────────────────────────────

class LogViewer(tk.Tk):

    def __init__(self, ble_port, lte_port, sensor_port):
        super().__init__()
        self.title("Tracker Dev Console")
        self.geometry("1800x980")
        self.configure(bg=PALETTE["BG"])

        self._q           = queue.Queue()
        self._stop        = threading.Event()
        self._rtt_stop    = threading.Event()   # cancelled and replaced on each respawn
        self._jlink_procs: list[subprocess.Popen] = []
        self._csv_file    = None
        self._csv_writer  = None
        self._recording   = False
        self._autoscroll  = {s: tk.BooleanVar(value=True) for s in SOURCES}
        self._line_count  = {s: 0 for s in SOURCES}
        # per-panel tab state: "uart" | "build" | "both"
        self._active_tab = {s: "uart" for s in SOURCES}
        self._all_lines  = {s: [] for s in SOURCES}   # (wall_str, msg, kind)
        self._tab_btns:  dict[str, dict[str, tk.Button]] = {}
        # per-panel persistent status label (populated in _build_ui)
        self._panel_status: dict[str, tk.Label] = {}
        # per-panel write queues for console send
        self._write_queues: dict[str, queue.Queue] = {}
        self._cmd_entries:  dict[str, tk.Entry]    = {}
        # key → list of buttons that get disabled during an action
        self._action_btns: dict[str, list[tk.Button]] = {}
        # sources currently mid-reset: suppress raw disconnect/connect messages
        self._resetting_sources: set[str] = set()
        # sources waiting for their *** Booting banner after a refresh
        self._waiting_for_boot: set[str] = set()
        # programmer SNR exclusion — prevents simultaneous flash on same DK
        self._snr_busy: set[str] = set()
        self._snr_mu = threading.Lock()
        # quiet events suppress disconnect spam in serial_reader during flash
        self._snr_quiet: dict[str, threading.Event] = {
            "1051217937": threading.Event(),
            "1050065248": threading.Event(),
        }
        # server branch label widget (set by _build_ui, updated by _fetch_server_branch)
        self._server_branch_lbl: tk.Label | None = None
        # security config panel state
        self._coap_mode_var    = tk.StringVar(value="oscore")
        self._ble_mode_var     = tk.StringVar(value="gatt_oscore")
        self._current_coap     = "oscore"
        self._current_ble      = "gatt_oscore"
        self._coap_current_lbl: tk.Label | None = None
        self._ble_current_lbl:  tk.Label | None = None

        self._build_ui()
        # initialise mode selectors from local.conf
        c = self._read_coap_mode()
        b = self._read_ble_mode()
        self._current_coap = c
        self._current_ble  = b
        self._coap_mode_var.set(c)
        self._ble_mode_var.set(b)
        self._update_mode_indicators()
        self._coap_mode_var.trace_add("write", lambda *_: self._update_mode_indicators())
        self._ble_mode_var.trace_add("write",  lambda *_: self._update_mode_indicators())

        self._launch(ble_port, lte_port, sensor_port)
        self._poll()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Log panels — Server on top (full-width), devices below ───────────
        panels_frame = tk.Frame(self, bg=PALETTE["BG"])
        panels_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 3))

        for i in range(3):
            panels_frame.columnconfigure(i, weight=1)
        panels_frame.rowconfigure(0, weight=1, minsize=200)  # Server row
        panels_frame.rowconfigure(1, weight=3)               # Device row

        self._texts = {}
        for src in SOURCES:
            fg        = SOURCE_COLOR[src]
            is_server = (src == "Server")

            if is_server:
                # Outer container for the full server row; holds log (left) + config (right)
                server_row = tk.Frame(panels_frame, bg=PALETTE["BG"])
                server_row.grid(row=0, column=0, columnspan=3, sticky="nsew",
                                padx=3, pady=(0, 3))
                col = tk.Frame(server_row, bg=PALETTE["BG2"])
                col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
            else:
                dev_idx = _DEVICE_SOURCES.index(src)
                col = tk.Frame(panels_frame, bg=PALETTE["BG2"])
                col.grid(row=1, column=dev_idx, sticky="nsew", padx=3)

            hdr = tk.Frame(col, bg=PALETTE["BG2"])
            hdr.pack(fill=tk.X, padx=4, pady=(4, 2))
            lbl_text = "Server  (docker compose logs)" if is_server else src
            tk.Label(hdr, text=lbl_text, fg=fg, bg=PALETTE["BG2"],
                     font=("monospace", 11, "bold")).pack(side=tk.LEFT)
            tk.Checkbutton(
                hdr, text="auto-scroll",
                variable=self._autoscroll[src],
                fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG2"], bd=0,
            ).pack(side=tk.RIGHT)

            self._tab_btns[src] = {}
            if not is_server:
                tab_frame = tk.Frame(hdr, bg=PALETTE["BG2"])
                tab_frame.pack(side=tk.RIGHT, padx=(4, 0))
                for tab_label, tab_key in [("UART", "uart"), ("Build", "build"), ("Both", "both")]:
                    is_active = (tab_key == "uart")
                    b = tk.Button(
                        tab_frame, text=tab_label,
                        bg="#21262d" if is_active else PALETTE["BG3"],
                        fg=fg if is_active else PALETTE["MUTE"],
                        activebackground="#30363d", relief=tk.FLAT,
                        padx=6, pady=1, font=("monospace", 8),
                        command=lambda s=src, t=tab_key: self._switch_tab(s, t),
                    )
                    b.pack(side=tk.LEFT, padx=1)
                    self._tab_btns[src][tab_key] = b

            if is_server:
                srv_btns = tk.Frame(col, bg=PALETTE["BG2"])
                srv_btns.pack(fill=tk.X, padx=4, pady=(0, 2))
                _SRV_BTN = {"bg": "#1a0d2e", "fg": fg,
                            "activebackground": "#2a1a42", "relief": tk.FLAT,
                            "font": ("monospace", 9)}
                tk.Button(srv_btns, text="Clear", padx=6, pady=2,
                          command=lambda: self._clear_source("Server"),
                          **_SRV_BTN).pack(side=tk.LEFT, padx=(0, 4))
                tk.Button(srv_btns, text="↺ Restart coap-server", padx=6, pady=2,
                          command=self._ssh_restart_server,
                          **_SRV_BTN).pack(side=tk.LEFT, padx=(0, 4))
                tk.Button(srv_btns, text="⟳ Rotate OSCORE", padx=6, pady=2,
                          command=self._rotate_oscore_keys,
                          **_SRV_BTN).pack(side=tk.LEFT, padx=(0, 4))
                tk.Button(srv_btns, text="⟳ Rotate DTLS", padx=6, pady=2,
                          command=self._rotate_dtls_keys,
                          **_SRV_BTN).pack(side=tk.LEFT, padx=(0, 4))

            stat_row = tk.Frame(col, bg=PALETTE["BG2"])
            stat_row.pack(fill=tk.X, padx=6, pady=(0, 2))
            ps = tk.Label(stat_row, text="", fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                          font=("monospace", 8))
            ps.pack(side=tk.LEFT)
            self._panel_status[src] = ps
            if is_server:
                self._server_branch_lbl = ps

            txt = scrolledtext.ScrolledText(
                col, bg=PALETTE["BG"], fg=fg,
                font=("monospace", 9), wrap=tk.WORD,
                state=tk.DISABLED, bd=0, padx=4, pady=2,
            )
            txt.tag_configure("ts",   foreground=SOURCE_COLOR[src])
            txt.tag_configure("msg",  foreground="#e6edf3")
            txt.tag_configure("meta", foreground=PALETTE["MUTE"])
            for level, color in LOG_LEVEL_COLOR.items():
                txt.tag_configure(level, foreground=color)
            txt.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))
            self._texts[src] = txt

            if is_server:
                self._build_server_config(server_row)
            else:
                inp_row = tk.Frame(col, bg=PALETTE["BG2"])
                inp_row.pack(fill=tk.X, padx=2, pady=(0, 3))
                entry = tk.Entry(
                    inp_row, bg=PALETTE["BG3"], fg=fg,
                    insertbackground=fg, relief=tk.FLAT,
                    font=("monospace", 9),
                )
                entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2), ipady=3)
                tk.Button(
                    inp_row, text="↵",
                    bg=PALETTE["BG3"], fg=fg,
                    activebackground="#30363d", relief=tk.FLAT,
                    padx=6, pady=2, font=("monospace", 9),
                    command=lambda s=src, e=entry: self._send_cmd(s, e),
                ).pack(side=tk.LEFT)
                entry.bind("<Return>", lambda event, s=src, e=entry: self._send_cmd(s, e))
                self._cmd_entries[src] = entry

        # ── Device action bar — 3 columns aligned under log panels ──────────────
        # Tinted button styles per source
        BTN_STYLE = {
            "BLE":      {"bg": "#0d2030", "fg": SOURCE_COLOR["BLE"],
                         "activebackground": "#162840"},
            "LTE":      {"bg": "#2a1e00", "fg": SOURCE_COLOR["LTE"],
                         "activebackground": "#3a2e10"},
            "Thingy53": {"bg": "#0d200f", "fg": SOURCE_COLOR["Thingy53"],
                         "activebackground": "#162a18"},
        }

        act = tk.Frame(self, bg=PALETTE["BG"], pady=2)
        act.pack(fill=tk.X, padx=6, pady=(0, 3))
        for i in range(3):
            act.columnconfigure(i, weight=1)

        def cbtn(parent, label, cmd, key, src):
            style = BTN_STYLE[src]
            b = tk.Button(
                parent, text=label,
                bg=style["bg"], fg=style["fg"],
                activebackground=style["activebackground"],
                relief=tk.FLAT, padx=6, pady=3,
                font=("monospace", 9),
                command=cmd,
            )
            b.pack(side=tk.LEFT, padx=2, pady=1)
            self._action_btns.setdefault(key, []).append(b)
            return b

        # ── BLE column (col 0) ────────────────────────────────────────────────
        ble_col = tk.Frame(act, bg=PALETTE["BG"])
        ble_col.grid(row=0, column=0, sticky="w", padx=(6, 3), pady=2)
        ble_r0 = tk.Frame(ble_col, bg=PALETTE["BG"])
        ble_r0.pack(fill=tk.X)
        cbtn(ble_r0, "Build",          lambda: self._do_build("ble"),                          "ble", "BLE")
        cbtn(ble_r0, "Build Pristine", lambda: self._do_build("ble", pristine=True),           "ble", "BLE")
        cbtn(ble_r0, "Flash",          lambda: self._do_flash("ble"),                          "ble", "BLE")
        ble_r1 = tk.Frame(ble_col, bg=PALETTE["BG"])
        ble_r1.pack(fill=tk.X)
        cbtn(ble_r1, "Build & Flash",          lambda: self._do_build_and_flash("ble"),                "ble", "BLE")
        cbtn(ble_r1, "Build Pristine & Flash", lambda: self._do_build_and_flash("ble", pristine=True), "ble", "BLE")

        # ── LTE column (col 1) ────────────────────────────────────────────────
        lte_col = tk.Frame(act, bg=PALETTE["BG"])
        lte_col.grid(row=0, column=1, sticky="w", padx=3, pady=2)
        lte_r0 = tk.Frame(lte_col, bg=PALETTE["BG"])
        lte_r0.pack(fill=tk.X)
        cbtn(lte_r0, "Build",          lambda: self._do_build("lte"),                          "lte", "LTE")
        cbtn(lte_r0, "Build Pristine", lambda: self._do_build("lte", pristine=True),           "lte", "LTE")
        cbtn(lte_r0, "Flash",          lambda: self._do_flash("lte"),                          "lte", "LTE")
        lte_r1 = tk.Frame(lte_col, bg=PALETTE["BG"])
        lte_r1.pack(fill=tk.X)
        cbtn(lte_r1, "Build & Flash",          lambda: self._do_build_and_flash("lte"),                "lte", "LTE")
        cbtn(lte_r1, "Build Pristine & Flash", lambda: self._do_build_and_flash("lte", pristine=True), "lte", "LTE")

        # ── Thingy:53 column (col 2) ──────────────────────────────────────────
        sens_col = tk.Frame(act, bg=PALETTE["BG"])
        sens_col.grid(row=0, column=2, sticky="w", padx=(3, 6), pady=2)
        sens_r0 = tk.Frame(sens_col, bg=PALETTE["BG"])
        sens_r0.pack(fill=tk.X)
        cbtn(sens_r0, "Build",          lambda: self._do_build("sensor"),                          "sensor", "Thingy53")
        cbtn(sens_r0, "Build Pristine", lambda: self._do_build("sensor", pristine=True),           "sensor", "Thingy53")
        cbtn(sens_r0, "Flash",          lambda: self._do_flash("sensor"),                          "sensor", "Thingy53")
        sens_r1 = tk.Frame(sens_col, bg=PALETTE["BG"])
        sens_r1.pack(fill=tk.X)
        cbtn(sens_r1, "Build & Flash",          lambda: self._do_build_and_flash("sensor"),                "sensor", "Thingy53")
        cbtn(sens_r1, "Build Pristine & Flash", lambda: self._do_build_and_flash("sensor", pristine=True), "sensor", "Thingy53")

        # ── Bottom bar: recording + clipboard ────────────────────────────────
        bar = tk.Frame(self, bg=PALETTE["BG"], pady=4)
        bar.pack(fill=tk.X, padx=6, pady=(0, 6))

        self._rec_btn = tk.Button(
            bar, text="⏺  Start recording",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=10, pady=5, font=("sans-serif", 10),
            command=self._toggle_recording,
        )
        self._rec_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._csv_label = tk.Label(
            bar, text="—", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
            font=("monospace", 9),
        )
        self._csv_label.pack(side=tk.LEFT, padx=(0, 12))

        tk.Button(
            bar, text="Clear all",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=10, pady=5, command=self._clear_all,
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Button(
            bar, text="↻ Refresh",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=10, pady=5, command=self._refresh_all,
        ).pack(side=tk.LEFT, padx=(0, 4))

        tk.Button(
            bar, text="↺ Reset 91X",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=8, pady=5, command=self._reset_91x,
        ).pack(side=tk.LEFT, padx=(0, 4))

        tk.Button(
            bar, text="↺ Reset 53",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=8, pady=5, command=lambda: self._do_reset("1050065248", "53", ["Thingy53"]),
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Frame(bar, bg=PALETTE["MUTE"], width=1, height=24).pack(
            side=tk.LEFT, padx=(0, 8), fill=tk.Y)

        tk.Label(bar, text="Copy:", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
                 font=("monospace", 9)).pack(side=tk.LEFT, padx=(0, 4))

        self._copy_include = {s: tk.BooleanVar(value=True) for s in SOURCES}
        for src in SOURCES:
            tk.Checkbutton(
                bar, text=src, variable=self._copy_include[src],
                fg=SOURCE_COLOR[src], bg=PALETTE["BG"],
                selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG"],
                font=("monospace", 9), bd=0,
            ).pack(side=tk.LEFT, padx=2)

        tk.Label(bar, text="last", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
                 font=("monospace", 9)).pack(side=tk.LEFT, padx=(8, 2))
        self._copy_lines = tk.IntVar(value=100)
        tk.Spinbox(
            bar, from_=10, to=5000, increment=10,
            textvariable=self._copy_lines, width=5,
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            buttonbackground=PALETTE["BG3"],
            relief=tk.FLAT, font=("monospace", 9),
        ).pack(side=tk.LEFT, padx=(0, 2))
        tk.Label(bar, text="lines", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
                 font=("monospace", 9)).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(
            bar, text="Copy",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=8, pady=5, command=self._copy_to_clipboard,
        ).pack(side=tk.LEFT)

        self._status = tk.Label(
            bar, text="", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
            font=("monospace", 9),
        )
        self._status.pack(side=tk.RIGHT)

    # ── Launch ────────────────────────────────────────────────────────────────

    def _launch(self, ble_port, lte_port, sensor_port):
        q91  = self._snr_quiet["1051217937"]
        q53  = self._snr_quiet["1050065248"]

        serial_ports = [
            ("BLE",      ble_port,    q91,  None),
            ("LTE",      lte_port,    q91,  None),
            ("Thingy53", sensor_port, q53,  None),
        ]
        for src, port, quiet, reconnect_cb in serial_ports:
            wq = queue.Queue()
            self._write_queues[src] = wq
            if port:
                threading.Thread(target=serial_reader,
                                 args=(src, port, self._q, self._stop),
                                 kwargs={"quiet": quiet, "reconnect_cb": reconnect_cb,
                                         "write_q": wq},
                                 daemon=True).start()
            else:
                self._append(src, time.time(),
                             "[port not found — waiting for device…]")

        self._jlink_procs = spawn_jlink_servers(JLINK_SERVERS, self._q)
        self._start_rtt_readers()

        threading.Thread(
            target=ssh_log_reader,
            args=("Server", SSH_SERVER_HOST, SSH_SERVER_CMD, self._q, self._stop),
            daemon=True,
        ).start()
        self._fetch_server_branch()

        missing = [(src, quiet, reconnect_cb)
                   for src, port, quiet, reconnect_cb in serial_ports if not port]
        if missing:
            threading.Thread(target=self._hotplug_watcher, args=(missing,),
                             daemon=True).start()

    def _hotplug_watcher(self, missing: list):
        find_fns = {
            "BLE":      lambda: find_thingy91x_ports()[0],
            "LTE":      lambda: find_thingy91x_ports()[1],
            "Thingy53": find_thingy53_port,
        }
        pending = {src: (find_fns[src], quiet, cb) for src, quiet, cb in missing}
        while not self._stop.is_set() and pending:
            time.sleep(1.0)
            for src in list(pending):
                find_fn, quiet, reconnect_cb = pending[src]
                port = find_fn()
                if port:
                    del pending[src]
                    wq = self._write_queues[src]
                    threading.Thread(
                        target=serial_reader,
                        args=(src, port, self._q, self._stop),
                        kwargs={"quiet": quiet, "reconnect_cb": reconnect_cb, "write_q": wq},
                        daemon=True,
                    ).start()

    # ── Message pump ─────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == _UI_:
                    try: item[2]()
                    except Exception: pass
                else:
                    source, ts, msg, kind = item
                    self._append(source, ts, msg, kind)
        except queue.Empty:
            pass
        except Exception:
            pass  # never let _poll die
        self.after(40, self._poll)

    def _append(self, source: str, ts: float, msg: str, kind: str = "build"):
        if source not in self._texts:
            return
        wall = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]

        if kind == "status":
            name = _RESET_LABEL.get(source, source)
            if msg.startswith("[disconnected:") and source in self._resetting_sources:
                msg = f"[↺ Resetting {name}…]"
            elif msg.startswith("[connected →") and source in self._resetting_sources:
                self._resetting_sources.discard(source)
                msg = f"[↺ {name} rebooted]"
            elif msg.startswith("[disconnected:") or msg.startswith("[connected →"):
                return

        if source in self._waiting_for_boot:
            if "*** Booting" in msg:
                self._waiting_for_boot.discard(source)
                self._clear_source(source)
                # fall through — display this line as the first entry
            else:
                return  # drop everything before the boot banner

        buf = self._all_lines[source]
        buf.append((wall, msg, kind))
        if len(buf) > 6000:
            del buf[:1000]

        tab = self._active_tab[source]
        visible = (tab == "both"
                   or kind == "status"
                   or (tab == "uart"  and kind == "dev")
                   or (tab == "build" and kind == "build"))
        if visible:
            txt  = self._texts[source]
            txt.configure(state=tk.NORMAL)
            if kind == "dev":
                txt.insert(tk.END, wall, "ts")
                txt.insert(tk.END, f"  {msg}\n", _dev_tag(msg))
            else:
                txt.insert(tk.END, wall, "meta")
                txt.insert(tk.END, f"  {msg}\n", "meta")
            self._line_count[source] += 1
            if self._line_count[source] > 6000:
                txt.delete("1.0", "1001.0")
                self._line_count[source] -= 1000
            if self._autoscroll[source].get():
                txt.see(tk.END)
            txt.configure(state=tk.DISABLED)

        if self._recording and self._csv_writer:
            self._csv_writer.writerow([
                datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                source, msg,
            ])
            self._csv_file.flush()

    def _switch_tab(self, source: str, tab: str):
        self._active_tab[source] = tab
        fg = SOURCE_COLOR[source]
        for t, b in self._tab_btns[source].items():
            if t == tab:
                b.configure(bg="#21262d", fg=fg)
            else:
                b.configure(bg=PALETTE["BG3"], fg=PALETTE["MUTE"])

        txt = self._texts[source]
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)
        visible = [
            (wall, msg, kind) for wall, msg, kind in self._all_lines[source]
            if (tab == "both"
                or kind == "status"
                or (tab == "uart"  and kind == "dev")
                or (tab == "build" and kind == "build"))
        ]
        for wall, msg, kind in visible:
            if kind == "dev":
                txt.insert(tk.END, wall, "ts")
                txt.insert(tk.END, f"  {msg}\n", _dev_tag(msg))
            else:
                txt.insert(tk.END, wall, "meta")
                txt.insert(tk.END, f"  {msg}\n", "meta")
        self._line_count[source] = len(visible)
        if self._autoscroll[source].get():
            txt.see(tk.END)
        txt.configure(state=tk.DISABLED)

    # ── Panel status label ────────────────────────────────────────────────────

    def _set_panel_status(self, source: str, msg: str, ok: bool | None = None):
        lbl = self._panel_status.get(source)
        if not lbl:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        if ok is True:
            fg = PALETTE["GRN"]
        elif ok is False:
            fg = PALETTE["RED"]
        else:
            fg = PALETTE["MUTE"]
        lbl.configure(text=f"{msg}  {ts}", fg=fg)

    # ── Console send ──────────────────────────────────────────────────────────

    def _send_cmd(self, source: str, entry: tk.Entry):
        text = entry.get()
        if not text:
            return
        entry.delete(0, tk.END)
        wq = self._write_queues.get(source)
        if wq:
            wq.put((text + "\r\n").encode())
        self._append(source, time.time(), f"$ {text}", "dev")

    # ── Programmer exclusion ──────────────────────────────────────────────────

    def _snr_try_acquire(self, snr: str, panels: list[str]) -> bool:
        """Return True and mark SNR busy, or post an error and return False."""
        with self._snr_mu:
            if snr in self._snr_busy:
                for p in panels:
                    self._q.put((p, time.time(),
                                 f"[programmer {snr} busy — wait for current flash to finish]",
                                 "build"))
                return False
            self._snr_busy.add(snr)
        if snr in self._snr_quiet:
            self._snr_quiet[snr].set()
        return True

    def _snr_release(self, snr: str):
        with self._snr_mu:
            self._snr_busy.discard(snr)
        if snr in self._snr_quiet:
            self._snr_quiet[snr].clear()

    def _start_rtt_readers(self, startup_delay: float = 3.0):
        """Cancel any existing rtt_reader threads and start fresh ones."""
        _dbg(f"_start_rtt_readers: cancelling old readers, starting new with delay={startup_delay}s")
        self._rtt_stop.set()               # signal all current rtt_reader threads to exit
        self._rtt_stop = threading.Event() # fresh event for the new generation
        for srv in JLINK_SERVERS:
            threading.Thread(
                target=rtt_reader,
                args=(srv["label"], "localhost", srv["rtt_port"],
                      self._q, self._rtt_stop),
                kwargs={"startup_delay": startup_delay},
                daemon=True,
            ).start()

    def _respawn_jlink(self):
        """Kill existing JLinkGDBServer procs and start fresh ones."""
        _dbg("_respawn_jlink: called")
        stop_jlink_servers(self._jlink_procs)
        _dbg("_respawn_jlink: spawning fresh servers")
        self._jlink_procs = spawn_jlink_servers(JLINK_SERVERS, self._q)
        self._start_rtt_readers()

    def _do_reset(self, snr: str, label: str, sources: list[str] | None = None):
        if sources:
            self._resetting_sources.update(sources)
        self._status.configure(text=f"Resetting {label}…")
        def run():
            result = subprocess.run(
                ["nrfutil", "device", "reset", "--serial-number", snr],
                capture_output=True, text=True,
            )
            ok  = result.returncode == 0
            msg = f"{label} reset {'OK' if ok else 'FAILED'}"
            self._q.put((_UI_, time.time(), lambda: self._status.configure(text=msg)))
        threading.Thread(target=run, daemon=True).start()

    def _shell_cmd(self, source: str, cmd: str):
        """Send a shell command string to a panel's write queue and echo it."""
        wq = self._write_queues.get(source)
        if wq:
            wq.put((cmd + "\r\n").encode())
            self._append(source, time.time(), f"$ {cmd}", "dev")
            self._status.configure(text=f"Sent to {source}: {cmd}")
        else:
            self._status.configure(text=f"{source} not connected")

    def _reset_91x(self):
        """Reset the whole Thingy:91X: nRF9151 then nRF5340 via 'reset all' shell command."""
        self._resetting_sources.update(["BLE", "LTE"])
        self._shell_cmd("BLE", "reset all")

    def _refresh_all(self):
        self._clear_all()
        self._status.configure(text="Refreshing…")
        self._resetting_sources.update(_DEVICE_SOURCES)
        # reset all via BLE shell (resets nRF9151 then reboots nRF5340)
        self._shell_cmd("BLE", "reset all")
        # reset Thingy:53 via nrfutil (no shell on Thingy:53)
        def reset_53():
            result = subprocess.run(
                ["nrfutil", "device", "reset", "--serial-number", "1050065248"],
                capture_output=True, text=True,
            )
            ok  = result.returncode == 0
            msg = f"53 reset {'OK' if ok else 'FAILED'}"
            self._q.put((_UI_, time.time(), lambda m=msg: self._status.configure(text=m)))
        threading.Thread(target=reset_53, daemon=True).start()

    # ── Security config panel ─────────────────────────────────────────────────

    def _build_server_config(self, parent: tk.Frame):
        fg  = SOURCE_COLOR["Server"]
        cfg = tk.Frame(parent, bg=PALETTE["BG2"])
        cfg.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(cfg, text="Configuration", fg=fg, bg=PALETTE["BG2"],
                 font=("monospace", 11, "bold"),
                 padx=8, pady=4).pack(anchor="w")
        tk.Frame(cfg, bg=PALETTE["BG3"], height=1).pack(fill=tk.X, padx=6, pady=(0, 6))

        # ── CoAP section ──────────────────────────────────────────────────────
        coap_sec = tk.Frame(cfg, bg=PALETTE["BG2"])
        coap_sec.pack(fill=tk.X, padx=8, pady=(0, 4))
        tk.Label(coap_sec, text="CoAP  (hub ↔ server)", fg=PALETTE["MUTE"],
                 bg=PALETTE["BG2"], font=("monospace", 8)).pack(anchor="w")

        cur_row = tk.Frame(coap_sec, bg=PALETTE["BG2"])
        cur_row.pack(anchor="w", pady=(1, 3))
        tk.Label(cur_row, text="Current:", fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                 font=("monospace", 8)).pack(side=tk.LEFT)
        self._coap_current_lbl = tk.Label(cur_row, text="—", fg=PALETTE["GRN"],
                                          bg=PALETTE["BG2"],
                                          font=("monospace", 8, "bold"))
        self._coap_current_lbl.pack(side=tk.LEFT, padx=(4, 0))

        radio_row = tk.Frame(coap_sec, bg=PALETTE["BG2"])
        radio_row.pack(anchor="w")
        for val, lbl in _COAP_LABELS:
            tk.Radiobutton(
                radio_row, text=lbl, variable=self._coap_mode_var, value=val,
                fg=fg, bg=PALETTE["BG2"], selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG2"], activeforeground=fg,
                font=("monospace", 9), bd=0,
            ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Frame(cfg, bg=PALETTE["BG3"], height=1).pack(fill=tk.X, padx=6, pady=4)

        # ── BLE section ───────────────────────────────────────────────────────
        ble_sec = tk.Frame(cfg, bg=PALETTE["BG2"])
        ble_sec.pack(fill=tk.X, padx=8, pady=(0, 4))
        tk.Label(ble_sec, text="BLE  (sensor → hub)", fg=PALETTE["MUTE"],
                 bg=PALETTE["BG2"], font=("monospace", 8)).pack(anchor="w")

        cur_row2 = tk.Frame(ble_sec, bg=PALETTE["BG2"])
        cur_row2.pack(anchor="w", pady=(1, 3))
        tk.Label(cur_row2, text="Current:", fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                 font=("monospace", 8)).pack(side=tk.LEFT)
        self._ble_current_lbl = tk.Label(cur_row2, text="—", fg=PALETTE["GRN"],
                                         bg=PALETTE["BG2"],
                                         font=("monospace", 8, "bold"))
        self._ble_current_lbl.pack(side=tk.LEFT, padx=(4, 0))

        ble_r1 = tk.Frame(ble_sec, bg=PALETTE["BG2"])
        ble_r1.pack(anchor="w")
        for val, lbl in _BLE_LABELS[:3]:
            tk.Radiobutton(
                ble_r1, text=lbl, variable=self._ble_mode_var, value=val,
                fg=fg, bg=PALETTE["BG2"], selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG2"], activeforeground=fg,
                font=("monospace", 9), bd=0,
            ).pack(side=tk.LEFT, padx=(0, 6))

        ble_r2 = tk.Frame(ble_sec, bg=PALETTE["BG2"])
        ble_r2.pack(anchor="w", pady=(2, 0))
        for val, lbl in _BLE_LABELS[3:]:
            tk.Radiobutton(
                ble_r2, text=lbl, variable=self._ble_mode_var, value=val,
                fg=fg, bg=PALETTE["BG2"], selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG2"], activeforeground=fg,
                font=("monospace", 9), bd=0,
            ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Frame(cfg, bg=PALETTE["BG3"], height=1).pack(fill=tk.X, padx=6, pady=(4, 6))

        # ── Apply button ──────────────────────────────────────────────────────
        bottom = tk.Frame(cfg, bg=PALETTE["BG2"])
        bottom.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Button(
            bottom, text="Apply & Restart Server",
            bg="#1a0d2e", fg=fg, activebackground="#2a1a42",
            relief=tk.FLAT, padx=8, pady=4,
            font=("monospace", 9),
            command=self._apply_security_modes,
        ).pack(side=tk.LEFT)
        tk.Label(bottom,
                 text="  Rebuild + reflash firmware to update hardware",
                 fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                 font=("monospace", 8)).pack(side=tk.LEFT)

    def _update_mode_indicators(self):
        coap_sel = self._coap_mode_var.get()
        ble_sel  = self._ble_mode_var.get()

        if self._coap_current_lbl:
            human = dict(_COAP_LABELS).get(self._current_coap, self._current_coap)
            if coap_sel != self._current_coap:
                sel_h = dict(_COAP_LABELS).get(coap_sel, coap_sel)
                self._coap_current_lbl.configure(
                    text=f"{human}  →  {sel_h}", fg=SOURCE_COLOR["LTE"])
            else:
                self._coap_current_lbl.configure(text=human, fg=PALETTE["GRN"])

        if self._ble_current_lbl:
            human = dict(_BLE_LABELS).get(self._current_ble, self._current_ble)
            if ble_sel != self._current_ble:
                sel_h = dict(_BLE_LABELS).get(ble_sel, ble_sel)
                self._ble_current_lbl.configure(
                    text=f"{human}  →  {sel_h}", fg=SOURCE_COLOR["LTE"])
            else:
                self._ble_current_lbl.configure(text=human, fg=PALETTE["GRN"])

    def _read_coap_mode(self) -> str:
        conf = _LTE_APP / "local.conf"
        if conf.exists():
            m = _COAP_MODE_PAT.search(conf.read_text())
            if m:
                return m.group(1).lower()
        return "oscore"

    def _read_ble_mode(self) -> str:
        conf = _SENS_APP / "local.conf"
        if conf.exists():
            m = _BLE_MODE_PAT.search(conf.read_text())
            if m:
                return m.group(1).lower()
        return "gatt_oscore"

    def _set_kconfig_mode(self, path: Path, prefix: str, new_line: str):
        text = path.read_text() if path.exists() else ""
        pat  = re.compile(rf"^{re.escape(prefix)}\w+=y", re.M)
        if pat.search(text):
            text = pat.sub(new_line, text)
        else:
            text = text.rstrip("\n") + "\n" + new_line + "\n"
        path.write_text(text)

    def _apply_security_modes(self):
        def run():
            coap = self._coap_mode_var.get()
            ble  = self._ble_mode_var.get()
            self._q.put(("Server", time.time(),
                         f"[Applying: CoAP={coap}  BLE={ble}]", "status"))
            try:
                self._set_kconfig_mode(
                    _LTE_APP / "local.conf",
                    "CONFIG_APP_COAP_SECURITY_",
                    f"CONFIG_APP_COAP_SECURITY_{coap.upper()}=y",
                )
                self._q.put(("Server", time.time(),
                             f"  → hub LTE local.conf: COAP_SECURITY_{coap.upper()}", "build"))

                self._set_kconfig_mode(
                    _SENS_APP / "local.conf",
                    "CONFIG_APP_BLE_SECURITY_",
                    f"CONFIG_APP_BLE_SECURITY_{ble.upper()}=y",
                )
                self._q.put(("Server", time.time(),
                             f"  → sensor local.conf: BLE_SECURITY_{ble.upper()}", "build"))

                env_cmd = (
                    f"python3 -c \""
                    f"import re, pathlib; "
                    f"p = pathlib.Path('/root/tracker-server/.env'); "
                    f"t = p.read_text(); "
                    f"t = re.sub(r'^SECURITY_MODE=.*', 'SECURITY_MODE={coap}', t, flags=re.M); "
                    f"p.write_text(t)\""
                )
                subprocess.run(["ssh", "-o", "StrictHostKeyChecking=accept-new",
                                SSH_SERVER_HOST, env_cmd], timeout=10)
                self._q.put(("Server", time.time(),
                             f"  → server .env: SECURITY_MODE={coap}", "build"))

                self._ssh_restart_server()
                self._q.put(("Server", time.time(),
                             "[local.conf written, server restarted — rebuild+reflash firmware]",
                             "status"))
            except Exception as e:
                self._q.put(("Server", time.time(), f"[apply error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    # ── Server actions ────────────────────────────────────────────────────────

    def _fetch_server_branch(self):
        def run():
            try:
                result = subprocess.run(
                    ["ssh",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ConnectTimeout=10",
                     SSH_SERVER_HOST,
                     "git -C ~/tracker-server fetch --quiet 2>&1;"
                     " git -C ~/tracker-server status --short --branch 2>&1"],
                    capture_output=True, text=True, timeout=20,
                )
                first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
                branch = first.lstrip("# ").split("...")[0].strip() or "?"
                if "[behind" in first:
                    n = first.split("[behind")[1].split("]")[0].strip()
                    label = f"branch: {branch}  ⚠ {n} behind"
                    color = PALETTE["RED"]
                elif "[ahead" in first:
                    n = first.split("[ahead")[1].split("]")[0].strip()
                    label = f"branch: {branch}  ↑ {n} ahead"
                    color = SOURCE_COLOR["LTE"]
                else:
                    label = f"branch: {branch}  ✓ up to date"
                    color = PALETTE["GRN"]
            except Exception as e:
                label = f"branch: (SSH error: {e})"
                color = PALETTE["MUTE"]
            self._q.put((_UI_, time.time(),
                         lambda l=label, c=color:
                         self._server_branch_lbl and
                         self._server_branch_lbl.configure(text=l, fg=c)))
        threading.Thread(target=run, daemon=True).start()

    def _ssh_restart_server(self):
        def run():
            self._q.put(("Server", time.time(), "[Restarting coap-server…]", "status"))
            try:
                result = subprocess.run(
                    ["ssh",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ConnectTimeout=10",
                     SSH_SERVER_HOST,
                     "cd tracker-server && docker compose restart coap-server 2>&1"],
                    capture_output=True, text=True, timeout=60,
                )
                ok = result.returncode == 0
                for line in result.stdout.splitlines():
                    if line.strip():
                        self._q.put(("Server", time.time(), f"  {line}", "build"))
                self._q.put(("Server", time.time(),
                             f"[coap-server restart {'OK' if ok else 'FAILED'}]", "status"))
                self._fetch_server_branch()
            except Exception as e:
                self._q.put(("Server", time.time(), f"[restart error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    def _rotate_oscore_keys(self):
        def run():
            self._q.put(("Server", time.time(), "[OSCORE key rotation started]", "status"))
            try:
                for name, conf_path in [("hub",    _LTE_APP  / "oscore.conf"),
                                         ("sensor", _SENS_APP / "oscore.conf")]:
                    self._q.put(("Server", time.time(),
                                 f"[generate_oscore_psk.py --name {name}]", "build"))
                    r = subprocess.run(
                        ["ssh",
                         "-o", "StrictHostKeyChecking=accept-new",
                         "-o", "ConnectTimeout=10",
                         SSH_SERVER_HOST,
                         f"cd ~/tracker-server && python3 generate_oscore_psk.py --name {name} 2>&1"],
                        capture_output=True, text=True, timeout=30,
                    )
                    for line in r.stdout.splitlines():
                        self._q.put(("Server", time.time(), f"  {line}", "build"))
                    if r.returncode != 0:
                        self._q.put(("Server", time.time(),
                                     f"[FAILED generating {name} context]", "status"))
                        return
                    conf_lines = [ln for ln in r.stdout.splitlines()
                                  if ln.startswith("CONFIG_APP_OSCORE_")]
                    conf_path.write_text("\n".join(conf_lines) + "\n")
                    self._q.put(("Server", time.time(), f"  → wrote {conf_path}", "build"))

                # Ensure OSCORE_CONTEXT_DIR is set to the in-container mount path
                env_cmd = (
                    "python3 -c \""
                    "import re, pathlib; "
                    "p = pathlib.Path('/root/tracker-server/.env'); "
                    "t = p.read_text(); "
                    "t = re.sub(r'^OSCORE_CONTEXT_DIR=.*', "
                    "'OSCORE_CONTEXT_DIR=/app/oscore-context', t, flags=re.M); "
                    "p.write_text(t)\""
                )
                subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                     SSH_SERVER_HOST, env_cmd],
                    timeout=10,
                )
                self._q.put(("Server", time.time(),
                             "[.env: OSCORE_CONTEXT_DIR=/app/oscore-context]", "build"))
                self._ssh_restart_server()
                self._q.put(("Server", time.time(),
                             "[OSCORE rotation done — rebuild + reflash hub and sensor]",
                             "status"))
            except Exception as e:
                self._q.put(("Server", time.time(),
                             f"[OSCORE rotation error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    def _rotate_dtls_keys(self):
        def run():
            self._q.put(("Server", time.time(), "[DTLS key rotation started]", "status"))
            try:
                r = subprocess.run(
                    ["ssh",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "ConnectTimeout=10",
                     SSH_SERVER_HOST,
                     "cd ~/tracker-server && python3 generate_dtls_psk.py 2>&1"],
                    capture_output=True, text=True, timeout=30,
                )
                for line in r.stdout.splitlines():
                    self._q.put(("Server", time.time(), f"  {line}", "build"))
                if r.returncode != 0:
                    self._q.put(("Server", time.time(), "[DTLS gen FAILED]", "status"))
                    return

                conf_lines = [ln for ln in r.stdout.splitlines()
                              if ln.startswith("CONFIG_APP_DTLS_")]
                dtls_conf = _LTE_APP / "dtls.conf"
                dtls_conf.write_text("\n".join(conf_lines) + "\n")
                self._q.put(("Server", time.time(), f"  → wrote {dtls_conf}", "build"))

                env_vals = {}
                for ln in r.stdout.splitlines():
                    if ln.startswith("DTLS_PSK_IDENTITY=") or ln.startswith("DTLS_PSK_KEY_HEX="):
                        k, v = ln.split("=", 1)
                        env_vals[k] = v

                if env_vals:
                    subs = "; ".join(
                        f"t = re.sub(r'^{k}=.*', r'{k}={v}', t, flags=re.M)"
                        for k, v in env_vals.items()
                    )
                    env_cmd = (
                        f"python3 -c \""
                        f"import re, pathlib; "
                        f"p = pathlib.Path('/root/tracker-server/.env'); "
                        f"t = p.read_text(); "
                        f"{subs}; "
                        f"p.write_text(t)\""
                    )
                    subprocess.run(
                        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                         SSH_SERVER_HOST, env_cmd],
                        timeout=10,
                    )
                    self._q.put(("Server", time.time(),
                                 "[.env updated with new DTLS PSK]", "build"))

                self._ssh_restart_server()
                self._q.put(("Server", time.time(),
                             "[DTLS rotation done — rebuild + reflash hub]", "status"))
            except Exception as e:
                self._q.put(("Server", time.time(),
                             f"[DTLS rotation error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    # ── Device actions ────────────────────────────────────────────────────────

    def _set_btns(self, key: str, enabled: bool):
        for b in self._action_btns.get(key, []):
            b.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _do_build(self, key: str, pristine: bool = False):
        tgt = TARGETS[key]
        self._set_btns(key, False)

        base_cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
        if pristine:
            # Insert --pristine right after 'west build'
            west_idx  = base_cmd.index("west")
            base_cmd  = (base_cmd[:west_idx + 2]
                         + ["--pristine"]
                         + base_cmd[west_idx + 2:])

        tag = f"build {'pristine' if pristine else ''}{key}".strip()
        self._status.configure(
            text=f"Building {tgt['label']}{'  (pristine)' if pristine else ''}...")

        lbl = tgt["label"]
        build_label = "Build pristine" if pristine else "Build"
        for p in tgt["panels"]:
            self._q.put((p, time.time(), f"[{build_label} started]", "status"))

        def done(ok, _panels=tgt["panels"]):
            txt        = f"{build_label} {'OK' if ok else 'FAILED'}"
            status_txt = f"{lbl} build {'OK' if ok else 'FAILED'}"
            self._q.put((_UI_, time.time(), lambda: self._set_btns(key, True)))
            self._q.put((_UI_, time.time(), lambda: self._status.configure(text=status_txt)))
            for p in _panels:
                self._q.put((p, time.time(), f"[{build_label} {'OK' if ok else 'FAILED'}]", "status"))
                self._q.put((_UI_, time.time(), lambda p=p, t=txt, o=ok:
                             self._set_panel_status(p, t, o)))

        threading.Thread(
            target=_stream_action,
            args=(tag, tgt["panels"], tgt["cwd"], base_cmd, self._q),
            kwargs={"done_cb": done},
            daemon=True,
        ).start()

    def _do_flash(self, key: str):
        tgt = TARGETS[key]
        self._set_btns(key, False)
        self._status.configure(text=f"Flashing {tgt['label']}...")

        def flash_thread():
            panels = tgt["panels"]
            cwd    = tgt["cwd"]
            snr    = tgt.get("snr")
            lbl    = tgt["label"]

            def _ui(txt):
                self._q.put((_UI_, time.time(), lambda: self._set_btns(key, True)))
                self._q.put((_UI_, time.time(), lambda s=txt: self._status.configure(text=s)))

            if snr and not self._snr_try_acquire(snr, panels):
                _ui(f"{lbl} flash blocked — programmer {snr} busy")
                return

            try:
                for p in panels:
                    self._q.put((p, time.time(), "[Flash started]", "status"))
                for pre_cmd in tgt.get("pre_flash_cmds", []):
                    for p in panels:
                        self._q.put((p, time.time(),
                                     f"[pre-flash] $ {' '.join(pre_cmd[-4:])}", "build"))
                    result = subprocess.run(pre_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        msg = result.stderr.strip() or result.stdout.strip()
                        for p in panels:
                            self._q.put((p, time.time(), f"[pre-flash failed: {msg}]", "build"))
                            self._q.put((p, time.time(), "[Flash FAILED (pre-flash)]", "status"))
                        _ui(f"{lbl} flash FAILED (pre-flash)")
                        for p in panels:
                            self._q.put((_UI_, time.time(), lambda p=p:
                                         self._set_panel_status(p, "Flash FAILED (pre-flash)", False)))
                        return

                flash_cmd = NRFUTIL_WRAP + tgt["flash_cmd"]

                def done(ok, _panels=panels):
                    txt = f"Flash {'OK' if ok else 'FAILED'}"
                    _ui(f"{lbl} flash {'OK' if ok else 'FAILED'}")
                    for p in _panels:
                        self._q.put((p, time.time(), f"[Flash {'OK' if ok else 'FAILED'}]", "status"))
                        self._q.put((_UI_, time.time(), lambda p=p, t=txt, o=ok:
                                     self._set_panel_status(p, t, o)))
                    if ok:
                        if key == "lte":
                            self._current_coap = self._coap_mode_var.get()
                        elif key == "sensor":
                            self._current_ble = self._ble_mode_var.get()
                        self._q.put((_UI_, time.time(), self._update_mode_indicators))
                    if key == "sensor":
                        self._q.put((_UI_, time.time(), self._respawn_jlink))

                _stream_action("flash", panels, cwd, flash_cmd, self._q, done_cb=done)
            finally:
                if snr:
                    self._snr_release(snr)

        threading.Thread(target=flash_thread, daemon=True).start()

    def _do_build_and_flash(self, key: str, pristine: bool = False):
        """Build then flash in sequence."""
        tgt = TARGETS[key]
        self._set_btns(key, False)
        label = f"{tgt['label']}{'  (pristine)' if pristine else ''}"
        self._status.configure(text=f"Building {label}...")
        lbl        = tgt["label"]
        build_lbl  = "Build pristine" if pristine else "Build"
        suffix     = "Pristine build & flash" if pristine else "Build & flash"
        for p in tgt["panels"]:
            self._q.put((p, time.time(), f"[{build_lbl} started]", "status"))

        def _ui(txt):
            self._q.put((_UI_, time.time(), lambda: self._set_btns(key, True)))
            self._q.put((_UI_, time.time(), lambda s=txt: self._status.configure(text=s)))

        def after_build(ok):
            for p in tgt["panels"]:
                self._q.put((p, time.time(),
                             f"[{build_lbl} {'OK' if ok else 'FAILED'}]", "status"))
            if not ok:
                _ui(f"{lbl} build FAILED — flash skipped")
                for p in tgt["panels"]:
                    self._q.put((_UI_, time.time(), lambda p=p:
                                 self._set_panel_status(p, "Build FAILED", False)))
                return

            panels = tgt["panels"]
            cwd    = tgt["cwd"]
            snr    = tgt.get("snr")

            if snr and not self._snr_try_acquire(snr, panels):
                _ui(f"{lbl} flash blocked — programmer {snr} busy")
                return

            self._q.put((_UI_, time.time(),
                         lambda: self._status.configure(text=f"Flashing {lbl}...")))
            for p in panels:
                self._q.put((p, time.time(), "[Flash started]", "status"))

            try:
                for pre_cmd in tgt.get("pre_flash_cmds", []):
                    for p in panels:
                        self._q.put((p, time.time(),
                                     f"[pre-flash] $ {' '.join(pre_cmd[-4:])}", "build"))
                    result = subprocess.run(pre_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        msg = result.stderr.strip() or result.stdout.strip()
                        for p in panels:
                            self._q.put((p, time.time(), f"[pre-flash failed: {msg}]", "build"))
                            self._q.put((p, time.time(), "[Flash FAILED (pre-flash)]", "status"))
                        _ui(f"{lbl} flash FAILED (pre-flash)")
                        for p in panels:
                            self._q.put((_UI_, time.time(), lambda p=p:
                                         self._set_panel_status(p, "Flash FAILED (pre-flash)", False)))
                        return
                flash_cmd = NRFUTIL_WRAP + tgt["flash_cmd"]

                def done(ok2, _panels=panels, _suffix=suffix):
                    txt = f"{_suffix} {'OK' if ok2 else 'FAILED'}"
                    _ui(f"{lbl} {_suffix.lower()} {'OK' if ok2 else 'FAILED'}")
                    for p in _panels:
                        self._q.put((p, time.time(),
                                     f"[Flash {'OK' if ok2 else 'FAILED'}]", "status"))
                        self._q.put((_UI_, time.time(), lambda p=p, t=txt, o=ok2:
                                     self._set_panel_status(p, t, o)))
                    if ok2:
                        if key == "lte":
                            self._current_coap = self._coap_mode_var.get()
                        elif key == "sensor":
                            self._current_ble = self._ble_mode_var.get()
                        self._q.put((_UI_, time.time(), self._update_mode_indicators))
                    if key == "sensor":
                        self._q.put((_UI_, time.time(), self._respawn_jlink))

                _stream_action("flash", panels, cwd, flash_cmd, self._q, done_cb=done)
            finally:
                if snr:
                    self._snr_release(snr)

        base_cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
        if pristine:
            west_idx = base_cmd.index("west")
            base_cmd = base_cmd[:west_idx + 2] + ["--pristine"] + base_cmd[west_idx + 2:]
        tag = f"build {'pristine ' if pristine else ''}{key}".strip()
        threading.Thread(
            target=_stream_action,
            args=(tag, tgt["panels"], tgt["cwd"], base_cmd, self._q),
            kwargs={"done_cb": after_build},
            daemon=True,
        ).start()

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_recording(self):
        if not self._recording:
            default = f"tracker_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                initialfile=default,
            )
            if not path:
                return
            self._csv_file   = open(path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(["timestamp", "source", "message"])
            self._recording  = True
            self._rec_btn.configure(text="⏹  Stop recording",
                                    bg="#3d1a1a", fg=PALETTE["RED"])
            self._csv_label.configure(text=path.split("/")[-1])
            self._status.configure(text=f"Recording → {path}")
        else:
            self._recording = False
            if self._csv_file:
                self._csv_file.close()
                self._csv_file   = None
                self._csv_writer = None
            self._rec_btn.configure(text="⏺  Start recording",
                                    bg=PALETTE["BG3"], fg=PALETTE["FG"])
            self._status.configure(text="Recording stopped")

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _copy_to_clipboard(self):
        n     = self._copy_lines.get()
        parts = []
        for src in SOURCES:
            if not self._copy_include[src].get():
                continue
            all_lines = self._texts[src].get("1.0", tk.END).splitlines()
            snippet   = all_lines[-n:] if len(all_lines) > n else all_lines
            parts.append(f"=== {src} (last {len(snippet)} lines) ===")
            parts.extend(snippet)
            parts.append("")
        if not parts:
            self._status.configure(text="Nothing selected to copy")
            return
        content = "\n".join(parts)
        self.clipboard_clear()
        self.clipboard_append(content)
        self._status.configure(text=f"Copied {content.count(chr(10))} lines to clipboard")

    # ── Clear ─────────────────────────────────────────────────────────────────

    def _clear_source(self, source: str):
        txt = self._texts.get(source)
        if txt:
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.configure(state=tk.DISABLED)
        self._line_count[source] = 0
        self._all_lines[source] = []

    def _clear_all(self):
        for src in _DEVICE_SOURCES:
            self._clear_source(src)

    # ── Close ─────────────────────────────────────────────────────────────────

    def on_close(self):
        self._stop.set()
        if self._csv_file:
            self._csv_file.close()
        # Terminate JLink procs without blocking the main thread on their exit.
        for proc in self._jlink_procs:
            try:
                proc.terminate()
            except Exception:
                pass
        self.destroy()


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tracker dev console")
    parser.add_argument("--ble",    help="BLE console port    (if00, auto-detected)")
    parser.add_argument("--lte",    help="LTE log port        (if02, auto-detected)")
    parser.add_argument("--sensor", help="Thingy:53 log port  (if00, auto-detected)")
    args = parser.parse_args()

    ble_port    = args.ble    or find_thingy91x_ports()[0]
    lte_port    = args.lte    or find_thingy91x_ports()[1]
    sensor_port = args.sensor or find_thingy53_port()

    app = LogViewer(ble_port, lte_port, sensor_port)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
