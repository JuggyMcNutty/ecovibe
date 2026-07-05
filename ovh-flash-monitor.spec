import sys
import os

block_cipher = None

project_root = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(project_root, "app", "main.py")],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "static"), "static"),
        (os.path.join(project_root, "templates"), "templates"),
    ],
    hiddenimports=[
        "encodings",
        "encodings.utf_8",
        "encodings.ascii",
        "encodings.latin_1",
        "encodings.iso8859_1",
        "encodings.cp1252",
        "encodings.codecs",
        "_codecs",
        "_codecs_tw",
        "_codecs_kr",
        "_codecs_jp",
        "_multibufect",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.auto",
        "starlette",
        "fastapi",
        "pydantic",
        "pydantic_settings",
        "ovh",
        "dotenv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ovh-flash-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ovh-flash-monitor",
)
