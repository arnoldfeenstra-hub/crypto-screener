# Refresh the exported data and deploy web/ to Vercel.
#
# Node and the Vercel CLI are already installed (via winget, user scope). The one
# step that cannot be automated is `vercel login` -- it authenticates as you, in a
# browser. Run this script; if you are not logged in it will say so and stop.
#
#   .\deploy.ps1              # deploy a preview
#   .\deploy.ps1 -Production  # deploy to the production URL

param(
    [switch]$Production,
    [string]$Db = "data/demo.duckdb"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$nodeDir = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.19.0-win-x64"
$vercel = "$nodeDir\vercel.cmd"
$python = ".venv\Scripts\python.exe"

if (-not (Test-Path $vercel)) {
    Write-Error "Vercel CLI not found at $vercel. Reinstall with: npm install -g vercel"
}

Write-Host "== Refreshing web/screener-data.json from $Db ==" -ForegroundColor Cyan
& $python -m export_web --db $Db --out web/screener-data.json
if ($LASTEXITCODE -ne 0) { Write-Error "export failed" }

Write-Host "`n== Checking Vercel auth ==" -ForegroundColor Cyan
& $vercel whoami 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Not logged in to Vercel." -ForegroundColor Yellow
    Write-Host "Run this once, then re-run this script:" -ForegroundColor Yellow
    Write-Host "  $vercel login" -ForegroundColor White
    exit 1
}

Write-Host "`n== Deploying web/ ==" -ForegroundColor Cyan
if ($Production) {
    & $vercel deploy --prod --yes
} else {
    & $vercel deploy --yes
}
