#!/usr/bin/env python3
"""
monitor.py — tail Thingy:91X serial ports.

Usage:
    python3 scripts/monitor.py              # auto-detect both ports
    python3 scripts/monitor.py --ble-only   # BLE console only  (if00)
    python3 scripts/monitor.py --lte-only   # LTE mirror only   (if02)
    python3 scripts/monitor.py --ble /dev/ttyACM0 --lte /dev/ttyACM1

Ports (stable names):
    BLE console : /dev/serial/by-id/usb-Nordic_Semiconductor_Thingy:91_X_UART_*-if00
    LTE mirror  : /dev/serial/by-id/usb-Nordic_Semiconductor_Thingy:91_X_UART_*-if02
"""

import argparse
import sys
import threading
import time
from pathlib import Path

import serial

_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from serial_io import find_thingy91x_ports
from ui_constants import BAUD

ANSI_RESET  = "\033[0m"
ANSI_CYAN   = "\033[96m"   # BLE console
ANSI_YELLOW = "\033[93m"   # LTE mirror


def reader(label: str, port: str, color: str, stop_event: threading.Event):
    prefix = f"{color}[{label}]{ANSI_RESET} "
    while not stop_event.is_set():
        try:
            with serial.Serial(port, BAUD, timeout=1) as ser:
                print(f"{color}[{label}]{ANSI_RESET} connected to {port}")
                buf = b""
                while not stop_event.is_set():
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", errors="replace").rstrip("\r")
                        print(f"{prefix}{text}")
        except serial.SerialException as e:
            if not stop_event.is_set():
                print(f"{color}[{label}]{ANSI_RESET} disconnected ({e}) — retrying in 2 s")
                time.sleep(2)
        except Exception as e:
            if not stop_event.is_set():
                print(f"{color}[{label}]{ANSI_RESET} error: {e} — retrying in 2 s")
                time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Monitor Thingy:91X serial ports")
    parser.add_argument("--ble", help="BLE console port (if00)")
    parser.add_argument("--lte", help="LTE mirror port (if02)")
    parser.add_argument("--ble-only", action="store_true",
                        help="Monitor BLE console (if00) only")
    parser.add_argument("--lte-only", action="store_true",
                        help="Monitor LTE mirror (if02) only")
    args = parser.parse_args()

    if args.ble_only and args.lte_only:
        print("ERROR: --ble-only and --lte-only are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    ble_port, lte_port = args.ble, args.lte

    # Auto-detect missing ports (skipped for the excluded side in single-port modes)
    if not args.lte_only and not ble_port:
        ble_port, auto_lte = find_thingy91x_ports()
        if not args.ble_only and not lte_port:
            lte_port = auto_lte
    elif not args.ble_only and not lte_port:
        _, lte_port = find_thingy91x_ports()

    if args.lte_only:
        ble_port = None
    if args.ble_only:
        lte_port = None

    if not ble_port and not lte_port:
        print("ERROR: no ports found. Pass --ble / --lte or check USB connection.",
              file=sys.stderr)
        sys.exit(1)

    stop = threading.Event()
    threads = []

    if ble_port:
        t = threading.Thread(
            target=reader, args=("BLE", ble_port, ANSI_CYAN, stop), daemon=True
        )
        t.start()
        threads.append(t)
    else:
        print(f"{ANSI_CYAN}[BLE]{ANSI_RESET} skipped (--lte-only)")

    if lte_port:
        t = threading.Thread(
            target=reader, args=("LTE", lte_port, ANSI_YELLOW, stop), daemon=True
        )
        t.start()
        threads.append(t)
    else:
        print(f"{ANSI_YELLOW}[LTE]{ANSI_RESET} skipped (--ble-only)")

    print("Monitoring — Ctrl+C to stop\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()


if __name__ == "__main__":
    main()
