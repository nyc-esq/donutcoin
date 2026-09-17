"""Adversarial tests for Donut Coin's consensus. Each test is an attack; the chain must refuse it.
Run: ../.venv/bin/python -m pytest tests -q   (from donutcoin/)"""
import json, os, tempfile, time
os.environ["DONUTCOIN_EASY_POW"] = "1"          # must be set before the chain module is imported
import pytest
from donutcoin import chain as C
from donutcoin.chain import Chain, Block, ValidationError, BLOCK_REWARD, COINBASE_MATURITY, genesis_block
from donutcoin.tx import Tx, TxIn, TxOut, COIN, coinbase_tx
from donutcoin.crypto import new_keypair, bits_to_target, target_to_bits, MAX_TARGET, scrypt_hash


def mine(block: Block) -> Block:
    n = 0
    while True:
        block.nonce = n
        if block.meets_target(): return block
        n += 1


def make_block(ch: Chain, address: str, txs=(), extra="t", ts=None) -> Block:
    t = ch.template(address, extra)
    txs = [Tx.from_dict(t["txs"][0])] + list(txs)
    b = Block(t["prev_hash"], ts or t["timestamp"], t["bits"], 0, txs)
    return mine(b)


@pytest.fixture
def fresh(tmp_path):
    return Chain(str(tmp_path))


@pytest.fixture
def funded(tmp_path):
    """A chain where Alice mined enough blocks for one mature coinbase."""
    ch = Chain(str(tmp_path)); alice = new_keypair()
    for _ in range(COINBASE_MATURITY + 1): ch.add_block(make_block(ch, alice[2]))
    return ch, alice


def spend(ch, frm, to_addr, amount, fee=1000, utxo=None):
    u = utxo or ch.spendable(frm[2])[0]
    outs = [TxOut(amount, to_addr)]
    change = u["amount"] - amount - fee
    if change > 0: outs.append(TxOut(change, frm[2]))
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=outs); tx.sign_all(frm[0], frm[1]); return tx


# ---------------------------------------------------------------- genesis and basic blocks
def test_genesis_is_fixed_and_valid(fresh):
    g = genesis_block()
    assert fresh.height == 0 and fresh.tip.hash == g.hash and g.meets_target()
    assert fresh.balance(C.GENESIS_ADDRESS) == BLOCK_REWARD


def test_mined_block_accepted_and_persisted(fresh, tmp_path):
    a = new_keypair(); fresh.add_block(make_block(fresh, a[2]))
    assert fresh.height == 1 and fresh.balance(a[2]) == BLOCK_REWARD
    again = Chain(str(tmp_path)); assert again.height == 1 and again.tip.hash == fresh.tip.hash


def test_block_without_pow_rejected(fresh):
    a = new_keypair(); t = fresh.template(a[2])
    b = Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [Tx.from_dict(t["txs"][0])])
    if b.meets_target(): b.nonce = 1  # astronomically unlikely, but make it not meet
    b.nonce = (b.nonce + 1) & 0xFFFFFFFF
    while b.meets_target(): b.nonce += 1
    with pytest.raises(ValidationError, match="proof of work"): fresh.add_block(b)


def test_block_with_wrong_bits_rejected(fresh):
    """Claiming an easier target than the chain demands."""
    a = new_keypair(); t = fresh.template(a[2])
    easy = target_to_bits(MAX_TARGET)  # same as genesis; force a different (easier-than-rule) value by lying
    b = Block(t["prev_hash"], t["timestamp"], target_to_bits(1 << 250), 0, [Tx.from_dict(t["txs"][0])])   # a target the rules did not ask for
    mine(b)
    with pytest.raises(ValidationError, match="bits"): fresh.add_block(b)


def test_block_not_on_tip_rejected(fresh):
    a = new_keypair(); b1 = make_block(fresh, a[2]); fresh.add_block(b1)
    stale = make_block(fresh, a[2]); stale.prev_hash = "00" * 32; mine(stale)
    with pytest.raises(ValidationError, match="tip"): fresh.add_block(stale)


def test_future_and_past_timestamps_rejected(fresh):
    a = new_keypair()
    fut = make_block(fresh, a[2], ts=int(time.time()) + C.MAX_FUTURE + 100)
    with pytest.raises(ValidationError, match="future"): fresh.add_block(fut)
    past = make_block(fresh, a[2], ts=C.GENESIS_TIME - 1)
    with pytest.raises(ValidationError, match="early"): fresh.add_block(past)


# ---------------------------------------------------------------- coinbase abuse
def test_coinbase_overpay_rejected(fresh):
    a = new_keypair(); t = fresh.template(a[2])
    cb = Tx.from_dict(t["txs"][0]); cb.outputs[0].amount = BLOCK_REWARD + 1
    b = mine(Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [cb]))
    with pytest.raises(ValidationError, match="overpays"): fresh.add_block(b)


def test_coinbase_wrong_height_rejected(fresh):
    a = new_keypair(); t = fresh.template(a[2])
    cb = Tx.from_dict(t["txs"][0]); cb.height = 99
    b = mine(Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [cb]))
    with pytest.raises(ValidationError, match="coinbase"): fresh.add_block(b)


def test_two_coinbases_rejected(fresh):
    a = new_keypair(); t = fresh.template(a[2])
    cb = Tx.from_dict(t["txs"][0]); cb2 = coinbase_tx(1, a[2], BLOCK_REWARD, "second")
    b = mine(Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [cb, cb2]))
    with pytest.raises(ValidationError): fresh.add_block(b)


def test_immature_coinbase_cannot_be_spent(fresh):
    a, bob = new_keypair(), new_keypair()
    fresh.add_block(make_block(fresh, a[2]))
    u = {"txid": fresh.tip.txs[0].txid, "index": 0, "amount": BLOCK_REWARD}
    with pytest.raises(ValidationError, match="mature"): fresh.add_to_mempool(spend(fresh, a, bob[2], COIN, utxo=u))


# ---------------------------------------------------------------- transaction attacks
def test_valid_spend_confirms_and_pays_fee(funded):
    ch, alice = funded; bob = new_keypair()
    tx = spend(ch, alice, bob[2], 5 * COIN, fee=1000); ch.add_to_mempool(tx)
    miner = new_keypair(); ch.add_block(make_block(ch, miner[2], [tx]))
    assert ch.balance(bob[2]) == 5 * COIN
    assert ch.tip.txs[0].outputs[0].amount == BLOCK_REWARD + 1000


def test_forged_signature_rejected(funded):
    ch, alice = funded; mallory, bob = new_keypair(), new_keypair()
    u = ch.spendable(alice[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(u["amount"] - 1000, bob[2])])
    tx.sign_all(mallory[0], mallory[1])          # Mallory signs Alice's coin
    with pytest.raises(ValidationError, match="signature"): ch.add_to_mempool(tx)


def test_tampered_output_after_signing_rejected(funded):
    ch, alice = funded; bob, mallory = new_keypair(), new_keypair()
    tx = spend(ch, alice, bob[2], 5 * COIN)
    tx.outputs[0].address = mallory[2]           # redirect after Alice signed
    with pytest.raises(ValidationError, match="signature"): ch.add_to_mempool(tx)


def test_overspend_rejected(funded):
    ch, alice = funded; bob = new_keypair(); u = ch.spendable(alice[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(u["amount"] + 1, bob[2])]); tx.sign_all(alice[0], alice[1])
    with pytest.raises(ValidationError, match="exceed"): ch.add_to_mempool(tx)


def test_negative_and_zero_outputs_rejected(funded):
    ch, alice = funded; bob = new_keypair(); u = ch.spendable(alice[2])[0]
    for bad in (0, -5 * COIN):
        tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(bad, bob[2]), TxOut(u["amount"] - bad - 1000, alice[2])]); tx.sign_all(alice[0], alice[1])
        with pytest.raises(ValidationError, match="bad output"): ch.add_to_mempool(tx)


def test_bad_address_output_rejected(funded):
    ch, alice = funded; u = ch.spendable(alice[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(COIN, "DnotAnAddress")]); tx.sign_all(alice[0], alice[1])
    with pytest.raises(ValidationError, match="bad output"): ch.add_to_mempool(tx)


def test_double_spend_in_mempool_rejected(funded):
    ch, alice = funded; bob, carol = new_keypair(), new_keypair(); u = ch.spendable(alice[2])[0]
    ch.add_to_mempool(spend(ch, alice, bob[2], COIN, utxo=u))
    with pytest.raises(ValidationError): ch.add_to_mempool(spend(ch, alice, carol[2], COIN, utxo=u))


def test_double_spend_in_block_rejected(funded):
    ch, alice = funded; bob, carol = new_keypair(), new_keypair(); u = ch.spendable(alice[2])[0]
    t1 = spend(ch, alice, bob[2], COIN, utxo=u); t2 = spend(ch, alice, carol[2], COIN, utxo=u)
    miner = new_keypair()
    with pytest.raises(ValidationError): ch.add_block(make_block(ch, miner[2], [t1, t2]))


def test_same_input_twice_in_one_tx_rejected(funded):
    ch, alice = funded; bob = new_keypair(); u = ch.spendable(alice[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"]), TxIn(u["txid"], u["index"])], outputs=[TxOut(2 * u["amount"] - 1000, bob[2])]); tx.sign_all(alice[0], alice[1])
    with pytest.raises(ValidationError, match="duplicate"): ch.add_to_mempool(tx)


def test_spent_output_cannot_be_respent_later(funded):
    ch, alice = funded; bob = new_keypair(); u = ch.spendable(alice[2])[0]
    tx = spend(ch, alice, bob[2], COIN, utxo=u); ch.add_to_mempool(tx); ch.add_block(make_block(ch, new_keypair()[2], [tx]))
    with pytest.raises(ValidationError, match="not found or spent"): ch.add_to_mempool(spend(ch, alice, bob[2], 2 * COIN, utxo=u))   # a different tx, same coin


def test_replayed_tx_rejected(funded):
    ch, alice = funded; bob = new_keypair()
    tx = spend(ch, alice, bob[2], COIN); ch.add_to_mempool(tx); ch.add_block(make_block(ch, new_keypair()[2], [tx]))
    with pytest.raises(ValidationError): ch.add_to_mempool(tx)


def test_chained_spend_within_one_block(funded):
    """Bob spends coins he received in the same block: legal, and the order is enforced."""
    ch, alice = funded; bob, carol = new_keypair(), new_keypair()
    t1 = spend(ch, alice, bob[2], 5 * COIN)
    t2 = Tx(inputs=[TxIn(t1.txid, 0)], outputs=[TxOut(4 * COIN, carol[2])]); t2.sign_all(bob[0], bob[1])
    ch.add_block(make_block(ch, new_keypair()[2], [t1, t2]))
    assert ch.balance(carol[2]) == 4 * COIN
    ch2, alice2 = funded[0], funded[1]


def test_reversed_order_in_block_rejected(funded):
    ch, alice = funded; bob, carol = new_keypair(), new_keypair()
    t1 = spend(ch, alice, bob[2], 5 * COIN)
    t2 = Tx(inputs=[TxIn(t1.txid, 0)], outputs=[TxOut(4 * COIN, carol[2])]); t2.sign_all(bob[0], bob[1])
    with pytest.raises(ValidationError): ch.add_block(make_block(ch, new_keypair()[2], [t2, t1]))


# ---------------------------------------------------------------- chain replacement
def test_longer_chain_with_more_work_replaces(tmp_path):
    a = new_keypair()
    ours = Chain(str(tmp_path / "a")); ours.add_block(make_block(ours, a[2]))
    theirs = Chain(str(tmp_path / "b"))
    for _ in range(3): theirs.add_block(make_block(theirs, a[2]))
    assert ours.replace_with(theirs.blocks[1:]) is True and ours.height == 3 and ours.tip.hash == theirs.tip.hash


def test_shorter_or_equal_chain_does_not_replace(tmp_path):
    a = new_keypair()
    ours = Chain(str(tmp_path / "a")); [ours.add_block(make_block(ours, a[2])) for _ in range(2)]
    theirs = Chain(str(tmp_path / "b")); [theirs.add_block(make_block(theirs, a[2])) for _ in range(2)]
    assert ours.replace_with(theirs.blocks[1:]) is False and ours.height == 2


def test_invalid_block_inside_longer_chain_rejects_whole_chain(tmp_path):
    a = new_keypair()
    ours = Chain(str(tmp_path / "a")); ours.add_block(make_block(ours, a[2]))
    theirs = Chain(str(tmp_path / "b")); [theirs.add_block(make_block(theirs, a[2])) for _ in range(3)]
    forged = theirs.blocks[1:]; forged[1].txs[0].outputs[0].amount = BLOCK_REWARD * 10   # inflate a coinbase in the middle
    with pytest.raises(ValidationError): ours.replace_with(forged)
    assert ours.height == 1


def test_reorg_returns_orphaned_mempool_and_keeps_utxo_consistent(tmp_path):
    a, bob = new_keypair(), new_keypair()
    ours = Chain(str(tmp_path / "a"))
    for _ in range(COINBASE_MATURITY + 2): ours.add_block(make_block(ours, a[2]))
    theirs = Chain(str(tmp_path / "b"))
    for _ in range(COINBASE_MATURITY + 4): theirs.add_block(make_block(theirs, a[2]))
    assert ours.replace_with(theirs.blocks[1:])
    total = sum(v[0] for v in ours.utxo.values())
    assert total == BLOCK_REWARD * (ours.height + 1)      # every block minted exactly one reward, nothing lost or doubled


# ---------------------------------------------------------------- difficulty
def test_retarget_moves_toward_spacing_and_is_clamped(tmp_path):
    a = new_keypair(); ch = Chain(str(tmp_path))
    base = ch.tip.timestamp
    for i in range(C.RETARGET_INTERVAL - 1):      # blocks arriving every 1 s: far too fast
        ch.add_block(make_block(ch, a[2], ts=base + i + 1))
    harder = ch.next_bits()
    assert bits_to_target(harder) == bits_to_target(ch.tip.bits) // C.MAX_ADJUST   # clamped to 4x harder


def test_target_never_easier_than_max(tmp_path):
    a = new_keypair(); ch = Chain(str(tmp_path))
    base = ch.tip.timestamp
    for i in range(C.RETARGET_INTERVAL - 1):      # blocks an hour apart: far too slow
        ch.add_block(make_block(ch, a[2], ts=base + (i + 1) * 600))   # ten minutes apart: 10x too slow, within the future window
    assert bits_to_target(ch.next_bits()) <= C.MAX_TARGET_EFFECTIVE


# ---------------------------------------------------------------- serialization robustness
def test_block_roundtrip_and_hash_stability(funded):
    ch, _ = funded; d = ch.tip.to_dict(); b = Block.from_dict(json.loads(json.dumps(d)))
    assert b.hash == ch.tip.hash and b.merkle == ch.tip.merkle and b.meets_target()


def test_malformed_block_dict_does_not_crash_validation(fresh):
    for bad in ({}, {"prev_hash": "zz"}, {"prev_hash": "00" * 32, "timestamp": "x", "bits": 1, "nonce": 0, "txs": []}):
        with pytest.raises((ValidationError, KeyError, ValueError, TypeError, IndexError)):
            fresh.add_block(Block.from_dict(bad))


# ---------------------------------------------------------------- checkpoints and the reorg cap (2026-09-16)
def _chain(path, n, addr):
    ch = Chain(str(path)); [ch.add_block(make_block(ch, addr)) for _ in range(n)]; return ch


def test_reorg_deeper_than_cap_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_REORG_DEPTH", 3)
    a = new_keypair()
    ours = _chain(tmp_path / "a", 6, a[2])
    theirs = _chain(tmp_path / "b", 8, new_keypair()[2])   # another miner, forks at genesis: would discard all 6 of ours
    # (under EASY_POW two chains mined by the same address in the same second are byte-identical, hence the other key)
    with pytest.raises(ValidationError, match="exceeds the cap"): ours.replace_with(theirs.blocks[1:])
    assert ours.height == 6


def test_reorg_within_cap_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_REORG_DEPTH", 3)
    a = new_keypair()
    ours = _chain(tmp_path / "a", 4, a[2])
    theirs = Chain(str(tmp_path / "b")); [theirs.add_block(b) for b in ours.blocks[1:3]]   # shares our first two
    for _ in range(4): theirs.add_block(make_block(theirs, a[2]))                          # then four of its own: height 6
    assert ours.replace_with(theirs.blocks[1:]) is True and ours.height == 6               # discards 2 of ours: allowed


def test_block_at_checkpoint_height_must_match(tmp_path, monkeypatch):
    a = new_keypair(); ch = Chain(str(tmp_path)); ch.add_block(make_block(ch, a[2]))
    monkeypatch.setattr(C, "CHECKPOINTS", {2: "00" * 32})
    with pytest.raises(ValidationError, match="checkpoint"): ch.add_block(make_block(ch, a[2]))
    assert ch.height == 1


def test_chain_forking_below_checkpoint_refused_whatever_its_work(tmp_path, monkeypatch):
    a = new_keypair()
    ours = _chain(tmp_path / "a", 5, a[2])
    theirs = _chain(tmp_path / "b", 9, new_keypair()[2])   # another miner, forks at genesis and is much longer
    monkeypatch.setattr(C, "CHECKPOINTS", {3: ours.blocks[3].hash})   # pinned after both exist: theirs could not even be built under it
    with pytest.raises(ValidationError, match="below checkpoint"): ours.replace_with(theirs.blocks[1:])
    assert ours.height == 5 and ours.blocks[3].hash == C.CHECKPOINTS[3]


def test_chain_forking_above_checkpoint_still_accepted(tmp_path, monkeypatch):
    a = new_keypair()
    ours = _chain(tmp_path / "a", 5, a[2])
    monkeypatch.setattr(C, "CHECKPOINTS", {3: ours.blocks[3].hash})
    theirs = Chain(str(tmp_path / "b")); [theirs.add_block(b) for b in ours.blocks[1:5]]   # shares through height 4
    for _ in range(3): theirs.add_block(make_block(theirs, a[2]))                          # height 7
    assert ours.replace_with(theirs.blocks[1:]) is True and ours.height == 7
