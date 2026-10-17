"""Closed-trade results survive the capped fill history, including for old files."""

import json
from pathlib import Path

import pytest

from edgebook.engine.paper import PaperAccount

NOW = 1_792_130_700_000


def test_closed_trades_kept_in_full_and_rebuilt_for_old_files(tmp_path: Path) -> None:
    acct = PaperAccount()
    acct.add_fill("A", "yes", 10, 0.5, 0.01, NOW, "x")
    acct.settle("A", "yes", NOW + 1)
    acct.fills.clear()  # as if the fill history had been capped away
    acct.save(tmp_path / "a.json")
    [(pnl, n, ts)] = PaperAccount.load(tmp_path / "a.json").closed
    assert pnl == pytest.approx(10 * (1 - 0.5 - 0.01))
    assert n == 10.0
    assert ts == NOW + 1
    old = json.loads((tmp_path / "a.json").read_text())
    del old["closed"]
    old["fills"] = [
        {
            "ts_ms": 1,
            "ticker": "B",
            "action": "SETTLE",
            "side": "no",
            "contracts": 5.0,
            "price": 0.0,
            "fees": 0.0,
            "cash_after": 0.0,
            "reason": "r",
            "pnl": -2.0,
        }
    ]
    (tmp_path / "old.json").write_text(json.dumps(old))
    assert PaperAccount.load(tmp_path / "old.json").closed == [(-2.0, 5.0, 1)]
    old["closed"] = [[-2.0, 5.0], [3.0, 1.0]]  # pre-timestamp records: time from the fill log
    (tmp_path / "old2.json").write_text(json.dumps(old))
    assert PaperAccount.load(tmp_path / "old2.json").closed == [(-2.0, 5.0, 1), (3.0, 1.0, 0)]
