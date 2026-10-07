"""Push: subscriptions are signed by the account's key; market events and receipts notify."""
import os, json, hashlib, time
from donutcoin.market import SETTLE_CONFIRMATIONS
import pytest
from fastapi.testclient import TestClient
from ecdsa import SigningKey, SECP256k1
from ecdsa.util import sigencode_string
from donutcoin.crypto import new_keypair, sha256d
from donutcoin.tx import Tx, TxIn, TxOut, COIN
from donutcoin.chain import Block, COINBASE_MATURITY


def signed(priv, pub, payload):
    payload = {**payload, "ts": int(time.time())}
    d = sha256d(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return {"payload": payload, "pubkey": pub, "signature": SigningKey.from_string(bytes.fromhex(priv), curve=SECP256k1).sign_digest_deterministic(d, hashfunc=hashlib.sha256, sigencode=sigencode_string).hex()}


def mine_to(node, address, n=1):
    for _ in range(n):
        t = node.chain.template(address, "t"); b = Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [Tx.from_dict(x) for x in t["txs"]])
        while not b.meets_target(): b.nonce += 1
        node.chain.add_block(b)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DONUTCOIN_DATA", str(tmp_path)); monkeypatch.setenv("DONUTCOIN_SECRET", "s")
    import importlib, donutcoin.node as node; importlib.reload(node)
    sent = []
    node.push.sender = lambda subscription_info, data, **kw: sent.append((subscription_info["endpoint"], json.loads(data)))
    node.push._keys = lambda: {"private_pem": "x", "public_key": "y"}
    c = TestClient(node.app); seller, buyer = new_keypair(), new_keypair()
    for name, k in (("seller", seller), ("buyer", buyer)):
        c.post("/api/account/register", json={"username": name, "auth": "a" * 64, "blob": "x" * 60, "address": k[2]})
    mine_to(node, seller[2], COINBASE_MATURITY + 1)
    return c, node, seller, buyer, sent


def test_subscribe_requires_the_account_key(env):
    c, node, seller, buyer, sent = env
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    assert c.post("/api/push/subscribe", json=signed(buyer[0], buyer[1], {"username": "seller", "subscription": sub})).status_code == 400   # wrong key
    assert c.post("/api/push/subscribe", json=signed(seller[0], seller[1], {"username": "seller", "subscription": {"endpoint": "http://nope"}})).status_code == 400
    assert c.post("/api/push/subscribe", json=signed(seller[0], seller[1], {"username": "seller", "subscription": sub})).status_code == 200
    assert c.get("/api/push/key").json()["key"] == "y"


def test_market_events_and_receipts_notify(env):
    c, node, seller, buyer, sent = env
    for name, k in (("seller", seller), ("buyer", buyer)):
        c.post("/api/push/subscribe", json=signed(k[0], k[1], {"username": name, "subscription": {"endpoint": f"https://push.example/{name}", "keys": {"p256dh": "k", "auth": "a"}}}))
    oid = c.post("/api/market/offer", json=signed(seller[0], seller[1], {"seller": seller[2], "amount": 100 * COIN, "price_cents": 500, "method": "apple_cash", "contact": "+1 555 010 0100"})).json()["id"]
    c.post(f"/api/market/offer/{oid}/take", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]}))
    assert sent[-1][0].endswith("/seller") and "taken" in sent[-1][1]["title"]
    c.post(f"/api/market/offer/{oid}/paid", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]}))
    assert "paid" in sent[-1][1]["title"]
    u = node.chain.spendable(seller[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(100 * COIN, buyer[2]), TxOut(u["amount"] - 100 * COIN - 1000, seller[2])]); tx.sign_all(seller[0], seller[1])
    node.chain.add_to_mempool(tx); mine_to(node, new_keypair()[2], SETTLE_CONFIRMATIONS)   # settled once past the reorg cap
    node.market.watch(); node.notify_receipts()
    titles = [t for _, m in sent for t in [m["title"]]]
    assert titles.count("Trade complete") == 2 and "DONUT received" in titles
    # a coinbase to a subscribed address does not notify (that would be one buzz a minute for a miner)
    before = len(sent); mine_to(node, seller[2]); node.notify_receipts(); assert len(sent) == before


def test_service_worker_served_at_root(env):
    c = env[0]; r = c.get("/sw.js"); assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript") and "push" in r.text


def test_status_and_test_send(env):
    c, node, seller, buyer, sent = env
    assert c.get("/api/push/status/seller").json()["registered"] == 0
    c.post("/api/push/subscribe", json=signed(seller[0], seller[1], {"username": "seller", "subscription": {"endpoint": "https://push.example/s", "keys": {"p256dh": "k", "auth": "a"}}}))
    assert c.get("/api/push/status/seller").json()["registered"] == 1
    assert c.post("/api/push/test", json=signed(buyer[0], buyer[1], {"username": "seller"})).status_code == 400      # someone else's key
    r = c.post("/api/push/test", json=signed(seller[0], seller[1], {"username": "seller"})); assert r.json() == {"sent": 1, "registered": 1} and "Princess Donut" in sent[-1][1]["body"]


def test_vapid_key_is_passed_as_an_object_not_a_pem_string(tmp_path):
    """pywebpush's string path calls Vapid.from_string, which cannot read the PKCS8 PEM that
    py_vapid writes. Every send failed with "Could not deserialize key data" before any network
    call. Whatever is handed over must be something pywebpush can actually use."""
    import json
    from donutcoin.push import Push, Vapid
    if Vapid is None:
        import pytest; pytest.skip("py_vapid not installed")

    p = Push(str(tmp_path), str(tmp_path / "accounts.db"))
    pem = json.load(open(p.vapid_file))["private_pem"]

    # the bug, reproduced: the string path is the one that breaks
    with __import__("pytest").raises(ValueError):
        Vapid.from_string(pem)

    # what we hand to pywebpush instead
    v = p._vapid()
    assert v is not None and isinstance(v, Vapid)
    assert not isinstance(v, str)
