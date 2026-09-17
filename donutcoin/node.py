"""The node: keeps the chain, serves the API and the explorer, gossips blocks and transactions
to its peers, and adopts a peer's chain when it carries more work.

  DONUTCOIN_DATA   data directory (default ~/.donutcoin)
  DONUTCOIN_PORT   listen port (default 8555)
  DONUTCOIN_PEERS  comma-separated http://host:port list
  DONUTCOIN_NAME   this node's name in /api/info
"""
import asyncio, hashlib, json, os, re, sqlite3, sys, time, urllib.request
from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .chain import Chain, Block, ValidationError, BLOCK_REWARD, TARGET_SPACING, RETARGET_INTERVAL, COINBASE_MATURITY, MAX_BLOCK_TXS, DEAD_ADDRESSES
from .tx import Tx, COIN
from .crypto import bits_to_target, difficulty_from_bits, work_from_bits, is_valid_address, pubkey_to_address
from .market import Market, MarketError, METHODS, SETTLE_CONFIRMATIONS
from .push import Push

DATA = os.path.expanduser(os.environ.get("DONUTCOIN_DATA", "~/.donutcoin"))
PORT = int(os.environ.get("DONUTCOIN_PORT", "8555"))
PEERS = [p.strip().rstrip("/") for p in os.environ.get("DONUTCOIN_PEERS", "").split(",") if p.strip()]
NAME = os.environ.get("DONUTCOIN_NAME", os.uname().nodename)
SECRET = os.environ.get("DONUTCOIN_SECRET", "")      # shared by the nodes; required for block push and peer changes when set
SELF_URL = os.environ.get("DONUTCOIN_SELF", "")      # how peers reach this node, e.g. http://<your-node-hostname>:8555
STATIC = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "static")   # PyInstaller unpacks next to _MEIPASS

app = FastAPI(title="Donut Coin node")
chain = Chain(DATA)
push = Push(DATA, os.path.join(DATA, "accounts.db"))
market = Market(os.path.join(DATA, "market.db"), chain, notify=push.notify_address)
_notified_height = chain.height
peers: set[str] = set(PEERS)
started = time.time()


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", "X-Donut-Key": SECRET, "X-Donut-Origin": SELF_URL, "User-Agent": "donutcoin-node/0.1"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)


def keyed(request: Request) -> bool:
    return not SECRET or request.headers.get("x-donut-key", "") == SECRET


def gossip(path: str, body: dict, exclude: str | None = None) -> None:
    for p in list(peers):
        if p == exclude: continue
        try: http("POST", p + path, body, timeout=3)
        except Exception: pass


def sync_from(peer: str) -> str:
    """Pull the peer's chain if it has more work than ours."""
    try:
        info = http("GET", peer + "/api/info")
    except Exception as e:
        return f"{peer}: unreachable ({e.__class__.__name__})"
    if int(info["work"]) <= chain.work(): return f"{peer}: not ahead"
    blocks, start = [], 1
    while True:
        page = http("GET", f"{peer}/api/chain?from={start}&limit=2000", timeout=60)
        blocks += [Block.from_dict(d) for d in page]
        if len(page) < 2000: break
        start += len(page)
    try:
        return f"{peer}: adopted, height {chain.height}" if chain.replace_with(blocks) else f"{peer}: rejected (not more work)"
    except ValidationError as e:
        return f"{peer}: invalid chain ({e})"


def pull_mempool(peer: str) -> int:
    """A transaction submitted at a peer (the public node) has to reach the node the miner uses."""
    n = 0
    try:
        for d in http("GET", peer + "/api/mempool"):
            try: chain.add_to_mempool(Tx.from_dict(d)); n += 1
            except ValidationError: pass
    except Exception: pass
    return n


def hashrate_estimate(n: int = 20) -> float:
    b = chain.blocks[-n:]
    if len(b) < 2: return 0.0
    span = max(b[-1].timestamp - b[0].timestamp, 1)
    return sum(work_from_bits(x.bits) for x in b[1:]) / span


def block_summary(b: Block, h: int) -> dict:
    return {"height": h, "hash": b.hash, "timestamp": b.timestamp, "txs": len(b.txs), "bits": b.bits,
            "difficulty": round(difficulty_from_bits(b.bits), 3), "miner": b.txs[0].outputs[0].address,
            "tag": b.txs[0].extra, "reward": b.txs[0].outputs[0].amount}


# ---- explorer
app.mount("/static", StaticFiles(directory=STATIC), name="static")
@app.get("/", response_class=HTMLResponse)
def explorer():
    with open(os.path.join(STATIC, "explorer.html")) as f: return f.read()


PAGES = {"mine": "mine.html", "whitepaper": "whitepaper.html", "terms": "terms.html", "privacy": "privacy.html", "wallet": "wallet.html", "market": "market.html", "how": "how.html"}

_releases_cache = {"at": 0.0, "data": None}


def latest_release() -> dict | None:
    """The newest tagged release on GitHub, cached ten minutes; the downloads page is built from it."""
    if time.time() - _releases_cache["at"] < 600: return _releases_cache["data"]
    try:
        req = urllib.request.Request("https://api.github.com/repos/nyc-esq/donutcoin/releases/latest", headers={"User-Agent": "donutcoin-node/0.1", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r: data = json.load(r)
    except Exception:
        data = _releases_cache["data"]
    _releases_cache.update(at=time.time(), data=data); return data


@app.get("/download", response_class=HTMLResponse)
def download():
    rel = latest_release() or {}
    assets = {a["name"]: a for a in rel.get("assets", [])}
    mac = ("Safari unpacks the zip. Double-click the file: Terminal opens and macOS says it cannot verify it. "
           "Open System Settings, Privacy &amp; Security, scroll down, click Open Anyway, then double-click again. Once. "
           "Or skip the dialog with the Terminal line below.")
    rows = [("donutcoin-macos-arm64.zip", "Mac, Apple silicon (M1 and later)", mac),
            ("donutcoin-macos-intel.zip", "Mac, Intel", mac),
            ("donutcoin-windows.exe", "Windows", "Windows will say it is unrecognised: More info, then Run anyway. Once."),
            ("donutcoin-linux-x86_64", "Linux (x86_64)", "chmod +x the file, then run it.")]
    with open(os.path.join(STATIC, "download.html")) as f: page = f.read()
    items = ""
    for name, label, note in rows:
        a = assets.get(name)
        if a:
            digest = (a.get("digest") or "").replace("sha256:", "")
            sha = f'<div class="muted mono" style="font-size:11px;word-break:break-all">SHA-256 {digest}</div>' if digest else ""
            items += f'<div class="dl"><div><b>{label}</b><div class="muted">{note} {a["size"] // 1048576} MB.</div>{sha}</div><a href="/download/file/{name}"><button>Download</button></a></div>'
        else:
            items += f'<div class="dl"><div><b>{label}</b><div class="muted">Being built; back within the hour.</div></div><button disabled>Not yet</button></div>'
    version = rel.get("tag_name", "not released yet"); when = (rel.get("published_at") or "")[:10]
    src = f'<a href="/source/tree/{version}">source at {version}</a>' if rel.get("tag_name") else ""
    return page.replace("{{ITEMS}}", items).replace("{{VERSION}}", version).replace("{{WHEN}}", when).replace("{{SOURCE}}", src)


@app.get("/{page}", response_class=HTMLResponse)
def page(page: str):
    if page == "source":           # the catch-all page route is registered first, so the bare /source lands here
        return Response(status_code=302, headers={"Location": "https://github.com/nyc-esq/donutcoin", "Cache-Control": "no-cache"})
    if page in ("install.sh", "install.ps1"):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(getattr(sys, "_MEIPASS", root), page) if os.path.exists(os.path.join(getattr(sys, "_MEIPASS", root), page)) else os.path.join(root, page)) as f:
            return Response(f.read(), media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-cache"})
    if page == "sw.js":            # the service worker must live at the root; this route catches it first
        with open(os.path.join(STATIC, "sw.js")) as f: return Response(f.read(), media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache, max-age=0"})   # never stale at the edge
    if page not in PAGES: raise HTTPException(404, "no such page")
    with open(os.path.join(STATIC, PAGES[page])) as f: return f.read()


# ---- api
@app.get("/api/info")
def info():
    return {"name": NAME, "height": chain.height, "tip": chain.tip.hash, "work": str(chain.work()), "bits": chain.next_bits(),
            "difficulty": round(difficulty_from_bits(chain.next_bits()), 3), "target": f"{bits_to_target(chain.next_bits()):064x}",
            "supply": chain.supply(), "reward": BLOCK_REWARD, "mempool": len(chain.mempool), "peers": len(peers),   # a count: peer addresses are private (2026-09-17)
            "hashrate": round(hashrate_estimate(), 1), "spacing": TARGET_SPACING, "retarget": RETARGET_INTERVAL,
            "maturity": COINBASE_MATURITY, "uptime": int(time.time() - started), "time": int(time.time())}


_stats_cache = {"at": 0.0, "data": None}


@app.get("/api/stats")
def stats():
    """The front page's charts, in one call, cached 30 s: hashrate per hour, transactions per hour,
    supply over the day, top holders, the tip's time, and the market's price points."""
    if time.time() - _stats_cache["at"] < 30: return _stats_cache["data"]
    now = int(time.time()); day = [b for b in chain.blocks if now - b.timestamp <= 86400 and b.timestamp > 0]
    hours = [{"h": i, "work": 0, "blocks": 0, "txs": 0, "minted": 0} for i in range(24)]
    for b in day:
        i = 23 - min(23, (now - b.timestamp) // 3600)
        hours[i]["work"] += work_from_bits(b.bits); hours[i]["blocks"] += 1; hours[i]["txs"] += len(b.txs) - 1; hours[i]["minted"] += b.txs[0].outputs[0].amount
    for h in hours: h["hashrate"] = round(h["work"] / 3600, 1)
    supply_now = chain.supply(); series, run = [], supply_now
    for h in reversed(hours): series.append(run); run -= h["minted"]
    series.reverse()
    balances: dict[str, int] = {}
    for (a, addr, _, _) in chain.utxo.values(): balances[addr] = balances.get(addr, 0) + a
    top = sorted(((a, v) for a, v in balances.items() if a not in DEAD_ADDRESSES), key=lambda kv: -kv[1])[:10]
    dead = sum(v for a, v in balances.items() if a in DEAD_ADDRESSES)
    live = supply_now - dead or 1
    holders = [{"address": a, "name": name_of(a), "balance": v, "share": round(100 * v / live, 2)} for a, v in top]
    data = {"time": now, "tip_time": chain.tip.timestamp, "height": chain.height, "hours": hours, "supply_series": series, "supply": supply_now,
            "circulating": supply_now - dead, "holders": holders, "addresses": len([a for a in balances if a not in DEAD_ADDRESSES]), "mempool": len(chain.mempool), "price": price()}
    _stats_cache.update(at=time.time(), data=data); return data


@app.get("/api/blocks")
def blocks(limit: int = Query(20, le=2000)):
    h = chain.height
    out = [block_summary(chain.blocks[i], i) for i in range(h, max(h - limit, -1), -1)]
    names = {}
    for b in out:
        if b["miner"] not in names: names[b["miner"]] = name_of(b["miner"])
        b["miner_name"] = names[b["miner"]]
        b["dead"] = b["miner"] in DEAD_ADDRESSES     # genesis and retired wallets: the explorer leaves them out of the miner charts
    return out


@app.get("/api/block/{key}")
def block(key: str):
    h = chain.index.get(key) if len(key) == 64 else (int(key) if key.isdigit() else None)
    if h is None or h > chain.height: raise HTTPException(404, "no such block")
    return chain.blocks[h].to_dict(h)


@app.get("/api/tx/{txid}")
def tx(txid: str):
    if txid in chain.mempool: return {"status": "mempool", **chain.mempool[txid].to_dict()}
    loc = chain.txindex.get(txid)
    if not loc: raise HTTPException(404, "no such tx")
    h, pos = loc
    return {"status": "confirmed", "height": h, "confirmations": chain.height - h + 1, **chain.blocks[h].txs[pos].to_dict()}


@app.get("/api/address/{addr}")
def address(addr: str):
    if not is_valid_address(addr): raise HTTPException(400, "not a Donut Coin address")
    txs = []
    for h in range(chain.height, -1, -1):
        for t in chain.blocks[h].txs:
            spent_amount = 0
            for i in t.inputs:
                if i.pubkey and pubkey_to_address(bytes.fromhex(i.pubkey)) == addr:
                    loc = chain.txindex.get(i.txid)
                    if loc: spent_amount += chain.blocks[loc[0]].txs[loc[1]].outputs[i.index].amount
            received = sum(o.amount for o in t.outputs if o.address == addr)
            if received or spent_amount:
                txs.append({"txid": t.txid, "height": h, "coinbase": t.coinbase, "received": received, "spent_from": spent_amount > 0,
                            "net": received - spent_amount})
        if len(txs) >= 50: break
    return {"address": addr, "balance": chain.balance(addr), "spendable": chain.spendable(addr), "recent": txs[:50]}


@app.get("/api/chain")
def raw_chain(start: int = Query(1, alias="from"), limit: int = Query(2000, le=5000)):
    return [b.to_dict(h) for h, b in enumerate(chain.blocks) if h >= start][:limit]


@app.get("/api/mempool")
def mempool():
    return [t.to_dict() for t in chain.mempool.values()]


@app.get("/api/template")
def template(address: str, extra: str = ""):
    if not is_valid_address(address): raise HTTPException(400, "not a Donut Coin address")
    return chain.template(address, extra)


@app.post("/api/tx")
def submit_tx(body: dict):
    t = Tx.from_dict(body)
    try: txid = chain.add_to_mempool(t)
    except ValidationError as e: raise HTTPException(400, str(e))
    gossip("/api/tx", t.to_dict())
    return {"txid": txid}


@app.post("/api/block")
async def submit_block(body: dict, request: Request):
    """Anyone may submit a block: proof of work is the admission ticket, and an invalid block costs
    one Scrypt hash to reject. Only a keyed peer may make this node *sync from* somewhere (the
    origin header), which is the part a stranger must never steer."""
    if len(body.get("txs", [])) > MAX_BLOCK_TXS: raise HTTPException(400, "too many txs")
    b = Block.from_dict(body)
    origin = request.headers.get("x-donut-origin") if keyed(request) else None
    if b.hash in chain.index: return {"status": "known", "height": chain.index[b.hash]}
    try:
        h = chain.add_block(b)
    except ValidationError as e:
        # not on our tip: maybe the sender is ahead - pull instead of arguing
        if origin and b.prev_hash != chain.tip.hash:
            if origin.startswith("http"): peers.add(origin)
            msg = await asyncio.to_thread(sync_from, origin)
            return {"status": "synced", "detail": msg}
        raise HTTPException(400, str(e))
    await asyncio.to_thread(gossip, "/api/block", b.to_dict(), origin)
    return {"status": "accepted", "height": h, "hash": b.hash}


# ---- accounts: hosted, non-custodial. The browser derives two keys from the password: one encrypts
# the wallet key locally (AES-GCM), one is sent as `auth` to log in. The server stores a hash of `auth`
# and the ciphertext. It can hand the blob back; it can never open it, spend from it, or reset it.
ACCOUNTS_DB = os.path.join(DATA, "accounts.db")
USERNAME = re.compile(r"^[a-z0-9_]{3,24}$")
_buckets: dict[str, deque] = defaultdict(deque)


def _db():
    c = sqlite3.connect(ACCOUNTS_DB, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY, auth_hash TEXT NOT NULL, blob TEXT NOT NULL, address TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL)")
    if "email" not in [r[1] for r in c.execute("PRAGMA table_info(accounts)")]:
        c.execute("ALTER TABLE accounts ADD COLUMN email TEXT")          # optional, 2026-09-17; unverified; used for nothing yet
    return c


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(body: dict) -> str | None:
    e = str(body.get("email") or "").strip().lower()
    if not e: return None
    if len(e) > 254 or not EMAIL.match(e): raise HTTPException(400, "that does not look like an email address")
    return e


def _pepper(kind: str, value: str) -> str:
    return hashlib.sha256(f"{SECRET}|{kind}|{value}".encode()).hexdigest()


def _limit(request: Request, n: int = 20, window: int = 600) -> None:
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    q = _buckets[ip]; now = time.time()
    while q and now - q[0] > window: q.popleft()
    if len(q) >= n: raise HTTPException(429, "slow down")
    q.append(now)


@app.get("/api/account/salt/{username}")
def account_salt(username: str, request: Request):
    """Deterministic per name, so an unknown name gets a salt too: no way to tell who exists."""
    _limit(request, 60)
    if not USERNAME.match(username): raise HTTPException(400, "username: 3-24 of a-z 0-9 _")
    return {"salt": _pepper("salt", username)}


@app.post("/api/account/register", status_code=201)
def account_register(body: dict, request: Request):
    _limit(request, 10)
    u, auth, blob, addr = str(body.get("username", "")), str(body.get("auth", "")), str(body.get("blob", "")), str(body.get("address", ""))
    if not USERNAME.match(u): raise HTTPException(400, "username: 3-24 of a-z 0-9 _")
    if not re.match(r"^[0-9a-f]{64}$", auth): raise HTTPException(400, "bad auth")
    if not (40 <= len(blob) <= 4096): raise HTTPException(400, "bad blob")
    if not is_valid_address(addr): raise HTTPException(400, "bad address")
    email = _email(body)
    c = _db()
    try:
        c.execute("INSERT INTO accounts (username, auth_hash, blob, address, created, updated, email) VALUES (?,?,?,?,?,?,?)", (u, _pepper("auth", auth), blob, addr, int(time.time()), int(time.time()), email)); c.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "that name is taken")
    finally: c.close()
    return {"username": u, "address": addr}


@app.post("/api/account/login")
def account_login(body: dict, request: Request):
    _limit(request, 20)
    u, auth = str(body.get("username", "")), str(body.get("auth", ""))
    c = _db(); row = c.execute("SELECT auth_hash, blob, address, email FROM accounts WHERE username=?", (u,)).fetchone(); c.close()
    if not row or row[0] != _pepper("auth", auth): raise HTTPException(401, "wrong name or password")
    return {"username": u, "blob": row[1], "address": row[2], "email": row[3]}


@app.get("/api/account/address/{username}")
def account_address(username: str, request: Request):
    """Pay by username: @name resolves to the address of that account. Public, like a phone book
    entry; the account's key and login stay private."""
    _limit(request, 120)
    u = username.lstrip("@").lower()
    if not USERNAME.match(u): raise HTTPException(400, "username: 3-24 of a-z 0-9 _")
    c = _db(); row = c.execute("SELECT address FROM accounts WHERE username=?", (u,)).fetchone(); c.close()
    if not row: raise HTTPException(404, f"no account named @{u}")
    return {"username": u, "address": row[0]}


@app.post("/api/account/update")
def account_update(body: dict, request: Request):
    """Rename and/or change password. Proves the old login, then swaps name, login hash and the
    re-encrypted blob in one step. The server still never sees a password or a key."""
    _limit(request, 20)
    u, auth = str(body.get("username", "")), str(body.get("auth", ""))
    new_u = str(body.get("new_username") or u); new_auth = str(body.get("new_auth") or auth); new_blob = body.get("new_blob")
    if not USERNAME.match(new_u): raise HTTPException(400, "username: 3-24 of a-z 0-9 _")
    if not re.match(r"^[0-9a-f]{64}$", new_auth): raise HTTPException(400, "bad auth")
    if new_blob is not None and not (40 <= len(str(new_blob)) <= 4096): raise HTTPException(400, "bad blob")
    c = _db()
    try:
        row = c.execute("SELECT auth_hash, blob FROM accounts WHERE username=?", (u,)).fetchone()
        if not row or row[0] != _pepper("auth", auth): raise HTTPException(401, "wrong name or password")
        if new_u != u and c.execute("SELECT 1 FROM accounts WHERE username=?", (new_u,)).fetchone(): raise HTTPException(409, "that name is taken")
        c.execute("UPDATE accounts SET username=?, auth_hash=?, blob=?, updated=? WHERE username=?",
                  (new_u, _pepper("auth", new_auth), str(new_blob) if new_blob is not None else row[1], int(time.time()), u))
        if "email" in body: c.execute("UPDATE accounts SET email=? WHERE username=?", (_email(body), new_u))
        c.commit()
    finally: c.close()
    return {"username": new_u}


# ---- the offer board (see market.py). Reads are public minus contacts; writes are signed by wallet keys.
def _m(fn, *a):
    try: return fn(*a)
    except MarketError as e: raise HTTPException(400, str(e))


def name_of(address: str | None) -> str | None:
    if not address: return None
    try:
        c = _db(); row = c.execute("SELECT username FROM accounts WHERE address=?", (address,)).fetchone(); c.close()
        return row[0] if row else None
    except Exception: return None


_doge_cache = {"at": 0.0, "usd": None}


def doge_usd() -> float | None:
    """Dogecoin's dollar price from CoinGecko, every ten minutes: the yardstick the operator asked for,
    since Doge is the code this coin is modelled on."""
    if time.time() - _doge_cache["at"] < 600: return _doge_cache["usd"]
    try:
        req = urllib.request.Request("https://api.coingecko.com/api/v3/simple/price?ids=dogecoin&vs_currencies=usd", headers={"User-Agent": "donutcoin-node/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r: usd = float(json.load(r)["dogecoin"]["usd"])
    except Exception:
        usd = _doge_cache["usd"]
    _doge_cache.update(at=time.time(), usd=usd); return usd


@app.get("/api/price")
def price():
    last = market.last_trade(); d = doge_usd()
    out = {"doge_usd": d, "last_trade": last, "points": market.price_points()}
    if last and d: out["donut_in_doge"] = last["cents_per_donut"] / 100 / d
    return out


def settling(o: dict) -> None:
    """Read-only: for a paid trade, has the seller's payment landed yet, and how deep is it? The
    watcher (market.py) completes the trade at SETTLE_CONFIRMATIONS; this lets the page count up to it."""
    o["settle_needed"] = SETTLE_CONFIRMATIONS; o["settling_height"] = None; o["confirmations"] = 0
    if o.get("status") != "paid" or not o.get("seller") or not o.get("buyer"): return
    for h in range(max(o.get("taken_height") or 0, 0) + 1, chain.height + 1):
        for t in chain.blocks[h].txs:
            if t.coinbase: continue
            if any(i.pubkey and pubkey_to_address(bytes.fromhex(i.pubkey)) == o["seller"] for i in t.inputs) and sum(x.amount for x in t.outputs if x.address == o["buyer"]) >= o["amount"]:
                o["settling_height"] = h; o["confirmations"] = chain.height - h + 1; return


@app.get("/api/market")
def market_board(address: str | None = None):
    offers = market.board(address); mine = market.mine(address) if address and is_valid_address(address) else []
    for o in offers + mine:
        o["seller_name"] = name_of(o.get("seller")); o["buyer_name"] = name_of(o.get("buyer"))
    for o in mine: settling(o)
    return {"methods": METHODS, "offers": offers, "mine": mine, "price": price(), "height": chain.height}


@app.get("/api/market/rep/{address}")
def market_rep(address: str):
    if not is_valid_address(address): raise HTTPException(400, "not a Donut Coin address")
    return market.reputation(address)


@app.post("/api/market/offer", status_code=201)
def market_create(body: dict, request: Request):
    _limit(request, 20); return _m(market.create, body)


@app.post("/api/market/offer/{oid}/take")
def market_take(oid: str, body: dict, request: Request):
    _limit(request, 30); return _m(market.take, oid, body)


@app.post("/api/market/offer/{oid}/paid")
def market_paid(oid: str, body: dict, request: Request):
    _limit(request, 30); return _m(market.paid, oid, body)


@app.post("/api/market/offer/{oid}/cancel")
def market_cancel(oid: str, body: dict, request: Request):
    _limit(request, 30); return _m(market.cancel, oid, body)


# ---- push notifications
@app.get("/api/push/key")
def push_key(): return {"key": push.public_key()}


@app.post("/api/push/subscribe")
def push_subscribe(body: dict, request: Request):
    _limit(request, 30); return _m(push.subscribe, body)


@app.post("/api/push/test")
def push_test(body: dict, request: Request):
    """Proves the whole path: a signed request from the account's key sends one notification to it."""
    _limit(request, 10)
    p = body.get("payload") or {}; u = str(p.get("username", "")).lower()
    addr = push._account_address(u)
    if not addr: raise HTTPException(400, "no such account")
    try: __import__("donutcoin.market", fromlist=["check_sig"]).check_sig(p, body.get("pubkey", ""), body.get("signature", ""), addr)
    except MarketError as e: raise HTTPException(400, str(e))
    n = push.notify(u, "Donut Coin", "Notifications work. Princess Donut approves.", "/wallet")
    return {"sent": n, "registered": push.count(u)}


@app.get("/api/push/status/{username}")
def push_status(username: str):
    return {"registered": push.count(username.lower())}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: dict, request: Request):
    _limit(request, 30); return _m(push.unsubscribe, body)


def notify_receipts() -> None:
    """Coins arriving (not mined) at an address with a subscribed account."""
    global _notified_height
    for h in range(_notified_height + 1, chain.height + 1):
        for t in chain.blocks[h].txs:
            if t.coinbase: continue
            for o in t.outputs:
                if push.usernames_for(o.address):
                    push.notify_address(o.address, "DONUT received", f"{o.amount / COIN:,.2f} DONUT arrived in block {h}.", "/wallet")
    _notified_height = chain.height


@app.get("/source")
def source():
    """The code, one click away: the site itself never prints the repository owner's handle."""
    return Response(status_code=302, headers={"Location": "https://github.com/nyc-esq/donutcoin", "Cache-Control": "no-cache"})


@app.get("/source/{rest:path}")
def source_path(rest: str, request: Request):
    """Also lets `git clone https://donutcoin.meme/source` work: git follows the redirect, query string included."""
    q = f"?{request.url.query}" if request.url.query else ""
    return Response(status_code=302, headers={"Location": f"https://github.com/nyc-esq/donutcoin/{rest}{q}", "Cache-Control": "no-cache"})


@app.get("/download/file/{name}")
def download_file(name: str):
    """Release files by name, so the page never prints the repository owner's URLs."""
    rel = latest_release() or {}
    for a in rel.get("assets", []):
        if a["name"] == name: return Response(status_code=302, headers={"Location": a["browser_download_url"], "Cache-Control": "no-cache"})
    raise HTTPException(404, "no such file in the latest release")


@app.get("/pay/{address}", response_class=HTMLResponse)
def pay(address: str, amount: str = ""):
    """A request-for-payment link: opens the wallet with the send form filled (after login if needed).
    Accepts an address or @username."""
    if address.startswith("@") or (not is_valid_address(address) and USERNAME.match(address.lower())):
        u = address.lstrip("@").lower()
        c = _db(); row = c.execute("SELECT address FROM accounts WHERE username=?", (u,)).fetchone(); c.close()
        if not row: raise HTTPException(404, f"no account named @{u}")
        address = f"@{u}"      # the wallet resolves it again and shows the name
    elif not is_valid_address(address): raise HTTPException(400, "not a Donut Coin address")
    try: amt = f"{float(amount):g}" if amount else ""
    except ValueError: amt = ""
    return f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/wallet#send={address}&amount={amt}"><title>Pay · Donut Coin</title><p>Opening your wallet… <a href="/wallet#send={address}&amount={amt}">continue</a></p>'


@app.get("/api/qr-link.svg")
def qr_link(text: str):
    """A QR for one of this site's own links (a pay request); anything else is refused."""
    if not (text.startswith("https://donutcoin.meme/") or text.startswith("http://")) or len(text) > 200: raise HTTPException(400, "only donutcoin.meme links")
    import segno, io
    buf = io.BytesIO(); segno.make(text, error="m").save(buf, kind="svg", scale=5, border=1, dark="#2b2118", light=None)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/qr/{address}.svg")
def qr(address: str):
    if not is_valid_address(address): raise HTTPException(400, "not a Donut Coin address")
    import segno, io
    buf = io.BytesIO(); segno.make(address, error="m").save(buf, kind="svg", scale=5, border=1, dark="#2b2118", light=None)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/peers")
def get_peers(request: Request):
    """Peer addresses are the operator's business; only a keyed caller sees them."""
    if not keyed(request): raise HTTPException(403, "peers only")
    return sorted(peers)


@app.post("/api/peers")
def add_peer(body: dict, request: Request):
    if not keyed(request): raise HTTPException(403, "peers only")
    p = body.get("url", "").rstrip("/")
    if p: peers.add(p)
    return sorted(peers)


@app.on_event("startup")
async def start_sync():
    async def loop():
        while True:
            for p in list(peers):
                await asyncio.to_thread(sync_from, p)
                await asyncio.to_thread(pull_mempool, p)
            try: await asyncio.to_thread(market.watch)
            except Exception as e: print(f"market watch: {e}", flush=True)
            try: await asyncio.to_thread(notify_receipts)
            except Exception as e: print(f"push receipts: {e}", flush=True)
            await asyncio.sleep(15)
    asyncio.create_task(loop())


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__": main()
