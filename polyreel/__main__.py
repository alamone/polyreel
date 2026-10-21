"""Entry point: ``python -m polyreel [files or folders...]``."""

from __future__ import annotations

import locale
import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setOrganizationName("Polyreel")
    app.setApplicationName("Polyreel")
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
