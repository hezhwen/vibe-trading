"""Live trading context — singleton lifecycle manager.

Manages initialization, wiring, and lifecycle of all live trading components.
Creates a single entry point for AgentLoop tools to interact with live trading.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .calendar import TradingCalendar
from .checkpoint import CheckpointManager
from .market_data import AKShareMarketDataProvider
from .broker import DummyBrokerAdapter
from .position_book import PositionBook
from .risk import RiskEngine, RiskLimits
from .paper_trader import PaperTrader
from .oms import OrderManager
from .scheduler import DataScheduler
from .data_quality import DataQualityChecker
from backtest.engines.china_a_v2 import ChinaAEngineV2


@dataclass
class LiveTradingConfig:
    """Configuration for live trading components."""
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.00025    # 万2.5
    commission_min: float = 5.0
    slippage_base: float = 0.0005
    slippage_k: float = 0.1
    poll_interval: float = 3.0           # market data poll seconds
    checkpoint_dir: str = "data/live_state"
    shadow_dir: str = "data/shadow_runs"
    max_position_pct: float = 0.25
    max_drawdown_pct: float = 0.20
    max_daily_loss: float = 50_000.0
    max_order_value: float = 5_000_000.0

    @classmethod
    def from_file(cls, path: str | Path) -> "LiveTradingConfig":
        """Load config from JSON file."""
        with open(path) as f:
            d = json.load(f)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_file(self, path: str | Path) -> None:
        """Save config to JSON file."""
        with open(path, "w") as f:
            json.dump({
                "initial_cash": self.initial_cash,
                "commission_rate": self.commission_rate,
                "commission_min": self.commission_min,
                "slippage_base": self.slippage_base,
                "slippage_k": self.slippage_k,
                "poll_interval": self.poll_interval,
                "checkpoint_dir": self.checkpoint_dir,
                "shadow_dir": self.shadow_dir,
                "max_position_pct": self.max_position_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_daily_loss": self.max_daily_loss,
                "max_order_value": self.max_order_value,
            }, f, indent=2, ensure_ascii=False)


class LiveTradingContext:
    """Singleton managing the lifecycle of all live trading components.

    Lazily initializes components on first use. Components can be
    individually disabled (returns None) when their dependencies
    are not available.
    """

    _instance: "LiveTradingContext | None" = None

    def __init__(self, config: LiveTradingConfig | None = None):
        self._config = config or LiveTradingConfig()
        self._calendar = None
        self._market_data = None
        self._checkpoint = None
        self._paper_trader = None
        self._broker = None
        self._oms = None
        self._position_book = None
        self._risk_engine = None
        self._scheduler = None
        self._data_quality = None
        self._initialized = False

    @classmethod
    def get_instance(cls, config: LiveTradingConfig | None = None) -> "LiveTradingContext":
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls(config)
        elif config is not None:
            cls._instance._config = config
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    # ── Component accessors (lazy init) ────────────────────────

    @property
    def calendar(self):
        if self._calendar is None:
            self._calendar = TradingCalendar()
        return self._calendar

    @property
    def market_data(self):
        if self._market_data is None:
            self._market_data = AKShareMarketDataProvider(
                poll_interval=self._config.poll_interval
            )
        return self._market_data

    @property
    def checkpoint(self):
        if self._checkpoint is None:
            self._checkpoint = CheckpointManager(self._config.checkpoint_dir)
        return self._checkpoint

    @property
    def position_book(self):
        if self._position_book is None:
            self._position_book = PositionBook(
                calendar=self.calendar,
                shadow_dir=self._config.shadow_dir,
                initial_cash=self._config.initial_cash,
            )
        return self._position_book

    @property
    def risk_engine(self):
        if self._risk_engine is None:
            limits = RiskLimits(
                max_position_pct=self._config.max_position_pct,
                max_drawdown_pct=self._config.max_drawdown_pct,
                max_daily_loss=self._config.max_daily_loss,
                max_order_value=self._config.max_order_value,
            )
            self._risk_engine = RiskEngine(
                limits=limits,
                position_book=self.position_book,
                calendar=self.calendar,
            )
        return self._risk_engine

    @property
    def paper_trader(self):
        if self._paper_trader is None:
            engine = ChinaAEngineV2(
                config={
                    "commission_rate": self._config.commission_rate,
                    "commission_min": self._config.commission_min,
                    "slippage_base": self._config.slippage_base,
                    "slippage_k": self._config.slippage_k,
                },
            )
            self._paper_trader = PaperTrader(
                engine=engine,
                calendar=self.calendar,
                market_data=self.market_data,
                initial_cash=self._config.initial_cash,
                on_fill=self._on_paper_fill,
            )
        return self._paper_trader

    @property
    def broker(self):
        if self._broker is None:
            self._broker = DummyBrokerAdapter()
        return self._broker

    @property
    def oms(self):
        if self._oms is None:
            self._oms = OrderManager(
                broker=self.broker,
                risk_engine=self.risk_engine,
                checkpoint=self.checkpoint,
                position_book=self.position_book,
            )
        return self._oms

    @property
    def scheduler(self):
        if self._scheduler is None:
            self._scheduler = DataScheduler(self.calendar)
        return self._scheduler

    @property
    def data_quality(self):
        if self._data_quality is None:
            self._data_quality = DataQualityChecker(self.calendar)
        return self._data_quality

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self, session_id: str | None = None) -> str:
        """Start all live trading components."""
        if self._initialized:
            return self.checkpoint.session_id or ""

        sid = self.checkpoint.init_session(session_id)

        # Try to recover from previous session
        snap = self.checkpoint.recover()
        if snap is not None:
            import logging
            logging.getLogger(__name__).info(
                f"Recovered session {snap.session_id} at sequence {snap.sequence}"
            )

        self.market_data.start()
        self.scheduler.start()
        self._initialized = True
        return sid

    def stop(self) -> None:
        """Stop all live trading components gracefully."""
        if self.market_data:
            self.market_data.stop()
        if self.scheduler:
            self.scheduler.stop()
        self._initialized = False

    # ── Internal callbacks ─────────────────────────────────────

    def _on_paper_fill(self, order) -> None:
        """Handle paper trading fill: update position book."""
        try:
            from datetime import datetime
            self.position_book.on_fill(
                symbol=order.symbol,
                side=order.side,
                qty=order.filled_qty,
                price=order.avg_fill_price,
                commission=order.commission,
                timestamp=datetime.now(),
                signal_source=order.signal_source,
            )
        except Exception:
            pass
