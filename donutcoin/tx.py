"""Transactions: a UTXO model like Bitcoin and Dogecoin. Coins live in unspent outputs; a
transaction consumes some outputs (proving ownership with a signature over the transaction id)
and creates new ones. The coinbase transaction has no inputs and mints the block reward."""
import json
from dataclasses import dataclass, field, asdict
from .crypto import sha256d, sign, verify, pubkey_to_address

COIN = 100_000_000          # sprinkles per DONUT
# Units, base 10, sized to the network's issuance (10,000 DONUT a block, one block a minute); 2026-09-17, "Ladder B":
# The ladder, base 10: what Princess Donut wins, what she wears, what she rules under, and who she
# is. It stops at the Queen because she already said so in block 0, and because a crown is an object
# while a queen is the person wearing it. Labels only; nothing in consensus depends on them, with one
# exception: NOTE_STAKE in chain.py borrows the Ribbon as the balance a note's author must hold.
RIBBON = 1_000_000 * COIN        # 100 blocks, about 1 h 40 of the whole network
TIARA  = 10 * RIBBON             # about a day
CROWN  = 10 * TIARA              # about a week
QUEEN  = 10 * CROWN              # about ten weeks
UNITS = [("Queen", QUEEN), ("Crown", CROWN), ("Tiara", TIARA), ("Ribbon", RIBBON)]


@dataclass
class TxOut:
    amount: int             # sprinkles
    address: str


@dataclass
class TxIn:
    txid: str
    index: int
    pubkey: str = ""
    signature: str = ""


@dataclass
class Tx:
    inputs: list = field(default_factory=list)      # [TxIn]
    outputs: list = field(default_factory=list)     # [TxOut]
    coinbase: bool = False
    height: int = 0                                 # coinbase only: makes every coinbase unique
    extra: str = ""                                 # coinbase: the miner's tag / extra nonce

    def signing_payload(self) -> dict:
        return {"inputs": [[i.txid, i.index] for i in self.inputs],
                "outputs": [[o.amount, o.address] for o in self.outputs],
                "coinbase": self.coinbase, "height": self.height, "extra": self.extra}

    def digest(self) -> bytes:
        return sha256d(json.dumps(self.signing_payload(), separators=(",", ":")).encode())

    @property
    def txid(self) -> str:
        return self.digest().hex()

    def sign_all(self, priv_hex: str, pub_hex: str) -> None:
        d = self.digest()
        for i in self.inputs:
            i.pubkey = pub_hex; i.signature = sign(priv_hex, d)

    def signatures_valid(self, spent_addresses: list[str]) -> bool:
        """spent_addresses[i] is the address that owns input i's output."""
        d = self.digest()
        for i, owner in zip(self.inputs, spent_addresses):
            if not i.pubkey or pubkey_to_address(bytes.fromhex(i.pubkey)) != owner: return False
            if not verify(i.pubkey, d, i.signature): return False
        return True

    def to_dict(self) -> dict:
        return {"txid": self.txid, "inputs": [asdict(i) for i in self.inputs],
                "outputs": [asdict(o) for o in self.outputs], "coinbase": self.coinbase,
                "height": self.height, "extra": self.extra}

    @classmethod
    def from_dict(cls, d: dict) -> "Tx":
        return cls(inputs=[TxIn(**i) for i in d.get("inputs", [])],
                   outputs=[TxOut(**o) for o in d.get("outputs", [])],
                   coinbase=bool(d.get("coinbase")), height=int(d.get("height", 0)), extra=str(d.get("extra", "")))


def coinbase_tx(height: int, address: str, amount: int, extra: str = "") -> Tx:
    return Tx(inputs=[], outputs=[TxOut(amount, address)], coinbase=True, height=height, extra=extra)
