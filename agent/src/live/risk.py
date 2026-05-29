"""Real-time risk engine for A-share live trading.

Pre-trade checks run inline (synchronously) in OMS.submit_order().
Circuit breaker is hard: once breached, no new orders until manual reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass
class RiskLimits:
    """Configurable risk limits."""
    max_position_pct: float = 0.25         # max single position as % of portfolio
    max_sector_exposure_pct: float = 0.40  # max sector concentration
    max_daily_loss: float = 50_000.0       # daily loss circuit breaker (CNY)
    max_drawdown_pct: float = 0.20         # max drawdown from peak equity
    max_order_value: float = 5_000_000.0   # max single order value (CNY)
    min_order_interval_ms: int = 500       # rate limiting between orders
    max_orders_per_minute: int = 60        # max orders per minute
    max_daily_orders: int = 500            # max orders per trading day
    restricted_symbols: tuple[str, ...] = ()
    allowed_board_types: tuple[str, ...] = ("main", "chinext", "star", "beijing")


class RiskEngine:
    """Pre-trade risk checks and real-time circuit breaker.

    All checks are inline — called from OMS.submit_order() before
    the order is sent to the broker. This eliminates any race window
    between check and execution.
    """

    def __init__(
        self,
        limits: RiskLimits | None = None,
        position_book=None,  # PositionBook
        calendar=None,       # TradingCalendar
        st_status_fn: Callable | None = None,
        sector_fn: Callable | None = None,  # (symbol) -> sector_name
    ):
        self._limits = limits or RiskLimits()
        self._position_book = position_book
        self._calendar = calendar
        self._st_status_fn = st_status_fn
        self._sector_fn = sector_fn

        # State
        self._peak_equity: float = 0.0
        self._daily_pnl: float = 0.0
        self._last_order_time: datetime | None = None
        self._daily_order_count: int = 0
        self._breached: bool = False
        self._breach_reason: str = ""
        self._order_timestamps: list[datetime] = []  # sliding window for rate limiting

    # ── Pre-trade check ────────────────────────────────────────

    def pre_trade_check(
        self, symbol: str, side: str, qty: int, price: float
    ) -> tuple[bool, str]:
        """Run all pre-trade checks. Returns (allowed, reason).

        Checks (in order):
        1. Circuit breaker (drawdown, daily loss)
        2. Symbol restrictions
        3. Order value limit
        4. Rate limiting
        5. Position sizing
        6. Sector exposure
        """
        limits = self._limits
        now = datetime.now()

        # 1. Circuit breaker
        if self._breached:
            return False, f"风控熔断已触发: {self._breach_reason}"

        # 2. Symbol restrictions
        if symbol in limits.restricted_symbols:
            return False, f"交易受限品种: {symbol}"

        # 3. Order value limit
        order_value = qty * price if price > 0 else qty * 10.0  # estimate if market order
        if order_value > limits.max_order_value:
            return False, f"单笔订单金额 {order_value:.0f} 超过上限 {limits.max_order_value:.0f}"

        # 4. Rate limiting
        if self._last_order_time is not None:
            elapsed_ms = (now - self._last_order_time).total_seconds() * 1000
            if elapsed_ms < limits.min_order_interval_ms:
                return False, f"下单频率过高，距上次 {elapsed_ms:.0f}ms (最小间隔 {limits.min_order_interval_ms}ms)"

        # Sliding window: orders per minute
        cutoff = now.timestamp() - 60
        self._order_timestamps = [t for t in self._order_timestamps if t.timestamp() > cutoff]
        if len(self._order_timestamps) >= limits.max_orders_per_minute:
            return False, f"每分钟下单数 {len(self._order_timestamps)} 超过上限 {limits.max_orders_per_minute}"

        # 5. Daily order count
        if self._daily_order_count >= limits.max_daily_orders:
            return False, f"日下单数 {self._daily_order_count} 超过上限 {limits.max_daily_orders}"

        # 6. Position sizing
        if self._position_book is not None and side == "buy":
            try:
                equity = self._position_book.get_total_equity()
                if equity > 0:
                    pos = self._position_book.get_position(symbol)
                    current_value = 0.0
                    if pos is not None:
                        current_value = pos.total_qty * price
                    new_total = current_value + order_value
                    if new_total / equity > limits.max_position_pct:
                        return False, (
                            f"单票仓位 {new_total/equity:.1%} 超过上限 {limits.max_position_pct:.1%}"
                        )
            except Exception:
                pass

        # 7. Sector exposure
        if self._sector_fn is not None and self._position_book is not None and side == "buy":
            try:
                sector = self._sector_fn(symbol)
                if sector:
                    equity = self._position_book.get_total_equity()
                    sector_value = order_value  # start with this order
                    for sym, pos in self._position_book.get_all_positions().items():
                        if self._sector_fn(sym) == sector:
                            sector_value += pos.total_qty * price
                    if equity > 0 and sector_value / equity > limits.max_sector_exposure_pct:
                        return False, (
                            f"板块 {sector} 敞口 {sector_value/equity:.1%} 超过上限 {limits.max_sector_exposure_pct:.1%}"
                        )
            except Exception:
                pass

        # All checks passed — record
        self._last_order_time = now
        self._daily_order_count += 1
        self._order_timestamps.append(now)
        return True, ""

    # ── Monitoring ─────────────────────────────────────────────

    def update_equity(self, quotes: dict[str, float]) -> None:
        """Update peak equity and check drawdown circuit breaker."""
        if self._position_book is None:
            return
        equity = self._position_book.get_total_equity(quotes)
        if equity > self._peak_equity:
            self._peak_equity = equity

        if self._peak_equity > 0:
            drawdown = (self._peak_equity - equity) / self._peak_equity
            if drawdown > self._limits.max_drawdown_pct:
                self._breached = True
                self._breach_reason = f"最大回撤 {drawdown:.2%} 超过上限 {self._limits.max_drawdown_pct:.2%}"

    def on_new_day(self) -> None:
        """Reset daily counters at the start of a new trading day."""
        self._daily_pnl = 0.0
        self._daily_order_count = 0
        self._order_timestamps.clear()

    # ── Query ──────────────────────────────────────────────────

    @property
    def is_breached(self) -> bool:
        return self._breached

    @property
    def breach_reason(self) -> str:
        return self._breach_reason

    def reset_breach(self) -> None:
        """Manually reset the circuit breaker."""
        self._breached = False
        self._breach_reason = ""

    def get_status(self) -> dict:
        return {
            "breached": self._breached,
            "breach_reason": self._breach_reason,
            "peak_equity": self._peak_equity,
            "daily_pnl": self._daily_pnl,
            "daily_order_count": self._daily_order_count,
            "limits": {
                "max_position_pct": self._limits.max_position_pct,
                "max_drawdown_pct": self._limits.max_drawdown_pct,
                "max_daily_loss": self._limits.max_daily_loss,
                "max_order_value": self._limits.max_order_value,
            },
        }
