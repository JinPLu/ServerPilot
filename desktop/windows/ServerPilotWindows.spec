# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("serverpilot")
# Alembic loads env.py and versions/*.py from disk when the database is
# initialised, so the migration package ships as data. collect_data_files skips
# .py by default, which leaves a build that cannot create its own database.
datas += collect_data_files("serverpilot.migrations", include_py_files=True)
datas.append((str(project_root / "desktop" / "assets" / "ServerPilot Icon.png"), "desktop/assets"))
datas.append((str(project_root / "desktop" / "windows" / "ui"), "desktop/windows/ui"))

a = Analysis(
    [str(project_root / "desktop" / "windows_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("webview") + [
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "tkinter",
        "tkinter.messagebox",
        "clr",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
    noarchive=False,
    optimize=0,
)

# Agents talk to ServerPilot over MCP stdio, so the download has to contain an
# MCP entry point. Without it the desktop archive ships only the human GUI and
# the agent half of the product is unreachable on Windows.
mcp = Analysis(
    [str(project_root / "src" / "serverpilot" / "mcp_server.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
    noarchive=False,
    optimize=0,
)

MERGE((a, "ServerPilot", "ServerPilot"), (mcp, "serverpilot-mcp", "serverpilot-mcp"))

pyz = PYZ(a.pure)
mcp_pyz = PYZ(mcp.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ServerPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "desktop" / "assets" / "ServerPilot Icon.png"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

mcp_exe = EXE(
    mcp_pyz,
    mcp.scripts,
    [],
    exclude_binaries=True,
    name="serverpilot-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # stdio JSON-RPC: this is a console program an agent spawns with pipes.
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
    a.datas,
    mcp_exe,
    mcp.binaries,
    mcp.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ServerPilot",
)
