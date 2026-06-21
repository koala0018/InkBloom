$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONPATH = "$root\.deps"
.\.venv-build\Scripts\python.exe -m PyInstaller --paths .deps --noconfirm --clean --onedir --windowed --name InkBloom `
  --add-data "templates;templates" --add-data "static;static" `
  --add-data "assets\models;assets\models" `
  --collect-all fitz --collect-all cv2 app.py
New-Item -ItemType Directory -Force release | Out-Null
Remove-Item -Recurse -Force release\InkBloom -ErrorAction SilentlyContinue
Copy-Item -Recurse dist\InkBloom release\InkBloom
New-Item -ItemType Directory -Force release\InkBloom\work,release\InkBloom\output,release\InkBloom\models | Out-Null
Copy-Item THIRD_PARTY_NOTICES.md release\InkBloom\THIRD_PARTY_NOTICES.md
Set-Content -Encoding UTF8 release\InkBloom\README.txt @"
InkBloom Portable
1. Double-click InkBloom.exe.
2. The browser opens automatically. Select a comic and an optional color reference.
3. Results are saved in the output folder and can also be downloaded in the page.
4. Move the complete InkBloom folder; do not copy the exe alone.
"@
Compress-Archive -Path release\InkBloom -DestinationPath release\InkBloom-Windows-x64-portable.zip -Force
