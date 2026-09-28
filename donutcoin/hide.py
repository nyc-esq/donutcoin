"""The hide list, for the node's operator: notes this node will not show. The chain keeps them;
nothing can remove a note from the chain. Takes effect within 30 s, no restart.

  python -m donutcoin.hide add <txid|D-address> "reason"
  python -m donutcoin.hide remove <txid|D-address>
  python -m donutcoin.hide list
"""
import json, os, sys, time

DATA = os.path.expanduser(os.environ.get("DONUTCOIN_DATA", "~/.donutcoin"))
PATH = os.path.join(DATA, "hidden.json")


def load() -> dict:
    try:
        with open(PATH) as f: d = json.load(f)
    except (OSError, ValueError): d = {}
    return {"txids": dict(d.get("txids", {})), "addresses": dict(d.get("addresses", {}))}


def save(d: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def main():
    a = sys.argv[1:]
    d = load()
    if a[:1] == ["list"]:
        for k, v in d["txids"].items(): print(f"tx      {k}  {v}")
        for k, v in d["addresses"].items(): print(f"address {k}  {v}")
        if not d["txids"] and not d["addresses"]: print("nothing hidden")
        return
    if len(a) >= 2 and a[0] in ("add", "remove"):
        key = a[1]; kind = "txids" if len(key) == 64 else "addresses"
        if a[0] == "add":
            d[kind][key] = f"{time.strftime('%Y-%m-%d')}: {' '.join(a[2:]) or 'no reason given'}"
        else: d[kind].pop(key, None)
        save(d); print(f"{a[0]}: {kind[:-1] if kind == 'txids' else 'address'} {key}; {len(d['txids'])} tx and {len(d['addresses'])} address entries"); return
    print(__doc__); sys.exit(2)


if __name__ == "__main__": main()
