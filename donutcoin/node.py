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
from . import __version__
from .chain import Chain, Block, ValidationError, BLOCK_REWARD, TARGET_SPACING, RETARGET_INTERVAL, COINBASE_MATURITY, maturity_at, MATURITY_RULES_HEIGHT, MAX_BLOCK_TXS, DEAD_ADDRESSES, MAX_NOTE_CHARS, NOTE_RULES_HEIGHT, NOTE_MIN_FEE, NOTE_LICENCE_BLOCKS
from .tx import Tx, COIN, RIBBON, UNITS
from .crypto import bits_to_target, difficulty_from_bits, work_from_bits, is_valid_address, pubkey_to_address, sha256d, verify
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


def version_tuple(v: str) -> tuple:
    try: return tuple(int(x) for x in str(v).split("."))
    except ValueError: return (0,)


RULES = {"notes_from": NOTE_RULES_HEIGHT, "maturity_from": MATURITY_RULES_HEIGHT}   # consensus knobs this node runs with; a peer's differ = a rule change
_update = {"version": None, "rules": None, "peer_name": None, "at": 0}
def note_update(info: dict) -> None:
    """A peer runs a newer version: remember it, say so once in the log, show it in /api/info and on
    the explorer. A notice, never a download: nobody should run code because a server said to."""
    v = str(info.get("version") or "")
    if not v or version_tuple(v) <= version_tuple(__version__): return
    rules = info.get("rules") if info.get("rules") != RULES else None
    if _update["version"] != v:
        print(f"update: a newer Donut Coin is out, v{v} (this node runs v{__version__})" + (f"; rule change: {rules}" if rules else "") + ". Get it from the Download page.", flush=True)
    _update.update(version=v, rules=rules, peer_name=info.get("name"), at=int(time.time()))


def sync_from(peer: str) -> str:
    """Pull the peer's chain if it has more work than ours."""
    try:
        info = http("GET", peer + "/api/info")
    except Exception as e:
        return f"{peer}: unreachable ({e.__class__.__name__})"
    note_update(info)
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
HTML_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}   # the pages change several times a day;
# without this a browser caches them by its own heuristics and a card added today can stay invisible


def html(name: str) -> str:
    """A page from static/, with the node's own version in the footer. One source for the version on
    every page: it used to be the node's number in one place and the latest GitHub release's in
    another, and on 2026-09-26 both said 0.1.9 about different code."""
    with open(os.path.join(STATIC, name)) as f: return f.read().replace("{{SITE_VERSION}}", __version__)


@app.get("/", response_class=HTMLResponse)
def explorer():
    return HTMLResponse(html("explorer.html"), headers=HTML_HEADERS)


PAGES = {"notes": "notes.html", "mine": "mine.html", "whitepaper": "whitepaper.html", "terms": "terms.html", "privacy": "privacy.html", "wallet": "wallet.html", "market": "market.html", "how": "how.html"}

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
            ("donutcoin-linux-x86_64", "Linux (x86_64)", "chmod +x the file, then run it."),
            ("donutcoin-linux-arm64", "Linux (arm64: Raspberry Pi, Ampere, an ARM VPS)", "chmod +x the file, then run it.")]
    page = html("download.html")
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
    if page == "notes.xml": return notes_rss()
    if page not in PAGES: raise HTTPException(404, "no such page")
    return HTMLResponse(html(PAGES[page]), headers=HTML_HEADERS)


# ---- api
def _settling_range() -> range:
    """Blocks whose transactions are not final yet: final means more than 20 confirmations."""
    return range(max(1, chain.height - 19), chain.height + 1)


def settling_count() -> int:
    return sum(len(chain.blocks[h].txs) - 1 for h in _settling_range())


def settling_left() -> int:
    """Blocks until the oldest unfinished transaction is final; 0 when there is nothing settling."""
    for h in _settling_range():
        if len(chain.blocks[h].txs) > 1: return 21 - (chain.height - h + 1)
    return 0


@app.get("/api/info")
def info():
    return {"name": NAME, "height": chain.height, "tip": chain.tip.hash, "work": str(chain.work()), "bits": chain.next_bits(),
            "difficulty": round(difficulty_from_bits(chain.next_bits()), 3), "target": f"{bits_to_target(chain.next_bits()):064x}",
            "supply": chain.supply(), "reward": BLOCK_REWARD, "mempool": len(chain.mempool), "peers": len(peers),   # a count: peer addresses are private (2026-09-17)
            "hashrate": round(hashrate_estimate(), 1), "spacing": TARGET_SPACING, "retarget": RETARGET_INTERVAL,
            "settling": settling_count(), "settling_left": settling_left(),
            "reorgs": len(chain.reorgs), "last_reorg": chain.reorgs[-1] if chain.reorgs else None,
            "maturity": maturity_at(chain.height + 1), "uptime": int(time.time() - started), "time": int(time.time()),
            "genesis": chain.blocks[0].timestamp,   # the chain's own age lives here and nowhere else; "uptime" is only this process

            "version": __version__, "rules": RULES,
            "update": ({"version": _update["version"], "rules": _update["rules"]} if _update["version"] else None)}


_stats_cache = {"at": 0.0, "data": None}


_series_cache: dict[int, tuple] = {}


@app.get("/api/series")
def series(hours: int = Query(168, ge=1, le=8760)):
    """Every chart on the front page, for one window, in one pass over the blocks. 84 buckets,
    clipped to genesis so a young chain still fills its charts, and everything that counts events is
    normalised per hour so a bar means the same thing whatever the bucket's width (2026-09-19)."""
    hit = _series_cache.get(hours)
    if hit and time.time() - hit[0] < 30: return hit[1]
    now = int(time.time()); POINTS = 84
    start = max(chain.blocks[0].timestamp, now - hours * 3600)
    out = {"from": start, "to": now, "points": POINTS, "hours": hours,
           "blocks": [], "hashrate": [], "difficulty": [], "supply": [], "txs": [], "miners": [],
           "gap": [], "who": [], "by_miner": [], "tally": []}
    if now <= start: return out

    step = (now - start) / POINTS
    first = 0
    while first + 1 < len(chain.blocks) and chain.blocks[first + 1].timestamp <= start: first += 1

    # who is worth a line of their own: the five biggest miners inside the window
    tally_all: dict[str, int] = {}
    for b in chain.blocks[first + 1:]:
        a = b.txs[0].outputs[0].address
        if a not in DEAD_ADDRESSES: tally_all[a] = tally_all.get(a, 0) + 1
    ranked = sorted(tally_all, key=lambda a: -tally_all[a])
    top, rest = ranked[:5], set(ranked[5:])
    label = {a: (("@" + n) if (n := name_of(a)) else a[:10] + "…") for a in top}
    out["who"] = [{"name": label[a], "n": tally_all[a]} for a in top] + ([{"name": "others", "n": sum(tally_all[a] for a in rest)}] if rest else [])
    names = [label[a] for a in top] + (["others"] if rest else [])
    out["by_miner"] = [{"name": n, "vals": []} for n in names]
    out["tally"] = [{"name": n, "vals": []} for n in names]

    run = dict.fromkeys(names, 0)
    bi = first
    for k in range(POINTS):
        t, lo = start + (k + 1) * step, bi
        while bi + 1 < len(chain.blocks) and chain.blocks[bi + 1].timestamp <= t: bi += 1
        span = chain.blocks[lo + 1:bi + 1]
        per_hour = (step / 3600) or 1
        out["blocks"].append(round(len(span) / per_hour, 2))
        out["hashrate"].append(round(sum(work_from_bits(b.bits) for b in span) / step, 1))
        out["difficulty"].append(round(difficulty_from_bits(chain.blocks[bi].bits), 3))
        out["supply"].append((bi + 1) * BLOCK_REWARD)
        out["txs"].append(round(sum(len(b.txs) - 1 for b in span) / per_hour, 2))
        live = [b for b in span if b.txs[0].outputs[0].address not in DEAD_ADDRESSES]
        out["miners"].append(len({b.txs[0].outputs[0].address for b in live}))
        gaps = [chain.blocks[j].timestamp - chain.blocks[j - 1].timestamp for j in range(lo + 1, bi + 1) if j > 0]
        out["gap"].append(round(sum(gaps) / len(gaps), 1) if gaps else 0)
        for b in live:
            a = b.txs[0].outputs[0].address
            n = label.get(a, "others" if rest else None)
            if n: run[n] += 1
        counts = dict.fromkeys(names, 0)
        for b in live:
            a = b.txs[0].outputs[0].address
            n = label.get(a, "others" if rest else None)
            if n: counts[n] += 1
        for srs in out["by_miner"]: srs["vals"].append(counts[srs["name"]])
        for srs in out["tally"]: srs["vals"].append(run[srs["name"]])
    _series_cache[hours] = (time.time(), out)
    return out


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
    # The last week in 84 buckets, clipped to genesis so a young chain fills its own charts. One pass
    # over the blocks gives all three: supply (every block mints the same reward and fees only move
    # value, so supply is just the block count), the work done in each bucket, and the difficulty at
    # the end of it.
    WEEK, POINTS = 7 * 86400, 84
    start = max(chain.blocks[0].timestamp, now - WEEK)
    week, week_hash, week_diff, week_blocks = [], [], [], []
    if now > start:
        step = (now - start) / POINTS
        bi = 0
        while bi + 1 < len(chain.blocks) and chain.blocks[bi + 1].timestamp <= start: bi += 1
        for k in range(POINTS):
            t, lo = start + (k + 1) * step, bi
            while bi + 1 < len(chain.blocks) and chain.blocks[bi + 1].timestamp <= t: bi += 1
            week.append((bi + 1) * BLOCK_REWARD)
            week_hash.append(round(sum(work_from_bits(chain.blocks[j].bits) for j in range(lo + 1, bi + 1)) / step, 1))
            week_diff.append(round(difficulty_from_bits(chain.blocks[bi].bits), 3))
            week_blocks.append(round((bi - lo) / (step / 3600), 2))      # blocks per hour, whatever the bucket width
    balances: dict[str, int] = {}
    for (a, addr, _, _) in chain.utxo.values(): balances[addr] = balances.get(addr, 0) + a
    # the list is for people to read: a Ribbon or more, and none of the dead wallets. The count below
    # is the honest one and includes every address holding anything (2026-09-19).
    top = sorted(((a, v) for a, v in balances.items() if a not in DEAD_ADDRESSES and v >= RIBBON), key=lambda kv: -kv[1])[:10]
    dead = sum(v for a, v in balances.items() if a in DEAD_ADDRESSES)
    live = supply_now - dead or 1
    holders = [{"address": a, "name": name_of(a), "balance": v, "share": round(100 * v / live, 2)} for a, v in top]
    ever_paid = {b.txs[0].outputs[0].address for b in chain.blocks}     # including the dead ones, for the holder count
    mined: dict[str, list] = {}                         # all-time leaderboard: every coinbase since genesis
    for i, b in enumerate(chain.blocks):
        a = b.txs[0].outputs[0].address
        if a in DEAD_ADDRESSES: continue
        r = mined.setdefault(a, [0, i, i, 0])
        r[0] += 1; r[2] = i; r[3] += b.txs[0].outputs[0].amount
    total_mined = sum(r[0] for r in mined.values()) or 1
    miners = [{"address": a, "name": name_of(a), "blocks": r[0], "first": r[1], "last": r[2],
               "earned": r[3], "share": round(100 * r[0] / total_mined, 1)}
              for a, r in sorted(mined.items(), key=lambda kv: -kv[1][0])][:10]
    holders_named = sum(1 for a in balances if name_of(a))
    holders_mined = sum(1 for a in balances if a in ever_paid)            # has been paid for a block at some point
    data = {"holders_named": holders_named, "holders_mined": holders_mined,
            "time": now, "tip_time": chain.tip.timestamp, "height": chain.height, "hours": hours, "supply_series": series, "supply_week": week, "week_hashrate": week_hash, "week_difficulty": week_diff, "week_blocks": week_blocks, "supply_week_from": start, "supply": supply_now, "miners": miners,
            "circulating": supply_now - dead, "holders": holders, "addresses": len(balances), "listed": len(top), "mempool": len(chain.mempool), "price": price()}
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
    d = chain.blocks[h].to_dict(h); d["txs"] = [scrub(t) for t in d["txs"]]; return d


def signer_of(t: Tx) -> str | None:
    return pubkey_to_address(bytes.fromhex(t.inputs[0].pubkey)) if t.inputs and t.inputs[0].pubkey else None


# ---- the hide list: DATA/hidden.json, {"txids": {id: reason}, "addresses": {addr: reason}}, managed by
# `python -m donutcoin.hide` on the node. A hidden note stays on the chain (nothing can remove it) but
# this node stops showing it: gone from the feed, blanked in the block and tx views. Re-read every 30 s.
HIDDEN_PATH = os.path.join(DATA, "hidden.json")
_hidden = {"at": 0.0, "mtime": None, "txids": {}, "addresses": {}}
def hidden() -> dict:
    if time.time() - _hidden["at"] > 30:
        _hidden["at"] = time.time()
        try: mtime = os.path.getmtime(HIDDEN_PATH)
        except OSError: mtime = None
        if mtime != _hidden["mtime"]:
            _hidden["mtime"] = mtime
            try:
                with open(HIDDEN_PATH) as f: d = json.load(f)
                _hidden["txids"], _hidden["addresses"] = dict(d.get("txids", {})), dict(d.get("addresses", {}))
            except Exception: _hidden["txids"], _hidden["addresses"] = {}, {}
    return _hidden

def is_hidden(txid: str, signer: str | None) -> bool:
    h = hidden(); return txid in h["txids"] or (signer is not None and signer in h["addresses"])

def scrub(d: dict) -> dict:
    """A tx dict for people to read: a hidden note's text is blanked and marked."""
    if d.get("extra") and not d.get("coinbase"):
        ins = d.get("inputs") or []
        who = pubkey_to_address(bytes.fromhex(ins[0]["pubkey"])) if ins and ins[0].get("pubkey") else None
        if is_hidden(d.get("txid", ""), who): d = {**d, "extra": "", "hidden": True}
    return d


def note_record(txid: str) -> dict | None:
    """One note as the site shows it: named author, not hidden, with its block; else None."""
    loc = chain.txindex.get(txid)
    if not loc: return None
    h, pos = loc; t = chain.blocks[h].txs[pos]
    if pos == 0 or not t.extra: return None
    who = signer_of(t); nm = name_of(who)
    if not nm or is_hidden(txid, who): return None
    return {"height": h, "timestamp": chain.blocks[h].timestamp, "txid": txid, "from": who, "from_name": nm, "text": t.extra}


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@app.get("/note/{txid}", response_class=HTMLResponse)
def note_page(txid: str):
    """A note's permanent address: the text, who signed it, the block that timestamps it, the transaction it rides in."""
    n = note_record(txid) if re.match(r"^[0-9a-f]{64}$", txid) else None
    if not n: raise HTTPException(404, "no such note")
    page = html("note.html")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(n["timestamp"]))
    conf = chain.height - n["height"] + 1
    settled = ('<span class="pill">final</span> Twenty-one blocks deep, so the chain will not rewrite it.'
               if conf > 20 else
               f'<b>{21 - conf} more block{"" if 21 - conf == 1 else "s"}</b> until it is final. Until then a heavier chain could still replace it.')
    title = n["text"][:70] + ("…" if len(n["text"]) > 70 else "")
    return HTMLResponse((page.replace("{{TITLE}}", _esc(title)).replace("{{OGTEXT}}", _esc(" ".join(n["text"].split()))).replace("{{TEXT}}", _esc(n["text"])).replace("{{AUTHOR}}", _esc(n["from_name"]))
                .replace("{{HEIGHT}}", str(n["height"])).replace("{{WHEN}}", when).replace("{{TXID}}", n["txid"]).replace("{{ADDRESS}}", n["from"] or "").replace("{{SETTLED}}", settled)), headers=HTML_HEADERS)


def brag(r: dict) -> str:
    """The one sentence this page is for: what a miner would paste somewhere. It becomes the link
    preview, so it has to stand alone with no page around it. Names are already in the title; this
    is the achievement. Never a monetary value, here or anywhere the node serves."""
    b, rank, of = r["blocks"], r["rank"], r["of"]
    if not b:                       # holds coins without ever finding a block: tipped, paid or traded to
        lead = f"Holds {r['balance'] / COIN:,.0f} DONUT without ever having mined a block: someone else paid them."
    elif rank == 1:                 # "#1 of 5" undersells the only rank that needs no arithmetic
        lead = f"The largest miner here, with {b:,} block{'' if b == 1 else 's'} — {r['share']}% of every block ever found."
    elif b == 1:                    # one block is a real thing to have done, and a percentage would hide that
        lead = f"Found a block, which makes them one of only {of} people who ever have."
    else:                           # the honest boast carries the field size: five miners is the point
        lead = f"#{rank} of {of} miners, with {b:,} blocks — {r['share']}% of every block ever found."
    quiet = (f" Quiet since {time.strftime('%-d %B', time.gmtime(r['last_time']))}."
             if b and r["last_time"] and time.time() - r["last_time"] > 7 * 86400 else "")
    return f"{lead}{quiet} Donut Coin is a proof-of-work coin named after a cat, worth nothing on purpose."


@app.get("/who/{key}", response_class=HTMLResponse)
def who_page(key: str):
    """A miner's permanent address. People share pages that are about them, and until this existed
    a miner only appeared inside a lookup box with no URL to paste."""
    a = resolve_who(key)
    if not a: raise HTTPException(404, "no such miner")
    r = miner_record(a)
    if not r["blocks"] and not r["balance"]: raise HTTPException(404, "no such miner")
    page = html("who.html")
    label = ("@" + r["name"]) if r["name"] else (a[:10] + "…")
    when = lambda t: time.strftime("%-d %b %Y", time.gmtime(t)) if t else "–"
    eq = in_units(r["balance"])
    fields = {
        "{{WHO}}": label, "{{ADDRESS}}": a, "{{SLUG}}": ("@" + r["name"]) if r["name"] else a,
        "{{TITLE}}": f"{label} · Donut Coin",
        "{{OGTITLE}}": f"{label} on the Donut Coin chain",
        "{{OGDESC}}": brag(r),
        "{{BRAG}}": brag(r),
        "{{BLOCKS}}": f"{r['blocks']:,}",
        "{{RANK}}": (f"#{r['rank']} of {r['of']}" if r["rank"] else "–"),
        "{{SHARE}}": f"{r['share']}%",
        "{{HOLDS}}": f"{r['balance'] / COIN:,.0f}",
        "{{HOLDS_EQ}}": eq,
        "{{EARNED}}": f"{r['earned'] / COIN:,.0f}",
        "{{FIRST}}": (f"block {r['first']:,}, {when(r['first_time'])}" if r["first"] is not None else "never mined"),
        "{{LAST}}": (f"block {r['last']:,}, {when(r['last_time'])}" if r["last"] is not None else "–"),
        "{{NOTES}}": f"{r['notes']:,}",
        "{{NOTESROW}}": "" if r["notes"] else "display:none",
    }
    for k, v in fields.items(): page = page.replace(k, _esc(str(v)) if k != "{{NOTESROW}}" else v)
    return HTMLResponse(page, headers=HTML_HEADERS)


def notes_rss() -> Response:
    items = notes(limit=100)["notes"]
    body = "".join(f"<item><title>@{_esc(n['from_name'])}: {_esc(n['text'][:80])}</title><link>https://donutcoin.meme/note/{n['txid']}</link><guid isPermaLink=\"true\">https://donutcoin.meme/note/{n['txid']}</guid>"
                   f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(n['timestamp']))}</pubDate><description>{_esc(n['text'])} (block {n['height']})</description></item>" for n in items)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Said on the Donut Coin chain</title><link>https://donutcoin.meme/notes</link>'
           '<description>Notes written into Donut Coin blocks, 280 characters at most, signed by their authors, kept forever.</description>' + body + '</channel></rss>')
    return Response(xml, media_type="application/rss+xml; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/transfers")
def transfers(limit: int = Query(20, le=200)):
    """Transfers, newest first: who signed, who was paid, how much. Coinbases are left out (they are
    in the block table already) and so is the change an author pays back to themselves, so a note
    shows as a payment of 0 with `note` set rather than as a payment to oneself."""
    out, h = [], chain.height
    while h >= 0 and len(out) < limit:
        b = chain.blocks[h]
        for t in reversed(b.txs[1:]):
            frm = signer_of(t)
            paid = [(o.amount, o.address) for o in t.outputs if o.address != frm]
            note = t.extra if (t.extra and name_of(frm) and not is_hidden(t.txid, frm)) else ""
            out.append({"height": h, "timestamp": b.timestamp, "txid": t.txid,
                        "from": frm, "from_name": name_of(frm),
                        "to": paid[0][1] if paid else frm, "to_name": name_of(paid[0][1]) if paid else name_of(frm),
                        "amount": sum(a for a, _ in paid), "outputs": len(paid), "note": note})
            if len(out) >= limit: break
        h -= 1
    return out


@app.get("/api/notes")
def notes(limit: int = Query(50, le=500)):
    """Notes written into transactions, newest first: the text, who signed it, and the block that
    timestamps it. A note is any non-coinbase transaction with text in its `extra` field."""
    out, h = [], chain.height
    while h >= 0 and len(out) < limit:
        b = chain.blocks[h]
        for t in reversed(b.txs[1:]):
            if t.extra:
                who = signer_of(t); name = name_of(who)
                if not name or is_hidden(t.txid, who): continue          # named authors only; the hide list wins
                out.append({"height": h, "timestamp": b.timestamp, "txid": t.txid, "from": who, "from_name": name, "text": t.extra})
                if len(out) >= limit: break
        h -= 1
    return {"max_chars": MAX_NOTE_CHARS, "min_fee": NOTE_MIN_FEE, "rules_from": NOTE_RULES_HEIGHT, "licence_blocks": NOTE_LICENCE_BLOCKS, "height": chain.height, "notes": out}


@app.get("/api/tx/{txid}")
def tx(txid: str):
    if txid in chain.mempool: return {"status": "mempool", **scrub(chain.mempool[txid].to_dict())}
    loc = chain.txindex.get(txid)
    if not loc: raise HTTPException(404, "no such tx")
    h, pos = loc
    # the tx dict carries its own "height" (the coinbase commitment, 0 on an ordinary transfer), so it
    # is spread FIRST and the block's height wins. The other way round reported block 0 for every
    # transfer, spotted 2026-09-19 on a real payment.
    return {**scrub(chain.blocks[h].txs[pos].to_dict()), "status": "confirmed", "height": h, "confirmations": chain.height - h + 1}


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


def in_units(n: int) -> str:
    """A big number said the way the coin says it: "2.4 Tiaras". Empty below a Ribbon, where DONUT
    is still the honest unit."""
    for name, size in UNITS:
        if n >= size:
            v = n / size
            return f"{v:,.2f}".rstrip("0").rstrip(".") + f" {name}" + ("" if 0.995 < v < 1.005 else "s")
    return ""


def miner_record(addr: str) -> dict:
    """Everything one address has done, in a single pass: blocks found, what they were paid, when
    they started and last turned up, notes written, what they hold now, and where they rank among
    all miners. The rank is over every address ever paid for a block, so it is a real standing and
    not a slice of a top-ten list."""
    counts: dict[str, int] = {}
    mine = {"blocks": 0, "earned": 0, "first": None, "first_time": 0, "last": None, "last_time": 0, "notes": 0}
    for h, b in enumerate(chain.blocks):
        a = b.txs[0].outputs[0].address
        if a not in DEAD_ADDRESSES: counts[a] = counts.get(a, 0) + 1
        if a == addr:
            mine["blocks"] += 1; mine["earned"] += b.txs[0].outputs[0].amount
            if mine["first"] is None: mine["first"], mine["first_time"] = h, b.timestamp
            mine["last"], mine["last_time"] = h, b.timestamp
        for t in b.txs[1:]:
            if t.extra and signer_of(t) == addr and not is_hidden(t.txid, addr): mine["notes"] += 1
    better = sum(1 for v in counts.values() if v > mine["blocks"])
    total = sum(counts.values()) or 1
    return {"address": addr, "name": name_of(addr), "balance": chain.balance(addr),
            "rank": (better + 1) if mine["blocks"] else None, "of": len(counts),
            "share": round(100 * mine["blocks"] / total, 1), "height": chain.height, **mine}


@app.get("/api/who/{key}")
def who(key: str):
    """One miner, by @username or by address. The address is public either way: it is on every block
    they found. Nothing here says who they are off the chain."""
    a = resolve_who(key)
    if not a: raise HTTPException(404, "no such miner")
    return miner_record(a)


def resolve_who(key: str) -> str | None:
    """@name, name, or a D-address, to an address. One place, so the page and the API agree."""
    k = (key or "").strip().lstrip("@")
    if is_valid_address(k): return k
    if not USERNAME.match(k.lower()): return None
    c = _db(); row = c.execute("SELECT address FROM accounts WHERE username=?", (k.lower(),)).fetchone(); c.close()
    return row[0] if row else None


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
    if t.extra and chain.height + 1 >= NOTE_RULES_HEIGHT:
        who = signer_of(t)
        if not name_of(who): raise HTTPException(400, "a note needs a named author: claim or register a @username for this address first")
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
    # Where the block came from, by Cloudflare's geolocation of the submitter: country, and region once
    # the "visitor location" transform is on. Journal only, region and country only, never the address,
    # never the API, never the chain (2026-09-21). Absent on gossip from a peer, so those stay silent.
    country = request.headers.get("cf-ipcountry")
    if country:
        region = request.headers.get("cf-region")
        print(f"block {h} accepted from {region + ', ' if region else ''}{country}, tag {b.txs[0].extra}", flush=True)
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


@app.post("/api/account/claim", status_code=201)
def account_claim(body: dict, request: Request):
    """A key claims a username by signature, no password: for machines and wallet files, like
    Donut's own notes wallet. The name resolves in the phone book and labels blocks and notes, but
    it cannot log in to the web wallet (there is no blob and no auth), so nothing to steal here."""
    _limit(request, 20)
    u, addr, pub, sig = str(body.get("username", "")).lstrip("@").lower(), str(body.get("address", "")), str(body.get("pubkey", "")), str(body.get("signature", ""))
    if not USERNAME.match(u): raise HTTPException(400, "username: 3-24 of a-z 0-9 _")
    if not is_valid_address(addr): raise HTTPException(400, "bad address")
    try:
        if pubkey_to_address(bytes.fromhex(pub)) != addr: raise HTTPException(400, "key does not match address")
    except ValueError: raise HTTPException(400, "bad pubkey")
    if not verify(pub, claim_digest(u, addr), sig): raise HTTPException(400, "bad signature")
    c = _db()
    try:
        c.execute("INSERT INTO accounts (username, auth_hash, blob, address, created, updated) VALUES (?,?,?,?,?,?)", (u, "claimed", "", addr, int(time.time()), int(time.time()))); c.commit()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(409, "that name is taken")
    c.close(); return {"username": u, "address": addr}


def claim_digest(username: str, address: str) -> bytes:
    return sha256d(json.dumps({"claim": username, "address": address}, sort_keys=True, separators=(",", ":")).encode())


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
    return {"registered": push.count(username.lower()), "alerts": push.alerts(username.lower())}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: dict, request: Request):
    _limit(request, 30); return _m(push.unsubscribe, body)


_seen_miners: set | None = None


def notify_receipts() -> None:
    """Coins arriving (not mined) at an address with a subscribed account."""
    global _notified_height, _seen_miners
    if _seen_miners is None: _seen_miners = {chain.blocks[i].txs[0].outputs[0].address for i in range(0, _notified_height + 1)}
    for h in range(_notified_height + 1, chain.height + 1):
        payee = chain.blocks[h].txs[0].outputs[0].address
        if payee not in _seen_miners:                                        # someone's first block
            _seen_miners.add(payee); nm = name_of(payee)
            push.notify_alerts("A new miner", f"{'@' + nm if nm else payee[:12] + '…'} found their first block, {h}.", "/")
        for t in chain.blocks[h].txs:
            if t.coinbase: continue
            if t.extra:
                who = signer_of(t); nm = name_of(who)
                if nm and not is_hidden(t.txid, who): push.notify_alerts(f"@{nm} said", t.extra[:140], f"/note/{t.txid}")
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


@app.post("/api/ops/alarm")
def ops_alarm(body: dict, request: Request):
    """The watchers' way to reach the operator's phone: keyed peers only, sent to the accounts named
    in the request. Nothing about the alarm is stored or shown on the site (2026-09-25)."""
    if not keyed(request): raise HTTPException(403, "peers only")
    to = [str(u).lower().lstrip("@") for u in body.get("to", []) if u][:5]
    title, text = str(body.get("title", "Donut Coin"))[:60], str(body.get("body", ""))[:300]
    return {"sent": sum(push.notify(u, title, text, "/") for u in to)}


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
            # One bad pass must never end the loop. A peer that goes down mid-download (the other node
            # restarting, say) used to raise out of here, and the task died silently: the node then
            # never synced from that peer again until it was restarted itself (2026-09-25).
            for p in list(peers):
                try:
                    await asyncio.to_thread(sync_from, p)
                    await asyncio.to_thread(pull_mempool, p)
                except Exception as e:
                    print("peer unreachable this pass, trying again in 15 s", flush=True)   # no exception name: "URLError" contains the watch's word "error"
            try: await asyncio.to_thread(market.watch)
            except Exception as e: print(f"market watch: {e}", flush=True)
            try: await asyncio.to_thread(notify_receipts)
            except Exception as e: print(f"push receipts: {e}", flush=True)
            await asyncio.sleep(15)
    asyncio.create_task(loop())


def main():
    import uvicorn
    # One line on startup. uvicorn runs at warning level, so without this a container logs nothing
    # at all and the only way to tell a healthy node from a wedged one is to curl it (2026-09-19).
    print(f"donutcoin {__version__}: http://0.0.0.0:{PORT}  data {DATA}  height {chain.height}  "
          f"peers {len(peers)}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__": main()
