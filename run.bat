@echo off
REM Fastest way to TEST genericMud on Windows: bootstrap a venv and run from source.
REM Usage:  run.bat               (connects to 127.0.0.1:4000)
REM         run.bat host 4000     (connect to a MUD)
REM         run.bat host 4000 --tls
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -e ".[gui,voice]"
python -m genericmud %*
endlocal
