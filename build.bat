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

python make_sbom.py --app-version local-build --output dist\sbom.json

powershell -NoProfile -Command "Compress-Archive -Force -Path dist\StorageScanner.exe -DestinationPath dist\StorageScanner-portable.zip"

powershell -NoProfile -Command "Get-FileHash dist\StorageScanner.exe, dist\StorageScanner-portable.zip, dist\sbom.json -Algorithm SHA256 | ForEach-Object { \"$($_.Hash)  $(Split-Path $_.Path -Leaf)\" } | Set-Content dist\SHA256SUMS.txt"

echo.
echo Done. Your files are in dist\:
echo   StorageScanner.exe            (the app)
echo   StorageScanner-portable.zip   (zipped copy)
echo   sbom.json                     (software bill of materials)
echo   SHA256SUMS.txt                (checksums for all of the above)
pause