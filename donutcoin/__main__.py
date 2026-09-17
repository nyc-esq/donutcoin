"""One command for everything: `donutcoin start` sets up a wallet and mines; the others are the
parts.  python -m donutcoin start|miner|wallet|node ..."""
import multiprocessing as mp, os, sys

NODE = os.environ.get("DONUTCOIN_NODE", "https://donutcoin.meme")


def start(argv):
    """Guided: make a wallet if there is none, show the address, mine to it."""
    from .wallet import PATH, main as wallet_main
    import json
    if not os.path.exists(PATH):
        sys.argv = ["wallet", "new"]; wallet_main()
        print(f"\nA new wallet was created at {PATH}. Back that file up: it is your coins.\n")
    addr = json.load(open(PATH))["address"]
    threads = max(1, mp.cpu_count() - 2)
    for i, a in enumerate(argv):
        if a == "--threads": threads = int(argv[i + 1])
    print(f"Mining Donut Coin to {addr} with {threads} threads against {NODE}. Ctrl-C stops. Watch the explorer at {NODE}\n")
    from .miner import main as miner_main
    sys.argv = ["miner", "--node", NODE, "--address", addr, "--threads", str(threads), "--tag", os.environ.get("DONUTCOIN_TAG", "donut")]
    miner_main()


def main():
    mp.freeze_support()                       # PyInstaller on Windows
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    rest = sys.argv[2:]
    if cmd == "start": return start(rest)
    if cmd == "miner":
        from .miner import main as m; sys.argv = ["miner"] + rest; return m()
    if cmd == "wallet":
        from .wallet import main as m; sys.argv = ["wallet"] + rest; return m()
    if cmd == "node":
        from .node import main as m; sys.argv = ["node"] + rest; return m()
    print("usage: donutcoin start [--threads N] | miner ... | wallet new|address|balance|send ... | node"); sys.exit(2)


if __name__ == "__main__": main()
