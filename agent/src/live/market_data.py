"""Market data bus for real-time A-share quotes.

Defines the Quote dataclass and abstract MarketDataProvider interface.
AKShareMarketDataProvider polls stock_zh_a_spot_em() for batch quotes
and dispatches to subscribers via callbacks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import pandas as pd

from .calendar import TradingCalendar


@dataclass(frozen=True)
class Quote:
    """Real-time market quote for a single A-share symbol.

    Provides all fields needed by ChinaAEngineV2:
    - can_execute: pct_chg, pre_close (for limit check)
    - can_fill: amount, pct_chg (for seal strength)
    - apply_slippage: amount (for ADV estimation)
    """
    symbol: str
    timestamp: datetime
    name: str = ""
    last_price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    volume: int = 0          # shares
    amount: float = 0.0      # CNY
    pct_chg: float = 0.0     # decimal (0.05 = 5%)
    bid_prices: tuple[float, ...] = ()
    bid_volumes: tuple[int, ...] = ()
    ask_prices: tuple[float, ...] = ()
    ask_volumes: tuple[int, ...] = ()
    turnover_rate: float = 0.0  # percentage (3.5 = 3.5%)

    @property
    def limit_up_price(self) -> float:
        """Calculated limit-up price based on pre_close and 10% default."""
        return round(self.pre_close * 1.10, 2)

    @property
    def limit_down_price(self) -> float:
        """Calculated limit-down price based on pre_close and 10% default."""
        return round(self.pre_close * 0.90, 2)

    def to_bar_series(self) -> pd.Series:
        """Convert to a bar-like pandas Series for V2 engine consumption."""
        return pd.Series({
            "open": self.open,
            "close": self.last_price,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
            "amount": self.amount,
            "volume": self.volume,
            "pct_chg": self.pct_chg * 100.0,  # V2 engine expects percentage form
        })


class MarketDataProvider(ABC):
    """Abstract market data interface.

    Concrete implementations: AKShareMarketDataProvider (polling),
    future: EastMoney WebSocket, QMT native quotes, etc.
    """

    @abstractmethod
    def get_realtime_quote(self, symbol: str) -> Quote | None:
        """Get a single real-time quote."""
        ...

    @abstractmethod
    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols at once."""
        ...

    @abstractmethod
    def subscribe(
        self, symbols: list[str], callback: Callable[[Quote], None]
    ) -> None:
        """Subscribe to quote updates for the given symbols."""
        ...

    @abstractmethod
    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from quote updates."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Clean up resources (connections, schedulers)."""
        ...


class AKShareMarketDataProvider(MarketDataProvider):
    """AKShare-based market data provider using polling.

    Uses stock_zh_a_spot_em() which returns all A-share spot quotes in one call.
    Subscriptions are simulated via periodic polling at configurable intervals.

    Minimum poll interval: 1s (rate limit consideration).
    """

    def __init__(self, poll_interval: float = 3.0):
        import logging
        self._logger = logging.getLogger(__name__)
        self._poll_interval = max(poll_interval, 1.0)
        self._callbacks: dict[str, list[Callable]] = {}
        self._scheduler = None
        self._cache: dict[str, Quote] = {}
        self._calendar = TradingCalendar()
        self._running = False

    def start(self) -> None:
        """Start the polling scheduler."""
        if self._running:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.add_job(
                self._poll,
                "interval",
                seconds=self._poll_interval,
                id="market_data_poll",
            )
            self._scheduler.start()
            self._running = True
        except ImportError:
            self._logger.warning("apscheduler not available; manual poll only")

    def stop(self) -> None:
        """Stop the polling scheduler."""
        self._running = False
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def close(self) -> None:
        self.stop()

    # ── Public API ─────────────────────────────────────────────

    def get_realtime_quote(self, symbol: str) -> Quote | None:
        """Get a quote from cache or fetch immediately."""
        if symbol in self._cache:
            return self._cache[symbol]
        quotes = self._fetch_all()
        return quotes.get(symbol)

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols."""
        all_quotes = self._fetch_all()
        return {s: all_quotes[s] for s in symbols if s in all_quotes}

    def subscribe(self, symbols: list[str], callback: Callable[[Quote], None]) -> None:
        """Register a callback for quote updates."""
        for sym in symbols:
            if sym not in self._callbacks:
                self._callbacks[sym] = []
            self._callbacks[sym].append(callback)

    def unsubscribe(self, symbols: list[str]) -> None:
        """Remove callbacks for the given symbols."""
        for sym in symbols:
            self._callbacks.pop(sym, None)

    # ── Internal polling ───────────────────────────────────────

    def _poll(self) -> None:
        """Periodic poll: fetch all quotes and dispatch to subscribers."""
        if not self._callbacks:
            return
        try:
            quotes = self._fetch_all()
            self._cache = quotes
            for sym, callbacks in list(self._callbacks.items()):
                quote = quotes.get(sym)
                if quote is not None:
                    for cb in callbacks:
                        try:
                            cb(quote)
                        except Exception:
                            pass
        except Exception:
            pass

    def _fetch_all(self) -> dict[str, Quote]:
        """Fetch all A-share spot quotes via AKShare."""
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            result: dict[str, Quote] = {}
            now = datetime.now()
            for _, row in df.iterrows():
                try:
                    code = str(row.get("代码", ""))
                    if not code:
                        continue
                    quote = Quote(
                        symbol=code,
                        timestamp=now,
                        name=str(row.get("名称", "")),
                        last_price=float(row.get("最新价", 0) or 0),
                        open=float(row.get("今开", 0) or 0),
                        high=float(row.get("最高", 0) or 0),
                        low=float(row.get("最低", 0) or 0),
                        pre_close=float(row.get("昨收", 0) or 0),
                        volume=int(row.get("成交量", 0) or 0),
                        amount=float(row.get("成交额", 0) or 0),
                        pct_chg=float(row.get("涨跌幅", 0) or 0) / 100.0,
                        turnover_rate=float(row.get("换手率", 0) or 0),
                    )
                    result[code] = quote
                except (ValueError, KeyError):
                    continue
            return result
        except ImportError:
            return {}
        except Exception:
            return {}
