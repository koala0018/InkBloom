param([string]$AppRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$modelRoot = Join-Path $AppRoot 'models\manganinja'
$source = Join-Path $modelRoot 'source'
$runtime = Join-Path $modelRoot 'runtime'
$uv = 'D:\ComfyUI_windows_portable\python_embeded\Scripts\uv.exe'

if (!(Test-Path $uv)) {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
}
if (!$uv) { throw 'uv not found. Install uv from https://docs.astral.sh/uv/' }

New-Item -ItemType Directory -Force $modelRoot | Out-Null
if (!(Test-Path (Join-Path $source '.git'))) {
    git clone --depth 1 https://github.com/sdbds/MangaNinjia-for-windows.git $source
}
if (!(Test-Path (Join-Path $runtime 'Scripts\python.exe'))) {
    & $uv venv $runtime --python 3.10 --python-preference managed
}

& $uv pip install --python (Join-Path $runtime 'Scripts\python.exe') `
    torch==2.7.1 torchvision==0.22.1 `
    --index-url https://download.pytorch.org/whl/cu128
& $uv pip install --python (Join-Path $runtime 'Scripts\python.exe') `
    -r (Join-Path $AppRoot 'manganinja-requirements.txt')

$hf = Join-Path $runtime 'Scripts\huggingface-cli.exe'
$checkpoints = Join-Path $modelRoot 'checkpoints'
& $hf download stable-diffusion-v1-5/stable-diffusion-v1-5 `
    --local-dir (Join-Path $checkpoints 'StableDiffusion') `
    --exclude '*.ckpt' '*.bin'
& $hf download lllyasviel/control_v11p_sd15_lineart `
    --local-dir (Join-Path $checkpoints 'models\control_v11p_sd15_lineart') `
    --exclude '*.bin'
& $hf download lllyasviel/Annotators sk_model.pth `
    --local-dir (Join-Path $checkpoints 'models\Annotators')
& $hf download Johanan0528/MangaNinjia `
    --local-dir (Join-Path $checkpoints 'MangaNinjia')
& $hf download openai/clip-vit-large-patch14 `
    --local-dir (Join-Path $checkpoints 'models\clip-vit-large-patch14') `
    --exclude '*.bin' '*.h5' '*.msgpack'

& (Join-Path $runtime 'Scripts\python.exe') -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.cuda.get_device_name(0), torch.cuda.get_arch_list())"
Set-Content -Encoding UTF8 (Join-Path $modelRoot 'READY') 'MangaNinja CC BY-NC 4.0 - non-commercial use only'
Write-Output 'MangaNinja installation complete.'
