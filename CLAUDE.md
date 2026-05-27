# tracker-utils

Developer tooling for the tracker project.

## Module map

The dev console is split across several files. **Read only what you need.**

| File | Purpose | Read when… |
|---|---|---|
| `build_config.py` | NCS version, TARGETS dict, paths, `_effective_build_cmd` | Any build/flash question |
| `ui_constants.py` | PALETTE, SOURCE_COLOR, BAUD, SOURCES, regex, COAP/BLE mode defs | UI or display question |
| `utils.py` | `_dbg`, `_stream_action` | Subprocess/logging question |
| `serial_io.py` | Port detection, `serial_reader`, `rtt_reader` | Serial/UART question |
| `jlink.py` | JLinkGDBServer spawn/stop | RTT question |
| `server_io.py` | `ssh_log_reader`, SSH constants | Server SSH question |
| `viewer.py` | `LogViewer` Tkinter class | UI layout or event-handling question |
| `logviewer.py` | Entry point (~30 lines) — argparse + `main()` | Entry point only |

Other files:

| File | Purpose |
|---|---|
| `ble.sh` / `lte.sh` / `sensor.sh` | Shell wrappers for building/flashing individual targets |
| `set_scenario.sh` | Sets security mode — uses **old** Kconfig names (`APP_CLOUD_SECURITY_*`); needs updating to `APP_COAP_SECURITY_*` |
| `monitor.py` | Standalone serial monitor (no GUI) |
| `verify.py` | Post-flash verification helper |

---

## build_config.py — key constants

| Name | Value | Purpose |
|---|---|---|
| `NCS_VERSION` | `"v3.1.1"` | Passed to `nrfutil sdk-manager toolchain launch` |
| `NRFUTIL_WRAP` | list | Prefix prepended to every `west` call |
| `SNR_HUB` | `"1051217937"` | nRF9151 DK → Thingy:91X |
| `SNR_SENSOR` | `"1050337728"` | nRF53 DK (PCA10095) → Thingy:53 |
| `TARGETS` | dict | Per-target build/flash config (see below) |

### TARGETS dict schema

Each key (`"lte"`, `"ble"`, `"sensor"`) maps to:
```python
{
    "label":               str,          # display name
    "panels":              [str, ...],   # which SOURCES panels to update
    "snr":                 str,          # programmer serial number
    "cwd":                 str,          # working directory for west
    "build_cmd":           [str, ...],   # bare west args (prepend NRFUTIL_WRAP)
    "optional_conf":       Path,         # appended as EXTRA_CONF_FILE= if it exists
    "security_conf_files": [Path, ...],  # also appended if they exist
    "cmake_image":         str,          # (ble only) image-scoped cmake var prefix
    "pre_flash_cmds":      [[str, ...]], # raw commands run before west flash (no wrap)
    "flash_cmd":           [str, ...],   # bare west args (prepend NRFUTIL_WRAP)
}
```

`_effective_build_cmd(tgt)` merges `optional_conf` + `security_conf_files` into
`-DEXTRA_CONF_FILE=` (or `-D<cmake_image>_EXTRA_CONF_FILE=` for BLE) only when
files exist on disk.

### Flash target routing

| Target | Board | Programmer | What gets flashed |
|---|---|---|---|
| `lte` | `thingy91x/nrf9151/ns` | `SNR_HUB` | nRF9151 app + MCUboot (sysbuild) |
| `ble` | `thingy91x/nrf5340/cpuapp` | `SNR_HUB` | nRF5340 BLE app (separate build) |
| `sensor` | `thingy53/nrf5340/cpuapp` | `SNR_SENSOR` | Thingy:53 nRF5340 app |

Do NOT build the LTE sysbuild and flash both cores with the nRF9151 DK — it only
supports NRF91 family and rejects the NRF53 image with a `WrongFamily` error.

---

## serial_io.py — key behaviour

**Reconnect loop:** opens port with `timeout=0.1`; sleeps 50 ms on `SerialException`;
puts `(source, ts, text, kind)` tuples into a shared `queue.Queue`.
- `kind="dev"` — firmware log line
- `kind="status"` — connect / disconnect events
- `kind="build"` — build/flash output

**Known USB CDC-ACM timing issue:** The nRF5340 re-enumerates its CDC-ACM port
when the BLE HCI controller starts (shared HFCLK), causing 1-3 rapid
disconnect/reconnect cycles within ~500 ms of boot. Output during those cycles is
lost — hardware behaviour, not fixable from Python.

---

## viewer.py — key behaviour

### `_append(source, ts, msg, kind)`

Central display logic:
1. **Status message filtering:** raw `[disconnected:]` / `[connected →]` messages
   are dropped unless the source is in `_resetting_sources`, where they become
   `[↺ Resetting …]` / `[↺ … rebooted]`.
2. **`_waiting_for_boot` gate:** when set, all messages are dropped until
   `*** Booting` is seen. Only set during explicit flash flows — NOT during Refresh.
3. Appends to `_all_lines[source]` (capped at 6000, trims 1000 at a time).
4. Colours by `kind` and log level tag (`<inf>`, `<err>`, etc.).

### Reset / Refresh

| Button | Method | What it does |
|---|---|---|
| **Reset 91X** | `_reset_91x()` | Sends `reset all` to BLE shell (GPIO pulse to nRF9151 RESET_N) |
| **Reset 53** | `_do_reset(SNR_SENSOR, …)` | `nrfutil device reset --serial-number <SNR_SENSOR>` |
| **Refresh** | `_refresh_all()` | Clears panels, resets 91X via BLE shell, resets 53 via nrfutil |

**Important:** The 91X can ONLY be reset via the BLE shell `reset all` command.
`nrfutil device reset --serial-number 1051217937` does NOT reach the nRF9151 reset line.

### Build & Flash

- `_snr_try_acquire` / `_snr_release`: mutex per programmer SNR — prevents two
  targets sharing the same DK from flashing simultaneously.
- `pre_flash_cmds` run WITHOUT the nrfutil wrap (used for `nrfutil device recover`
  before the BLE app flash).

---

## set_scenario.sh — known issue

Uses old Kconfig symbol names `APP_CLOUD_SECURITY_*`. Current firmware uses
`APP_COAP_SECURITY_*`. Needs updating before it can correctly configure
`local.conf` for DTLS/OSCORE modes.
