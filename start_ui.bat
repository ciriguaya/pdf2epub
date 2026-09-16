@echo off
rem Starts the drag & drop UI with the Python of the marker virtual environment.
rem Override the environment location by setting PDF2EPUB_PYTHON before running.

setlocal
set "PY=%PDF2EPUB_PYTHON%"
if not defined PY set "PY=%USERPROFILE%\marker_env\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\marker_env\Scripts\python.exe"
if not exist "%PY%" (
  echo Could not find the marker virtual environment.
  echo Expected: %USERPROFILE%\marker_env\Scripts\python.exe
  echo Set PDF2EPUB_PYTHON to that path and run this file again.
  pause
  exit /b 1
)

"%PY%" "%~dp0pdf2epub_ui.py"
if errorlevel 1 pause
endlocal
