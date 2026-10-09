# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec —— 生成单文件 LingxingOrderTool.exe。
同时支持"直接 API"和"浏览器自动化"两种查询模式。

打包: pyinstaller lingxing_order_tool.spec --clean --noconfirm
"""

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "playwright._impl._api_structures",
        "playwright._impl._driver",
        "playwright.sync_api",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "playwright._impl._driver_firefox",
        "playwright._impl._driver_webkit",
        "selenium",
        "tkinter", "test", "unittest", "doctest",
        "numpy", "PIL",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LingxingOrderTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True, upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
