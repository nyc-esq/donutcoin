"""The account API and a browser-style (compact) signature, through the FastAPI app."""
import os, json, hashlib
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DONUTCOIN_DATA", str(tmp_path)); monkeypatch.setenv("DONUTCOIN_SECRET", "s")
    import importlib, donutcoin.node as node; importlib.reload(node)
    return TestClient(node.app), node


def test_register_login_and_no_enumeration(client):
    c, node = client
    s1 = c.get("/api/account/salt/alice").json()["salt"]; s2 = c.get("/api/account/salt/nobody_here").json()["salt"]
    assert len(s1) == 64 and len(s2) == 64 and s1 != s2
    from donutcoin.crypto import new_keypair
    priv, pub, addr = new_keypair()
    r = c.post("/api/account/register", json={"username": "alice", "auth": "a" * 64, "blob": "x" * 60, "address": addr}); assert r.status_code == 201
    assert c.post("/api/account/register", json={"username": "alice", "auth": "b" * 64, "blob": "x" * 60, "address": addr}).status_code == 409
    assert c.post("/api/account/register", json={"username": "Bad Name", "auth": "a" * 64, "blob": "x" * 60, "address": addr}).status_code == 400
    assert c.post("/api/account/register", json={"username": "bob", "auth": "a" * 64, "blob": "x" * 60, "address": "Dnope"}).status_code == 400
    ok = c.post("/api/account/login", json={"username": "alice", "auth": "a" * 64}); assert ok.status_code == 200 and ok.json()["blob"] == "x" * 60 and ok.json()["address"] == addr
    assert c.post("/api/account/login", json={"username": "alice", "auth": "b" * 64}).status_code == 401
    assert c.post("/api/account/login", json={"username": "nobody_here", "auth": "b" * 64}).status_code == 401


def test_qr_and_wallet_page(client):
    c, node = client
    from donutcoin.crypto import new_keypair
    addr = new_keypair()[2]
    assert c.get(f"/api/qr/{addr}.svg").headers["content-type"].startswith("image/svg")
    assert c.get("/api/qr/Dnope.svg").status_code == 400
    assert "Donut Coin wallet" in c.get("/wallet").text


def test_compact_signature_accepted_like_the_browser_makes_it():
    from donutcoin.crypto import new_keypair, verify, sign
    from ecdsa import SigningKey, SECP256k1
    from ecdsa.util import sigencode_string
    priv, pub, _ = new_keypair(); digest = hashlib.sha256(b"x").digest()
    compact = SigningKey.from_string(bytes.fromhex(priv), curve=SECP256k1).sign_digest_deterministic(digest, hashfunc=hashlib.sha256, sigencode=sigencode_string).hex()
    assert len(compact) == 128 and verify(pub, digest, compact) and verify(pub, digest, sign(priv, digest))
    assert not verify(pub, hashlib.sha256(b"y").digest(), compact)


def test_rate_limit_trips(client):
    c, node = client
    codes = [c.get("/api/account/salt/zz1").status_code for _ in range(61)]
    assert 429 in codes


def test_rename_and_password_change(client):
    c, node = client
    from donutcoin.crypto import new_keypair
    addr = new_keypair()[2]
    c.post("/api/account/register", json={"username": "old_name", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    assert c.post("/api/account/update", json={"username": "old_name", "auth": "b" * 64, "new_username": "new_name"}).status_code == 401
    r = c.post("/api/account/update", json={"username": "old_name", "auth": "a" * 64, "new_username": "new_name", "new_auth": "c" * 64, "new_blob": "y" * 60})
    assert r.status_code == 200 and r.json()["username"] == "new_name"
    assert c.post("/api/account/login", json={"username": "old_name", "auth": "a" * 64}).status_code == 401
    ok = c.post("/api/account/login", json={"username": "new_name", "auth": "c" * 64}); assert ok.status_code == 200 and ok.json()["blob"] == "y" * 60
    c.post("/api/account/register", json={"username": "taken", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    assert c.post("/api/account/update", json={"username": "new_name", "auth": "c" * 64, "new_username": "taken"}).status_code == 409


def test_optional_email(client):
    c, node = client
    from donutcoin.crypto import new_keypair
    addr = new_keypair()[2]
    assert c.post("/api/account/register", json={"username": "em1", "auth": "a" * 64, "blob": "x" * 60, "address": addr, "email": "not-an-email"}).status_code == 400
    assert c.post("/api/account/register", json={"username": "em1", "auth": "a" * 64, "blob": "x" * 60, "address": addr, "email": " Nerd@Example.com "}).status_code == 201
    assert c.post("/api/account/login", json={"username": "em1", "auth": "a" * 64}).json()["email"] == "nerd@example.com"
    assert c.post("/api/account/register", json={"username": "em2", "auth": "a" * 64, "blob": "x" * 60, "address": addr}).status_code == 201
    assert c.post("/api/account/login", json={"username": "em2", "auth": "a" * 64}).json()["email"] is None
    c.post("/api/account/update", json={"username": "em1", "auth": "a" * 64, "email": ""})
    assert c.post("/api/account/login", json={"username": "em1", "auth": "a" * 64}).json()["email"] is None


def test_pay_by_username(client):
    c, node = client
    from donutcoin.crypto import new_keypair
    addr = new_keypair()[2]
    c.post("/api/account/register", json={"username": "payme", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    assert c.get("/api/account/address/payme").json() == {"username": "payme", "address": addr}
    assert c.get("/api/account/address/@PayMe").json()["address"] == addr
    assert c.get("/api/account/address/nobody9").status_code == 404
    assert c.get("/api/account/address/bad name").status_code == 400
    r = c.get("/pay/@payme?amount=3"); assert r.status_code == 200 and "/wallet#send=@payme&amount=3" in r.text
    assert c.get("/pay/@nobody9").status_code == 404


def test_download_page_renders_without_github(client, monkeypatch):
    c, node = client
    monkeypatch.setattr(node, "latest_release", lambda: {"tag_name": "v9.9.9", "published_at": "2026-09-17T00:00:00Z", "assets": [{"name": "donutcoin-windows.exe", "size": 12 * 1048576, "browser_download_url": "https://example/x.exe"}]})
    r = c.get("/download"); assert r.status_code == 200 and "v9.9.9" in r.text and "/download/file/donutcoin-windows.exe" in r.text and "https://example/x.exe" not in r.text and "Not yet" in r.text
    monkeypatch.setattr(node, "latest_release", lambda: None)
    assert "not released yet" in c.get("/download").text


def test_source_and_download_redirects(client, monkeypatch):
    c, node = client
    r = c.get("/source", follow_redirects=False); assert r.status_code == 302 and "github.com" in r.headers["location"]
    r = c.get("/source/info/refs?service=git-upload-pack", follow_redirects=False); assert r.headers["location"].endswith("/info/refs?service=git-upload-pack")
    monkeypatch.setattr(node, "latest_release", lambda: {"tag_name": "v1", "assets": [{"name": "donutcoin-linux-x86_64", "size": 1, "browser_download_url": "https://example/f"}]})
    r = c.get("/download/file/donutcoin-linux-x86_64", follow_redirects=False); assert r.status_code == 302 and r.headers["location"] == "https://example/f"
    assert c.get("/download/file/nope", follow_redirects=False).status_code == 404
    # The rule is about what the page *shows*, not what it links to (2026-09-19): downloads
    # route through this site's own endpoint, and the account name is not copy on the page. An
    # href may point anywhere, which is the only way to offer a container image at all.
    import re
    visible = re.sub(r'href="[^"]*"', "", c.get("/download").text)
    assert "nyc-esq" not in visible
    assert "github.com" not in visible


def test_stats_endpoint(client):
    c, node = client
    s = c.get("/api/stats").json()
    assert len(s["hours"]) == 24 and len(s["supply_series"]) == 24 and s["supply"] >= 0 and isinstance(s["holders"], list) and "tip_time" in s


def test_dead_addresses_excluded_from_holders(client):
    c, node = client
    from donutcoin.chain import DEAD_ADDRESSES, GENESIS_ADDRESS
    assert GENESIS_ADDRESS in DEAD_ADDRESSES
    s = c.get("/api/stats").json()
    assert all(h["address"] not in DEAD_ADDRESSES for h in s["holders"])
    assert s["circulating"] <= s["supply"]


def test_miner_pays_a_username(client, monkeypatch):
    """`--address @name` on the miner resolves through the node's phone book."""
    c, node = client
    from donutcoin import miner
    from donutcoin.crypto import new_keypair
    addr = new_keypair()[2]
    c.post("/api/account/register", json={"username": "digger", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    def fake_get(url):
        r = c.get(url.replace("http://n", ""))
        if r.status_code != 200: raise RuntimeError(r.status_code)
        return r.json()
    monkeypatch.setattr(miner, "get", fake_get)
    assert miner.resolve_address("http://n", "@digger") == addr
    assert miner.resolve_address("http://n", "@Digger") == addr
    assert miner.resolve_address("http://n", addr) == addr
    with pytest.raises(SystemExit):
        miner.resolve_address("http://n", "@nobody9")


def test_notes_feed(client):
    c, node = client
    r = c.get("/api/notes"); assert r.status_code == 200 and r.json()["max_chars"] == 280 and r.json()["notes"] == []


def test_claim_username_by_signature(client):
    """A key claims a name with a signature; the name resolves, cannot log in, and cannot be taken again."""
    c, node = client
    from donutcoin.crypto import new_keypair, sign
    priv, pub, addr = new_keypair()
    sig = sign(priv, node.claim_digest("donut", addr))
    r = c.post("/api/account/claim", json={"username": "@Donut", "address": addr, "pubkey": pub, "signature": sig}); assert r.status_code == 201, r.text
    assert c.get("/api/account/address/donut").json()["address"] == addr
    assert c.post("/api/account/login", json={"username": "donut", "auth": "a" * 64}).status_code == 401
    assert c.post("/api/account/register", json={"username": "donut", "auth": "a" * 64, "blob": "x" * 60, "address": addr}).status_code == 409
    other = new_keypair()
    assert c.post("/api/account/claim", json={"username": "thief", "address": addr, "pubkey": other[1], "signature": sign(other[0], node.claim_digest("thief", addr))}).status_code == 400
    assert c.post("/api/account/claim", json={"username": "forged", "address": addr, "pubkey": pub, "signature": sign(other[0], node.claim_digest("forged", addr))}).status_code == 400


def test_note_submission_needs_a_name_and_hide_list(client, tmp_path, monkeypatch):
    c, node = client
    from donutcoin.crypto import new_keypair
    from donutcoin.tx import Tx, TxIn, TxOut
    from donutcoin import hide
    # a note from an unnamed address is refused at the door (the chain rules would refuse it later anyway)
    who = new_keypair()
    tx = Tx(inputs=[TxIn("0" * 64, 0)], outputs=[TxOut(1, who[2])], extra="hello"); tx.sign_all(who[0], who[1])
    r = c.post("/api/tx", json=tx.to_dict()); assert r.status_code == 400 and "named author" in r.text
    # the hide list is read from DATA/hidden.json and blanks a note in the tx view
    monkeypatch.setattr(hide, "PATH", node.HIDDEN_PATH); monkeypatch.setattr(hide, "DATA", os.path.dirname(node.HIDDEN_PATH))
    import sys
    monkeypatch.setattr(sys, "argv", ["hide", "add", who[2], "test reason"]); hide.main()
    node._hidden["at"] = 0
    assert node.is_hidden("x" * 64, who[2]) and not node.is_hidden("x" * 64, new_keypair()[2])
    d = node.scrub({**tx.to_dict(), "txid": tx.txid}); assert d["extra"] == "" and d["hidden"] is True
    monkeypatch.setattr(sys, "argv", ["hide", "remove", who[2]]); hide.main(); node._hidden["at"] = 0
    assert not node.is_hidden("x" * 64, who[2])


def test_version_and_update_notice(client):
    c, node = client
    from donutcoin import __version__
    i = c.get("/api/info").json()
    assert i["version"] == __version__ and i["rules"] == {"notes_from": node.NOTE_RULES_HEIGHT, "maturity_from": node.MATURITY_RULES_HEIGHT} and i["update"] is None
    node.note_update({"version": "0.0.1", "rules": node.RULES})            # older: nothing
    assert node._update["version"] is None
    node.note_update({"version": __version__, "rules": node.RULES})        # same: nothing
    assert node._update["version"] is None
    node.note_update({"version": "99.0.0", "name": "peer", "rules": {"notes_from": 1}})
    assert node._update["version"] == "99.0.0" and node._update["rules"] == {"notes_from": 1}
    u = c.get("/api/info").json()["update"]; assert u == {"version": "99.0.0", "rules": {"notes_from": 1}}
    node._update.update(version=None, rules=None)


def test_notes_page_permalink_and_rss(client):
    c, node = client
    assert c.get("/notes").status_code == 200 and "Said on the chain" in c.get("/notes").text
    assert c.get("/note/" + "0" * 64).status_code == 404 and c.get("/note/nope").status_code == 404
    r = c.get("/notes.xml"); assert r.status_code == 200 and r.text.startswith("<?xml") and "<rss" in r.text


def test_push_alerts_flag(client, monkeypatch):
    c, node = client
    from donutcoin.crypto import new_keypair
    import donutcoin.push as P
    priv, pub, addr = new_keypair()
    c.post("/api/account/register", json={"username": "buzz", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    monkeypatch.setattr(P, "check_sig", lambda *a, **k: True)
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    r = c.post("/api/push/subscribe", json={"payload": {"username": "buzz", "subscription": sub, "alerts": True}, "pubkey": pub, "signature": "00"}); assert r.status_code == 200, r.text
    assert c.get("/api/push/status/buzz").json() == {"registered": 1, "alerts": True}
    c.post("/api/push/subscribe", json={"payload": {"username": "buzz", "subscription": sub, "alerts": False}, "pubkey": pub, "signature": "00"})
    assert c.get("/api/push/status/buzz").json()["alerts"] is False


def test_tx_reports_the_block_height_not_the_tx_field(client):
    """A transfer's own `height` field is 0; /api/tx must answer with the block it landed in."""
    c, node = client
    from donutcoin.crypto import new_keypair
    from donutcoin.tx import Tx, TxIn, TxOut
    ch = node.chain
    who = new_keypair()
    t = Tx(inputs=[TxIn("0" * 64, 0)], outputs=[TxOut(1, who[2])]); t.sign_all(who[0], who[1])
    assert t.height == 0
    b = ch.tip
    ch.blocks[-1].txs.append(t); ch.txindex[t.txid] = (ch.height, len(b.txs) - 1)
    r = c.get("/api/tx/" + t.txid).json()
    assert r["height"] == ch.height and r["confirmations"] == 1


def _mine_to(node, addr, n=2):
    """n blocks paid to addr. The template carries no nonce (that is the miner's job) and each block
    is stamped a minute on, so a burst of them does not drive the difficulty up."""
    from donutcoin.chain import Block
    from donutcoin.tx import Tx
    for _ in range(n):
        t = node.chain.template(addr)
        b = Block(t["prev_hash"], node.chain.tip.timestamp + 60, t["bits"], 0, [Tx.from_dict(t["txs"][0])])
        while not b.meets_target(): b.nonce += 1
        node.chain.add_block(b)


def test_who_page_and_api(client):
    c, node = client
    from donutcoin.crypto import new_keypair
    _, _, addr = new_keypair()
    _mine_to(node, addr, 3)

    r = c.get(f"/api/who/{addr}"); assert r.status_code == 200
    d = r.json()
    assert d["blocks"] == 3 and d["rank"] == 1 and d["address"] == addr
    assert d["earned"] == 3 * node.BLOCK_REWARD and d["first"] is not None and d["notes"] == 0

    # a page exists for a bare address, and the placeholders are all filled
    p = c.get(f"/who/{addr}"); assert p.status_code == 200 and "{{" not in p.text
    assert addr in p.text and "og:title" in p.text

    # and by @username once one is claimed
    c.post("/api/account/register", json={"username": "minerbob", "auth": "a" * 64, "blob": "x" * 60, "address": addr})
    assert c.get("/api/who/@minerbob").json()["blocks"] == 3
    assert c.get("/who/@minerbob").status_code == 200
    assert "@minerbob" in c.get("/who/@minerbob").text

    # strangers and nonsense do not get a page
    assert c.get("/api/who/@nobody").status_code == 404
    assert c.get("/who/not a name").status_code == 404
    _, _, unused = new_keypair()
    assert c.get(f"/who/{unused}").status_code == 404      # never mined, holds nothing


def test_in_units():
    from donutcoin.node import in_units
    from donutcoin.tx import RIBBON, TIARA, COIN
    assert in_units(0) == "" and in_units(999 * COIN) == ""
    assert in_units(RIBBON) == "1 Ribbon"
    assert in_units(2 * RIBBON) == "2 Ribbons"
    assert in_units(TIARA) == "1 Tiara"
    assert in_units(15 * RIBBON) == "1.5 Tiaras"


def test_brag_covers_every_kind_of_miner():
    from donutcoin.node import brag
    import time as _t
    base = {"blocks": 0, "rank": None, "of": 5, "share": 0.0, "balance": 0, "last_time": 0}
    tail = "worth nothing on purpose."

    paid = brag({**base, "balance": 10_000 * 10**8})
    assert "10,000 DONUT" in paid and "ever having mined" in paid

    top = brag({**base, "blocks": 1191, "rank": 1, "share": 36.1, "last_time": int(_t.time())})
    assert "largest miner" in top and "1,191 blocks" in top and "36.1%" in top and "#1" not in top

    one = brag({**base, "blocks": 1, "rank": 4, "share": 0.1, "last_time": int(_t.time())})
    assert "one of only 5 people" in one and "0.1%" not in one      # a percentage would hide the achievement

    mid = brag({**base, "blocks": 503, "rank": 2, "share": 15.3, "last_time": int(_t.time())})
    assert mid.startswith("#2 of 5 miners") and "503 blocks" in mid

    # gone quiet: said plainly, and only for someone who has actually mined
    old = brag({**base, "blocks": 20, "rank": 3, "share": 1.0, "last_time": int(_t.time()) - 30 * 86400})
    assert "Quiet since" in old
    assert "Quiet since" not in paid

    for line in (paid, top, one, mid, old):
        assert line.endswith(tail) and "$" not in line             # never a monetary value


def test_ops_alarm_is_for_keyed_peers_only():
    from fastapi.testclient import TestClient
    from donutcoin import node
    c = TestClient(node.app)
    assert c.post("/api/ops/alarm", json={"to": ["x"], "body": "hi"}).status_code == 403
    r = c.post("/api/ops/alarm", json={"to": ["nobody"], "body": "hi"}, headers={"X-Donut-Key": node.SECRET})
    assert r.status_code == 200 and r.json() == {"sent": 0}


def test_every_page_shows_the_running_version_in_its_footer():
    """One version on the whole site, the node's own (2026-09-26): it used to be the node's number on
    the explorer and the latest GitHub release's on the download page."""
    from fastapi.testclient import TestClient
    from donutcoin import node, __version__
    c = TestClient(node.app)
    for path in ["/"] + [f"/{p}" for p in node.PAGES]:
        body = c.get(path).text
        assert f"Donut Coin v{__version__}" in body and "{{SITE_VERSION}}" not in body, path
