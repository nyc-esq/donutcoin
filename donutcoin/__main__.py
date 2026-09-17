"""One command for everything: `donutcoin start` mines into your account or a wallet file; the
others are the parts.  python -m donutcoin start|miner|wallet|node ..."""
import multiprocessing as mp, os, sys

NODE = os.environ.get("DONUTCOIN_NODE", "https://donutcoin.meme")


def start(argv):
    """Guided. Rewards go to an account on the wallet site (`--to @name`, asked for the first time and
    remembered in ~/.donutcoin/start.json) or to a local wallet file, created if there is none."""
    from .wallet import PATH, main as wallet_main
    import json
    cfg = os.path.join(os.path.dirname(PATH), "start.json")
    to, threads = os.environ.get("DONUTCOIN_TO", "").strip(), max(1, mp.cpu_count() - 2)
    for i, a in enumerate(argv):
        if a == "--threads": threads = int(argv[i + 1])
        if a == "--to": to = argv[i + 1].strip()
    if not to and os.path.exists(cfg):
        try: to = json.load(open(cfg)).get("to", "")
        except Exception: to = ""
    if not to and not os.path.exists(PATH) and sys.stdin.isatty():
        print(f"Have an account on {NODE}/wallet? Type its @username and every block you find lands in it.\n"
              f"Press Enter instead to create a wallet file on this computer (a separate wallet).")
        to = input("mine to: ").strip()
        if to and not to.startswith("@") and not to.startswith("D"): to = "@" + to
    if to:
        os.makedirs(os.path.dirname(cfg), exist_ok=True); json.dump({"to": to}, open(cfg, "w"))
        addr, where = to, f"(remembered in {cfg}; run with --to @other to change)"
    else:
        if not os.path.exists(PATH):
            sys.argv = ["wallet", "new"]; wallet_main()
            print(f"\nA new wallet was created at {PATH}. Back that file up: it is your coins.\n")
        addr, where = json.load(open(PATH))["address"], f"(the wallet file {PATH})"
    print(f"Mining Donut Coin to {addr} {where} with {threads} threads against {NODE}. Ctrl-C stops. Watch the explorer at {NODE}\n")
    from .miner import main as miner_main
    tag = os.environ.get("DONUTCOIN_TAG") or (addr[1:] if addr.startswith("@") else "donut")
    sys.argv = ["miner", "--node", NODE, "--address", addr, "--threads", str(threads), "--tag", tag]
    miner_main()


def main():
    mp.freeze_support()                       # PyInstaller on Windows
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    rest = sys.argv[2:]
    if cmd in ("-h", "--help", "help") or "--help" in rest or "-h" in rest: cmd = "usage"
    if cmd == "start": return start(rest)
    if cmd == "miner":
        from .miner import main as m; sys.argv = ["miner"] + rest; return m()
    if cmd == "wallet":
        from .wallet import main as m; sys.argv = ["wallet"] + rest; return m()
    if cmd == "node":
        from .node import main as m; sys.argv = ["node"] + rest; return m()
    print("usage: donutcoin start [--to @username|D-address] [--threads N] | miner ... | wallet new|address|balance|send ... | node"); sys.exit(2)


if __name__ == "__main__": main()
