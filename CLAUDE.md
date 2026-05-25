# tracker-utils

Developer tooling for the tracker project.

## Files

| File | Purpose |
|---|---|
| `logviewer.py` | Main dev console — 3-panel GUI, serial reader, build/flash integration |
| `ble.sh` / `lte.sh` / `sensor.sh` | Shell wrappers for building/flashing individual targets |
| `set_scenario.sh` | Sets security mode in `.env.testing` and `local.conf` files. Uses old Kconfig names (`APP_CLOUD_SECURITY_*`) — needs updating to `APP_COAP_SECURITY_*` |
| `monitor.py` | Standalone serial monitor (no GUI) |
| `verify.py` | Post-flash verification helper |

---

## logviewer.py

Tkinter GUI that reads three USB CDC-ACM serial ports simultaneously and provides
build/flash buttons for each target.

### Key constants (top of file)

| Name | Value | Purpose |
|---|---|---|
| `NCS_VERSION` | `"v3.1.1"` | Passed to `nrfutil sdk-manager toolchain launch` for all west commands |
| `NRFUTIL_WRAP` | list | Prefix applied to every `west` call |
| `SOURCES` | `["BLE", "LTE", "Thingy53"]` | Panel names — order matters for UI layout |
| `TARGETS` | dict | Per-target build/flash config (see below) |
| `BAUD` | `115200` | Serial baud rate for all devices |

### TARGETS dict

Each key (`"lte"`, `"ble"`, `"sensor"`) maps to:
```python
{
    "label":          str,          # display name
    "panels":         [str, ...],   # which SOURCES panels to update
    "snr":            str,          # nrfutil serial number
    "cwd":            str,          # working directory for west
    "build_cmd":      [str, ...],   # bare west args (no nrfutil wrap)
    "optional_conf":  Path,         # if exists, appended as -DEXTRA_CONF_FILE=
    "pre_flash_cmds": [[str, ...]], # raw commands run before west flash (no wrap)
    "flash_cmd":      [str, ...],   # bare west args
}
```

`_effective_build_cmd(tgt)` appends `-- -DEXTRA_CONF_FILE=<path>` only when
`optional_conf` exists on disk. This is how `local.conf` is injected without
hardcoding it in the committed build command.

### Serial reader (`serial_reader` function, line ~315)

One thread per source. Reconnect loop:
- Opens port with `timeout=0.1`; reads up to 256 bytes per iteration
- On `SerialException` (disconnect): sleeps **50 ms** then retries
- Puts `(source, ts, text, kind)` tuples into the shared `queue.Queue`
- `kind` values: `"dev"` (firmware log), `"status"` (connect/disconnect events),
  `"build"` (build/flash output)

**Known USB CDC-ACM timing issue:** The nRF5340 (both Thingy:91X BLE app and
Thingy:53) re-enumerates its USB CDC-ACM port when the BLE HCI controller starts
(shared HFCLK). This causes 1-3 rapid disconnect/reconnect cycles within ~500 ms
of boot. Any firmware output during those cycles is lost. This is hardware
behaviour — not fixable from Python.

### Queue processor (`_poll`, line ~779)

Runs every 25 ms on the Tkinter main thread via `after()`. Drains the queue and
calls `_append` for each item.

### `_append(source, ts, msg, kind)` (line ~795)

Central display logic:
1. **Status message filtering**: raw `[disconnected:]` and `[connected →]` messages
   are dropped unless the source is in `_resetting_sources`. When in
   `_resetting_sources`, they're transformed to `[↺ Resetting …]` /
   `[↺ … rebooted]` respectively.
2. **`_waiting_for_boot` gate**: if set for a source, all messages are dropped until
   `*** Booting` is seen. Only set during explicit build+flash flows — NOT during
   Refresh (removed to prevent the "screen goes black and never recovers" problem).
3. Appends to `_all_lines[source]` buffer (capped at 6000 lines, trims 1000 at a time).
4. Applies colour based on `kind` and log level tag (`<inf>`, `<err>`, etc.).

### Reset / Refresh

| Button | Code | What it does |
|---|---|---|
| **Reset 91X** | `_reset_91x()` | Sends `reset all` to BLE shell (resets nRF9151 via GPIO P1.07, then reboots nRF5340) |
| **Reset 53** | `_do_reset("1050065248", …)` | `nrfutil device reset --serial-number 1050065248` |
| **Refresh** | `_refresh_all()` | Clears all panels, sends `reset all` to BLE shell, resets Thingy:53 via nrfutil in background thread |

**Important:** The 91X can ONLY be reset via the BLE shell `reset all`/`reset lte`
command (shell sends a GPIO pulse to nRF9151 RESET_N). `nrfutil device reset
--serial-number 1051217937` does NOT reach the nRF9151 reset line.

`_resetting_sources` is a `set` used to track in-progress resets so that
disconnect/reconnect messages are shown as `↺ Resetting` / `↺ rebooted` instead
of raw text. Sources are added on reset start and discarded on reconnect.

### Build & Flash (`_do_build`, `_do_flash`, `_do_build_and_flash`)

- `_snr_try_acquire` / `_snr_release`: mutex per programmer serial number so two
  targets sharing the same DK can't flash simultaneously.
- Build output is streamed into the target's panel(s) via `_stream_action`.
- After a successful flash, `_waiting_for_boot` is set for that target's panels so
  the panel clears and waits for `*** Booting` before showing output.
- `pre_flash_cmds` (if present) are run WITHOUT `nrfutil sdk-manager` wrap — used
  for `nrfutil device recover` before the BLE app flash.

### Flash targets and programmer routing

| Target | Board | Programmer SNR | What gets flashed |
|---|---|---|---|
| `lte` | `thingy91x/nrf9151/ns` | `1051217937` | nRF9151 app + MCUboot (sysbuild) |
| `ble` | `thingy91x/nrf5340/cpuapp` | `1051217937` | nRF5340 app only (separate build) |
| `sensor` | `thingy53/nrf5340/cpuapp` | `1050065248` | nRF5340 app (Thingy:53) |

The LTE and BLE hub apps are built and flashed **separately** even though they
run on the same board. Do NOT build the LTE sysbuild and try to flash both cores
with the nRF9151 DK — the DK's J-Link only supports NRF91 family and will reject
the NRF53 image with a `WrongFamily` error.

### `_shell_cmd(source, cmd)`

Sends a string (+ `\r\n`) to the write queue for that source's serial port and
echoes it to the panel. Only works when the port is connected (`_write_queues`
contains the source key).

### Hotplug watcher (`_hotplug_watcher`, line ~755)

Background thread. Polls `find_thingy91x_ports()` and `find_thingy53_port()` every
2 seconds. If a previously-missing port appears, restarts all serial readers.

---

## set_scenario.sh — known issue

Uses old Kconfig symbol names `APP_CLOUD_SECURITY_*`. Current firmware uses
`APP_COAP_SECURITY_*`. The script needs updating before it can correctly configure
`local.conf` for DTLS/OSCORE modes.
