@echo off
:: ==========================================
:: Run as Administrator
:: ==========================================
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit
)

:: ==========================================
:: Start XAMPP Control Panel
:: ==========================================
start "" "C:\xampp\xampp-control.exe"

:: Wait few seconds for XAMPP
timeout /t 5 /nobreak >nul

:: ==========================================
:: Open command prompt and run Flask app
:: ==========================================
start cmd /k "cd /d C:\xampp\htdocs\pvc_app && python run.py"
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" "http://192.168.0.103:5000"