#!/usr/bin/env bash
# Refresh the committed Kalshi API spec snapshot used by tests/test_kalshi_spec_drift.py.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in openapi.yaml asyncapi.yaml; do
  curl -fsSL "https://docs.kalshi.com/$f" -o "tests/fixtures/kalshi/$f"
done
