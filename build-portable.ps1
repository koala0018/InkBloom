$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONPATH = "$root\.deps"
$env:PYTHONNOUSERSITE = '1'
$sourceStyle2Paints = Join-Path $root 'models\style2paints'
$portableStyle2Paints = Join-Path $root 'release\InkBloom\models\style2paints'
$preserveRoot = Join-Path $root 'work\build-preserve'
$preservedStyle2Paints = Join-Path $preserveRoot 'style2paints'
if (!(Test-Path -LiteralPath (Join-Path $sourceStyle2Paints 'READY')) -and
    (Test-Path -LiteralPath (Join-Path $portableStyle2Paints 'READY'))) {
  New-Item -ItemType Directory -Force $preserveRoot | Out-Null
  Remove-Item -Recurse -Force $preservedStyle2Paints -ErrorAction SilentlyContinue
  Move-Item -LiteralPath $portableStyle2Paints -Destination $preservedStyle2Paints
}
.\.venv-build\Scripts\python.exe -m PyInstaller --paths .deps --noconfirm --clean --onedir --console --name InkBloom `
  --add-data "templates;templates" --add-data "static;static" `
  --add-data "assets\models;assets\models" `
  --add-data "install-style2paints.ps1;." `
  --collect-all fitz --collect-all cv2 app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
New-Item -ItemType Directory -Force release | Out-Null
Remove-Item -Recurse -Force release\InkBloom -ErrorAction SilentlyContinue
Copy-Item -Recurse dist\InkBloom release\InkBloom
New-Item -ItemType Directory -Force release\InkBloom\work,release\InkBloom\output,release\InkBloom\models | Out-Null
Copy-Item THIRD_PARTY_NOTICES.md release\InkBloom\THIRD_PARTY_NOTICES.md
Copy-Item install-style2paints.ps1 release\InkBloom\
if (Test-Path -LiteralPath models\style2paints\READY) {
  Copy-Item -LiteralPath models\style2paints -Destination release\InkBloom\models\style2paints -Recurse -Force
} elseif (Test-Path -LiteralPath (Join-Path $preservedStyle2Paints 'READY')) {
  Move-Item -LiteralPath $preservedStyle2Paints -Destination release\InkBloom\models\style2paints
}
Set-Content -Encoding UTF8 release\InkBloom\README.txt @"
InkBloom Portable
1. Double-click InkBloom.exe and keep the small status window open while using the app.
2. The browser opens automatically. Select a comic and one or more color references.
3. Results, Style2Paints layers, and logs are available in the page.
4. Move the complete InkBloom folder; do not copy the exe alone.
"@
tar.exe -acf release\InkBloom-Windows-x64-portable.zip -C release InkBloom
if ($LASTEXITCODE -ne 0) { throw 'Portable ZIP build failed.' }
