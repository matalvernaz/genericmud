# -*- mode: python ; coding: utf-8 -*-
import glob
import os

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks import get_package_paths

datas = [('frontend', 'frontend'), ('genericmud/config/keymaps', 'genericmud/config/keymaps')]
binaries = []
# _cffi_backend is what _prism_cffi.pyd loads from C at init; no Python source imports it,
# so it only reaches the bundle transitively through cffi. Pinned here because losing it
# fails the same silent way the missing .pyd did -- prism raises, factory.py drops to SAPI.
hiddenimports = ['websockets', 'win32com.client', 'pythoncom', '_cffi_backend']
datas += copy_metadata('genericmud')
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('prism')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# prism ships _prism_cffi.pyd inside prism/_native/, a plain directory that prism/_native.py
# splices onto prism.__path__ at import time. collect_all misses it three ways over: the
# module graph never sees a module there, collect_dynamic_libs only takes *.dll on Windows,
# and collect_data_files filters .pyd out. Without this, `import prism` raises in the frozen
# build and voice/factory.py swallows it and drops to SAPI -- self-voicing through the user's
# own screen reader disappears from the shipped exe with no error anywhere.
_, _prism_pkg = get_package_paths('prism')
binaries += [
    (p, 'prism/_native') for p in glob.glob(os.path.join(_prism_pkg, '_native', '*.pyd'))
]
tmp_ret = collect_all('pygame')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('lupa')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run_genericmud.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='genericMud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='genericMud',
)
