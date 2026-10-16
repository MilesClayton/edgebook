"""Our Kalshi wire models must accept Kalshi's own published examples.

Runs against the committed snapshot in tests/fixtures/kalshi by default, or
against freshly downloaded specs when KALSHI_SPEC_DIR is set (nightly job).
"""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from edgebook.venues.kalshi.models import WS_MESSAGE, Balance, MarketsPage, RestMarket

SPEC_DIR = Path(os.environ.get("KALSHI_SPEC_DIR", Path(__file__).parent / "fixtures" / "kalshi"))
WS_TYPES = {"orderbook_snapshot", "orderbook_delta", "trade", "cfbenchmarks_value"}


def load(name: str) -> Any:
    return yaml.safe_load((SPEC_DIR / name).read_text())


def ws_examples() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for name, message in load("asyncapi.yaml")["components"]["messages"].items():
        for ex in message.get("examples", []):
            payload = ex.get("payload", {})
            if isinstance(payload, dict) and payload.get("type") in WS_TYPES:
                out.append((f"{name}:{ex.get('name')}", payload))
    return out


EXAMPLES = ws_examples()


def test_spec_has_examples_for_every_channel_we_use() -> None:
    assert {p["type"] for _, p in EXAMPLES} == WS_TYPES


@pytest.mark.parametrize(("name", "payload"), EXAMPLES, ids=[n for n, _ in EXAMPLES])
def test_ws_examples_parse(name: str, payload: dict[str, Any]) -> None:
    msg = WS_MESSAGE.validate_python(payload)
    assert msg.type == payload["type"]


def _schema_fields(schema: str) -> tuple[set[str], set[str]]:
    s = load("openapi.yaml")["components"]["schemas"][schema]
    return set(s.get("properties", {})), set(s.get("required", []))


@pytest.mark.parametrize(
    ("model", "schema"),
    [(RestMarket, "Market"), (MarketsPage, "GetMarketsResponse"), (Balance, "GetBalanceResponse")],
)
def test_rest_models_match_openapi(model: type, schema: str) -> None:
    props, required = _schema_fields(schema)
    ours = model.model_fields  # type: ignore[attr-defined]
    missing = set(ours) - props
    assert not missing, f"{schema} no longer has {missing}"
    for name, field in ours.items():
        if field.is_required():
            assert name in required, f"{schema}.{name} is optional in the spec but required here"
