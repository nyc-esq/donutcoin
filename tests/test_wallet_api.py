"""The account API and a browser-style (compact) signature, through the FastAPI app."""
import os, json, hashlib
os.environ["DONUTCOIN_EASY_POW"] = "1"
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
    r = c.get("/download"); assert r.status_code == 200 and "v9.9.9" in r.text and "https://example/x.exe" in r.text and "Not yet" in r.text
    monkeypatch.setattr(node, "latest_release", lambda: None)
    assert "not released yet" in c.get("/download").text
