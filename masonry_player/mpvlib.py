"""Import python-mpv, making a bundled libmpv DLL findable on Windows."""

from __future__ import annotations

import os
import sys


def _candidate_dirs() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    dirs = [here, root, os.path.join(root, "lib"), os.getcwd()]
    if getattr(sys, "frozen", False):  # PyInstaller
        dirs.insert(0, os.path.dirname(sys.executable))
        dirs.insert(0, getattr(sys, "_MEIPASS", ""))
    return [d for d in dirs if d and os.path.isdir(d)]


if os.name == "nt":
    for d in _candidate_dirs():
        if any(os.path.exists(os.path.join(d, n)) for n in ("libmpv-2.dll", "mpv-2.dll", "mpv-1.dll")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(d)
            break

try:
    import mpv  # noqa: E402
except OSError as e:  # libmpv itself is missing
    mpv = None
    LOAD_ERROR = str(e)
else:
    LOAD_ERROR = None
