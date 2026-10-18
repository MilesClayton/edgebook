"""Transactional single-host ledger. Every mutation uses an independent connection."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from edgebook.execution.live_ledger import (
    CENT,
    DOLLAR,
    ConfirmRequest,
    GuardError,
    Intent,
    Limits,
    debit,
    dollars,
    fee_reserve,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY, expires INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, expires INTEGER NOT NULL,
    generation INTEGER NOT NULL, body TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders (
    key TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL, order_id TEXT UNIQUE, reserved INTEGER NOT NULL,
    filled INTEGER NOT NULL DEFAULT 0, cost INTEGER NOT NULL DEFAULT 0,
    fees INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY, side TEXT NOT NULL, units INTEGER NOT NULL, cost INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY, order_key TEXT NOT NULL REFERENCES orders(key), body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements (ticker TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watched_markets (
    ticker TEXT PRIMARY KEY, close_ms INTEGER NOT NULL, outcome INTEGER);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
    kind TEXT NOT NULL, body TEXT NOT NULL);
"""


class SQLiteLedger:
    def __init__(self, path: Path, limits: Limits, identity: str = "offline-test") -> None:
        self.path, self.limits = path, limits
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "meta" not in tables:
                raise GuardError("unrecognized legacy ledger; explicit migration required")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE id=1").fetchone()
            binding = {"identity": identity, "limits": limits.model_dump()}
            if row is not None and self.state(db)["identity"] == identity:
                # caps are owner-editable (set_limits); the ledger's stored caps are
                # authoritative and settings only seed a new ledger. Spend/loss never reset.
                self.limits = Limits.model_validate(self.state(db)["limits"])
            if row is None:
                if db.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
                    raise GuardError("ledger binding missing for existing orders")
                state = {
                    **binding,
                    "paused": True,
                    "generation": 0,
                    "account_ms": 0,
                    "balance": 0,
                    "loss": 0,
                    "pnl": 0,
                    "error": "reconciliation required",
                }
                db.execute("INSERT INTO meta VALUES(1,?)", (json.dumps(state),))
            elif self.state(db)["identity"] != identity:
                raise GuardError("ledger identity changed; existing budget cannot be reset")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def state(db: sqlite3.Connection) -> dict[str, Any]:
        return dict(json.loads(db.execute("SELECT value FROM meta WHERE id=1").fetchone()[0]))

    @staticmethod
    def save_state(db: sqlite3.Connection, state: Mapping[str, Any]) -> None:
        db.execute("UPDATE meta SET value=? WHERE id=1", (json.dumps(state),))

    @staticmethod
    def event(db: sqlite3.Connection, at: int, kind: str, body: Any) -> None:
        db.execute("INSERT INTO audit(at,kind,body) VALUES(?,?,?)", (at, kind, json.dumps(body)))

    def audit(self, at: int, kind: str, body: Any) -> None:
        with self.transaction() as db:
            self.event(db, at, kind, body)

    @staticmethod
    def resting_remainders(db: sqlite3.Connection) -> dict[str, tuple[str, int]]:
        """Unfilled contracts of our resting maker orders: ticker -> (side, units)."""
        out: dict[str, tuple[str, int]] = {}
        for row in db.execute("SELECT * FROM orders WHERE status='resting'"):
            intent = Intent.model_validate_json(json.loads(row["body"])["intent_json"])
            side, units = out.get(intent.ticker, (intent.side, 0))
            out[intent.ticker] = (side, units + intent.units - row["filled"])
        return out

    @staticmethod
    def unresolved(db: sqlite3.Connection) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM orders WHERE status IN ('pending','accepted','unknown')"
            ).fetchone()
            is not None
        )

    def recover(self, now: int) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE orders SET status='unknown',updated=? "
                "WHERE status IN ('pending','accepted')",
                (now,),
            )
            state = self.state(db)
            state.update(paused=True, account_ms=0, generation=state["generation"] + 1)
            self.save_state(db, state)
            self.event(db, now, "restart", {"paused": True})

    def set_limits(self, limits: Limits, now: int) -> Limits:
        """Owner changes the caps. Spend, exposure and loss so far are kept, so raising a
        cap adds room and lowering one below what is already used blocks new entries."""
        with self.transaction() as db:
            state = self.state(db)
            old = state["limits"]
            state["limits"] = limits.model_dump()
            self.save_state(db, state)
            self.event(db, now, "limits_changed", {"from": old, "to": state["limits"]})
        self.limits = limits
        return limits

    def set_autopilot(self, enabled: bool, now: int, kind: str = "autopilot") -> None:
        with self.transaction() as db:
            state = self.state(db)
            state[kind] = enabled
            self.save_state(db, state)
            self.event(db, now, kind, {"enabled": enabled})

    def pause(self, now: int, reason: str = "user paused") -> None:
        with self.transaction() as db:
            state = self.state(db)
            state.update(paused=True, generation=state["generation"] + 1, error=reason)
            self.save_state(db, state)
            self.event(db, now, "pause", {"reason": reason})

    def resume(self, now: int, max_age: int) -> None:
        with self.transaction() as db:
            state = self.state(db)
            self.check_account(db, state, now, max_age)
            state.update(paused=False, error="", generation=state["generation"] + 1)
            self.save_state(db, state)
            self.event(db, now, "resume", {"unattended": bool(state.get("autopilot", False))})

    def account(
        self,
        now: int,
        balance: int,
        positions: Mapping[str, int],
        resting_client_ids: Collection[str] = (),
    ) -> None:
        with self.transaction() as db:
            expected = {
                row["ticker"]: row["units"] * (1 if row["side"] == "yes" else -1)
                for row in db.execute("SELECT * FROM positions WHERE units>0")
            }
            actual = {ticker: units for ticker, units in positions.items() if units}
            # A resting maker order can fill between our order read and this account read:
            # allow the position to be ahead of the ledger by at most its unfilled remainder,
            # on its side. The next reconciliation applies those fills.
            slack = self.resting_remainders(db)
            for ticker in set(expected) | set(actual):
                want, have = expected.get(ticker, 0), actual.get(ticker, 0)
                side, room = slack.get(ticker, ("yes", 0))
                ahead = (have - want) * (1 if side == "yes" else -1)
                if have != want and not (0 < ahead <= room and have * want >= 0):
                    raise GuardError(
                        "account positions differ from routed ledger; reconcile manual activity"
                    )
            ours = {
                str(uuid.uuid5(uuid.NAMESPACE_URL, row["proposal_id"]))
                for row in db.execute(
                    "SELECT proposal_id FROM orders WHERE status IN ('resting','accepted')"
                )
            }
            if not set(resting_client_ids) <= ours:
                raise GuardError("resting account orders not placed here require operator review")
            if self.unresolved(db):
                raise GuardError("unknown or pending order status requires reconciliation")
            state = self.state(db)
            state.update(account_ms=now, balance=balance, error="")
            self.save_state(db, state)
            self.event(db, now, "account", {"balance": balance, "positions": actual})

    @staticmethod
    def check_account(
        db: sqlite3.Connection, state: Mapping[str, Any], now: int, max_age: int
    ) -> None:
        if not 0 <= now - state["account_ms"] <= max_age or not state["account_ms"]:
            raise GuardError("account state is stale or unknown")
        if SQLiteLedger.unresolved(db):
            raise GuardError("unknown or pending order status requires reconciliation")

    def review(self, body: dict[str, Any], now: int, ttl: int) -> str:
        review_id = secrets.token_hex(24)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?)", (review_id, now + ttl, json.dumps(body))
            )
            db.execute(
                "INSERT OR IGNORE INTO watched_markets(ticker,close_ms) VALUES(?,?)",
                (body["ticker"], int(body["observed_ms"] + body["seconds_left"] * 1000)),
            )
            self.event(db, now, "review", {"review_id": review_id, **body})
        return review_id

    def due_outcomes(self, now: int) -> list[str]:
        with self.connection() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT ticker FROM watched_markets WHERE close_ms<=? AND outcome IS NULL "
                    "ORDER BY close_ms LIMIT 20",
                    (now,),
                )
            ]

    def record_outcome(self, ticker: str, yes_value: int, now: int) -> None:
        if not 0 <= yes_value <= DOLLAR:
            raise GuardError("invalid market outcome")
        with self.transaction() as db:
            updated = db.execute(
                "UPDATE watched_markets SET outcome=? WHERE ticker=? AND outcome IS NULL",
                (yes_value, ticker),
            ).rowcount
            if updated:
                self.event(db, now, "market_outcome", {"ticker": ticker, "yes_value": yes_value})

    def get_review(self, review_id: str, now: int) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None or now >= row["expires"]:
                raise GuardError("review expired or missing")
            return dict(json.loads(row["body"]))

    def proposal(self, body: dict[str, Any], now: int, ttl: int) -> dict[str, Any]:
        proposal_id, token = secrets.token_hex(24), secrets.token_urlsafe(32)
        with self.transaction() as db:
            state = self.state(db)
            db.execute(
                "INSERT INTO proposals(id,token_hash,expires,generation,body) VALUES(?,?,?,?,?)",
                (
                    proposal_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    now + ttl,
                    state["generation"],
                    json.dumps(body),
                ),
            )
            self.event(db, now, "proposal", {"proposal_id": proposal_id, **body})
        return {
            **body,
            "proposal_id": proposal_id,
            "confirmation_token": token,
            "expires_ms": now + ttl,
            "status": "confirmation_required",
            "submitted": False,
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT body,expires FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GuardError("proposal missing")
            return {**json.loads(row["body"]), "expires_ms": row["expires"]}

    @staticmethod
    def totals(db: sqlite3.Connection) -> tuple[int, int]:
        spent = 0
        pending = 0
        for row in db.execute("SELECT * FROM orders"):
            intent = Intent.model_validate_json(json.loads(row["body"])["intent_json"])
            if intent.action == "buy":
                spent += row["cost"] + row["fees"]
                if row["status"] in {"pending", "accepted", "unknown", "resting"}:
                    remainder = max(0, row["reserved"] - row["cost"] - row["fees"])
                    pending += remainder
                    spent += remainder
        exposure = sum(row[0] for row in db.execute("SELECT cost FROM positions")) + pending
        return spent, exposure

    def reserve(
        self,
        request: ConfirmRequest,
        now: int,
        max_age: int,
        validation_expires_ms: int | None = None,
        clock: Callable[[], int] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.transaction() as db:
            now = clock() if clock is not None else now
            row = db.execute(
                "SELECT * FROM orders WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            proposal = db.execute(
                "SELECT * FROM proposals WHERE id=?", (request.proposal_id,)
            ).fetchone()
            if proposal is None or not secrets.compare_digest(
                proposal["token_hash"],
                hashlib.sha256(request.confirmation_token.encode()).hexdigest(),
            ):
                raise GuardError("invalid confirmation")
            body = json.loads(proposal["body"])
            if request.intent.model_dump() != body["intent"]:
                raise GuardError("confirmed ticker, side, price or quantity changed")
            if row is not None:
                if row["proposal_id"] != request.proposal_id:
                    raise GuardError("idempotency key reused for a different proposal")
                return self.order_view(row), False
            state = self.state(db)
            self.limits = Limits.model_validate(state["limits"])
            if proposal["used"] or proposal["generation"] != state["generation"]:
                raise GuardError("proposal consumed or invalidated by pause/resume")
            if now >= proposal["expires"]:
                raise GuardError("confirmation expired")
            if validation_expires_ms is not None and now >= validation_expires_ms:
                raise GuardError("data expired while acquiring reservation")
            if state["paused"]:
                raise GuardError("execution paused")
            self.check_account(db, state, now, max_age)
            intent = request.intent
            if not 0 < intent.units <= self.limits.max_contracts * 100:
                raise GuardError("quantity cap")
            if not 0 < intent.price < DOLLAR:
                raise GuardError("invalid price")
            reserved = debit(intent)
            if reserved > self.limits.order_debit_cents * CENT:
                raise GuardError("fee-inclusive per-order debit cap")
            _, exposure = self.totals(db)
            position = db.execute(
                "SELECT * FROM positions WHERE ticker=?", (intent.ticker,)
            ).fetchone()
            if intent.action == "buy":
                self.check_entry(db, state, intent, reserved)
            elif (
                not position or position["side"] != intent.side or position["units"] < intent.units
            ):
                raise GuardError("exit exceeds routed position")
            else:
                closing_cost = position["cost"] * intent.units // position["units"]
                minimum_net = intent.price * intent.units // 100 - fee_reserve(intent.units)
                if state["loss"] + exposure - closing_cost + max(0, closing_cost - minimum_net) > (
                    self.limits.realized_loss_cents * CENT
                ):
                    raise GuardError("exit fee would exceed loss cap")
            order_body = {
                "intent_json": intent.model_dump_json(),
                "proposal_id": request.proposal_id,
                "decision": body["decision"],
                "context": body["context"],
            }
            db.execute(
                "INSERT INTO orders(key,proposal_id,body,status,reserved,created,updated)"
                " VALUES(?,?,?,'pending',?,?,?)",
                (
                    request.idempotency_key,
                    request.proposal_id,
                    json.dumps(order_body),
                    reserved,
                    now,
                    now,
                ),
            )
            db.execute("UPDATE proposals SET used=1 WHERE id=?", (request.proposal_id,))
            state["account_ms"] = 0
            self.save_state(db, state)
            self.event(
                db,
                now,
                "reservation",
                {
                    "key": request.idempotency_key,
                    "reserved": reserved,
                    **order_body,
                },
            )
            result = db.execute(
                "SELECT * FROM orders WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            return self.order_view(result), True

    def check_entry(
        self, db: sqlite3.Connection, state: Mapping[str, Any], intent: Intent, reserved: int
    ) -> None:
        """Every money cap for a buy that reserves `reserved` (its worst-case debit)."""
        spent, exposure = self.totals(db)
        position = db.execute("SELECT * FROM positions WHERE ticker=?", (intent.ticker,)).fetchone()
        if position and position["units"] and position["side"] != intent.side:
            raise GuardError("entry would offset opposite position")
        if spent + reserved > self.limits.purchase_spend_cents * CENT:
            raise GuardError("cumulative purchase-spend cap")
        if exposure + reserved > self.limits.open_exposure_cents * CENT:
            raise GuardError("open exposure cap")
        if state["loss"] + exposure + reserved > self.limits.realized_loss_cents * CENT:
            raise GuardError("realized-loss cap including worst-case open loss")
        if reserved > state["balance"]:
            raise GuardError("insufficient available balance")

    def reserve_resting(
        self, key: str, intent: Intent, context: dict[str, Any], now: int, max_age: int
    ) -> dict[str, Any]:
        """Reserve a resting maker buy (long-shot pilot) under the same caps as a confirmed
        order. The whole worst-case debit stays reserved while the order rests, so the caps
        hold even if every resting order fills and loses."""
        if intent.action != "buy":
            raise GuardError("the pilot only rests buy orders")
        with self.transaction() as db:
            state = self.state(db)
            self.limits = Limits.model_validate(state["limits"])
            if state["paused"]:
                raise GuardError("execution paused")
            self.check_account(db, state, now, max_age)
            if db.execute("SELECT 1 FROM orders WHERE key=?", (key,)).fetchone():
                raise GuardError("order key already used")
            if intent.ticker in self.resting_remainders(db):
                raise GuardError("already resting an order in this market")
            if not 0 < intent.units <= self.limits.max_contracts * 100:
                raise GuardError("quantity cap")
            if not 0 < intent.price < DOLLAR:
                raise GuardError("invalid price")
            reserved = debit(intent)
            if reserved > self.limits.order_debit_cents * CENT:
                raise GuardError("fee-inclusive per-order debit cap")
            self.check_entry(db, state, intent, reserved)
            body = {
                "intent_json": intent.model_dump_json(),
                "proposal_id": key,
                "resting": True,
                "decision": {"action": "longshot_pilot"},
                "context": context,
            }
            db.execute(
                "INSERT INTO orders(key,proposal_id,body,status,reserved,created,updated)"
                " VALUES(?,?,?,'pending',?,?,?)",
                (key, key, json.dumps(body), reserved, now, now),
            )
            state["account_ms"] = 0
            self.save_state(db, state)
            self.event(db, now, "reservation", {"key": key, "reserved": reserved, **body})
            row = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            return self.order_view(row)

    @staticmethod
    def order_view(row: sqlite3.Row) -> dict[str, Any]:
        body = json.loads(row["body"])
        return {
            "idempotency_key": row["key"],
            "proposal_id": row["proposal_id"],
            "intent": json.loads(body["intent_json"]),
            "status": row["status"],
            "submission_attempted": (
                False
                if body.get("not_dispatched")
                else None
                if row["status"] in {"pending", "unknown"}
                else True
            ),
            "broker_acknowledged": row["order_id"] is not None,
            "submitted": row["order_id"] is not None,
            "order_id": row["order_id"],
            "filled_quantity": f"{row['filled'] / 100:.2f}",
            "fill_cost": dollars(row["cost"]),
            "fees": dollars(row["fees"]),
            "reserved_debit": dollars(row["reserved"]),
            "resting": bool(body.get("resting")),
            "created_ms": row["created"],
        }

    def orders(self, unresolved_only: bool = False) -> list[dict[str, Any]]:
        with self.connection() as db:
            query = "SELECT * FROM orders"
            if unresolved_only:
                query += " WHERE status IN ('pending','accepted','unknown','resting')"
            return [self.order_view(row) for row in db.execute(query + " ORDER BY created")]

    def mark(
        self,
        key: str,
        status: str,
        now: int,
        order_id: str | None = None,
        local_rejection: bool = False,
    ) -> None:
        if status not in {"accepted", "rejected", "unknown"}:
            raise GuardError("invalid order status")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            if row is None or row["status"] not in {"pending", "accepted", "unknown", "resting"}:
                raise GuardError("order not pending")
            if status == "rejected" and (row["order_id"] or row["filled"]):
                raise GuardError("accepted order cannot be released as rejected")
            if local_rejection:
                body = json.loads(row["body"])
                body["not_dispatched"] = True
                db.execute("UPDATE orders SET body=? WHERE key=?", (json.dumps(body), key))
            db.execute(
                "UPDATE orders SET status=?,order_id=COALESCE(?,order_id),updated=? WHERE key=?",
                (status, order_id, now, key),
            )
            self.event(
                db, now, "order_status", {"key": key, "status": status, "order_id": order_id}
            )

    def reconcile(
        self, key: str, order_id: str, status: str, fills: list[dict[str, Any]], now: int
    ) -> None:
        if status not in {"canceled", "executed", "resting"}:
            raise GuardError("unrecognized broker order status")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
            if row is None or row["order_id"] not in {None, order_id}:
                raise GuardError("unknown or mismatched broker order")
            body = json.loads(row["body"])
            intent = Intent.model_validate_json(body["intent_json"])
            maker = bool(body.get("resting"))
            total_units = sum(fill["units"] for fill in fills)
            total_cost = sum(fill["cost"] for fill in fills)
            total_fees = sum(fill["fee"] for fill in fills)
            if len({fill["id"] for fill in fills}) != len(fills):
                raise GuardError("duplicate fill ids")
            if not 0 <= total_units <= intent.units or total_units < row["filled"]:
                raise GuardError("fill quantity inconsistent")
            if total_fees < 0 or total_cost < 0:
                raise GuardError("negative fill accounting")
            if total_cost < row["cost"] or total_fees < row["fees"]:
                raise GuardError("broker fill accounting moved backwards")
            stored_ids = {
                item[0] for item in db.execute("SELECT id FROM fills WHERE order_key=?", (key,))
            }
            if not stored_ids.issubset({fill["id"] for fill in fills}):
                raise GuardError("previously applied fills missing")
            if total_cost > intent.price * total_units // 100 and intent.action == "buy":
                raise GuardError("fill exceeds confirmed limit")
            if total_cost < intent.price * total_units // 100 and intent.action == "sell":
                raise GuardError("exit fill below confirmed limit")
            if total_cost + total_fees > row["reserved"] and intent.action == "buy":
                raise GuardError("fill debit exceeds reservation")
            if total_fees > fee_reserve(intent.units):
                raise GuardError("fee bound exceeded")
            position = db.execute(
                "SELECT * FROM positions WHERE ticker=?", (intent.ticker,)
            ).fetchone()
            units = position["units"] if position else 0
            cost = position["cost"] if position else 0
            state = self.state(db)
            for fill in fills:
                stored = db.execute(
                    "SELECT body,order_key FROM fills WHERE id=?", (fill["id"],)
                ).fetchone()
                encoded = json.dumps(fill, sort_keys=True)
                if stored:
                    if stored[0] != encoded or stored[1] != key:
                        raise GuardError("broker changed an already applied fill")
                    continue
                if fill["units"] <= 0 or fill["fee"] < 0 or fill["cost"] < 0:
                    raise GuardError("invalid fill")
                limit_cost = intent.price * fill["units"] // 100
                if (intent.action == "buy" and fill["cost"] > limit_cost) or (
                    intent.action == "sell" and fill["cost"] < limit_cost
                ):
                    raise GuardError("individual fill violates confirmed limit")
                if intent.action == "buy":
                    if position and units and position["side"] != intent.side:
                        raise GuardError("fill would offset opposite position")
                    units += fill["units"]
                    cost += fill["cost"] + fill["fee"]
                else:
                    if not position or position["side"] != intent.side or units < fill["units"]:
                        raise GuardError("sell fill exceeds held position")
                    removed_cost = cost * fill["units"] // units
                    pnl = fill["cost"] - fill["fee"] - removed_cost
                    state["pnl"] += pnl
                    state["loss"] += max(0, -pnl)
                    units -= fill["units"]
                    cost -= removed_cost
                db.execute("INSERT INTO fills VALUES(?,?,?)", (fill["id"], key, encoded))
                self.event(db, now, "fill", {"key": key, **fill})
            db.execute(
                "INSERT INTO positions VALUES(?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
                "side=excluded.side,units=excluded.units,cost=excluded.cost",
                (intent.ticker, intent.side, units, cost),
            )
            db.execute(
                "UPDATE orders SET status=?,order_id=?,filled=?,cost=?,fees=?,updated=? "
                "WHERE key=?",
                (
                    status if status != "resting" or maker else "accepted",
                    order_id,
                    total_units,
                    total_cost,
                    total_fees,
                    now,
                    key,
                ),
            )
            state["account_ms"] = 0
            self.save_state(db, state)
            self.event(db, now, "reconciled", {"key": key, "status": status})

    def settle(self, ticker: str, yes_value: int, now: int) -> None:
        if not 0 <= yes_value <= DOLLAR:
            raise GuardError("invalid settlement value")
        with self.transaction() as db:
            if self.unresolved(db) or ticker in self.resting_remainders(db):
                raise GuardError("orders must be reconciled before settlement")
            existing = db.execute(
                "SELECT body FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            if existing:
                if json.loads(existing[0])["yes_value"] != yes_value:
                    raise GuardError("settlement changed")
                return
            row = db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if not row or not row["units"]:
                return
            value = yes_value if row["side"] == "yes" else DOLLAR - yes_value
            pnl = value * row["units"] // 100 - row["cost"]
            state = self.state(db)
            state["pnl"] += pnl
            state["loss"] += max(0, -pnl)
            state["account_ms"] = 0
            self.save_state(db, state)
            body = {"yes_value": yes_value, "units": row["units"], "pnl": pnl}
            db.execute("INSERT INTO settlements VALUES(?,?)", (ticker, json.dumps(body)))
            db.execute("UPDATE positions SET units=0,cost=0 WHERE ticker=?", (ticker,))
            self.event(db, now, "settlement", {"ticker": ticker, **body})

    def status(self) -> dict[str, Any]:
        with self.connection() as db:
            state = self.state(db)
            self.limits = Limits.model_validate(state["limits"])
            spent, exposure = self.totals(db)
            return {
                "paused": state["paused"],
                "autopilot": bool(state.get("autopilot", False)),
                "account_updated_ms": state["account_ms"],
                "error": state["error"],
                "limits": self.limits.model_dump(),
                "purchase_spend": dollars(spent),
                "remaining_purchase_budget": dollars(
                    max(0, self.limits.purchase_spend_cents * CENT - spent)
                ),
                "open_exposure": dollars(exposure),
                "realized_loss": dollars(state["loss"]),
                "realized_pnl": dollars(state["pnl"]),
                "available_balance": dollars(state["balance"]),
                "unknown_or_pending": self.unresolved(db),
                "longshot": bool(state.get("longshot", False)),
                "resting_orders": len(self.resting_remainders(db)),
                "positions": [
                    dict(row) for row in db.execute("SELECT * FROM positions WHERE units>0")
                ],
                "unattended_enabled": False,
            }
