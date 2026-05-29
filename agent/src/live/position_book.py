"""Real-time position tracking with T+1 awareness and Shadow Account integration.

Reuses ASharePosition from china_a_v2.py for T+1 buy/sell/unfreeze semantics.
Maintains a journal of all trades for Shadow Account post-hoc analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from backtest.engines.china_a_v2 import ASharePosition
from .calendar import TradingCalendar


@dataclass
class LivePosition:
    """Real-time position extending ASharePosition with live market data.

    T+1 tracking: delegates to internal ASharePosition.buy()/sell()/unfreeze().
    """
    symbol: str
    total_qty: int = 0
    frozen_qty: int = 0        # T+1 frozen (today's buys)
    available_qty: int = 0     # derived: total_qty - frozen_qty
    avg_cost: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0  # cumulative
    today_buys: int = 0        # qty bought today (for reporting)
    today_sells: int = 0       # qty sold today (for reporting)
    last_update: str = ""

    def refresh_market_value(self, last_price: float) -> None:
        """Update market_value and unrealized_pnl from current price."""
        self.market_value = self.total_qty * last_price
        self.unrealized_pnl = self.total_qty * (last_price - self.avg_cost)


@dataclass
class TradeRecord:
    """A single completed trade, compatible with Shadow Account format."""
    trade_id: str
    symbol: str
    side: str              # "buy" | "sell"
    qty: int
    price: float
    commission: float
    timestamp: str          # ISO 8601
    trade_date: str         # YYYY-MM-DD
    pnl: float = 0.0        # realized PnL (for sells)
    signal_source: str = ""  # strategy/skill that generated this trade


class PositionBook:
    """Real-time position bookkeeping.

    Integrates with:
    - ASharePosition (T+1 logic)
    - ShadowAccount storage (trade journal export)
    """

    def __init__(
        self,
        calendar: TradingCalendar | None = None,
        shadow_dir: str | None = None,
        initial_cash: float = 1_000_000.0,
    ):
        self._calendar = calendar or TradingCalendar()
        self._positions: dict[str, ASharePosition] = {}
        self._live_positions: dict[str, LivePosition] = {}
        self._trade_history: list[TradeRecord] = []
        self._cash: float = initial_cash
        self._frozen_cash: float = 0.0
        self._trade_counter: int = 0
        self._shadow_dir = shadow_dir

    # ── Position operations ────────────────────────────────────

    def on_fill(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        commission: float,
        timestamp: datetime,
        signal_source: str = "",
    ) -> TradeRecord:
        """Process a fill: update position and record trade."""
        self._trade_counter += 1
        trade_id = f"T{self._trade_counter:06d}"
        trade_date_str = timestamp.strftime("%Y-%m-%d")
        td = timestamp.date()

        # T+1 tracking via ASharePosition
        if symbol not in self._positions:
            self._positions[symbol] = ASharePosition(symbol=symbol)

        pos = self._positions[symbol]

        if side == "buy":
            pos.buy(qty, price, td)
            self._cash -= (qty * price + commission)
            self._frozen_cash += qty * price
        else:  # sell
            actual_qty = pos.sell(qty)
            if actual_qty <= 0:
                raise ValueError(f"No available shares to sell for {symbol}")
            self._cash += (actual_qty * price - commission)

        # Update LivePosition
        lp = self._live_positions.get(symbol)
        if lp is None:
            lp = LivePosition(symbol=symbol)
            self._live_positions[symbol] = lp

        lp.symbol = symbol
        lp.total_qty = pos.total_qty
        lp.frozen_qty = pos.frozen_qty
        lp.available_qty = pos.available_qty
        lp.avg_cost = pos.entry_price
        lp.last_update = timestamp.isoformat()

        if side == "buy":
            lp.today_buys += qty
        else:
            lp.today_sells += qty

        # Realized PnL for sells
        pnl = 0.0
        if side == "sell":
            pnl = qty * (price - lp.avg_cost) - commission
            lp.realized_pnl += pnl

        trade = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            commission=commission,
            timestamp=timestamp.isoformat(),
            trade_date=trade_date_str,
            pnl=pnl,
            signal_source=signal_source,
        )
        self._trade_history.append(trade)
        return trade

    def on_new_day(self, trade_date: date) -> None:
        """Unfreeze T+1 positions for a new trading day."""
        for pos in self._positions.values():
            pos.unfreeze()
        # Reset daily counters
        for lp in self._live_positions.values():
            lp.today_buys = 0
            lp.today_sells = 0
            lp.frozen_qty = 0
            lp.available_qty = lp.total_qty  # all available after unfreeze
        self._frozen_cash = 0.0

    # ── Query ──────────────────────────────────────────────────

    def get_position(self, symbol: str) -> LivePosition | None:
        return self._live_positions.get(symbol)

    def get_all_positions(self) -> dict[str, LivePosition]:
        return dict(self._live_positions)

    def get_available_cash(self) -> float:
        return self._cash

    def get_total_equity(self, quotes: dict[str, float] | None = None) -> float:
        """Calculate total equity = cash + market value of positions."""
        equity = self._cash
        if quotes:
            for sym, lp in self._live_positions.items():
                price = quotes.get(sym)
                if price is not None:
                    equity += lp.total_qty * price
        return equity

    def get_trade_history(self) -> list[TradeRecord]:
        return list(self._trade_history)

    # ── Shadow Account integration ─────────────────────────────

    def to_shadow_journal(self) -> list[dict]:
        """Export trade history in Shadow Account-compatible format."""
        return [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "price": t.price,
                "commission": t.commission,
                "timestamp": t.timestamp,
                "trade_date": t.trade_date,
                "pnl": t.pnl,
                "signal_source": t.signal_source,
            }
            for t in self._trade_history
        ]
