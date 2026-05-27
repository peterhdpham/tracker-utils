"""
serial_io.py — USB serial port detection and reader threads.

Covers:
  • find_thingy91x_ports() / find_thingy53_port()  — USB CDC-ACM auto-detection
  • serial_reader()  — reconnecting serial reader thread
  • rtt_reader()     — RTT telnet reader thread (via JLinkGDBServer)
"""

import glob
import queue
import socket
import threading
import time

import serial

from ui_constants import BAUD, strip_ansi
from utils import _dbg


# ── Port auto-detection ──────────────────────────────────────────────────────────

def find_thingy91x_ports():
    """Return (if00, if02) for the first Thingy:91X found, or (None, None)."""
    pattern = "/dev/serial/by-id/usb-Nordic_Semiconductor_Thingy*91*"
    ports   = sorted(glob.glob(pattern))
    if00    = next((p for p in ports if p.endswith("-if00")), None)
    if02    = next((p for p in ports if p.endswith("-if02")), None)
    return if00, if02


def find_thingy53_port():
    """Return the first CDC-ACM port for a Thingy:53, or None."""
    # May enumerate as either a board-named or generic Zephyr CDC-ACM device.
    for pattern in ("/dev/serial/by-id/*Thingy*53*",
                    "/dev/serial/by-id/*Zephyr*CDC_ACM*"):
        ports = sorted(glob.glob(pattern))
        port  = next((p for p in ports if p.endswith("-if00")), None)
        if port:
            return port
    return None


# ── Serial reader ────────────────────────────────────────────────────────────────

def serial_reader(source: str, port: str, q: queue.Queue, stop: threading.Event,
                  quiet: threading.Event | None = None, reconnect_cb=None,
                  write_q: queue.Queue | None = None):
    """
    Reconnecting serial reader for one CDC-ACM device.

    Puts (source, timestamp, text, kind) tuples into *q*:
      kind="dev"    — firmware log line
      kind="status" — connect / disconnect events

    Known timing issue: The nRF5340 re-enumerates its CDC-ACM port when the BLE
    HCI controller starts (shared HFCLK), causing 1-3 rapid disconnect/reconnect
    cycles within ~500 ms of boot. Output during those cycles is lost — this is
    hardware behaviour, not fixable from Python.
    """
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
                time.sleep(0.05)   # fast retry — catch device reboots quickly
        except Exception as e:
            if not stop.is_set():
                if was_connected:
                    _dbg(f"serial_reader [{source}]: error: {e}")
                if not (quiet and quiet.is_set()) and was_connected:
                    q.put((source, time.time(), f"[error: {e}]", "status"))
                was_connected = False
                time.sleep(0.05)


# ── RTT reader ───────────────────────────────────────────────────────────────────

def rtt_reader(source: str, host: str, port: int,
               q: queue.Queue, stop: threading.Event,
               startup_delay: float = 2.5):
    """
    Read RTT output from JLinkGDBServer's telnet port.

    Waits *startup_delay* seconds for the GDB server to attach before connecting.
    Retries every 3 s on connection failure.
    """
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
