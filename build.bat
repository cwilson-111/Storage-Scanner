@echo off
REM Build a standalone StorageScanner.exe (no Python needed to run the result).
REM Requires: pip install -r requirements-dev.txt

pyinstaller --onefile --windowed --name StorageScanner --icon icon.ico --add-data "icon.ico;." treesize.py

echo.
echo Done. Your executable is at: dist\StorageScanner.exe
