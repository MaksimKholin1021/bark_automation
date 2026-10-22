@echo off
cd /d "%~dp0"
set "LINK=%CD%\sessions\chrome-profile"
set "TARGET=%LOCALAPPDATA%\Google\Chrome\User Data"
if not exist "%CD%\sessions" mkdir "%CD%\sessions"
if not exist "%LINK%" mklink /J "%LINK%" "%TARGET%" >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%LINK%" --restore-last-session "https://mail.google.com/mail/u/0/#search/from:team@bark.com+newer_than:2d+(speaking+to+OR+contacting)"
