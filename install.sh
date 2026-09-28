#!/bin/bash
# Donut Coin installer for macOS and Linux.  curl -fsSL https://donutcoin.meme/install.sh | bash
# Puts the code in ~/donutcoin, makes a Python environment, creates a wallet if you have none,
# and starts mining to it against donutcoin.meme. Re-run any time to update or restart.
set -eu
echo "Donut Coin: a cat's coin. No value, all fun."
PY=$(command -v python3 || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is needed. macOS: 'brew install python' (or https://python.org). Debian/Ubuntu: 'sudo apt install python3 python3-venv git'."; exit 1
fi
command -v git >/dev/null || { echo "git is needed (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git)"; exit 1; }
D="$HOME/donutcoin"
if [ -d "$D/.git" ]; then git -C "$D" pull -q; else git clone -q https://donutcoin.meme/source "$D"; fi
cd "$D"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "Installed in $D. Starting. It asks for the @username of your account on donutcoin.meme/wallet (or add --to @name); press Enter instead for a wallet file at ~/.donutcoin/wallet.json (back it up)."
exec .venv/bin/python -m donutcoin start "$@"
