"""
milestone_window.py — Live milestone progress tracker for test suites.

Launched automatically when a suite or single-test run starts.

Usage:
    python3 core/milestone_window.py <live_file_path>

The live file is appended to in real time by EventLog.  This window polls it
every 500 ms, parses milestone strings, and updates the display.

Layout:
  ┌─ Suite header (N/total PASS | RUNNING | queued) ──────────────────────────┐
  │  Scrollable scenario table  (click a row to select)                       │
  ├─ 10 milestone tabs ─────────────────────────────────────────────────────── │
  │  Tab content: checkpoint rows with ⏳ / ✓ / ✗ + wall time + T+ elapsed    │
  └───────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import csv
import re
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, font as tkfont

# ── Path setup ────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent.resolve()
_ROOT = _HERE.parent.resolve()
for p in (str(_HERE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import milestones as MS

# ── Colour palette (matches logviewer) ────────────────────────────────────────

PAL = {
    "BG":      "#0d1117",
    "BG2":     "#161b22",
    "BG3":     "#21262d",
    "FG":      "#c9d1d9",
    "MUTE":    "#8b949e",
    "RED":     "#ff7b72",
    "GRN":     "#57ab5a",
    "YLW":     "#e3b341",
    "BLU":     "#79c0ff",
    "PRP":     "#d2a8ff",
    "BORDER":  "#30363d",
}

STATUS_ICON = {"pending": "⏳", "done": "✓", "fail": "✗", "running": "⚡"}
STATUS_FG   = {
    "pending": PAL["MUTE"],
    "done":    PAL["GRN"],
    "fail":    PAL["RED"],
    "running": PAL["YLW"],
}

# ── Phase / checkpoint definitions ────────────────────────────────────────────
# Each entry: (tab_label, [ (checkpoint_label, ms_constant, required_coap_modes_or_None) ])
# required_coap_modes: set of coap_mode strings where this checkpoint applies;
#   None means always shown.  Missing checkpoint rows are displayed grayed-out.

_ALL_COAP    = None  # shorthand: applies to any CoAP mode
_ALL_BLE     = None  # shorthand: applies to any BLE mode
_OSCORE_COAP = frozenset({"oscore", "dtls_oscore"})
_P2P_BLE     = frozenset({"gatt", "lesc", "gatt_oscore"})

# Each checkpoint: (label, ms_constant, required_coap_modes_or_None, required_ble_modes_or_None)
PHASE_DEFS: list[tuple[str, list[tuple[str, str, object, object]]]] = [
    ("Test", [
        ("Scenario started",        MS.T_SCENARIO,           _ALL_COAP, _ALL_BLE),
        ("Configs written",          MS.T_WRITING_CONFIGS,    _ALL_COAP, _ALL_BLE),
        ("LTE reset sent",           MS.T_RESETTING_LTE,      _ALL_COAP, _ALL_BLE),
        ("Server restarted",         MS.SERVER_RESTARTED,     _ALL_COAP, _ALL_BLE),
        ("BLE build started",        MS.BLE_BUILD_STARTED,    _ALL_COAP, _ALL_BLE),
        ("BLE build complete",       MS.BLE_BUILD_COMPLETE,   _ALL_COAP, _ALL_BLE),
        ("BLE build FAILED",         MS.BLE_BUILD_FAILED,     _ALL_COAP, _ALL_BLE),
        ("BLE flash started",        MS.BLE_FLASH_STARTED,    _ALL_COAP, _ALL_BLE),
        ("BLE flashed",              MS.BLE_FLASHED,          _ALL_COAP, _ALL_BLE),
        ("Sensor build started",     MS.SENSOR_BUILD_STARTED, _ALL_COAP, _ALL_BLE),
        ("Sensor build complete",    MS.SENSOR_BUILD_COMPLETE,_ALL_COAP, _ALL_BLE),
        ("Sensor build FAILED",      MS.SENSOR_BUILD_FAILED,  _ALL_COAP, _ALL_BLE),
        ("Sensor flash started",     MS.SENSOR_FLASH_STARTED, _ALL_COAP, _ALL_BLE),
        ("Sensor flashed",           MS.SENSOR_FLASHED,       _ALL_COAP, _ALL_BLE),
    ]),
    ("Security Context", [
        ("BLE booted",               MS.BLE_BOOTED,                   _ALL_COAP, _ALL_BLE),
        ("BLE security context set", MS.BLE_SECURITY_CONTEXT,         _ALL_COAP, _ALL_BLE),
        ("Config sent to LTE",       MS.BLE_SENT_SECURITY_CONFIG,     _ALL_COAP, _ALL_BLE),
        ("LTE received config",      MS.LTE_RECEIVED_SECURITY_CONFIG, _ALL_COAP, _ALL_BLE),
        ("BLE ready",                MS.BLE_READY,                    _ALL_COAP, _ALL_BLE),
    ]),
    ("LTE Ready", [
        ("LTE booted",               MS.LTE_BOOTED,              _ALL_COAP, _ALL_BLE),
        ("Connected to server",      MS.LTE_CONNECTED_TO_SERVER, _ALL_COAP, _ALL_BLE),
    ]),
    ("BLE Ready", [
        ("BLE booted",               MS.BLE_BOOTED,           _ALL_COAP, _ALL_BLE),
        ("Scanning for sensor",      MS.BLE_SCANNING,         _ALL_COAP, _ALL_BLE),
        ("Sensor connected (P2P)",   MS.BLE_SENSOR_CONNECTED, _ALL_COAP, _P2P_BLE),
    ]),
    ("Sensor Ready", [
        ("Sensor booted",            MS.SENSOR_BOOTED,        _ALL_COAP, _ALL_BLE),
        ("Hub P2P connected",        MS.BLE_SENSOR_CONNECTED, _ALL_COAP, _P2P_BLE),
        ("Timesync sent to sensor",  MS.LTE_TIMESYNC_SENT,    _ALL_COAP, _ALL_BLE),
    ]),
    ("Sensor→Server", [
        ("Sample sent",              MS.SENSOR_SAMPLE_SENT,    _ALL_COAP, _ALL_BLE),
        ("Sample at hub (BLE)",      MS.BLE_SAMPLE_RECEIVED,   _ALL_COAP, _ALL_BLE),
        ("Forwarded to LTE",         MS.BLE_SAMPLE_FORWARDED,  _ALL_COAP, _ALL_BLE),
        ("Sample at hub (LTE)",      MS.LTE_SAMPLE_RECEIVED,   _ALL_COAP, _ALL_BLE),
        ("CoAP POST sent",           MS.LTE_COAP_POST_SENT,    _ALL_COAP, _ALL_BLE),
    ]),
    ("Server✓Sensor", [
        ("Request received",         MS.SERVER_REQUEST_RECEIVED,  _ALL_COAP,    _ALL_BLE),
        ("OSCORE decrypted",         MS.SERVER_OSCORE_DECRYPTED,  _OSCORE_COAP, _ALL_BLE),
        ("InfluxDB write OK",        MS.SERVER_INFLUXDB_OK,       _ALL_COAP,    _ALL_BLE),
        ("CoAP ACK sent",            MS.SERVER_ACK_SENT,          _ALL_COAP,    _ALL_BLE),
        ("CoAP ACK at hub",          MS.LTE_ACK_RECEIVED,         _ALL_COAP,    _ALL_BLE),
    ]),
    ("Location→Server", [
        ("Location search started",  MS.LTE_LOCATION_SEARCH_STARTED, _ALL_COAP, _ALL_BLE),
        ("Wi-Fi fix",                MS.LTE_LOCATION_FIX_WIFI,       _ALL_COAP, _ALL_BLE),
        ("GNSS fix",                 MS.LTE_LOCATION_FIX_GNSS,       _ALL_COAP, _ALL_BLE),
        ("Cell fix",                 MS.LTE_LOCATION_FIX_CELL,       _ALL_COAP, _ALL_BLE),
        ("Location failed",          MS.LTE_LOCATION_FAILED,         _ALL_COAP, _ALL_BLE),
        ("Location CoAP ACK",        MS.LTE_LOCATION_ACK_RECEIVED,   _ALL_COAP, _ALL_BLE),
    ]),
    ("Server✓Location", [
        ("Location write OK",        MS.SERVER_INFLUXDB_LOCATION_OK, _ALL_COAP, _ALL_BLE),
    ]),
    ("Test Status", [
        ("PASS",                     MS.T_PASS, _ALL_COAP, _ALL_BLE),
        ("FAIL",                     MS.T_FAIL, _ALL_COAP, _ALL_BLE),
    ]),
]

# Build flat reverse lookup: ms_constant → list of (phase_idx, checkpoint_idx)
_MS_INDEX: dict[str, list[tuple[int, int]]] = {}
for _pi, (_tab, _cps) in enumerate(PHASE_DEFS):
    for _ci, (_lbl, _ms, _coap, _ble) in enumerate(_cps):
        _MS_INDEX.setdefault(_ms, []).append((_pi, _ci))

# Patterns that mark failure (checkpoint shown as ✗ instead of ✓)
_FAIL_CONSTANTS = frozenset({
    MS.BLE_BUILD_FAILED,
    MS.SENSOR_BUILD_FAILED,
    MS.LTE_LOCATION_FAILED,
    MS.BLE_ERROR_SENSOR_TIMEOUT,
    MS.LTE_ERROR_NETWORK,
    MS.LTE_ERROR_COAP_TIMEOUT,
    MS.SERVER_ERROR_OSCORE_REPLAY,
    MS.SERVER_ERROR_OSCORE_DECRYPT,
    MS.SERVER_ERROR_INFLUXDB,
})

# T+ regex
_T_RE   = re.compile(r"\[T\+\s*([\d.]+)s\]")
# Suite header line
_SUITE_RE = re.compile(r"^\[Suite\]\s+(\d+)\s+(\S+)\s+(.+)$")
# Scenario marker from T_SCENARIO constant: "[Test] Scenario N/total: ble + coap"
_SCEN_RE  = re.compile(r"\[Test\] Scenario\s+(\d+)/(\d+):\s+(\S+)\s*\+\s*(\S+)")
# PASS / FAIL result lines
_PASS_RE        = re.compile(re.escape(MS.T_PASS))
_FAIL_RE        = re.compile(re.escape(MS.T_FAIL))
_FAIL_REASON_RE = re.compile(re.escape(MS.T_FAIL_REASON) + r"\s*(.+)$")


# ── Per-scenario state ────────────────────────────────────────────────────────

class ScenarioState:
    def __init__(self, idx: int, total: int, ble: str, coap: str, wall_start: str):
        self.idx        = idx
        self.total      = total
        self.ble        = ble
        self.coap       = coap
        self.wall_start = wall_start
        self.status     = "running"   # running | pass | fail
        self.elapsed: float | None = None
        # {(phase_idx, cp_idx): {"status": "pending"|"done"|"fail", "wall": str, "t_plus": float}}
        self.checkpoints: dict[tuple[int, int], dict] = {}
        self.fail_reasons: list[str] = []

    @property
    def label(self) -> str:
        return f"{self.ble} + {self.coap}"


# ── Main window ───────────────────────────────────────────────────────────────

class MilestoneWindow(tk.Tk):

    def __init__(self, live_path: Path):
        super().__init__()
        self._live_path    = live_path
        self._file_pos     = 0
        self._scenarios:   list[ScenarioState] = []
        self._selected_idx = 0   # index into _scenarios list
        self._suite_total  = 0
        self._suite_name   = ""

        # Tk config
        self.title("Milestone Tracker")
        self.configure(bg=PAL["BG"])
        self.geometry("1100x750")

        self._build_ui()
        self._poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        mono = tkfont.Font(family="Monospace", size=10)

        # ── Header bar ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=PAL["BG2"], pady=6, padx=10)
        hdr.pack(fill=tk.X, side=tk.TOP)
        self._hdr_lbl = tk.Label(hdr, text="Milestone Tracker", bg=PAL["BG2"],
                                  fg=PAL["FG"], font=("Monospace", 12, "bold"))
        self._hdr_lbl.pack(side=tk.LEFT)

        # ── Suite overview table ─────────────────────────────────────────────
        tbl_frame = tk.Frame(self, bg=PAL["BG"], padx=6, pady=4)
        tbl_frame.pack(fill=tk.X, side=tk.TOP)

        cols = ("#", "Scenario", "Status", "Elapsed", "Started")
        self._tree = ttk.Treeview(tbl_frame, columns=cols, show="headings",
                                   height=6, selectmode="browse")
        widths = [30, 200, 380, 80, 90]
        for col, w in zip(cols, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, minwidth=w, anchor=tk.W)

        vsb = ttk.Scrollbar(tbl_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview",
                         background=PAL["BG2"], foreground=PAL["FG"],
                         fieldbackground=PAL["BG2"], rowheight=22,
                         font=("Monospace", 10))
        style.configure("Treeview.Heading",
                         background=PAL["BG3"], foreground=PAL["MUTE"],
                         relief="flat")
        style.map("Treeview", background=[("selected", PAL["BG3"])],
                  foreground=[("selected", PAL["BLU"])])

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.tag_configure("pass",    foreground=PAL["GRN"])
        self._tree.tag_configure("fail",    foreground=PAL["RED"])
        self._tree.tag_configure("running", foreground=PAL["YLW"])
        self._tree.tag_configure("queued",  foreground=PAL["MUTE"])

        # ── Divider ──────────────────────────────────────────────────────────
        tk.Frame(self, bg=PAL["BORDER"], height=1).pack(fill=tk.X, pady=2)

        # ── Selected scenario label ───────────────────────────────────────────
        sel_bar = tk.Frame(self, bg=PAL["BG"], padx=8, pady=2)
        sel_bar.pack(fill=tk.X)
        self._sel_lbl = tk.Label(sel_bar, text="No scenario selected",
                                  bg=PAL["BG"], fg=PAL["MUTE"],
                                  font=("Monospace", 10))
        self._sel_lbl.pack(side=tk.LEFT)

        # Fail reason bar — only visible when a failed scenario is selected
        fail_bar = tk.Frame(self, bg=PAL["BG"], padx=8, pady=1)
        fail_bar.pack(fill=tk.X)
        self._fail_lbl = tk.Label(fail_bar, text="", bg=PAL["BG"], fg=PAL["RED"],
                                   font=("Monospace", 10), anchor=tk.W, justify=tk.LEFT)
        self._fail_lbl.pack(fill=tk.X)

        # ── Single scrollable checkpoint area ────────────────────────────────
        cp_outer = tk.Frame(self, bg=PAL["BG2"])
        cp_outer.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        canvas = tk.Canvas(cp_outer, bg=PAL["BG2"], highlightthickness=0)
        sb     = ttk.Scrollbar(cp_outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg=PAL["BG2"])
        cwin  = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_configure(e, c=canvas, w=cwin):
            c.itemconfig(w, width=c.winfo_width())
            c.configure(scrollregion=c.bbox("all"))

        canvas.bind("<Configure>", _on_configure)
        inner.bind("<Configure>", lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))

        def _on_wheel(e, c=canvas):
            c.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        self._phase_headers: list[tk.Label]                  = []
        self._cp_labels:     list[list[dict[str, tk.Label]]] = []

        for phase_idx, (tab_label, checkpoints) in enumerate(PHASE_DEFS):
            # Section header
            hdr_lbl = tk.Label(inner, text=f"  {tab_label}",
                                bg=PAL["BG3"], fg=PAL["MUTE"],
                                font=("Monospace", 10, "bold"),
                                anchor=tk.W, pady=3)
            hdr_lbl.pack(fill=tk.X, padx=0, pady=(8 if phase_idx else 2, 0))
            self._phase_headers.append(hdr_lbl)

            row_widgets: list[dict[str, tk.Label]] = []
            for _cp_idx, (cp_label, ms_const, required_modes, required_ble) in enumerate(checkpoints):
                row = tk.Frame(inner, bg=PAL["BG2"], pady=2)
                row.pack(fill=tk.X, padx=12)

                icon_lbl = tk.Label(row, text="⏳", bg=PAL["BG2"],
                                     fg=PAL["MUTE"], width=3, anchor=tk.W,
                                     font=("Monospace", 10))
                icon_lbl.pack(side=tk.LEFT)

                name_lbl = tk.Label(row, text=cp_label, bg=PAL["BG2"],
                                     fg=PAL["MUTE"], width=28, anchor=tk.W,
                                     font=("Monospace", 10))
                name_lbl.pack(side=tk.LEFT, padx=(0, 8))

                wall_lbl = tk.Label(row, text="—", bg=PAL["BG2"],
                                     fg=PAL["MUTE"], width=12, anchor=tk.W,
                                     font=("Monospace", 10))
                wall_lbl.pack(side=tk.LEFT, padx=(0, 8))

                tp_lbl = tk.Label(row, text="—", bg=PAL["BG2"],
                                   fg=PAL["MUTE"], width=12, anchor=tk.W,
                                   font=("Monospace", 10))
                tp_lbl.pack(side=tk.LEFT)

                row_widgets.append({
                    "icon": icon_lbl,
                    "name": name_lbl,
                    "wall": wall_lbl,
                    "tp":   tp_lbl,
                    "required_modes": required_modes,
                    "required_ble":   required_ble,
                    "ms_const":       ms_const,
                })

            self._cp_labels.append(row_widgets)

    # ── Event handling ────────────────────────────────────────────────────────

    def _on_select(self, _event=None):
        sel = self._tree.selection()
        if not sel:
            return
        item = self._tree.item(sel[0])
        try:
            idx = int(item["values"][0]) - 1
        except (IndexError, ValueError):
            return
        if 0 <= idx < len(self._scenarios):
            self._selected_idx = idx
            self._refresh_tabs()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            if self._live_path.exists():
                with self._live_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self._file_pos)
                    new_text = fh.read()
                    self._file_pos = fh.tell()
                if new_text:
                    for line in new_text.splitlines():
                        self._handle_line(line.strip())
        except Exception:
            pass
        finally:
            self.after(500, self._poll)

    def _handle_line(self, line: str):
        if not line:
            return

        # Suite header: [Suite] 16  2026-05-28_220000  core
        m = _SUITE_RE.match(line)
        if m:
            self._suite_total = int(m.group(1))
            self._suite_name  = m.group(3)
            self._hdr_lbl.config(text=f"Milestone Tracker — {self._suite_name}")
            self._update_header()
            return

        # Extract wall clock and T+ elapsed
        wall_str = line[:12] if len(line) > 12 and line[2] == ":" else ""
        tp_m = _T_RE.search(line)
        t_plus = float(tp_m.group(1)) if tp_m else 0.0

        # Scenario start marker
        m = _SCEN_RE.search(line)
        if m:
            idx, total, ble, coap = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            self._add_scenario(idx, total, ble, coap, wall_str)
            self._update_header()
            return

        # Fail reason (must be checked before T_FAIL, which is a prefix of T_FAIL_REASON)
        m = _FAIL_REASON_RE.search(line)
        if m:
            self._add_fail_reason(m.group(1).strip())
            return

        # Pass / Fail
        if _PASS_RE.search(line):
            self._set_current_result("pass", t_plus)
            return
        if _FAIL_RE.search(line):
            self._set_current_result("fail", t_plus)
            return

        # Milestone checkpoint matching
        for ms_const, positions in _MS_INDEX.items():
            if ms_const in line:
                status = "fail" if ms_const in _FAIL_CONSTANTS else "done"
                self._mark_checkpoint(ms_const, status, wall_str, t_plus)

    def _add_scenario(self, idx: int, total: int, ble: str, coap: str, wall: str):
        sc = ScenarioState(idx, total, ble, coap, wall)
        # pad list if needed (idx is 1-based)
        while len(self._scenarios) < idx:
            self._scenarios.append(None)  # type: ignore
        self._scenarios[idx - 1] = sc

        # T_SCENARIO is already satisfied — mark it done immediately
        for (pi, ci) in _MS_INDEX.get(MS.T_SCENARIO, []):
            sc.checkpoints[(pi, ci)] = {"status": "done", "wall": wall, "t_plus": 0.0}

        tag = "running"
        self._tree.insert("", tk.END, iid=str(idx),
                           values=(idx, sc.label, "⚡ Running", "—", wall or "—"),
                           tags=(tag,))
        self._tree.selection_set(str(idx))
        self._selected_idx = idx - 1
        self._tree.see(str(idx))
        self._refresh_tabs()

    def _add_fail_reason(self, reason: str):
        if not self._scenarios:
            return
        for candidate in reversed(self._scenarios):
            if candidate is not None and candidate.status == "running":
                candidate.fail_reasons.append(reason)
                if self._selected_idx == candidate.idx - 1:
                    self._update_fail_label(candidate)
                return

    def _set_current_result(self, result: str, t_plus: float):
        if not self._scenarios:
            return
        # Find the most recently running scenario
        sc = None
        for candidate in reversed(self._scenarios):
            if candidate is not None and candidate.status == "running":
                sc = candidate
                break
        if sc is None:
            return
        sc.status  = result
        sc.elapsed = t_plus
        if result == "fail" and sc.fail_reasons:
            short = sc.fail_reasons[0][:50]
            icon  = f"✗ FAIL: {short}{'…' if len(sc.fail_reasons[0]) > 50 else ''}"
        else:
            icon  = "✓ PASS" if result == "pass" else "✗ FAIL"
        color = "pass" if result == "pass" else "fail"
        self._tree.item(str(sc.idx), values=(sc.idx, sc.label, icon,
                                              f"{t_plus:.1f}s", sc.wall_start),
                         tags=(color,))
        self._update_header()
        if self._selected_idx == sc.idx - 1:
            self._refresh_tabs()

        # Auto-export when every expected scenario has a final result
        total = self._suite_total or len(self._scenarios)
        done  = sum(1 for s in self._scenarios if s and s.status in ("pass", "fail"))
        if done >= total > 0:
            self._write_report_csv()

    def _mark_checkpoint(self, ms_const: str, status: str, wall: str, t_plus: float):
        if not self._scenarios:
            return
        # Find the current running scenario
        sc = None
        for candidate in reversed(self._scenarios):
            if candidate is not None and candidate.status == "running":
                sc = candidate
                break
        if sc is None:
            return

        positions = _MS_INDEX.get(ms_const, [])
        for (pi, ci) in positions:
            key = (pi, ci)
            if key not in sc.checkpoints or sc.checkpoints[key]["status"] == "pending":
                sc.checkpoints[key] = {"status": status, "wall": wall, "t_plus": t_plus}

        # Update tab color for the affected phases
        affected_phases = {pi for (pi, ci) in positions}
        if self._selected_idx == sc.idx - 1:
            for pi in affected_phases:
                self._refresh_tab(pi, sc)
            self._refresh_tab_headers(sc)

    def _update_header(self):
        total   = self._suite_total or len(self._scenarios)
        n_pass  = sum(1 for s in self._scenarios if s and s.status == "pass")
        n_fail  = sum(1 for s in self._scenarios if s and s.status == "fail")
        n_run   = sum(1 for s in self._scenarios if s and s.status == "running")
        n_queue = max(0, total - len([s for s in self._scenarios if s]))
        parts = []
        if n_pass:  parts.append(f"{n_pass} PASS")
        if n_fail:  parts.append(f"{n_fail} FAIL")
        if n_run:   parts.append(f"{n_run} running")
        if n_queue: parts.append(f"{n_queue} queued")
        suite_label = self._suite_name or "Suite"
        self._hdr_lbl.config(
            text=f"Milestone Tracker — {suite_label} — {total} scenarios  |  "
                 + ("  ".join(parts) if parts else "waiting…")
        )

    # ── Tab rendering ─────────────────────────────────────────────────────────

    def _refresh_tabs(self):
        sc = self._current_scenario()
        label = sc.label if sc else "—"
        self._sel_lbl.config(
            text=f"Showing: scenario {self._selected_idx + 1}  ({label})" if sc else "No scenario selected"
        )
        self._update_fail_label(sc)
        for pi in range(len(PHASE_DEFS)):
            self._refresh_tab(pi, sc)
        self._refresh_tab_headers(sc)

    def _update_fail_label(self, sc: ScenarioState | None):
        if sc and sc.status == "fail" and sc.fail_reasons:
            lines = "\n".join(f"  ✗ {r}" for r in sc.fail_reasons)
            self._fail_lbl.config(text=lines)
        elif sc and sc.status == "running" and sc.fail_reasons:
            # reasons logged before final FAIL verdict (e.g. still waiting on other criteria)
            lines = "\n".join(f"  ✗ {r}" for r in sc.fail_reasons)
            self._fail_lbl.config(text=lines)
        else:
            self._fail_lbl.config(text="")

    def _refresh_tab(self, phase_idx: int, sc: ScenarioState | None):
        rows = self._cp_labels[phase_idx]
        for ci, row_w in enumerate(rows):
            key     = (phase_idx, ci)
            ms_const  = row_w["ms_const"]
            req_modes = row_w["required_modes"]
            req_ble   = row_w["required_ble"]

            # Determine if this checkpoint applies to the current scenario
            if sc is None \
               or (req_modes is not None and sc.coap not in req_modes) \
               or (req_ble   is not None and sc.ble  not in req_ble):
                self._set_cp_row(row_w, "n/a", "—", "—", PAL["BG3"])
                continue

            cp = sc.checkpoints.get(key)
            if cp is None:
                self._set_cp_row(row_w, "pending", "—", "—", PAL["BG2"])
            else:
                wall   = cp["wall"] or "—"
                t_str  = f"T+{cp['t_plus']:6.1f}s"
                self._set_cp_row(row_w, cp["status"], wall, t_str, PAL["BG2"])

    def _set_cp_row(self, row_w: dict, status: str, wall: str, tp: str, bg: str):
        if status == "n/a":
            fg   = PAL["BG3"]
            icon = "·"
        else:
            fg   = STATUS_FG.get(status, PAL["MUTE"])
            icon = STATUS_ICON.get(status, "⏳")

        row_w["icon"].config(text=icon, fg=fg)
        row_w["name"].config(fg=fg)
        row_w["wall"].config(text=wall, fg=fg)
        row_w["tp"].config(text=tp, fg=fg)

    def _refresh_tab_headers(self, sc: ScenarioState | None):
        for pi, (tab_label, checkpoints) in enumerate(PHASE_DEFS):
            if sc is None:
                self._phase_headers[pi].config(fg=PAL["MUTE"], text=f"  {tab_label}")
                continue
            cps_applicable = [
                (pi, ci) for ci, (_, _, req_coap, req_ble) in enumerate(checkpoints)
                if (req_coap is None or sc.coap in req_coap)
                and (req_ble  is None or sc.ble  in req_ble)
            ]
            states = [sc.checkpoints.get(k, {}).get("status", "pending")
                      for k in cps_applicable]
            if all(s == "done" for s in states) and states:
                color = PAL["GRN"]
            elif any(s == "fail" for s in states):
                color = PAL["RED"]
            elif any(s == "done" for s in states):
                color = PAL["YLW"]
            else:
                color = PAL["MUTE"]
            done_times = [sc.checkpoints[k]["t_plus"]
                          for k in cps_applicable if k in sc.checkpoints]
            if len(done_times) >= 2:
                phase_elapsed = f"  [{max(done_times) - min(done_times):.1f}s]"
            elif len(done_times) == 1:
                phase_elapsed = f"  [T+{done_times[0]:.1f}s]"
            else:
                phase_elapsed = ""
            self._phase_headers[pi].config(fg=color, text=f"  {tab_label}{phase_elapsed}")

    # ── CSV report ────────────────────────────────────────────────────────────

    def _write_report_csv(self):
        out_path = self._live_path.parent / "milestone_report.csv"
        try:
            with out_path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "scenario", "ble_mode", "coap_mode",
                    "result", "elapsed_s",
                    "phase", "checkpoint", "ms_constant",
                    "status", "wall_time", "t_plus_s",
                ])
                for sc in self._scenarios:
                    if sc is None:
                        continue
                    elapsed = f"{sc.elapsed:.3f}" if sc.elapsed is not None else ""
                    for pi, (tab_label, checkpoints) in enumerate(PHASE_DEFS):
                        for ci, (cp_label, ms_const, req_coap, req_ble) in enumerate(checkpoints):
                            if (req_coap is not None and sc.coap not in req_coap) \
                            or (req_ble  is not None and sc.ble  not in req_ble):
                                cp_status = "n/a"
                                wall      = ""
                                t_plus    = ""
                            else:
                                cp = sc.checkpoints.get((pi, ci))
                                if cp is None:
                                    cp_status = "pending"
                                    wall      = ""
                                    t_plus    = ""
                                else:
                                    cp_status = cp["status"]
                                    wall      = cp["wall"] or ""
                                    t_plus    = f"{cp['t_plus']:.3f}"
                            w.writerow([
                                sc.idx, sc.ble, sc.coap,
                                sc.status, elapsed,
                                tab_label, cp_label, ms_const,
                                cp_status, wall, t_plus,
                            ])
        except Exception as exc:
            print(f"[MilestoneWindow] CSV report failed: {exc}", flush=True)
            return
        print(f"[MilestoneWindow] Report written: {out_path}", flush=True)

    def _on_close(self):
        self._write_report_csv()
        self.destroy()

    def _current_scenario(self) -> ScenarioState | None:
        if 0 <= self._selected_idx < len(self._scenarios):
            return self._scenarios[self._selected_idx]
        return None


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: milestone_window.py <live_file_path>")
        sys.exit(1)
    live_path = Path(sys.argv[1])
    app = MilestoneWindow(live_path)
    app.mainloop()


if __name__ == "__main__":
    main()
