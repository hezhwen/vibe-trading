"""Live trading module for A-share real-time trading.

Provides:
  - Trading calendar with session phase awareness
  - Market data bus (AKShare polling, extensible to WebSocket)
  - Paper trading sandbox (reuses V2 engine execution models)
  - Broker adapter interface (with DummyBrokerAdapter for testing)
  - Order Management System with state machine and WAL persistence
  - Position tracking with T+1 awareness
  - Risk engine with pre-trade checks and circuit breaker
  - Data refresh scheduler for ETL pipelines
  - Data quality checks

Quick start:
    from live.context import LiveTradingContext, LiveTradingConfig

    config = LiveTradingConfig(initial_cash=1_000_000.0)
    ctx = LiveTradingContext.get_instance(config)
    ctx.start()

    # Use paper trader
    pt = ctx.paper_trader
    order = pt.submit_order("600519", "buy", 100, order_type="market")

    ctx.stop()
"""

from __future__ import annotations


def check_available() -> bool:
    """Check if live trading prerequisites are available.

    Returns True if at minimum AKShare is installed (for market data).
    Real broker SDKs are optional — DummyBrokerAdapter is used otherwise.
    """
    try:
        import akshare  # noqa: F401
        return True
    except ImportError:
        return False


__version__ = "0.1.0"
