$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Repo = "prostoodin1/BlenderRenderWatchdog"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source = Join-Path $Root "blender_render_watchdog.py"
$Exe = Join-Path $Root "dist\BlenderRenderWatchdog.exe"
$Manifest = Join-Path $Root "github_release\update_manifest.json"
$ReleaseNotes = Join-Path $Root "RELEASE_NOTES.md"

$VersionMatch = Select-String -LiteralPath $Source -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionMatch) {
    throw "APP_VERSION was not found in $Source"
}
$Version = $VersionMatch.Matches[0].Groups[1].Value
$Tag = "v$Version"

Write-Host "Blender Render Watchdog GitHub Publisher" -ForegroundColor Cyan
Write-Host "Repo: $Repo"
Write-Host "Version: $Tag"

if (-not (Test-Path -LiteralPath $Exe)) {
    throw "EXE not found: $Exe"
}

if (-not (Test-Path -LiteralPath (Split-Path -Parent $Manifest))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Manifest) -Force | Out-Null
}

$manifestJson = [ordered]@{
    version = $Version
    exe_url = "https://github.com/$Repo/releases/latest/download/BlenderRenderWatchdog.exe"
    notes = "Blender Render Watchdog $Tag"
} | ConvertTo-Json -Depth 5
Set-Content -LiteralPath $Manifest -Value $manifestJson -Encoding UTF8

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Host "GitHub CLI not found. Trying to install with winget..." -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget not found. Install GitHub CLI manually: https://cli.github.com/"
    }
    winget install --id GitHub.cli --source winget --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) {
        throw "GitHub CLI installed, but gh.exe is not available yet. Restart this CMD and run again."
    }
}

Write-Host "Checking GitHub login..." -ForegroundColor Cyan
$authOk = $false
try {
    gh auth status 2>$null
    if ($LASTEXITCODE -eq 0) { $authOk = $true }
} catch { $authOk = $false }

if (-not $authOk) {
    Write-Host "GitHub login required. Browser login will open." -ForegroundColor Yellow
    gh auth login --web --scopes "repo"
}

Write-Host "Checking repository..." -ForegroundColor Cyan
gh repo view $Repo 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating repository $Repo..." -ForegroundColor Yellow
    gh repo create $Repo --public --description "Blender Render Watchdog app" --add-readme
}

Write-Host "Uploading release assets..." -ForegroundColor Cyan
gh release view $Tag --repo $Repo 1>$null 2>$null
if ($LASTEXITCODE -eq 0) {
    gh release upload $Tag $Exe $Manifest --repo $Repo --clobber
} else {
    if (Test-Path -LiteralPath $ReleaseNotes) {
        gh release create $Tag $Exe $Manifest --repo $Repo --title "Blender Render Watchdog $Tag" --notes-file $ReleaseNotes --latest
    } else {
        gh release create $Tag $Exe $Manifest --repo $Repo --title "Blender Render Watchdog $Tag" --notes "Stable build with automatic update support." --latest
    }
}

Write-Host "Done!" -ForegroundColor Green
Write-Host "Release: https://github.com/$Repo/releases/latest"
Write-Host "The app can now auto-check and download updates from GitHub."
