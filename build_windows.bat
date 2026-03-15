@echo off
echo Building Helyx Residual Monitor...
pyinstaller --onefile --windowed --name "HelyxResidualMonitor" helyx_monitor.py
echo.
echo Build complete. Executable is in dist\HelyxResidualMonitor.exe
pause
