"""Blocks and the chain: validation, the UTXO set, difficulty retargeting, persistence.

Consensus, Doge-flavoured: one-minute blocks, Scrypt proof of work, a flat 10,000 DONUT reward
that never halves (supply is uncapped, like Doge), coinbase spendable after 3 blocks, difficulty
retargeted every 10 blocks and clamped to a 4x move either way.
"""
import json, os, struct, threading, time
from statistics import median
from .crypto import sha256d, scrypt_hash, bits_to_target, target_to_bits, work_from_bits, MAX_TARGET, is_valid_address
from .tx import Tx, TxOut, COIN, coinbase_tx

BLOCK_REWARD = 10_000 * COIN
TARGET_SPACING = 60
RETARGET_INTERVAL = 10
MAX_ADJUST = 4
COINBASE_MATURITY = 3
MAX_BLOCK_TXS = 500
MAX_FUTURE = 2 * 3600
GENESIS_TIME = 1789610400                      # 2026-09-17 02:00:00 UTC (2026-09-16 22:00 EDT)
GENESIS_ADDRESS = "DonutCoinGenesisCatInATiaraAndSunglassesXXX"   # unspendable on purpose
# Addresses whose coins can never move, so they are dead weight, not holders: the genesis coinbase
# (unspendable by design) and throwaway test wallets whose keys were discarded on 2026-09-17.
# Excluded from the front page's top-holders table; still counted in the supply, because the coins exist.
DEAD_ADDRESSES = {
    "DonutCoinGenesisCatInATiaraAndSunglassesXXX",   # block 0, unspendable
    "DPBE1oHpqje7S2j1BhMmtWmgimS5pouXhD",             # the accidental miner; those rewards were burned (key gone)
    "DDjUxNRD6H8ddRxezdUjapsrhsG4qApaUD",             # first live wallet test, key discarded
    "D8bQdmeaALXK1U52Q5VnWZv1xaMiZFkUfc",             # early trade test, key discarded
    "DRzpgFzibm7J6rTMdMKP8XXAUi1yTnJfFo",             # the first mining wallet, swept and retired on launch day
}
GENESIS_EXTRA = "Princess Donut, The Queen, named herself via askdonut.ai, 2026-09-16: \"It will rise to power and then collapse in a way that makes sense, just as I do.\""
GENESIS_NONCE = 1483506
# Test mode: DONUTCOIN_EASY_POW=1 makes every target trivial so the adversarial tests can mine
# hundreds of blocks in seconds. It changes the genesis hash, so it is a different chain - a node
# started this way can never sync with the real one, which is the point.
EASY_POW = os.environ.get("DONUTCOIN_EASY_POW") == "1"
MAX_TARGET_EFFECTIVE = (1 << 255) if EASY_POW else MAX_TARGET

# ---- Checkpoints and the reorg cap (2026-09-16). The operator: "ideally I'd want other people to mine, but not if it
# destroys the entire chain." Two rules that make outside miners safe to admit:
#   CHECKPOINTS pins a height to its block id. A block arriving at that height with another id is invalid, and a
#   competing chain that forks at or below our newest checkpoint is refused however much work it carries - so
#   nothing at or below a checkpoint can ever be rewritten. Add one per deploy from a block a few hours old that
#   both nodes agree on (curl <node>/api/block/<height>); both nodes must carry the same list.
#   MAX_REORG_DEPTH caps how many of our own blocks a longer chain may replace between checkpoints. A legitimate
#   reorg is one or two blocks (two miners found a block at once); anything deeper is an attack or a broken node.
#   The market should ask for more confirmations than this before treating a payment as final.
# Empty under EASY_POW: that is a different chain (see above); the tests set their own.
CHECKPOINTS: dict[int, str] = {} if EASY_POW else {
    60: "5c25a3ccb347316d23fd7e57b9d738d3d1fa121c091e30e2bc79c194c3d40c6b",   # 2026-09-16 22:54 EDT; donut and the public node agree
}
MAX_REORG_DEPTH = 20


class Block:
    def __init__(self, prev_hash: str, timestamp: int, bits: int, nonce: int, txs: list[Tx], version: int = 1):
        self.version, self.prev_hash, self.timestamp, self.bits, self.nonce, self.txs = version, prev_hash, timestamp, bits, nonce, txs

    @staticmethod
    def merkle_root(txids: list[str]) -> str:
        layer = [bytes.fromhex(t) for t in txids] or [b"\0" * 32]
        while len(layer) > 1:
            if len(layer) % 2: layer.append(layer[-1])
            layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
        return layer[0].hex()

    @property
    def merkle(self) -> str:
        return self.merkle_root([t.txid for t in self.txs])

    def header(self, nonce: int | None = None) -> bytes:
        """80 bytes: version, prev hash, merkle root, time, bits, nonce - Bitcoin's layout."""
        return struct.pack("<I", self.version) + bytes.fromhex(self.prev_hash) + bytes.fromhex(self.merkle) \
            + struct.pack("<III", self.timestamp, self.bits, self.nonce if nonce is None else nonce)

    @property
    def hash(self) -> str:            # block id (sha256d), what the explorer shows
        return sha256d(self.header()).hex()

    @property
    def pow_hash(self) -> str:        # what must fall under the target
        return scrypt_hash(self.header()).hex()

    def meets_target(self) -> bool:
        return int(self.pow_hash, 16) <= bits_to_target(self.bits)

    def to_dict(self, height: int | None = None) -> dict:
        d = {"hash": self.hash, "version": self.version, "prev_hash": self.prev_hash, "merkle": self.merkle,
             "timestamp": self.timestamp, "bits": self.bits, "nonce": self.nonce, "txs": [t.to_dict() for t in self.txs]}
        if height is not None: d["height"] = height
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(d["prev_hash"], int(d["timestamp"]), int(d["bits"]), int(d["nonce"]),
                   [Tx.from_dict(t) for t in d["txs"]], int(d.get("version", 1)))


def genesis_block() -> Block:
    cb = coinbase_tx(0, GENESIS_ADDRESS, BLOCK_REWARD, GENESIS_EXTRA)
    b = Block("0" * 64, GENESIS_TIME, target_to_bits(MAX_TARGET_EFFECTIVE), GENESIS_NONCE, [cb])
    if EASY_POW:
        b.nonce = 0
        while not b.meets_target(): b.nonce += 1
    return b


def mine_genesis() -> int:
    b = genesis_block(); n = 0
    while True:
        b.nonce = n
        if b.meets_target(): return n
        n += 1


class ValidationError(Exception):
    pass


class Chain:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir; os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "chain.jsonl")
        self.lock = threading.RLock()
        self.blocks: list[Block] = []
        self.utxo: dict[tuple[str, int], tuple[int, str, int, bool]] = {}   # (txid, idx) -> (amount, address, height, coinbase)
        self.mempool: dict[str, Tx] = {}
        self.index: dict[str, int] = {}                                    # block hash -> height
        self.txindex: dict[str, tuple[int, int]] = {}                      # txid -> (height, position)
        self._load()

    # ---- state
    def _apply(self, block: Block, height: int) -> None:
        for pos, tx in enumerate(block.txs):
            for i in tx.inputs: self.utxo.pop((i.txid, i.index), None)
            for n, o in enumerate(tx.outputs): self.utxo[(tx.txid, n)] = (o.amount, o.address, height, tx.coinbase)
            self.txindex[tx.txid] = (height, pos)
            self.mempool.pop(tx.txid, None)
        self.index[block.hash] = height
        self.blocks.append(block)

    def _load(self) -> None:
        g = genesis_block()
        if not g.meets_target(): raise SystemExit("genesis nonce not set: run mine_genesis()")
        self._apply(g, 0)
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    if not line.strip(): continue
                    b = Block.from_dict(json.loads(line))
                    self._validate(b); self._apply(b, len(self.blocks))
        # drop mempool state on start; it is not persisted

    def _persist(self, block: Block) -> None:
        with open(self.path, "a") as f: f.write(json.dumps(block.to_dict()) + "\n")

    def _rewrite(self) -> None:
        with open(self.path + ".tmp", "w") as f:
            for b in self.blocks[1:]: f.write(json.dumps(b.to_dict()) + "\n")
        os.replace(self.path + ".tmp", self.path)

    # ---- queries
    @property
    def height(self) -> int: return len(self.blocks) - 1
    @property
    def tip(self) -> Block: return self.blocks[-1]

    def work(self) -> int: return sum(work_from_bits(b.bits) for b in self.blocks)

    def supply(self) -> int: return sum(a for (a, _, _, _) in self.utxo.values())

    def balance(self, address: str) -> int:
        return sum(a for (a, addr, _, _) in self.utxo.values() if addr == address)

    def spendable(self, address: str) -> list[dict]:
        out = []
        for (txid, idx), (a, addr, h, cb) in self.utxo.items():
            if addr != address: continue
            if cb and self.height - h < COINBASE_MATURITY: continue
            out.append({"txid": txid, "index": idx, "amount": a, "height": h})
        return out

    def median_time_past(self, n: int = 11) -> int:
        return int(median(b.timestamp for b in self.blocks[-n:]))

    def next_bits(self) -> int:
        h = self.height + 1
        if h % RETARGET_INTERVAL != 0 or h < RETARGET_INTERVAL:
            return self.tip.bits
        first = self.blocks[h - RETARGET_INTERVAL]
        actual = max(self.tip.timestamp - first.timestamp, 1)
        expected = TARGET_SPACING * (RETARGET_INTERVAL - 1)
        actual = min(max(actual, expected // MAX_ADJUST), expected * MAX_ADJUST)
        target = bits_to_target(self.tip.bits) * actual // expected
        return target_to_bits(min(target, MAX_TARGET_EFFECTIVE))

    # ---- validation
    def _validate_tx(self, tx: Tx, view: dict, height: int) -> int:
        """Returns the fee. `view` is the UTXO set as seen at this point in the block."""
        if tx.coinbase: raise ValidationError("unexpected coinbase")
        if not tx.inputs or not tx.outputs: raise ValidationError("empty tx")
        if len({(i.txid, i.index) for i in tx.inputs}) != len(tx.inputs): raise ValidationError("duplicate input")
        total_in, owners = 0, []
        for i in tx.inputs:
            u = view.get((i.txid, i.index))
            if u is None: raise ValidationError(f"input {i.txid[:12]}:{i.index} not found or spent")
            amount, owner, h, cb = u
            if cb and height - h < COINBASE_MATURITY: raise ValidationError("coinbase not mature")
            total_in += amount; owners.append(owner)
        total_out = 0
        for o in tx.outputs:
            if o.amount <= 0 or not is_valid_address(o.address): raise ValidationError("bad output")
            total_out += o.amount
        if total_out > total_in: raise ValidationError("outputs exceed inputs")
        if not tx.signatures_valid(owners): raise ValidationError("bad signature")
        return total_in - total_out

    def _validate(self, block: Block, view: dict | None = None) -> None:
        if block.prev_hash != self.tip.hash: raise ValidationError("does not extend the tip")
        if block.bits != self.next_bits(): raise ValidationError(f"wrong bits {block.bits:#x}, expected {self.next_bits():#x}")
        if not block.meets_target(): raise ValidationError("proof of work below target")
        if block.timestamp <= self.median_time_past(): raise ValidationError("timestamp too early")
        if block.timestamp > time.time() + MAX_FUTURE: raise ValidationError("timestamp too far in the future")
        if not block.txs or not block.txs[0].coinbase: raise ValidationError("first tx must be coinbase")
        if len(block.txs) > MAX_BLOCK_TXS: raise ValidationError("too many txs")
        height = self.height + 1
        if height in CHECKPOINTS and block.hash != CHECKPOINTS[height]: raise ValidationError(f"checkpoint mismatch at height {height}")
        cb = block.txs[0]
        if cb.height != height or cb.inputs or len(cb.outputs) != 1: raise ValidationError("bad coinbase")
        txids = [t.txid for t in block.txs]
        if len(set(txids)) != len(txids): raise ValidationError("duplicate txid")
        view = dict(self.utxo) if view is None else view
        fees = 0
        for tx in block.txs[1:]:
            fees += self._validate_tx(tx, view, height)
            for i in tx.inputs: view.pop((i.txid, i.index))
            for n, o in enumerate(tx.outputs): view[(tx.txid, n)] = (o.amount, o.address, height, False)
        if cb.outputs[0].amount > BLOCK_REWARD + fees: raise ValidationError("coinbase overpays")

    # ---- mutation
    def add_block(self, block: Block) -> int:
        with self.lock:
            self._validate(block)
            self._apply(block, self.height + 1)
            self._persist(block)
            return self.height

    def add_to_mempool(self, tx: Tx) -> str:
        with self.lock:
            if tx.txid in self.mempool or tx.txid in self.txindex: raise ValidationError("already known")
            view = dict(self.utxo)
            for m in self.mempool.values():
                for i in m.inputs: view.pop((i.txid, i.index), None)
            self._validate_tx(tx, view, self.height + 1)
            self.mempool[tx.txid] = tx
            return tx.txid

    def template(self, address: str, extra: str = "") -> dict:
        """What a miner needs: everything but the nonce (and the coinbase extra, which is its to vary)."""
        with self.lock:
            height = self.height + 1
            view, chosen, fees = dict(self.utxo), [], 0
            for tx in list(self.mempool.values())[:MAX_BLOCK_TXS - 1]:
                try:
                    fees += self._validate_tx(tx, view, height)
                    for i in tx.inputs: view.pop((i.txid, i.index))
                    for n, o in enumerate(tx.outputs): view[(tx.txid, n)] = (o.amount, o.address, height, False)
                    chosen.append(tx)
                except ValidationError:
                    self.mempool.pop(tx.txid, None)
            cb = coinbase_tx(height, address, BLOCK_REWARD + fees, extra)
            return {"height": height, "prev_hash": self.tip.hash, "bits": self.next_bits(),
                    "target": f"{bits_to_target(self.next_bits()):064x}",
                    "timestamp": max(int(time.time()), self.median_time_past() + 1),
                    "txs": [cb.to_dict()] + [t.to_dict() for t in chosen]}

    def replace_with(self, blocks: list[Block]) -> bool:
        """Adopt another node's chain if it carries more work. Rebuilt from genesis in a scratch
        chain so a bad block anywhere rejects the whole thing; small chain, cheap to do."""
        with self.lock:
            # Where does the candidate leave our chain? (blocks[i] is height i+1.) Refuse before rebuilding if it
            # would rewrite at or below our newest checkpoint, or discard more of our blocks than the cap allows.
            shared = 0
            while shared < min(len(blocks), self.height) and blocks[shared].hash == self.blocks[shared + 1].hash: shared += 1
            pinned = max((h for h in CHECKPOINTS if h <= self.height), default=0)
            if shared < pinned: raise ValidationError(f"forks at height {shared}, below checkpoint {pinned}")
            if self.height - shared > MAX_REORG_DEPTH: raise ValidationError(f"reorg of {self.height - shared} blocks exceeds the cap of {MAX_REORG_DEPTH}")
            scratch = Chain.__new__(Chain)
            scratch.lock = threading.RLock(); scratch.blocks, scratch.utxo, scratch.mempool, scratch.index, scratch.txindex = [], {}, {}, {}, {}
            scratch._apply(genesis_block(), 0)
            for b in blocks:
                scratch._validate(b); scratch._apply(b, scratch.height + 1)
            if scratch.work() <= self.work(): return False
            old_mempool = self.mempool
            self.blocks, self.utxo, self.index, self.txindex, self.mempool = scratch.blocks, scratch.utxo, scratch.index, scratch.txindex, {}
            self._rewrite()
            for tx in old_mempool.values():
                try: self.add_to_mempool(tx)
                except ValidationError: pass
            return True
