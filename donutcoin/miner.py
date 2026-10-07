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


def worker(idx: int, tag: str, jobs, job_id, stop, counter, found):
    """One long-lived hashing process. It never exits between blocks: the main loop publishes each new
    template on `jobs` and bumps `job_id`, and the worker switches over within 500 hashes. Workers
    used to be killed and re-forked at every block and every two-minute refresh, which left the CPU
    idle for seconds at a time; on a well-cooled Donut the fans chased every gap (2026-09-26)."""
    mine, prefix, target, block, nonce, n = -1, b"", -1, None, 0, 0
    while True:
        # Shared flags are cross-process locks: read them only between batches of 500 hashes (or while
        # idle), never per hash, or twenty workers queue on the lock and hash like one (2026-09-17).
        if stop.is_set(): return
        if job_id.value != mine:                          # a newer job: take the latest one queued
            jid, tmpl = jobs.get()
            while not jobs.empty(): jid, tmpl = jobs.get()
            txs = [Tx.from_dict(t) for t in tmpl["txs"]]
            txs[0].extra = f"{tag}/{idx}/{os.getpid()}"
            block = Block(tmpl["prev_hash"], tmpl["timestamp"], tmpl["bits"], 0, txs)
            target, prefix, mine = bits_to_target(tmpl["bits"]), block.header()[:76], jid   # all but the nonce, once
            nonce = idx * 1_000_003
        if target < 0:                                    # nothing to do (no job yet, or this one solved)
            time.sleep(0.05); continue
        for _ in range(500):
            h = scrypt_hash(prefix + (nonce & 0xFFFFFFFF).to_bytes(4, "little"))
            if int.from_bytes(h, "big") <= target:
                block.nonce = nonce & 0xFFFFFFFF
                found.put((mine, block.to_dict())); target = -1
                nonce += 1; break
            nonce += 1
        with counter.get_lock(): counter.value += 500


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
    ctx = mp.get_context()                                 # the platform default, as before: spawn on macOS and Windows
    stop, job_id, counter, found = ctx.Event(), ctx.Value("q", -1), ctx.Value("q", 0), ctx.Queue()
    queues = [ctx.Queue() for _ in range(a.threads)]
    spawn = lambda i: ctx.Process(target=worker, args=(i, a.tag, queues[i], job_id, stop, counter, found), daemon=True)
    procs = [spawn(i) for i in range(a.threads)]
    for p in procs: p.start()
    def _term(*_): raise KeyboardInterrupt                    # launchd and systemd stop with SIGTERM: same clean exit as Ctrl-C
    signal.signal(signal.SIGTERM, _term)
    try:
        _mine_forever(a, payee, procs, spawn, queues, job_id, counter, found)
    except KeyboardInterrupt:
        stop.set()
        for pr in procs: pr.join(timeout=3)
        for pr in procs:
            if pr.is_alive(): pr.kill()
        print("miner stopped; workers stopped with it", flush=True)


# A template is refreshed well before it is two minutes old, so its timestamp stays close to the
# clock; this used to be each worker quitting at 120 s, taking all twenty down together.
REFRESH_S = 60


def _mine_forever(a, payee, procs, spawn, queues, job_id, counter, found):
    blocks, jid, tmpl, t_tmpl, last = 0, -1, None, 0.0, time.time()
    def publish(t):
        nonlocal jid, tmpl, t_tmpl
        jid, tmpl, t_tmpl = jid + 1, t, time.time()
        for q in queues: q.put((jid, t))
        job_id.value = jid                                 # after the puts, so a worker never waits on an empty queue
    while True:
        if tmpl is None or time.time() - t_tmpl >= REFRESH_S:
            try: publish(get(f"{a.node}/api/template?address={payee}"))
            except Exception as e:
                print(f"node unreachable: {e}", flush=True); time.sleep(5); continue
        time.sleep(1)
        for i, p in enumerate(procs):                      # a worker that died is replaced, not mourned
            if not p.is_alive():
                procs[i] = spawn(i); procs[i].start()
                if tmpl is not None: queues[i].put((jid, tmpl))
        while not found.empty():
            fid, b = found.get()
            if fid != jid: continue                        # solved an old job: that height is gone
            try:
                r = post(f"{a.node}/api/block", b); blocks += 1
                print(f"*** block {tmpl['height']} found: {b['hash'][:16]}... -> {r.get('status')}", flush=True)
            except Exception as e:
                print(f"submit failed: {e}", flush=True)
            tmpl = None                                    # fetch the next height at once
        if time.time() - last >= 10:
            with counter.get_lock(): hashed, counter.value = counter.value, 0
            print(f"{hashed / (time.time() - last):8.0f} H/s | height {tmpl['height'] if tmpl else '?'} | blocks {blocks}", flush=True)
            last = time.time()
        if tmpl is None: continue
        try:
            info = get(f"{a.node}/api/info")
            if info["height"] >= tmpl["height"]: tmpl = None                         # someone else found this height
            elif info["mempool"] != len(tmpl["txs"]) - 1 and time.time() - t_tmpl > 5: tmpl = None   # new transactions: rebuild
        except Exception: pass

if __name__ == "__main__": main()
