# Donut Coin

A proof-of-work coin named after Princess Donut, the Queen Anne Chonk, a cat in a tiara and
sunglasses. No value, all fun. Live at **https://donutcoin.meme** (explorer, wallet, market,
white paper).

| | |
|---|---|
| Proof of work | Scrypt (N=1024, r=1, p=1) over an 80-byte header, like Dogecoin |
| Block time | 60 s target; difficulty retargets every 10 blocks, clamped to 4x |
| Reward | 10,000 DONUT a block, never halves; supply uncapped |
| Units | 1 DONUT = 100,000,000 sprinkles. Four above it, base 10: Ribbon (1,000,000 DONUT, 100 blocks), Tiara (10 Ribbons), Crown (10 Tiaras), Queen (10 Crowns) |
| Addresses | secp256k1, Base58Check with Dogecoin's version byte, so they start with **D** |
| Coinbase maturity | 3 blocks, 21 from block 3,444. A reorg destroys a coinbase rather than returning it to the mempool, and the reorg cap is 20 |
| Block limits | at most 500 transactions and 3,000 inputs and outputs per block; 2,000 inputs and 500 outputs per transaction. Every input is a signature check, so this bounds the work a block can force on a node |
| Node policy (not consensus) | builds blocks to 2,250 inputs and outputs, refuses dust under 0.01 DONUT, holds 5,000 waiting transactions. A block breaking these is still valid; set your own, no fork needed |

## Run it

The easy way, on a Mac or Linux (Python 3.10+ and git):

```bash
curl -fsSL https://donutcoin.meme/install.sh | bash
```

Windows, in PowerShell: `irm https://donutcoin.meme/install.ps1 | iex`. Or download a single
file from the [latest release](https://github.com/nyc-esq/donutcoin/releases/latest).

Both ask for the @username of your account on donutcoin.meme/wallet and mine into it, or make a
wallet at `~/.donutcoin/wallet.json` if you press Enter (back it up; it is your coins).

`@yourname` is an account made on the [wallet page](https://donutcoin.meme/wallet); mine into it
and your blocks carry your name. A bare `D...` address works too and stays anonymous.

Or clone it and read what you are running first. The installer is eighteen lines; this is
the same thing by hand:

```bash
git clone https://github.com/nyc-esq/donutcoin && cd donutcoin
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m donutcoin miner --node https://donutcoin.meme --address @yourname --threads 4
.venv/bin/python -m donutcoin wallet new                                   # or mine to a wallet file: this prints its address
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

## Leave it running

A miner you have to remember to start is a demo. A miner that survives a reboot is a node. Both
recipes below run as *you*, need no root, and pick up the account you chose the first time
(remembered in `~/.donutcoin/start.json`), so they take no arguments.

**macOS.** Save as `~/Library/LaunchAgents/meme.donutcoin.miner.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>meme.donutcoin.miner</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>cd "$HOME/donutcoin" &amp;&amp; exec .venv/bin/python -m donutcoin start --threads 4</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>20</integer>
  <key>StandardOutPath</key><string>/tmp/donutcoin.log</string>
  <key>StandardErrorPath</key><string>/tmp/donutcoin.log</string>
</dict></plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/meme.donutcoin.miner.plist
launchctl print gui/$(id -u)/meme.donutcoin.miner | head    # is it alive?
tail -f /tmp/donutcoin.log
launchctl bootout gui/$(id -u)/meme.donutcoin.miner         # stop and forget it
```

Do **not** add `ProcessType` / `Background` to that plist, however tempting it reads. On Apple
silicon it pins the process to the efficiency cores and costs you most of your hashrate. `Nice`
and `LowPriorityIO` are the polite knobs; they yield the machine without giving up the P-cores.
A laptop still stops mining when it sleeps — that is the lid, not the agent.

**Linux.** Save as `~/.config/systemd/user/donutcoin.service`:

```ini
[Unit]
Description=Donut Coin miner
After=network-online.target

[Service]
WorkingDirectory=%h/donutcoin
ExecStart=%h/donutcoin/.venv/bin/python -m donutcoin start --threads 4
Restart=always
RestartSec=10
Nice=19
CPUWeight=10
IOWeight=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now donutcoin
loginctl enable-linger "$USER"        # keep mining when you are not logged in
journalctl --user -u donutcoin -f
```

**Windows.** Task Scheduler, "Create Basic Task", trigger *When I log on*, action
`%USERPROFILE%\donutcoin\.venv\Scripts\python.exe` with arguments `-m donutcoin start` and
"Start in" `%USERPROFILE%\donutcoin`. Tick *Run whether user is logged on or not* only if you
want it mining at the login screen.

**How many threads?** Start at half your cores and watch the temperature, not the hashrate. More
threads is not reliably faster: past a certain point the work spreads onto cores that boost lower,
and on several machines fewer threads ran both slower *and* hotter. Find the setting your fans can
live with and leave it there. This coin is worth nothing; it is not worth your laptop.

Nothing here updates itself. To take a new version: `git -C ~/donutcoin pull` and restart the
service.

## Running a node

A node keeps a full copy of the chain, checks every block itself, serves the explorer and the
API, and adopts the chain with the most work. It trusts no peer: a bad block is rejected whoever
sent it, so pointing at someone else's node to catch up costs you nothing in trust.

```bash
cd ~/donutcoin
DONUTCOIN_PEERS=https://donutcoin.meme .venv/bin/python -m donutcoin node
```

It listens on `0.0.0.0:8555` and needs about 58 MB of memory. The chain is 2.2 MB for the first
few thousand blocks and grows by roughly a block a minute.

### Docker

```bash
docker compose up -d      # then http://localhost:8555
```

The compose file builds locally, runs as a non-root user, and keeps everything in a named volume
at `/data`. A prebuilt multi-arch image is published with each release if you would rather not
build, and it covers arm64 as well as x86_64:

```bash
docker run -d -p 8555:8555 -v donutcoin-data:/data \
  -e DONUTCOIN_PEERS=https://donutcoin.meme ghcr.io/nyc-esq/donutcoin:latest
```
 Mining is commented out and off by default: a node is useful on its own, and nobody's
container should start burning CPU because they ran `up`.

The image is about 265 MB, most of which is `python:3.12-slim`. The healthcheck asks only whether
the node is serving, not whether it has caught up, because a node doing its first sync is
legitimately behind and a height-based check would restart it forever at the worst moment.

If you bind-mount a host directory instead of using the named volume, it has to be writable by
uid 1000, or the first write fails.

### Settings

All configuration is environment variables. Put them in a file and have systemd or launchd read
it; there is no config file and at eight keys there does not need to be one.

| Variable | Default | Does |
|---|---|---|
| `DONUTCOIN_DATA` | `~/.donutcoin` | data directory: the chain, the wallet, the small databases |
| `DONUTCOIN_PORT` | `8555` | listen port |
| `DONUTCOIN_PEERS` | empty | comma-separated node URLs to sync from |
| `DONUTCOIN_NAME` | the machine's hostname | the name this node reports in `/api/info` |
| `DONUTCOIN_SELF` | empty | how peers reach this node, if it is reachable |
| `DONUTCOIN_SECRET` | empty | shared secret; only a peer holding it may make this node sync from it |
| `DONUTCOIN_WALLET` | `~/.donutcoin/wallet.json` | wallet file for the miner and the command line. Note it does **not** follow `DONUTCOIN_DATA`: set both if you move the data directory |
| `DONUTCOIN_TO` | ask | `@username` or `D…` address that `start` mines into |
| `DONUTCOIN_TAG` | your `@username`, else `donut` | a label on your blocks |

`DONUTCOIN_SECRET` is the only one with a security consequence: leave it empty and nobody can
steer which chain your node follows.

### Behind a reverse proxy

The node speaks plain HTTP and does no TLS of its own. Terminate it in front. It needs nothing
special: no websockets, no long-polling, no path rewriting. Serve it at a domain root, not under
a sub-path, because the pages link to `/api/...` absolutely.

Caddy, which is two lines and gets you a certificate:

```
donut.example.com {
    reverse_proxy localhost:8555
}
```

nginx, if you already run one:

```nginx
server {
    server_name donut.example.com;
    location / {
        proxy_pass http://127.0.0.1:8555;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

If you are not exposing it, bind it to your own machine instead and skip all of this: the node
has no account you can lose and no admin page, but it does serve a wallet, and a wallet on the
open internet over plain HTTP is a bad habit even when the keys never leave the browser.

### Backups

One file matters. Everything else can be thrown away.

| Path | Back it up? |
|---|---|
| `<data>/wallet.json` | **yes.** It is your keys. Lose it and the coins are gone; there is no reset |
| `<data>/chain.jsonl` | no. It re-downloads from any peer, and your node re-validates it |
| `<data>/*.db` | only if you run a public node with accounts or offers on it |

A wallet file is small and changes rarely. Copy it somewhere off the machine once, check you can
read it, and you are done. Nothing here needs a backup schedule.

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
| `tests/` | 74 adversarial tests: `pip install pytest httpx` then `python -m pytest tests -q` |

## License

MIT. See `LICENSE`.
