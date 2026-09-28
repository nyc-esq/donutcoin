"""Blocks and the chain: validation, the UTXO set, difficulty retargeting, persistence.

Consensus, Doge-flavoured: one-minute blocks, Scrypt proof of work, a flat 10,000 DONUT reward
that never halves (supply is uncapped, like Doge), coinbase spendable after 3 blocks, difficulty
retargeted every 10 blocks and clamped to a 4x move either way.
"""
import json, os, struct, threading, time
from statistics import median
from .crypto import sha256d, scrypt_hash, bits_to_target, target_to_bits, work_from_bits, MAX_TARGET, is_valid_address
from .tx import Tx, TxOut, COIN, coinbase_tx, RIBBON

BLOCK_REWARD = 10_000 * COIN
TARGET_SPACING = 60
RETARGET_INTERVAL = 10
MAX_ADJUST = 4
# Mined coins must not be spendable until the block that created them is beyond undoing. A reorg
# puts ordinary payments back in the mempool, because the coins they spend existed before the
# fork; it *destroys* a coinbase, so anything spending one becomes permanently invalid and the
# person who was paid simply loses it. Maturity of 3 against a reorg cap of 20 left a 17-block
# hole, and the tip and the sweep both spend coinbases. 21 is not a guess: MAX_REORG_DEPTH is a
# hard refusal, so 20 is the deepest anything can be undone and 21 is past it. It is also the
# number the market, the explorer and the notes already call final. Dogecoin uses 30 at the same
# block time, but it has no reorg cap to derive from. (2026-09-19)
COINBASE_MATURITY = 3                     # before MATURITY_RULES_HEIGHT; 26 historical spends rely on it
COINBASE_MATURITY_FINAL = 21
MATURITY_RULES_HEIGHT = int(os.environ.get("DONUTCOIN_MATURITY_RULES_HEIGHT", "3444"))


def maturity_at(height: int) -> int:
    """The maturity rule in force at a height. Old blocks must keep validating under the old one:
    26 coinbases were legitimately spent younger than 21 before this rule existed."""
    return COINBASE_MATURITY_FINAL if height >= MATURITY_RULES_HEIGHT else COINBASE_MATURITY
MAX_BLOCK_TXS = 500
# Nothing bounded a block's real size: 500 transactions of unlimited inputs each meant unlimited
# bytes on disk (one JSONL line per block) and unlimited work to verify, since every input costs
# an ECDSA check at about 1.5 ms. These caps bound both. They are tightenings, so an older node
# still accepts every block a new one does, and no block in the chain so far comes close: the
# largest transaction ever seen here has 489 inputs (a sweep) and the busiest block 492 in and
# out together. A block at the cap takes about 4.5 s to verify, which is the ceiling being
# bought. Raising any of these later is a hard fork; lowering is not. (2026-09-19)
MAX_TX_INPUTS = 2000
MAX_TX_OUTPUTS = 500
MAX_BLOCK_IO = 3000                       # inputs plus outputs across every transaction in a block

# Policy, not consensus. These are what THIS node chooses to relay and to build; nothing here is
# a rule, a block breaking them is still valid, and anyone may change them in their own copy
# without a fork. Dogecoin's shape, at its own numbers: it builds blocks at 75% of the consensus
# cap and refuses dust below 0.01 DOGE, with the same 60 s block time and the same 10,000 reward.
# The point of the block policy is not tidiness. With no fee pressure, a spammer needs no hash
# power: they post fat transactions and honest miners write them into the chain for free. A
# policy cap makes them mine their own spam. (2026-09-19)
POLICY_BLOCK_IO = 3 * MAX_BLOCK_IO // 4   # 2250: Dogecoin builds at 750 kB against a 1 MB rule
POLICY_DUST = COIN // 100                 # 0.01 DONUT, Dogecoin's dust limit unchanged
POLICY_MEMPOOL_TXS = 5000                 # Dogecoin bounds by megabytes; counting does the same job
MAX_NOTE_CHARS = 280                      # a transaction's note: a tweet. The block gives it a time, the signature an author.
MAX_TAG_CHARS = 64                        # the coinbase tag a miner writes
# Notes, from NOTE_RULES_HEIGHT on (blocks before it keep the old, looser rule). Decided 2026-09-17:
# one note per block; a licence earned by mining; hold a Ribbon to speak; a fee worth 1% of a block.
NOTE_RULES_HEIGHT = int(os.environ.get("DONUTCOIN_NOTE_RULES_HEIGHT", "560"))
NOTE_LICENCE_BLOCKS = 10_080              # the signer must have received a block reward this recently (about a week)
NOTE_STAKE = RIBBON                       # and hold at least a Ribbon at that height
NOTE_MIN_FEE = 100 * COIN                 # and pay at least 100 DONUT
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
#   both nodes agree on; ./add-checkpoint.sh does it and refuses if the nodes disagree.
#   MAX_REORG_DEPTH caps how many of our own blocks a longer chain may replace between checkpoints. A legitimate
#   reorg is one or two blocks (two miners found a block at once); anything deeper is an attack or a broken node.
#   The market should ask for more confirmations than this before treating a payment as final.
# Empty under EASY_POW: that is a different chain (see above); the tests set their own.
CHECKPOINTS: dict[int, str] = {} if EASY_POW else {
    60: "5c25a3ccb347316d23fd7e57b9d738d3d1fa121c091e30e2bc79c194c3d40c6b",   # 2026-09-16 22:54 EDT; donut and the public node agree
    3397: "1c352b39c861b67bab73d8fc111a4455d8cade55a3b74c4102653b6a0b5abf6d",   # 2026-09-19 15:49 EDT; both nodes agree
    6056: "44efe42b3eaf36815bc9808a31276b2bdfb3ea69f08f2c02b3ff896c6ad4485d",   # 2026-09-21 16:25 EDT; both nodes agree
    8307: "e755ebb7cd1061adad2f28c3d95d7690382b7e4bceeedee8bce69b18c3d31cf5",   # 2026-09-23 15:54 EDT; both nodes agree
}
MAX_REORG_DEPTH = 20
# Below the newest checkpoint, re-deriving the two cryptographic proofs buys nothing. Each block
# names its parent's hash, so genesis to a pinned block is a hash chain: tamper with any byte and
# a link breaks. The checkpoint is the authority on what that history is, and the work that
# earned it is no longer in question. A trusted load still checks every structural rule and the
# linkage, and skips only the scrypt proof of work and the signatures. Profiled on the real chain
# at height 3,457: signatures were 64% of load time, scrypt 24%. DONUTCOIN_VERIFY_ALL=1 verifies
# every block from genesis anyway, for anyone who would rather not take our word for the
# checkpoint. (2026-09-19)
VERIFY_ALL = os.environ.get("DONUTCOIN_VERIFY_ALL", "") == "1"


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
        self.reorgs: list[dict] = []                                       # every chain replacement since start: a fact worth logging, not an error (2026-09-21)
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
        pinned = 0 if VERIFY_ALL else max(CHECKPOINTS, default=0)     # settled history; see VERIFY_ALL
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    if not line.strip(): continue
                    b = Block.from_dict(json.loads(line))
                    h = len(self.blocks)
                    self._validate(b, trusted=(h <= pinned)); self._apply(b, h)
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
        # A coin already claimed by a waiting transaction is not spendable: listing it let a wallet
        # build a second spend of it, which the node then refused. The sweep did exactly that behind
        # one of @donut's own notes on 2026-09-26.
        claimed = {(i.txid, i.index) for t in list(self.mempool.values()) for i in t.inputs}
        out = []
        for (txid, idx), (a, addr, h, cb) in self.utxo.items():
            if addr != address or (txid, idx) in claimed: continue
            if cb and self.height - h < maturity_at(self.height + 1): continue
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
    def _validate_tx(self, tx: Tx, view: dict, height: int, trusted: bool = False) -> int:
        """Returns the fee. `view` is the UTXO set as seen at this point in the block."""
        if tx.coinbase: raise ValidationError("unexpected coinbase")
        if not tx.inputs or not tx.outputs: raise ValidationError("empty tx")
        if len(tx.extra) > MAX_NOTE_CHARS: raise ValidationError(f"note over {MAX_NOTE_CHARS} characters")
        if len(tx.inputs) > MAX_TX_INPUTS: raise ValidationError(f"over {MAX_TX_INPUTS} inputs")
        if len(tx.outputs) > MAX_TX_OUTPUTS: raise ValidationError(f"over {MAX_TX_OUTPUTS} outputs")
        if len({(i.txid, i.index) for i in tx.inputs}) != len(tx.inputs): raise ValidationError("duplicate input")
        total_in, owners = 0, []
        for i in tx.inputs:
            u = view.get((i.txid, i.index))
            if u is None: raise ValidationError(f"input {i.txid[:12]}:{i.index} not found or spent")
            amount, owner, h, cb = u
            if cb and height - h < maturity_at(height): raise ValidationError("coinbase not mature")
            total_in += amount; owners.append(owner)
        total_out = 0
        for o in tx.outputs:
            if o.amount <= 0 or not is_valid_address(o.address): raise ValidationError("bad output")
            total_out += o.amount
        if total_out > total_in: raise ValidationError("outputs exceed inputs")
        if not trusted and not tx.signatures_valid(owners): raise ValidationError("bad signature")
        fee = total_in - total_out
        if tx.extra and height >= NOTE_RULES_HEIGHT: self._validate_note(tx, owners, view, fee, height)
        return total_in - total_out

    def _validate_note(self, tx: Tx, owners: list, view: dict, fee: int, height: int) -> None:
        """A note may be written by one address that found a block in the last NOTE_LICENCE_BLOCKS,
        holds NOTE_STAKE at this height, and pays NOTE_MIN_FEE. Named authorship is the public
        node's rule, not the chain's: names live on the site."""
        signer = owners[0]
        if any(o != signer for o in owners): raise ValidationError("a note is signed by one address")
        if fee < NOTE_MIN_FEE: raise ValidationError(f"a note pays at least {NOTE_MIN_FEE // COIN} DONUT")
        lo = max(1, height - NOTE_LICENCE_BLOCKS)
        if not any(self.blocks[h].txs[0].outputs[0].address == signer for h in range(height - 1, lo - 1, -1)):
            raise ValidationError("no licence: the signer has not found a block in the last week")
        if sum(a for (a, owner, _, _) in view.values() if owner == signer) < NOTE_STAKE:
            raise ValidationError("the signer holds less than a Ribbon")

    def _validate(self, block: Block, view: dict | None = None, trusted: bool = False) -> None:
        if block.prev_hash != self.tip.hash: raise ValidationError("does not extend the tip")
        if block.bits != self.next_bits(): raise ValidationError(f"wrong bits {block.bits:#x}, expected {self.next_bits():#x}")
        if not trusted and not block.meets_target(): raise ValidationError("proof of work below target")
        if block.timestamp <= self.median_time_past(): raise ValidationError("timestamp too early")
        if block.timestamp > time.time() + MAX_FUTURE: raise ValidationError("timestamp too far in the future")
        if not block.txs or not block.txs[0].coinbase: raise ValidationError("first tx must be coinbase")
        if len(block.txs) > MAX_BLOCK_TXS: raise ValidationError("too many txs")
        if sum(len(t.inputs) + len(t.outputs) for t in block.txs) > MAX_BLOCK_IO:
            raise ValidationError(f"over {MAX_BLOCK_IO} inputs and outputs in one block")
        height = self.height + 1
        if height in CHECKPOINTS and block.hash != CHECKPOINTS[height]: raise ValidationError(f"checkpoint mismatch at height {height}")
        cb = block.txs[0]
        if cb.height != height or cb.inputs or len(cb.outputs) != 1: raise ValidationError("bad coinbase")
        if len(cb.extra) > MAX_TAG_CHARS: raise ValidationError("coinbase tag too long")
        txids = [t.txid for t in block.txs]
        if len(set(txids)) != len(txids): raise ValidationError("duplicate txid")
        view = dict(self.utxo) if view is None else view
        fees = 0
        if height >= NOTE_RULES_HEIGHT and sum(1 for t in block.txs[1:] if t.extra) > 1: raise ValidationError("one note per block")
        for tx in block.txs[1:]:
            fees += self._validate_tx(tx, view, height, trusted)
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
        """Policy, not consensus: a block containing any of what is refused here is still valid.
        This node simply will not relay it or mine it."""
        with self.lock:
            if tx.txid in self.mempool or tx.txid in self.txindex: raise ValidationError("already known")
            if len(self.mempool) >= POLICY_MEMPOOL_TXS: raise ValidationError("mempool full, try again in a block")
            view = dict(self.utxo)
            for m in self.mempool.values():
                for i in m.inputs: view.pop((i.txid, i.index), None)
            self._validate_tx(tx, view, self.height + 1)          # consensus first, so its errors are the ones reported
            if any(o.amount < POLICY_DUST for o in tx.outputs):   # then policy
                raise ValidationError(f"output below the dust limit of {POLICY_DUST / COIN:g} DONUT")
            self.mempool[tx.txid] = tx
            return tx.txid

    def template(self, address: str, extra: str = "") -> dict:
        """What a miner needs: everything but the nonce (and the coinbase extra, which is its to vary)."""
        with self.lock:
            height = self.height + 1
            view, chosen, fees, io = dict(self.utxo), [], 0, 1      # io counts the coinbase's one output
            noted = False
            for tx in list(self.mempool.values()):
                if len(chosen) >= MAX_BLOCK_TXS - 1 or io >= POLICY_BLOCK_IO - 1: break
                if tx.extra and noted and height >= NOTE_RULES_HEIGHT: continue         # one note per block; this one waits
                if len(tx.inputs) + len(tx.outputs) + io > POLICY_BLOCK_IO: continue   # would not fit; a later, smaller one might
                try:
                    fees += self._validate_tx(tx, view, height)
                    if tx.extra: noted = True
                    io += len(tx.inputs) + len(tx.outputs)
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
            depth, added = self.height - shared, len(blocks) - shared
            # Only a replacement that discards blocks we had is a reorg. Depth 0 is a node that was
            # behind catching up (after a reboot, say): nothing lost, so no alarm word (2026-09-25).
            if depth:
                print(f"reorg: {depth} block(s) replaced from height {shared + 1}, {added} adopted", flush=True)
                self.reorgs.append({"at": int(time.time()), "height": shared + 1, "depth": depth, "added": added})
            else:
                print(f"caught up: {added} block(s) adopted from height {shared + 1}", flush=True)
            old_mempool = self.mempool
            self.blocks, self.utxo, self.index, self.txindex, self.mempool = scratch.blocks, scratch.utxo, scratch.index, scratch.txindex, {}
            self._rewrite()
            for tx in old_mempool.values():
                try: self.add_to_mempool(tx)
                except ValidationError: pass
            return True
