@echo off
REM Build a standalone dist\genericMud\genericMud.exe. Run this ON Windows
REM (PyInstaller does not cross-compile). Produces a windowed GUI app.
setlocal
cd /d "%~dp0"
where uv >nul 2>nul || (
  echo uv is not installed. Install it, then run this again:
  echo     winget install --id=astral-sh.uv -e
  echo see https://docs.astral.sh/uv/getting-started/installation/
  exit /b 1
)
uv sync --no-dev --extra gui --extra voice --extra audio --extra package || exit /b 1
REM Build from genericMud.spec, never from run_genericmud.py + flags: passing the
REM script makes PyInstaller regenerate (overwrite) the spec, which silently drops
REM the hand-written bits collect_all can't express -- notably prism's
REM _native\_prism_cffi.pyd, without which `import prism` raises in the frozen app
REM and voice/factory.py falls back to SAPI instead of speaking through NVDA.
uv run --no-dev pyinstaller --noconfirm genericMud.spec || exit /b 1
echo.
echo Built: dist\genericMud\genericMud.exe
endlocal
