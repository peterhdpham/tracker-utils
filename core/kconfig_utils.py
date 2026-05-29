"""
kconfig_utils.py — Kconfig file manipulation helpers.

Shared by viewer.py and runtest.py.
"""

import re
from pathlib import Path


def _update_kconfig_key(path: Path, line: str):
    """Replace or append a single KEY=value line."""
    key  = line.split("=")[0]
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(key)}=.*", re.M)
    text = pat.sub(line, text) if pat.search(text) else (text.rstrip("\n") + "\n" + line + "\n")
    path.write_text(text)


def _set_kconfig_mode(path: Path, prefix: str, new_line: str):
    """Replace or append the unique CONFIG_<prefix>*=y line."""
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(prefix)}\w+=y", re.M)
    text = pat.sub(new_line, text) if pat.search(text) else (text.rstrip("\n") + "\n" + new_line + "\n")
    path.write_text(text)


def _set_kconfig_value(path: Path, key: str, value: bool):
    """Set KEY=y or KEY=n, overriding any existing value."""
    text = path.read_text() if path.exists() else ""
    pat  = re.compile(rf"^{re.escape(key)}=.*\n?", re.M)
    text = pat.sub("", text)
    text = text.rstrip("\n") + f"\n{key}={'y' if value else 'n'}\n"
    path.write_text(text)


def _read_kconfig_value(path: Path, key: str) -> str:
    """Return the unquoted value of KEY, or '' if not found."""
    if not path.exists():
        return ""
    m = re.search(rf'^{re.escape(key)}="?(.*?)"?\s*$', path.read_text(), re.M)
    return m.group(1) if m else ""
