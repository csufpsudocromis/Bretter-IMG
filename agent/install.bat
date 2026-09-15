@echo off
:: Bretter-IMG Agent Installer
:: Run as Administrator

set INSTALL_DIR=C:\ProgramData\BretterIMG
set AGENT_EXE=%INSTALL_DIR%\agent.exe

echo === Bretter-IMG Agent Installer ===
echo.

:: Create directories
mkdir "%INSTALL_DIR%" 2>nul
mkdir "%INSTALL_DIR%\temp" 2>nul
mkdir "%INSTALL_DIR%\winpe" 2>nul

:: Copy agent binary
copy /Y "%~dp0agent.exe" "%AGENT_EXE%"

:: Set server URL
set /p SERVER_URL="Enter Bretter-IMG server URL (e.g. http://192.168.1.10:8000): "
set /p AGENT_TOKEN="Enter agent token (from console): "

:: Write environment config
(
echo BRETTER_SERVER=%SERVER_URL%
echo BRETTER_AGENT_TOKEN=%AGENT_TOKEN%
echo BRETTER_ID_FILE=%INSTALL_DIR%\machine_id.txt
echo BRETTER_TEMP=%INSTALL_DIR%\temp
echo BRETTER_WINPE_STAGE=%INSTALL_DIR%\winpe
) > "%INSTALL_DIR%\agent.env"

:: Register as Windows service using sc
sc create BretterIMGAgent ^
    binPath= "\"%AGENT_EXE%\"" ^
    DisplayName= "Bretter-IMG Imaging Agent" ^
    start= auto ^
    obj= LocalSystem

sc description BretterIMGAgent "Remote imaging agent for Bretter-IMG console"
sc start BretterIMGAgent

echo.
echo Agent installed and started as Windows service.
echo Service name: BretterIMGAgent
pause
