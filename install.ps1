# Donut Coin installer for Windows (PowerShell).  irm https://donutcoin.meme/install.ps1 | iex
# Needs Python 3.10+ (python.org, tick "Add to PATH") and Git (git-scm.com).
$ErrorActionPreference = "Stop"
Write-Host "Donut Coin: a cat's coin. No value, all fun."
try { $v = & python -c "import sys; print(sys.version_info >= (3, 10))" } catch { $v = "False" }
if ($v.Trim() -ne "True") { Write-Host "Python 3.10 or newer is needed: https://python.org (tick 'Add python.exe to PATH')."; exit 1 }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host "Git is needed: https://git-scm.com"; exit 1 }
$D = Join-Path $HOME "donutcoin"
if (Test-Path (Join-Path $D ".git")) { git -C $D pull -q } else { git clone -q https://donutcoin.meme/source $D }
Set-Location $D
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
& .venv\Scripts\pip.exe install -q -r requirements.txt
Write-Host "Installed in $D. Starting. It asks for the @username of your account on donutcoin.meme/wallet (or set `$env:DONUTCOIN_TO first); press Enter instead for a wallet file at $HOME\.donutcoin\wallet.json (back it up)."
& .venv\Scripts\python.exe -m donutcoin start
