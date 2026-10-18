"""Pro-terminal style market read handed to Jev: trade tape, book depth, index momentum."""

import pytest

from edgebook.engine.state import LiveState
from tests.test_engine import NOW, world


def test_flow_summarises_tape_book_and_momentum() -> None:
    s = world(spot=82_000.0, secs=400)
    s.add_trade("M", 0.10, 5, "no", NOW - 900_000)  # too old (prints arrive in time order)
    s.add_trade("M", 0.30, 60, "yes", NOW - 200_000)  # only in the 5-minute window
    s.add_trade("M", 0.42, 10, "no", NOW - 50_000)  # bought DOWN
    s.add_trade("M", 0.40, 30, "yes", NOW - 30_000)  # bought UP
    s.add_trade("OTHER", 0.90, 999, "yes", NOW - 1_000)  # another market: ignored
    flow = s.flow("M", NOW)
    minute = flow["tape"]["last_60s"]
    assert minute == {
        "trades": 2,
        "contracts": 40,
        "share_bought_up": 0.75,
        "avg_up_price": pytest.approx(0.405),
    }
    five = flow["tape"]["last_5m"]
    assert five["trades"] == 3
    assert five["share_bought_up"] == pytest.approx(0.9)
    assert flow["tape"]["last_trade"] == {
        "up_price": 0.40,
        "contracts": 30,
        "buyer": "up",
        "age_s": 30.0,
    }
    assert flow["book"]["buy_up"] == [[0.5, 500.0]]
    assert flow["book"]["buy_down"] == [[0.52, 500.0]]
    assert flow["book"]["up_share_of_depth"] == 0.5
    assert flow["momentum"]["index_change_5m_bps"] is not None


def test_flow_with_no_data() -> None:
    flow = LiveState().flow("M", NOW)
    assert flow["tape"]["last_60s"] == {
        "trades": 0,
        "contracts": 0,
        "share_bought_up": None,
        "avg_up_price": None,
    }
    assert flow["tape"]["last_trade"] is None
    assert flow["book"] == {"buy_up": [], "buy_down": [], "up_share_of_depth": None}
    assert flow["momentum"] == {"index_change_60s_bps": None, "index_change_5m_bps": None}
