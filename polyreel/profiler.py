"""Optional performance log, for troubleshooting slowdowns on a particular
machine.  Off unless enabled in Settings (or with ``--profile``).

It records what the app is doing (players starting and stopping, plans,
benchmark rounds), a snapshot every second, and every time the window stops
responding: for how long, and the code it was busy in.  If it is stuck for a
couple of seconds, every thread's stack is written out by Python's
faulthandler, which works even when Python itself is blocked.
"""

from __future__ import annotations

import faulthandler
import os
import platform
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

STALL_NOTE = 0.25    # window unresponsive this long: note it, and where it is busy
STALL_AGAIN = 1.0    # ...note where it is again at this point
STALL_DUMP = 2.0     # ...and at this point write out every thread's stack
KEEP_LOGS = 10


class Profiler:
    def __init__(self) -> None:
        self.enabled = False
        self.path: Path | None = None
        self._file = None
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._beat = 0.0
        self._main = threading.main_thread().ident
        self._cpu = (time.monotonic(), time.process_time())
        self._worst_gap = 0.0
        self._noted: set[str] = set()
        self._watching = False

    # -- lifecycle -----------------------------------------------------------
    def start(self, folder: str, header: dict) -> Path:
        if self.enabled:
            return self.path
        logs = Path(folder)
        logs.mkdir(parents=True, exist_ok=True)
        for old in sorted(logs.glob("polyreel-*.log"))[:-(KEEP_LOGS - 1)]:
            try:
                old.unlink()
            except OSError:
                pass
        self.path = logs / f"polyreel-{datetime.now():%Y%m%d-%H%M%S}.log"
        self._file = open(self.path, "w", encoding="utf-8", buffering=1)
        self._t0 = time.monotonic()
        self._beat = time.monotonic()
        self._cpu = (time.monotonic(), time.process_time())
        self._noted = set()
        self._watching = True
        self.enabled = True
        self._file.write(f"Polyreel performance log, {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        for key, value in header.items():
            self._file.write(f"  {key}: {value}\n")
        self._file.write("\nseconds  what\n")
        threading.Thread(target=self._watch, daemon=True, name="profiler").start()
        return self.path

    def stop(self) -> None:
        if not self.enabled:
            return
        self.log("log", "stopped")
        self.enabled = False
        faulthandler.cancel_dump_traceback_later()
        with self._lock:
            self._file.close()
            self._file = None

    # -- recording -----------------------------------------------------------
    def log(self, kind: str, text: str) -> None:
        """Add a line (any thread)."""
        if not self.enabled:
            return
        line = f"{time.monotonic() - self._t0:8.3f}  [{kind}] {text}\n"
        with self._lock:
            if self._file:
                self._file.write(line)

    def quiet(self) -> None:
        """Keep logging, but stop watching for freezes (when closing)."""
        self._watching = False
        faulthandler.cancel_dump_traceback_later()

    def note_once(self, key: str) -> bool:
        """True the first time ``key`` is asked about in this log."""
        if key in self._noted:
            return False
        self._noted.add(key)
        return True

    def beat(self) -> None:
        """Called by the window's thread a few times a second."""
        if not self.enabled or not self._watching:
            return
        now = time.monotonic()
        self._worst_gap = max(self._worst_gap, now - self._beat)
        self._beat = now
        # Re-armed on every beat, so it fires only if the window's thread
        # stops beating for STALL_DUMP seconds.
        faulthandler.dump_traceback_later(STALL_DUMP, repeat=False, file=self._file)

    def snapshot(self, **fields) -> None:
        """Once a second: the app's numbers plus CPU, memory and threads."""
        if not self.enabled:
            return
        now, cpu = time.monotonic(), time.process_time()
        then, cpu_then = self._cpu
        self._cpu = (now, cpu)
        share = (cpu - cpu_then) / max(1e-6, now - then) / (os.cpu_count() or 1)
        fields.update(cpu=f"{share * 100:.0f}%", mem=f"{_memory_mb():.0f}MB",
                      threads=threading.active_count(), ui_gap=f"{self._worst_gap * 1000:.0f}ms")
        self._worst_gap = 0.0
        self.log("stats", " ".join(f"{k}={v}" for k, v in fields.items()))

    # -- watchdog ------------------------------------------------------------
    def _watch(self) -> None:
        stalled_since = None
        noted = 0
        while self.enabled:
            time.sleep(0.05)
            if not self._watching:
                continue
            gap = time.monotonic() - self._beat
            if gap >= STALL_NOTE:
                if stalled_since is None:
                    stalled_since, noted = self._beat, 0
                if noted == 0 or (noted == 1 and gap >= STALL_AGAIN):
                    noted += 1
                    self.log("stall", f"window unresponsive for {gap:.2f}s so far; busy in:\n"
                                      + self._main_stack())
            elif stalled_since is not None:
                self.log("stall", f"window unresponsive for {self._beat - stalled_since:.2f}s in all")
                stalled_since = None

    def _main_stack(self) -> str:
        frame = sys._current_frames().get(self._main)
        if frame is None:
            return "          (unknown)"
        lines = traceback.format_stack(frame)[-8:]
        return "".join("          " + line.replace("\n", "\n          ").rstrip() + "\n"
                       for line in lines).rstrip("\n")


def _memory_mb() -> float:
    """This process's memory in use (working set), in MB."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            c = Counters()
            c.cb = ctypes.sizeof(c)
            if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return 0.0
            return c.WorkingSetSize / 2**20
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except Exception:
        return 0.0


def system_info() -> dict:
    import PySide6
    return {"os": platform.platform(), "cpu": f"{platform.processor() or platform.machine()}, "
                                              f"{os.cpu_count()} logical cores",
            "python": sys.version.split()[0], "qt": PySide6.__version__}


def log_folder() -> str:
    """Where performance logs go (e.g. %LOCALAPPDATA%\\Polyreel\\Polyreel\\logs)."""
    from PySide6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
    return str(Path(base) / "logs")


PROFILER = Profiler()
