# PyInstaller spec: Lavora.exe (windowed GUI, asks for administrator rights) and lavora-cli.exe (console) sharing one
# runtime folder, like Zapi. Run through installer\build.ps1. main.py picks the mode from the exe name.
a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=[("assets", "assets")],
    hiddenimports=["cryptography.hazmat.primitives.kdf.scrypt", "cryptography.hazmat.primitives.ciphers.aead"],
    excludes=["PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtOpenGL",
              "PySide6.QtDBus", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtNetwork", "PySide6.QtMultimedia",
              "PySide6.QtSvg", "PySide6.QtBluetooth", "tkinter", "numpy", "PIL"],
)
pyz = PYZ(a.pure)
# raw disk access needs elevation, so the GUI carries a requireAdministrator manifest. The CLI does not: reading image
# files needs no rights, and device access reports a clear "run as administrator" message instead.
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Lavora", icon="assets/lavora.ico", console=False, uac_admin=True,
          version="installer/generated/version_Lavora.txt")
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="lavora-cli", icon="assets/lavora.ico", console=True,
          version="installer/generated/version_lavora-cli.txt")
coll = COLLECT(gui, cli, a.binaries, a.datas, name="Lavora")
