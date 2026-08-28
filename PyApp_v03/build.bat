@echo off
REM Builds Signal.exe from app.py using PyInstaller.
REM Run this on Windows, inside this folder, after installing requirements.txt.

pyinstaller --noconfirm --onefile --windowed --name Signal app.py

echo.
echo Done. Find Signal.exe in the "dist" folder.
pause
