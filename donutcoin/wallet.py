"""A wallet is one key pair in a file only you can read. The node never sees the private key;
this command builds and signs the transaction locally and hands the node the signed result.

  python -m donutcoin.wallet new                  # creates ~/.donutcoin/wallet.json (0600)
  python -m donutcoin.wallet address
  python -m donutcoin.wallet balance [--node URL]
  python -m donutcoin.wallet send --to D... --amount 12.5 [--fee 0.01] [--node URL]
  python -m donutcoin.wallet note "up to 280 characters"      # written into the chain, timestamped by its block
"""
import argparse, json, os, sys, urllib.request
from .crypto import new_keypair, is_valid_address, sign
from .tx import Tx, TxIn, TxOut, COIN

PATH = os.path.expanduser(os.environ.get("DONUTCOIN_WALLET", "~/.donutcoin/wallet.json"))


def load():
    if not os.path.exists(PATH): sys.exit(f"no wallet at {PATH}; run: wallet new")
    return json.load(open(PATH))


UA = {"User-Agent": "donutcoin/0.1 (+https://donutcoin.meme)"}   # Cloudflare refuses Python's default agent


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r: return json.load(r)


def fmt(sprinkles: int) -> str:
    return f"{sprinkles / COIN:,.8f}".rstrip("0").rstrip(".") + " DONUT"


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new"); sub.add_parser("address")
    b = sub.add_parser("balance"); b.add_argument("--node", default="http://127.0.0.1:8555")
    s = sub.add_parser("send"); s.add_argument("--to", required=True); s.add_argument("--amount", type=float, required=True)
    s.add_argument("--fee", type=float, default=0.01); s.add_argument("--node", default="http://127.0.0.1:8555")
    n = sub.add_parser("note", help="write up to 280 characters into the chain (1 DONUT to yourself carries it; the fee is 100 DONUT; needs a licence, a Ribbon and a @name)")
    n.add_argument("text"); n.add_argument("--fee", type=float, default=100.0); n.add_argument("--node", default="http://127.0.0.1:8555")
    cl = sub.add_parser("claim", help="claim a @username for this wallet's key, by signature (no password, no web login)")
    cl.add_argument("name"); cl.add_argument("--node", default="http://127.0.0.1:8555")
    a = ap.parse_args()
    if a.cmd == "new":
        if os.path.exists(PATH): sys.exit(f"{PATH} exists; move it first if you really want a new one")
        priv, pub, addr = new_keypair()
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "w") as f: json.dump({"private_key": priv, "public_key": pub, "address": addr}, f, indent=2)
        os.chmod(PATH, 0o600); print(addr); return
    w = load()
    if a.cmd == "address": print(w["address"]); return
    if a.cmd == "claim":
        import hashlib
        name = a.name.lstrip("@").lower()
        payload = json.dumps({"claim": name, "address": w["address"]}, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
        body = {"username": name, "address": w["address"], "pubkey": w["public_key"], "signature": sign(w["private_key"], digest)}
        req = urllib.request.Request(f"{a.node}/api/account/claim", method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as r: print(f"@{json.load(r)['username']} is now {w['address']}"); return
        except urllib.error.HTTPError as e: sys.exit(f"claim refused: {e.read().decode()[:200]}")
    if a.cmd == "balance":
        d = get(f"{a.node}/api/address/{w['address']}")
        print(f"{w['address']}\n balance   {fmt(d['balance'])}\n spendable {fmt(sum(u['amount'] for u in d['spendable']))}  ({len(d['spendable'])} outputs)"); return
    if a.cmd == "note":
        if len(a.text) > 280: sys.exit(f"note is {len(a.text)} characters; the chain takes 280")
        if not a.text.strip(): sys.exit("empty note")
        a.to, a.amount, a.extra = w["address"], 1.0, a.text
    if a.cmd in ("send", "note"):
        if a.to.startswith("@") or not is_valid_address(a.to):
            try: r = get(f"{a.node}/api/account/address/{a.to.lstrip('@')}"); print(f"@{r['username']} is {r['address']}"); a.to = r["address"]
            except Exception: sys.exit("bad address: give a D… address or a known @username")
        amount, fee = int(round(a.amount * COIN)), int(round(a.fee * COIN))
        utxos = sorted(get(f"{a.node}/api/address/{w['address']}")["spendable"], key=lambda u: -u["amount"])
        chosen, total = [], 0
        for u in utxos:
            chosen.append(u); total += u["amount"]
            if total >= amount + fee: break
        if total < amount + fee: sys.exit(f"insufficient spendable balance: {fmt(total)}")
        outs = [TxOut(amount, a.to)]
        if total - amount - fee > 0: outs.append(TxOut(total - amount - fee, w["address"]))
        tx = Tx(inputs=[TxIn(u["txid"], u["index"]) for u in chosen], outputs=outs, extra=getattr(a, "extra", ""))
        tx.sign_all(w["private_key"], w["public_key"])
        req = urllib.request.Request(f"{a.node}/api/tx", method="POST", data=json.dumps(tx.to_dict()).encode(), headers={"Content-Type": "application/json", **UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as r: print("noted" if a.cmd == "note" else "sent", json.load(r)["txid"])
        except urllib.error.HTTPError as e: sys.exit(f"{a.cmd} refused: {e.read().decode()[:200]}")


if __name__ == "__main__": main()
