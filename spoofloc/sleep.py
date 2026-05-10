"""System sleep prevention via macOS caffeinate."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from . import config as cfg_mod


def _pid_file() -> Path:
    return cfg_mod.cache_dir() / "caffeinate.pid"


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire() -> None:
    """Start caffeinate -s if not already running. Idempotent."""
    pf = _pid_file()
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            if _is_alive(pid):
                return
        except ValueError:
            pass
        pf.unlink(missing_ok=True)

    proc = subprocess.Popen(
        ["caffeinate", "-s"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pf.write_text(str(proc.pid))


def release() -> None:
    """Kill caffeinate and remove the PID file."""
    pf = _pid_file()
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text().strip())
        if _is_alive(pid):
            os.kill(pid, signal.SIGTERM)
    except (ValueError, PermissionError):
        pass
    pf.unlink(missing_ok=True)
