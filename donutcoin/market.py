"""The offer board: DONUT for Apple Cash, between two people, with no custody.

An offer is a signed message from a wallet key ("I sell N DONUT for $X, pay me by Apple Cash at
<contact>"). Taking, claiming payment and cancelling are signed by whichever party acts. The site
never holds coins or money: the buyer pays in Apple Cash, the seller sends DONUT from their own
wallet, and the chain watcher marks the trade complete once the coins have landed at the buyer's address
and are SETTLE_CONFIRMATIONS deep (past the reorg cap, so no longer chain can take them back).
Reputation is the count of completed trades per address. The seller's contact is shown only to
the buyer who took the offer.
"""
import json, re, secrets, sqlite3, time
from .crypto import sha256d, verify, pubkey_to_address, is_valid_address
from .tx import COIN
from .chain import MAX_REORG_DEPTH

# A trade completes only once the seller's payment is buried deeper than the chain's reorg cap: at that depth no
# competing chain can drop the block, so the buyer's coins cannot vanish after the seller has been paid in cash.
# 2026-09-16, the operator: "add the 20 confirmations to the market" - 21, one more than the cap. At one-minute blocks
# that is ~21 minutes from the coins being mined to "Trade complete".
SETTLE_CONFIRMATIONS = MAX_REORG_DEPTH + 1

METHODS = {"apple_cash": "Apple Cash", "cash_app": "Cash App"}
CONTACT_RULES = {"apple_cash": (r"^(\+?[0-9() .-]{7,20}|[^@\s]+@[^@\s]+\.[^@\s]+)$", "a phone number or the email on your Apple Cash"),
                 "cash_app": (r"^\$?[A-Za-z][A-Za-z0-9_-]{0,19}$", "your $cashtag")}
OPEN, TAKEN, PAID, COMPLETE, CANCELLED = "open", "taken", "paid", "complete", "cancelled"
TAKE_TIMEOUT = 24 * 3600          # a taken offer the buyer never pays can be released by the seller after this


class MarketError(Exception):
    pass


def _digest(payload: dict) -> bytes:
    return sha256d(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())


def check_sig(payload: dict, pubkey: str, signature: str, expect_address: str) -> None:
    """The browser signs sha256d of the sorted, compact JSON of `payload`."""
    try: addr = pubkey_to_address(bytes.fromhex(pubkey))
    except Exception: raise MarketError("bad public key")
    if addr != expect_address: raise MarketError("key does not match the address")
    if not verify(pubkey, _digest(payload), signature): raise MarketError("bad signature")
    if abs(time.time() - int(payload.get("ts", 0))) > 600: raise MarketError("stale signature; check your clock")


class Market:
    def __init__(self, path: str, chain, notify=None):
        self.path, self.chain = path, chain
        self.notify = notify or (lambda address, title, body, url="/market": 0)   # push, wired by the node
        c = self._db()
        DDL = """CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY, seller TEXT, amount INTEGER NOT NULL, price_cents INTEGER NOT NULL,
            method TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            created INTEGER NOT NULL, buyer TEXT, taken_at INTEGER, taken_height INTEGER, paid_at INTEGER,
            code TEXT, complete_txid TEXT, completed_at INTEGER, cancelled_at INTEGER, side TEXT NOT NULL DEFAULT 'sell')"""
        c.execute(DDL)
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(offers)")}
        if "side" not in cols:
            c.execute("ALTER TABLE offers ADD COLUMN side TEXT NOT NULL DEFAULT 'sell'"); cols = {r[1]: r for r in c.execute("PRAGMA table_info(offers)")}
        if cols["seller"][3] == 1:      # the first schema had seller NOT NULL; a 'buy' request has no seller until taken (2026-09-17)
            c.executescript("BEGIN; ALTER TABLE offers RENAME TO offers_old; " + DDL.replace("IF NOT EXISTS ", "") + "; "
                            "INSERT INTO offers SELECT id, seller, amount, price_cents, method, contact, note, status, created, buyer, taken_at, taken_height, paid_at, code, complete_txid, completed_at, cancelled_at, side FROM offers_old; "
                            "DROP TABLE offers_old; COMMIT;")
        c.execute("CREATE TABLE IF NOT EXISTS prices (ts INTEGER NOT NULL, cents_per_donut REAL NOT NULL, amount INTEGER NOT NULL, offer TEXT)")
        c.commit(); c.close()

    def _db(self):
        return sqlite3.connect(self.path, check_same_thread=False)

    @staticmethod
    def _check_contact(method: str, contact: str) -> str:
        rule, hint = CONTACT_RULES[method]
        contact = contact.strip()
        if method == "cash_app": contact = "$" + contact.lstrip("$")
        if not (2 <= len(contact) <= 80) or not re.match(rule, contact): raise MarketError(f"contact: {hint}")
        return contact

    def last_trade(self) -> dict | None:
        c = self._db(); r = c.execute("SELECT ts, cents_per_donut, amount FROM prices ORDER BY ts DESC LIMIT 1").fetchone(); c.close()
        return {"ts": r[0], "cents_per_donut": r[1], "amount": r[2]} if r else None

    def price_points(self, limit: int = 200) -> list[dict]:
        c = self._db(); rows = c.execute("SELECT ts, cents_per_donut, amount FROM prices ORDER BY ts DESC LIMIT ?", (limit,)).fetchall(); c.close()
        return [{"ts": t, "cents_per_donut": p, "amount": a} for t, p, a in reversed(rows)]

    # ---- reads
    @staticmethod
    def _row(r, cols, for_address: str | None = None) -> dict:
        d = dict(zip(cols, r))
        party = for_address is not None and for_address in (d["seller"], d["buyer"])   # an untaken offer has buyer None; an anonymous viewer is not a party
        if not party: d["contact"] = None                     # only the two parties see the Apple Cash contact
        return d

    def _select(self, where: str, args: tuple, for_address: str | None = None, limit: int = 100) -> list[dict]:
        c = self._db(); cur = c.execute(f"SELECT * FROM offers WHERE {where} ORDER BY created DESC LIMIT ?", args + (limit,))
        cols = [d[0] for d in cur.description]; rows = [self._row(r, cols, for_address) for r in cur.fetchall()]; c.close()
        return rows

    def reputation(self, address: str) -> dict:
        c = self._db()
        sold = c.execute("SELECT count(*) FROM offers WHERE seller=? AND status=?", (address, COMPLETE)).fetchone()[0]
        bought = c.execute("SELECT count(*) FROM offers WHERE buyer=? AND status=?", (address, COMPLETE)).fetchone()[0]
        c.close(); return {"sold": sold, "bought": bought}

    def board(self, for_address: str | None = None) -> list[dict]:
        out = []
        for o in self._select("status=?", (OPEN,), for_address):
            poster = o["seller"] if o["side"] == "sell" else o["buyer"]
            o["poster"] = poster; o["poster_rep"] = self.reputation(poster)
            o["seller_can_cover"] = (sum(u["amount"] for u in self.chain.spendable(o["seller"])) >= o["amount"]) if o["seller"] else None
            o["seller_rep"] = self.reputation(o["seller"]) if o["seller"] else None
            out.append(o)
        return out

    def mine(self, address: str) -> list[dict]:
        return self._select("seller=? OR buyer=?", (address, address), address)

    # ---- writes (each proves the actor holds the key for the address it claims)
    def create(self, body: dict) -> dict:
        """side 'sell': `seller` offers DONUT and gives a contact. side 'buy': `buyer` wants DONUT and
        will pay whoever takes it; the taker supplies the contact then."""
        p = body.get("payload") or {}
        side = p.get("side", "sell")
        if side not in ("sell", "buy"): raise MarketError("side: sell or buy")
        poster = p.get("seller" if side == "sell" else "buyer", "")
        amount, price, method, note = int(p.get("amount", 0)), int(p.get("price_cents", 0)), p.get("method", ""), str(p.get("note", ""))[:140]
        if not is_valid_address(poster): raise MarketError("bad address")
        if not (COIN <= amount <= 10_000_000 * COIN): raise MarketError("amount: between 1 and 10,000,000 DONUT")
        if not (100 <= price <= 1_000_000_00): raise MarketError("price: between $1 and $1,000,000")
        if method not in METHODS: raise MarketError("payment method not supported")
        contact = self._check_contact(method, str(p.get("contact", ""))) if side == "sell" else ""
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), poster)
        if side == "sell" and sum(u["amount"] for u in self.chain.spendable(poster)) < amount: raise MarketError("your spendable balance does not cover this offer")
        c = self._db()
        if c.execute("SELECT count(*) FROM offers WHERE (seller=? OR buyer=?) AND status IN (?,?,?)", (poster, poster, OPEN, TAKEN, PAID)).fetchone()[0] >= 5:
            c.close(); raise MarketError("at most five live offers per wallet")
        oid = secrets.token_hex(8)
        c.execute("INSERT INTO offers (id, seller, buyer, amount, price_cents, method, contact, note, status, created, side) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, poster if side == "sell" else None, poster if side == "buy" else None, amount, price, method, contact, note, OPEN, int(time.time()), side)); c.commit(); c.close()
        return {"id": oid}

    def _get(self, c, oid: str) -> dict:
        cur = c.execute("SELECT * FROM offers WHERE id=?", (oid,)); r = cur.fetchone()
        if not r: raise MarketError("no such offer")
        return dict(zip([d[0] for d in cur.description], r))

    def take(self, oid: str, body: dict) -> dict:
        """Taking a sell offer: the taker is the buyer. Taking a buy (wanted) offer: the taker is the
        seller and brings the contact the buyer will pay."""
        p = body.get("payload") or {}; who = p.get("who") or p.get("buyer", "")
        if not is_valid_address(who): raise MarketError("bad address")
        if p.get("offer") != oid: raise MarketError("signature is for another offer")
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), who)
        c = self._db(); o = self._get(c, oid)
        if o["status"] != OPEN: c.close(); raise MarketError("that offer is no longer open")
        if who in (o["seller"], o["buyer"]): c.close(); raise MarketError("you cannot take your own offer")
        code = "DC-" + secrets.token_hex(2).upper()
        if o["side"] == "sell":
            c.execute("UPDATE offers SET status=?, buyer=?, taken_at=?, taken_height=?, code=? WHERE id=? AND status=?",
                      (TAKEN, who, int(time.time()), self.chain.height, code, oid, OPEN)); c.commit(); c.close()
            self.notify(o["seller"], "Your offer was taken", f"{o['amount'] // COIN:,} DONUT for ${o['price_cents'] / 100:,.2f}. Wait for the payment, code {code}.", "/market")
            return {"id": oid, "code": code, "contact": o["contact"], "method": METHODS[o["method"]], "amount": o["amount"], "price_cents": o["price_cents"]}
        contact = self._check_contact(o["method"], str(p.get("contact", "")))
        if sum(u["amount"] for u in self.chain.spendable(who)) < o["amount"]: c.close(); raise MarketError("your spendable balance does not cover this")
        c.execute("UPDATE offers SET status=?, seller=?, contact=?, taken_at=?, taken_height=?, code=? WHERE id=? AND status=?",
                  (TAKEN, who, contact, int(time.time()), self.chain.height, code, oid, OPEN)); c.commit(); c.close()
        self.notify(o["buyer"], "A seller took your request", f"Pay ${o['price_cents'] / 100:,.2f} by {METHODS[o['method']]} with code {code}; they send {o['amount'] // COIN:,} DONUT.", "/market")
        return {"id": oid, "code": code, "method": METHODS[o["method"]], "amount": o["amount"], "price_cents": o["price_cents"]}

    def paid(self, oid: str, body: dict) -> dict:
        p = body.get("payload") or {}; buyer = p.get("buyer", "")
        if p.get("offer") != oid: raise MarketError("signature is for another offer")
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), buyer)
        c = self._db(); o = self._get(c, oid)
        if o["status"] != TAKEN or o["buyer"] != buyer: c.close(); raise MarketError("not your open trade")
        c.execute("UPDATE offers SET status=?, paid_at=? WHERE id=?", (PAID, int(time.time()), oid)); c.commit(); c.close()
        self.notify(o["seller"], "Buyer says they paid", f"Check for ${o['price_cents'] / 100:,.2f} with code {o['code']}, then send {o['amount'] // COIN:,} DONUT.", "/market")
        return {"id": oid, "status": PAID}

    def cancel(self, oid: str, body: dict) -> dict:
        p = body.get("payload") or {}; who = p.get("who", "")
        if p.get("offer") != oid: raise MarketError("signature is for another offer")
        check_sig(p, body.get("pubkey", ""), body.get("signature", ""), who)
        c = self._db(); o = self._get(c, oid); now = int(time.time())
        poster = o["seller"] if o["side"] == "sell" else o["buyer"]
        taker = o["buyer"] if o["side"] == "sell" else o["seller"]
        ok = (who == poster and o["status"] == OPEN) \
            or (who == taker and o["status"] == TAKEN) \
            or (who == poster and o["status"] == TAKEN and now - o["taken_at"] > TAKE_TIMEOUT)
        if not ok: c.close(); raise MarketError("this trade cannot be cancelled by you right now" + (" (a buyer who said they paid must be settled or contacted)" if o["status"] == PAID else ""))
        new = CANCELLED if (who == poster and o["status"] == OPEN) else OPEN
        if o["side"] == "sell":
            c.execute("UPDATE offers SET status=?, buyer=NULL, taken_at=NULL, taken_height=NULL, paid_at=NULL, code=NULL, cancelled_at=? WHERE id=?", (new, now if new == CANCELLED else None, oid))
        else:
            c.execute("UPDATE offers SET status=?, seller=NULL, contact='', taken_at=NULL, taken_height=NULL, paid_at=NULL, code=NULL, cancelled_at=? WHERE id=?", (new, now if new == CANCELLED else None, oid))
        c.commit(); c.close()
        return {"id": oid, "status": new}

    # ---- the watcher: coins from seller to buyer, at or above the offer's amount, since the take
    def watch(self) -> int:
        c = self._db(); n = 0
        live = [dict(zip([d[0] for d in c.execute("SELECT * FROM offers LIMIT 0").description], r))
                for r in c.execute("SELECT * FROM offers WHERE status IN (?,?)", (TAKEN, PAID)).fetchall()]
        for o in live:
            # only blocks with SETTLE_CONFIRMATIONS or more on top count (block h has chain.height - h + 1)
            for h in range(max(o["taken_height"] or 0, 0) + 1, self.chain.height - SETTLE_CONFIRMATIONS + 2):
                for t in self.chain.blocks[h].txs:
                    if t.coinbase: continue
                    from_seller = any(i.pubkey and pubkey_to_address(bytes.fromhex(i.pubkey)) == o["seller"] for i in t.inputs)
                    to_buyer = sum(x.amount for x in t.outputs if x.address == o["buyer"])
                    if from_seller and to_buyer >= o["amount"]:
                        c.execute("UPDATE offers SET status=?, complete_txid=?, completed_at=? WHERE id=?", (COMPLETE, t.txid, int(time.time()), o["id"])); n += 1
                        c.execute("INSERT INTO prices VALUES (?,?,?,?)", (int(time.time()), o["price_cents"] / (o["amount"] / COIN), o["amount"], o["id"]))
                        self.notify(o["buyer"], "Trade complete", f"{o['amount'] // COIN:,} DONUT arrived. Enjoy.", "/wallet")
                        self.notify(o["seller"], "Trade complete", f"The chain confirmed your {o['amount'] // COIN:,} DONUT to the buyer.", "/market")
                        break
                else: continue
                break
        c.commit(); c.close(); return n
