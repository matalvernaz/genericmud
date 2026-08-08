@echo off
REM Fastest way to TEST genericMud on Windows: uv builds the environment and runs from source.
REM Usage:  run.bat               (connects to 127.0.0.1:4000)
REM         run.bat host 4000     (connect to a MUD)
REM         run.bat host 4000 --tls
setlocal
cd /d "%~dp0"
where uv >nul 2>nul || (
  echo uv is not installed. Install it, then run this again:
  echo     winget install --id=astral-sh.uv -e
  echo see https://docs.astral.sh/uv/getting-started/installation/
  exit /b 1
)
REM uv creates .venv, fetches Python 3.12 if it isn't already here, and installs the locked
REM dependencies before running -- no separate bootstrap step. --no-dev skips the test tools.
uv run --no-dev --extra gui --extra voice python -m genericmud %*
endlocal
