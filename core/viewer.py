"""
viewer.py — LogViewer Tkinter GUI.

Imports everything it needs from the other modules; contains no
build/serial/SSH logic itself — only UI construction and event handling.
"""

import csv
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, scrolledtext
from typing import Any

from build_config import (
    NCS_VERSION, NRFUTIL_WRAP, TARGETS, _effective_build_cmd,
    _BLE_APP, _SENS_APP, SNR_SENSOR,
)
from serial_io import find_thingy91x_ports, find_thingy53_port, serial_reader
from server_io import SSH_SERVER_HOST, SSH_SERVER_CMD, ssh_log_reader, _ssh
from ui_constants import (
    BAUD, SOURCES, _DEVICE_SOURCES, PALETTE, SOURCE_COLOR, _RESET_LABEL,
    _UI_, LOG_LEVEL_COLOR, _MILESTONE_RE,
    _COAP_LABELS, _BLE_LABELS, _COAP_MODE_PAT, _BLE_MODE_PAT,
    _OSCORE_BLE_MODES, _OSCORE_COAP_MODES, _HUB_BLE_RELAY_FLAGS,
    _EVENT_PATTERNS,
    _dev_tag,
)
from kconfig_utils import _update_kconfig_key, _set_kconfig_mode, _set_kconfig_value
from utils import _dbg, _stream_action
import milestones as _MS


class LogViewer(tk.Tk):

    def __init__(self, ble_port, lte_port, sensor_port):
        super().__init__()
        self.title("Tracker Dev Console")
        self.configure(bg=PALETTE["BG"])
        self.update_idletasks()
        if sys.platform == "win32":
            self.state("zoomed")
        else:
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{w}x{h}+0+0")
            self.attributes("-zoomed", True)

        self._q           = queue.Queue()
        self._stop        = threading.Event()
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
        # programmer SNR exclusion — prevents simultaneous flash on same DK
        self._snr_busy: set[str] = set()
        self._snr_mu = threading.Lock()
        # quiet events suppress disconnect spam in serial_reader during flash
        self._snr_quiet: dict[str, threading.Event] = {
            snr: threading.Event()
            for snr in {tgt["snr"] for tgt in TARGETS.values() if tgt.get("snr")}
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
        # log watchers: background threads can wait for a pattern in a panel
        self._log_watchers: list = []   # [(source, pattern, threading.Event)]
        self._log_watcher_mu = threading.Lock()
        # test-run state
        self._test_running           = False
        self._cancel_event           = threading.Event()
        self._test_run_btn:      tk.Button | None = None
        self._suite_run_btn:     tk.Button | None = None
        self._smoke_run_btn:     tk.Button | None = None
        self._prebuild_btn:      tk.Button | None = None
        self._prebuilt_run_btn:  tk.Button | None = None
        self._build_suite_btn:   tk.Button | None = None
        self._cancel_btn:        tk.Button | None = None
        self._prebuild_session                    = None
        self._prebuild_status_lbl: tk.Label | None = None
        self._test_status_lbl:   tk.Label | None = None
        self._milestone_win_proc: subprocess.Popen | None = None
        self._live_log: Any = None   # active EventLog during a test; read from Tk thread, set by test thread
        self._test_mode_lbl:     tk.Label | None = None
        self._wrong_dtls_var         = tk.BooleanVar(value=False)
        self._wrong_oscore_hub_var   = tk.BooleanVar(value=False)
        self._wrong_oscore_sensor_var = tk.BooleanVar(value=False)

        self._build_ui()
        # initialise mode selectors from local.conf
        c = self._read_coap_mode()
        b = self._read_ble_mode()
        self._current_coap = c
        self._current_ble  = b
        self._coap_mode_var.set(c)
        self._ble_mode_var.set(b)
        self._on_ble_mode_changed()   # sets initial radio states + indicators
        self._update_test_mode_label()
        self._coap_mode_var.trace_add("write", lambda *_: (self._update_mode_indicators(),
                                                            self._update_test_mode_label()))
        self._ble_mode_var.trace_add("write",  lambda *_: (self._on_ble_mode_changed(),
                                                            self._update_test_mode_label()))

        self._launch(ble_port, lte_port, sensor_port)
        self._poll()
        self.after(200, self._refresh_prebuild_status)

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
            is_events = (src == "Events")

            if is_server:
                server_row = tk.Frame(panels_frame, bg=PALETTE["BG"])
                server_row.grid(row=0, column=0, columnspan=4, sticky="nsew",
                                padx=3, pady=(0, 3))
                col = tk.Frame(server_row, bg=PALETTE["BG2"])
                col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
            elif is_events:
                col = tk.Frame(server_row, bg=PALETTE["BG2"])
                col.pack(side=tk.LEFT, fill=tk.BOTH, padx=(2, 0))
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
            if not is_server and not is_events:
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

            if is_events:
                ev_btns = tk.Frame(col, bg=PALETTE["BG2"])
                ev_btns.pack(fill=tk.X, padx=4, pady=(0, 2))
                tk.Button(ev_btns, text="Clear", padx=6, pady=2,
                          bg=PALETTE["BG3"], fg=fg,
                          activebackground="#30363d", relief=tk.FLAT,
                          font=("monospace", 9),
                          command=lambda: self._clear_source("Events"),
                          ).pack(side=tk.LEFT)

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
                if level == "milestone":
                    txt.tag_configure(level, foreground=color, font=("monospace", 9, "bold"))
                else:
                    txt.tag_configure(level, foreground=color)
            txt.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))
            self._texts[src] = txt

            if not is_server and not is_events:
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

        # ── Device action bar ────────────────────────────────────────────────
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

        # BLE column
        ble_col = tk.Frame(act, bg=PALETTE["BG"])
        ble_col.grid(row=0, column=1, sticky="w", padx=3, pady=2)
        ble_r0 = tk.Frame(ble_col, bg=PALETTE["BG"])
        ble_r0.pack(fill=tk.X)
        cbtn(ble_r0, "Build",          lambda: self._do_build("ble"),                          "ble", "BLE")
        cbtn(ble_r0, "Build Pristine", lambda: self._do_build("ble", pristine=True),           "ble", "BLE")
        cbtn(ble_r0, "Flash",          lambda: self._do_flash("ble"),                          "ble", "BLE")
        ble_r1 = tk.Frame(ble_col, bg=PALETTE["BG"])
        ble_r1.pack(fill=tk.X)
        cbtn(ble_r1, "Build & Flash",          lambda: self._do_build_and_flash("ble"),                "ble", "BLE")
        cbtn(ble_r1, "Build Pristine & Flash", lambda: self._do_build_and_flash("ble", pristine=True), "ble", "BLE")

        # LTE column
        lte_col = tk.Frame(act, bg=PALETTE["BG"])
        lte_col.grid(row=0, column=0, sticky="w", padx=(6, 3), pady=2)
        lte_r0 = tk.Frame(lte_col, bg=PALETTE["BG"])
        lte_r0.pack(fill=tk.X)
        cbtn(lte_r0, "Build",          lambda: self._do_build("lte"),                          "lte", "LTE")
        cbtn(lte_r0, "Build Pristine", lambda: self._do_build("lte", pristine=True),           "lte", "LTE")
        cbtn(lte_r0, "Flash",          lambda: self._do_flash("lte"),                          "lte", "LTE")
        lte_r1 = tk.Frame(lte_col, bg=PALETTE["BG"])
        lte_r1.pack(fill=tk.X)
        cbtn(lte_r1, "Build & Flash",          lambda: self._do_build_and_flash("lte"),                "lte", "LTE")
        cbtn(lte_r1, "Build Pristine & Flash", lambda: self._do_build_and_flash("lte", pristine=True), "lte", "LTE")

        # Sensor column
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

        # ── Test run bar ─────────────────────────────────────────────────────
        test_bar = tk.Frame(self, bg=PALETTE["BG"], pady=2)
        test_bar.pack(fill=tk.X, padx=6, pady=(0, 2))

        tk.Label(test_bar, text="Run Test:", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
                 font=("monospace", 9, "bold")).pack(side=tk.LEFT, padx=(0, 4))

        self._test_mode_lbl = tk.Label(
            test_bar, text="…", fg=PALETTE["FG"], bg=PALETTE["BG"],
            font=("monospace", 9))
        self._test_mode_lbl.pack(side=tk.LEFT, padx=(0, 10))

        tk.Frame(test_bar, bg=PALETTE["MUTE"], width=1, height=16).pack(
            side=tk.LEFT, padx=(0, 8), fill=tk.Y)

        for chk_text, chk_var in [
            ("Wrong DTLS",         self._wrong_dtls_var),
            ("Wrong OSCORE (hub)", self._wrong_oscore_hub_var),
            ("Wrong OSCORE (sens)", self._wrong_oscore_sensor_var),
        ]:
            tk.Checkbutton(
                test_bar, text=chk_text, variable=chk_var,
                fg=PALETTE["RED"], bg=PALETTE["BG"],
                selectcolor=PALETTE["BG3"],
                activebackground=PALETTE["BG"],
                font=("monospace", 9), bd=0,
            ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Frame(test_bar, bg=PALETTE["MUTE"], width=1, height=16).pack(
            side=tk.LEFT, padx=(0, 8), fill=tk.Y)

        self._test_run_btn = tk.Button(
            test_bar, text="▶ Run Test",
            bg="#162a18", fg=SOURCE_COLOR["Thingy53"],
            activebackground="#1e3820", relief=tk.FLAT,
            padx=10, pady=3, font=("monospace", 9, "bold"),
            command=self._run_test,
        )
        self._test_run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._suite_run_btn = tk.Button(
            test_bar, text="▶ Suite",
            bg="#1a1a2e", fg="#9ECBFF",
            activebackground="#22223a", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9, "bold"),
            command=self._run_suite,
        )
        self._suite_run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._smoke_run_btn = tk.Button(
            test_bar, text="▶ Smoke",
            bg="#1a1a2e", fg="#79c0ff",
            activebackground="#22223a", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9),
            command=self._run_smoke,
        )
        self._smoke_run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._prebuilt_run_btn = tk.Button(
            test_bar, text="▶ Prebuilt",
            bg="#1a2a1a", fg="#79c0ff",
            activebackground="#223022", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9, "bold"),
            command=self._run_prebuilt_suite,
        )
        self._prebuilt_run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._prebuild_btn = tk.Button(
            test_bar, text="⚙ Prebuild",
            bg="#1a2a1a", fg="#57ab5a",
            activebackground="#223022", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9),
            command=self._prebuild_suite,
        )
        self._prebuild_btn.pack(side=tk.LEFT, padx=(0, 2))

        self._prebuild_status_lbl = tk.Label(
            test_bar, text="", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
            font=("monospace", 8))
        self._prebuild_status_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self._build_suite_btn = tk.Button(
            test_bar, text="⚙▶ Build+Suite",
            bg="#1a1a2e", fg="#a371f7",
            activebackground="#22223a", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9, "bold"),
            command=self._prebuild_then_suite,
        )
        self._build_suite_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._cancel_btn = tk.Button(
            test_bar, text="✕ Cancel",
            bg="#2a1010", fg=PALETTE["RED"],
            activebackground="#3a1818", relief=tk.FLAT,
            padx=8, pady=3, font=("monospace", 9, "bold"),
            state=tk.DISABLED,
            command=self._cancel_test,
        )
        self._cancel_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._test_status_lbl = tk.Label(
            test_bar, text="", fg=PALETTE["MUTE"], bg=PALETTE["BG"],
            font=("monospace", 9))
        self._test_status_lbl.pack(side=tk.LEFT)

        # ── Bottom bar ───────────────────────────────────────────────────────
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
            padx=8, pady=5,
            command=lambda: self._do_reset(SNR_SENSOR, "53", ["Thingy53"]),
        ).pack(side=tk.LEFT, padx=(0, 4))

        tk.Button(
            bar, text="📍 Location",
            bg=PALETTE["BG3"], fg=SOURCE_COLOR["LTE"],
            activebackground="#30363d", relief=tk.FLAT,
            padx=8, pady=5, command=self._trigger_location,
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
        q91 = self._snr_quiet[TARGETS["lte"]["snr"]]
        q53 = self._snr_quiet[TARGETS["sensor"]["snr"]]

        serial_ports = [
            ("BLE",      ble_port,    q91),
            ("LTE",      lte_port,    q91),
            ("Thingy53", sensor_port, q53),
        ]
        for src, port, quiet in serial_ports:
            wq = queue.Queue()
            self._write_queues[src] = wq
            if port:
                threading.Thread(target=serial_reader,
                                 args=(src, port, self._q, self._stop),
                                 kwargs={"quiet": quiet, "write_q": wq},
                                 daemon=True).start()
            else:
                self._append(src, time.time(),
                             "[port not found — waiting for device…]")

        threading.Thread(
            target=ssh_log_reader,
            args=("Server", SSH_SERVER_HOST, SSH_SERVER_CMD, self._q, self._stop),
            daemon=True,
        ).start()
        self._fetch_server_branch()

        missing = [(src, quiet)
                   for src, port, quiet in serial_ports if not port]
        if missing:
            threading.Thread(target=self._hotplug_watcher, args=(missing,),
                             daemon=True).start()

    def _hotplug_watcher(self, missing: list):
        find_fns = {
            "BLE":      lambda: find_thingy91x_ports()[0],
            "LTE":      lambda: find_thingy91x_ports()[1],
            "Thingy53": find_thingy53_port,
        }
        pending = {src: (find_fns[src], quiet) for src, quiet in missing}
        while not self._stop.is_set() and pending:
            time.sleep(1.0)
            for src in list(pending):
                find_fn, quiet = pending[src]
                port = find_fn()
                if port:
                    del pending[src]
                    wq = self._write_queues[src]
                    threading.Thread(
                        target=serial_reader,
                        args=(src, port, self._q, self._stop),
                        kwargs={"quiet": quiet, "write_q": wq},
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
            txt = self._texts[source]
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

        if source != "Events":
            # Forward firmware UART milestone strings to the active test live log.
            # self._live_log is set by _run_test_thread (background thread); Python GIL
            # makes the reference read atomic.  EventLog.log() is lock-protected.
            if kind == "dev":
                _ll = self._live_log
                if _ll is not None:
                    for _ms in _MS.FIRMWARE_MILESTONES:
                        if _ms in msg:
                            _ll.log(_ms)
                            break
                    else:
                        # Timesync: firmware emits "Timesync sent to BLE central: …"
                        if source == "LTE" and "Timesync sent to BLE" in msg:
                            _ll.log(_MS.LTE_TIMESYNC_SENT)

            m = _MILESTONE_RE.search(msg)
            if m:
                # Forward the milestone string verbatim — extract from the match
                # position so it works even when wrapped in a Zephyr log prefix.
                self._emit_event(msg[m.start():].split("\n")[0].strip())
            else:
                for pat, label in _EVENT_PATTERNS:
                    if pat.search(msg):
                        self._emit_event(f"[{source}] {label}")
                        break
            with self._log_watcher_mu:
                for wsrc, wpat, wev in self._log_watchers:
                    if wsrc == source and wpat.search(msg):
                        wev.set()

    def _emit_event(self, msg: str):
        self._q.put(("Events", time.time(), msg, "status"))

    def _register_log_watcher(self, source: str, pattern) -> threading.Event:
        """Register a watcher immediately and return its Event.
        Call this before starting the wait so events that fire early are not missed."""
        import re as _re
        if isinstance(pattern, str):
            pattern = _re.compile(pattern)
        ev = threading.Event()
        with self._log_watcher_mu:
            self._log_watchers.append((source, pattern, ev))
        return ev

    def _finish_log_watcher(self, ev: threading.Event, timeout: float) -> bool:
        """Wait for a previously registered watcher event. Removes it when done.
        Polls _cancel_event every 250 ms so a cancel request breaks long waits quickly."""
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if self._cancel_event.is_set():
                    return False
                if ev.wait(min(remaining, 0.25)):
                    return True
        finally:
            with self._log_watcher_mu:
                self._log_watchers[:] = [w for w in self._log_watchers if w[2] is not ev]

    def _wait_for_log(self, source: str, pattern, timeout: float) -> bool:
        """Register watcher and block until *pattern* appears, or *timeout* expires."""
        ev = self._register_log_watcher(source, pattern)
        return self._finish_log_watcher(ev, timeout)

    def _cancel_test(self):
        """User clicked Cancel — signal all test/suite threads to stop."""
        self._cancel_event.set()
        if self._cancel_btn:
            self._cancel_btn.config(state=tk.DISABLED)
        self._test_status_lbl.config(text="cancelling…", fg=PALETTE["RED"])
        self._emit_event("[Test] Cancelled by user")

    def _make_suite_folder(self, suite_name: str, total: int) -> "tuple[Path, Path]":
        """Create timestamped suite folder under testruns/, write live file header."""
        ts         = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        suite_path = Path(__file__).parent.parent / "testruns" / f"{ts}_suite-{suite_name}"
        suite_path.mkdir(parents=True, exist_ok=True)
        live_path  = suite_path / "milestone_live.txt"
        live_path.write_text(f"[Suite] {total}  {ts}  {suite_name}\n", encoding="utf-8")
        return suite_path, live_path

    def _launch_milestone_window(self, live_path: Path):
        """Spawn the milestone tracker window as a detached background process."""
        import sys as _sys
        if self._milestone_win_proc and self._milestone_win_proc.poll() is None:
            return  # already running
        self._milestone_win_proc = subprocess.Popen(
            [_sys.executable, str(Path(__file__).parent / "milestone_window.py"), str(live_path)],
            start_new_session=True,
        )

    def _update_test_mode_label(self):
        if self._test_mode_lbl:
            b = self._ble_mode_var.get()
            c = self._coap_mode_var.get()
            self._test_mode_lbl.config(text=f"{b} + {c}")

    def _run_test(self):
        if self._test_running:
            return
        ble  = self._ble_mode_var.get()
        coap = self._coap_mode_var.get()
        wrong = frozenset(filter(None, [
            "wrong_dtls"          if self._wrong_dtls_var.get()          else None,
            "wrong_oscore_hub"    if self._wrong_oscore_hub_var.get()    else None,
            "wrong_oscore_sensor" if self._wrong_oscore_sensor_var.get() else None,
        ]))
        suite_path, live_path = self._make_suite_folder("single", 1)
        self._launch_milestone_window(live_path)

        self._cancel_event.clear()
        self._test_running = True
        self._test_run_btn.config(state=tk.DISABLED, text="● Running…")
        self._suite_run_btn.config(state=tk.DISABLED)
        self._smoke_run_btn.config(state=tk.DISABLED)
        self._prebuilt_run_btn.config(state=tk.DISABLED)
        self._build_suite_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)
        self._test_status_lbl.config(text="starting…", fg=PALETTE["MUTE"])
        self._emit_event(f"Test started: {ble} + {coap}")

        def _run():
            try:
                self._run_test_thread(ble, coap, wrong, suite_path=suite_path,
                                      live_path=live_path, idx=1, write_suite_summary=True)
            except Exception as e:
                self._emit_event(f"Test ERROR: {e}")
            finally:
                self._test_running = False
                self.after(0, lambda: (
                    self._test_run_btn.config(state=tk.NORMAL, text="▶ Run Test"),
                    self._suite_run_btn.config(state=tk.NORMAL),
                    self._smoke_run_btn.config(state=tk.NORMAL),
                    self._prebuilt_run_btn.config(state=tk.NORMAL),
                    self._build_suite_btn.config(state=tk.NORMAL),
                    self._cancel_btn.config(state=tk.DISABLED),
                    self._test_status_lbl.config(text="", fg=PALETTE["MUTE"]),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _run_test_thread(self, ble_mode: str, coap_mode: str, wrong_flags: frozenset,
                         prebuild_session=None, suite_path: Path | None = None,
                         live_path: Path | None = None, idx: int | None = None,
                         total: int = 1,
                         write_suite_summary: bool = False):
        import sys as _sys
        _here = Path(__file__).parent.resolve()
        if str(_here) not in _sys.path:
            _sys.path.insert(0, str(_here))

        import runtest as _rt

        def _status(msg: str):
            self._test_status_lbl.config(text=msg)
            self._emit_event(msg)

        log = _rt.EventLog(live_path=live_path)
        self._live_log = log   # expose to _append() for UART milestone forwarding

        # Validate pairing
        from ui_constants import _OSCORE_BLE_MODES, _OSCORE_COAP_MODES
        if (ble_mode in _OSCORE_BLE_MODES) and (coap_mode not in _OSCORE_COAP_MODES):
            _status("Invalid pairing — BLE OSCORE requires CoAP OSCORE")
            return

        run_dir = _rt.RunDir(ble_mode, coap_mode, wrong_flags, suite_path=suite_path, idx=idx)
        # Record panel buffer offsets so we only save lines from this test onwards.
        log_offsets = {src: len(self._all_lines.get(src, [])) for src in ("BLE", "LTE", "Thingy53")}
        label   = f"{ble_mode} + {coap_mode}"
        if wrong_flags:
            label += f"  [{', '.join(wrong_flags)}]"
        log.log(f"{_MS.T_SCENARIO} {idx or 1}/{total}: {ble_mode} + {coap_mode}")
        log.log(f"Results → testruns/{run_dir.path.name}")
        t_start = time.time()

        # Write configs
        _status("writing configs…")
        _rt._setup_configs(ble_mode, coap_mode, wrong_flags, log)
        log.log(_MS.T_WRITING_CONFIGS)
        rebuild_sensor = True

        # Snapshot configs
        from build_config import _BLE_APP, _SENS_APP
        for src, dst in [
            (_BLE_APP  / "security.conf", "ble_security.conf"),
            (_SENS_APP / "security.conf", "sensor_security.conf"),
        ]:
            if src.exists():
                run_dir.config_path(dst).write_bytes(src.read_bytes())

        # Update server env
        _rt._ssh_update_env("SECURITY_MODE", coap_mode)

        # Phase 1: Reset LTE via write queue, parallel build + server restart
        _status("reset LTE…")
        log.log(_MS.T_RESETTING_LTE)
        log.log("BLE→LTE: sending reset lte via logviewer write queue")
        self._write_queues["BLE"].put(b"reset lte\r\n")
        time.sleep(0.5)

        def _stream_build(key: str, pristine: bool = True) -> bool:
            tgt  = TARGETS[key]
            cmd  = NRFUTIL_WRAP + _effective_build_cmd(tgt)
            if pristine:
                wi  = cmd.index("west")
                cmd = cmd[:wi + 2] + ["--pristine"] + cmd[wi + 2:]
            ok_box = [False]
            _stream_action(
                f"build {key}", tgt["panels"], tgt["cwd"], cmd, self._q,
                done_cb=lambda ok: ok_box.__setitem__(0, ok),
            )
            if ok_box[0]:
                self._emit_event(f"{tgt['label']}: build complete")
            return ok_box[0]

        def _stream_flash(key: str) -> bool:
            tgt    = TARGETS[key]
            snr    = tgt.get("snr")
            panels = tgt["panels"]
            if snr and not self._snr_try_acquire(snr, panels):
                log.log(f"Flash blocked — programmer {snr} busy")
                return False
            try:
                for pre_cmd in tgt.get("pre_flash_cmds", []):
                    r = subprocess.run(pre_cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        msg = r.stderr.strip() or r.stdout.strip()
                        for p in panels:
                            self._q.put((p, time.time(), f"[pre-flash failed: {msg}]", "build"))
                        return False
                flash_cmd = NRFUTIL_WRAP + tgt["flash_cmd"]
                ok_box = [False]
                _stream_action(
                    f"flash {key}", panels, tgt["cwd"], flash_cmd, self._q,
                    done_cb=lambda ok: ok_box.__setitem__(0, ok),
                )
                if ok_box[0]:
                    self._emit_event(f"{tgt['label']}: flashed")
                return ok_box[0]
            finally:
                if snr:
                    self._snr_release(snr)

        def _stream_flash_from_dir(key: str, build_dir) -> bool:
            tgt    = TARGETS[key]
            snr    = tgt.get("snr")
            panels = tgt["panels"]
            if snr and not self._snr_try_acquire(snr, panels):
                log.log(f"Flash blocked — programmer {snr} busy")
                return False
            try:
                for pre_cmd in tgt.get("pre_flash_cmds", []):
                    r = subprocess.run(pre_cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        msg = r.stderr.strip() or r.stdout.strip()
                        for p in panels:
                            self._q.put((p, time.time(), f"[pre-flash failed: {msg}]", "build"))
                        return False
                flash_cmd = list(tgt["flash_cmd"])
                bd_idx = flash_cmd.index("--build-dir")
                flash_cmd[bd_idx + 1] = str(build_dir)
                ok_box = [False]
                _stream_action(
                    f"flash {key} (prebuilt)", panels, tgt["cwd"],
                    NRFUTIL_WRAP + flash_cmd, self._q,
                    done_cb=lambda ok: ok_box.__setitem__(0, ok),
                )
                if ok_box[0]:
                    self._emit_event(f"{tgt['label']}: flashed (prebuilt)")
                return ok_box[0]
            finally:
                if snr:
                    self._snr_release(snr)

        build_ok: dict[str, bool] = {}
        use_prebuild_sensor = False
        use_prebuild_ble    = False

        # ── Prebuild-aware build resolution ───────────────────────────────────
        if prebuild_session is not None and not wrong_flags:
            sensor_status = prebuild_session.get_sensor_status(ble_mode)
            ble_status    = prebuild_session.get_ble_status(ble_mode, coap_mode)

            if sensor_status in ("building", "pending"):
                _status("waiting for prebuild: sensor…")
                log.log(f"Sensor [{ble_mode}]: waiting for prebuild…")
                sensor_status = prebuild_session.wait_for_sensor(ble_mode)
            if ble_status in ("building", "pending"):
                _status("waiting for prebuild: BLE…")
                log.log(f"BLE [{_rt._ble_variant_key(ble_mode, coap_mode)}]: waiting for prebuild…")
                ble_status = prebuild_session.wait_for_ble(ble_mode, coap_mode)

            if sensor_status == "failed":
                _status("Prebuild sensor FAILED")
                log.log("Sensor prebuild FAILED — aborting scenario")
                run_dir.write_timeline(log)
                return False
            if ble_status == "failed":
                _status("Prebuild BLE FAILED")
                log.log("BLE prebuild FAILED — aborting scenario")
                run_dir.write_timeline(log)
                return False

            if (sensor_status == "done" and
                    _rt._prebuild_artifact_ready(prebuild_session.sensor_dirs[ble_mode])):
                use_prebuild_sensor = True
                build_ok["sensor"]  = True
                log.log(f"Sensor: using prebuilt [{prebuild_session.sensor_dirs[ble_mode].name}]")
            if (ble_status == "done" and
                    _rt._prebuild_artifact_ready(
                        prebuild_session.ble_dirs[_rt._ble_variant_key(ble_mode, coap_mode)])):
                use_prebuild_ble = True
                build_ok["ble"]  = True
                log.log(f"BLE: using prebuilt [{prebuild_session.ble_dirs[_rt._ble_variant_key(ble_mode, coap_mode)].name}]")

        def _do_build_ble():
            build_ok["ble"] = _stream_build("ble", pristine=True)

        def _do_build_sensor():
            build_ok["sensor"] = _stream_build("sensor", pristine=True)

        def _do_restart_server():
            build_ok["server"] = _rt._ssh_restart_server(log)

        build_threads = []
        if not use_prebuild_ble:
            build_threads.append(threading.Thread(target=_do_build_ble, daemon=True))
        if not use_prebuild_sensor:
            if rebuild_sensor:
                log.log(f"Sensor: rebuild ({ble_mode})")
                build_threads.append(threading.Thread(target=_do_build_sensor, daemon=True))
            else:
                build_ok["sensor"] = True
                log.log("Sensor: skipping rebuild (unchanged)")
        build_threads.append(threading.Thread(target=_do_restart_server, daemon=True))

        _status("building…" if build_threads[:-1] else "restarting server…")
        for t in build_threads:
            t.start()

        log.log(f"Waiting {_rt._WAIT_LTE_REBOOT} s for LTE reboot while builds run…")
        if self._cancel_event.wait(_rt._WAIT_LTE_REBOOT):
            _status("Cancelled")
            run_dir.write_timeline(log)
            return False

        for t in build_threads:
            t.join()

        if self._cancel_event.is_set():
            _status("Cancelled")
            run_dir.write_timeline(log)
            return False

        if not build_ok.get("ble"):
            _status("BLE build FAILED")
            log.log("BLE build FAILED — aborting")
            run_dir.write_timeline(log)
            return False
        if not build_ok.get("sensor"):
            _status("Sensor build FAILED")
            log.log("Sensor build FAILED — aborting")
            run_dir.write_timeline(log)
            return False

        # Phase 2: Flash sensor + BLE in parallel (SNR_SENSOR vs SNR_HUB — different programmers)
        gatt_mode = ble_mode in ("gatt", "lesc", "gatt_oscore")

        _status("flashing sensor + BLE…")
        _flash_ok: dict[str, bool] = {}

        def _do_flash_sensor_parallel():
            if rebuild_sensor or use_prebuild_sensor:
                log.log(_MS.SENSOR_FLASH_STARTED)
                if use_prebuild_sensor:
                    ok = _stream_flash_from_dir(
                        "sensor", prebuild_session.sensor_dirs[ble_mode])
                else:
                    ok = _stream_flash("sensor")
                _flash_ok["sensor"] = ok
                if ok:
                    log.log(_MS.SENSOR_FLASHED)
            else:
                _flash_ok["sensor"] = True

        def _do_flash_ble_parallel():
            log.log(_MS.BLE_FLASH_STARTED)
            if use_prebuild_ble:
                ok = _stream_flash_from_dir(
                    "ble",
                    prebuild_session.ble_dirs[_rt._ble_variant_key(ble_mode, coap_mode)],
                )
            else:
                ok = _stream_flash("ble")
            _flash_ok["ble"] = ok
            if ok:
                log.log(_MS.BLE_FLASHED)

        _ts = threading.Thread(target=_do_flash_sensor_parallel, daemon=True)
        _tb = threading.Thread(target=_do_flash_ble_parallel,    daemon=True)
        _ts.start(); _tb.start()
        _ts.join();  _tb.join()

        if not _flash_ok.get("sensor", True):
            _status("Sensor flash FAILED")
            run_dir.write_timeline(log)
            return False
        if not _flash_ok.get("ble"):
            _status("BLE flash FAILED")
            run_dir.write_timeline(log)
            return False

        if self._cancel_event.is_set():
            _status("Cancelled")
            run_dir.write_timeline(log)
            return False

        _rt._print_ble_ready(ble_mode, coap_mode, log)
        self._emit_event(f"BLE flashed ({ble_mode} / {coap_mode})")

        # Phase 3: Event-driven readiness gates
        # Register ALL watchers before blocking on any of them so that events
        # arriving before a later wait is reached are not missed.
        import re as _re
        _ev_ble = self._register_log_watcher(
            "BLE", _re.compile(r"BLE scan starting|BLE scan active|BLE scan started|Scanning for sensor"))
        _ev_lte = self._register_log_watcher(
            "LTE", _re.compile(r"Connected to Cloud|Custom CoAP connection successful"))
        _ev_sensor = self._register_log_watcher(
            "Thingy53", _re.compile(r"Hub connected")) if gatt_mode else None

        # 3a: BLE scan active
        _status("waiting: BLE scan active…")
        log.log("Waiting for BLE: scan active (up to 30 s)…")
        ble_ready = self._finish_log_watcher(_ev_ble, 30)
        if ble_ready:
            log.log("BLE: scan active — hub is searching for sensor")
            self._emit_event("BLE: scan active")
        else:
            log.log("WARNING: BLE scan active not seen within 30 s (continuing)")

        # 3b: LTE connected to cloud
        _status("waiting: LTE connected to cloud…")
        log.log("Waiting for LTE: Connected to Cloud (up to 90 s)…")
        lte_ready = self._finish_log_watcher(_ev_lte, 90)
        if lte_ready:
            log.log("LTE: connected to cloud")
            self._emit_event("LTE: connected to cloud")
        else:
            log.log("WARNING: LTE cloud connection not seen within 90 s (continuing)")
            self._emit_event("WARNING: LTE cloud connection timeout")

        # 3c: For GATT modes, wait for sensor to connect to hub
        if gatt_mode:
            _status("waiting: sensor connected to hub…")
            log.log("Waiting for sensor BLE connection (up to 60 s)…")
            sensor_conn = self._finish_log_watcher(_ev_sensor, 60)
            if sensor_conn:
                log.log(_MS.BLE_SENSOR_CONNECTED)
                log.log("Sensor: hub connected")
                self._emit_event("Sensor: hub connected")
            else:
                log.log("WARNING: sensor BLE connection not seen within 60 s (continuing)")
                self._emit_event("WARNING: sensor BLE connection timeout")

        # Print server context now that everything is up
        _rt._print_server_ready(log)
        env = _rt._ssh_read_env()
        run_dir.config_path("server_env.txt").write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(env.items())), encoding="utf-8"
        )

        # Phase 4a: Location — send first, wait for fix, wait for its own ACK.
        # Registering _ev_location_coap before sending ensures we don't miss a fast response.
        _ev_location      = self._register_log_watcher("LTE", r"location: Wi-Fi")
        _ev_location_coap = self._register_log_watcher("LTE", r"CoAP response")

        _status("triggering location search…")
        log.log("BLE: sending att_location search → nRF9151 IPC")
        self._write_queues["BLE"].put(b"att_location search\r\n")
        self._emit_event("BLE: att_location search sent")

        _status("waiting: LTE location fix…")
        location_seen = self._finish_log_watcher(_ev_location, 60)
        if location_seen:
            log.log(_MS.LTE_LOCATION_SEARCH_STARTED)
            log.log(_MS.LTE_LOCATION_FIX_WIFI)
            log.log("LTE: location fix seen (Wi-Fi) ✓")
            self._emit_event("LTE: location fix active")
        else:
            log.log("WARNING: location fix not seen in LTE within 60 s")
            self._emit_event("WARNING: location timeout")

        _status("waiting: LTE location CoAP ACK…")
        location_coap_ok = self._finish_log_watcher(_ev_location_coap, 30)
        if location_coap_ok:
            log.log(_MS.LTE_LOCATION_ACK_RECEIVED)
            log.log("LTE: Location CoAP ACK received ✓")
            self._emit_event("LTE: location CoAP ACK")
        else:
            log.log("WARNING: no location CoAP ACK seen in LTE within 30 s")
            self._emit_event("WARNING: location CoAP ACK timeout")

        # Phase 4b: Sensor data — after location is confirmed, trigger sample + wait for its ACK.
        # Registering both watchers before sending avoids missing fast events.
        # For OSCORE relay modes the hub logs "[LTE] OSCORE relay ACK" rather than "CoAP response".
        env_pat  = r"OSCORE relay.*forwarded to server" if ble_mode in _OSCORE_BLE_MODES else r"BLE env\b"
        coap_pat = r"OSCORE relay ACK" if ble_mode in _OSCORE_BLE_MODES else r"CoAP response"
        _ev_env     = self._register_log_watcher("LTE", env_pat)
        _ev_coap_ok = self._register_log_watcher("LTE", coap_pat)

        _status("triggering att_sample…")
        log.log("Sensor: sending att_sample via logviewer write queue")
        self._write_queues["Thingy53"].put(b"att_sample\r\n")

        _status("waiting: LTE received Thingy53 data…")
        env_seen = self._finish_log_watcher(_ev_env, 30)
        if env_seen:
            # For OSCORE relay the firmware log differs from LTE_SAMPLE_RECEIVED constant;
            # log it explicitly so _append() UART forwarding isn't relied upon for this path.
            if ble_mode in _OSCORE_BLE_MODES:
                log.log(_MS.LTE_SAMPLE_RECEIVED)
            log.log("LTE: Thingy53 env data received ✓")
            self._emit_event("LTE: Thingy53 env received")
        else:
            log.log("WARNING: Thingy53 env not seen at LTE within 30 s")
            self._emit_event("WARNING: env timeout")

        _status("waiting: LTE CoAP response from server…")
        coap_ok = self._finish_log_watcher(_ev_coap_ok, 60)
        if coap_ok:
            log.log(_MS.LTE_ACK_RECEIVED)
            log.log("LTE: CoAP response received (server acknowledged sensor payload) ✓")
            self._emit_event("LTE: CoAP response OK")
        else:
            log.log("WARNING: no CoAP response seen in LTE within 60 s")
            self._emit_event("WARNING: CoAP response timeout")

        # Phase 5: Fetch server logs for the record
        elapsed = int(time.time() - t_start) + 5
        r = _rt._ssh(
            f"cd tracker-server && docker compose logs --since {elapsed}s coap-server 2>&1",
            timeout=15,
        )
        server_log = r.stdout.strip()
        run_dir.log_path("server.txt").write_text(server_log, encoding="utf-8")

        # Surface server-side errors from the captured log.
        if "ReplayErrorWithEcho" in server_log:
            log.log(f"{_MS.T_FAIL_REASON} server rejected OSCORE payload (ReplayErrorWithEcho) — sensor data not stored")
            log.log(_MS.SERVER_ERROR_OSCORE_REPLAY)
            self._emit_event("WARNING: OSCORE replay rejected by server")
            env_seen = False
        if "[Server] ERROR: OSCORE decryption failed" in server_log:
            log.log(f"{_MS.T_FAIL_REASON} server OSCORE decryption failed (check context keys / replay counter)")
            log.log(_MS.SERVER_ERROR_OSCORE_DECRYPT)
            self._emit_event("WARNING: server OSCORE decryption failed")
            coap_ok = False
        if "[Server] ERROR: CoAP error" in server_log:
            # Extract the first occurrence for the reason string
            for _line in server_log.splitlines():
                if "[Server] ERROR: CoAP error" in _line:
                    log.log(f"{_MS.T_FAIL_REASON} server returned CoAP error: {_line.strip()}")
                    break
            coap_ok = False

        # Determine pass/fail from event watchers
        if wrong_flags:
            passed = True
            log.log("MANUAL CHECK — negative test: verify server logs show expected failure")
        else:
            passed = env_seen and location_seen and location_coap_ok and coap_ok
            if not passed:
                if not location_seen:
                    log.log(f"{_MS.T_FAIL_REASON} location fix timeout (no Wi-Fi fix seen at LTE within 60 s)")
                if not location_coap_ok:
                    log.log(f"{_MS.T_FAIL_REASON} location CoAP ACK timeout (server did not ACK location POST within 30 s)")
                if not env_seen:
                    log.log(f"{_MS.T_FAIL_REASON} sensor data timeout (env data not seen at LTE hub within 30 s)")
                if not coap_ok:
                    log.log(f"{_MS.T_FAIL_REASON} sensor CoAP ACK timeout (server did not ACK sensor POST within 60 s — possible OSCORE replay/context issue)")

        status_str = "✓ PASS" if passed else "✗ FAIL"
        log.log(f"{_MS.T_PASS if passed else _MS.T_FAIL} {label}")
        self._emit_event(f"Test {status_str}: {label}")

        elapsed_seconds = round(time.time() - t_start, 1)
        run_dir.write_info({
            "timestamp":       datetime.now().isoformat(timespec="seconds"),
            "ble_mode":        ble_mode,
            "coap_mode":       coap_mode,
            "wrong_flags":     list(wrong_flags),
            "result":          "PASS" if passed else "FAIL",
            "elapsed_seconds": elapsed_seconds,
            "rebuild_sensor":  rebuild_sensor,
        })
        run_dir.write_timeline(log)
        self._live_log = None
        log.close()

        # Save UART panel logs (BLE, LTE, Thingy53) so the test run is self-contained.
        # Slice from the offset recorded at test start so we only include lines
        # from this test, not stale lines from previous runs still in the buffer.
        for src in ("BLE", "LTE", "Thingy53"):
            offset = log_offsets.get(src, 0)
            lines  = self._all_lines.get(src, [])[offset:]
            text = "".join(
                f"{wall}  {msg}\n" for wall, msg, kind in lines if kind != "build"
            )
            run_dir.log_path(f"{src.lower().replace(':', '')}.txt").write_text(
                text, encoding="utf-8"
            )

        # For single-test runs, write suite_summary.json directly.
        # Suite runs have this written by _run_suite_thread after all scenarios finish.
        import json as _json
        if suite_path is not None and write_suite_summary:
            suffix = ("_" + "_".join(sorted(wrong_flags))) if wrong_flags else ""
            suite_path.joinpath("suite_summary.json").write_text(
                _json.dumps({
                    "timestamp":       datetime.now().isoformat(timespec="seconds"),
                    "suite_name":      "single",
                    "total":           1,
                    "passed":          1 if passed else 0,
                    "failed":          0 if passed else 1,
                    "elapsed_seconds": elapsed_seconds,
                    "scenarios": [{
                        "index":           1,
                        "ble":             ble_mode,
                        "coap":            coap_mode,
                        "result":          "PASS" if passed else "FAIL",
                        "elapsed_seconds": elapsed_seconds,
                        "dir":             f"01_b-{ble_mode}_c-{coap_mode}{suffix}",
                    }],
                }, indent=2),
                encoding="utf-8",
            )

        _status(f"{status_str}  ({label})")
        if not self._cancel_event.is_set():
            time.sleep(1)
        return passed

    # ── Prebuild helpers ─────────────────────────────────────────────────────

    def _refresh_prebuild_status(self):
        """Check filesystem for existing prebuild artifacts and update the label."""
        import json, sys as _sys
        _root = Path(__file__).parent.parent.resolve()
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        try:
            import runtest as _rt
            suite_path = Path(__file__).parent / "test_suite.json"
            if not suite_path.exists() or not self._prebuild_status_lbl:
                return
            with open(suite_path) as f:
                suite = json.load(f)
            scenarios = [s for s in suite.get("scenarios", [])
                         if not s.get("skip", False) and not s.get("rotate")]
            seen_sensor: set[str] = set()
            seen_ble:   set[str] = set()
            ready = 0
            total = 0
            for s in scenarios:
                ble, coap = s["ble"], s["coap"]
                sk = ble
                if sk not in seen_sensor:
                    seen_sensor.add(sk)
                    total += 1
                    if _rt._prebuild_artifact_ready(_rt._sensor_prebuild_dir(ble)):
                        ready += 1
                bk = _rt._ble_variant_key(ble, coap)
                if bk not in seen_ble:
                    seen_ble.add(bk)
                    total += 1
                    if _rt._prebuild_artifact_ready(_rt._ble_prebuild_dir(ble, coap)):
                        ready += 1
            if total == 0:
                text, color = "", PALETTE["MUTE"]
            elif ready == total:
                text, color = f"({ready}/{total} ✓)", PALETTE["GRN"]
            else:
                text, color = f"({ready}/{total})", PALETTE["MUTE"]
            self._prebuild_status_lbl.config(text=text, fg=color)
        except Exception:
            pass

    def _prebuild_then_suite(self):
        """Build ALL variants in parallel, wait for completion, refresh all nodes,
        clear logs, then run the suite using prebuilt artifacts (no per-scenario rebuild)."""
        if self._test_running:
            return
        import json, sys as _sys
        _root = Path(__file__).parent.parent.resolve()
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        import runtest as _rt

        suite_path = Path(__file__).parent / "test_suite.json"
        if not suite_path.exists():
            self._test_status_lbl.config(text="test_suite.json not found")
            return
        with open(suite_path) as f:
            suite = json.load(f)
        scenarios = [s for s in suite.get("scenarios", []) if not s.get("skip", False)]
        if not scenarios:
            self._test_status_lbl.config(text="no scenarios to run")
            return

        self._cancel_event.clear()
        self._test_running = True
        self._test_run_btn.config(state=tk.DISABLED)
        self._suite_run_btn.config(state=tk.DISABLED)
        self._smoke_run_btn.config(state=tk.DISABLED)
        self._prebuilt_run_btn.config(state=tk.DISABLED)
        self._prebuild_btn.config(state=tk.DISABLED, text="⚙ Building…")
        self._build_suite_btn.config(state=tk.DISABLED, text="⚙▶ Building…")
        self._cancel_btn.config(state=tk.NORMAL)
        self._prebuild_status_lbl.config(text="building…", fg=PALETTE["MUTE"])
        self._test_status_lbl.config(text="building…", fg=PALETTE["MUTE"])

        pb_suite_name = suite.get("name", "Suite")
        folder_path, live_path = self._make_suite_folder(pb_suite_name, len(scenarios))
        self._launch_milestone_window(live_path)

        def _build_then_run():
            import runtest as _rt2
            log = _rt2.EventLog(live_path=live_path)
            self._emit_event(f"Build+Suite: {pb_suite_name} ({len(scenarios)} scenarios)")

            def on_progress(kind: str, key: str, status: str):
                self.after(0, self._refresh_prebuild_status)

            session = _rt2.prebuild_all(scenarios, log, on_progress=on_progress)
            self._prebuild_session = session

            # Wait for ALL builds to finish before touching hardware
            for ev in session.sensor_events.values():
                ev.wait()
            for ev in session.ble_events.values():
                ev.wait()

            failed = (sum(1 for s in session.sensor_status.values() if s == "failed") +
                      sum(1 for s in session.ble_status.values()    if s == "failed"))
            if failed:
                log.log(f"Build+Suite: {failed} build(s) FAILED — aborting suite")
                self.after(0, lambda: self._test_status_lbl.config(
                    text=f"{failed} build(s) FAILED", fg=PALETTE["RED"]))
                self._test_running = False
                self.after(0, lambda: (
                    self._test_run_btn.config(state=tk.NORMAL),
                    self._suite_run_btn.config(state=tk.NORMAL),
                    self._smoke_run_btn.config(state=tk.NORMAL),
                    self._prebuilt_run_btn.config(state=tk.NORMAL),
                    self._prebuild_btn.config(state=tk.NORMAL, text="⚙ Prebuild"),
                    self._build_suite_btn.config(state=tk.NORMAL, text="⚙▶ Build+Suite"),
                    self._cancel_btn.config(state=tk.DISABLED),
                ))
                self.after(0, self._refresh_prebuild_status)
                return

            log.log("All builds complete — resetting all nodes…")
            self.after(0, lambda: (
                self._build_suite_btn.config(text="⚙▶ Resetting…"),
                self._test_status_lbl.config(text="resetting nodes…", fg=PALETTE["MUTE"]),
            ))

            # Reset 91X via BLE shell, reset Thingy:53 via nrfutil
            self._resetting_sources.update(["BLE", "LTE", "Thingy53"])
            if self._write_queues.get("BLE"):
                self._write_queues["BLE"].put(b"reset all\r\n")
            subprocess.run(
                ["nrfutil", "device", "reset", "--serial-number", SNR_SENSOR],
                capture_output=True, text=True,
            )

            # Wait for nodes to reboot
            log.log("Waiting 30 s for all nodes to reboot…")
            self.after(0, lambda: self._test_status_lbl.config(
                text="waiting for reboot…", fg=PALETTE["MUTE"]))
            if self._cancel_event.wait(30):
                self._test_running = False
                self.after(0, lambda: (
                    self._test_run_btn.config(state=tk.NORMAL),
                    self._suite_run_btn.config(state=tk.NORMAL),
                    self._smoke_run_btn.config(state=tk.NORMAL),
                    self._prebuilt_run_btn.config(state=tk.NORMAL),
                    self._prebuild_btn.config(state=tk.NORMAL, text="⚙ Prebuild"),
                    self._build_suite_btn.config(state=tk.NORMAL, text="⚙▶ Build+Suite"),
                    self._cancel_btn.config(state=tk.DISABLED),
                    self._test_status_lbl.config(text="Cancelled", fg=PALETTE["MUTE"]),
                ))
                return

            # Clear all log panels before the suite starts
            log.log("Clearing log panels…")
            self.after(0, self._clear_all)

            self.after(0, lambda: (
                self._build_suite_btn.config(text="⚙▶ Running…"),
                self._test_status_lbl.config(text="starting suite…", fg=PALETTE["MUTE"]),
            ))
            self._run_suite_thread(scenarios, pb_suite_name, self._build_suite_btn,
                                   folder_path, live_path)

        threading.Thread(target=_build_then_run, daemon=True).start()

    def _run_prebuilt_suite(self):
        """Run the full suite using existing prebuilt artifacts — no rebuild, flash-only.
        Requires ⚙ Prebuild to have completed successfully first."""
        if self._test_running:
            return
        import json, sys as _sys
        _root = Path(__file__).parent.parent.resolve()
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        import runtest as _rt

        suite_path = Path(__file__).parent / "test_suite.json"
        if not suite_path.exists():
            self._test_status_lbl.config(text="test_suite.json not found")
            return
        with open(suite_path) as f:
            suite = json.load(f)
        scenarios = [s for s in suite.get("scenarios", []) if not s.get("skip", False)]
        if not scenarios:
            self._test_status_lbl.config(text="no scenarios to run")
            return

        # Verify all prebuilt artifacts exist on disk; build a session from them
        missing = []
        for s in scenarios:
            ble, coap = s["ble"], s["coap"]
            if not _rt._prebuild_artifact_ready(_rt._sensor_prebuild_dir(ble)):
                missing.append(f"sensor/{ble}")
            if not _rt._prebuild_artifact_ready(_rt._ble_prebuild_dir(ble, coap)):
                missing.append(f"ble/{_rt._ble_variant_key(ble, coap)}")
        if missing:
            self._test_status_lbl.config(
                text=f"missing prebuilts: {', '.join(missing[:3])}{'…' if len(missing) > 3 else ''}",
                fg=PALETTE["RED"],
            )
            self._emit_event(f"▶ Prebuilt: missing {len(missing)} artifact(s) — run ⚙ Prebuild first")
            return

        # Reconstruct (or reuse) a PrebuildSession with all statuses marked done
        if self._prebuild_session is None:
            session = _rt.prebuild_all([], _rt.EventLog())   # empty session skeleton
            self._prebuild_session = session
        session = self._prebuild_session
        for s in scenarios:
            ble, coap = s["ble"], s["coap"]
            bk = _rt._ble_variant_key(ble, coap)
            session.sensor_status[ble]  = "done"
            session.sensor_dirs[ble]    = _rt._sensor_prebuild_dir(ble)
            import threading as _th
            if ble not in session.sensor_events:
                ev = _th.Event(); ev.set(); session.sensor_events[ble] = ev
            session.ble_status[bk]  = "done"
            session.ble_dirs[bk]    = _rt._ble_prebuild_dir(ble, coap)
            if bk not in session.ble_events:
                ev = _th.Event(); ev.set(); session.ble_events[bk] = ev

        suite_name = suite.get("name", "Suite (prebuilt)")
        folder_path, live_path = self._make_suite_folder(suite_name, len(scenarios))
        self._launch_milestone_window(live_path)

        self._cancel_event.clear()
        self._test_running = True
        self._test_run_btn.config(state=tk.DISABLED)
        self._suite_run_btn.config(state=tk.DISABLED)
        self._smoke_run_btn.config(state=tk.DISABLED)
        self._prebuilt_run_btn.config(state=tk.DISABLED, text="● Prebuilt…")
        self._prebuild_btn.config(state=tk.DISABLED)
        self._build_suite_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)
        self._emit_event(f"▶ Prebuilt Suite: {suite_name} ({len(scenarios)} scenarios)")
        threading.Thread(
            target=self._run_suite_thread,
            args=(scenarios, suite_name, self._prebuilt_run_btn, folder_path, live_path),
            daemon=True,
        ).start()

    def _prebuild_suite(self):
        """Load test_suite.json and spawn parallel prebuild threads for all unique variants."""
        import json
        suite_path = Path(__file__).parent / "test_suite.json"
        if not suite_path.exists():
            self._test_status_lbl.config(text="test_suite.json not found")
            return
        with open(suite_path) as f:
            suite = json.load(f)
        scenarios = [s for s in suite.get("scenarios", []) if not s.get("skip", False)]
        if not scenarios:
            self._test_status_lbl.config(text="no scenarios to prebuild")
            return
        self._prebuild_session = None
        self._prebuild_btn.config(state=tk.DISABLED, text="⚙ Building…")
        self._test_status_lbl.config(text="prebuild started…", fg=PALETTE["MUTE"])
        self._emit_event("Prebuild started")
        threading.Thread(target=self._prebuild_suite_thread, args=(scenarios,), daemon=True).start()

    def _prebuild_suite_thread(self, scenarios: list):
        import sys as _sys
        _root = Path(__file__).parent.parent.resolve()  # tracker-utils/
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        import runtest as _rt

        log        = _rt.EventLog()
        done_count = [0]
        session    = None  # assigned after prebuild_all() returns; guard below

        def on_progress(kind: str, key: str, status: str):
            if status in ("done", "failed"):
                done_count[0] += 1
            if session is None:
                return
            total = len(session.sensor_events) + len(session.ble_events)
            color = PALETTE["GRN"] if status == "done" else (
                    PALETTE["RED"] if status == "failed" else PALETTE["MUTE"])
            self._emit_event(f"Prebuild {kind} {key}: {status}")
            self.after(0, lambda c=color, d=done_count[0], t=total:
                self._test_status_lbl.config(
                    text=f"prebuild {d}/{t}", fg=c if d == t else PALETTE["MUTE"]))

        session = _rt.prebuild_all(scenarios, log, on_progress=on_progress)
        self._prebuild_session = session

        for ev in session.sensor_events.values():
            ev.wait()
        for ev in session.ble_events.values():
            ev.wait()

        failed  = (sum(1 for s in session.sensor_status.values() if s == "failed") +
                   sum(1 for s in session.ble_status.values()    if s == "failed"))
        total   = len(session.sensor_events) + len(session.ble_events)
        summary = f"prebuild {total - failed}/{total} OK"
        color   = PALETTE["GRN"] if failed == 0 else PALETTE["RED"]
        self._emit_event(f"Prebuild complete: {summary}")
        self.after(0, lambda: (
            self._prebuild_btn.config(state=tk.NORMAL, text="⚙ Prebuild"),
            self._test_status_lbl.config(text=summary, fg=color),
        ))
        self.after(0, self._refresh_prebuild_status)

    def _run_suite(self, suite_file: str = "test_suite.json"):
        """Load suite_file from the same directory and run all non-skipped scenarios."""
        if self._test_running:
            return
        import json
        suite_path = Path(__file__).parent / suite_file
        if not suite_path.exists():
            self._test_status_lbl.config(text=f"{suite_file} not found")
            return
        with open(suite_path) as f:
            suite = json.load(f)
        scenarios = [s for s in suite.get("scenarios", []) if not s.get("skip", False)]
        if not scenarios:
            self._test_status_lbl.config(text="no scenarios to run")
            return
        suite_name = suite.get("name", Path(suite_file).stem)
        folder_path, live_path = self._make_suite_folder(suite_name, len(scenarios))
        self._launch_milestone_window(live_path)

        self._cancel_event.clear()
        self._test_running = True
        active_btn = (self._smoke_run_btn
                      if suite_file == "test_suite_smoke.json"
                      else self._suite_run_btn)
        self._test_run_btn.config(state=tk.DISABLED)
        self._suite_run_btn.config(state=tk.DISABLED, text="▶ Suite" if active_btn is not self._suite_run_btn else "● Suite…")
        self._smoke_run_btn.config(state=tk.DISABLED, text="▶ Smoke" if active_btn is not self._smoke_run_btn else "● Smoke…")
        self._prebuilt_run_btn.config(state=tk.DISABLED)
        self._build_suite_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)
        threading.Thread(
            target=self._run_suite_thread,
            args=(scenarios, suite_name, active_btn, folder_path, live_path),
            daemon=True,
        ).start()

    def _run_smoke(self):
        self._run_suite("test_suite_smoke.json")

    def _run_suite_thread(self, scenarios: list, suite_name: str, active_btn: tk.Button,
                          suite_path: Path | None = None, live_path: Path | None = None):
        import sys as _sys, json as _json, time as _time
        _root = Path(__file__).parent.parent.resolve()  # tracker-utils/
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        import runtest as _rt

        total        = len(scenarios)
        passed_count = 0
        suite_results: list[dict] = []
        suite_t0     = _time.time()
        self._emit_event(f"Suite started: {suite_name} ({total} scenarios)")

        cancelled = False
        i = 0
        for i, s in enumerate(scenarios):
            if self._cancel_event.is_set():
                self._emit_event(f"[Test] Suite cancelled — {passed_count}/{i} passed so far")
                cancelled = True
                break

            ble     = s["ble"]
            coap    = s["coap"]
            wrong   = frozenset(s.get("wrong",  []))
            rotate  = s.get("rotate", [])
            comment = s.get("comment", "")
            label   = f"{ble} + {coap}" + (f"  ({comment})" if comment else "")
            sc_idx  = i + 1

            self._emit_event(f"Suite {sc_idx}/{total}: {label}")
            self.after(0, lambda n=sc_idx, t=total, b=active_btn:
                b.config(text=f"● {n}/{t}"))

            # Key rotation before the test, if requested
            if "dtls" in rotate:
                _rt._rotate_dtls_keys(_rt.EventLog())
            oscore_targets = []
            if "oscore_hub"    in rotate or "oscore_both" in rotate:
                oscore_targets.append("hub")
            if "oscore_sensor" in rotate or "oscore_both" in rotate:
                oscore_targets.append("sensor")
            if oscore_targets:
                _rt._rotate_oscore_keys(oscore_targets, _rt.EventLog())

            sc_t0 = _time.time()
            try:
                passed = self._run_test_thread(ble, coap, wrong,
                                               prebuild_session=self._prebuild_session,
                                               suite_path=suite_path,
                                               live_path=live_path,
                                               idx=sc_idx,
                                               total=total)
                if passed:
                    passed_count += 1
            except Exception as e:
                passed = False
                self._emit_event(f"Suite ERROR in {label}: {e}")

            sc_elapsed = round(_time.time() - sc_t0, 1)
            suffix = ("_" + "_".join(sorted(wrong))) if wrong else ""
            suite_results.append({
                "index":           sc_idx,
                "ble":             ble,
                "coap":            coap,
                "result":          "PASS" if passed else "FAIL",
                "elapsed_seconds": sc_elapsed,
                "dir":             f"{sc_idx:02d}_b-{ble}_c-{coap}{suffix}",
            })

        if suite_path is not None:
            try:
                suite_path.joinpath("suite_summary.json").write_text(
                    _json.dumps({
                        "timestamp":       __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                        "suite_name":      suite_name,
                        "total":           total,
                        "passed":          passed_count,
                        "failed":          len(suite_results) - passed_count,
                        "elapsed_seconds": round(_time.time() - suite_t0, 1),
                        "scenarios":       suite_results,
                    }, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

        if cancelled:
            summary = f"Cancelled — {passed_count}/{i} passed"
        else:
            summary = f"{passed_count}/{total} PASSED"
            self._emit_event(f"Suite complete: {suite_name} — {summary}")
        color = "#4caf50" if (not cancelled and passed_count == total) else "#f44336"
        self._test_running = False
        self.after(0, lambda: (
            self._test_run_btn.config(state=tk.NORMAL),
            self._suite_run_btn.config(state=tk.NORMAL, text="▶ Suite"),
            self._smoke_run_btn.config(state=tk.NORMAL, text="▶ Smoke"),
            self._prebuilt_run_btn.config(state=tk.NORMAL, text="▶ Prebuilt"),
            self._build_suite_btn.config(state=tk.NORMAL, text="⚙▶ Build+Suite"),
            self._prebuild_btn.config(state=tk.NORMAL, text="⚙ Prebuild"),
            self._cancel_btn.config(state=tk.DISABLED),
            self._test_status_lbl.config(text=summary, fg=color),
        ))
        self.after(0, self._refresh_prebuild_status)

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
        fg = PALETTE["GRN"] if ok is True else PALETTE["RED"] if ok is False else PALETTE["MUTE"]
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
        wq = self._write_queues.get(source)
        if wq:
            wq.put((cmd + "\r\n").encode())
            self._append(source, time.time(), f"$ {cmd}", "dev")
            self._status.configure(text=f"Sent to {source}: {cmd}")
        else:
            self._status.configure(text=f"{source} not connected")

    def _trigger_location(self):
        """Trigger location search via BLE shell → nRF9151 IPC."""
        wq = self._write_queues.get("BLE")
        if wq:
            wq.put(b"att_location search\r\n")
            self._emit_event("BLE: att_location search sent")

    def _reset_91x(self):
        """Reset the whole Thingy:91X via 'reset all' BLE shell command."""
        self._resetting_sources.update(["BLE", "LTE"])
        self._shell_cmd("BLE", "reset all")
        self._emit_event("BLE: reset all sent")

    def _refresh_all(self):
        self._clear_all()
        self._status.configure(text="Refreshing…")
        self._resetting_sources.update(_DEVICE_SOURCES)
        self._shell_cmd("BLE", "reset all")
        self._emit_event("Refresh triggered")
        def reset_53():
            result = subprocess.run(
                ["nrfutil", "device", "reset", "--serial-number", SNR_SENSOR],
                capture_output=True, text=True,
            )
            ok  = result.returncode == 0
            msg = f"53 reset {'OK' if ok else 'FAILED'}"
            self._q.put((_UI_, time.time(), lambda m=msg: self._status.configure(text=m)))
        threading.Thread(target=reset_53, daemon=True).start()

    def _on_ble_mode_changed(self, *_):
        ble = self._ble_mode_var.get()
        oscore_ble = ble in _OSCORE_BLE_MODES
        if oscore_ble and self._coap_mode_var.get() not in _OSCORE_COAP_MODES:
            self._coap_mode_var.set("dtls_oscore")
        self._update_mode_indicators()

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
        conf = _BLE_APP / "security.conf"
        if conf.exists():
            m = _COAP_MODE_PAT.search(conf.read_text())
            if m:
                return m.group(1).lower()
        return "oscore"

    def _read_ble_mode(self) -> str:
        conf = _SENS_APP / "security.conf"
        if conf.exists():
            m = _BLE_MODE_PAT.search(conf.read_text())
            if m:
                return m.group(1).lower()
        return "gatt_oscore"

    def _apply_coap_mode(self):
        def run():
            coap = self._coap_mode_var.get()
            self._q.put(("Server", time.time(),
                         f"[Applying CoAP mode: {coap}]", "status"))
            try:
                _set_kconfig_mode(
                    _BLE_APP / "security.conf",
                    "CONFIG_APP_COAP_SECURITY_",
                    f"CONFIG_APP_COAP_SECURITY_{coap.upper()}=y",
                )
                self._q.put(("Server", time.time(),
                             f"  → hub BLE security.conf: COAP_SECURITY_{coap.upper()}", "build"))

                env_cmd = (
                    f"python3 -c \""
                    f"import re, pathlib; "
                    f"p = pathlib.Path('/root/tracker-server/.env'); "
                    f"t = p.read_text(); "
                    f"t = re.sub(r'^SECURITY_MODE=.*', 'SECURITY_MODE={coap}', t, flags=re.M); "
                    f"p.write_text(t)\""
                )
                _ssh(env_cmd, timeout=10)
                self._q.put(("Server", time.time(),
                             f"  → server .env: SECURITY_MODE={coap}", "build"))

                self._ssh_restart_server()

                def after_apply():
                    self._current_coap = coap
                    self._update_mode_indicators()
                    self._shell_cmd("BLE", f"coap_mode {coap}")
                    self._emit_event(f"CoAP mode → {coap}")

                self._q.put((_UI_, time.time(), after_apply))
                self._q.put(("Server", time.time(),
                             f"[CoAP mode {coap} applied — no reflash needed]", "status"))
            except Exception as e:
                self._q.put(("Server", time.time(), f"[apply CoAP error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    def _apply_ble_mode(self):
        ble = self._ble_mode_var.get()
        self._emit_event(f"BLE mode → {ble}")
        self._q.put(("Thingy53", time.time(),
                     f"[Applying BLE mode: {ble} — sensor + hub BLE reflash]", "status"))
        _set_kconfig_mode(
            _SENS_APP / "security.conf",
            "CONFIG_APP_BLE_SECURITY_",
            f"CONFIG_APP_BLE_SECURITY_{ble.upper()}=y",
        )
        self._q.put(("Thingy53", time.time(),
                     f"  → sensor security.conf: BLE_SECURITY_{ble.upper()}", "build"))
        flags = _HUB_BLE_RELAY_FLAGS.get(ble, {})
        for flag in ("CONFIG_APP_SENSOR_RELAY_BROADCAST", "CONFIG_APP_SENSOR_RELAY_OSCORE"):
            _set_kconfig_value(_BLE_APP / "security.conf", flag, flags.get(flag, False))
        relay_names = [f.replace("CONFIG_APP_", "") for f, v in flags.items() if v]
        self._q.put(("BLE", time.time(),
                     f"  → hub BLE security.conf relay: {', '.join(relay_names) or 'none'}",
                     "build"))
        self._do_build_and_flash("sensor", pristine=True)
        self._do_build_and_flash("ble",    pristine=True)

    # ── Server actions ────────────────────────────────────────────────────────

    def _fetch_server_branch(self):
        def run():
            try:
                result = _ssh(
                    "git -C ~/tracker-server fetch --quiet 2>&1;"
                    " git -C ~/tracker-server status --short --branch 2>&1",
                    timeout=20,
                )
                first  = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
                branch = first.lstrip("# ").split("...")[0].strip() or "?"
                if "[behind" in first:
                    n     = first.split("[behind")[1].split("]")[0].strip()
                    label = f"branch: {branch}  ⚠ {n} behind"
                    color = PALETTE["RED"]
                elif "[ahead" in first:
                    n     = first.split("[ahead")[1].split("]")[0].strip()
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
                result = _ssh(
                    "cd tracker-server && docker compose up --force-recreate -d coap-server 2>&1",
                    timeout=60,
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
                for name, conf_path in [("hub",    _BLE_APP  / "security.conf"),
                                         ("sensor", _SENS_APP / "security.conf")]:
                    self._q.put(("Server", time.time(),
                                 f"[generate_oscore_psk.py --name {name}]", "build"))
                    r = _ssh(
                        f"cd ~/tracker-server && python3 generate_oscore_psk.py --name {name} 2>&1",
                        timeout=30,
                    )
                    for line in r.stdout.splitlines():
                        self._q.put(("Server", time.time(), f"  {line}", "build"))
                    if r.returncode != 0:
                        self._q.put(("Server", time.time(),
                                     f"[FAILED generating {name} context]", "status"))
                        return
                    conf_lines = [ln for ln in r.stdout.splitlines()
                                  if ln.startswith("CONFIG_APP_OSCORE_")]
                    for ln in conf_lines:
                        _update_kconfig_key(conf_path, ln)
                    self._q.put(("Server", time.time(), f"  → wrote {conf_path}", "build"))

                env_cmd = (
                    "python3 -c \""
                    "import re, pathlib; "
                    "p = pathlib.Path('/root/tracker-server/.env'); "
                    "t = p.read_text(); "
                    "t = re.sub(r'^OSCORE_CONTEXT_DIR=.*', "
                    "'OSCORE_CONTEXT_DIR=/app/oscore-context', t, flags=re.M); "
                    "p.write_text(t)\""
                )
                _ssh(env_cmd, timeout=10)
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
                r = _ssh(
                    "cd ~/tracker-server && python3 generate_dtls_psk.py 2>&1",
                    timeout=30,
                )
                for line in r.stdout.splitlines():
                    self._q.put(("Server", time.time(), f"  {line}", "build"))
                if r.returncode != 0:
                    self._q.put(("Server", time.time(), "[DTLS gen FAILED]", "status"))
                    return

                conf_lines = [ln for ln in r.stdout.splitlines()
                              if ln.startswith("CONFIG_APP_DTLS_")]
                dtls_conf = _BLE_APP / "security.conf"
                for ln in conf_lines:
                    _update_kconfig_key(dtls_conf, ln)
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
                    _ssh(env_cmd, timeout=10)
                    self._q.put(("Server", time.time(),
                                 "[.env updated with new DTLS PSK]", "build"))

                self._ssh_restart_server()
                self._q.put(("Server", time.time(),
                             "[DTLS rotation done — rebuild + reflash BLE]", "status"))
            except Exception as e:
                self._q.put(("Server", time.time(),
                             f"[DTLS rotation error: {e}]", "status"))
        threading.Thread(target=run, daemon=True).start()

    # ── Device build / flash ──────────────────────────────────────────────────

    def _set_btns(self, key: str, enabled: bool):
        for b in self._action_btns.get(key, []):
            b.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _do_build(self, key: str, pristine: bool = False):
        tgt = TARGETS[key]
        self._set_btns(key, False)

        base_cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
        if pristine:
            west_idx = base_cmd.index("west")
            base_cmd = (base_cmd[:west_idx + 2]
                        + ["--pristine"]
                        + base_cmd[west_idx + 2:])

        tag        = f"build {'pristine' if pristine else ''}{key}".strip()
        build_label = "Build pristine" if pristine else "Build"
        lbl        = tgt["label"]
        self._status.configure(
            text=f"Building {lbl}{'  (pristine)' if pristine else ''}...")
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
                        if key == "ble":
                            self._current_coap = self._coap_mode_var.get()
                        elif key == "sensor":
                            self._current_ble = self._ble_mode_var.get()
                        self._q.put((_UI_, time.time(), self._update_mode_indicators))

                _stream_action("flash", panels, cwd, flash_cmd, self._q, done_cb=done)
            finally:
                if snr:
                    self._snr_release(snr)

        threading.Thread(target=flash_thread, daemon=True).start()

    def _do_build_and_flash(self, key: str, pristine: bool = False):
        tgt = TARGETS[key]
        self._set_btns(key, False)
        lbl       = tgt["label"]
        build_lbl = "Build pristine" if pristine else "Build"
        suffix    = "Pristine build & flash" if pristine else "Build & flash"
        self._status.configure(
            text=f"Building {lbl}{'  (pristine)' if pristine else ''}...")
        for p in tgt["panels"]:
            self._q.put((p, time.time(), f"[{build_lbl} started]", "status"))

        def _ui(txt):
            self._q.put((_UI_, time.time(), lambda: self._set_btns(key, True)))
            self._q.put((_UI_, time.time(), lambda s=txt: self._status.configure(text=s)))

        def after_build(ok):
            for p in tgt["panels"]:
                self._q.put((p, time.time(),
                             f"[{build_lbl} {'OK' if ok else 'FAILED'}]", "status"))
            if ok:
                self._emit_event(f"{lbl}: build complete")
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
                        self._emit_event(f"{lbl}: flashed")
                        if key == "ble":
                            self._current_coap = self._coap_mode_var.get()
                        elif key == "sensor":
                            self._current_ble = self._ble_mode_var.get()
                        self._q.put((_UI_, time.time(), self._update_mode_indicators))

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
        self._all_lines[source]  = []

    def _clear_all(self):
        for src in _DEVICE_SOURCES:
            self._clear_source(src)

    # ── Close ─────────────────────────────────────────────────────────────────

    def on_close(self):
        self._stop.set()
        if self._csv_file:
            self._csv_file.close()
        self.destroy()
