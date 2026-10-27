# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH)
source = root / "src"
datas = [(str(source / "app.py"), "."),
         (str(root / ".streamlit" / "config.toml"), ".streamlit")]
binaries = []
hiddenimports = ["optimizer", "pandas.io.formats.style", "jinja2", "altair"]
for package in ("streamlit", "ortools"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
for package in ("streamlit", "altair", "pandas", "ortools"):
    datas += copy_metadata(package, recursive=True)

a = Analysis(
    [str(source / "launcher.py")], pathex=[str(source)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["matplotlib", "torch", "tensorflow", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ShiftStudio", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
)