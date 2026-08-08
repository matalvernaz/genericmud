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
uv run --no-dev pyinstaller --onedir --name genericMud --windowed ^
  --copy-metadata genericmud ^
  --add-data "frontend;frontend" ^
  --add-data "genericmud\config\keymaps;genericmud\config\keymaps" ^
  --collect-all webview ^
  --collect-all prism ^
  --collect-all pygame ^
  --collect-all lupa --hidden-import websockets ^
  --hidden-import win32com.client --hidden-import pythoncom ^
  run_genericmud.py
echo.
echo Built: dist\genericMud\genericMud.exe
endlocal
