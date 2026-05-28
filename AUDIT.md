# tracker-utils — Module Map & Dead Code Audit

Generated: 2026-05-27

---

## 1. File structure

```
tracker-utils/
├── logviewer.py        Entry point — argparse + main(), launches LogViewer
├── viewer.py           LogViewer Tkinter class (~1000 lines, main GUI)
├── build_config.py     NCS version, TARGETS dict, _effective_build_cmd()
├── ui_constants.py     PALETTE, SOURCE_COLOR, BAUD, SOURCES, regex, mode defs
├── utils.py            _dbg(), _stream_action()
├── serial_io.py        Port detection, serial_reader(), rtt_reader()
├── jlink.py            JLinkGDBServer spawn/stop (currently NOP — see §3)
├── server_io.py        SSH_SERVER_HOST, ssh_log_reader()
├── monitor.py          Standalone headless serial monitor (no GUI)
├── verify.py           Post-scenario CoAP smoke test (standalone script)
├── runtest.py          Automated scenario runner (headless + called by viewer)
├── runtest.sh          Thin wrapper: cd + exec runtest.py
├── ble.sh              Build/flash/monitor for hub-ble (nRF5340)
├── lte.sh              Build/flash/monitor for hub-lte (nRF9151)
├── sensor.sh           Build/flash/monitor for sensor (Thingy:53)
├── set_scenario.sh     Write Kconfig scenario config to local.conf files
├── test_suite.json     Scenario list consumed by viewer "Suite" button + runtest --suite
├── .runtest_state.json Persisted BLE mode + conf hash for smart sensor rebuild
├── requirements.txt    pyserial, aiocoap, cbor2
├── testruns/           Auto-generated per-run output dirs (gitignored)
├── CLAUDE.md           AI context (developer docs)
└── README.md           User docs — **OUTDATED**, see §3.13
```

---

## 2. Module dependency map

```
logviewer.py
  └── viewer.py
        ├── build_config.py   (TARGETS, _effective_build_cmd, paths)
        ├── jlink.py          (JLINK_SERVERS=[], spawn_jlink_servers, stop_jlink_servers)
        ├── serial_io.py      (find_*_ports, serial_reader, rtt_reader)
        ├── server_io.py      (SSH_SERVER_HOST, ssh_log_reader)
        ├── ui_constants.py   (PALETTE, SOURCES, _EVENT_PATTERNS, mode defs, …)
        └── utils.py          (_dbg, _stream_action)

runtest.py (standalone or called from viewer._run_test_thread)
  ├── build_config.py
  ├── serial_io.py
  ├── server_io.py
  └── ui_constants.py

monitor.py    — standalone, no shared imports (own BAUD + find_thingy_ports)
verify.py     — standalone, no shared imports
ble.sh        — invokes monitor.py (usb) and logviewer.py (monitor)
lte.sh        — same
sensor.sh     — same
set_scenario.sh — pure bash, no Python imports
runtest.sh    — exec runtest.py
```

---

## 3. Dead / excessive / inconsistent code — flagged for review

Each item is flagged `🔴 DEAD` (code that currently does nothing), `🟡 DUPLICATE` (logic copy-pasted), or `🟠 STALE` (was correct once, no longer is). You should personally confirm before removing.

---

### 3.1 🔴 DEAD — `jlink.py` entire module (and `rtt_reader` in `serial_io.py`)

`jlink.py` line 26:
```python
JLINK_SERVERS: list[dict] = [
    # All three devices currently log via USB CDC-ACM; RTT is unused.
]
```

**Everything downstream of this empty list is a no-op:**

| Location | Call | Effect with empty list |
|---|---|---|
| `viewer.py:500` | `spawn_jlink_servers(JLINK_SERVERS, ...)` | returns `[]` immediately |
| `viewer.py:501` | `self._start_rtt_readers()` | iterates empty `JLINK_SERVERS`, starts 0 threads |
| `viewer.py:1165–1170` | `_respawn_jlink()` | stop nothing, spawn nothing, start nothing |
| `viewer.py:1782, 1868` | `self._q.put((_UI_, ..., self._respawn_jlink))` | queues the no-op after every sensor flash |
| `serial_io.py` | `rtt_reader()` function | defined but **never called** (only caller is `_start_rtt_readers` above) |

`rtt_reader` in `serial_io.py` and the entire content of `jlink.py` below the empty list definition can be removed when RTT is no longer planned. If RTT may come back, keep it but be aware it silently does nothing today.

**Cross-device note:** These are local dev tools only; removing this code has no effect on any firmware, the hub, or the server.

---

### 3.2 🔴 DEAD — `_build_server_config()` in `viewer.py` (line 1226)

This method builds a full configuration side panel (~100 lines):
- CoAP mode radio buttons + "Apply CoAP & Restart Server"
- BLE mode radio buttons + "Apply BLE & Flash Sensor + Hub BLE"
- OSCORE runtime enable/disable buttons
- Build conf file existence display

**It is never called anywhere.** `grep -n "_build_server_config" viewer.py` returns only the definition.

The UI has the security controls built inline in `_build_ui()` (the bottom action bars and test bar), and the mode state is managed through `_coap_mode_var`/`_ble_mode_var`. This panel was probably extracted into its own method but the call site was never added.

Candidate for removal if the inline controls are sufficient.

---

### 3.3 🔴 DEAD — `_waiting_for_boot` in `viewer.py` (line 75)

```python
self._waiting_for_boot: set[str] = set()
```

The CLAUDE.md says: *"when set, all messages are dropped until `*** Booting` is seen. Only set during explicit flash flows."*

But `.add()` and `.update()` are **never called on this set anywhere in the file**. The set is initialised, the guard check in `_append()` (line 571) is present, but the trigger is missing. The boot-gate feature is structurally dead — it reads empty forever.

**Result:** `if source in self._waiting_for_boot:` is always `False`; the 6-line block is unreachable code.

---

### 3.4 🔴 DEAD — `reconnect_cb` in `viewer._launch()` (always `None`)

```python
serial_ports = [
    ("BLE",      ble_port,    q91, None),   # reconnect_cb = None
    ("LTE",      lte_port,    q91, None),   # reconnect_cb = None
    ("Thingy53", sensor_port, q53, None),   # reconnect_cb = None
]
```

`serial_reader()` accepts `reconnect_cb` and calls it on reconnect — but since it's always `None`, the callback is never invoked. This was originally used to trigger JLink respawn on reconnect, which is also dead (§3.1). The `reconnect_cb` parameter passing in `_launch()`, `_hotplug_watcher()`, and the `serial_reader()` signature can all be simplified.

---

### 3.5 🟡 DUPLICATE — Kconfig helpers in both `viewer.py` and `runtest.py`

These functions appear in both files with identical or near-identical logic:

| Function | `viewer.py` line | `runtest.py` line |
|---|---|---|
| `_update_kconfig_key(path, line)` | 1422 | 196 |
| `_set_kconfig_mode(path, prefix, new_line)` | 1432 | 182 |
| `_set_kconfig_value(path, key, value)` | 1441 | 188 |
| `_read_kconfig_value(path, key)` | — | 203 |

`viewer.py` calls its own copies in `_apply_coap_mode` and `_apply_ble_mode`. `runtest.py` calls its copies in `_setup_configs`. Both operate on the same conf files.

These could live in `build_config.py` or a new `kconfig_utils.py`, and both files could import them. Not a bug, but maintenance risk when fixing one copy without the other.

---

### 3.6 🟡 DUPLICATE — `monitor.py` reimplements `serial_io.py` utilities

`monitor.py` is a fully standalone script with no imports from the shared modules. Two specific duplications:

1. `find_thingy_ports()` at line 30 — identical pattern to `find_thingy91x_ports()` in `serial_io.py`.
2. `BAUD = 115200` at line 23 — duplicates `BAUD` in `ui_constants.py`.

Since `monitor.py` is intentionally standalone (useful when the other modules aren't installed), the duplication is deliberate. Just be aware that if the USB device pattern changes you need to update both files.

---

### 3.7 🟡 DUPLICATE — SSH boilerplate scattered through `viewer.py`

`runtest.py` has a clean shared helper:
```python
def _ssh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess: ...
```

`viewer.py` builds the SSH subprocess inline in five separate methods:
- `_fetch_server_branch()` (line 1521)
- `_ssh_restart_server()` (line 1553)
- `_rotate_oscore_keys()` (line 1579)
- `_rotate_dtls_keys()` (line 1629)
- `_apply_coap_mode()` (line 1471)

Each repeats `["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", SSH_SERVER_HOST, ...]`. A shared `_ssh()` function in `server_io.py` (which already holds `SSH_SERVER_HOST`) would remove this duplication.

---

### 3.8 🟠 STALE — `set_scenario.sh` writes wrong Kconfig names

`set_scenario.sh` lines 98, 105, 113, 122 write:
```
CONFIG_APP_CLOUD_SECURITY_NONE=y
CONFIG_APP_CLOUD_SECURITY_OSCORE=y
CONFIG_APP_CLOUD_SECURITY_DTLS=y
CONFIG_APP_CLOUD_SECURITY_DTLS_OSCORE=y
```

Current firmware and all other tools (`runtest.py`, `viewer.py`, `ui_constants.py`) use:
```
CONFIG_APP_COAP_SECURITY_NONE=y
CONFIG_APP_COAP_SECURITY_OSCORE=y
CONFIG_APP_COAP_SECURITY_DTLS=y
CONFIG_APP_COAP_SECURITY_DTLS_OSCORE=y
```

The script is **broken for CoAP scenario switching** — it writes stale Kconfig names that the firmware does not recognise. The `--ble` path is unaffected. This is also documented in `CLAUDE.md` under "known issue".

**Cross-device note:** Fixing this affects `tracker-hub/apps/tracker-hub-lte/local.conf` which controls the nRF9151 build. Do NOT apply without rebuilding and reflashing the LTE target.

---

### 3.9 🟠 STALE — `ble.sh` build command differs from `build_config.py`

Two inconsistencies vs `build_config.py`:

1. `ble.sh` line 24–29 does **not** pass `--sysbuild`, but `build_config.py` "ble" target does.
2. `ble.sh` unconditionally appends `-DEXTRA_CONF_FILE=local.conf` without checking if the file exists. `build_config.py`'s `_effective_build_cmd()` only appends it when the file is present.

Result: `ble.sh build` and the GUI/runtest BLE build produce different west invocations. If `local.conf` doesn't exist, `ble.sh` will error; the GUI/runtest will not.

---

### 3.10 🟠 STALE — `README.md` documents non-existent scripts

`README.md` describes two scripts:
- `west.sh <target> [pristine] [flash]`
- `flash.sh <target>`

Neither file exists in the repo. The actual workflow is `ble.sh`, `lte.sh`, `sensor.sh`. The README is likely from an earlier organization.

---

### 3.11 🟠 STALE — Legacy shell aliases

Shell scripts include aliases for old subcommand names:

| File | Alias | Real target | Risk of removing |
|---|---|---|---|
| `ble.sh:73` | `screen` → `usb` | CDC-ACM monitor | Low — `usb` is documented |
| `lte.sh:73` | `screen` → `usb` | CDC-ACM monitor | Low |
| `sensor.sh:72` | `screen` → `rtt` | RTT viewer | Low |
| `sensor.sh:73` | `jlink` → `jtag` | J-Link session | Low |

No harm keeping them, but they add noise to the usage strings and `case` blocks.

---

### 3.12 🟡 UNUSED IMPORT — `ttk` in `viewer.py` (line 18)

```python
from tkinter import filedialog, scrolledtext, ttk  # noqa: F401 (ttk available for callers)
```

`ttk` is never referenced in `viewer.py`. The comment says it is "available for callers" but no external code imports it from here. Safe to remove.

---

### 3.13 Non-issues (kept for context)

- `testruns/` directory — auto-generated by `runtest.py` / `viewer.py`. Tracked by git. Not dead code.
- `.runtest_state.json` — persisted state for smart sensor rebuild. Actively read/written by `runtest.py`.
- `test_suite.json` — consumed by both `viewer._run_suite()` and `runtest.py --suite`.
- `verify.py` — standalone, not called by any other module. Legitimately separate tool.
- `monitor.py` — legitimately standalone, intentional independence from shared modules.
- `CLAUDE.md` — AI context doc, not dead code.

---

## 4. Summary table

| # | File(s) | Issue | Severity |
|---|---|---|---|
| 3.1 | `jlink.py`, `serial_io.rtt_reader`, `viewer._start_rtt_readers`, `_respawn_jlink` | `JLINK_SERVERS=[]` → entire RTT subsystem is a no-op | 🔴 Dead |
| 3.2 | `viewer._build_server_config()` | Method defined, never called (~100 lines dead UI) | 🔴 Dead |
| 3.3 | `viewer._waiting_for_boot` | Set declared, never `.add()`ed — boot-gate guard unreachable | 🔴 Dead |
| 3.4 | `viewer._launch()` `reconnect_cb` | Always `None` — callback path dead | 🔴 Dead |
| 3.5 | `viewer.py` + `runtest.py` | Kconfig helpers copy-pasted in both files | 🟡 Duplicate |
| 3.6 | `monitor.py` | Reimplements `find_thingy91x_ports` and `BAUD` | 🟡 Duplicate |
| 3.7 | `viewer.py` (5 methods) | SSH subprocess boilerplate repeated inline | 🟡 Duplicate |
| 3.8 | `set_scenario.sh` | Writes `APP_CLOUD_SECURITY_*` (old) instead of `APP_COAP_SECURITY_*` — CoAP path broken | 🟠 Stale |
| 3.9 | `ble.sh` | Missing `--sysbuild`; hardcodes conf file without existence check | 🟠 Stale |
| 3.10 | `README.md` | Documents `west.sh` / `flash.sh` which don't exist | 🟠 Stale |
| 3.11 | `ble.sh`, `lte.sh`, `sensor.sh` | Legacy `screen`/`jlink` aliases | 🟠 Stale |
| 3.12 | `viewer.py:18` | `ttk` imported but never used | 🟡 Unused |
