"""
jlink.py — JLinkGDBServer process management.

Spawns and terminates JLinkGDBServer processes for RTT logging.
Currently JLINK_SERVERS is empty because all three devices log via USB
CDC-ACM; this module is retained for when RTT is re-enabled.
"""

import queue
import shutil
import subprocess
import threading
import time

from utils import _dbg

# ── Server definitions ────────────────────────────────────────────────────────────
#
# Each entry describes one JLinkGDBServer instance:
#   label     — panel name to route output to
#   device    — J-Link device string (e.g. "NRF5340_XXAA_APP")
#   serial    — programmer serial number
#   gdb_port  — GDB server port
#   rtt_port  — RTT telnet port (read by rtt_reader in serial_io.py)

JLINK_SERVERS: list[dict] = [
    # All three devices currently log via USB CDC-ACM; RTT is unused.
]


# ── Internal helpers ──────────────────────────────────────────────────────────────

def _find_jlink_server() -> str | None:
    for name in ("JLinkGDBServerCL", "JLinkGDBServer", "JLinkGDBServerExe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _pipe_jlink_stderr(srv: dict, proc: subprocess.Popen, q: queue.Queue):
    """Forward JLinkGDBServer stderr lines to the panel queue in real time."""
    if proc.stderr:
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                q.put((srv["label"], time.time(), f"  [jlink] {line}", "build"))
    q.put((srv["label"], time.time(),
           f"[JLinkGDBServer exited (code {proc.returncode})]", "build"))


# ── Public API ────────────────────────────────────────────────────────────────────

def spawn_jlink_servers(servers: list[dict], q: queue.Queue) -> list[subprocess.Popen]:
    """Start one JLinkGDBServer process per entry in *servers*. Returns the procs."""
    _dbg(f"spawn_jlink_servers: starting {len(servers)} server(s)")
    if not servers:
        return []
    exe = _find_jlink_server()
    if not exe:
        _dbg("spawn_jlink_servers: JLinkGDBServer not found in PATH")
        for srv in servers:
            q.put((srv["label"], time.time(),
                   "[JLinkGDBServer not found in PATH — RTT unavailable]", "build"))
        return []
    procs = []
    for srv in servers:
        cmd = [
            exe,
            "-device",        srv["device"],
            "-if",            "SWD",
            "-speed",         "4000",
            "-select",        f"USB={srv['serial']}",
            "-port",          str(srv["gdb_port"]),
            "-rtttelnetport", str(srv["rtt_port"]),
            "-nogui",
            "-autoconnect",   "1",
        ]
        q.put((srv["label"], time.time(),
               f"[spawning: {' '.join(cmd)}]", "build"))
        try:
            _dbg(f"  spawning: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)
            _dbg(f"  {srv['label']} JLinkGDBServer PID {proc.pid}")
            q.put((srv["label"], time.time(),
                   f"[JLinkGDBServer PID {proc.pid} — RTT telnet on :{srv['rtt_port']}]",
                   "build"))
            threading.Thread(target=_pipe_jlink_stderr, args=(srv, proc, q),
                             daemon=True).start()
        except Exception as e:
            _dbg(f"  failed to spawn {srv['label']} JLinkGDBServer: {e}")
            q.put((srv["label"], time.time(),
                   f"[Failed to start JLinkGDBServer: {e}]", "build"))
    _dbg(f"spawn_jlink_servers: done, {len(procs)} started")
    return procs


def stop_jlink_servers(procs: list[subprocess.Popen]):
    """Terminate all JLinkGDBServer processes, SIGKILL after 3 s."""
    _dbg(f"stop_jlink_servers: terminating {len(procs)} process(es)")
    for proc in procs:
        try:
            proc.terminate()
            _dbg(f"  terminate → PID {proc.pid}")
        except Exception as e:
            _dbg(f"  terminate PID {proc.pid} failed: {e}")
    for proc in procs:
        try:
            proc.wait(timeout=3)
            _dbg(f"  PID {proc.pid} exited (code {proc.returncode})")
        except subprocess.TimeoutExpired:
            _dbg(f"  PID {proc.pid} timeout — sending SIGKILL")
            try:
                proc.kill()
                proc.wait(timeout=2)
                _dbg(f"  PID {proc.pid} killed")
            except Exception as e:
                _dbg(f"  kill PID {proc.pid} failed: {e}")
        except Exception as e:
            _dbg(f"  wait PID {proc.pid} failed: {e}")
    _dbg("stop_jlink_servers: done")
