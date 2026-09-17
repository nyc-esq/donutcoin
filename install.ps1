# Donut Coin installer for Windows (PowerShell).  irm https://donutcoin.meme/install.ps1 | iex
# Needs Python 3.12+ (python.org, tick "Add to PATH") and Git (git-scm.com).
$ErrorActionPreference = "Stop"
Write-Host "Donut Coin: a cat's coin. No value, all fun."
try { $v = & python -c "import sys; print(sys.version_info >= (3, 12))" } catch { $v = "False" }
if ($v.Trim() -ne "True") { Write-Host "Python 3.12 or newer is needed: https://python.org (tick 'Add python.exe to PATH')."; exit 1 }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host "Git is needed: https://git-scm.com"; exit 1 }
$D = Join-Path $HOME "donutcoin"
if (Test-Path (Join-Path $D ".git")) { git -C $D pull -q } else { git clone -q https://github.com/nyc-esq/donutcoin $D }
Set-Location $D
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
& .venv\Scripts\pip.exe install -q -r requirements.txt
Write-Host "Installed in $D. Starting: this creates a wallet at $HOME\.donutcoin\wallet.json if you have none (back it up), then mines."
& .venv\Scripts\python.exe -m donutcoin start
