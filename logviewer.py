#!/usr/bin/env python3
"""
logviewer.py — tracker project dev console.

Features:
  • 3-panel log viewer: BLE (if00), LTE (if02), Thingy:53 RTT
  • Auto-spawns JLinkGDBServer for Thingy:53 RTT
  • Per-device: Reset, Build, Build Pristine, Flash
  • CSV recording, clipboard copy

Usage:
    python3 scripts/logviewer.py

Dependencies:
    pip install pyserial
"""

import argparse
import csv
import glob
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

import serial

# ── Paths ───────────────────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
_HUB_DIR     = _SCRIPTS_DIR.parent / "tracker-hub"
_SENSOR_DIR  = _SCRIPTS_DIR.parent / "tracker-sensor-node"

# ── J-Link server definitions ───────────────────────────────────────────────────

JLINK_SERVERS = [
    {
        "label":    "Thingy53",
        "device":   "nRF5340_XXAA_APP",
        "serial":   "1050337728",   # nRF52 DK
        "gdb_port": 2331,
        "rtt_port": 19021,
    },
    # Uncomment for hub-ble RTT (needs CONFIG_LOG_BACKEND_RTT=y in hub-ble prj.conf)
    # {
    #     "label":    "BLE",
    #     "device":   "nRF5340_XXAA_APP",
    #     "serial":   "1051217937",   # nRF9151 DK → Thingy:91X nRF5340
    #     "gdb_port": 2332,
    #     "rtt_port": 19022,
    # },
]

# ── Device action definitions ───────────────────────────────────────────────────
#
# build_cmd / flash_cmd are the bare west subcommand args.
# They are automatically wrapped with nrfutil sdk-manager toolchain launch.

NRFUTIL_WRAP = [
    "nrfutil", "sdk-manager", "toolchain", "launch",
    "--ncs-version", "v3.1.0", "--",
]

DEVICES = {
    "91X": {
        "label":     "Thingy:91X",
        "panels":    ["BLE", "LTE"],
        "snr":       "1051217937",
        "cwd":       str(_HUB_DIR),
        "build_cmd": [
            "west", "build",
            "-b", "thingy91x/nrf9151/ns",
            "apps/tracker-hub-lte",
            "--", "-DEXTRA_CONF_FILE=local.conf",
        ],
        "flash_cmd": ["west", "flash", "--recover"],
    },
    "53": {
        "label":     "Thingy:53",
        "panels":    ["Thingy53"],
        "snr":       "1050337728",
        "cwd":       str(_SENSOR_DIR),
        "build_cmd": [
            "west", "build",
            "-b", "thingy53/nrf5340/cpuapp",
            "tracker-node",
            "--", "-DEXTRA_CONF_FILE=local.conf",
        ],
        "flash_cmd": ["west", "flash"],
    },
}

# ── Display ──────────────────────────────────────────────────────────────────────

BAUD    = 115200
SOURCES = ["BLE", "LTE", "Thingy53"]

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
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Port auto-detection ─────────────────────────────────────────────────────────

def find_thingy91x_ports():
    pattern = "/dev/serial/by-id/usb-Nordic_Semiconductor_Thingy*91*"
    ports   = sorted(glob.glob(pattern))
    if00    = next((p for p in ports if p.endswith("-if00")), None)
    if02    = next((p for p in ports if p.endswith("-if02")), None)
    return if00, if02


# ── J-Link process management ───────────────────────────────────────────────────

def _find_jlink_server() -> str | None:
    for name in ("JLinkGDBServerCL", "JLinkGDBServer", "JLinkGDBServerExe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _monitor_jlink(srv: dict, proc: subprocess.Popen, q: queue.Queue):
    proc.wait()
    stderr_out = ""
    if proc.stderr:
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
    msg = f"[JLinkGDBServer exited (code {proc.returncode})"
    if stderr_out:
        msg += f": {stderr_out.splitlines()[0]}"
    msg += "]"
    q.put((srv["label"], time.time(), msg))


def spawn_jlink_servers(servers: list[dict], q: queue.Queue) -> list[subprocess.Popen]:
    exe = _find_jlink_server()
    if not exe:
        for srv in servers:
            q.put((srv["label"], time.time(),
                   "[JLinkGDBServer not found in PATH — RTT unavailable]"))
        return []
    procs = []
    for srv in servers:
        cmd = [
            exe,
            "-device",        srv["device"],
            "-if",            "SWD",
            "-speed",         "4000",
            "-SelectEmuBySN", srv["serial"],
            "-port",          str(srv["gdb_port"]),
            "-rtttelnetport", str(srv["rtt_port"]),
            "-nogui",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            procs.append(proc)
            q.put((srv["label"], time.time(),
                   f"[JLinkGDBServer PID {proc.pid} — RTT on :{srv['rtt_port']}]"))
            threading.Thread(target=_monitor_jlink, args=(srv, proc, q),
                             daemon=True).start()
        except Exception as e:
            q.put((srv["label"], time.time(),
                   f"[Failed to start JLinkGDBServer: {e}]"))
    return procs


def stop_jlink_servers(procs: list[subprocess.Popen]):
    for proc in procs:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass


# ── Reader threads ──────────────────────────────────────────────────────────────

def serial_reader(source: str, port: str, q: queue.Queue, stop: threading.Event):
    while not stop.is_set():
        try:
            with serial.Serial(port, BAUD, timeout=1) as ser:
                q.put((source, time.time(), f"[connected → {port}]"))
                buf = b""
                while not stop.is_set():
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = strip_ansi(
                            line.decode("utf-8", errors="replace").rstrip("\r"))
                        if text:
                            q.put((source, time.time(), text))
        except serial.SerialException as e:
            if not stop.is_set():
                q.put((source, time.time(),
                       f"[disconnected: {e} — retrying in 2 s]"))
                time.sleep(2)
        except Exception as e:
            if not stop.is_set():
                q.put((source, time.time(), f"[error: {e} — retrying in 2 s]"))
                time.sleep(2)


def rtt_reader(source: str, host: str, port: int,
               q: queue.Queue, stop: threading.Event,
               startup_delay: float = 2.5):
    time.sleep(startup_delay)
    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                q.put((source, time.time(), f"[RTT connected → {host}:{port}]"))
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
                                q.put((source, time.time(), text))
                    except socket.timeout:
                        continue
        except (ConnectionRefusedError, OSError) as e:
            if not stop.is_set():
                q.put((source, time.time(),
                       f"[RTT not ready ({e}) — retrying in 3 s]"))
                time.sleep(3)


# ── Device actions (reset / build / flash) ─────────────────────────────────────

def _stream_action(tag: str, panels: list[str], cwd: str,
                   cmd: list[str], q: queue.Queue,
                   done_cb=None):
    """Run cmd, stream its output to panels, call done_cb(ok: bool) when finished."""
    for p in panels:
        q.put((p, time.time(), f"[{tag}] $ {' '.join(cmd[-4:])}"))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                for p in panels:
                    q.put((p, time.time(), f"  {line}"))
        proc.wait()
        ok  = proc.returncode == 0
        msg = f"[{tag}: {'OK' if ok else f'FAILED (exit {proc.returncode})'}]"
        for p in panels:
            q.put((p, time.time(), msg))
        if done_cb:
            done_cb(ok)
    except Exception as e:
        for p in panels:
            q.put((p, time.time(), f"[{tag}: error: {e}]"))
        if done_cb:
            done_cb(False)


def reset_device(dev: dict, q: queue.Queue):
    exe = shutil.which("nrfjprog")
    if not exe:
        for p in dev["panels"]:
            q.put((p, time.time(), "[nrfjprog not found — cannot reset]"))
        return
    for p in dev["panels"]:
        q.put((p, time.time(), f"[pin reset → SNR {dev['snr']}]"))
    result = subprocess.run(
        [exe, "--reset", "--snr", dev["snr"]],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        for p in dev["panels"]:
            q.put((p, time.time(), f"[reset failed: {msg}]"))


# ── GUI ─────────────────────────────────────────────────────────────────────────

class LogViewer(tk.Tk):

    def __init__(self, ble_port, lte_port):
        super().__init__()
        self.title("Tracker Dev Console")
        self.geometry("1760x960")
        self.configure(bg=PALETTE["BG"])

        self._q           = queue.Queue()
        self._stop        = threading.Event()
        self._jlink_procs: list[subprocess.Popen] = []
        self._csv_file    = None
        self._csv_writer  = None
        self._recording   = False
        self._autoscroll  = {s: tk.BooleanVar(value=True) for s in SOURCES}
        self._line_count  = {s: 0 for s in SOURCES}
        self._action_btns: dict[str, list[tk.Button]] = {}  # dev_key → buttons

        self._build_ui()
        self._launch(ble_port, lte_port)
        self._poll()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Log panels ───────────────────────────────────────────────────────
        panels = tk.Frame(self, bg=PALETTE["BG"])
        panels.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 3))

        self._texts = {}
        for i, src in enumerate(SOURCES):
            fg  = SOURCE_COLOR[src]
            col = tk.Frame(panels, bg=PALETTE["BG2"])
            col.grid(row=0, column=i, sticky="nsew", padx=3)
            panels.columnconfigure(i, weight=1)
            panels.rowconfigure(0, weight=1)

            hdr = tk.Frame(col, bg=PALETTE["BG2"])
            hdr.pack(fill=tk.X, padx=4, pady=(4, 2))
            tk.Label(hdr, text=src, fg=fg, bg=PALETTE["BG2"],
                     font=("monospace", 11, "bold")).pack(side=tk.LEFT)
            tk.Checkbutton(
                hdr, text="auto-scroll",
                variable=self._autoscroll[src],
                fg=PALETTE["MUTE"], bg=PALETTE["BG2"],
                selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG2"], bd=0,
            ).pack(side=tk.RIGHT)

            txt = scrolledtext.ScrolledText(
                col, bg=PALETTE["BG"], fg=fg,
                font=("monospace", 9), wrap=tk.WORD,
                state=tk.DISABLED, bd=0, padx=4, pady=2,
            )
            txt.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))
            self._texts[src] = txt

        # ── Device action bar ─────────────────────────────────────────────────
        act = tk.Frame(self, bg=PALETTE["BG2"], pady=5)
        act.pack(fill=tk.X, padx=6, pady=(0, 3))

        for dev_key, dev in DEVICES.items():
            grp = tk.Frame(act, bg=PALETTE["BG2"])
            grp.pack(side=tk.LEFT, padx=(0, 20))

            tk.Label(grp, text=dev["label"], fg=PALETTE["FG"],
                     bg=PALETTE["BG2"],
                     font=("monospace", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))

            btns = []
            specs = [
                ("Reset",          lambda d=dev: self._do_reset(d)),
                ("Build",          lambda d=dev: self._do_build(d, pristine=False)),
                ("Build Pristine", lambda d=dev: self._do_build(d, pristine=True)),
                ("Flash",          lambda d=dev: self._do_flash(d)),
            ]
            for label, cmd in specs:
                b = tk.Button(
                    grp, text=label,
                    bg=PALETTE["BG3"], fg=PALETTE["FG"],
                    activebackground="#30363d", relief=tk.FLAT,
                    padx=8, pady=3, font=("monospace", 9),
                    command=cmd,
                )
                b.pack(side=tk.LEFT, padx=2)
                btns.append(b)

            self._action_btns[dev_key] = btns

        # ── Recording / clipboard bar ─────────────────────────────────────────
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
        self._csv_label.pack(side=tk.LEFT, padx=(0, 16))

        tk.Button(
            bar, text="Clear all",
            bg=PALETTE["BG3"], fg=PALETTE["FG"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=10, pady=5, command=self._clear_all,
        ).pack(side=tk.LEFT, padx=(0, 16))

        sep = tk.Frame(bar, bg=PALETTE["MUTE"], width=1, height=24)
        sep.pack(side=tk.LEFT, padx=(0, 12), fill=tk.Y)

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

    def _launch(self, ble_port, lte_port):
        for src, port in [("BLE", ble_port), ("LTE", lte_port)]:
            if port:
                threading.Thread(target=serial_reader,
                                 args=(src, port, self._q, self._stop),
                                 daemon=True).start()
            else:
                self._append(src, time.time(),
                             "[port not found — connect device and restart]")

        self._jlink_procs = spawn_jlink_servers(JLINK_SERVERS, self._q)
        for srv in JLINK_SERVERS:
            threading.Thread(
                target=rtt_reader,
                args=(srv["label"], "localhost", srv["rtt_port"],
                      self._q, self._stop),
                kwargs={"startup_delay": 2.5},
                daemon=True,
            ).start()

    # ── Message pump ─────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                source, ts, msg = self._q.get_nowait()
                self._append(source, ts, msg)
        except queue.Empty:
            pass
        self.after(40, self._poll)

    def _append(self, source: str, ts: float, msg: str):
        if source not in self._texts:
            return
        wall = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        line = f"{wall}  {msg}\n"

        txt = self._texts[source]
        txt.configure(state=tk.NORMAL)
        txt.insert(tk.END, line)
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

    # ── Device actions ────────────────────────────────────────────────────────

    def _set_action_btns(self, dev_key: str, enabled: bool):
        for b in self._action_btns.get(dev_key, []):
            b.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _do_reset(self, dev: dict):
        threading.Thread(
            target=reset_device, args=(dev, self._q), daemon=True,
        ).start()
        self._status.configure(text=f"Resetting {dev['label']}...")

    def _do_build(self, dev: dict, pristine: bool):
        dev_key = next(k for k, v in DEVICES.items() if v is dev)
        self._set_action_btns(dev_key, False)

        cmd = NRFUTIL_WRAP + dev["build_cmd"]
        if pristine:
            # insert --pristine before the first '--' separator
            sep = cmd.index("--", len(NRFUTIL_WRAP))
            cmd = cmd[:sep] + ["--pristine"] + cmd[sep:]

        tag = f"build{'  pristine' if pristine else ''}"
        self._status.configure(text=f"Building {dev['label']}...")

        def done(ok):
            self.after(0, lambda: self._set_action_btns(dev_key, True))
            self.after(0, lambda: self._status.configure(
                text=f"{dev['label']} build {'OK' if ok else 'FAILED'}"))

        threading.Thread(
            target=_stream_action,
            args=(tag, dev["panels"], dev["cwd"], cmd, self._q),
            kwargs={"done_cb": done},
            daemon=True,
        ).start()

    def _do_flash(self, dev: dict):
        dev_key = next(k for k, v in DEVICES.items() if v is dev)
        self._set_action_btns(dev_key, False)

        cmd = NRFUTIL_WRAP + dev["flash_cmd"]
        self._status.configure(text=f"Flashing {dev['label']}...")

        def done(ok):
            self.after(0, lambda: self._set_action_btns(dev_key, True))
            self.after(0, lambda: self._status.configure(
                text=f"{dev['label']} flash {'OK — resetting' if ok else 'FAILED'}"))
            if ok:
                self.after(500, lambda: self._do_reset(dev))

        threading.Thread(
            target=_stream_action,
            args=("flash", dev["panels"], dev["cwd"], cmd, self._q),
            kwargs={"done_cb": done},
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

    def _clear_all(self):
        for src, txt in self._texts.items():
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.configure(state=tk.DISABLED)
            self._line_count[src] = 0

    # ── Close ─────────────────────────────────────────────────────────────────

    def on_close(self):
        self._stop.set()
        if self._csv_file:
            self._csv_file.close()
        stop_jlink_servers(self._jlink_procs)
        self.destroy()


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tracker dev console")
    parser.add_argument("--ble", help="BLE console port (if00, auto-detected)")
    parser.add_argument("--lte", help="LTE mirror port  (if02, auto-detected)")
    args = parser.parse_args()

    ble_port, lte_port = args.ble, args.lte
    if not ble_port or not lte_port:
        auto_ble, auto_lte = find_thingy91x_ports()
        ble_port = ble_port or auto_ble
        lte_port = lte_port or auto_lte

    app = LogViewer(ble_port, lte_port)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
