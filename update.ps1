# Re-scrape all indicators and refresh site/data/.
# Run manually, or schedule daily with Windows Task Scheduler:
#   Program:  powershell.exe
#   Args:     -NoProfile -ExecutionPolicy Bypass -File "<full path>\update.ps1"
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python scraper\scrape.py
Write-Host "Done. Data refreshed at $(Get-Date)."
