"""Deribit options-implied odds vs Kalshi's hourly ladder: fetcher, model, rule, report."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import fill_fee
from edgebook.research import deribit
from edgebook.research import options_implied as oi

T0 = 1_790_409_600  # an hour boundary (2026-10-01 08:00 UTC)
SIGMA = 0.40


# ---------------------------------------------------------------- deribit


def test_parse_instrument() -> None:
    o = deribit.parse_instrument("BTC-1OCT26-80000-C")
    assert o == deribit.Option(1_790_841_600, 80_000.0, True)
    assert deribit.parse_instrument("BTC-17SEP26-71000-P") is not None
    assert deribit.parse_instrument("BTC-PERPETUAL") is None
    assert deribit.parse_instrument("BTC-17XXX26-71000-P") is None


def _trade(i: int, ts_ms: int) -> dict[str, Any]:
    return {
        "trade_id": str(i),
        "timestamp": ts_ms,
        "instrument_name": "BTC-2OCT26-80000-C",
        "iv": 40.0,
        "price": 0.01,
        "mark_price": 0.011,
        "index_price": 80_000.0,
        "amount": 1.0,
        "direction": "buy",
    }


def test_backfill_pages_both_sources_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deribit, "PAUSE_S", 0.0)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        q = dict(req.url.params)
        if req.url.path.endswith("get_volatility_index_data"):
            end = int(q["end_timestamp"])
            if end > T0 * 1000 + 120_000:  # first page: newest two minutes + cursor
                data = [[T0 * 1000 + 120_000, 1, 1, 1, 41.0], [T0 * 1000 + 60_000, 1, 1, 1, 40.0]]
                return httpx.Response(
                    200, json={"result": {"data": data, "continuation": T0 * 1000 + 60_000}}
                )
            return httpx.Response(200, json={"result": {"data": [[T0 * 1000, 1, 1, 1, 39.0]]}})
        start = int(q["start_timestamp"])
        if start < T0 * 1000 + 5000:
            page = [_trade(1, T0 * 1000 + 1000), _trade(2, T0 * 1000 + 5000)]
            return httpx.Response(200, json={"result": {"trades": page, "has_more": True}})
        page = [_trade(2, T0 * 1000 + 5000), _trade(3, T0 * 1000 + 9000)]
        return httpx.Response(200, json={"result": {"trades": page, "has_more": False}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dv, tr = deribit.backfill(tmp_path, T0, T0 + 600, client)
    assert (dv, tr) == (3, 3)  # duplicate trade 2 across pages is stored once
    dvol = pq.read_table(tmp_path / deribit.DVOL_FILE).to_pylist()
    assert [r["ts"] for r in dvol] == [T0 + 60, T0 + 120, T0 + 180]  # close times
    # resume: nothing new
    assert deribit.backfill(tmp_path, T0, T0 + 600, client) == (0, 0)
    assert any("history" not in c for c in calls)


def test_get_retries_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deribit, "PAUSE_S", 0.0)
    n = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectError("boom")
        if n["calls"] == 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"result": {"ok": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert deribit._get(client, "https://x.test/a", {}) == {"ok": 1}
    dead = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError):
        deribit._get(dead, "https://x.test/a", {})


def test_deribit_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(deribit, "backfill", lambda d, s, e, c: (1, 2))
    deribit.main(["--dir", str(tmp_path), "--start", "2026-10-01", "--end", "2026-10-02"])
    assert "added 1 DVOL minutes and 2 option trades" in capsys.readouterr().out


# ---------------------------------------------------------------- model


def test_effective_tau_discounts_the_averaging_minute() -> None:
    assert oi.effective_tau(3600) * oi.YEAR_S == pytest.approx(3600 - 40)
    assert oi.effective_tau(10) * oi.YEAR_S == 1.0


def test_p_above_is_driftless_and_shift_moves_it() -> None:
    tau = oi.effective_tau(3600)
    p = oi.p_above(80_000, 80_000, SIGMA, tau)
    assert p == pytest.approx(0.5, abs=0.01)
    assert p < 0.5
    assert oi.p_above(80_000, 80_000, SIGMA, tau, 0.5) > 0.65


def test_ladder_fit_recovers_vol_and_level() -> None:
    tau = oi.effective_tau(1800)
    strikes = [79_500.0, 79_800.0, 80_000.0, 80_200.0, 80_500.0]
    mids = [oi.p_above(80_000, k, 0.5, tau, 0.1) for k in strikes]
    fit = oi.fit_ladder(80_000, strikes, mids, tau)
    assert fit is not None
    assert fit.sigma == pytest.approx(0.5, rel=1e-6)
    assert fit.shift_sd == pytest.approx(0.1, abs=1e-6)
    assert oi.fit_ladder(80_000, strikes[:2], mids[:2], tau) is None
    assert oi.fit_ladder(80_000, [80_000.0] * 3, [0.5] * 3, tau) is None
    assert oi.fit_ladder(80_000, strikes, mids[::-1], tau) is None  # upside-down ladder


def test_implied_vol_round_trips_and_rejects_bad_prices() -> None:
    t = 0.5 / 365
    for call in (True, False):
        px = oi.bs_price(80_000, 80_500, 0.45, t, call)
        assert oi.implied_vol(px, 80_000, 80_500, t, call) == pytest.approx(0.45, rel=1e-6)
    assert oi.implied_vol(1.0, 80_000, 79_000, t, True) is None  # below intrinsic
    assert oi.implied_vol(1.0, 80_000, 80_000, 0, True) is None
    assert oi.implied_vol(79_000, 80_000, 80_000, t, True) is None  # no root below 600%


def _option_rows(t0: int, t1: int, sigma: float = SIGMA) -> list[dict[str, Any]]:
    """Near-ATM calls/puts on the next 08:00 expiry every 2 minutes, plus noise rows."""
    rows = []
    for i, ts in enumerate(range(t0, t1, 120)):
        expiry = ts - ts % 86400 + 8 * 3600
        if expiry - ts < 2 * 3600:
            expiry += 86400
        day = datetime.fromtimestamp(expiry, UTC)
        mon = next(k for k, v in deribit.MONTHS.items() if v == day.month)
        call = i % 2 == 0
        name = f"BTC-{day.day}{mon}{day.year % 100}-80000-{'C' if call else 'P'}"
        t = (expiry - ts) / oi.YEAR_S
        mark = oi.bs_price(80_000, 80_000, sigma, t, call) / 80_000
        rows.append(
            {
                "trade_id": str(i),
                "ts_ms": ts * 1000,
                "instrument": name,
                "iv": sigma * 100,
                "price": mark,
                "mark_price": mark,
                "index_price": 80_000.0,
                "amount": 1.0,
                "direction": "buy",
            }
        )
    rows.append({**rows[0], "trade_id": "x", "instrument": "BTC-PERPETUAL"})
    rows.append({**rows[0], "trade_id": "y", "mark_price": None})
    return rows


def test_atm_iv_uses_recent_front_expiry_trades() -> None:
    ot = oi.OptionTrades.from_rows(_option_rows(T0, T0 + 7200))
    atm = ot.atm(T0 + 3600)
    assert atm is not None
    iv, expiry, idx = atm
    assert iv == pytest.approx(SIGMA, rel=1e-4)
    assert expiry > T0 + 3600
    assert idx == 80_000.0
    assert ot.atm(T0 - 7200) is None  # no trades yet


def test_series_is_point_in_time() -> None:
    s = oi.Series(np.array([100, 160]), np.array([1.0, 2.0]))
    assert s.at(159) == 1.0
    assert s.at(160) == 2.0
    assert s.at(99) is None
    assert s.at(1000) is None


def test_seasonal_factor_scales_busy_hours_up() -> None:
    ts = np.arange(T0, T0 + 3 * 86400, 60)
    rng = np.random.default_rng(1)
    hour = (ts // 3600) % 24
    r = rng.normal(0, np.where(hour == 14, 0.003, 0.001))
    spot = oi.Series(ts, 80_000 * np.exp(np.cumsum(r)))
    w = oi.hourly_weights(spot, int(ts[-1]))
    assert w.mean() == pytest.approx(1.0)
    assert w[14] > 3
    busy_close = T0 - T0 % 86400 + 15 * 3600
    f = oi.seasonal_factor(w, busy_close - 3600, busy_close, busy_close + 17 * 3600)
    assert f > 1.5
    quiet = oi.seasonal_factor(w, busy_close, busy_close + 3600, busy_close + 17 * 3600)
    assert quiet < 1


def test_hourly_weights_fill_missing_hours() -> None:
    ts = np.arange(T0, T0 + 3600, 60)  # a single hour of data
    spot = oi.Series(ts, 80_000 + np.arange(ts.size, dtype=float))
    w = oi.hourly_weights(spot, T0 + 7200)
    assert np.allclose(w, 1.0)


# ---------------------------------------------------------------- rule


def row(**kw: Any) -> oi.Row:
    base: dict[str, Any] = {
        "event": "E",
        "ticker": "E-T80000",
        "close_ts": T0,
        "minutes_left": 30,
        "strike": 80_000.0,
        "spot": 80_000.0,
        "bid": 0.40,
        "ask": 0.42,
        "yes": True,
        "sigma_k": SIGMA,
        "shift_k": 0.0,
        "sigma": {"opt": SIGMA, "opt_seasonal": SIGMA, "dvol": SIGMA},
        "deribit_index": 80_000.0,
        "future": (),
    }
    base.update(kw)
    return oi.Row(**base)


def test_taker_signal_buys_the_cheap_side_after_fees() -> None:
    r = row()  # options say ~0.5, YES offered at 0.42
    tr = oi.signal(r, oi.Rule("opt", 0.03, "taker"))
    assert tr is not None
    assert tr.side == "yes"
    assert tr.price == 0.42
    fee = fill_fee(0.42, oi.SIZE, LiquidityRole.TAKER) / oi.SIZE
    assert tr.pnl == pytest.approx(1 - 0.42 - fee)
    assert oi.signal(r, oi.Rule("opt", 0.10, "taker")) is None
    no = oi.signal(row(bid=0.58, ask=0.60, yes=True), oi.Rule("opt", 0.03, "taker"))
    assert no is not None
    assert no.side == "no"
    assert no.pnl < 0
    assert oi.signal(row(sigma={}), oi.Rule("opt", 0.0, "taker")) is None
    assert oi.signal(row(bid=0.0, ask=0.02), oi.Rule("opt", 0.0, "taker")) is not None


def test_maker_signal_needs_a_fill() -> None:
    rule = oi.Rule("opt", 0.03, "maker")
    assert oi.signal(row(), rule) is None  # no later prints
    hit = row(future=((T0 + 60, 0.39, 0.40, 0.40, 5.0),))
    tr = oi.signal(hit, rule)
    assert tr is not None
    assert tr.side == "yes"
    assert tr.price == 0.40
    assert oi.maker_filled(row(future=((T0 + 60, 0.42, 0.44, 0.43, 5.0),)), "no")
    assert not oi.maker_filled(row(future=((T0 + 60, 0.40, 0.42, None, 5.0),)), "no")


def test_run_takes_the_first_firing_per_market() -> None:
    rows = [
        row(minutes_left=30),
        row(minutes_left=50, bid=0.48, ask=0.50),  # no edge
        row(minutes_left=40, yes=False),
        row(ticker="E-T2", minutes_left=20),
    ]
    trades = oi.run(rows, oi.Rule("opt", 0.03, "taker"))
    assert sorted((t.ticker, t.minutes_left) for t in trades) == [("E-T2", 20), ("E-T80000", 40)]


def test_by_close_resamples_closes() -> None:
    t = [
        oi.Trade("E", "a", T0, 30, "yes", 0.4, 0.05, 0.5),
        oi.Trade("E", "b", T0, 30, "yes", 0.4, 0.05, -0.4),
        oi.Trade("F", "c", T0 + 3600, 30, "yes", 0.4, 0.05, 0.3),
    ]
    v = oi.by_close(t, [T0, T0 + 3600, T0 + 7200], n_boot=200)
    assert v.trades == 3
    assert v.closes_traded == 2
    assert v.closes_total == 3
    assert v.cents_per_contract == pytest.approx(100 * 0.4 / 3)
    assert v.ci_lo <= v.cents_per_contract <= v.ci_hi
    assert v.worst_close_dollars == pytest.approx(0.1)
    assert v.best_close_dollars == 0.3
    assert v.days == pytest.approx(3 / 24)
    empty = oi.by_close([], [T0])
    assert empty.trades == 0
    assert math.isnan(empty.cents_per_contract)


# ---------------------------------------------------------------- end to end


def _world(tmp: Path, n_events: int = 9) -> tuple[Path, Path]:
    """A synthetic Kalshi ladder, Coinbase spot, DVOL and option trades."""
    rng = np.random.default_rng(3)
    start = T0 - 86400
    ts = np.arange(start, T0 + n_events * 3600 + 120, 60)
    sd_min = SIGMA / math.sqrt(oi.YEAR_S / 60)
    px = 80_000 * np.exp(np.cumsum(rng.normal(0, sd_min, ts.size)))
    spot = dict(zip(ts.tolist(), px.tolist(), strict=True))
    kd, dd = tmp / "k", tmp / "d"
    kd.mkdir()
    dd.mkdir()
    pq.write_table(pa.table({"ts": ts, "close": px}), kd / "spot_1m_BTC-USD.parquet")
    pq.write_table(
        pa.table({"ts": ts, "dvol": np.full(ts.size, SIGMA * 100)}), dd / deribit.DVOL_FILE
    )
    pq.write_table(
        pa.Table.from_pylist(_option_rows(start, int(ts[-1])), schema=deribit.TRADES),
        dd / deribit.TRADES_FILE,
    )
    markets, candles = [], []
    for e in range(n_events):
        close = T0 + (e + 1) * 3600
        settle = spot[close]
        centre = round(spot[close - 3600], -2)
        for k in (centre - 200, centre - 100, centre, centre + 100, centre + 200):
            tk = f"KXBTCD-{e}-T{k}"
            markets.append(
                {
                    "series": "KXBTCD",
                    "event_ticker": f"KXBTCD-{e}",
                    "ticker": tk,
                    "open_ts": close - 3600,
                    "close_ts": close,
                    "strike_type": "greater",
                    "floor": float(k),
                    "cap": None,
                    "result": "yes" if settle > k else "no",
                    "expiration_value": settle,
                }
            )
            for t in range(close - 3600 + 60, close + 1, 60):
                p = oi.p_above(spot[t], k, SIGMA * 1.1, oi.effective_tau(close - t))
                bid = max(round(p - 0.01 + rng.normal(0, 0.03), 2), 0.01)
                candles.append(
                    {
                        "ticker": tk,
                        "end_ts": t,
                        "bid": bid,
                        "ask": min(bid + 0.02, 0.99),
                        "last": bid + 0.01,
                        "volume": 3.0,
                    }
                )
    pq.write_table(pa.Table.from_pylist(markets), kd / "kalshi_markets_KXBTCD.parquet")
    pq.write_table(pa.Table.from_pylist(candles), kd / "kalshi_candles_KXBTCD.parquet")
    return kd, dd


def test_build_and_report_end_to_end(tmp_path: Path) -> None:
    kd, dd = _world(tmp_path)
    data = oi.build(kd, dd)
    assert data.rows
    assert all(r.minutes_left in oi.HORIZONS_MIN for r in data.rows)
    assert len(data.basis_bps["brti_vs_coinbase"]) == 9
    r0 = data.rows[0]
    assert r0.sigma["opt"] == pytest.approx(SIGMA, rel=1e-3)
    assert r0.sigma_k is not None
    assert 0.2 < r0.sigma_k < 0.9
    train, test, cut = oi.split(data.rows)
    assert train
    assert test
    assert max(r.close_ts for r in train) < cut <= test[0].close_ts
    cmp = oi.compare(train)
    assert set(cmp["scores"]) >= {"kalshi_mid", "vol_swap", "level_swap", "opt_seasonal"}
    assert oi.compare([]) == {"n": 0}

    out = tmp_path / "train.json"
    rep = oi.main(
        ["--kalshi-dir", str(kd), "--deribit-dir", str(dd), "--out", str(out), "--train-only"],
        out=lambda s: None,
    )
    assert "test_verdict" not in rep
    assert rep["selected_on_train"]["variant"] in oi.VARIANTS
    assert len(rep["train_grid"]) == len(oi.grid())

    full = tmp_path / "full.json"
    rep = oi.main(
        [
            "--kalshi-dir",
            str(kd),
            "--deribit-dir",
            str(dd),
            "--out",
            str(full),
            "--rule",
            "opt:0.02:taker",
        ],
        out=lambda s: None,
    )
    assert rep["registered"] == {"variant": "opt", "threshold": 0.02, "role": "taker"}
    assert set(rep["realized_width_test"]) >= {"kalshi", "opt"}
    saved = json.loads(full.read_text())
    assert saved["test_verdict"]["closes_total"] == len({r.close_ts for r in test})
    # the 1-contract fee is rounded up to a whole cent, so it can only cost more
    a, b = saved["test_verdict"], saved["test_verdict_1_contract_fee"]
    if a["trades"]:
        assert b["cents_per_contract"] <= a["cents_per_contract"] + 1e-9
