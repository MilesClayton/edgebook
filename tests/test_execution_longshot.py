from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from edgebook.engine.decide import Call
from edgebook.engine.loop import LONGSHOT_DATA_GATES
from edgebook.engine.maker import LongshotConfig
from edgebook.engine.state import MarketInfo
from edgebook.execution.live_ledger import DOLLAR, GuardError, Intent, money
from edgebook.execution.longshot import longshot_quotes
from edgebook.execution.service import ExecutionService
from edgebook.recorder.http import make_app
from edgebook.venues.kalshi.auth import KalshiSigner
from tests.execution_helpers import NOW, TICKER, fill, intent, live_settings, ready
from tests.test_execution_broker import broker_for, fixture_routes, wire_order
from tests.test_execution_service import FixtureBroker, Harness

EVENT = TICKER.rsplit("-", 1)[0]
CLOSE_MS = NOW + 1_800_000


class RestingBroker(FixtureBroker):
    """Fixture broker whose resting orders wait for fills or a cancel."""

    def __init__(self, clock: list[int]) -> None:
        super().__init__(clock)
        self.bodies: list[dict[str, Any]] = []
        self.canceled: list[str] = []

    async def account(self) -> dict[str, Any]:
        account = await super().account()
        account["resting_client_ids"] = [
            cid for cid, order in self.orders.items() if order["status"] == "resting"
        ]
        return account

    async def submit(
        self,
        selected: Intent,
        client_id: str,
        deadline_ms: int | None = None,
        resting_until_s: int | None = None,
    ) -> dict[str, Any]:
        if resting_until_s is None:
            return await super().submit(selected, client_id, deadline_ms)
        self.submissions += 1
        self.bodies.append({"intent": selected, "expires_s": resting_until_s})
        identifier = f"resting-{self.submissions}"
        self.orders[client_id] = {
            "order_id": identifier,
            "client_order_id": client_id,
            "status": "resting",
        }
        self.fills[identifier] = []
        return {"order_id": identifier, "client_order_id": client_id}

    async def cancel(self, order_id: str) -> None:
        self.canceled.append(order_id)
        for order in self.orders.values():
            if order["order_id"] == order_id and order["status"] == "resting":
                order["status"] = "canceled"

    def fill_resting(self, number: int, selected: Intent, units: int) -> None:
        identifier = f"resting-{number}"
        self.fills[identifier].append(
            fill(f"{identifier}-fill-{len(self.fills[identifier])}", units, selected.limit_price)
        )
        filled = sum(f["units"] for f in self.fills[identifier])
        order = next(o for o in self.orders.values() if o["order_id"] == identifier)
        order["status"] = "executed" if filled == selected.units else "resting"
        sign = 1 if selected.side == "yes" else -1
        self.positions[selected.ticker] = self.positions.get(selected.ticker, 0) + sign * units


def sale(ticker: str = TICKER, price: str = "0.60", **updates: Any) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "series": "KXBTC15M",
        "side": "no",
        "price": money(price),
        "fair": 0.97,
        "seconds_left": 1500.0,
        "close_ms": CLOSE_MS,
        **updates,
    }


class Pilot:
    def __init__(self, path: Path, **settings: Any) -> None:
        self.harness = Harness(path)
        self.broker = RestingBroker(self.harness.clock)
        self.harness.broker = self.broker
        self.sales: list[dict[str, Any]] = [sale()]
        self.service = ExecutionService(
            live_settings(**settings),
            self.harness.ledger,
            self.broker,
            self.harness.source,
            lambda: self.harness.clock[0],
            proof=lambda: self.harness.proven,
            longshot_quotes=lambda: self.sales,
            longshot_proof=lambda: self.harness.proven,
        )

    async def start(self) -> None:
        await self.service.control("resume")
        await self.service.set_longshot(True)


@pytest.fixture
def pilot(tmp_path: Path) -> Pilot:
    return Pilot(tmp_path / "ledger.sqlite3")


async def test_pilot_is_off_until_switched_on(pilot: Pilot) -> None:
    await pilot.service.control("resume")
    assert await pilot.service.longshot_step() == []
    assert pilot.broker.submissions == 0
    harness = Harness(pilot.harness.ledger.path.with_name("other.sqlite3"))
    with pytest.raises(GuardError, match="not configured"):
        await harness.service.set_longshot(True)
    assert await harness.service.longshot_step() == []


async def test_pilot_rests_keeps_and_never_adds(pilot: Pilot) -> None:
    await pilot.start()
    notes = await pilot.service.longshot_step()
    assert notes[0]["outcome"] == "resting NO bid 0.6000 x 1.00"
    assert pilot.broker.bodies[0]["expires_s"] == CLOSE_MS // 1000 - 300
    status = await pilot.service.status()
    assert status["resting_orders"] == 1
    assert status["unknown_or_pending"] is False
    # the whole worst-case debit stays reserved while it rests
    assert status["open_exposure"] == "0.62760000"
    assert status["longshot_last"] == notes
    # unchanged: the order keeps its place in the queue
    assert await pilot.service.longshot_step() == []
    assert pilot.broker.submissions == 1
    # partly filled while resting, and the account read sees the fill first
    selected = Intent.model_validate(pilot.service.resting()[0]["intent"])
    pilot.broker.fill_resting(1, selected, 40)
    status = await pilot.service.status()
    assert status["paused"] is False
    assert status["positions"][0]["units"] == 40
    pilot.broker.fill_resting(1, selected, 60)
    await pilot.service.status()
    assert pilot.service.resting() == []
    # holding the market now: no second order on it
    assert await pilot.service.longshot_step() == []
    assert pilot.broker.submissions == 1


async def test_pilot_cancels_when_paper_stops_switch_off_and_pause(pilot: Pilot) -> None:
    await pilot.start()
    await pilot.service.longshot_step()
    pilot.sales = []
    notes = await pilot.service.longshot_step()
    assert notes[0]["outcome"].startswith("canceled")
    assert pilot.broker.canceled == ["resting-1"]
    status = await pilot.service.status()
    assert status["resting_orders"] == 0
    assert status["open_exposure"] == "0.00000000"
    assert status["purchase_spend"] == "0.00000000"
    # price changed: cancel and re-post
    pilot.sales = [sale()]
    await pilot.service.longshot_step()
    pilot.sales = [sale(price="0.59")]
    pilot.broker.no_bid = money("0.59")
    notes = await pilot.service.longshot_step()
    assert [n["outcome"][:8] for n in notes] == ["canceled", "resting "]
    await pilot.service.set_longshot(False)
    assert pilot.service.resting() == []
    await pilot.service.set_longshot(True)
    await pilot.service.longshot_step()
    await pilot.service.control("pause")
    assert pilot.service.resting() == []
    assert len(pilot.broker.canceled) == 4


async def test_pilot_limits(tmp_path: Path) -> None:
    pilot = Pilot(tmp_path / "a.sqlite3", longshot_event_cents=100)
    pilot.sales = [sale(f"{EVENT}-A"), sale(f"{EVENT}-B")]
    await pilot.start()
    notes = await pilot.service.longshot_step()
    assert notes[1]["outcome"] == "refused: per-close limit reached for this hour"
    capped = Pilot(tmp_path / "b.sqlite3", longshot_max_orders=1)
    capped.sales = [sale("KXBTC15M-A-1"), sale("KXBTC15M-B-1")]
    await capped.start()
    notes = await capped.service.longshot_step()
    assert notes[1]["outcome"] == "skipped: resting-order limit reached"
    # the account-wide caps (open exposure $3) still bind
    many = Pilot(tmp_path / "c.sqlite3")
    many.sales = [sale(f"KXBTC15M-E{i}-1") for i in range(5)]
    await many.start()
    notes = await many.service.longshot_step()
    assert [n["outcome"] for n in notes][-1] == "refused: open exposure cap"
    assert many.broker.submissions == 4


async def test_pilot_refuses_moved_book_late_markets_and_unproven(pilot: Pilot) -> None:
    await pilot.start()
    pilot.sales = [sale(price="0.59")]
    notes = await pilot.service.longshot_step()
    assert notes[0]["outcome"].startswith("refused: live book moved")
    pilot.sales = [sale(close_ms=NOW + 320_000)]
    notes = await pilot.service.longshot_step()
    assert notes[0]["outcome"].startswith("refused: too close")
    pilot.sales = [sale()]
    pilot.harness.proven = {"proven": False, "reason": "12 trades"}
    notes = await pilot.service.longshot_step()
    assert "paper strategy proves profitable: 12 trades" in notes[0]["outcome"]
    assert pilot.broker.submissions == 0
    waived = Pilot(pilot.harness.ledger.path.with_name("w.sqlite3"), require_paper_proof=False)
    waived.harness.proven = {"proven": False, "reason": "12 trades"}
    await waived.start()
    assert (await waived.service.longshot_step())[0]["outcome"].startswith("resting")
    with waived.harness.ledger.connection() as db:
        kinds = {row[0] for row in db.execute("SELECT kind FROM audit")}
    assert {"paper_proof_waived", "longshot_order", "longshot"} <= kinds


async def test_pilot_pauses_when_cancel_fails(pilot: Pilot) -> None:
    await pilot.start()
    await pilot.service.longshot_step()

    async def broken(order_id: str) -> None:
        raise OSError("offline")

    pilot.broker.cancel = broken  # type: ignore[method-assign]
    pilot.sales = []
    notes = await pilot.service.longshot_step()
    assert notes[0]["outcome"] == "cancel failed: OSError"
    assert (await pilot.service.status(refresh=False))["paused"] is True


def test_ledger_account_tolerates_only_our_resting_fills(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    order = ledger.reserve_resting(
        "longshot-1", intent(side="no", limit_price="0.6000"), {}, NOW, 5000
    )
    with pytest.raises(GuardError, match="pending"):  # not yet acknowledged
        ledger.account(NOW, 100 * DOLLAR, {})
    ledger.mark("longshot-1", "accepted", NOW, "o-1")
    ledger.reconcile("longshot-1", "o-1", "resting", [], NOW)
    assert ledger.orders()[0]["status"] == "resting"
    assert ledger.status()["unknown_or_pending"] is False
    cid = ExecutionService.client_id(order)
    ledger.account(NOW, 100 * DOLLAR, {TICKER: -40}, [cid])  # fill not yet applied: allowed
    with pytest.raises(GuardError, match="already resting"):
        ledger.reserve_resting("longshot-2", intent(side="no"), {}, NOW, 5000)
    with pytest.raises(GuardError, match="differ"):
        ledger.account(NOW, 100 * DOLLAR, {TICKER: -140}, [cid])  # more than it can fill
    with pytest.raises(GuardError, match="differ"):
        ledger.account(NOW, 100 * DOLLAR, {TICKER: 40}, [cid])  # wrong side
    with pytest.raises(GuardError, match="not placed here"):
        ledger.account(NOW, 100 * DOLLAR, {}, [cid, "someone-else"])
    with pytest.raises(GuardError, match="reconciled before settlement"):
        ledger.settle(TICKER, 0, NOW)
    with pytest.raises(GuardError, match="only rests buy"):
        ledger.reserve_resting("longshot-3", intent(action="sell"), {}, NOW, 5000)
    ledger.reconcile("longshot-1", "o-1", "canceled", [], NOW)
    assert ledger.status()["open_exposure"] == "0.00000000"


def test_ledger_resting_reservation_checks(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    ledger.reserve_resting("longshot-1", intent(), {}, NOW, 5000)
    with pytest.raises(GuardError, match="stale or unknown"):  # each order needs a fresh read
        ledger.reserve_resting("longshot-2", intent(), {}, NOW, 5000)
    ledger.mark("longshot-1", "rejected", NOW)
    ledger.account(NOW, 100 * DOLLAR, {})
    with pytest.raises(GuardError, match="key already used"):
        ledger.reserve_resting("longshot-1", intent(), {}, NOW, 5000)
    for bad, reason in [
        (intent(quantity="4.00"), "quantity cap"),
        (intent(quantity="2.00", limit_price="0.9000"), "per-order debit"),
    ]:
        with pytest.raises(GuardError, match=reason):
            ledger.reserve_resting("longshot-x", bad, {}, NOW, 5000)
    ledger.pause(NOW)
    with pytest.raises(GuardError, match="paused"):
        ledger.reserve_resting("longshot-y", intent(), {}, NOW, 5000)


async def test_broker_resting_order_cancel_and_account(signer: KalshiSigner) -> None:
    seen: list[httpx.Request] = []
    routes = fixture_routes()
    routes["/portfolio/orders"] = {"orders": [wire_order(client_order_id="ours")]}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        if request.method == "POST":
            return httpx.Response(201, json={"order_id": "o-1", "client_order_id": "c"})
        if request.method == "DELETE":
            return httpx.Response(404 if path.endswith("gone") else 200, json={})
        return httpx.Response(200, json=routes[path])

    broker = broker_for(signer, handle)
    assert (await broker.account())["resting_client_ids"] == ["ours"]
    await broker.submit(intent(side="no", limit_price="0.9500"), "c", resting_until_s=1234)
    body = json.loads(seen[-1].content)
    assert body["side"] == "ask"
    assert body["price"] == "0.0500"
    assert body["time_in_force"] == "good_till_canceled"
    assert body["post_only"] is True
    assert body["expiration_ts"] == 1234
    assert body["reduce_only"] is False
    with pytest.raises(GuardError, match="only buys may rest"):
        await broker.submit(intent(action="sell"), "c", resting_until_s=1234)
    await broker.cancel("o-1")
    assert seen[-1].method == "DELETE"
    assert seen[-1].url.path == "/trade-api/v2/portfolio/orders/o-1"
    assert seen[-1].url.params["subaccount"] == "2"
    assert "KALSHI-ACCESS-SIGNATURE" in seen[-1].headers
    await broker.cancel("gone")  # already canceled or filled: fine
    await broker.close()


def call(**updates: Any) -> Call:
    base: dict[str, Any] = {
        "ticker": TICKER,
        "series": "KXBTC15M",
        "action": "NO TRADE",
        "reason": "",
        "seconds_left": 1200.0,
        "strike": None,
        "index": None,
        "index_age_ms": 100,
        "fair_up": 0.03,
        "up_price": 0.05,
        "down_price": 0.96,
        "gates": dict.fromkeys(LONGSHOT_DATA_GATES, True),
    }
    return Call(**{**base, **updates})


def test_quotes_follow_the_paper_rule_and_data_checks() -> None:
    markets = {TICKER: MarketInfo(TICKER, "KXBTC15M", 0, CLOSE_MS // 1000, "greater", 1.0, None)}
    cfg, series = LongshotConfig(), ("KXBTC15M", "KXBTCD")

    def quotes(*calls: Call, tick_ms: int = NOW) -> list[dict[str, Any]]:
        return longshot_quotes(calls, markets, cfg, series, NOW, tick_ms, 3000)

    [q] = quotes(call())
    assert (q["side"], q["price"], q["close_ms"]) == ("no", money("0.95"), CLOSE_MS)
    assert quotes(call(), tick_ms=NOW - 60_000) == []  # paper engine stalled
    assert quotes(call(fair_up=0.06)) == []  # fair value says it is not overpriced
    assert quotes(call(gates={"market open": True})) == []
    assert quotes(call(index_age_ms=9000)) == []
    assert quotes(call(series="OTHER")) == []
    assert quotes(call(ticker="UNKNOWN")) == []


async def test_http_longshot_switch(pilot: Pilot, tmp_path: Path) -> None:
    settings = pilot.service.settings
    app = make_app(
        tmp_path, "dashboard-only", pilot.service, settings.api_token, settings.proposal_token
    )
    operator = {"Authorization": "Bearer " + settings.api_token}
    async with TestClient(TestServer(app)) as client:
        bad = await client.post(
            "/execution/longshot", json={"confirmed": True, "enabled": 1}, headers=operator
        )
        assert bad.status == 409
        ok = await client.post(
            "/execution/longshot", json={"confirmed": True, "enabled": True}, headers=operator
        )
        assert ok.status == 200
        assert (await ok.json())["longshot"] is True
        status = await (await client.get("/execution/status", headers=operator)).json()
        assert status["longshot_available"] is True
        assert status["longshot_paper_proof"]["strategy"] == "longshot"
