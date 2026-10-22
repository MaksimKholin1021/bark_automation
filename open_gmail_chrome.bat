@echo off
cd /d "%~dp0"
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --restore-last-session "https://mail.google.com/mail/u/0/#search/from:team@bark.com+newer_than:2d+(speaking+to+OR+contacting)"
