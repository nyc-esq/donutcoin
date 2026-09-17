"""Web push: the phone buzzes when something happens to your wallet or your trades.

Subscriptions are per account and signed with the account's wallet key (no password material is
kept anywhere for this). VAPID keys are generated once per node into DATA/vapid.json. Sending is
best-effort; a subscription the push service reports gone (404/410) is dropped.
"""
import base64, json, os, sqlite3, time
from .crypto import is_valid_address
from .market import check_sig, MarketError

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
except Exception:            # pragma: no cover - the tests stub sending
    webpush = None; WebPushException = Exception; Vapid = None


class Push:
    def __init__(self, data_dir: str, accounts_db: str):
        self.path = os.path.join(data_dir, "push.db"); self.accounts_db = accounts_db
        self.vapid_file = os.path.join(data_dir, "vapid.json")
        c = self._db(); c.execute("CREATE TABLE IF NOT EXISTS subs (endpoint TEXT PRIMARY KEY, username TEXT NOT NULL, sub TEXT NOT NULL, created INTEGER NOT NULL)"); c.commit(); c.close()
        self.sender = webpush
        self._keys()

    def _db(self):
        return sqlite3.connect(self.path, check_same_thread=False)

    def _keys(self) -> dict:
        if os.path.exists(self.vapid_file): return json.load(open(self.vapid_file))
        if Vapid is None: return {}
        v = Vapid(); v.generate_keys()
        raw = v.public_key.public_bytes(encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.X962,
                                        format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint)
        keys = {"private_pem": v.private_pem().decode(), "public_key": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
        with open(self.vapid_file, "w") as f: json.dump(keys, f)
        os.chmod(self.vapid_file, 0o600); return keys

    def public_key(self) -> str:
        return self._keys().get("public_key", "")

    # ---- subscribe / unsubscribe, signed by the account's wallet key
    def _account_address(self, username: str) -> str | None:
        try:
            c = sqlite3.connect(self.accounts_db); row = c.execute("SELECT address FROM accounts WHERE username=?", (username,)).fetchone(); c.close()
        except sqlite3.OperationalError:       # no accounts table yet: nobody has registered
            return None
        return row[0] if row else None

    def subscribe(self, body: dict) -> dict:
        p = body.get("payload") or {}; u = str(p.get("username", "")).lower(); sub = p.get("subscription") or {}
        addr = self._account_address(u)
        if not addr: raise MarketError("no such account")
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), addr)
        endpoint = str(sub.get("endpoint", ""))
        if not endpoint.startswith("https://") or "keys" not in sub: raise MarketError("bad subscription")
        c = self._db(); c.execute("INSERT OR REPLACE INTO subs VALUES (?,?,?,?)", (endpoint, u, json.dumps(sub), int(time.time()))); c.commit(); c.close()
        return {"ok": True}

    def unsubscribe(self, body: dict) -> dict:
        p = body.get("payload") or {}; u = str(p.get("username", "")).lower(); endpoint = str(p.get("endpoint", ""))
        addr = self._account_address(u)
        if not addr: raise MarketError("no such account")
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), addr)
        c = self._db(); c.execute("DELETE FROM subs WHERE endpoint=? AND username=?", (endpoint, u)); c.commit(); c.close()
        return {"ok": True}

    def count(self, username: str) -> int:
        c = self._db(); n = c.execute("SELECT count(*) FROM subs WHERE username=?", (username,)).fetchone()[0]; c.close(); return n

    # ---- sending
    def usernames_for(self, address: str) -> list[str]:
        try:
            c = sqlite3.connect(self.accounts_db); rows = c.execute("SELECT username FROM accounts WHERE address=?", (address,)).fetchall(); c.close()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    def notify_address(self, address: str, title: str, body: str, url: str = "/wallet") -> int:
        return sum(self.notify(u, title, body, url) for u in self.usernames_for(address))

    def notify(self, username: str, title: str, body: str, url: str = "/wallet") -> int:
        keys = self._keys()
        if not keys or self.sender is None: return 0
        c = self._db(); subs = c.execute("SELECT endpoint, sub FROM subs WHERE username=?", (username,)).fetchall(); n = 0
        for endpoint, sub in subs:
            try:
                self.sender(subscription_info=json.loads(sub), data=json.dumps({"title": title, "body": body, "url": url}),
                            vapid_private_key=keys["private_pem"], vapid_claims={"sub": "mailto:donut@donutcoin.meme"}, ttl=3600)
                n += 1
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                print(f"push to @{username} failed: {code} {str(e)[:120]}", flush=True)
                if code in (404, 410): c.execute("DELETE FROM subs WHERE endpoint=?", (endpoint,)); print(f"push: dropped a gone subscription for @{username}", flush=True)
            except Exception as e:
                print(f"push to @{username} error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        c.commit(); c.close(); return n
