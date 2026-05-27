"""
utils.py — shared debug logging and subprocess streaming helper.

Imported by all other modules that need _dbg or _stream_action.
"""

import queue
import subprocess
import sys
import time
from datetime import datetime


def _dbg(msg: str):
    """Write a timestamped debug line to stderr."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[logviewer] {ts}  {msg}", file=sys.stderr, flush=True)


def _stream_action(tag: str, panels: list[str], cwd: str,
                   cmd: list[str], q: queue.Queue, done_cb=None):
    """
    Run *cmd* in a subprocess, stream its stdout+stderr into *q* as build lines,
    then call *done_cb(ok)* when it exits.

    Intended to run in a daemon thread — never call from the Tkinter main thread.
    """
    _dbg(f"_stream_action [{tag}]: $ {' '.join(cmd)}")
    for p in panels:
        q.put((p, time.time(), f"[{tag}] $ {' '.join(cmd[-5:])}", "build"))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        _dbg(f"_stream_action [{tag}]: PID {proc.pid}")
        for line in (proc.stdout or []):
            line = line.rstrip()
            if line:
                for p in panels:
                    q.put((p, time.time(), f"  {line}", "build"))
        proc.wait()
        ok  = proc.returncode == 0
        _dbg(f"_stream_action [{tag}]: exit {proc.returncode} ({'OK' if ok else 'FAILED'})")
        msg = f"[{tag}: {'OK' if ok else f'FAILED (exit {proc.returncode})'}]"
        for p in panels:
            q.put((p, time.time(), msg, "build"))
        if done_cb:
            done_cb(ok)
    except Exception as e:
        _dbg(f"_stream_action [{tag}]: exception: {e}")
        for p in panels:
            q.put((p, time.time(), f"[{tag}: error: {e}]", "build"))
        if done_cb:
            done_cb(False)
