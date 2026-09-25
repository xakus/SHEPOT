# Сборка SHEPOT под Windows: PyInstaller → установщик Inno Setup + zip.
# Запускать из корня репозитория в venv, где стоит ".[gpu,dev]".
# Результат: release\SHEPOT-Setup-<версия>.exe и release\SHEPOT-<версия>-windows-x64.zip
$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")
$Version = python -c "import sys; sys.path.insert(0, 'src'); import shepot; print(shepot.__version__)"
New-Item -ItemType Directory -Force release | Out-Null

Write-Host "==> иконки"
python -m shepot.icons packaging/icons/generated
if ($LASTEXITCODE -ne 0) { throw "иконки не созданы" }

Write-Host "==> PyInstaller"
pyinstaller packaging/shepot.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

Write-Host "==> zip (переносная версия)"
$Zip = "release\SHEPOT-$Version-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
# 7-Zip быстрее Compress-Archive на гигабайтах DLL и не упирается в 2 ГБ
& 7z a -tzip -mx=5 $Zip .\dist\SHEPOT | Out-Null
if ($LASTEXITCODE -ne 0) { throw "zip не создан" }

Write-Host "==> установщик (Inno Setup)"
$Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) { $Iscc = "iscc" }
& $Iscc "/DAppVersion=$Version" packaging\windows\shepot.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }

Get-ChildItem release | Format-Table Name, Length
