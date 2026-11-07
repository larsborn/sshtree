# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one folder with two executables sharing the same libraries.
#   SshTree.exe      windowed GUI (no console window)
#   sshtree-cli.exe  console build, used by the GUI to run ssh inside the
#                    terminal (`--exec`) and for the command-line options
# Build:  pyinstaller --noconfirm sshtree.spec

a = Analysis(
    ["sshtree.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["import_putty_registry"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PIL", "numpy", "unittest", "pydoc", "doctest", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SshTree",
    console=False,
    icon="assets/sshtree.ico",
    upx=False,
)
cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sshtree-cli",
    console=True,
    icon="assets/sshtree.ico",
    upx=False,
)
coll = COLLECT(gui, cli, a.binaries, a.datas, name="SshTree")
