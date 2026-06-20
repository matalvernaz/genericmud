@echo off
REM Build a standalone dist\genericMud.exe. Run this ON Windows (PyInstaller does
REM not cross-compile). Produces a single-file console exe.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -e ".[gui,voice,package]"
pyinstaller --onefile --name genericMud --console ^
  --add-data "frontend;frontend" ^
  --add-data "genericmud\config\keymaps;genericmud\config\keymaps" ^
  --collect-all webview ^
  --hidden-import lupa --hidden-import websockets ^
  --hidden-import win32com.client --hidden-import pythoncom ^
  run_genericmud.py
echo.
echo Built: dist\genericMud.exe
echo For NVDA-voice (instead of SAPI), put nvdaControllerClient.dll next to the exe.
endlocal
