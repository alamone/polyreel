"""Entry point: ``python -m polyreel [files or folders...]``."""

from __future__ import annotations

import locale
import sys
from pathlib import Path

from . import __version__


def self_test() -> int:
    """Check that Qt and libmpv load and a player can start, without opening
    a window.  Used by the release build to test the packaged program."""
    import PySide6.QtWidgets  # noqa: F401

    from .mpvlib import LOAD_ERROR, mpv
    if LOAD_ERROR:
        print("libmpv failed to load:", LOAD_ERROR)
        return 2
    locale.setlocale(locale.LC_NUMERIC, "C")
    player = mpv.MPV(vo="null", ao="null", idle="yes")
    print("Polyreel", __version__, "- libmpv", player.mpv_version)
    player.terminate()
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    # Many players each report back on their own thread; let the window's
    # thread get its turn quickly so it stays responsive.
    sys.setswitchinterval(0.001)
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), 2)   # THREAD_PRIORITY_HIGHEST
    app = QApplication(sys.argv)
    app.setOrganizationName("Polyreel")
    app.setApplicationName("Polyreel")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "icon.png")))
    app.setQuitOnLastWindowClosed(True)
    # libmpv requires the C numeric locale; Qt may have changed it.
    locale.setlocale(locale.LC_NUMERIC, "C")

    from .mpvlib import LOAD_ERROR
    if LOAD_ERROR:
        QMessageBox.critical(
            None, "libmpv not found",
            "Polyreel needs libmpv.\n\n"
            "Windows: download libmpv-2.dll (see README) and put it next to this program.\n"
            "Linux: install libmpv (e.g. 'sudo apt install libmpv2').\n"
            "macOS: 'brew install mpv'.\n\n" + LOAD_ERROR)
        return 1

    from .app import MainWindow
    win = MainWindow(sys.argv[1:])
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
