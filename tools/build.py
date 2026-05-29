#!/usr/bin/env python3
"""
build.py — CLI build/flash wrapper around build_config.py.

Called by ble.sh, lte.sh, sensor.sh so that build_config.py is the
single source of truth for all west commands and conf-file logic.

Usage:
    python3 tracker-utils/build.py build <target> [--pristine]
    python3 tracker-utils/build.py flash <target>

Targets: lte  ble  sensor
"""

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from build_config import TARGETS, NRFUTIL_WRAP, _effective_build_cmd


def cmd_build(key: str, pristine: bool) -> int:
    tgt = TARGETS[key]
    cmd = NRFUTIL_WRAP + _effective_build_cmd(tgt)
    if pristine:
        wi  = cmd.index("west")
        cmd = cmd[:wi + 2] + ["--pristine"] + cmd[wi + 2:]
    return subprocess.run(cmd, cwd=tgt["cwd"]).returncode


def cmd_flash(key: str) -> int:
    tgt = TARGETS[key]
    for pre in tgt.get("pre_flash_cmds", []):
        r = subprocess.run(pre)
        if r.returncode != 0:
            return r.returncode
    cmd = NRFUTIL_WRAP + tgt["flash_cmd"]
    return subprocess.run(cmd, cwd=tgt["cwd"]).returncode


def main():
    ap = argparse.ArgumentParser(description="Build/flash tracker firmware targets")
    ap.add_argument("action", choices=["build", "flash"])
    ap.add_argument("target", choices=list(TARGETS))
    ap.add_argument("--pristine", action="store_true")
    args = ap.parse_args()
    if args.action == "build":
        sys.exit(cmd_build(args.target, args.pristine))
    else:
        sys.exit(cmd_flash(args.target))


if __name__ == "__main__":
    main()
