"""A whole Apple Cash trade through the API: offer, take, paid, the seller sends, the watcher
completes it; plus the things that must be refused."""
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
    sig = SigningKey.from_string(bytes.fromhex(priv), curve=SECP256k1).sign_digest_deterministic(d, hashfunc=hashlib.sha256, sigencode=sigencode_string).hex()
    return {"payload": payload, "pubkey": pub, "signature": sig}


def mine_to(node, address, n=1):
    for _ in range(n):
        t = node.chain.template(address, "t"); b = Block(t["prev_hash"], t["timestamp"], t["bits"], 0, [Tx.from_dict(x) for x in t["txs"]])
        while not b.meets_target(): b.nonce += 1
        node.chain.add_block(b)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DONUTCOIN_DATA", str(tmp_path)); monkeypatch.setenv("DONUTCOIN_SECRET", "s")
    import importlib, donutcoin.node as node; importlib.reload(node)
    seller, buyer = new_keypair(), new_keypair()
    mine_to(node, seller[2], COINBASE_MATURITY + 1)
    return TestClient(node.app), node, seller, buyer


def test_full_trade(env):
    c, node, seller, buyer = env
    offer = {"seller": seller[2], "amount": 500 * COIN, "price_cents": 1000, "method": "apple_cash", "contact": "+1 555 0100", "note": "first trade"}
    r = c.post("/api/market/offer", json=signed(seller[0], seller[1], offer)); assert r.status_code == 201, r.text; oid = r.json()["id"]
    board = c.get("/api/market").json()["offers"]; assert board[0]["id"] == oid and board[0]["contact"] is None and board[0]["seller_can_cover"]
    assert c.post(f"/api/market/offer/{oid}/take", json=signed(seller[0], seller[1], {"offer": oid, "buyer": seller[2]})).status_code == 400   # own offer
    t = c.post(f"/api/market/offer/{oid}/take", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]})); assert t.status_code == 200, t.text
    assert t.json()["contact"] == "+1 555 0100" and t.json()["code"].startswith("DC-")
    assert c.get(f"/api/market?address={buyer[2]}").json()["mine"][0]["contact"] == "+1 555 0100"       # a party sees the contact
    assert c.get(f"/api/market?address={new_keypair()[2]}").json()["offers"] == []                        # taken offers leave the board
    assert c.post(f"/api/market/offer/{oid}/paid", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]})).status_code == 200
    # the seller cannot cancel once the buyer says they paid
    assert c.post(f"/api/market/offer/{oid}/cancel", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2]})).status_code == 400
    # the seller sends the coins from their own wallet; the watcher completes the trade
    u = node.chain.spendable(seller[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(500 * COIN, buyer[2]), TxOut(u["amount"] - 500 * COIN - 1000, seller[2])]); tx.sign_all(seller[0], seller[1])
    node.chain.add_to_mempool(tx); mine_to(node, seller[2])
    assert node.market.watch() == 0                                  # one confirmation: not settled yet
    mine_to(node, seller[2], SETTLE_CONFIRMATIONS - 2); assert node.market.watch() == 0   # one short of the line
    mine_to(node, seller[2]); assert node.market.watch() == 1        # SETTLE_CONFIRMATIONS deep: complete
    mine = c.get(f"/api/market?address={seller[2]}").json()["mine"][0]; assert mine["status"] == "complete" and mine["complete_txid"] == tx.txid
    assert c.get(f"/api/market/rep/{seller[2]}").json() == {"sold": 1, "bought": 0} and c.get(f"/api/market/rep/{buyer[2]}").json() == {"sold": 0, "bought": 1}


def test_refusals(env):
    c, node, seller, buyer = env
    base = {"seller": seller[2], "amount": 500 * COIN, "price_cents": 1000, "method": "apple_cash", "contact": "+1 555 0100"}
    assert c.post("/api/market/offer", json=signed(buyer[0], buyer[1], base)).status_code == 400                          # signed by someone else
    assert c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "amount": 10 ** 12 * COIN})).status_code == 400   # cannot cover / out of range
    assert c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "method": "venmo"})).status_code == 400
    stale = signed(seller[0], seller[1], base); stale["payload"]["ts"] -= 3600
    assert c.post("/api/market/offer", json=stale).status_code == 400                                                   # stale or tampered
    r = c.post("/api/market/offer", json=signed(seller[0], seller[1], base)); oid = r.json()["id"]
    assert c.post(f"/api/market/offer/{oid}/paid", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]})).status_code == 400   # not taken yet
    assert c.post(f"/api/market/offer/{oid}/cancel", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2]})).status_code == 200
    assert c.get("/api/market").json()["offers"] == []


def test_cash_app_method_and_contact_rules(env):
    c, node, seller, buyer = env
    base = {"seller": seller[2], "amount": 100 * COIN, "price_cents": 500, "method": "cash_app"}
    assert c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "contact": "bad tag!"})).status_code == 400
    r = c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "contact": "chris_w"})); assert r.status_code == 201
    t = c.post(f"/api/market/offer/{r.json()['id']}/take", json=signed(buyer[0], buyer[1], {"offer": r.json()["id"], "buyer": buyer[2]}))
    assert t.json()["contact"] == "$chris_w" and t.json()["method"] == "Cash App"
    assert c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "method": "apple_cash", "contact": "nonsense"})).status_code == 400
    assert c.post("/api/market/offer", json=signed(seller[0], seller[1], {**base, "method": "apple_cash", "contact": "+1 (555) 010-0100"})).status_code == 201


def test_pay_link_and_link_qr(env):
    c, node, seller, buyer = env
    r = c.get(f"/pay/{buyer[2]}?amount=12.5"); assert r.status_code == 200 and f"/wallet#send={buyer[2]}&amount=12.5" in r.text
    assert c.get("/pay/Dnope").status_code == 404      # reads as a username nobody has
    assert c.get("/pay/D!!!").status_code == 400        # neither an address nor a possible username
    assert c.get(f"/api/qr-link.svg?text=https://donutcoin.meme/pay/{buyer[2]}").headers["content-type"].startswith("image/svg")
    assert c.get("/api/qr-link.svg?text=https://evil.example/x").status_code == 400


def test_wanted_side_full_flow_and_price(env):
    c, node, seller, buyer = env
    # the buyer posts a request; no contact needed
    r = c.post("/api/market/offer", json=signed(buyer[0], buyer[1], {"side": "buy", "buyer": buyer[2], "amount": 200 * COIN, "price_cents": 400, "method": "apple_cash"})); assert r.status_code == 201, r.text; oid = r.json()["id"]
    b = c.get("/api/market").json(); assert b["offers"][0]["side"] == "buy" and b["offers"][0]["poster"] == buyer[2] and b["offers"][0]["seller"] is None
    # a seller takes it, bringing a contact; must be funded
    poor = new_keypair()
    assert c.post(f"/api/market/offer/{oid}/take", json=signed(poor[0], poor[1], {"offer": oid, "who": poor[2], "contact": "+1 555 010 0100"})).status_code == 400
    assert c.post(f"/api/market/offer/{oid}/take", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2], "contact": "nonsense"})).status_code == 400
    t = c.post(f"/api/market/offer/{oid}/take", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2], "contact": "+1 555 010 0100"})); assert t.status_code == 200, t.text
    mine_b = c.get(f"/api/market?address={buyer[2]}").json()["mine"][0]; assert mine_b["contact"] == "+1 555 010 0100" and mine_b["seller"] == seller[2]
    assert c.post(f"/api/market/offer/{oid}/paid", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]})).status_code == 200
    u = node.chain.spendable(seller[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(200 * COIN, buyer[2]), TxOut(u["amount"] - 200 * COIN - 1000, seller[2])]); tx.sign_all(seller[0], seller[1])
    node.chain.add_to_mempool(tx); mine_to(node, seller[2], SETTLE_CONFIRMATIONS); node.market.watch()
    p = c.get("/api/price").json(); assert p["last_trade"]["cents_per_donut"] == 2.0 and len(p["points"]) == 1
    assert c.get("/api/market").json()["price"]["last_trade"]["amount"] == 200 * COIN


def test_wanted_side_backing_out(env):
    c, node, seller, buyer = env
    oid = c.post("/api/market/offer", json=signed(buyer[0], buyer[1], {"side": "buy", "buyer": buyer[2], "amount": 100 * COIN, "price_cents": 200, "method": "cash_app"})).json()["id"]
    c.post(f"/api/market/offer/{oid}/take", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2], "contact": "tag_1"}))
    # the seller (taker) backs out -> open again without a seller; the buyer (poster) can then withdraw
    assert c.post(f"/api/market/offer/{oid}/cancel", json=signed(seller[0], seller[1], {"offer": oid, "who": seller[2]})).json()["status"] == "open"
    assert c.get("/api/market").json()["offers"][0]["seller"] is None
    assert c.post(f"/api/market/offer/{oid}/cancel", json=signed(buyer[0], buyer[1], {"offer": oid, "who": buyer[2]})).json()["status"] == "cancelled"


def test_old_schema_migrates(tmp_path):
    """A market.db from the first version (seller NOT NULL, no side) must open and accept requests."""
    import sqlite3
    from donutcoin.market import Market
    path = str(tmp_path / "market.db"); c = sqlite3.connect(path)
    c.execute("""CREATE TABLE offers (id TEXT PRIMARY KEY, seller TEXT NOT NULL, amount INTEGER NOT NULL, price_cents INTEGER NOT NULL,
        method TEXT NOT NULL, contact TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created INTEGER NOT NULL, buyer TEXT,
        taken_at INTEGER, taken_height INTEGER, paid_at INTEGER, code TEXT, complete_txid TEXT, completed_at INTEGER, cancelled_at INTEGER)""")
    c.execute("INSERT INTO offers (id, seller, amount, price_cents, method, contact, status, created) VALUES ('a', 'Dx', 1, 100, 'apple_cash', '+1', 'complete', 1)"); c.commit(); c.close()
    class FakeChain:
        height = 0; blocks = []
        def spendable(self, a): return []
    m = Market(path, FakeChain())
    c = sqlite3.connect(path); cols = {r[1]: r for r in c.execute("PRAGMA table_info(offers)")}
    assert cols["seller"][3] == 0 and "side" in cols and c.execute("SELECT side, status FROM offers WHERE id='a'").fetchone() == ("sell", "complete"); c.close()


def test_settling_annotation_counts_confirmations(env):
    c, node, seller, buyer = env
    oid = c.post("/api/market/offer", json=signed(seller[0], seller[1], {"seller": seller[2], "amount": 100 * COIN, "price_cents": 500, "method": "apple_cash", "contact": "+1 555 010 0100"})).json()["id"]
    c.post(f"/api/market/offer/{oid}/take", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]}))
    c.post(f"/api/market/offer/{oid}/paid", json=signed(buyer[0], buyer[1], {"offer": oid, "buyer": buyer[2]}))
    m = c.get(f"/api/market?address={buyer[2]}").json()["mine"][0]; assert m["settling_height"] is None and m["confirmations"] == 0 and m["settle_needed"] >= 1
    u = node.chain.spendable(seller[2])[0]
    tx = Tx(inputs=[TxIn(u["txid"], u["index"])], outputs=[TxOut(100 * COIN, buyer[2]), TxOut(u["amount"] - 100 * COIN - 1000, seller[2])]); tx.sign_all(seller[0], seller[1])
    node.chain.add_to_mempool(tx); mine_to(node, new_keypair()[2]); node.market.watch()
    m = c.get(f"/api/market?address={buyer[2]}").json()["mine"][0]; assert m["status"] == "paid" and m["settling_height"] == node.chain.height and m["confirmations"] == 1
    mine_to(node, new_keypair()[2], 2); m = c.get(f"/api/market?address={buyer[2]}").json()["mine"][0]; assert m["confirmations"] == 3
