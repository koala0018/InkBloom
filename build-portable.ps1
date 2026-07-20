$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONPATH = "$root\.deps"
$env:PYTHONNOUSERSITE = '1'
$python = 'python'
$pythonArgs = @('-S')
& $python @pythonArgs -m PyInstaller --paths .deps --noconfirm --clean --onedir --console --name ComfyInkBloom `
  --add-data "templates;templates" --collect-all fitz app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
New-Item -ItemType Directory -Force release | Out-Null
if (Test-Path -LiteralPath release\ComfyInkBloom) { Remove-Item -LiteralPath release\ComfyInkBloom -Recurse -Force }
Copy-Item -LiteralPath dist\ComfyInkBloom -Destination release\ComfyInkBloom -Recurse
New-Item -ItemType Directory -Force release\ComfyInkBloom\work,release\ComfyInkBloom\output | Out-Null
Set-Content -Encoding UTF8 release\ComfyInkBloom\README.txt @"
ComfyInkBloom
1. Start both ComfyUI services: 2080 Ti on 8188 and 5060 Ti on 8189.
2. Run ComfyInkBloom.exe and upload images, PDF, ZIP/CBZ, or a folder.
3. Set prompts and optional width/height. Outputs are in work/<job>/colored.
"@
tar.exe -acf release\ComfyInkBloom-Windows-x64-portable.zip -C release ComfyInkBloom
if ($LASTEXITCODE -ne 0) { throw 'Portable ZIP build failed.' }
