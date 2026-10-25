"""Helpers for `factory up`: port check and opening the user's browser (incl. WSL)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path


def is_wsl() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def port_free(host: str, port: int) -> bool:
    """True if a server could listen on host:port now (same semantics as uvicorn)."""
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
        if sys.platform != "win32":
            # Ignore TIME_WAIT leftovers of a previous run, as uvicorn does. (On Windows,
            # SO_REUSEADDR would allow binding over a live listener, so it is not set.)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def open_browser(url: str) -> str | None:
    """Open `url` in the user's browser. Returns how, or None if nothing worked.

    Inside WSL the Linux side usually has no browser, so hand the URL to Windows
    (WSL2 forwards localhost, so the same URL works there).
    """
    if is_wsl():
        for command in ("wslview", "explorer.exe"):
            if shutil.which(command):
                subprocess.Popen(
                    [command, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return command
    try:
        return "webbrowser" if webbrowser.open(url) else None
    except webbrowser.Error:
        return None
