"""Paper trading sandbox for simulated live A-share trading.

Wires real-time Quote objects into ChinaAEngineV2's execution models:
can_execute, can_fill, apply_slippage, _calc_commission, ASharePosition.

All fills are simulated. No real money is touched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable


from backtest.engines.china_a_v2 import (
    ChinaAEngineV2,
)
from .calendar import TradingCalendar
from .market_data import MarketDataProvider, Quote


@dataclass
class PaperOrder:
    """A paper trading order."""
    order_id: str
    symbol: str
    side: str              # "buy" | "sell"
    qty: int
    price: float           # limit price (0 for market orders)
    order_type: str        # "limit" | "market"
    status: str            # "pending", "submitted", "partial", "filled", "cancelled", "rejected"
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    reject_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    signal_source: str = ""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "order_type": self.order_type,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "commission": self.commission,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "signal_source": self.signal_source,
        }


class PaperTrader:
    """Simulated live trading sandbox.

    Subscribes to MarketDataProvider, feeds quotes through V2 engine
    execution models, and maintains simulated positions/cash.
    """

    def __init__(
        self,
        engine: ChinaAEngineV2,
        calendar: TradingCalendar,
        market_data: MarketDataProvider,
        initial_cash: float = 1_000_000.0,
        on_fill: Callable | None = None,
    ):
        self._engine = engine
        self._calendar = calendar
        self._market_data = market_data
        self._initial_cash = initial_cash

        # Enrich engine with live ST/suspension lookup if not set
        if engine._st_status_fn is None or engine._st_status_fn.__name__ == "<lambda>":
            engine._st_status_fn = lambda s, d: 0
        if engine._suspended_fn is None or engine._suspended_fn.__name__ == "<lambda>":
            engine._suspended_fn = lambda s, d: False

        self._cash: float = initial_cash
        self._pending_orders: dict[str, PaperOrder] = {}
        self._filled_orders: list[PaperOrder] = []
        self._subscribed_symbols: set[str] = set()
        self._on_fill = on_fill  # external fill callback (for PositionBook)

    # ── Order submission ───────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float = 0.0,
        order_type: str = "limit",
        signal_source: str = "",
    ) -> PaperOrder:
        """Submit a paper order.

        For market orders (price=0): fills at next available quote.
        For limit orders: fills when quote crosses the limit price.
        """
        now = datetime.now().isoformat()

        # Lot rounding
        qty = max(qty // 100 * 100, 100)

        order = PaperOrder(
            order_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_type=order_type,
            status="pending",
            created_at=now,
            updated_at=now,
            signal_source=signal_source,
        )

        # Pre-trade checks using V2 engine
        engine = self._engine
        engine.set_date(self._calendar.next_trading_day(date.today()) if not self._calendar.is_trading_day(date.today()) else date.today())

        # Get a quote to use as bar
        quote = self._market_data.get_realtime_quote(symbol)
        if quote is None:
            order.status = "rejected"
            order.reject_reason = f"No quote available for {symbol}"
            self._pending_orders[order.order_id] = order
            return order

        direction = 1 if side == "buy" else 0
        bar = quote.to_bar_series()

        # Rule check
        exec_result = engine.can_execute(symbol, direction, bar)
        if not exec_result.allowed:
            order.status = "rejected"
            order.reject_reason = exec_result.reason
            self._pending_orders[order.order_id] = order
            return order

        # Liquidity check (buy only)
        if direction == 1:
            fill_result = engine.can_fill(symbol, direction, bar)
            if not fill_result.allowed:
                order.status = "rejected"
                order.reject_reason = fill_result.reason
                self._pending_orders[order.order_id] = order
                return order

        # Cash check
        if side == "buy":
            required = qty * (price or quote.last_price) * 1.01  # 1% buffer
            if required > self._cash:
                order.status = "rejected"
                order.reject_reason = f"Insufficient cash: need {required:.0f}, have {self._cash:.0f}"
                self._pending_orders[order.order_id] = order
                return order

        # Position check for sells
        if side == "sell":
            pos = engine.get_position(symbol)
            if pos is None or pos.available_qty < qty:
                order.status = "rejected"
                order.reject_reason = f"Insufficient position: need {qty}, have {pos.available_qty if pos else 0}"
                self._pending_orders[order.order_id] = order
                return order

        order.status = "submitted"
        self._pending_orders[order.order_id] = order

        # Subscribe to quotes for this symbol
        if symbol not in self._subscribed_symbols:
            self._subscribed_symbols.add(symbol)
            self._market_data.subscribe([symbol], self._on_quote)

        # Market orders: try to fill immediately
        if order_type == "market":
            self._try_fill(order, quote)

        return order

    # ── Quote processing ───────────────────────────────────────

    def _on_quote(self, quote: Quote) -> None:
        """Handle incoming real-time quote: check pending orders for fills."""
        engine = self._engine
        trade_date = quote.timestamp.date()
        if self._calendar.is_trading_day(trade_date):
            engine.set_date(trade_date)

        # Check each pending order on this symbol
        for order in list(self._pending_orders.values()):
            if order.symbol != quote.symbol:
                continue
            if order.status not in ("submitted", "partial"):
                continue
            self._try_fill(order, quote)

    def _try_fill(self, order: PaperOrder, quote: Quote) -> None:
        """Attempt to fill a pending order against a quote."""
        if order.order_type == "limit" and order.price > 0:
            if order.side == "buy" and quote.last_price > order.price:
                return  # price too high for buy limit
            if order.side == "sell" and quote.last_price < order.price:
                return  # price too low for sell limit

        engine = self._engine
        direction = 1 if order.side == "buy" else 0
        bar = quote.to_bar_series()

        # Apply slippage
        order_value = order.qty * quote.last_price
        fill_price = engine.apply_slippage(
            quote.last_price, direction, bar, order_value=order_value
        )

        # Calculate commission
        commission = engine._calc_commission(
            order.qty, fill_price, is_open=(order.side == "buy")
        )

        # Execute
        if order.side == "buy":
            ok, reason = engine.execute_buy(order.symbol, order.qty, fill_price, bar)
            if ok:
                self._cash -= (order.qty * fill_price + commission)
        else:
            ok, reason = engine.execute_sell(order.symbol, order.qty, fill_price, bar)
            if ok:
                self._cash += (order.qty * fill_price - commission)

        if ok:
            order.status = "filled"
            order.filled_qty = order.qty
            order.avg_fill_price = fill_price
            order.commission = commission
            order.updated_at = datetime.now().isoformat()
            self._filled_orders.append(order)
            self._pending_orders.pop(order.order_id, None)

            if self._on_fill:
                self._on_fill(order)
        else:
            order.status = "rejected"
            order.reject_reason = reason
            order.updated_at = datetime.now().isoformat()

    # ── Order management ───────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        order = self._pending_orders.get(order_id)
        if order is None:
            return False
        if order.status in ("submitted", "partial"):
            order.status = "cancelled"
            order.updated_at = datetime.now().isoformat()
            self._pending_orders.pop(order_id, None)
            return True
        return False

    # ── Portfolio queries ──────────────────────────────────────

    def get_portfolio_snapshot(self) -> dict:
        """Current portfolio state."""
        positions = {}
        for sym, pos in self._engine._positions.items():
            positions[sym] = {
                "symbol": pos.symbol,
                "total_qty": pos.total_qty,
                "frozen_qty": pos.frozen_qty,
                "available_qty": pos.available_qty,
                "entry_price": pos.entry_price,
            }
        return {
            "cash": self._cash,
            "positions": positions,
            "pending_orders": len(self._pending_orders),
            "filled_orders": len(self._filled_orders),
        }

    def get_orders(self) -> list[dict]:
        """All orders (pending + filled)."""
        all_orders = list(self._pending_orders.values()) + self._filled_orders
        return [o.to_dict() for o in all_orders]

    def mark_new_day(self, trade_date: date) -> None:
        """Unfreeze T+1 positions for a new trading day."""
        self._engine.set_date(trade_date)
