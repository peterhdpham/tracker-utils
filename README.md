# Build & Flash Scripts

Four scripts. All must be run from the workspace root or any subdirectory — they resolve paths relative to their own location.

---

## `set_scenario.sh` — Configure a test scenario

Writes consistent Kconfig options to `local.conf` files for a given BLE + CoAP scenario
pair, preserving existing secrets (OSCORE keys, DTLS PSK, server hostname).

```
./scripts/set_scenario.sh --ble <B> [--coap <S>] [--server] [--restart]
./scripts/set_scenario.sh --coap <S> [--server] [--restart]
```

| BLE scenario | Description |
|---|---|
| `B0` | Broadcast plain (non-connectable ADV) |
| `B1` | GATT plain (no encryption) |
| `B3` | GATT + OSCORE E2E (thesis target) |

| CoAP scenario | Description |
|---|---|
| `S1` | Plain CoAP, port 5683 |
| `S2` | CoAP + OSCORE, port 5683 |
| `S3` | CoAP + DTLS, port 5684 |
| `S4` | DTLS + OSCORE, port 5684 (thesis full stack) |

| Option | Description |
|---|---|
| `--server` | Also update `tracker-server/.env` SECURITY_MODE |
| `--restart` | `--server` + `docker compose restart tracker-server` |

**Example — switch to full thesis stack:**
```bash
./scripts/set_scenario.sh --ble B3 --coap S4 --restart
./scripts/sensor.sh pristine   # BLE mode changed
./scripts/lte.sh               # CoAP mode changed
```

After running, rebuild affected targets as printed by the script.

---

## `verify.py` — Smoke-test the server after a scenario switch

Sends test CoAP messages with the correct credentials for the configured scenario
and prints PASS / FAIL for each check.  Reads credentials automatically from
`tracker-server/.env` and `tracker-server/oscore-context/`.

```
python3 scripts/verify.py --coap <S> [options]
```

| Option | Description |
|---|---|
| `--coap S1..S4` | Scenario to verify (required) |
| `--host HOST` | Override server hostname |
| `--oscore-ctx DIR` | Override OSCORE context directory |
| `--check-influx` | Query InfluxDB for recently written test data |

**Requirements:**
```bash
pip install "aiocoap[oscore,tinydtls]>=0.4.7" cbor2
pip install "influxdb-client[async]"  # only for --check-influx
```

**Example workflow:**
```bash
./scripts/set_scenario.sh --coap S2 --server --restart
# wait ~15 s for server restart
python3 scripts/verify.py --coap S2
```

Expected output for S2:
```
Host     : klosteret.duckdns.org
Scenario : S2

S2: OSCORE → coap://klosteret.duckdns.org/data
  PASS  plain CoAP rejected (4.01 Unauthorized)
  PASS  correct-key OSCORE accepted (2.04 Changed)
  PASS  wrong-key OSCORE rejected (4.01 Unauthorized)

All 3 check(s) passed
```

---

---

## `west.sh` — Build (and optionally flash)

```
./scripts/west.sh <target> [pristine] [flash]
```

| Argument | Description |
|---|---|
| `target` | `lte` \| `ble` \| `sensor` |
| `pristine` | Clean build (deletes CMake cache before building) |
| `flash` | Flash the device after a successful build |

**Examples:**

```bash
# Build only
./scripts/west.sh lte
./scripts/west.sh ble
./scripts/west.sh sensor

# Clean build
./scripts/west.sh lte pristine

# Build then flash
./scripts/west.sh lte flash
./scripts/west.sh ble flash

# Clean build then flash
./scripts/west.sh sensor pristine flash
```

---

## `flash.sh` — Flash only (skips build)

Use this when the firmware is already built and you just want to re-flash.

```
./scripts/flash.sh <target>
```

```bash
./scripts/flash.sh lte
./scripts/flash.sh ble
./scripts/flash.sh sensor
```

---

## Targets

| Target | SoC | Board | Flash method |
|---|---|---|---|
| `lte` | nRF9151 | `thingy91x/nrf9151/ns` | J-Link SWD — SW2 → nRF91 |
| `ble` | nRF5340 | `thingy91x/nrf5340/cpuapp` | J-Link SWD — SW2 → nRF53 |
| `sensor` | nRF5340 | `thingy53/nrf5340/cpuapp` | MCUboot USB DFU (hold button + reset) |

---

## Hardware setup

### `lte` — nRF9151 via J-Link

1. Connect the 10-pin SWD cable from the external debug probe to **P8** on the Thingy:91 X.
2. Set **SW2** to **nRF91**.
3. Connect the debug probe to your computer via USB.
4. Power on the Thingy:91 X (**SW1 → ON**).

### `ble` — nRF5340 on Thingy:91 X via J-Link

1. Connect the 10-pin SWD cable from the external debug probe to **P8**.
2. Set **SW2** to **nRF53**.
3. Connect the debug probe to your computer via USB.
4. Power on the Thingy:91 X (**SW1 → ON**).

**Recovery (if the device stops showing up in `nrfutil device list`):**

This happens if the nRF5340 app is flashed without USB support. Recover via J-Link SWD:

1. Connect the 10-pin SWD cable from the external debug probe to **P8**.
2. Set **SW2** to **nRF53**.
3. Run:
   ```bash
   nrfutil device program \
     --firmware build/ble/merged.hex \
     --serial-number <J-Link serial> \
     --traits jlink \
     --x-family nrf53
   ```

### `sensor` — nRF5340 on Thingy:53 via USB DFU

1. Connect the Thingy:53 to your computer via USB-C.
2. **Enter bootloader mode:** hold the button on the Thingy:53 while power-cycling (or hold button + press reset).
3. The device should appear with the `mcuBoot` trait in `nrfutil device list`.

Check connected devices at any time:

```bash
nrfutil device list
```

---

## Build output locations

| Target | Build directory |
|---|---|
| `lte` | `build/lte/` |
| `ble` | `build/ble/` |
| `sensor` | `build/sensor/` |

The `ble` and `sensor` builds produce `dfu_application.zip` in their build directories — this is what gets flashed via MCUboot DFU.
