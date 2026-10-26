@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First install the bench from PowerShell in this folder:
    echo   py -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[test]"
    echo See docs\PROTOTYPE.md for setup and privacy notes.
    pause
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -m jobwolf
if errorlevel 1 pause
popd
