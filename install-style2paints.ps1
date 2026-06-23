param([string]$ArchivePath = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$modelRoot = Join-Path $root 'models\style2paints'
$expectedMd5 = '6dbccce33c5ac9ea3bdae3eabe93c94d'
$expectedSize = 2604850176

if (!$ArchivePath) {
    $candidates = @(
        (Join-Path $root 'style2paints45beta1214B.zip'),
        (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads\style2paints45beta1214B.zip')
    )
    $ArchivePath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (!$ArchivePath -or !(Test-Path -LiteralPath $ArchivePath)) {
    Write-Host 'Download style2paints45beta1214B.zip from an official Style2Paints link:' -ForegroundColor Yellow
    Write-Host 'Google Drive: https://drive.google.com/open?id=1gmg2wwNIp4qMzxqP12SbcmVAHsLt1iRE'
    Write-Host 'Baidu Pan: https://pan.baidu.com/s/15xCm1jRVeHipHkiB3n1vzA'
    Write-Host 'Put the ZIP beside InkBloom.exe, then run this script again.'
    exit 2
}

$archive = Get-Item -LiteralPath $ArchivePath
if ($archive.Length -ne $expectedSize) {
    throw "Official archive size mismatch: $($archive.Length); expected $expectedSize"
}
$md5 = (Get-FileHash -LiteralPath $archive.FullName -Algorithm MD5).Hash.ToLowerInvariant()
if ($md5 -ne $expectedMd5) {
    throw "Official archive MD5 mismatch: $md5"
}

New-Item -ItemType Directory -Force $modelRoot | Out-Null
Write-Host 'Extracting the official Style2Paints V4.5 application...'
Expand-Archive -LiteralPath $archive.FullName -DestinationPath $modelRoot -Force
$launchers = Get-ChildItem -LiteralPath $modelRoot -Filter '*.exe' -Recurse | Where-Object {
    $_.Name -match 'style2paint|launch|server'
}
if (!$launchers) {
    throw 'Archive extracted, but no Style2Paints launcher was found.'
}
Set-Content -LiteralPath (Join-Path $modelRoot 'READY') -Encoding UTF8 @"
Style2Paints V4.5 official binary
Archive MD5: $expectedMd5
Official pretrained models and binary releases: all rights reserved by their authors.
"@
Write-Host 'Style2Paints V4.5 installation completed.' -ForegroundColor Green
