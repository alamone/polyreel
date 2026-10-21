# PyInstaller build for Polyreel: one folder with Polyreel.exe, Python, Qt and
# libmpv, so users need nothing else installed.
#
#   pyinstaller --noconfirm packaging/polyreel.spec
#
# On Windows, put libmpv-2.dll in the project root first (or point LIBMPV_DLL
# at it).  Elsewhere the system libmpv is used and nothing is bundled.

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

binaries = []
if sys.platform == "win32":
    dll = os.environ.get("LIBMPV_DLL", os.path.join(ROOT, "libmpv-2.dll"))
    if not os.path.exists(dll):
        raise SystemExit(f"libmpv-2.dll not found at {dll}")
    binaries.append((dll, "."))

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[(os.path.join(ROOT, "polyreel", "assets"), os.path.join("polyreel", "assets"))],
    # Qt modules and stdlib parts Polyreel never uses; leaving them out
    # keeps the download smaller.
    excludes=[
        "tkinter", "unittest", "pydoc", "test",
        "numpy", "PIL",   # optional extras of python-mpv (screenshots)
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
        "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtSvg",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtDesigner",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Polyreel",
    console=False,
    icon=os.path.join(SPECPATH, "polyreel.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Polyreel")
