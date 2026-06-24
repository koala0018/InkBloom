$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cobraRoot = Join-Path $root 'models\cobra'
$repo = Join-Path $cobraRoot 'Cobra'
$venv = Join-Path $cobraRoot '.venv'

New-Item -ItemType Directory -Force $cobraRoot | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $repo 'app.py'))) {
  git clone https://github.com/zhuang2002/Cobra $repo
}

if (!(Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
  python -m venv $venv
}

$python = Join-Path $venv 'Scripts\python.exe'
$workerSource = Join-Path $root 'tools\cobra_worker.py'
if (!(Test-Path -LiteralPath $workerSource)) {
  $workerSource = Join-Path $root '_internal\tools\cobra_worker.py'
}
if (!(Test-Path -LiteralPath $workerSource)) {
  throw 'Missing tools\cobra_worker.py'
}
Copy-Item -LiteralPath $workerSource -Destination (Join-Path $cobraRoot 'inkbloom_worker.py') -Force
& $python -m pip install --upgrade pip
& $python -m pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128
$filteredRequirements = Join-Path $cobraRoot 'requirements-inkbloom.txt'
Get-Content -LiteralPath (Join-Path $repo 'requirements.txt') |
  Where-Object { $_ -notmatch '^(torch|torchvision|torchaudio)==' -and $_ -notmatch '^-e\s+\./diffusers' } |
  Set-Content -LiteralPath $filteredRequirements -Encoding UTF8
& $python -m pip install -e (Join-Path $repo 'diffusers')
& $python -m pip install -r $filteredRequirements
Remove-Item -LiteralPath $filteredRequirements -Force

$app = Join-Path $repo 'app.py'
$content = Get-Content -LiteralPath $app -Raw
$content = $content -replace [regex]::Escape('model_global_path = snapshot_download(repo_id="JunhaoZhuang/Cobra", cache_dir=''./Cobra/'', repo_type="model")'), @'
model_global_path = snapshot_download(
    repo_id="JunhaoZhuang/Cobra",
    cache_dir=os.getenv("INKBLOOM_COBRA_CACHE", "./Cobra/"),
    repo_type="model",
    local_files_only=os.getenv("INKBLOOM_COBRA_OFFLINE") == "1",
)
'@
$content = $content -replace 'line_model\.cuda\(\)', 'line_model.half().cuda()'
$content = $content -replace [regex]::Escape("image_encoder = CLIPVisionModelWithProjection.from_pretrained(os.path.join(model_global_path, 'image_encoder')).to('cuda')"), @'
image_encoder = CLIPVisionModelWithProjection.from_pretrained(
    os.path.join(model_global_path, 'image_encoder'),
    torch_dtype=torch.float16,
).to('cuda')
'@
$content = $content -replace 'tensor = torch\.from_numpy\(patch\)\.cuda\(\)', 'tensor = torch.from_numpy(patch).half().cuda()'
if ($content -match '(?m)^demo\.launch\(\)\s*$') {
  $content = $content -replace '(?m)^demo\.launch\(\)\s*$', "if __name__ == `"__main__`":`r`n    demo.launch()"
}
Set-Content -LiteralPath $app -Value $content -Encoding UTF8

$env:HF_HOME = Join-Path $cobraRoot 'huggingface'
$env:HUGGINGFACE_HUB_CACHE = $env:HF_HOME
$env:TORCH_HOME = Join-Path $cobraRoot 'torch'
& $python -c "from huggingface_hub import snapshot_download; snapshot_download('JunhaoZhuang/Cobra'); snapshot_download('PixArt-alpha/PixArt-XL-2-1024-MS', allow_patterns=['model_index.json','transformer/*','vae/*','scheduler/*'])"
New-Item -ItemType File -Force (Join-Path $cobraRoot 'READY') | Out-Null
Write-Host 'Cobra installation completed.'
