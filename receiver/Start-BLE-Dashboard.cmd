@echo off
setlocal
cd /d "%~dp0"
set "MSAI_BLE_NAME="
echo Open http://127.0.0.1:8000, select BLE, then choose the base station COM port.
echo Wi-Fi and Ethernet nodes continue using TCP in the same dashboard.
echo Windows Bluetooth is not needed for the ESP32-S3 USB base station.
call "%~dp0Start-Dashboard.cmd"
endlocal
