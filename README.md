# Donut Coin

A proof-of-work coin named after Princess Donut, the Queen Anne Chonk, a cat in a tiara and
sunglasses. No value, all fun. Live at **https://donutcoin.meme** (explorer, wallet, market,
white paper).

| | |
|---|---|
| Proof of work | Scrypt (N=1024, r=1, p=1) over an 80-byte header, like Dogecoin |
| Block time | 60 s target; difficulty retargets every 10 blocks, clamped to 4x |
| Reward | 10,000 DONUT a block, never halves; supply uncapped |
| Units | 1 DONUT = 100,000,000 sprinkles; 1 Ribbon = 1,000,000 DONUT (100 blocks); 1 Tiara = 10 Ribbons (about a day of the network); 1 Chonk = 10 Tiaras (about a week) |
| Addresses | secp256k1, Base58Check with Dogecoin's version byte, so they start with **D** |
| Coinbase maturity | 3 blocks |

## Run it

The easy way, on a Mac or Linux (Python 3.12+ and git):

```bash
curl -fsSL https://donutcoin.meme/install.sh | bash
```

Windows, in PowerShell: `irm https://donutcoin.meme/install.ps1 | iex`. Or download a single
file from the [latest release](https://github.com/nyc-esq/donutcoin/releases/latest).

Both ask for the @username of your account on donutcoin.meme/wallet and mine into it, or make a
wallet at `~/.donutcoin/wallet.json` if you press Enter (back it up; it is your coins).

The long way:

```bash
git clone https://github.com/nyc-esq/donutcoin && cd donutcoin
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m donutcoin wallet new                                   # your address
.venv/bin/python -m donutcoin miner --node https://donutcoin.meme --address D... --threads 4 --tag me
.venv/bin/python -m donutcoin wallet balance --node https://donutcoin.meme
.venv/bin/python -m donutcoin wallet send --to @someone --amount 250 --node https://donutcoin.meme
.venv/bin/python -m donutcoin note "up to 280 characters, kept forever"      # a note in the chain: needs a block found this week, a Ribbon, a @name, 100 DONUT fee
DONUTCOIN_PEERS=https://donutcoin.meme .venv/bin/python -m donutcoin node    # your own node: http://localhost:8555
```

Nothing updates itself. A node that syncs from a newer one says so in its log and on its own
explorer, with the block at which any rule changes; you pull and restart when you choose. Rule
changes are announced with an activation height in the release notes.

A node keeps a full copy of the chain, checks every block itself, serves the explorer and the
API, and adopts the chain with the most work. Anyone may submit a block with valid proof of work.

## What is in here

| Path | Does |
|---|---|
| `donutcoin/crypto.py` | hashes, keys, addresses, the compact difficulty encoding |
| `donutcoin/tx.py` | UTXO transactions and signatures |
| `donutcoin/chain.py` | blocks, validation, retargeting, persistence, chain replacement |
| `donutcoin/node.py` | the node: API, explorer, peers, wallet accounts, push notifications |
| `donutcoin/miner.py` | the miner, one process per thread |
| `donutcoin/wallet.py` | the command-line wallet |
| `donutcoin/market.py` | the offer board (DONUT for Apple Cash or Cash App, no custody) |
| `donutcoin/push.py` | web push |
| `static/` | the explorer, wallet and market pages; the white paper, terms and privacy |
| `tests/` | 45 adversarial tests: `python -m pytest tests -q` |

## License

MIT. See `LICENSE`.
