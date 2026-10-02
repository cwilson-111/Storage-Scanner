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

rem Launch the built .exe headlessly and check it actually works before
rem packaging it (see smoke_test_build.py).
python smoke_test_build.py dist\StorageScanner.exe
if errorlevel 1 (
    echo.
    echo Smoke test FAILED - the built StorageScanner.exe does not work. See above.
    pause
    exit /b 1
)

rem The portable ZIP holds a one-folder build, which starts without
rem unpacking itself to %%TEMP%% first (about 2 s faster per launch).
pyinstaller ^
 --noconfirm ^
 --onedir ^
 --windowed ^
 --name StorageScanner ^
 --icon icon.ico ^
 --add-data "icon.ico;." ^
 --distpath dist\portable ^
 --workpath build\portable ^
 Storage-Scanner.py

python smoke_test_build.py dist\portable\StorageScanner\StorageScanner.exe
if errorlevel 1 (
    echo.
    echo Smoke test FAILED - the portable StorageScanner folder does not work. See above.
    pause
    exit /b 1
)

python make_sbom.py --app-version local-build --output dist\sbom.json

powershell -NoProfile -Command "Compress-Archive -Force -Path dist\portable\StorageScanner -DestinationPath dist\StorageScanner-portable.zip"

powershell -NoProfile -Command "Get-FileHash dist\StorageScanner.exe, dist\StorageScanner-portable.zip, dist\sbom.json -Algorithm SHA256 | ForEach-Object { \"$($_.Hash)  $(Split-Path $_.Path -Leaf)\" } | Set-Content dist\SHA256SUMS.txt"

echo.
echo Done. Your files are in dist\:
echo   StorageScanner.exe            (the app)
echo   StorageScanner-portable.zip   (the faster-starting folder version, zipped)
echo   sbom.json                     (software bill of materials)
echo   SHA256SUMS.txt                (checksums for all of the above)
pause