#!/usr/bin/env python3
"""
logviewer.py — tracker project dev console (entry point).

Run from tracker_project/ or tracker-utils/:
    python3 tracker-utils/logviewer.py

All logic lives in core/:
    core/build_config.py  — build/flash targets (read this for build questions)
    core/ui_constants.py  — display constants, colour palette, regex patterns
    core/utils.py         — _dbg, _stream_action
    core/serial_io.py     — port detection, serial_reader, rtt_reader
    core/server_io.py     — SSH log reader
    core/viewer.py        — LogViewer Tkinter class

Dependencies:
    pip install pyserial
"""

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE / "core"))

from serial_io import find_thingy91x_ports, find_thingy53_port
from viewer import LogViewer


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
