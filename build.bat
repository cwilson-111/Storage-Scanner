@echo off

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del StorageScanner.spec 2>nul

pyinstaller ^
 --noconfirm ^
 --clean ^
 --onefile ^
 --windowed ^
 --name StorageScanner ^
 --icon icon.ico ^
 --add-data "icon.ico;." ^
 Storage-Scanner.py

echo.
echo Done. Your executable is at: dist\StorageScanner.exe
pause