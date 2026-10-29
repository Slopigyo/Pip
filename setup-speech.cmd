@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo Install the bench environment first; see start-bench.cmd.
    pause
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -m jobwolf.speech_setup --install
set "setup_result=%errorlevel%"
if not "%setup_result%"=="0" pause
popd
exit /b %setup_result%
