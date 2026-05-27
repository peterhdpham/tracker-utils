"""
server_io.py — SSH connection to the tracker-server on CT 101.

Contains:
  • SSH_SERVER_HOST / SSH_SERVER_CMD  — connection constants
  • ssh_log_reader()                  — reconnecting SSH log-tail thread
"""

import queue
import subprocess
import time

from ui_constants import strip_ansi
from utils import _dbg

# ── Server connection constants ───────────────────────────────────────────────────

SSH_SERVER_HOST = "root@10.10.10.10"
SSH_SERVER_CMD  = "cd tracker-server && docker compose logs -f"

# ── SSH log reader ────────────────────────────────────────────────────────────────

def ssh_log_reader(source: str, host: str, remote_cmd: str,
                   q: queue.Queue, stop,
                   retry_delay: float = 5.0):
    """
    Stream remote docker-compose logs over SSH into *q*.

    Reconnects automatically after SSH exits or fails. Each line is put as
    (source, timestamp, text, "dev").
    """
    _dbg(f"ssh_log_reader [{source}]: connecting to {host}")
    while not stop.is_set():
        q.put((source, time.time(), f"[SSH connecting → {host}]", "status"))
        try:
            proc = subprocess.Popen(
                ["ssh",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ServerAliveInterval=10",
                 "-o", "ConnectTimeout=10",
                 host, remote_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _dbg(f"ssh_log_reader [{source}]: PID {proc.pid}")
            for raw in proc.stdout:
                if stop.is_set():
                    break
                line = strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
                if line and "Ignoring unparsable message" not in line:
                    q.put((source, time.time(), line, "dev"))
            proc.wait()
            if not stop.is_set():
                q.put((source, time.time(),
                       f"[SSH closed (exit {proc.returncode}) — retrying in {retry_delay:.0f}s]",
                       "status"))
                time.sleep(retry_delay)
        except Exception as e:
            if not stop.is_set():
                _dbg(f"ssh_log_reader [{source}]: error: {e}")
                q.put((source, time.time(),
                       f"[SSH error: {e} — retrying in {retry_delay:.0f}s]", "status"))
                time.sleep(retry_delay)
