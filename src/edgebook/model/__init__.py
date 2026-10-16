"""Deterministic pricing model: volatility forecast, settlement pricer, calibration.

Everything here is a pure function of its inputs (no I/O, no clocks), so the
backtest, paper trader and live trader all price identically.
"""
