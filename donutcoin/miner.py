"""The miner: every worker builds its own block from the node's template (its own coinbase tag
makes its own merkle root), then walks the nonce. First past the target submits. Threads are real
processes because Scrypt in Python releases nothing worth sharing.

  python -m donutcoin.miner --node http://127.0.0.1:8555 --address D... --threads 8 --tag donut
"""
import argparse, json, multiprocessing as mp, os, signal, sys, time, urllib.request
from .chain import Block
from .tx import Tx
from .crypto import bits_to_target, scrypt_hash


UA = {"User-Agent": "donutcoin/0.1 (+https://donutcoin.meme)"}   # Cloudflare refuses Python's default agent


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r: return json.load(r)


def post(url, body):
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Donut-Key": os.environ.get("DONUTCOIN_SECRET", ""), **UA})
    with urllib.request.urlopen(req, timeout=10) as r: return json.load(r)


def worker(idx: int, tmpl: dict, tag: str, stop, counter, found):
    txs = [Tx.from_dict(t) for t in tmpl["txs"]]
    txs[0].extra = f"{tag}/{idx}/{os.getpid()}"
    block = Block(tmpl["prev_hash"], tmpl["timestamp"], tmpl["bits"], 0, txs)
    target = bits_to_target(tmpl["bits"])
    prefix = block.header()[:76]           # everything but the nonce, computed once
    nonce, n = idx * 1_000_003, 0
    while True:
        h = scrypt_hash(prefix + (nonce & 0xFFFFFFFF).to_bytes(4, "little"))
        n += 1
        if int.from_bytes(h, "big") <= target:
            block.nonce = nonce & 0xFFFFFFFF
            found.put(block.to_dict()); stop.set(); break
        nonce += 1
        if n % 500 == 0:
            # the shared flag and counter are cross-process locks: touch them every 500 hashes, not
            # every hash, or the workers serialize on the lock and 20 threads hash like one (2026-09-17)
            with counter.get_lock(): counter.value += 500
            if stop.is_set() or time.time() - tmpl["timestamp"] > 120: break


def resolve_address(node: str, address: str) -> str:
    """`@name` becomes the address of that account on the wallet site, looked up on the node (the same
    phone-book endpoint the wallet uses to pay by username). A D-address passes through."""
    if not address.startswith("@"): return address
    try:
        return get(f"{node}/api/account/address/{address[1:]}")["address"]
    except Exception as e:
        sys.exit(f"no account named {address} on {node} ({e}). Create one at {node}/wallet, or pass a D-address.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="http://127.0.0.1:8555"); ap.add_argument("--address", required=True, help="a D-address, or @username of an account on the wallet site")
    ap.add_argument("--threads", type=int, default=max(1, mp.cpu_count() // 2)); ap.add_argument("--tag", default=None, help="label written into your blocks (default: the @username, else 'donut')")
    a = ap.parse_args()
    payee = resolve_address(a.node, a.address)
    a.tag = a.tag or (a.address[1:] if a.address.startswith("@") else "donut")
    print(f"donutcoin miner: {a.threads} workers -> {a.node}, paying {a.address}" + (f" ({payee})" if payee != a.address else ""), flush=True)
    total, t0, blocks = 0, time.time(), 0
    live = {"procs": [], "stop": None}
    def _term(*_): raise KeyboardInterrupt                    # launchd and systemd stop with SIGTERM: same clean exit as Ctrl-C
    signal.signal(signal.SIGTERM, _term)
    try:
        _mine_forever(a, payee, total, blocks, live)
    except KeyboardInterrupt:
        if live["stop"] is not None: live["stop"].set()
        for pr in live["procs"]: pr.join(timeout=3)
        for pr in live["procs"]:
            if pr.is_alive(): pr.kill()
        print("miner stopped; workers stopped with it", flush=True)


def _mine_forever(a, payee, total, blocks, live):
    while True:
        try:
            tmpl = get(f"{a.node}/api/template?address={payee}")
        except Exception as e:
            print(f"node unreachable: {e}", flush=True); time.sleep(5); continue
        stop, counter, found = mp.Event(), mp.Value("q", 0), mp.Queue()
        t_tmpl = time.time()
        procs = [mp.Process(target=worker, args=(i, tmpl, a.tag, stop, counter, found), daemon=True) for i in range(a.threads)]
        live["procs"], live["stop"] = procs, stop
        for p in procs: p.start()
        last = time.time()
        while any(p.is_alive() for p in procs):
            time.sleep(1)
            if not found.empty(): break
            if time.time() - last >= 10:
                rate = counter.value / (time.time() - last); total += counter.value
                with counter.get_lock(): counter.value = 0
                last = time.time()
                print(f"{rate:8.0f} H/s | height {tmpl['height']} | difficulty target {tmpl['target'][:12]}... | blocks {blocks}", flush=True)
            try:
                info = get(f"{a.node}/api/info")
                if info["height"] >= tmpl["height"]:
                    stop.set(); break                     # someone else found this height
                if info["mempool"] != len(tmpl["txs"]) - 1 and time.time() - t_tmpl > 5:
                    stop.set(); break                     # new transactions waiting: rebuild the block
            except Exception: pass
        stop.set()
        if not found.empty():
            b = found.get()
            try:
                r = post(f"{a.node}/api/block", b); blocks += 1
                print(f"*** block {b.get('height', tmpl['height'])} found: {b['hash'][:16]}... -> {r.get('status')}", flush=True)
            except Exception as e:
                print(f"submit failed: {e}", flush=True)
        for p in procs: p.join(timeout=2)


if __name__ == "__main__": main()
