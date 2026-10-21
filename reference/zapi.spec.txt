# PyInstaller spec: Zapi.exe (windowed app) and zapi-cli.exe (console runner) sharing one runtime folder.
# Run through build.bat. Both executables start the same program; main.py picks the mode from the exe name.
from PyInstaller.utils.hooks import collect_all

datas = [("fonts", "fonts"), ("assets", "assets")]
binaries = []
hidden = ["yaml"]
for pkg in ("grpc", "grpc_reflection", "google.protobuf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=datas,
    binaries=binaries,
    hiddenimports=hidden,
    excludes=["PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtOpenGL",
              "PySide6.QtDBus", "PySide6.QtSql", "PySide6.QtTest", "tkinter", "PIL", "numpy"],
)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Zapi", icon="assets/zapi.ico", console=False,
          version="installer/generated/version_Zapi.txt")
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="zapi-cli", icon="assets/zapi.ico", console=True,
          version="installer/generated/version_zapi-cli.txt")
coll = COLLECT(gui, cli, a.binaries, a.datas, name="Zapi")
