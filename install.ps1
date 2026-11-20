# Antigravity Patcher -- Quick Setup (Windows)
# Run: powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  ANTIGRAVITY PATCHER & ASSISTANT INSTALLER" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check Administrator Privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] Note: Administrator privileges required for DNS NRPT rules." -ForegroundColor Yellow
}

# 2. Deploy ag-toolkit extension
$Version = "1.2.0"
$HomeDir = [System.Environment]::GetFolderPath('UserProfile')
$ExtRoots = @(
    (Join-Path $HomeDir ".antigravity-ide\extensions"),
    (Join-Path $HomeDir ".vscode\extensions")
)

$VsixUrl = "https://github.com/EnergyStario/antigravity-patcher/releases/latest/download/ag-toolkit-$Version.vsix"
$TempVsix = Join-Path $env:TEMP "ag-toolkit-$Version.zip"
$TempExtract = Join-Path $env:TEMP "ag-toolkit-unpack"

Write-Host "[*] Downloading Antigravity Assistant extension..." -ForegroundColor Cyan
try {
    Invoke-WebRequest -Uri $VsixUrl -OutFile $TempVsix -UseBasicParsing -TimeoutSec 20
    Write-Host "[+] Download complete: ag-toolkit-$Version.vsix" -ForegroundColor Green

    # Unpack VSIX (standard zip archive with extension/ prefix)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $TempExtract) { Remove-Item -Path $TempExtract -Recurse -Force }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($TempVsix, $TempExtract)

    $ExtSource = Join-Path $TempExtract "extension"
    if (Test-Path $ExtSource) {
        foreach ($root in $ExtRoots) {
            if (-not (Test-Path $root)) { New-Item -Path $root -ItemType Directory -Force | Out-Null }
            Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -like "energystar.ag-toolkit*"
            } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

            $t = Join-Path $root "energystar.ag-toolkit-$Version"
            Copy-Item -Path $ExtSource -Destination $t -Recurse -Force
            Write-Host "[+] Extension installed to: $(Split-Path $root -Leaf)\energystar.ag-toolkit-$Version" -ForegroundColor Green
        }
    }
} catch {
    Write-Host "[-] Could not download VSIX directly: $_" -ForegroundColor Yellow
} finally {
    if (Test-Path $TempVsix) { Remove-Item -Path $TempVsix -Force -ErrorAction SilentlyContinue }
    if (Test-Path $TempExtract) { Remove-Item -Path $TempExtract -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host "[+] Antigravity Assistant successfully installed!" -ForegroundColor Green
Write-Host "    Restart Antigravity IDE to see the Status Bar shield & account switcher." -ForegroundColor White
Write-Host ""
